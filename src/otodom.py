"""Parse a public Otodom Warsaw apartment-sale search page as JSON.

Only individual, priced apartments in the verified search-result format are
exported. No JavaScript, listing detail page or internal API is used. Coordinates
of a city boundary and investment-level price/area ranges are not apartment
features. This adapter supports the verified exact room codes ONE through FOUR.
"""

from __future__ import annotations

import json
import logging
import math
import re
from urllib.parse import parse_qsl, urlsplit

from bs4 import BeautifulSoup

from .fetch_data import FIELDS, _observed_at, canonical_url


LOGGER = logging.getLogger(__name__)
SOURCE = "www.otodom.pl"
WARSAW_LOCATION = "mazowieckie/warszawa/warszawa/warszawa"
SEARCH_PATH = "/pl/wyniki/sprzedaz/mieszkanie/" + WARSAW_LOCATION
LEGACY_SEARCH_PATH = "/pl/oferty/sprzedaz/mieszkanie/warszawa"
ROOMS = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4}
FLOORS = dict(zip(
    ("GROUND", "FIRST", "SECOND", "THIRD", "FOURTH", "FIFTH", "SIXTH",
     "SEVENTH", "EIGHTH", "NINTH", "TENTH"), range(11),
))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Powtórzony klucz w JSON Otodom.")
        result[key] = value
    return result


def _nonfinite_json(value):
    raise ValueError("Nieskończona lub nieokreślona liczba w JSON Otodom.")


def _finite_json_float(value):
    number = float(value)
    if not math.isfinite(number):
        return _nonfinite_json(value)
    return number


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _search_url(page_url, *, canonical=False, expected_page=1):
    if not isinstance(page_url, str) or re.search(r"[\x00-\x20\x7f\\]", page_url):
        raise ValueError("Nieprawidłowy adres Otodom.")
    parts = urlsplit(page_url)
    paths = {SEARCH_PATH} if canonical else {SEARCH_PATH, LEGACY_SEARCH_PATH}
    if (
        parts.scheme != "https" or parts.netloc != SOURCE
        or parts.path.rstrip("/") not in paths or "#" in page_url
    ):
        raise ValueError("Parser Otodom wymaga strony sprzedaży mieszkań w Warszawie.")
    page_values = [value for key, value in parse_qsl(parts.query, keep_blank_values=True, max_num_fields=100)
                   if key.casefold() == "page"]
    if len(page_values) > 1:
        raise ValueError("Powtórzony parametr strony Otodom.")
    # Canonical URLs may identify the base search even on a later result page.
    if page_values and page_values[0] != str(expected_page):
        raise ValueError("Adres Otodom wskazuje inną stronę niż żądana.")
    if expected_page != 1 and not canonical and not page_values:
        raise ValueError("Brak numeru żądanej strony w adresie Otodom.")


def _state(soup):
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if re.search(r"access denied|verify you are human|just a moment|captcha|robot check", title, re.I):
        raise ValueError("Otodom zwrócił stronę weryfikacji zamiast ofert.")
    scripts = soup.find_all("script", id="__NEXT_DATA__")
    if len(scripts) != 1 or scripts[0].get("type") != "application/json":
        raise ValueError("Brak jednoznacznego bloku JSON ofert Otodom.")
    decoder = json.JSONDecoder(
        object_pairs_hook=_unique_object, parse_constant=_nonfinite_json,
        parse_float=_finite_json_float,
    )
    try:
        state = decoder.decode(scripts[0].string or scripts[0].get_text())
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Nieprawidłowy JSON stanu Otodom.") from exc
    if not isinstance(state, dict):
        raise ValueError("Stan Otodom musi być obiektem JSON.")
    return state


def _catalogue(state, *, expected_page=1, require_pagination=False):
    try:
        page = state["props"]["pageProps"]
        search = page["data"]["searchAds"]
        items = search["items"]
        pagination = search["pagination"]
        filters = page["filteringQueryParams"]
        canonical = page["canonicalURL"]
    except (KeyError, TypeError) as exc:
        raise ValueError("Nieznana struktura katalogu Otodom.") from exc
    if (
        not isinstance(page, dict) or not isinstance(search, dict)
        or not isinstance(items, list) or not isinstance(pagination, dict)
        or not isinstance(filters, dict) or not isinstance(canonical, str)
        or page.get("estate") != "FLAT" or page.get("transaction") != "SELL"
        or page.get("location") != WARSAW_LOCATION
        or type(filters.get("page")) is not int or filters["page"] != expected_page
        or type(pagination.get("currentPage")) is not int or pagination["currentPage"] != expected_page
    ):
        raise ValueError("Katalog Otodom nie potwierdza żądanej strony sprzedaży mieszkań w Warszawie.")
    # A canonical link must confirm the page scope, not merely point at Otodom.
    if canonical.startswith("/") and not canonical.startswith("//"):
        canonical = "https://" + SOURCE + canonical
    _search_url(canonical, canonical=True, expected_page=expected_page)
    metadata = {
        "requested_page": expected_page, "current_page": pagination["currentPage"],
        "filter_page": filters["page"], "items_on_page": len(items),
    }
    if require_pagination:
        for name, minimum in (("itemsPerPage", 1), ("totalItems", 0), ("totalPages", 1)):
            if type(pagination.get(name)) is not int or pagination[name] < minimum:
                raise ValueError("Nieprawidłowe metadane stronicowania Otodom.")
        if pagination["itemsPerPage"] > 1000 or expected_page > pagination["totalPages"]:
            raise ValueError("Sprzeczne metadane stronicowania Otodom.")
        metadata["pagination"] = {name: pagination[name] for name in
                                  ("currentPage", "itemsPerPage", "totalItems", "totalPages")}
        metadata["items_per_page_mismatch"] = len(items) != pagination["itemsPerPage"]
        sorting = page.get("sortingOption")
        metadata["sorting"] = {
            key: value for key, value in sorting.items()
            if key in {"by", "direction"} and isinstance(value, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value)
        } if isinstance(sorting, dict) else {}
    return items, metadata


def _location(ad):
    try:
        locations = ad["location"]["reverseGeocoding"]["locations"]
    except (KeyError, TypeError) as exc:
        raise ValueError("brak potwierdzonego miasta oferty") from exc
    if not isinstance(locations, list) or any(not isinstance(location, dict) for location in locations):
        raise ValueError("nieprawidłowa struktura lokalizacji")
    cities = [location for location in locations if location.get("locationLevel") == "city_or_village"]
    if not cities or any(city.get("id") != WARSAW_LOCATION or city.get("name") != "Warszawa" for city in cities):
        raise ValueError("miasto inne niż Warszawa lub sprzeczne lokalizacje")
    districts = [location for location in locations if location.get("locationLevel") == "district"]
    names = set()
    for district in districts:
        identifier, name = district.get("id"), district.get("name")
        if (
            not isinstance(identifier, str) or not identifier.startswith(WARSAW_LOCATION + "/")
            or identifier.count("/") != WARSAW_LOCATION.count("/") + 1
            or identifier.endswith("/")
            or not isinstance(name, str) or not name.strip()
        ):
            raise ValueError("sprzeczne lub nieprawidłowe oznaczenie dzielnicy")
        names.add((identifier, name.strip()))
    if len(names) > 1:
        raise ValueError("sprzeczne dzielnice tej samej oferty")
    return next(iter(names))[1] if names else None


def _record(ad, observed_at):
    if not isinstance(ad, dict):
        raise ValueError("pozycja nie jest ofertą")
    if ad.get("estate") != "FLAT" or ad.get("transaction") != "SELL":
        raise ValueError("pozycja nie jest pojedynczym mieszkaniem na sprzedaż")
    if ad.get("hidePrice", False) is not False:
        raise ValueError("cena oferty jest ukryta lub ma nieznany status")
    listing_id = ad.get("id")
    if type(listing_id) is not int or listing_id <= 0:
        raise ValueError("brak dodatniego identyfikatora Otodom")
    slug = ad.get("slug")
    if not isinstance(slug, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*-ID[A-Za-z0-9]+", slug):
        raise ValueError("nieprawidłowy adres oferty Otodom")
    # The same apartment can also appear as an HPR advertising presentation
    # with an artificial ID. Its href starts with 'hpr/'; it is not a new ad.
    if ad.get("href") != "[lang]/ad/" + slug:
        raise ValueError("reklamowy lub nieznany wariant prezentacji oferty")
    url = canonical_url("https://" + SOURCE + "/pl/oferta/" + slug)
    price = ad.get("totalPrice")
    if not isinstance(price, dict) or price.get("currency") != "PLN":
        raise ValueError("brak ceny całkowitej w PLN")
    amount = _number(price.get("value"))
    area = _number(ad.get("areaInSquareMeters"))
    if amount is None or amount <= 0:
        raise ValueError("nieprawidłowa cena całkowita")
    if area is None or area <= 0:
        raise ValueError("brak dodatniego metrażu")
    room_code = ad.get("roomsNumber")
    rooms = ROOMS.get(room_code) if isinstance(room_code, str) else None
    if rooms is None:
        raise ValueError("brak zweryfikowanej dokładnej liczby pokoi")
    floor_code = ad.get("floorNumber")
    floor = FLOORS.get(floor_code) if isinstance(floor_code, str) else None
    district = _location(ad)
    # Search-state geometries describe locations, not individual properties.
    return dict(zip(FIELDS, (
        SOURCE, str(listing_id), url, "Warszawa", district, amount, area, rooms,
        floor, None, None, None, None, observed_at,
    )))


def parse_otodom_search(html, page_url, observed_at=None):
    """Return unique valid apartment records with one UTC observation time.

    Invalid individual records are skipped with a warning. A changed page
    structure, zero valid rows or conflicting identities fail the preview.
    """
    records, _ = _parse_search(html, page_url, observed_at=observed_at)
    return records


def parse_otodom_search_page(html, page_url, observed_at=None, *, expected_page):
    """Opt-in page parser with verified pagination and rejection counts.

    A canonical link can identify the base search, but the requested URL,
    filtering state and result pagination must all agree on the exact page.
    """
    if type(expected_page) is not int or not 1 <= expected_page <= 1000:
        raise ValueError("Numer strony Otodom musi być liczbą od 1 do 1000.")
    return _parse_search(html, page_url, observed_at=observed_at,
                         expected_page=expected_page, require_pagination=True)


def _parse_search(html, page_url, observed_at=None, *, expected_page=1, require_pagination=False):
    _search_url(page_url, expected_page=expected_page)
    moment = _observed_at(observed_at)
    if not isinstance(html, str) or not html.strip():
        raise ValueError("Pusty HTML Otodom.")
    items, metadata = _catalogue(_state(BeautifulSoup(html, "html.parser")),
                                 expected_page=expected_page, require_pagination=require_pagination)
    records, urls, rejected = {}, {}, {}
    duplicates = 0
    for ad in items:
        try:
            record = _record(ad, moment)
        except (ValueError, TypeError) as exc:
            reason = str(exc)
            rejected[reason] = rejected.get(reason, 0) + 1
            continue
        identifier, url = record["listing_id"], record["url"]
        if identifier in records and records[identifier] != record:
            raise ValueError("Sprzeczne dane dla tego samego identyfikatora Otodom.")
        if url in urls and urls[url] != identifier:
            raise ValueError("Sprzeczne identyfikatory tego samego adresu Otodom.")
        if identifier in records:
            duplicates += 1
        records[identifier], urls[url] = record, identifier
    for reason, count in rejected.items():
        LOGGER.warning("Pominięto %s pozycji Otodom: %s.", count, reason)
    if not records:
        raise ValueError("Strona Otodom nie zawiera poprawnych ofert z ceną, metrażem i dokładną liczbą pokoi.")
    metadata.update(parsed_listings=len(records), skipped_items=sum(rejected.values()),
                    skipped_by_reason=rejected, duplicates_on_page=duplicates)
    return list(records.values()), metadata
