"""Read apartment features already published in Bemovo's homepage HTML.

The adapter reads JSON arguments of ``self.__next_f.push(...)`` and the JSON
records in the resulting React Server Components stream. It never evaluates
JavaScript, follows CMS endpoints, or derives missing rooms/floors from names.
The confirmed source contract is Bemovo PH1 in Warsaw. A changed or ambiguous
contract raises ValueError so a partial catalogue is not silently imported.
"""

from __future__ import annotations

import json
import math
import re

from bs4 import BeautifulSoup


PUSH_CALL = re.compile(r"\bself\s*\.\s*__next_f\s*\.\s*push\s*\(")
RSC_RECORD = re.compile(r"(?P<record_id>[0-9a-fA-F]*):(?P<body>.*)")
KNOWN_STATUSES = {"available", "reserved", "sold"}
REQUIRED_FIELDS = {
    "number", "slugNumber", "id", "price", "area", "rooms", "floor",
    "city", "investment", "status", "building", "isCommercialUnit",
}
FEATURE_URL_BASE = "https://bemovo.pl/pl/mieszkanie/"


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON zawiera powtórzony klucz obiektu.")
        result[key] = value
    return result


def _reject_non_json_number(value):
    raise ValueError("JSON zawiera niedozwoloną stałą liczbową.")


def _decoder():
    return json.JSONDecoder(
        object_pairs_hook=_unique_object, parse_constant=_reject_non_json_number,
    )


def _flight_chunks(html):
    decoder = _decoder()
    chunks = []
    for script in BeautifulSoup(html, "html.parser").find_all("script"):
        code = script.string or script.get_text()
        position = 0
        while match := PUSH_CALL.search(code, position):
            start = match.end()
            while start < len(code) and code[start].isspace():
                start += 1
            try:
                argument, end = decoder.raw_decode(code, start)
            except ValueError as error:
                raise ValueError("Nieprawidłowy argument JSON self.__next_f.push.") from error
            while end < len(code) and code[end].isspace():
                end += 1
            if end >= len(code) or code[end] != ")":
                raise ValueError("self.__next_f.push musi otrzymać pojedynczy argument JSON.")
            position = end + 1
            if not isinstance(argument, list) or not argument or type(argument[0]) is not int:
                raise ValueError("Zmieniony format argumentu self.__next_f.push.")
            channel = argument[0]
            if channel == 1:
                if len(argument) != 2 or not isinstance(argument[1], str):
                    raise ValueError("Zmieniony format tekstowego fragmentu RSC.")
                chunks.append(argument[1])
            elif channel not in {0, 2}:
                # Binary streams need their own verified adapter, rather than
                # silently ignoring a possible part of the apartment catalogue.
                raise ValueError("Nieobsługiwany kanał danych RSC; sprawdź schemat źródła.")
    if not chunks:
        raise ValueError("Brak publicznych tekstowych danych self.__next_f.push w HTML.")
    return "".join(chunks)


def _rsc_json_records(stream):
    decoder = _decoder()
    records = {}
    for line in stream.splitlines():
        if not line.strip():
            continue
        match = RSC_RECORD.fullmatch(line)
        if not match:
            raise ValueError("Nieprawidłowy lub nieobsługiwany rekord RSC.")
        record_id, body = match.group("record_id", "body")
        body = body.lstrip()
        # The live page contains module imports I[...] and resource hints
        # :HL[...]. Validate their JSON, but do not inspect them as page props.
        is_metadata = body.startswith("I") or body.startswith("HL")
        if body.startswith("HL"):
            body = body[2:]
        elif body.startswith("I"):
            body = body[1:]
        try:
            value, end = decoder.raw_decode(body)
        except ValueError as error:
            raise ValueError("Nieprawidłowy JSON rekordu RSC; sprawdź schemat strony.") from error
        if body[end:].strip():
            raise ValueError("Rekord RSC zawiera dane poza wartością JSON.")
        if is_metadata:
            if not isinstance(value, list):
                raise ValueError("Zmieniony format metadanych RSC.")
            continue
        if not record_id:
            raise ValueError("Rekord danych RSC nie ma identyfikatora.")
        if record_id in records and records[record_id] != value:
            raise ValueError("Sprzeczne rekordy RSC o tym samym identyfikatorze.")
        records[record_id] = value
    return list(records.values())


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _text(row, key):
    value = row[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Pole {key} musi być niepustym tekstem.")
    if "\ufffd" in value:
        raise ValueError(f"Pole {key} zawiera błąd kodowania znaków.")
    return value.strip()


def _positive_number(row, key):
    value = row[key]
    if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
        raise ValueError(f"Pole {key} musi być dodatnią, skończoną liczbą JSON.")
    return float(value)


def _normalise(row):
    if not isinstance(row, dict):
        raise ValueError("Każdy element apartments musi być obiektem JSON.")
    missing = REQUIRED_FIELDS - row.keys()
    if missing:
        raise ValueError("Zmieniony schemat apartments; brak pól: " + ", ".join(sorted(missing)) + ".")
    number = _text(row, "number")
    slug = _text(row, "slugNumber")
    if not re.fullmatch(r"[A-Za-z0-9]+(?:/[A-Za-z0-9]+)*", number):
        raise ValueError("Nieprawidłowy numer lokalu.")
    if not re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", slug) or slug != number.replace("/", "-"):
        raise ValueError("slugNumber nie odpowiada jawnemu numerowi lokalu.")
    city = _text(row, "city")
    investment = _text(row, "investment")
    if city != "Warszawa" or investment != "Bemovo PH1":
        raise ValueError("Nieoczekiwane miasto lub inwestycja; adapter obsługuje Warszawę, Bemovo PH1.")
    status = _text(row, "status")
    if status not in KNOWN_STATUSES:
        raise ValueError("Nieznany status lokalu; sprawdź schemat źródła.")
    commercial = row["isCommercialUnit"]
    if type(commercial) is not bool:
        raise ValueError("isCommercialUnit musi mieć wartość logiczną JSON.")
    rooms = row["rooms"]
    if not (commercial and rooms is None):
        if type(rooms) is not int or rooms <= 0:
            raise ValueError("Liczba pokoi mieszkania musi być dodatnią liczbą całkowitą; None jest dozwolone tylko dla lokalu usługowego.")
    floor = row["floor"]
    if type(floor) is not int or floor < 0:
        raise ValueError("Piętro musi być nieujemną liczbą całkowitą; parter to 0.")
    return {
        "number": number,
        "slug_number": slug,
        "source_id": _text(row, "id"),
        "city": city,
        "investment": investment,
        "building": _text(row, "building"),
        "status": status,
        "is_commercial_unit": commercial,
        "area_m2": _positive_number(row, "area"),
        "rooms": rooms,
        "floor": floor,
        "website_price_pln": _positive_number(row, "price"),
        "feature_url": FEATURE_URL_BASE + slug + "/",
    }


def _normalise_catalogue(rows):
    if not isinstance(rows, list) or not rows:
        raise ValueError("apartments musi być niepustą pełną listą lokali.")
    by_number, by_slug, by_id = {}, {}, {}
    for raw in rows:
        row = _normalise(raw)
        for index, key in (
            (by_number, (row["investment"], row["number"])),
            (by_slug, (row["investment"], row["slug_number"])),
            (by_id, (row["investment"], row["source_id"])),
        ):
            if key in index and index[key] != row:
                raise ValueError("Sprzeczne duplikaty lokalu w apartments.")
            index[key] = row
    return list(by_number.values())


def parse_bemovo_features(html):
    """Return the unique public apartment catalogue with explicit source fields.

    Sold/reserved apartments and commercial units remain in the result for
    auditing. The join must decide which of them may enter the training table.
    website_price_pln is an audit value; the pilot's target comes from gov CSV.
    """
    if not isinstance(html, str) or not html.strip():
        raise ValueError("HTML musi być niepustym tekstem.")
    records = _rsc_json_records(_flight_chunks(html))
    catalogues = []
    for record in records:
        for node in _walk(record):
            if "apartments" in node:
                catalogues.append(_normalise_catalogue(node["apartments"]))
    if not catalogues:
        raise ValueError("Brak pełnej listy apartments w publicznych propsach strony Bemovo.")
    selected = catalogues[0]
    signature = sorted(selected, key=lambda row: (row["investment"], row["number"]))
    for candidate in catalogues[1:]:
        if sorted(candidate, key=lambda row: (row["investment"], row["number"])) != signature:
            raise ValueError("HTML zawiera różne listy apartments; nie można jednoznacznie wybrać pełnego katalogu.")
    return selected
