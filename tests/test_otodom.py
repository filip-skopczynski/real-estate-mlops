"""Otodom public-HTML parsing tests with fictional apartment records only."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.fetch_data import FIELDS
from src.otodom import parse_otodom_search


FIXTURES = Path(__file__).parent / "fixtures"
WARSAW = "mazowieckie/warszawa/warszawa/warszawa"
SEARCH_PATH = "/pl/wyniki/sprzedaz/mieszkanie/" + WARSAW
PAGE_URL = "https://www.otodom.pl" + SEARCH_PATH
OBSERVED_AT = datetime(2026, 10, 7, 11, 30, tzinfo=timezone.utc)
SLUG = "fikcyjne-mieszkanie-testowe-IDsyntheticA"
AD_URL = "https://www.otodom.pl/pl/oferta/" + SLUG


def location(city_id=WARSAW, city_name="Warszawa", district="Mokotów"):
    locations = [{
        "id": city_id, "locationLevel": "city_or_village", "name": city_name,
        "fullName": city_name + ", mazowieckie", "__typename": "BasicLocationObject",
    }]
    if district is not None:
        district_slug = district.lower().translate(str.maketrans("ąćęłńóśżź", "acelnoszz")).replace(" ", "-")
        locations.append({
            "id": city_id + "/" + district_slug, "locationLevel": "district", "name": district,
            "fullName": district + ", " + city_name + ", mazowieckie",
            "__typename": "BasicLocationObject",
        })
    return {
        "mapDetails": {"radius": 0},
        "address": {"city": None, "province": None},
        "reverseGeocoding": {"locations": locations},
    }


def apartment(**updates):
    """A fictional offer; title and price/m² deliberately disagree with data."""
    result = {
        "id": 990101,
        "slug": SLUG,
        "href": "[lang]/ad/" + SLUG,
        "title": "Fikcyjny przykład 9 pokoi i 999 m²",
        "estate": "FLAT", "transaction": "SELL", "hidePrice": False,
        "source": "urn:partner:fictional-fixture", "isCrossListed": False,
        "totalPrice": {"value": 1355000, "currency": "PLN"},
        "pricePerSquareMeter": {"value": 1, "currency": "PLN"},
        "areaInSquareMeters": 67.61, "roomsNumber": "THREE", "floorNumber": "THIRD",
        "location": location(),
        "dateCreated": "2025-01-01T00:00:00Z",
        "pushedUpAt": "2026-10-06T08:00:00Z",
        **updates,
    }
    if "slug" in updates and "href" not in updates:
        result["href"] = "[lang]/ad/" + str(updates["slug"])
    return result


def search_state(items=None):
    return {
        "props": {"pageProps": {
            "estate": "FLAT", "transaction": "SELL", "location": WARSAW,
            "canonicalURL": SEARCH_PATH,
            "filteringQueryParams": {"page": 1},
            "data": {"searchAds": {
                "items": [apartment()] if items is None else items,
                "pagination": {"currentPage": 1},
            }},
        }},
        "page": "/[lang]/results/[...searchingCriteria]",
    }


def html_for(state):
    return (
        '<!doctype html><html><head><title>Otodom fictional test</title></head><body>'
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(state, ensure_ascii=False) + '</script></body></html>'
    )


def records_for(items, **kwargs):
    return parse_otodom_search(html_for(search_state(items)), PAGE_URL, **kwargs)


def test_synthetic_fixture_exports_only_valid_warsaw_sale_apartments():
    html = (FIXTURES / "otodom_search.html").read_text(encoding="utf-8")
    rows = parse_otodom_search(html, PAGE_URL, OBSERVED_AT)
    assert len(rows) == 2
    first, second = rows
    assert {row["listing_id"] for row in rows} == {"990101", "990102"}
    assert all(set(row) == set(FIELDS) for row in rows)
    assert first["source"] == second["source"] == "www.otodom.pl"
    assert first["city"] == second["city"] == "Warszawa"
    assert first["district"] == "Mokotów"
    assert second["district"] == "Wola"
    assert first["price_pln"] == 1355000
    assert first["area_m2"] == 67.61
    assert first["rooms"] == 3 and first["floor"] == 3
    assert second["price_pln"] == 720000
    assert second["area_m2"] == 48.2
    assert second["rooms"] == 2 and second["floor"] is None
    assert all(row["observed_at"] == OBSERVED_AT for row in rows)


def test_embedded_state_is_json_without_javascript_execution():
    html = '<script>throw new Error("inert JavaScript");</script>' + html_for(search_state())
    with patch("builtins.eval", side_effect=AssertionError("no JS execution")), patch(
        "builtins.exec", side_effect=AssertionError("no JS execution"),
    ):
        assert parse_otodom_search(html, PAGE_URL)[0]["listing_id"] == "990101"


def test_actual_total_price_area_and_rooms_win_over_title_and_price_per_m2():
    row = records_for([apartment()])[0]
    assert row["price_pln"] == 1355000
    assert row["area_m2"] == 67.61
    assert row["rooms"] == 3
    assert row["url"] == AD_URL
    assert row["source"] != "urn:partner:fictional-fixture"


@pytest.mark.parametrize("field", ["totalPrice", "areaInSquareMeters", "roomsNumber"])
def test_missing_required_measurements_are_not_inferred_from_descriptions(field):
    invalid = apartment()
    invalid.pop(field)
    with pytest.raises(ValueError):
        records_for([invalid])


def test_price_per_m2_cannot_replace_missing_total_price():
    invalid = apartment(totalPrice=None)
    with pytest.raises(ValueError):
        records_for([invalid])


@pytest.mark.parametrize("currency", [None, "EUR", "USD", "pln"])
def test_only_explicit_pln_total_price_is_accepted(currency):
    with pytest.raises(ValueError):
        records_for([apartment(totalPrice={"value": 1355000, "currency": currency})])


@pytest.mark.parametrize("price", [0, -100, True, None, "NaN", "1355000", {"value": 100}])
def test_invalid_required_price_fails_if_no_valid_records_remain(price):
    with pytest.raises(ValueError):
        records_for([apartment(totalPrice={"value": price, "currency": "PLN"})])


@pytest.mark.parametrize("area", [0, -1, True, None, "NaN", "67.61", "67,61 m²", {"value": 67.61}])
def test_invalid_area_cannot_be_guessed_from_price_or_title(area):
    with pytest.raises(ValueError):
        records_for([apartment(areaInSquareMeters=area)])


@pytest.mark.parametrize("rooms,expected", [("ONE", 1), ("TWO", 2), ("THREE", 3), ("FOUR", 4)])
def test_confirmed_exact_room_enums_are_decoded(rooms, expected):
    assert records_for([apartment(roomsNumber=rooms)])[0]["rooms"] == expected


@pytest.mark.parametrize("rooms", [None, True, 3, "three", "FIVE", "SIX", "TEN", "FOUR_OR_MORE", "TEN_OR_MORE", [], {}])
def test_unknown_or_ambiguous_rooms_are_not_exported_as_exact(rooms):
    with pytest.raises(ValueError):
        records_for([apartment(roomsNumber=rooms)])


@pytest.mark.parametrize("floor,expected", [
    ("GROUND", 0), ("FIRST", 1), ("SECOND", 2), ("THIRD", 3), ("FOURTH", 4),
    ("FIFTH", 5), ("SIXTH", 6), ("SEVENTH", 7), ("EIGHTH", 8), ("NINTH", 9),
    ("TENTH", 10), ("ABOVE_TENTH", None), (None, None), ("UNKNOWN", None),
])
def test_exact_floor_enums_and_buckets_are_distinguished(floor, expected):
    assert records_for([apartment(floorNumber=floor)])[0]["floor"] == expected


def test_optional_features_remain_none_when_unavailable():
    row = records_for([apartment(location=location(district=None), floorNumber=None)])[0]
    for field in ("district", "floor", "build_year", "latitude", "longitude", "distance_km"):
        assert row[field] is None


def test_map_radius_without_coordinates_is_not_exported_as_a_location():
    row = records_for([apartment()])[0]
    assert row["latitude"] is None
    assert row["longitude"] is None
    assert row["distance_km"] is None


def test_foreign_city_rental_house_and_investment_cards_are_excluded():
    ads = [
        apartment(id=990110, location=location("malopolskie/krakow/krakow/krakow", "Kraków")),
        apartment(id=990111, transaction="RENT"),
        apartment(id=990112, estate="HOUSE"),
        apartment(id=990113, estate="INVESTMENT", totalPrice=None, roomsNumber=None),
        {"type": "ADVERTISING_WIDGET", "totalPrice": 1},
        apartment(),
    ]
    assert [row["listing_id"] for row in records_for(ads)] == ["990101"]


@pytest.mark.parametrize("city_id,city_name", [
    (WARSAW, "Warszawa Zachodnia"),
    (WARSAW, "Kraków"),
    ("mazowieckie/warszawa/warszawa/nowa-warszawa", "Warszawa"),
])
def test_city_identity_requires_exact_matching_id_and_name(city_id, city_name):
    with pytest.raises(ValueError):
        records_for([apartment(location=location(city_id, city_name))])


def test_city_is_not_inferred_from_search_context_when_missing_in_record():
    changed = location()
    changed["reverseGeocoding"]["locations"] = [
        item for item in changed["reverseGeocoding"]["locations"]
        if item["locationLevel"] != "city_or_village"
    ]
    with pytest.raises(ValueError):
        records_for([apartment(location=changed)])


def test_conflicting_city_records_fail_for_affected_offer():
    changed = location()
    changed["reverseGeocoding"]["locations"].append({
        "id": "malopolskie/krakow/krakow/krakow", "name": "Kraków",
        "locationLevel": "city_or_village",
    })
    with pytest.raises(ValueError):
        records_for([apartment(location=changed)])


def test_district_comes_from_location_not_title_or_search_filters():
    row = parse_otodom_search(
        html_for(search_state([apartment(title="Fikcyjne Bemowo", location=location(district="Żoliborz"))])),
        PAGE_URL + "?district=bemowo",
    )[0]
    assert row["district"] == "Żoliborz"


def test_district_outside_confirmed_warsaw_city_does_not_become_a_warsaw_district():
    changed = location()
    changed["reverseGeocoding"]["locations"][1]["id"] = "malopolskie/krakow/krakow/krakow/podgorze"
    with pytest.raises(ValueError):
        records_for([apartment(location=changed)])


def test_empty_district_identity_is_rejected_even_when_a_display_name_exists():
    changed = location()
    changed["reverseGeocoding"]["locations"][1]["id"] = WARSAW + "/"
    with pytest.raises(ValueError):
        records_for([apartment(location=changed)])


def test_ambiguous_district_does_not_silently_choose_the_first():
    changed = location()
    changed["reverseGeocoding"]["locations"].append({
        "id": WARSAW + "/wola", "locationLevel": "district", "name": "Wola",
    })
    with pytest.raises(ValueError):
        records_for([apartment(location=changed)])


def test_hidden_price_is_not_exported_even_when_state_contains_an_amount():
    with pytest.raises(ValueError):
        records_for([apartment(hidePrice=True)])


@pytest.mark.parametrize("identifier", [0, -1, True, None, "990101", 990101.0])
def test_source_id_must_be_a_positive_integer_and_is_not_replaced_by_a_hash(identifier):
    with pytest.raises(ValueError):
        records_for([apartment(id=identifier)])


@pytest.mark.parametrize("slug", [
    None, "", 990101, "no-stable-source-suffix", "bad/slash-IDsyntheticA",
    "https://other.example/ad-IDsyntheticA", "bad?tracker=1-IDsyntheticA",
    "bad#fragment-IDsyntheticA",
])
def test_invalid_or_foreign_slug_cannot_produce_a_canonical_offer_url(slug):
    with pytest.raises(ValueError):
        records_for([apartment(slug=slug)])


def test_url_uses_confirmed_slug_and_template_not_cross_listed_origin():
    ad = apartment(isCrossListed=True)
    row = records_for([ad])[0]
    assert row["url"] == AD_URL
    assert row["source"] == "www.otodom.pl"


@pytest.mark.parametrize("href", [
    None, "", "https://other.example/untrusted-link", "[lang]/ad/other-IDdifferent",
    "hpr/[lang]/ad/" + SLUG, {"url": "[lang]/ad/" + SLUG},
])
def test_unknown_or_promoted_presentation_templates_are_not_source_offers(href):
    with pytest.raises(ValueError):
        records_for([apartment(href=href)])


def test_promoted_hpr_presentation_of_the_same_offer_is_not_a_second_identity():
    promoted = apartment(id=99010100067, href="hpr/[lang]/ad/" + SLUG)
    rows = records_for([promoted, apartment()])
    assert len(rows) == 1
    assert rows[0]["listing_id"] == "990101"


def test_identity_is_stable_across_title_refresh_and_observation_time():
    first = records_for([apartment()], observed_at=OBSERVED_AT)[0]
    second = records_for([apartment(title="Inny fikcyjny tytuł", pushedUpAt="2026-10-07T10:00:00Z")],
                         observed_at=OBSERVED_AT + timedelta(hours=1))[0]
    assert first["listing_id"] == second["listing_id"] == "990101"
    assert first["url"] == second["url"] == AD_URL
    assert first["observed_at"] != second["observed_at"]


def test_exact_duplicate_source_ids_are_counted_once():
    assert len(records_for([apartment(), deepcopy(apartment())])) == 1


def test_same_canonical_offer_url_cannot_have_two_source_identities():
    with pytest.raises(ValueError):
        records_for([apartment(), apartment(id=990102)])


@pytest.mark.parametrize("change", ["price", "area", "rooms", "district", "slug"])
def test_conflicting_duplicate_ids_fail_the_whole_preview(change):
    duplicate = apartment()
    if change == "price":
        duplicate["totalPrice"]["value"] = 1300000
    elif change == "area":
        duplicate["areaInSquareMeters"] = 70
    elif change == "rooms":
        duplicate["roomsNumber"] = "TWO"
    elif change == "district":
        duplicate["location"] = location(district="Wola")
    else:
        duplicate["slug"] = "fikcyjne-inne-mieszkanie-IDsyntheticB"
        duplicate["href"] = "[lang]/ad/" + duplicate["slug"]
    with pytest.raises(ValueError):
        records_for([apartment(), duplicate])


def test_malformed_record_warns_without_discarding_other_valid_offers(caplog):
    invalid = apartment(id=990103, areaInSquareMeters=None)
    rows = records_for([invalid, apartment()])
    assert [row["listing_id"] for row in rows] == ["990101"]
    assert any(record.levelname == "WARNING" for record in caplog.records)


def test_capture_time_is_shared_utc_not_publication_or_refresh_time():
    local_time = OBSERVED_AT.astimezone(timezone(timedelta(hours=2)))
    second = apartment(id=990102, slug="fikcyjne-inne-IDsyntheticB")
    rows = records_for([apartment(), second], observed_at=local_time.isoformat())
    assert all(row["observed_at"] == OBSERVED_AT for row in rows)
    assert all(row["observed_at"].tzinfo is timezone.utc for row in rows)


def test_default_timestamp_is_aware_utc_and_identical_for_the_page():
    second = apartment(id=990102, slug="fikcyjne-inne-IDsyntheticB")
    before = datetime.now(timezone.utc)
    rows = records_for([apartment(), second])
    after = datetime.now(timezone.utc)
    assert before <= rows[0]["observed_at"] <= after
    assert rows[0]["observed_at"] == rows[1]["observed_at"]
    assert rows[0]["observed_at"].tzinfo is timezone.utc


@pytest.mark.parametrize("timestamp", [datetime(2026, 10, 7), "2026-10-07T11:30:00", "invalid"])
def test_invalid_or_naive_observation_time_is_rejected(timestamp):
    with pytest.raises(ValueError):
        records_for([apartment()], observed_at=timestamp)


@pytest.mark.parametrize("change", [
    "props", "pageProps", "data", "searchAds", "items", "pagination",
    "estate", "transaction", "location", "canonicalURL",
    "filter_page", "filter_page_bool", "filter_page_float", "current_page", "current_page_bool",
])
def test_required_page_schema_and_warsaw_sale_first_page_context_are_checked(change):
    state = search_state()
    page = state["props"]["pageProps"]
    if change == "props":
        state.pop("props")
    elif change == "pageProps":
        state["props"].pop("pageProps")
    elif change == "data":
        page.pop("data")
    elif change in ("searchAds",):
        page["data"].pop(change)
    elif change in ("items", "pagination"):
        page["data"]["searchAds"].pop(change)
    elif change == "estate":
        page[change] = "HOUSE"
    elif change == "transaction":
        page[change] = "RENT"
    elif change == "location":
        page[change] = "malopolskie/krakow/krakow/krakow"
    elif change == "canonicalURL":
        page[change] = SEARCH_PATH.replace("sprzedaz", "wynajem")
    elif change == "filter_page":
        page["filteringQueryParams"]["page"] = 2
    elif change == "filter_page_bool":
        page["filteringQueryParams"]["page"] = True
    elif change == "filter_page_float":
        page["filteringQueryParams"]["page"] = 1.0
    elif change == "current_page":
        page["data"]["searchAds"]["pagination"]["currentPage"] = 2
    else:
        page["data"]["searchAds"]["pagination"]["currentPage"] = True
    with pytest.raises(ValueError):
        parse_otodom_search(html_for(state), PAGE_URL)


@pytest.mark.parametrize("items", [[], None, "not-a-list", {"items": []}])
def test_empty_or_changed_items_schema_does_not_look_like_success(items):
    state = search_state()
    state["props"]["pageProps"]["data"]["searchAds"]["items"] = items
    with pytest.raises(ValueError):
        parse_otodom_search(html_for(state), PAGE_URL)


@pytest.mark.parametrize("page_url", [
    PAGE_URL + "?page=2",
    PAGE_URL.replace("sprzedaz", "wynajem"),
    PAGE_URL.replace("mieszkanie", "dom"),
    PAGE_URL.replace("https://", "http://"),
    PAGE_URL.replace("www.otodom.pl", "other.example"),
    PAGE_URL + "#fragment",
])
def test_search_url_must_confirm_supported_first_page_scope(page_url):
    with pytest.raises(ValueError):
        parse_otodom_search(html_for(search_state()), page_url)


@pytest.mark.parametrize("html", [
    "", "<html>No public state</html>",
    '<html><title>Just a moment...</title><div id="cf-challenge-running">Challenge</div></html>',
    '<script id="__NEXT_DATA__" type="application/json">{broken}</script>',
    '<script id="__NEXT_DATA__" type="application/json">alert("must not run")</script>',
    '<script id="__NEXT_DATA__" type="application/json">null</script>',
])
def test_missing_malformed_or_challenge_html_fails_honestly(html):
    with pytest.raises(ValueError):
        parse_otodom_search(html, PAGE_URL)


def test_challenge_title_is_rejected_even_when_valid_state_is_present():
    html = html_for(search_state()).replace("Otodom fictional test", "Just a moment...")
    with pytest.raises(ValueError):
        parse_otodom_search(html, PAGE_URL)


def test_multiple_public_state_blocks_are_not_silently_combined():
    with pytest.raises(ValueError):
        parse_otodom_search(html_for(search_state()) + html_for(search_state()), PAGE_URL)


@pytest.mark.parametrize("fragment", [
    '"audit": 1, "audit": 1', '"audit": NaN', '"audit": Infinity', '"audit": 1e999',
])
def test_duplicate_json_keys_and_nonfinite_numbers_fail_strict_decoding(fragment):
    serialized = json.dumps(search_state(), ensure_ascii=False)[:-1] + ',' + fragment + '}'
    html = '<script id="__NEXT_DATA__" type="application/json">' + serialized + '</script>'
    with pytest.raises(ValueError):
        parse_otodom_search(html, PAGE_URL)
