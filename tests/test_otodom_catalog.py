"""Fictional discovery scenarios; tests never contact a portal or database."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

from bs4 import BeautifulSoup
import pytest

from src import otodom_catalog as catalog
from src.fetch_otodom import OtodomError

MOMENT = datetime(2026, 10, 7, 15, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parent / "fixtures/otodom_search.html"


def state():
    return json.loads(BeautifulSoup(FIXTURE.read_text(encoding="utf-8"), "html.parser")
                      .find("script", id="__NEXT_DATA__").string)


def page(number=1, total_pages=1, identifiers=(990101,), mutate=None):
    value = state()
    details = value["props"]["pageProps"]
    base = details["data"]["searchAds"]["items"][0]
    items = []
    for identifier in identifiers:
        ad = deepcopy(base)
        ad["id"] = identifier
        ad["slug"] = f"syntetyczne-mieszkanie-IDtest{identifier}"
        ad["href"] = "[lang]/ad/" + ad["slug"]
        items.append(ad)
    details["filteringQueryParams"]["page"] = number
    details["sortingOption"] = {"by": "LATEST", "direction": "DESC"}
    details["data"]["searchAds"] = {
        "items": items,
        "pagination": {"currentPage": number, "itemsPerPage": 36,
                       "totalItems": 36 * total_pages, "totalPages": total_pages},
    }
    if mutate:
        mutate(details)
    return '<html><script id="__NEXT_DATA__" type="application/json">' + json.dumps(value) + "</script></html>"


def parse(html, number=1):
    return catalog.parse_otodom_catalog_page(html, catalog.transport._page_url(catalog.SCOPE_URL, number),
                                             MOMENT, expected_page=number)


def collect(pages, **kwargs):
    return catalog.collect_otodom_catalog(html_pages=pages, observed_at=MOMENT, **kwargs)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    from curl_cffi import requests
    monkeypatch.setattr(requests, "Session", lambda *args, **kwargs: pytest.fail("Network session forbidden"))


def test_sparse_discovery_retains_an_identity_that_preview_cannot_use():
    def mutate(details):
        ad = details["data"]["searchAds"]["items"][0]
        ad.update(hidePrice=True, areaInSquareMeters=None, roomsNumber="SIX")
    rows, audit = parse(page(mutate=mutate))
    assert len(rows) == 1
    assert set(rows[0]) == set(catalog.CATALOG_FIELDS)
    assert rows[0]["price_pln"] is None
    assert rows[0]["area_m2"] is None
    assert rows[0]["rooms"] is None
    assert rows[0]["rooms_min"] is None
    assert audit["sparse_price_count"] == audit["sparse_area_count"] == audit["sparse_rooms_count"] == 1


def test_verified_five_room_code_keeps_exact_count_in_catalogue_only():
    rows, _ = parse(page(mutate=lambda details: details["data"]["searchAds"]["items"][0].update(roomsNumber="FIVE")))
    assert rows[0]["rooms"] == 5 and rows[0]["rooms_min"] is None


@pytest.mark.parametrize("field,value", [
    ("areaInSquareMeters", None), ("areaInSquareMeters", "70"),
    ("areaInSquareMeters", 0), ("areaInSquareMeters", -1),
    ("areaInSquareMeters", True), ("totalPrice", None),
    ("totalPrice", {"value": "900000", "currency": "PLN"}),
    ("totalPrice", {"value": 900000, "currency": "EUR"}),
    ("totalPrice", {"value": 0, "currency": "PLN"}),
    ("totalPrice", {"value": True, "currency": "PLN"}),
    ("hidePrice", "false"), ("hidePrice", None),
])
def test_unverified_numbers_are_null_without_losing_identity(field, value):
    html = page(mutate=lambda details: details["data"]["searchAds"]["items"][0].update({field: value}))
    rows, _ = parse(html)
    key = "area_m2" if field == "areaInSquareMeters" else "price_pln"
    assert len(rows) == 1 and rows[0][key] is None


def test_source_creation_and_promotion_timestamps_are_not_fabricated_publication():
    rows, _ = parse(page())
    assert rows[0]["published_at"] is None
    assert rows[0]["observed_at"] == MOMENT
    assert all(rows[0][field] is None for field in ("latitude", "longitude", "distance_km", "build_year"))


@pytest.mark.parametrize("field,value", [
    ("estate", "INVESTMENT"), ("transaction", "RENT"), ("id", True),
    ("id", 0), ("id", "990101"), ("slug", "../../outside"),
    ("href", "hpr/[lang]/ad/syntetyczne-mieszkanie-IDtest990101"),
])
def test_unconfirmed_identity_and_investment_presentations_are_rejected(field, value):
    rows, metadata = parse(page(mutate=lambda details: details["data"]["searchAds"]["items"][0].update({field: value})))
    assert rows == []
    assert metadata["skipped_items"] == 1


def test_ambiguous_or_other_city_is_rejected():
    def mutate(details):
        details["data"]["searchAds"]["items"][0]["location"]["reverseGeocoding"]["locations"][0]["name"] = "Kraków"
    rows, metadata = parse(page(mutate=mutate))
    assert rows == [] and metadata["skipped_by_reason"] == {"unconfirmed_warsaw_location": 1}


@pytest.mark.parametrize("sorting", [None, {"by": "DEFAULT", "direction": "DESC"}, {"by": "LATEST", "direction": "ASC"}, {"by": "LATEST"}])
def test_requested_newest_order_must_be_confirmed(sorting):
    html = page(mutate=lambda details: details.update(sortingOption=sorting))
    with pytest.raises(ValueError, match="sorting"):
        parse(html)


@pytest.mark.parametrize("suffix", ["", "?by=DEFAULT&direction=DESC", "?by=LATEST&direction=DESC&priceMax=900000", "?by=LATEST&by=LATEST&direction=DESC"])
def test_catalogue_scope_rejects_missing_or_ambiguous_sort_and_hidden_filters(suffix):
    with pytest.raises(ValueError, match="scope"):
        catalog.parse_otodom_catalog_page(page(), catalog.transport.DEFAULT_URL + suffix, MOMENT)


def test_valid_empty_advertised_catalogue_completes():
    def empty(details):
        details["data"]["searchAds"]["pagination"].update(totalItems=0, totalPages=0)
    rows, audit = collect([page(identifiers=(), mutate=empty)])
    assert rows == [] and audit["completed"] is True
    assert audit["termination"] == "pagination_end"


def test_all_investment_page_does_not_terminate_later_property_discovery():
    rows, audit = collect([
        page(1, 2, mutate=lambda details: details["data"]["searchAds"]["items"][0].update(estate="INVESTMENT")),
        page(2, 2, identifiers=(990102,)),
    ])
    assert [row["listing_id"] for row in rows] == ["990102"]
    assert audit["completed"] is True and audit["pages_read"] == 2


def test_bootstrap_resumes_after_committed_page_and_does_not_return_to_first():
    _, first = collect([page(1, 3)], max_pages=1)
    assert first["status"] == "ok" and first["completed"] is False
    assert first["checkpoint"]["next_page"] == 2
    rows, second = collect([page(2, 3, (990102,)), page(3, 3, (990103,))], checkpoint=first["checkpoint"])
    assert [row["listing_id"] for row in rows] == ["990102", "990103"]
    assert second["completed"] is True and second["start_page"] == 2


def test_failure_preserves_previous_rows_and_retries_same_page_next_time():
    rows, audit = collect([page(1, 3), page(3, 3, (990103,))])
    assert len(rows) == 1 and audit["status"] == "partial"
    assert audit["checkpoint"]["next_page"] == 2
    assert audit["error"]["page"] == 2


def test_first_page_error_returns_sanitized_partial_cursor():
    rows, audit = collect(["<html><title>Access denied private-payload</title></html>"])
    assert not rows and audit["status"] == "partial"
    assert audit["checkpoint"]["next_page"] == 1
    assert "private-payload" not in json.dumps(audit)


def test_listing_budget_commits_whole_pages_only():
    rows, audit = collect([page(1, 2), page(2, 2, (990102, 990103))], max_listings=2)
    assert len(rows) == 1 and audit["termination"] == "listing_budget"
    assert audit["status"] == "ok" and audit["checkpoint"]["next_page"] == 2
    assert audit["pages_read"] == 1


def test_daily_known_ids_do_not_stop_at_first_known_page():
    rows, audit = collect([page(1, 2), page(2, 2, (990102,))], mode="daily", known_ids={"990101"})
    assert len(rows) == 2
    assert audit["known_listing_encounters"] == 1 and audit["new_to_known_ids"] == 1
    assert audit["sorting_verified_newest"] is True
    assert audit["daily_discovery_guarantee"] is False


def test_duplicate_page_guard_keeps_cursor_at_repeated_page():
    rows, audit = collect([page(1, 2), page(2, 2)])
    assert len(rows) == 1 and audit["status"] == "partial"
    assert audit["checkpoint"]["next_page"] == 2


def test_cross_page_changed_features_keep_latest_validated_encounter():
    changed = page(2, 2, mutate=lambda details: details["data"]["searchAds"]["items"][0].update(areaInSquareMeters=80))
    rows, audit = collect([page(1, 2), changed])
    assert len(rows) == 1 and rows[0]["area_m2"] == 80
    assert audit["status"] == "ok" and audit["completed"] is True
    assert audit["duplicate_encounters"] == audit["changed_duplicates"] == 1
    assert rows[0]["observed_at"] == MOMENT


def test_cross_page_slug_changes_keep_latest_url_without_guessing_missing_features():
    def mutate(details):
        ad = details["data"]["searchAds"]["items"][0]
        ad.update(slug="zmieniony-tytul-IDtest990101", href="[lang]/ad/zmieniony-tytul-IDtest990101", totalPrice=None)
    rows, audit = collect([page(1, 2), page(2, 2, mutate=mutate)])
    assert len(rows) == 1 and rows[0]["url"].endswith("zmieniony-tytul-IDtest990101")
    assert rows[0]["price_pln"] is None and rows[0]["observed_at"] == MOMENT
    assert audit["changed_duplicates"] == 1 and audit["completed"] is True


def test_cross_page_same_url_with_different_source_ids_still_fails():
    def mutate(details):
        ad = details["data"]["searchAds"]["items"][0]
        ad.update(slug="syntetyczne-mieszkanie-IDtest990101", href="[lang]/ad/syntetyczne-mieszkanie-IDtest990101")
    rows, audit = collect([page(1, 2), page(2, 2, (990102,), mutate=mutate)])
    assert len(rows) == 1 and audit["status"] == "partial"
    assert audit["checkpoint"]["next_page"] == 2 and audit["changed_duplicates"] == 0


@pytest.mark.parametrize("change", [
    {"mode": "daily"}, {"scope_url": catalog.transport.DEFAULT_URL},
    {"next_page": 0}, {"next_page": True}, {"finished": True},
    {"source": "www.olx.pl"}, {"version": True}, {"extra": 1},
])
def test_checkpoint_cannot_cross_source_sort_scope_or_mode(change):
    _, audit = collect([page(1, 2)], max_pages=1)
    checkpoint = audit["checkpoint"] | change
    with pytest.raises(OtodomError):
        collect([page(2, 2)], checkpoint=checkpoint)


@pytest.mark.parametrize("kwargs", [
    {"mode": "arbitrary"}, {"max_pages": True}, {"max_pages": 1001},
    {"max_requests": 1}, {"max_requests": True}, {"max_listings": 0},
    {"delay": 0}, {"max_duration_seconds": -1}, {"max_duration_seconds": float("inf")},
    {"known_ids": [100]},
])
def test_invalid_limits_rejected_before_reading(kwargs):
    with pytest.raises(OtodomError):
        collect([page()], **kwargs)


class Response:
    def __init__(self, status, content):
        self.status_code, self.content = status, content
        self.headers = {}
        self.closed = False
    def iter_content(self):
        yield self.content
    def close(self):
        self.closed = True


class Session:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []
    def get(self, url, **kwargs):
        self.calls.append(url)
        assert self.responses, "Exceeded synthetic request budget"
        return self.responses.pop(0)


def test_network_request_budget_includes_robots_and_retains_resume_cursor():
    session = Session([Response(200, b"User-agent: *\nAllow: /\n"), Response(200, page(1, 2).encode())])
    rows, audit = catalog.collect_otodom_catalog(session=session, max_requests=2, observed_at=MOMENT, sleep=lambda _: None)
    assert len(session.calls) == 2 and len(rows) == 1
    assert audit["status"] == "ok" and audit["termination"] == "request_budget"
    assert audit["request_counts"] == {"robots": 1, "html": 1}
    assert audit["checkpoint"]["next_page"] == 2


@pytest.mark.parametrize("status", [403, 429])
def test_access_block_on_last_allowed_request_is_a_failure_not_budget_success(status):
    session = Session([Response(200, b"User-agent: *\nAllow: /\n"), Response(status, b"blocked")])
    rows, audit = catalog.collect_otodom_catalog(session=session, max_requests=2, observed_at=MOMENT, sleep=lambda _: None)
    assert rows == [] and audit["status"] == "partial"
    assert audit["termination"] == "page_error" and audit["checkpoint"]["next_page"] == 1
    assert len(session.calls) == 2


def test_robots_denial_prevents_html_request():
    session = Session([Response(200, b"User-agent: *\nDisallow: /\n")])
    rows, audit = catalog.collect_otodom_catalog(session=session, observed_at=MOMENT, sleep=lambda _: None)
    assert rows == [] and audit["status"] == "partial" and len(session.calls) == 1


def test_audit_no_inventory_or_training_claim_and_json_serializable():
    _, audit = collect([page()])
    assert audit["training_performed"] is False and audit["availability_inference"] is False
    assert audit["detail_pages_fetched"] == 0
    json.dumps(audit, allow_nan=False)


def grouped_page(*, children=None, parent_change=None, direct=False, number=1, total_pages=1):
    def mutate(details):
        base = details["data"]["searchAds"]["items"][0]
        direct_item = deepcopy(base)
        parent = {"id": 990900, "estate": "INVESTMENT", "transaction": "SELL",
                  "slug": "syntetyczna-inwestycja-IDgroup", "href": "[lang]/investment/syntetyczna-inwestycja-IDgroup",
                  "relatedAds": [deepcopy(base)] if children is None else children(base)}
        if parent_change:
            parent_change(parent)
        details["data"]["searchAds"]["items"] = ([direct_item] if direct else []) + [parent]
    return page(number, total_pages, mutate=mutate)


def test_grouped_apartments_are_individual_records_and_never_parent_ranges():
    rows, meta = parse(grouped_page())
    assert len(rows)==1 and rows[0]["listing_id"]=="990101"
    assert "990900" not in {r["listing_id"] for r in rows}
    assert meta["grouped_parents"]==meta["grouped_children"]==1
    assert meta["raw_top_level_items"]==meta["expanded_candidates"]==1
    assert meta["skipped_items"]==0
    _, audit=collect([grouped_page()])
    assert audit["grouped_parents"]==audit["grouped_children"]==1
    assert audit["detail_pages_fetched"]==0 and audit["completed"]


def test_direct_and_grouped_duplicate_identity_is_deduplicated():
    rows, meta=parse(grouped_page(direct=True))
    assert len(rows)==1 and meta["duplicates_on_page"]==1
    assert meta["raw_top_level_items"]==meta["expanded_candidates"]==2


@pytest.mark.parametrize("change,reason", [
    (lambda child:child.update(href="hpr/"+child["href"]),"investment_or_advertising_presentation"),
    (lambda child:child.update(id=False),"invalid_listing_identity"),
    (lambda child:child.update(estate="HOUSE"),"not_individual_apartment_sale"),
    (lambda child:child.update(transaction="RENT"),"not_individual_apartment_sale"),
    (lambda child:child.update(location={}),"unconfirmed_warsaw_location"),
    (lambda child:child.update(slug="invalid"),"invalid_listing_url"),
])
def test_each_grouped_child_is_independently_validated(change,reason):
    def children(base):
        invalid=deepcopy(base);change(invalid)
        return [deepcopy(base),invalid]
    rows,meta=parse(grouped_page(children=children))
    assert len(rows)==1 and meta["grouped_children"]==2
    assert meta["skipped_by_reason"]=={reason:1}


def test_sparse_child_survives_without_price_area_or_exact_rooms():
    def children(base):
        base.update(hidePrice=True,areaInSquareMeters=None,roomsNumber="UNKNOWN")
        return [base]
    rows,meta=parse(grouped_page(children=children))
    assert len(rows)==1
    assert all(rows[0][field] is None for field in ("price_pln","area_m2","rooms","published_at"))
    assert meta["sparse_price_count"]==meta["sparse_area_count"]==meta["sparse_rooms_count"]==1


@pytest.mark.parametrize("related",[{"unexpected":[]},"not-a-list",False,9])
def test_malformed_group_children_is_reported_without_losing_valid_direct_rows(related):
    rows,meta=parse(grouped_page(direct=True,parent_change=lambda parent:parent.update(relatedAds=related)))
    assert len(rows)==1 and meta["skipped_by_reason"]=={"malformed_grouped_children":1}


@pytest.mark.parametrize("change",[
    lambda parent:parent.update(id=False),
    lambda parent:parent.update(slug="invalid"),
    lambda parent:parent.update(href="hpr/[lang]/investment/syntetyczna-inwestycja-IDgroup"),
])
def test_invalid_group_parent_does_not_authorize_expansion(change):
    rows,meta=parse(grouped_page(parent_change=change))
    assert rows==[] and meta["skipped_by_reason"]=={"invalid_grouped_parent":1}


def test_nested_investment_children_are_not_recursively_expanded():
    def children(base):
        return [{"estate":"INVESTMENT","transaction":"SELL","relatedAds":[base]}]
    rows,meta=parse(grouped_page(children=children))
    assert rows==[] and meta["grouped_children"]==1
    assert meta["skipped_by_reason"]=={"not_individual_apartment_sale":1}


def test_malformed_child_does_not_discard_other_children():
    rows,meta=parse(grouped_page(children=lambda base:[base,None,False,[]]))
    assert len(rows)==1 and meta["grouped_children"]==4
    assert meta["skipped_by_reason"]=={"not_individual_apartment_sale":3}


def test_grouped_cardinality_bound_fails_page_and_keeps_resume_cursor():
    html=grouped_page(children=lambda base:[deepcopy(base) for _ in range(catalog.MAX_GROUPED_CHILDREN+1)])
    with pytest.raises(ValueError): parse(html)
    rows,audit=collect([html])
    assert rows==[] and audit["status"]=="partial" and audit["checkpoint"]["next_page"]==1


def test_grouped_and_direct_conflicting_identity_fails_whole_page():
    def children(base):
        base["totalPrice"]["value"]+=1
        return [base]
    with pytest.raises(ValueError):parse(grouped_page(direct=True,children=children))


def test_two_grouped_source_ids_cannot_share_one_url():
    def children(base):
        other=deepcopy(base);other["id"]+=1
        return [base,other]
    with pytest.raises(ValueError):parse(grouped_page(children=children))


def test_cross_page_grouped_duplicate_is_deduped_after_discovery():
    def changed(base):
        base["totalPrice"]["value"]+=1
        return [base]
    rows,audit=collect([grouped_page(total_pages=2),grouped_page(number=2,total_pages=2,children=changed)])
    assert len(rows)==1 and audit["duplicate_encounters"]==1
    assert audit["grouped_children"]==2 and audit["completed"]
