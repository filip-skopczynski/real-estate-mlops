"""OLX public-HTML parsing tests using only generated, fictional listings."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.fetch_data import FIELDS
from src.olx import parse_olx_search, parse_olx_search_page


FIXTURES = Path(__file__).parent / "fixtures"
PAGE_URL = "https://www.olx.pl/nieruchomosci/mieszkania/sprzedaz/warszawa/"
OBSERVED_AT = datetime(2026, 10, 7, 8, 15, tzinfo=timezone.utc)
AD_URL = "https://www.olx.pl/d/oferta/syntetyczne-mieszkanie-a-IDsyntheticA.html"


def parameter(key, normalized, display):
    return {"key": key, "normalizedValue": normalized, "value": display}


def apartment(**updates):
    """A synthetic ad with a deliberately inconsistent diagnostic PLN/m²."""
    return {
        "id": 990001,
        "url": AD_URL + "?utm_source=fixture&fbclid=test#photo",
        "externalUrl": "https://www.otodom.pl/pl/oferta/fictional-never-fetched",
        "title": "Sztuczne ogłoszenie testowe: 9 pokoi, 999 m²",
        "category": {"id": 14, "type": "real_estate"},
        "location": {
            "cityName": "Warszawa", "cityNormalizedName": "warszawa",
            "districtName": "Mokotów",
        },
        "price": {
            "budget": False, "free": False, "exchange": False,
            "regularPrice": {"value": 1355000, "currencyCode": "PLN"},
            "displayValue": "1 355 000 zł",
        },
        "params": [
            parameter("m", "67.61", "67,61 m²"),
            parameter("rooms", "three", "3 pokoje"),
            parameter("floor_select", "floor_3", "3"),
            parameter("price_per_m", "1", "1 zł/m²"),
        ],
        "map": {"lat": 52.2, "lon": 21.03, "show_detailed": False},
        "createdTime": "2025-01-01T00:00:00Z",
        "lastRefreshTime": "2026-10-06T06:00:00Z",
        **updates,
    }


def search_state(ads=None):
    return {
        "listing": {
            "listing": {
                "ads": [apartment()] if ads is None else ads,
                "categoryId": 14,
                "requestParams": {
                    "categoryPath": "nieruchomosci/mieszkania/sprzedaz/warszawa",
                },
                "pageNumber": 0,
            },
        },
        "categories": {
            "list": {"14": {"path": "nieruchomosci/mieszkania/sprzedaz"}},
        },
    }


def html_for(state, *, encoded_string=True):
    serialized = json.dumps(state, ensure_ascii=False)
    assignment = json.dumps(serialized, ensure_ascii=False) if encoded_string else serialized
    return (
        '<!doctype html><html><head><title>OLX synthetic test</title></head><body>'
        '<script id="olx-init-config">'
        'window.__PRERENDERED_STATE__ = ' + assignment + ';'
        '</script></body></html>'
    )


def records_for(ads, **kwargs):
    return parse_olx_search(html_for(search_state(ads)), PAGE_URL, **kwargs)


def set_param(ad, key, value, display=None):
    changed = deepcopy(ad)
    changed["params"] = [param for param in changed["params"] if param["key"] != key]
    if value is not None:
        changed["params"].append(parameter(key, value, str(value) if display is None else display))
    return changed


def test_fixture_exports_only_fictional_sale_apartments_with_common_fields():
    html = (FIXTURES / "olx_search.html").read_text(encoding="utf-8")
    rows = parse_olx_search(html, PAGE_URL, OBSERVED_AT)
    assert len(rows) == 2
    assert {row["listing_id"] for row in rows} == {"990001", "990002"}
    assert all(set(row) == set(FIELDS) for row in rows)
    first, second = rows
    assert first["source"] == second["source"] == "www.olx.pl"
    assert first["city"] == second["city"] == "Warszawa"
    assert first["district"] == "Mokotów"
    assert second["district"] == "Wola"
    assert first["price_pln"] == 1355000
    assert first["area_m2"] == 67.61
    assert first["rooms"] == 3
    assert first["floor"] == 3
    assert second["price_pln"] == 720000
    assert second["area_m2"] == 48.2
    assert second["rooms"] == 2
    assert second["floor"] is None
    assert all(row["observed_at"] == OBSERVED_AT for row in rows)


@pytest.mark.parametrize("encoded_string", [True, False])
def test_public_state_is_decoded_as_json_without_running_javascript(encoded_string):
    html = '<script>throw new Error("inert JavaScript");</script>' + html_for(
        search_state(), encoded_string=encoded_string,
    )
    with patch("builtins.eval", side_effect=AssertionError("must not execute JS")), patch(
        "builtins.exec", side_effect=AssertionError("must not execute JS"),
    ):
        assert records_for([apartment()])[0]["listing_id"] == "990001"
        assert parse_olx_search(html, PAGE_URL)[0]["listing_id"] == "990001"


def test_total_pln_price_actual_area_and_rooms_win_over_per_m2_and_title():
    row = records_for([apartment()])[0]
    assert row["price_pln"] == 1355000
    assert row["area_m2"] == 67.61
    assert row["rooms"] == 3
    # 1 PLN/m² and "999 m² / 9 pokoi" are deliberate traps in the input.
    assert row["area_m2"] != row["price_pln"]
    assert row["url"] == AD_URL
    assert "otodom" not in row["url"]


def test_per_m2_price_cannot_replace_missing_total_price():
    invalid = apartment()
    invalid["price"].pop("regularPrice")
    with pytest.raises(ValueError):
        records_for([invalid])


@pytest.mark.parametrize("key", ["m", "rooms"])
def test_missing_required_measurements_are_not_inferred_from_descriptions(key):
    invalid = set_param(apartment(), key, None)
    with pytest.raises(ValueError):
        records_for([invalid])


@pytest.mark.parametrize("rooms,expected", [("one", 1), ("two", 2), ("three", 3)])
def test_exact_room_enums_are_decoded(rooms, expected):
    row = records_for([set_param(apartment(), "rooms", rooms)])[0]
    assert row["rooms"] == expected


def test_four_or_more_rooms_are_not_misrepresented_as_exactly_four():
    invalid = set_param(apartment(), "rooms", "four", "4 i więcej")
    with pytest.raises(ValueError):
        records_for([invalid])


@pytest.mark.parametrize("floor,expected", [
    ("floor_-1", -1), ("floor_0", 0), ("floor_1", 1), ("floor_10", 10),
    ("floor_11", None), ("floor_17", None),
])
def test_exact_floors_and_non_exact_floor_buckets(floor, expected):
    row = records_for([set_param(apartment(), "floor_select", floor)])[0]
    assert row["floor"] == expected


def test_optional_features_remain_none_when_not_reported():
    ad = set_param(apartment(), "floor_select", None)
    ad["location"].pop("districtName")
    ad.pop("map")
    row = records_for([ad])[0]
    for field in ("district", "floor", "build_year", "latitude", "longitude", "distance_km"):
        assert row[field] is None


@pytest.mark.parametrize("show_detailed", [False, None])
def test_approximate_map_coordinates_are_not_exported_as_exact(show_detailed):
    map_data = {"lat": 52.2, "lon": 21.03}
    if show_detailed is not None:
        map_data["show_detailed"] = show_detailed
    row = records_for([apartment(map=map_data)])[0]
    assert row["latitude"] is None
    assert row["longitude"] is None
    assert row["distance_km"] is None


def test_explicit_detailed_coordinates_produce_distance():
    row = records_for([apartment(map={"lat": 52.2, "lon": 21.03, "show_detailed": True})])[0]
    assert row["latitude"] == 52.2
    assert row["longitude"] == 21.03
    assert row["distance_km"] is not None and row["distance_km"] > 0


@pytest.mark.parametrize("map_data", [
    {"lat": 52.2, "show_detailed": True},
    {"lon": 21.03, "show_detailed": True},
    {"lat": 91, "lon": 21.03, "show_detailed": True},
    {"lat": 52.2, "lon": 181, "show_detailed": True},
    {"lat": True, "lon": 21.03, "show_detailed": True},
])
def test_invalid_optional_coordinate_pair_is_not_partially_exported(map_data):
    row = records_for([apartment(map=map_data)])[0]
    assert row["latitude"] is None
    assert row["longitude"] is None
    assert row["distance_km"] is None


def test_explicit_foreign_city_house_and_rental_category_are_excluded():
    foreign = apartment(id=990010, location={"cityName": "Kraków", "cityNormalizedName": "krakow"})
    house = apartment(id=990011, category={"id": 17, "type": "real_estate"})
    rental = apartment(id=990012, category={"id": 15, "type": "real_estate"})
    rows = records_for([foreign, house, rental, apartment()])
    assert [row["listing_id"] for row in rows] == ["990001"]


@pytest.mark.parametrize("city_name,normalized", [
    ("Warszawa Zachodnia", "warszawa-zachodnia"),
    ("Nowa Warszawa", "nowa-warszawa"),
    ("Warszawa", "krakow"),
])
def test_warsaw_city_match_is_exact_and_normalized_identity_must_agree(city_name, normalized):
    invalid = apartment(location={"cityName": city_name, "cityNormalizedName": normalized})
    with pytest.raises(ValueError):
        records_for([invalid])


def test_advertising_widget_is_not_an_apartment_record():
    widget = {"type": "sponsored_banner", "campaign": "fictional", "price": "1 zł/m²"}
    rows = records_for([widget, apartment()])
    assert [row["listing_id"] for row in rows] == ["990001"]


@pytest.mark.parametrize("currency", ["EUR", "USD", None])
def test_only_explicit_pln_total_prices_are_accepted(currency):
    invalid = apartment()
    invalid["price"]["regularPrice"]["currencyCode"] = currency
    with pytest.raises(ValueError):
        records_for([invalid])


@pytest.mark.parametrize("flag", ["budget", "free", "exchange"])
def test_budget_free_and_exchange_ads_do_not_become_asking_price_records(flag):
    invalid = apartment()
    invalid["price"][flag] = True
    with pytest.raises(ValueError):
        records_for([invalid])


@pytest.mark.parametrize("price", [0, -100, True, None, "NaN"])
def test_malformed_total_price_is_not_silently_exported(price):
    invalid = apartment()
    invalid["price"]["regularPrice"]["value"] = price
    with pytest.raises(ValueError):
        records_for([invalid])


@pytest.mark.parametrize("key,value", [
    ("m", "0"), ("m", "-1"), ("m", "NaN"), ("m", "Infinity"),
    ("m", True), ("m", {"area": 67.61}),
    ("rooms", "five"), ("rooms", True), ("rooms", ""),
])
def test_malformed_required_parameters_fail_when_no_valid_ad_remains(key, value):
    with pytest.raises(ValueError):
        records_for([set_param(apartment(), key, value)])


def test_one_malformed_ad_warns_and_does_not_remove_other_valid_records(caplog):
    invalid = set_param(apartment(id=990003), "m", None)
    rows = records_for([invalid, apartment()])
    assert [row["listing_id"] for row in rows] == ["990001"]
    assert any(record.levelname == "WARNING" for record in caplog.records)


@pytest.mark.parametrize("value", [{"count": 3}, ["three"]])
def test_changed_rooms_value_schema_skips_only_affected_ad(value, caplog):
    invalid = set_param(apartment(id=990003), "rooms", value)
    rows = records_for([invalid, apartment()])
    assert [row["listing_id"] for row in rows] == ["990001"]
    assert any(record.levelname == "WARNING" for record in caplog.records)


def test_category_identifier_does_not_accept_float_schema_change():
    invalid = apartment(category={"id": 14.0, "type": "real_estate"})
    with pytest.raises(ValueError):
        records_for([invalid])


def test_identity_is_stable_across_trackers_title_refresh_and_capture_time():
    first = records_for([apartment()], observed_at=OBSERVED_AT)[0]
    changed = apartment(
        title="Nowy sztuczny tytuł", lastRefreshTime="2026-10-07T09:00:00Z",
        url=AD_URL + "?utm_campaign=other&gclid=other#top",
    )
    second = records_for([changed], observed_at=OBSERVED_AT + timedelta(hours=1))[0]
    assert first["listing_id"] == second["listing_id"] == "990001"
    assert first["url"] == second["url"] == AD_URL
    assert first["observed_at"] != second["observed_at"]


@pytest.mark.parametrize("url", [
    "https://other.example/d/oferta/synthetic.html",
    "http://www.olx.pl/d/oferta/synthetic.html",
    "javascript:alert(1)",
    "https://www.olx.pl/nieruchomosci/mieszkania/",
])
def test_listing_url_must_be_an_https_same_host_olx_offer(url):
    with pytest.raises(ValueError):
        records_for([apartment(url=url)])


@pytest.mark.parametrize("identifier", [0, -1, True, None, "990001"])
def test_missing_or_non_integer_source_identity_is_not_replaced_by_url_hash(identifier):
    with pytest.raises(ValueError):
        records_for([apartment(id=identifier)])


def test_exact_duplicate_ids_are_counted_once_after_url_canonicalization():
    duplicate = apartment(url=AD_URL + "?utm_medium=duplicate#top")
    rows = records_for([apartment(), duplicate])
    assert len(rows) == 1
    assert rows[0]["listing_id"] == "990001"


def test_same_canonical_url_cannot_have_two_source_identities():
    with pytest.raises(ValueError):
        records_for([apartment(), apartment(id=990002, url=AD_URL)])


@pytest.mark.parametrize("change", ["price", "area", "rooms", "district", "url"])
def test_duplicate_source_id_with_conflicting_record_content_fails_closed(change):
    duplicate = apartment()
    if change == "price":
        duplicate["price"]["regularPrice"]["value"] = 1300000
    elif change == "area":
        duplicate = set_param(duplicate, "m", "70")
    elif change == "rooms":
        duplicate = set_param(duplicate, "rooms", "two")
    elif change == "district":
        duplicate["location"]["districtName"] = "Wola"
    else:
        duplicate["url"] = "https://www.olx.pl/d/oferta/syntetyczne-inne-IDsyntheticB.html"
    with pytest.raises(ValueError):
        records_for([apartment(), duplicate])


def test_district_is_taken_from_location_and_not_from_title_or_search_url():
    ad = apartment(title="Sztuczne mieszkanie Bemowo", location={
        "cityName": "Warszawa", "cityNormalizedName": "warszawa", "districtName": "Żoliborz",
    })
    row = parse_olx_search(html_for(search_state([ad])), PAGE_URL + "?search[district_id]=bemowo")[0]
    assert row["district"] == "Żoliborz"


def test_capture_timestamp_is_shared_utc_and_not_the_listing_publication_time():
    local_time = OBSERVED_AT.astimezone(timezone(timedelta(hours=2)))
    other = apartment(id=990002, url=AD_URL.replace("syntheticA", "syntheticB"))
    rows = records_for([apartment(), other], observed_at=local_time.isoformat())
    assert all(row["observed_at"] == OBSERVED_AT for row in rows)
    assert all(row["observed_at"].tzinfo is timezone.utc for row in rows)


def test_default_capture_is_aware_utc_and_identical_for_whole_page():
    other = apartment(id=990002, url=AD_URL.replace("syntheticA", "syntheticB"))
    before = datetime.now(timezone.utc)
    rows = records_for([apartment(), other])
    after = datetime.now(timezone.utc)
    assert before <= rows[0]["observed_at"] <= after
    assert rows[0]["observed_at"] == rows[1]["observed_at"]
    assert rows[0]["observed_at"].tzinfo is timezone.utc


@pytest.mark.parametrize("timestamp", [datetime(2026, 10, 7), "2026-10-07T08:15:00", "not-a-date"])
def test_invalid_capture_timestamp_is_rejected(timestamp):
    with pytest.raises(ValueError):
        records_for([apartment()], observed_at=timestamp)


@pytest.mark.parametrize("ads", [[], None, "not-a-list", {"items": []}])
def test_empty_or_changed_ads_schema_does_not_look_like_success(ads):
    state = search_state()
    state["listing"]["listing"]["ads"] = ads
    with pytest.raises(ValueError):
        parse_olx_search(html_for(state), PAGE_URL)


@pytest.mark.parametrize("changed", [
    "missing_listing", "missing_ads", "wrong_category", "wrong_category_path", "wrong_request_path",
    "float_category",
])
def test_required_search_schema_and_sale_context_are_checked(changed):
    state = search_state()
    listing = state["listing"]["listing"]
    if changed == "missing_listing":
        state.pop("listing")
    elif changed == "missing_ads":
        listing.pop("ads")
    elif changed == "wrong_category":
        listing["categoryId"] = 15
    elif changed == "float_category":
        listing["categoryId"] = 14.0
    elif changed == "wrong_category_path":
        state["categories"]["list"]["14"]["path"] = "nieruchomosci/mieszkania/wynajem"
    else:
        listing["requestParams"]["categoryPath"] = "nieruchomosci/mieszkania/sprzedaz/krakow"
    with pytest.raises(ValueError):
        parse_olx_search(html_for(state), PAGE_URL)


@pytest.mark.parametrize("category_path", [None, 123, ["nieruchomosci/mieszkania/sprzedaz/warszawa"]])
def test_changed_required_request_path_type_has_clear_parser_error(category_path):
    state = search_state()
    state["listing"]["listing"]["requestParams"]["categoryPath"] = category_path
    with pytest.raises(ValueError):
        parse_olx_search(html_for(state), PAGE_URL)


@pytest.mark.parametrize("page_number", [True, 1])
def test_state_must_confirm_integer_first_page(page_number):
    state = search_state()
    state["listing"]["listing"]["pageNumber"] = page_number
    with pytest.raises(ValueError):
        parse_olx_search(html_for(state), PAGE_URL)


@pytest.mark.parametrize("page_url", [
    PAGE_URL + "?page=2",
    "https://www.olx.pl/nieruchomosci/mieszkania/wynajem/warszawa/",
])
def test_page_url_cannot_claim_second_page_or_rental_scope(page_url):
    with pytest.raises(ValueError):
        parse_olx_search(html_for(search_state()), page_url)


@pytest.mark.parametrize("html", [
    "", "<html>no public state</html>",
    '<html><title>Just a moment...</title><div id="cf-challenge-running">Challenge</div></html>',
    '<script id="olx-init-config">window.__PRERENDERED_STATE__ = {broken};</script>',
    '<script id="olx-init-config">window.__PRERENDERED_STATE__ = alert("must not run");</script>',
])
def test_missing_malformed_or_challenge_html_fails_honestly(html):
    with pytest.raises(ValueError):
        parse_olx_search(html, PAGE_URL)


def test_challenge_title_is_rejected_even_if_valid_state_is_also_present():
    html = html_for(search_state()).replace("OLX synthetic test", "Just a moment...")
    with pytest.raises(ValueError):
        parse_olx_search(html, PAGE_URL)


def test_multiple_public_state_blocks_are_not_silently_combined():
    html = html_for(search_state()) + html_for(search_state())
    with pytest.raises(ValueError):
        parse_olx_search(html, PAGE_URL)


@pytest.mark.parametrize("fragment", [
    '"audit": 1, "audit": 1',
    '"audit": NaN',
    '"audit": Infinity',
    '"audit": 1e999',
])
def test_duplicate_json_keys_and_nonfinite_json_numbers_are_rejected(fragment):
    # The real listing remains valid; a permissive JSON decoder would export
    # it and ignore these extra keys. This exercises strict JSON validation.
    serialized = json.dumps(search_state(), ensure_ascii=False)[:-1] + ',' + fragment + '}'
    html = '<script id="olx-init-config">window.__PRERENDERED_STATE__ = ' + json.dumps(serialized) + ';</script>'
    with pytest.raises(ValueError):
        parse_olx_search(html, PAGE_URL)


def paginated_state(page=2, ads=None):
    state = search_state(ads)
    catalogue = state["listing"]["listing"]
    catalogue.update(pageNumber=page - 1, totalPages=25, totalElements=1000, visibleElements=4437)
    catalogue["requestParams"]["page"] = page - 1
    return state


def test_pagination_is_explicit_and_reports_source_result_cap():
    html = html_for(paginated_state())
    url = PAGE_URL + "?page=2"
    with pytest.raises(ValueError):
        parse_olx_search(html, url, OBSERVED_AT)
    rows, metadata = parse_olx_search_page(html, url, OBSERVED_AT, expected_page=2)
    assert len(rows) == 1
    assert metadata == {
        "page": 2, "page_number": 1, "raw_items": 1, "parsed_listings": 1,
        "skipped_items": 0, "skip_reasons": {}, "total_pages": 25,
        "total_elements": 1000, "visible_elements": 4437, "reported_result_cap": True,
    }


@pytest.mark.parametrize("page", [0, -1, True, 2.0, "2", 1001])
def test_invalid_expected_page_is_rejected(page):
    with pytest.raises(ValueError):
        parse_olx_search_page(html_for(paginated_state()), PAGE_URL + "?page=2", expected_page=page)


@pytest.mark.parametrize("query", ["", "?page=1", "?page=02", "?page=2&page=2", "?page=2&PAGE=2", "?%70age=2&page=2"])
def test_pagination_url_must_unambiguously_match_requested_page(query):
    with pytest.raises(ValueError):
        parse_olx_search_page(html_for(paginated_state()), PAGE_URL + query, expected_page=2)


@pytest.mark.parametrize("field,value", [
    ("pageNumber", 0), ("pageNumber", True), ("pageNumber", "1"),
    ("totalPages", 1), ("totalPages", -1), ("totalPages", True),
    ("totalElements", "1000"), ("visibleElements", -1),
])
def test_pagination_state_mismatch_and_invalid_counts_fail(field, value):
    state = paginated_state()
    state["listing"]["listing"][field] = value
    with pytest.raises(ValueError):
        parse_olx_search_page(html_for(state), PAGE_URL + "?page=2", expected_page=2)


def test_pagination_request_state_cannot_disagree_with_page_number():
    state = paginated_state()
    state["listing"]["listing"]["requestParams"]["page"] = 0
    with pytest.raises(ValueError):
        parse_olx_search_page(html_for(state), PAGE_URL + "?page=2", expected_page=2)


def test_pagination_can_report_empty_eligible_page_and_skips_without_inference():
    invalid = set_param(apartment(), "rooms", "four")
    rows, metadata = parse_olx_search_page(
        html_for(paginated_state(2, [invalid])), PAGE_URL + "?page=2", expected_page=2,
    )
    assert rows == []
    assert metadata["raw_items"] == metadata["skipped_items"] == 1
    assert metadata["parsed_listings"] == 0


def test_missing_pagination_metadata_remains_unknown():
    _, metadata = parse_olx_search_page(html_for(search_state()), PAGE_URL)
    assert metadata["total_pages"] is None
    assert metadata["total_elements"] is None
    assert metadata["visible_elements"] is None
    assert metadata["reported_result_cap"] is False
