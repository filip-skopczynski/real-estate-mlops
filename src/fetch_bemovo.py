"""Collect the verified Bemovo pilot: public government prices + website features.

This adapter is deliberately restricted to one developer, project and address.
The default CLI writes a local snapshot; cloud writes require --save-db.
"""
from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import time
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from curl_cffi import requests
from dotenv import load_dotenv

from src.bemovo_features import parse_bemovo_features
from src.bemovo_prices import parse_bemovo_prices
from src.fetch_data import FIELDS

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET_ID = "39940"
DATASET_URL = f"https://api.dane.gov.pl/1.4/datasets/{DATASET_ID}"
RESOURCE_URL = DATASET_URL + "/resources?per_page=1&sort=-data_date"
FEATURES_URL = "https://bemovo.pl/pl/"
WARSAW = ZoneInfo("Europe/Warsaw")
ALLOWED_HOSTS = {"api.dane.gov.pl", "dane.rejestr-cen-nieruchomosci.pl", "bemovo.pl"}
MAX_BYTES = 5_000_000


class BemovoError(ValueError):
    """The source is unavailable or no longer satisfies its verified contract."""


def _timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise BemovoError("Observation time must include a timezone.")
    return value.astimezone(timezone.utc)


def combine_bemovo_records(prices, features, *, observed_at: datetime, snapshot_date: date):
    """Require a complete, consistent available inventory before emitting rows."""
    observed_at = _timestamp(observed_at)
    if observed_at.astimezone(WARSAW).date() != snapshot_date:
        raise BemovoError("The government snapshot date differs from the observation day.")
    price_index = {row["unit_number"]: row for row in prices}
    feature_index = {row["number"]: row for row in features}
    if len(price_index) != len(prices) or len(feature_index) != len(features):
        raise BemovoError("Duplicate apartment identities prevent a reliable join.")
    if not prices or not features:
        raise BemovoError("The source contains no apartment prices or features.")
    if set(price_index) - set(feature_index):
        raise BemovoError("Some government apartments have no matching website features.")
    available = [row for row in features if row["status"] == "available" and not row["is_commercial_unit"]]
    if not available:
        raise BemovoError("No available residential apartments were found.")
    if {row["number"] for row in available} - set(price_index):
        raise BemovoError("Some available apartments have no government price.")
    records = []
    for unit, price in price_index.items():
        feature = feature_index[unit]
        if feature["is_commercial_unit"]:
            raise BemovoError("A government apartment matched a commercial unit.")
        if feature["status"] != "available":
            continue
        if feature["city"] != "Warszawa" or feature["investment"] != "Bemovo PH1":
            raise BemovoError("The apartment belongs to an unexpected city or project.")
        if price["developer_nip"] != "5252801624" or price["building_number"] != "95":
            raise BemovoError("The price belongs to an unexpected developer or address.")
        if not price["valid_from"] <= observed_at <= price["valid_until"]:
            raise BemovoError("An apartment price is not valid at observation time.")
        if abs(Decimal(str(price["price_pln"])) - Decimal(str(feature["website_price_pln"]))) > Decimal("0.01"):
            raise BemovoError("Government and website apartment prices disagree.")
        records.append({
            "source": "dane.gov.pl:39940",
            "listing_id": "5252801624:Bemovo PH1:" + unit,
            "url": feature["feature_url"],
            "city": "Warszawa", "district": "Bemowo",
            "price_pln": price["price_pln"],
            "area_m2": feature["area_m2"], "rooms": feature["rooms"], "floor": feature["floor"],
            "build_year": None, "latitude": None, "longitude": None, "distance_km": None,
            "observed_at": observed_at,
        })
    records.sort(key=lambda row: row["listing_id"])
    report = {
        "dataset_id": DATASET_ID, "snapshot_date": snapshot_date.isoformat(),
        "observed_at": observed_at.isoformat(), "market": "primary",
        "price_interpretation": "gross apartment asking price; parking/extras excluded",
        "csv_apartments": len(prices), "website_units": len(features),
        "website_status_counts": {status: sum(row["status"] == status for row in features) for status in sorted({row["status"] for row in features})},
        "website_commercial_units": sum(row["is_commercial_unit"] for row in features),
        "matched_available_apartments": len(records), "price_mismatches": 0,
        "missing_optional_features": ["build_year", "latitude", "longitude", "distance_km"],
        "area_provenance": "website area; never reconstructed from price",
        "scope": "one investment; not a validation of Warsaw-wide prediction quality",
    }
    return records, report


def _allowed_url(url):
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS or parts.username or parts.password or parts.port not in {None, 443}:
        raise BemovoError("The source redirected to an unexpected address.")


def _read_text(session, url, *, delay, sleep):
    """Bound requests, retries, redirects and response size; stop on 403/429."""
    _allowed_url(url)
    for attempt in range(3):
        current = url
        for redirect in range(4):
            if delay:
                sleep(delay)
            try:
                response = session.get(current, impersonate="chrome120", timeout=30, allow_redirects=False)
            except requests.RequestsError:
                if attempt == 2:
                    raise BemovoError("The source could not be reached after bounded retries.") from None
                break
            status = response.status_code
            if status in {403, 429}:
                raise BemovoError(f"The source returned HTTP {status}; collection stopped.")
            if status in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location") or response.headers.get("location")
                if not location or redirect == 3:
                    raise BemovoError("Invalid or excessive source redirects.")
                current = urljoin(current, location)
                _allowed_url(current)
                continue
            if 500 <= status < 600:
                if attempt == 2:
                    raise BemovoError("The source repeatedly returned a server error.")
                break
            if status != 200:
                raise BemovoError(f"The source returned HTTP {status}.")
            content = response.content
            if len(content) > MAX_BYTES:
                raise BemovoError("The source response exceeds the size limit.")
            try:
                return content.decode("utf-8-sig")
            except UnicodeError:
                raise BemovoError("The source did not return expected UTF-8 text.") from None
    raise BemovoError("The source could not be read.")


def collect_bemovo(*, session=None, delay=2.0, sleep=time.sleep, observed_at=None):
    """Read public metadata, one CSV and one HTML page; never run website JS."""
    if not math.isfinite(delay) or delay < 0:
        raise BemovoError("Request delay must be finite and non-negative.")
    own_session = session is None
    session = session or requests.Session()
    try:
        dataset = json.loads(_read_text(session, DATASET_URL, delay=delay, sleep=sleep))["data"]
        if str(dataset["id"]) != DATASET_ID or dataset["attributes"].get("license_name") != "CC0 1.0":
            raise BemovoError("The dataset identity or published reuse terms changed.")
        resources = json.loads(_read_text(session, RESOURCE_URL, delay=delay, sleep=sleep))["data"]
        if not isinstance(resources, list) or len(resources) != 1:
            raise BemovoError("The latest government resource could not be identified.")
        resource = resources[0]
        attributes = resource["attributes"]
        snapshot_date = date.fromisoformat(attributes["data_date"])
        expected_day = _timestamp(observed_at or datetime.now(timezone.utc)).astimezone(WARSAW).date()
        if snapshot_date != expected_day:
            raise BemovoError("The latest government resource is not for the current Warsaw day.")
        csv_url = attributes["download_url"]
        if urlsplit(csv_url).hostname != "api.dane.gov.pl":
            raise BemovoError("Expected the official government resource download URL.")
        csv_text = _read_text(session, csv_url, delay=delay, sleep=sleep)
        html = _read_text(session, FEATURES_URL, delay=delay, sleep=sleep)
        captured_at = _timestamp(observed_at or datetime.now(timezone.utc))
        prices = parse_bemovo_prices(csv_text, as_of_date=snapshot_date)
        features = parse_bemovo_features(html)
        records, report = combine_bemovo_records(prices, features, observed_at=captured_at, snapshot_date=snapshot_date)
        report.update({
            "dataset_url": "https://dane.gov.pl/pl/dataset/39940", "resource_id": str(resource["id"]),
            "price_download_url": csv_url, "features_url": FEATURES_URL,
            "license_name": dataset["attributes"]["license_name"],
            "csv_sha256": hashlib.sha256(csv_text.encode("utf-8")).hexdigest(),
            "html_sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(),
        })
        return records, report
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise BemovoError("The published source format changed; no data were written.") from None
    finally:
        if own_session:
            session.close()


def _write_snapshot(records, report, output, report_output):
    output.parent.mkdir(parents=True, exist_ok=True)
    report_output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows({**row, "observed_at": row["observed_at"].isoformat()} for row in records)
    report_output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv=None):
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description="Sprawdź i pobierz kompletne mieszkania pilotażowej inwestycji Bemovo.")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "data" / "bemovo.csv")
    parser.add_argument("--report-output", type=Path, default=PROJECT_ROOT / "data" / "bemovo_audit.json")
    parser.add_argument("--delay", type=float, default=os.getenv("REQUEST_DELAY_SECONDS", "2"))
    parser.add_argument("--save-db", action="store_true", help="Zapisz zweryfikowane obserwacje również w PostgreSQL.")
    args = parser.parse_args(argv)
    engine = None
    try:
        records, report = collect_bemovo(delay=args.delay)
        if args.save_db:
            from sqlalchemy.exc import SQLAlchemyError
            from src.database import get_engine, init_db, upsert_listings
            try:
                engine = get_engine()
                init_db(engine)
                inserted = upsert_listings(engine, records)
            except SQLAlchemyError:
                raise BemovoError("Nie udało się zapisać danych do bazy; sprawdź lokalną konfigurację połączenia.") from None
            report["database_new_observations"] = inserted
        _write_snapshot(records, report, args.output, args.report_output)
        print(f"Zweryfikowane dostępne mieszkania: {len(records)}. Niezgodności cen: 0.")
        print(f"CSV: {args.output}")
        print(f"Raport: {args.report_output}")
        if args.save_db:
            print(f"Nowe obserwacje w bazie: {inserted}.")
        return 0
    except BemovoError as error:
        print(f"Błąd źródła: {error}")
        return 1
    except (ValueError, OSError):
        print("Nie zakończono pobierania. Sprawdź dostępność źródła, jego aktualność i zgodność danych.")
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
