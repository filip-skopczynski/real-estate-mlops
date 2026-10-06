"""Synthetic adapter fixtures; no real apartment rows are committed here."""

import csv
from datetime import date, datetime, timezone
from io import StringIO

import pytest

from src.bemovo_prices import (
    BASE_PRICE_PREFIX, HEADERS, POSTAL_CODE_HEADER, PRICE_PER_M2_PREFIX,
    parse_bemovo_prices,
)


BASE_PRICE_HEADER = BASE_PRICE_PREFIX + " [zł]"
DIAGNOSTIC_HEADER = PRICE_PER_M2_PREFIX + " użytkowej lokalu mieszkalnego / domu jednorodzinnego [zł]"
COMBINED_PRICE_HEADER = (
    "Cena lokalu mieszkalnego lub domu jednorodzinnego uwzględniająca cenę lokalu "
    "stanowiącą iloczyn powierzchni oraz metrażu i innych składowych ceny [zł]"
)
AS_OF = date(2026, 10, 5)


def apartment(**changes):
    row = {
        HEADERS["nip"]: "5252801624",
        HEADERS["developer_url"]: "https://bemovo.pl/",
        HEADERS["city"]: "Warszawa",
        HEADERS["street"]: "ul. Batalionów Chłopskich",
        HEADERS["building_number"]: "95",
        HEADERS["property_type"]: "Lokal mieszkalny",
        HEADERS["unit_number"]: "A0/01",
        HEADERS["valid_from"]: "2026-10-01 09:00:00+02:00",
        HEADERS["valid_until"]: "2026-10-05 23:59:59+02:00",
        BASE_PRICE_HEADER: "900000.50",
        DIAGNOSTIC_HEADER: "15000.00",
        COMBINED_PRICE_HEADER: "9999999.99",
        POSTAL_CODE_HEADER: "01-307",
    }
    row.update(changes)
    return row


def csv_fixture(rows, headers=None, bom=False):
    headers = headers or list(rows[0])
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return ("\ufeff" if bom else "") + buffer.getvalue()


def test_base_price_and_unit_identity_are_preserved_without_inferring_area():
    rows = parse_bemovo_prices(csv_fixture([apartment()], bom=True), as_of_date=AS_OF)
    assert len(rows) == 1
    parsed = rows[0]
    assert parsed["unit_number"] == "A0/01"
    assert parsed["price_pln"] == 900000.50
    assert parsed["price_per_m2_pln"] == 15000
    assert parsed["valid_from"] == datetime(2026, 10, 1, 7, tzinfo=timezone.utc)
    assert parsed["valid_until"] == datetime(2026, 10, 5, 21, 59, 59, tzinfo=timezone.utc)
    assert parsed["city"] == "Warszawa"
    assert parsed["street"] == "Batalionów Chłopskich"
    assert parsed["building_number"] == "95"
    assert not any("area" in key or "metraz" in key for key in parsed)


def test_parking_and_extras_are_skipped_instead_of_added_to_apartment_price():
    extra = apartment(**{
        HEADERS["property_type"]: "X", HEADERS["unit_number"]: "X",
        BASE_PRICE_HEADER: "X", DIAGNOSTIC_HEADER: "X",
    })
    parsed = parse_bemovo_prices(csv_fixture([apartment(), extra, extra]), as_of_date=AS_OF)
    assert len(parsed) == 1
    assert parsed[0]["price_pln"] == 900000.50


def test_verified_extras_only_feed_can_support_a_full_sold_out_catalogue():
    extra = apartment(**{HEADERS["property_type"]: "X", HEADERS["unit_number"]: "X",
                         BASE_PRICE_HEADER: "X", DIAGNOSTIC_HEADER: "X"})
    text = csv_fixture([extra])
    with pytest.raises(ValueError):
        parse_bemovo_prices(text, as_of_date=AS_OF)
    assert parse_bemovo_prices(text, as_of_date=AS_OF, allow_empty=True) == []
    header_only = csv_fixture([], headers=list(extra))
    with pytest.raises(ValueError):
        parse_bemovo_prices(header_only, as_of_date=AS_OF, allow_empty=True)
    wrong_source = {**extra, HEADERS["nip"]: "9999999999"}
    with pytest.raises(ValueError, match="context"):
        parse_bemovo_prices(csv_fixture([wrong_source]), as_of_date=AS_OF, allow_empty=True)


def test_houses_and_unknown_property_types_are_explicitly_rejected():
    for kind in ["Dom jednorodzinny", "Unknown", "Lokal mieszkalny — promocja"]:
        with pytest.raises(ValueError, match="houses|property type"):
            parse_bemovo_prices(csv_fixture([apartment(**{HEADERS["property_type"]: kind})]), as_of_date=AS_OF)


@pytest.mark.parametrize("field,value", [
    ("nip", "9999999999"),
    ("developer_url", "https://bemovo.pl.evil.invalid/"),
    ("developer_url", "https://bemovo.pl@evil.invalid/"),
    ("developer_url", "https://user@bemovo.pl/"),
    ("city", "Kraków"),
    ("street", "ul. Inna"),
    ("building_number", "96"),
])
def test_unexpected_apartment_context_fails_the_whole_resource(field, value):
    changed = apartment(**{HEADERS[field]: value, HEADERS["unit_number"]: "B1/02"})
    with pytest.raises(ValueError, match="context"):
        parse_bemovo_prices(csv_fixture([apartment(), changed]), as_of_date=AS_OF)


@pytest.mark.parametrize("number", ["A0/1", "A4/01", "C0/01", "a0/01", "A0/01-extra"])
def test_unit_pattern_is_explicit_and_preserves_leading_zeros(number):
    with pytest.raises(ValueError, match="unit number"):
        parse_bemovo_prices(csv_fixture([apartment(**{HEADERS["unit_number"]: number})]), as_of_date=AS_OF)


@pytest.mark.parametrize("changed_price", ["900000.50", "800000"])
def test_duplicate_units_are_rejected_even_if_the_prices_agree(changed_price):
    with pytest.raises(ValueError, match="duplicate"):
        parse_bemovo_prices(csv_fixture([apartment(), apartment(**{BASE_PRICE_HEADER: changed_price})]), as_of_date=AS_OF)


@pytest.mark.parametrize("price", ["0", "-1", "NaN", "Infinity", "1e9999", "1e-9999", "X", ""])
def test_invalid_base_prices_are_never_silently_skipped(price):
    with pytest.raises(ValueError, match="base price"):
        parse_bemovo_prices(csv_fixture([apartment(**{BASE_PRICE_HEADER: price})]), as_of_date=AS_OF)


def test_price_per_m2_is_optional_but_provided_nonfinite_values_are_rejected():
    row = apartment()
    del row[DIAGNOSTIC_HEADER]
    assert parse_bemovo_prices(csv_fixture([row]), as_of_date=AS_OF)[0]["price_per_m2_pln"] is None
    row = apartment(**{DIAGNOSTIC_HEADER: "X"})
    assert parse_bemovo_prices(csv_fixture([row]), as_of_date=AS_OF)[0]["price_per_m2_pln"] is None
    with pytest.raises(ValueError, match="diagnostic"):
        parse_bemovo_prices(csv_fixture([apartment(**{DIAGNOSTIC_HEADER: "NaN"})]), as_of_date=AS_OF)


def test_decimal_comma_does_not_change_the_csv_delimiter():
    row = apartment(**{BASE_PRICE_HEADER: "900 000,50"})
    assert parse_bemovo_prices(csv_fixture([row]), as_of_date=AS_OF)[0]["price_pln"] == 900000.50


@pytest.mark.parametrize("timestamp", [
    "2026-10-01 09:00:00", "not-a-date", "2026-10-01 09:00:00+01:00",
    "2026-10-01 09:00:00+05:00",
])
def test_missing_invalid_and_mismatched_timezones_fail_closed(timestamp):
    with pytest.raises(ValueError, match="timestamp|timezone"):
        parse_bemovo_prices(csv_fixture([apartment(**{HEADERS["valid_from"]: timestamp})]), as_of_date=AS_OF)


@pytest.mark.parametrize("start,end", [
    ("2026-10-06T00:00:00+02:00", "2026-10-06T23:59:59+02:00"),
    ("2026-10-01T00:00:00+02:00", "2026-10-04T23:59:59+02:00"),
    ("2026-10-05T09:00:00+02:00", "2026-10-04T09:00:00+02:00"),
])
def test_expired_future_and_reversed_intervals_fail_closed(start, end):
    row = apartment(**{HEADERS["valid_from"]: start, HEADERS["valid_until"]: end})
    with pytest.raises(ValueError, match="validity"):
        parse_bemovo_prices(csv_fixture([row]), as_of_date=AS_OF)


def test_local_day_intersection_and_both_fall_clock_offsets_are_supported():
    row = apartment(**{
        HEADERS["valid_from"]: "2026-10-04T22:01:00Z",
        HEADERS["valid_until"]: "2026-10-04T22:30:00Z",
    })
    assert len(parse_bemovo_prices(csv_fixture([row]), as_of_date=AS_OF)) == 1
    fold = apartment(**{
        HEADERS["valid_from"]: "2026-10-25T02:30:00+02:00",
        HEADERS["valid_until"]: "2026-10-25T02:30:00+01:00",
    })
    parsed = parse_bemovo_prices(csv_fixture([fold]), as_of_date=date(2026, 10, 25))[0]
    assert (parsed["valid_until"] - parsed["valid_from"]).total_seconds() == 3600


def test_missing_ambiguous_headers_and_ragged_csv_fail_closed():
    row = apartment()
    del row[HEADERS["nip"]]
    with pytest.raises(ValueError, match="Missing"):
        parse_bemovo_prices(csv_fixture([row]), as_of_date=AS_OF)
    ambiguous = apartment(**{BASE_PRICE_PREFIX + " second column": "800000"})
    with pytest.raises(ValueError, match="Exactly one"):
        parse_bemovo_prices(csv_fixture([ambiguous]), as_of_date=AS_OF)
    valid = csv_fixture([apartment()])
    with pytest.raises(ValueError, match="header count"):
        parse_bemovo_prices(valid + "extra,ragged,row\n", as_of_date=AS_OF)


def test_explicit_date_argument_and_nonempty_apartment_set_are_required():
    with pytest.raises(ValueError, match="datetime.date"):
        parse_bemovo_prices(csv_fixture([apartment()]), as_of_date=datetime(2026, 10, 5))
    excluded = apartment(**{HEADERS["property_type"]: "X"})
    with pytest.raises(ValueError, match="no validated"):
        parse_bemovo_prices(csv_fixture([excluded]), as_of_date=AS_OF)
