"""Parse the public OLX search page's embedded JSON, without executing JS.

The original API covers a first-page Warsaw apartment-sale preview. A separate
page API explicitly validates pagination. Neither API follows internal links
from the page state or external Otodom links. The source's
``four`` room code means "4 or more", so those rows cannot provide the exact
room count required by this initial adapter.
"""

from __future__ import annotations

import json
import logging
import math
import re
from urllib.parse import parse_qsl, urlsplit

from bs4 import BeautifulSoup

from .fetch_data import FIELDS, WARSAW_CENTRE, _observed_at, canonical_url


LOGGER = logging.getLogger(__name__)
SOURCE = "www.olx.pl"
SEARCH_PATH = "/nieruchomosci/mieszkania/sprzedaz/warszawa/"
CATEGORY_PATH = "nieruchomosci/mieszkania/sprzedaz"
ROOMS = {"one": 1, "two": 2, "three": 3}
ASSIGNMENT = re.compile(r"(?:^|;)\s*window\.__PRERENDERED_STATE__\s*=\s*")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Powtórzony klucz w JSON OLX.")
        result[key] = value
    return result


def _nonfinite_json(value):
    raise ValueError("Nieskończona lub nieokreślona liczba w JSON OLX.")


def _finite_json_float(value):
    number = float(value)
    if not math.isfinite(number):
        return _nonfinite_json(value)
    return number


def _number(value):
    """Read a normalized numeric value, never an amount from a description."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value):
            return None
    elif not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _search_url(page_url, expected_page=1):
    if type(expected_page) is not int or not 1 <= expected_page <= 1000:
        raise ValueError("Numer strony OLX musi być liczbą całkowitą od 1 do 1000.")
    parts = urlsplit(page_url)
    if (
        parts.scheme != "https" or parts.netloc != SOURCE
        or parts.path.rstrip("/") != SEARCH_PATH.rstrip("/")
        or parts.fragment
    ):
        raise ValueError("Parser OLX wymaga strony sprzedaży mieszkań w Warszawie.")
    pages = [value for key, value in parse_qsl(parts.query, keep_blank_values=True, max_num_fields=100) if key.casefold() == "page"]
    if len(pages) > 1 or (pages and pages[0] != str(expected_page)) or (not pages and expected_page != 1):
        raise ValueError("Adres OLX nie potwierdza oczekiwanego numeru strony.")


def _state(soup):
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if re.search(r"access denied|verify you are human|just a moment|captcha|robot check", title, re.I):
        raise ValueError("OLX zwrócił stronę weryfikacji zamiast ofert.")
    scripts = soup.find_all("script", id="olx-init-config")
    if len(scripts) != 1:
        raise ValueError("Brak jednoznacznego bloku danych OLX; struktura strony mogła się zmienić.")
    body = scripts[0].string or scripts[0].get_text()
    assignments = list(ASSIGNMENT.finditer(body))
    if len(assignments) != 1:
        raise ValueError("Brak jednoznacznego stanu ofert OLX.")
    decoder = json.JSONDecoder(
        object_pairs_hook=_unique_object, parse_constant=_nonfinite_json,
        parse_float=_finite_json_float,
    )
    try:
        value, end = decoder.raw_decode(body[assignments[0].end():])
        remainder = body[assignments[0].end() + end:].lstrip()
        if remainder and not remainder.startswith(";"):
            raise ValueError("Stan OLX nie jest samodzielnym JSON.")
        if isinstance(value, str):
            value = decoder.decode(value)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Nieprawidłowy JSON stanu OLX.") from exc
    if not isinstance(value, dict):
        raise ValueError("Stan OLX musi być obiektem JSON.")
    return value


def _catalogue(state, expected_page=1):
    try:
        catalogue = state["listing"]["listing"]
        category = state["categories"]["list"]["14"]
        request = catalogue["requestParams"]
        ads = catalogue["ads"]
    except (KeyError, TypeError) as exc:
        raise ValueError("Nieznana struktura katalogu OLX.") from exc
    request_path = request.get("categoryPath") if isinstance(request, dict) else None
    if (
        not isinstance(catalogue, dict) or not isinstance(category, dict)
        or not isinstance(request, dict) or not isinstance(ads, list)
        or type(catalogue.get("categoryId")) is not int or catalogue["categoryId"] != 14
        or category.get("path") != CATEGORY_PATH
        or not isinstance(request_path, str) or request_path.rstrip("/") != SEARCH_PATH.strip("/")
        or type(catalogue.get("pageNumber")) is not int or catalogue["pageNumber"] != expected_page - 1
        or ("page" in request and (type(request["page"]) is not int or request["page"] != expected_page - 1))
    ):
        raise ValueError("Katalog OLX nie potwierdza oczekiwanej strony sprzedaży mieszkań w Warszawie.")
    return catalogue


def _parameters(ad):
    params = ad.get("params")
    if not isinstance(params, list):
        raise ValueError("brak parametrów mieszkania")
    result = {}
    for param in params:
        if not isinstance(param, dict) or not isinstance(param.get("key"), str):
            raise ValueError("nieprawidłowa struktura parametrów")
        key = param["key"]
        if key in result:
            raise ValueError("powtórzony parametr mieszkania")
        result[key] = param
    return result


def _coordinates(ad):
    location = ad.get("map")
    if not isinstance(location, dict) or location.get("show_detailed") is not True:
        return None, None, None
    lat, lon = _number(location.get("lat")), _number(location.get("lon"))
    if lat is None or lon is None or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        return None, None, None
    lat1, lon1 = map(math.radians, WARSAW_CENTRE)
    lat2, lon2 = map(math.radians, (lat, lon))
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    distance = round(6371.0088 * 2 * math.asin(math.sqrt(min(1, max(0, a)))), 4)
    return lat, lon, distance


def _record(ad, page_url, observed_at):
    if not isinstance(ad, dict):
        raise ValueError("pozycja nie jest ofertą")
    category, location = ad.get("category"), ad.get("location")
    if not isinstance(category, dict) or type(category.get("id")) is not int or category["id"] != 14:
        raise ValueError("inna kategoria niż sprzedaż mieszkania")
    if not isinstance(location, dict) or location.get("cityName") != "Warszawa":
        raise ValueError("miasto inne niż Warszawa lub brak miasta")
    if location.get("cityNormalizedName", "warszawa") != "warszawa":
        raise ValueError("sprzeczne oznaczenie miasta")
    if ad.get("isActive", True) is not True or ad.get("status", "active") != "active":
        raise ValueError("oferta nie jest aktywna na stronie")
    listing_id = ad.get("id")
    if type(listing_id) is not int or listing_id <= 0:
        raise ValueError("brak dodatniego identyfikatora OLX")
    url_value = ad.get("url")
    if not isinstance(url_value, str) or not url_value:
        raise ValueError("brak adresu oferty OLX")
    url = canonical_url(url_value, page_url)
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != SOURCE or not re.fullmatch(r"/d/oferta/[^/]+\.html", parts.path):
        raise ValueError("adres oferty poza OLX lub nieznany format")
    price = ad.get("price")
    if not isinstance(price, dict) or any(price.get(flag) is True for flag in ("budget", "free", "exchange")):
        raise ValueError("brak zwykłej ceny sprzedaży")
    regular = price.get("regularPrice")
    if not isinstance(regular, dict) or regular.get("currencyCode") != "PLN":
        raise ValueError("brak ceny całkowitej w PLN")
    amount = _number(regular.get("value"))
    if amount is None or amount <= 0:
        raise ValueError("nieprawidłowa cena całkowita")
    params = _parameters(ad)
    area = _number(params.get("m", {}).get("normalizedValue"))
    if area is None or area <= 0:
        raise ValueError("brak dodatniego metrażu")
    rooms = ROOMS.get(params.get("rooms", {}).get("normalizedValue"))
    if rooms is None:
        raise ValueError("brak dokładnej liczby pokoi (4 i więcej też jest nieokreślone)")
    floor_code = params.get("floor_select", {}).get("normalizedValue")
    floor = None
    if isinstance(floor_code, str) and re.fullmatch(r"floor_(?:-1|[0-9]|10)", floor_code):
        floor = int(floor_code.removeprefix("floor_"))
    district = location.get("districtName")
    district = district.strip() if isinstance(district, str) and district.strip() else None
    lat, lon, distance = _coordinates(ad)
    return dict(zip(FIELDS, (
        SOURCE, str(listing_id), url, "Warszawa", district, amount, area, rooms,
        floor, None, lat, lon, distance, observed_at,
    )))


def _parse_page(html, page_url, observed_at, expected_page, allow_empty):
    """Return valid, unique OLX preview records with one UTC observation time.

    Missing required apartment features reject the affected row. Missing or
    changed page state, zero valid rows, and contradictory duplicate identities
    fail the whole preview; they must never replace a previous successful file.
    """
    _search_url(page_url, expected_page)
    moment = _observed_at(observed_at)
    if not isinstance(html, str) or not html.strip():
        raise ValueError("Pusty HTML OLX.")
    catalogue = _catalogue(_state(BeautifulSoup(html, "html.parser")), expected_page)
    ads = catalogue["ads"]
    records, urls = {}, {}
    rejected = {}
    for ad in ads:
        try:
            record = _record(ad, page_url, moment)
        except (ValueError, TypeError) as exc:
            reason = str(exc)
            rejected[reason] = rejected.get(reason, 0) + 1
            continue
        key = record["listing_id"]
        if key in records and records[key] != record:
            raise ValueError("Sprzeczne dane dla tego samego identyfikatora OLX.")
        url = record["url"]
        if url in urls and urls[url] != key:
            raise ValueError("Sprzeczne identyfikatory tego samego adresu OLX.")
        records[key], urls[url] = record, key
    for reason, count in rejected.items():
        LOGGER.warning("Pominięto %s pozycji OLX: %s.", count, reason)
    if not records and not allow_empty:
        raise ValueError("Strona OLX nie zawiera poprawnych ofert z ceną, metrażem i dokładną liczbą pokoi.")
    metadata = {
        "page": expected_page, "page_number": catalogue["pageNumber"],
        "raw_items": len(ads), "parsed_listings": len(records),
        "skipped_items": sum(rejected.values()), "skip_reasons": rejected,
    }
    for source_key, key in (("totalPages", "total_pages"), ("totalElements", "total_elements"), ("visibleElements", "visible_elements")):
        value = catalogue.get(source_key)
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError("Nieprawidłowe metadane paginacji OLX.")
        metadata[key] = value
    if metadata["total_pages"] is not None and metadata["total_pages"] < expected_page:
        raise ValueError("Numer strony przekracza deklarowaną paginację OLX.")
    total, visible = metadata["total_elements"], metadata["visible_elements"]
    metadata["reported_result_cap"] = total is not None and visible is not None and visible > total
    return list(records.values()), metadata


def parse_olx_search(html, page_url, observed_at=None):
    """Preserve the strict first-page preview API."""
    records, _ = _parse_page(html, page_url, observed_at, 1, False)
    return records


def parse_olx_search_page(html, page_url, observed_at=None, *, expected_page=1):
    """Explicit pagination API: validate the requested public HTML page.

    Metadata exposes only counts and static rejection reasons, never page state
    tokens. Empty eligible results are represented without inventing records;
    the collector decides whether the first page is usable.
    """
    return _parse_page(html, page_url, observed_at, expected_page, True)
