"""Read Warsaw sale listings from schema.org JSON-LD in HTML.

This is a generic structured-data adapter, not a verified integration with a
particular portal. Pages that expose data only through JavaScript, a private
API, or site-specific markup require a separate adapter. No browser runtime,
captcha solving, or proxy rotation is used.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup


LOGGER = logging.getLogger(__name__)
FIELDS = (
    "source", "listing_id", "url", "city", "district", "price_pln", "area_m2",
    "rooms", "floor", "build_year", "latitude", "longitude", "distance_km",
    "observed_at",
)
PROPERTY_TYPES = {"Apartment"}
LISTING_TYPES = PROPERTY_TYPES | {"RealEstateListing", "Offer"}
NON_APARTMENT_TYPES = {"House", "SingleFamilyResidence", "Residence", "DetachedHouse"}
WARSAW_CENTRE = (52.2297, 21.0122)  # A fixed reference point, not travel distance.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUEST_TIMEOUT = 30
MAX_ATTEMPTS = 3
MAX_REDIRECTS = 3
TRACKING_KEYS = {"fbclid", "gclid", "msclkid", "dclid", "yclid"}


class FetchError(RuntimeError):
    """Collection cannot safely or meaningfully continue."""


def canonical_url(url, base_url=None):
    """Resolve a URL and remove fragments and known tracking parameters only."""
    parts = urlsplit(urljoin(base_url or "", str(url)))
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("URL musi być adresem HTTP lub HTTPS.")
    if parts.username or parts.password:
        raise ValueError("URL nie może zawierać loginu ani hasła.")
    query = [
        (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_KEYS
    ]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", urlencode(query), ""))


def _origin(url):
    parts = urlsplit(url)
    return parts.scheme.lower(), parts.hostname.lower(), parts.port or (443 if parts.scheme == "https" else 80)


def _types(node):
    values = node.get("@type", [])
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return set()
    return {str(value).rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1] for value in values}


def _walk(value):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk(nested)


def _jsonld(html):
    soup = BeautifulSoup(html, "html.parser")
    documents = []
    for script in soup.find_all("script"):
        if str(script.get("type", "")).lower().split(";")[0].strip() != "application/ld+json":
            continue
        try:
            documents.append(json.loads(script.string or script.get_text()))
        except (json.JSONDecodeError, TypeError):
            LOGGER.warning("Pominięto nieprawidłowy blok JSON-LD.")
    nodes = [node for document in documents for node in _walk(document)]
    index = {}
    for node in nodes:
        if node.get("@id"):
            key = str(node["@id"])
            # A later {"@id": "#offer"} reference must not replace its definition.
            index[key] = {**index.get(key, {}), **node}
    return soup, nodes, index


def _object(value, index):
    if isinstance(value, list):
        return _object(value[0], index) if value else {}
    if isinstance(value, str):
        return index.get(value, {})
    if not isinstance(value, dict):
        return {}
    return {**index.get(str(value.get("@id", "")), {}), **value}


def _number(value, *, money=False):
    """Read finite numbers, including Polish spaces, decimal commas and PLN."""
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = re.sub(r"\s+", "", value)
        text = re.sub(r"(?:PLN|zł|m²|m2|km)$", "", text, flags=re.IGNORECASE)
        if not re.fullmatch(r"[+-]?\d+(?:[.,]\d+)*", text):
            return None
        if "," in text and "." in text:
            decimal = "," if text.rfind(",") > text.rfind(".") else "."
            thousands = "." if decimal == "," else ","
            text = text.replace(thousands, "").replace(decimal, ".")
        elif text.count(",") > 1 or text.count(".") > 1:
            separator = "," if "," in text else "."
            groups = text.lstrip("+-").split(separator)
            if not all(len(group) == 3 for group in groups[1:]):
                return None
            text = text.replace(separator, "")
        elif money and re.fullmatch(r"\d{1,3}\.\d{3}", text):
            text = text.replace(".", "")
        else:
            text = text.replace(",", ".")
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _integer(value, *, minimum=0, maximum=None):
    number = _number(value)
    if number is None or number != int(number) or number < minimum:
        return None
    return int(number) if maximum is None or number <= maximum else None


def _observed_at(value):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observed_at musi być datą ze strefą czasową.")
    return value.astimezone(timezone.utc)


def _is_rental(*nodes):
    for node in nodes:
        if not node:
            continue
        function = str(node.get("businessFunction", "")).lower()
        if "lease" in function or "rent" in function or node.get("leaseLength"):
            return True
        text = " ".join(str(node.get(key, "")) for key in ("name", "category", "description")).lower()
        if re.search(r"\b(?:wynajem|wynajęcia|wynajecia|rent|rental)\b", text):
            return True
        specification = node.get("priceSpecification", {})
        specifications = specification if isinstance(specification, list) else [specification]
        for spec in specifications:
            if isinstance(spec, dict):
                period = str(spec.get("unitText", "")) + str(spec.get("billingDuration", ""))
                if re.search(r"month|monthly|miesiąc|miesiac|/mo|P1M", period, re.IGNORECASE):
                    return True
    return False


def _identifier(value):
    if isinstance(value, dict):
        value = value.get("value", value.get("@id"))
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return str(value).strip() or None
    return None


def _distance(latitude, longitude):
    try:
        centre_latitude = float(os.environ.get("WARSAW_CENTER_LAT", WARSAW_CENTRE[0]))
        centre_longitude = float(os.environ.get("WARSAW_CENTER_LON", WARSAW_CENTRE[1]))
    except ValueError:
        raise ValueError("WARSAW_CENTER_LAT/LON muszą zawierać liczby.") from None
    if not math.isfinite(centre_latitude) or not -90 <= centre_latitude <= 90 or not math.isfinite(centre_longitude) or not -180 <= centre_longitude <= 180:
        raise ValueError("WARSAW_CENTER_LAT/LON mają nieprawidłowe współrzędne.")
    lat1, lon1 = map(math.radians, (centre_latitude, centre_longitude))
    lat2, lon2 = map(math.radians, (latitude, longitude))
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return round(6371.0088 * 2 * math.asin(math.sqrt(min(1, max(0, a)))), 4)


def _record(node, index, page_url, observed_at):
    types = _types(node)
    offer = node if "Offer" in types else _object(node.get("offers"), index)
    apartment = _object(offer.get("itemOffered"), index)
    if not apartment:
        apartment = _object(node.get("mainEntity", node.get("about")), index)
    if not apartment:
        apartment = node
    if _types(apartment) & NON_APARTMENT_TYPES:
        return None
    if not offer:
        offer = _object(apartment.get("offers"), index)
    if _is_rental(node, offer, apartment):
        return None

    address = _object(apartment.get("address", node.get("address")), index)
    city = str(address.get("addressLocality", "")).strip()
    if city.casefold() not in {"warszawa", "warsaw"}:
        return None
    district = apartment.get("district") or address.get("district") or address.get("addressDistrict")

    specification = _object(offer.get("priceSpecification"), index)
    quantity = _object(specification.get("referenceQuantity"), index)
    unit = str(specification.get("unitText", "")) + " " + str(specification.get("unitCode", "")) + " " + str(quantity.get("unitText", "")) + " " + str(quantity.get("unitCode", ""))
    if re.search(r"m²|m2|\bMTK\b|square.?met(?:er|re)|sqm", unit, re.IGNORECASE):
        # The target is the price of the whole apartment, not PLN per m².
        return None
    raw_price = offer.get("price", specification.get("price", node.get("price")))
    currency = offer.get("priceCurrency", specification.get("priceCurrency", node.get("priceCurrency")))
    if not currency and isinstance(raw_price, str) and re.search(r"PLN|zł", raw_price, re.IGNORECASE):
        currency = "PLN"
    price = _number(raw_price, money=True)
    if str(currency).upper() != "PLN" or price is None or price <= 0:
        return None

    size = apartment.get("floorSize", node.get("floorSize"))
    if isinstance(size, dict):
        unit = str(size.get("unitCode", size.get("unitText", "MTK"))).strip().lower()
        if unit not in {"mtk", "m2", "m²", "sqm", "square meter", "square metre"}:
            return None
    area = _number(size)
    # Bedrooms are not a substitute for the number of rooms.
    rooms = _integer(apartment.get("numberOfRooms", node.get("numberOfRooms")), minimum=1)
    if area is None or area <= 0 or rooms is None:
        return None

    raw_url = node.get("url") or apartment.get("url") or offer.get("url")
    if not raw_url:
        main_page = node.get("mainEntityOfPage")
        raw_url = main_page.get("@id") if isinstance(main_page, dict) else main_page
    if not raw_url:
        node_id = node.get("@id", "")
        raw_url = node_id if str(node_id).startswith(("http://", "https://", "/")) else page_url
    try:
        url = canonical_url(raw_url, page_url)
    except ValueError:
        return None
    listing_id = _identifier(node.get("identifier")) or _identifier(apartment.get("identifier")) or _identifier(offer.get("identifier"))
    if listing_id is None:
        listing_id = "url_" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]

    geo = _object(apartment.get("geo", node.get("geo")), index)
    latitude = _number(geo.get("latitude"))
    longitude = _number(geo.get("longitude"))
    if latitude is not None and not -90 <= latitude <= 90:
        latitude = None
    if longitude is not None and not -180 <= longitude <= 180:
        longitude = None
    distance = _number(apartment.get("distance_km", apartment.get("centreDistance_km")))
    if distance is not None and distance < 0:
        distance = None
    if distance is None and latitude is not None and longitude is not None:
        distance = _distance(latitude, longitude)

    floor_value = apartment.get("floorLevel", node.get("floorLevel"))
    floor = 0 if str(floor_value).strip().casefold() in {"parter", "ground floor"} else _integer(floor_value, minimum=-5)
    build_year = _integer(apartment.get("yearBuilt", node.get("yearBuilt")), minimum=1800, maximum=datetime.now(timezone.utc).year + 1)
    return {
        "source": urlsplit(page_url).hostname.lower(), "listing_id": listing_id,
        "url": url, "city": "Warszawa", "district": str(district).strip() if district else None,
        "price_pln": price, "area_m2": area, "rooms": rooms, "floor": floor,
        "build_year": build_year, "latitude": latitude, "longitude": longitude,
        "distance_km": distance, "observed_at": observed_at,
    }


def parse_listings(html, page_url, observed_at=None):
    """Return validated Warsaw sale records; malformed/incomplete nodes are skipped.

    observed_at is a UTC datetime representing collection time, not publication
    time. No sale status is invented: explicit rental metadata is rejected, but
    a source must still be checked to ensure its selected URL contains sales.
    """
    page_url = canonical_url(page_url)
    timestamp = _observed_at(observed_at)
    _, nodes, index = _jsonld(html)
    records = {}
    # Resolve the entity once, rather than counting its listing, offer and
    # apartment as separate observations. Rental context also stays attached.
    candidates = sorted((node for node in nodes if _types(node) & LISTING_TYPES), key=lambda node: 0 if "RealEstateListing" in _types(node) else 1 if "Offer" in _types(node) else 2)
    consumed_objects = set()
    consumed_refs = set()
    for node in candidates:
        node_ref = node.get("@id") if isinstance(node.get("@id"), str) else None
        if id(node) in consumed_objects or node_ref in consumed_refs:
            continue
        pending = list(_walk(node))[1:]
        while pending:
            child = pending.pop()
            child_id = child.get("@id") if isinstance(child.get("@id"), str) else None
            if id(child) in consumed_objects or (child_id and child_id in consumed_refs):
                continue
            consumed_objects.add(id(child))
            if child_id:
                consumed_refs.add(child_id)
                if child_id in index:
                    pending.extend(list(_walk(index[child_id]))[1:])
        record = _record(node, index, page_url, timestamp)
        if record:
            records.setdefault((record["source"], record["listing_id"]), record)
    return list(records.values())


def _detail_urls(html, page_url):
    _, nodes, index = _jsonld(html)
    urls = []
    for node in nodes:
        if "ItemList" not in _types(node):
            continue
        elements = node.get("itemListElement", [])
        for element in elements if isinstance(elements, list) else [elements]:
            item = _object(element, index)
            if "ListItem" in _types(item):
                item = item.get("item", item.get("url"))
            if isinstance(item, str):
                raw_url = item
            else:
                item = _object(item, index)
                raw_url = item.get("url") or item.get("@id")
            if not raw_url:
                continue
            try:
                url = canonical_url(raw_url, page_url)
            except ValueError:
                continue
            if _origin(url) == _origin(page_url) and url != canonical_url(page_url) and url not in urls:
                urls.append(url)
    return urls


def _next_url(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    link = soup.find(lambda tag: tag.name in {"a", "link"} and "next" in (tag.get("rel") or []))
    if not link or not link.get("href"):
        return None
    try:
        url = canonical_url(link["href"], page_url)
    except ValueError:
        return None
    return url if _origin(url) == _origin(page_url) else None


def _fetch_html(session, url, *, origin, delay, sleep):
    """Retry transient failures, with bounded same-origin redirects and timeout."""
    # Importing exceptions creates no session and no network requests.
    from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError, Timeout as CurlTimeout

    for redirect in range(MAX_REDIRECTS + 1):
        for attempt in range(MAX_ATTEMPTS):
            if delay:
                sleep(delay * 2**attempt)
            try:
                response = session.get(url, impersonate="chrome120", timeout=REQUEST_TIMEOUT, allow_redirects=False)
            except (CurlConnectionError, CurlTimeout, TimeoutError, ConnectionError) as exc:
                if attempt + 1 == MAX_ATTEMPTS:
                    raise FetchError("Nie udało się pobrać strony po 3 próbach (połączenie lub timeout).") from exc
                continue
            if response.status_code in {403, 429}:
                raise FetchError(f"Serwis zwrócił HTTP {response.status_code}; zbieranie zatrzymano. Sprawdź dostęp i zasady źródła.")
            if 500 <= response.status_code < 600:
                if attempt + 1 == MAX_ATTEMPTS:
                    raise FetchError(f"Serwis zwrócił HTTP {response.status_code} po 3 próbach.")
                continue
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                if not location:
                    raise FetchError("Przekierowanie bez adresu Location.")
                target = canonical_url(location, url)
                if _origin(target) != origin:
                    raise FetchError("Przekierowanie do innej domeny zatrzymano; wybierz właściwy URL źródła.")
                url = target
                break
            if not 200 <= response.status_code < 300:
                raise FetchError(f"Nie udało się pobrać strony: HTTP {response.status_code}.")
            return response.text, url
        else:
            raise FetchError("Nie udało się pobrać strony.")
    raise FetchError("Zbyt wiele przekierowań.")


def fetch_listings(url, *, source=None, max_pages=2, max_listings=100, delay=2.0, session=None, sleep=time.sleep):
    """Fetch up to max_pages index pages and max_listings same-origin details.

    Both attempted detail URLs and returned records are bounded. A session and
    sleep can be supplied for offline tests. HTML/JSON-LD parsing does not run JS.
    """
    if max_pages < 1 or max_listings < 1 or not math.isfinite(delay) or delay < 0:
        raise ValueError("Limity muszą być dodatnie, a opóźnienie nieujemne i skończone.")
    url = canonical_url(url)
    origin = _origin(url)
    own_session = session is None
    if own_session:
        from curl_cffi import requests
        session = requests.Session()
    records = {}
    visited = set()
    detail_attempts = 0
    timestamp = datetime.now(timezone.utc)
    try:
        for _ in range(max_pages):
            if url in visited or len(records) >= max_listings:
                break
            visited.add(url)
            html, actual_url = _fetch_html(session, url, origin=origin, delay=delay, sleep=sleep)
            visited.add(actual_url)
            for record in parse_listings(html, actual_url, timestamp):
                if source:
                    record["source"] = source
                records[(record["source"], record["listing_id"])] = record
                if len(records) >= max_listings:
                    break
            for detail_url in _detail_urls(html, actual_url):
                if len(records) >= max_listings or detail_attempts >= max_listings:
                    break
                if detail_url in visited or any(record["url"] == detail_url for record in records.values()):
                    continue
                visited.add(detail_url)
                detail_attempts += 1
                detail_html, actual_detail_url = _fetch_html(session, detail_url, origin=origin, delay=delay, sleep=sleep)
                visited.add(actual_detail_url)
                for record in parse_listings(detail_html, actual_detail_url, timestamp):
                    if source:
                        record["source"] = source
                    records[(record["source"], record["listing_id"])] = record
                    if len(records) >= max_listings:
                        break
            url = _next_url(html, actual_url)
            if not url:
                break
    finally:
        if own_session:
            session.close()
    if not records:
        raise FetchError("Brak kompletnych ofert sprzedaży Warszawy w JSON-LD. Źródło może wymagać własnego adaptera lub JavaScript; integracja tego portalu nie jest potwierdzona.")
    return list(records.values())


def _write_csv(records, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDS)
        writer.writeheader()
        for record in records:
            writer.writerow({**record, "observed_at": record["observed_at"].isoformat()})


def main(argv=None):
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description="Pobierz oferty Warszawy ze zgodnego JSON-LD; wybrany portal wymaga sprawdzenia.")
    parser.add_argument("--url", default=os.environ.get("LISTINGS_URL"), help="Adres strony ofert; alternatywnie LISTINGS_URL.")
    parser.add_argument("--source", help="Nazwa źródła; domyślnie hostname URL.")
    parser.add_argument("--max-pages", type=int, default=os.environ.get("MAX_PAGES", "2"))
    parser.add_argument("--max-listings", type=int, default=os.environ.get("MAX_LISTINGS", "100"))
    parser.add_argument("--delay", type=float, default=os.environ.get("REQUEST_DELAY_SECONDS", "2"))
    parser.add_argument("--html", type=Path, help="Zapisane HTML do lokalnego testu, bez sieci.")
    parser.add_argument("--output", type=Path, help="Opcjonalny eksport rekordów do CSV.")
    parser.add_argument("--no-db", action="store_true", help="Pomiń zapis do bazy, np. podczas testu offline.")
    args = parser.parse_args(argv)
    if args.max_pages < 1 or args.max_listings < 1 or not math.isfinite(args.delay) or args.delay < 0:
        parser.error("Limity muszą być dodatnie, a opóźnienie nieujemne i skończone.")
    if not args.url and not args.html:
        parser.error("Ustaw LISTINGS_URL lub --url po wybraniu źródła; do testu offline użyj --html.")
    try:
        if args.html:
            page_url = args.url or "https://fixtures.example/warszawa/demo"
            records = parse_listings(args.html.read_text(encoding="utf-8"), page_url)[:args.max_listings]
            if not records:
                raise FetchError("Plik HTML nie zawiera kompletnych ofert Warszawy w obsługiwanym JSON-LD.")
            if args.source:
                for record in records:
                    record["source"] = args.source
        else:
            records = fetch_listings(args.url, source=args.source, max_pages=args.max_pages, max_listings=args.max_listings, delay=args.delay)
        if args.output:
            _write_csv(records, args.output)
            print(f"CSV: {args.output}")
        print(f"Pobrano poprawnych ofert: {len(records)}.")
        if not args.no_db:
            if __package__:
                from .database import get_engine, init_db, upsert_listings
            else:
                from database import get_engine, init_db, upsert_listings
            from sqlalchemy.exc import SQLAlchemyError

            engine = None
            try:
                engine = get_engine()
                init_db(engine)
                inserted = upsert_listings(engine, records)
            except SQLAlchemyError:
                raise FetchError("Nie udało się zapisać do bazy. Sprawdź konfigurację połączenia i dostęp do serwera.") from None
            finally:
                if engine is not None:
                    engine.dispose()
            print(f"Zapisano nowych obserwacji w bazie: {inserted}.")
        return 0
    except (FetchError, ValueError, OSError) as exc:
        print(f"Błąd: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
