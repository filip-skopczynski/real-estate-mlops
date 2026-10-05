"""Narrow adapter for the official GH Development 7 / Bemovo apartment CSV.

This module does not fetch data. It parses base apartment asking prices, never
adds parking/extras and never derives floor area from the two price columns.
"""

from __future__ import annotations

import csv
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from io import StringIO
import math
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


WARSAW = ZoneInfo("Europe/Warsaw")
EXPECTED_NIP = "5252801624"
EXPECTED_HOST = "bemovo.pl"
EXPECTED_CITY = "Warszawa"
EXPECTED_STREET = "Batalionów Chłopskich"
EXPECTED_BUILDING = "95"
HEADERS = {
    "nip": "NIP",
    "developer_url": "Adres strony internetowej dewelopera",
    "city": "Miejscowość lokalizacji przedsięwzięcia deweloperskiego lub zadania inwestycyjnego",
    "street": "Ulica lokalizacji przedsięwzięcia deweloperskiego lub zadania inwestycyjnego",
    "building_number": "Nr nieruchomości lokalizacji przedsięwzięcia deweloperskiego lub zadania inwestycyjnego",
    "property_type": "Rodzaj nieruchomości: lokal mieszkalny, dom jednorodzinny",
    "unit_number": "Nr lokalu lub domu jednorodzinnego nadany przez dewelopera",
    "valid_from": "Data od której obowiązuje oferta",
    "valid_until": "Data do której obowiązuje oferta",
}
POSTAL_CODE_HEADER = "Kod pocztowy lokalizacji przedsięwzięcia deweloperskiego lub zadania inwestycyjnego"
BASE_PRICE_PREFIX = (
    "Cena lokalu mieszkalnego lub domu jednorodzinnego będących przedmiotem umowy "
    "stanowiąca iloczyn ceny m2 oraz powierzchni"
)
PRICE_PER_M2_PREFIX = "Cena m 2 powierzchni"
EXCLUDED_TYPES = {"X", "Miejsce postojowe", "Komórka lokatorska"}


def _positive_price(value: str, field: str, row_number: int) -> float:
    # The source uses ungrouped decimal-dot numbers. Spaces/decimal commas are
    # also accepted; punctuation with both comma and dot is deliberately ambiguous.
    text = value.strip().replace("\u00a0", "").replace(" ", "")
    if "," in text and "." in text:
        raise ValueError(f"Row {row_number}: ambiguous numeric punctuation in {field}.")
    try:
        amount = Decimal(text.replace(",", "."))
        if not amount.is_finite() or amount <= 0:
            raise ValueError(f"Row {row_number}: {field} must be positive and finite.")
        result = float(amount)
        if not math.isfinite(result) or result <= 0:
            raise ValueError(f"Row {row_number}: {field} must remain positive and finite as a float.")
        return result
    except (InvalidOperation, OverflowError):
        raise ValueError(f"Row {row_number}: invalid numeric value in {field}.") from None


def _aware_timestamp(value: str, field: str, row_number: int) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"Row {row_number}: {field} must be an ISO timestamp with timezone.") from None
    offset = parsed.utcoffset()
    if offset is None:
        raise ValueError(f"Row {row_number}: {field} must contain an explicit timezone.")
    # This adapter accepts Warsaw local offsets or explicit UTC. A mismatched
    # +01/+02 offset (including a nonexistent spring-clock time) is rejected.
    if offset != timedelta(0):
        local = parsed.astimezone(WARSAW)
        if offset not in {timedelta(hours=1), timedelta(hours=2)} or (
            local.utcoffset() != offset
            or local.replace(tzinfo=None) != parsed.replace(tzinfo=None)
        ):
            raise ValueError(f"Row {row_number}: {field} timezone does not match Europe/Warsaw.")
    return parsed.astimezone(timezone.utc)


def parse_bemovo_prices(csv_text: str, *, as_of_date: date) -> list[dict]:
    """Return validated Bemovo apartment base prices applicable on a Warsaw day.

    Every included apartment must pass all checks; invalid included records fail
    the whole resource. Only exact excluded property types are skipped. The
    caller must additionally check validity at the actual observation instant.
    Returned timestamps are aware UTC values. price_per_m2_pln is diagnostic.
    """
    if not isinstance(csv_text, str) or "\x00" in csv_text:
        raise ValueError("The apartment resource must be CSV text without NUL bytes.")
    if not isinstance(as_of_date, date) or isinstance(as_of_date, datetime):
        raise ValueError("as_of_date must be an explicit datetime.date.")
    try:
        reader = csv.DictReader(StringIO(csv_text.lstrip("\ufeff")), delimiter=",", strict=True)
        headers = reader.fieldnames
        if not headers:
            raise ValueError("The apartment resource has no CSV headers.")
        headers = [header.strip() for header in headers]
        if len(headers) != len(set(headers)):
            raise ValueError("The apartment resource has duplicate CSV headers.")
        reader.fieldnames = headers
        missing = [header for header in HEADERS.values() if header not in headers]
        if missing:
            raise ValueError("Missing required apartment CSV columns: " + ", ".join(missing))
        base_headers = [header for header in headers if header.startswith(BASE_PRICE_PREFIX)]
        if len(base_headers) != 1:
            raise ValueError("Exactly one base apartment price column is required.")
        diagnostic_headers = [header for header in headers if header.startswith(PRICE_PER_M2_PREFIX)]
        if len(diagnostic_headers) > 1:
            raise ValueError("Multiple diagnostic price-per-m2 columns are ambiguous.")
        rows = list(reader)
    except csv.Error:
        raise ValueError("The apartment resource is malformed comma-delimited CSV.") from None

    day_start = datetime.combine(as_of_date, time.min, tzinfo=WARSAW).astimezone(timezone.utc)
    day_end = datetime.combine(as_of_date + timedelta(days=1), time.min, tzinfo=WARSAW).astimezone(timezone.utc)
    result = []
    seen_units = set()
    for row_number, row in enumerate(rows, start=2):
        if None in row or any(value is None for value in row.values()):
            raise ValueError(f"Row {row_number}: CSV values do not match the header count.")
        property_type = row[HEADERS["property_type"]].strip()
        if property_type in EXCLUDED_TYPES:
            continue
        if property_type.casefold() == "dom jednorodzinny":
            raise ValueError(f"Row {row_number}: houses are not supported by the apartment adapter.")
        if property_type != "Lokal mieszkalny":
            raise ValueError(f"Row {row_number}: unexpected property type in the apartment resource.")

        nip = row[HEADERS["nip"]].strip()
        developer_url = row[HEADERS["developer_url"]].strip()
        try:
            website = urlsplit(developer_url)
            valid_website = (
                website.scheme == "https" and website.hostname == EXPECTED_HOST
                and website.username is None and website.password is None
                and website.port in {None, 443}
            )
        except ValueError:
            valid_website = False
        city = row[HEADERS["city"]].strip()
        street = re.sub(r"^ul\.\s*", "", row[HEADERS["street"]].strip(), flags=re.IGNORECASE)
        street = " ".join(street.split())
        building = row[HEADERS["building_number"]].strip()
        if (
            nip != EXPECTED_NIP or not valid_website
            or city.casefold() != EXPECTED_CITY.casefold()
            or street.casefold() != EXPECTED_STREET.casefold()
            or building != EXPECTED_BUILDING
        ):
            raise ValueError(f"Row {row_number}: unexpected developer or project address context.")

        unit = row[HEADERS["unit_number"]].strip()
        if not re.fullmatch(r"[AB][0-3]/[0-9]{2}", unit):
            raise ValueError(f"Row {row_number}: unexpected apartment unit number.")
        if unit in seen_units:
            raise ValueError(f"Row {row_number}: duplicate apartment unit {unit}; resource is ambiguous.")
        seen_units.add(unit)

        price = _positive_price(row[base_headers[0]], "base price", row_number)
        price_per_m2 = None
        if diagnostic_headers:
            diagnostic = row[diagnostic_headers[0]].strip()
            if diagnostic not in {"", "X"}:
                price_per_m2 = _positive_price(diagnostic, "diagnostic price per m2", row_number)
        valid_from = _aware_timestamp(row[HEADERS["valid_from"]], "valid_from", row_number)
        valid_until = _aware_timestamp(row[HEADERS["valid_until"]], "valid_until", row_number)
        if valid_until < valid_from:
            raise ValueError(f"Row {row_number}: apartment validity interval is reversed.")
        if not (valid_from < day_end and valid_until >= day_start):
            raise ValueError(f"Row {row_number}: apartment validity does not intersect {as_of_date.isoformat()} in Europe/Warsaw.")

        result.append({
            "unit_number": unit,
            "price_pln": price,
            "price_per_m2_pln": price_per_m2,
            "valid_from": valid_from,
            "valid_until": valid_until,
            "city": EXPECTED_CITY,
            "street": EXPECTED_STREET,
            "building_number": EXPECTED_BUILDING,
            "postal_code": row.get(POSTAL_CODE_HEADER, "").strip() or None,
            "address": f"ul. {EXPECTED_STREET} {EXPECTED_BUILDING}, {EXPECTED_CITY}",
            "developer_nip": EXPECTED_NIP,
            "developer_url": developer_url,
        })
    if not result:
        raise ValueError("The resource contains no validated apartment prices.")
    return result
