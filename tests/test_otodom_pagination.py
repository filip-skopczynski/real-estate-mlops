"""Bounded Otodom pagination tests with fictional listings and fake transport."""
from datetime import datetime, timezone
import json

import pytest

from src import fetch_otodom as collector
from src.otodom import parse_otodom_search, parse_otodom_search_page

MOMENT = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
WARSAW = "mazowieckie/warszawa/warszawa/warszawa"
ROBOTS = b"User-agent: *\nAllow: /\nContent-Signal: search=yes,ai-input=no,ai-train=no\n"


def ad(identifier=1001, price=700000):
    slug = f"fikcyjny-test-IDsynthetic{identifier}"
    return {
        "id": identifier, "slug": slug, "href": "[lang]/ad/" + slug,
        "estate": "FLAT", "transaction": "SELL", "hidePrice": False,
        "totalPrice": {"currency": "PLN", "value": price},
        "areaInSquareMeters": 50, "roomsNumber": "TWO",
        "location": {"reverseGeocoding": {"locations": [{
            "locationLevel": "city_or_village", "id": WARSAW, "name": "Warszawa",
        }]}},
    }


def state(number=1, items=None, total_pages=2):
    return {"props": {"pageProps": {
        "estate": "FLAT", "transaction": "SELL", "location": WARSAW,
        "canonicalURL": collector.SEARCH_PATH,
        "filteringQueryParams": {"page": number},
        "sortingOption": {"by": "DEFAULT", "direction": "DESC"},
        "data": {"searchAds": {
            "items": [ad(number)] if items is None else items,
            "pagination": {"currentPage": number, "itemsPerPage": 36,
                           "totalItems": total_pages * 36, "totalPages": total_pages},
        }},
    }}}


def html(value):
    return '<html><script id="__NEXT_DATA__" type="application/json">' + json.dumps(value) + '</script></html>'


def page_url(number):
    return collector.DEFAULT_URL if number == 1 else collector.DEFAULT_URL + f"?page={number}"


class Response:
    def __init__(self, content=b"", status=200, headers=None):
        self.status_code, self.content = status, content
        self.headers = headers or {}
        self.closed = False

    def iter_content(self):
        yield self.content

    def close(self):
        self.closed = True


class Session:
    def __init__(self, responses):
        self.responses, self.calls, self.closed = list(responses), [], False

    def get(self, url, **options):
        self.calls.append((url, options))
        if not self.responses:
            raise AssertionError("Unexpected extra network request")
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    from curl_cffi import requests

    def fail(*args, **kwargs):
        raise AssertionError("Network sessions are forbidden in synthetic tests")

    monkeypatch.setattr(requests, "Session", fail)


def collect(pages, **kwargs):
    return collector.collect_otodom_search(html_pages=pages, observed_at=MOMENT, **kwargs)


def test_later_page_requires_explicit_opt_in():
    document = html(state(2))
    with pytest.raises(ValueError):
        parse_otodom_search(document, page_url(2), MOMENT)
    records, metadata = parse_otodom_search_page(document, page_url(2), MOMENT, expected_page=2)
    assert records[0]["listing_id"] == "2"
    assert metadata["requested_page"] == metadata["current_page"] == metadata["filter_page"] == 2


@pytest.mark.parametrize("url", [page_url(2), collector.DEFAULT_URL + "?page=1&page=1",
                                  collector.DEFAULT_URL + "?page=1&PAGE=1"])
def test_first_page_validation_rejects_other_or_duplicate_pages(url):
    with pytest.raises(collector.OtodomError):
        collector.validate_search_url(url)
    with pytest.raises(ValueError):
        parse_otodom_search(html(state()), url, MOMENT)


@pytest.mark.parametrize("url", [collector.DEFAULT_URL, collector.DEFAULT_URL + "?page=1",
                                  collector.DEFAULT_URL + "?page=02", page_url(2) + "&page=2"])
def test_exact_page_url_required(url):
    with pytest.raises(ValueError):
        parse_otodom_search_page(html(state(2)), url, MOMENT, expected_page=2)


@pytest.mark.parametrize("part,value", [("filter", 1), ("filter", "2"), ("current", 1),
                                         ("current", True), ("canonical", collector.SEARCH_PATH + "?page=1")])
def test_page_state_and_canonical_parameter_must_agree(part, value):
    value_state = state(2)
    props = value_state["props"]["pageProps"]
    if part == "filter":
        props["filteringQueryParams"]["page"] = value
    elif part == "current":
        props["data"]["searchAds"]["pagination"]["currentPage"] = value
    else:
        props["canonicalURL"] = value
    with pytest.raises(ValueError):
        parse_otodom_search_page(html(value_state), page_url(2), MOMENT, expected_page=2)


@pytest.mark.parametrize("key,value", [("itemsPerPage", 0), ("itemsPerPage", 1001),
    ("totalItems", -1), ("totalItems", True), ("totalPages", 0), ("totalPages", "2")])
def test_invalid_pagination_metadata_fails(key, value):
    value_state = state()
    value_state["props"]["pageProps"]["data"]["searchAds"]["pagination"][key] = value
    with pytest.raises(ValueError):
        parse_otodom_search_page(html(value_state), page_url(1), MOMENT, expected_page=1)


def test_page_cannot_exceed_advertised_final_page():
    with pytest.raises(ValueError):
        parse_otodom_search_page(html(state(2, total_pages=1)), page_url(2), MOMENT, expected_page=2)


def test_metadata_reports_skips_nominal_size_and_sorting():
    items = [ad(1), ad(2), ad(3)]
    items[1]["roomsNumber"] = "UNKNOWN"
    items[2]["hidePrice"] = True
    rows, audit = collect([html(state(items=items, total_pages=1))])
    assert len(rows) == 1
    meta = audit["page_metadata"][0]
    assert meta["items_on_page"] == 3
    assert meta["parsed_listings"] == 1
    assert meta["skipped_items"] == 2
    assert sum(meta["skipped_by_reason"].values()) == 2
    assert meta["items_per_page_mismatch"] is True
    assert meta["sorting"] == {"by": "DEFAULT", "direction": "DESC"}
    assert audit["termination"] == "pagination_end"
    assert audit["availability_inference"] is False
    assert audit["training_performed"] is False
    json.dumps(audit, allow_nan=False)


def test_same_page_identity_conflicts_still_fail():
    with pytest.raises(collector.OtodomError):
        collect([html(state(items=[ad(1), ad(1, price=600000)]))])


def test_multi_page_offline_collection_uses_one_timestamp_and_no_io():
    rows, audit = collect([html(state(1)), html(state(2))])
    assert [row["listing_id"] for row in rows] == ["1", "2"]
    assert all(row["observed_at"] == MOMENT for row in rows)
    assert audit["status"] == "ok"
    assert audit["pages_read"] == 2
    assert audit["request_counts"] == {"robots": 0, "html": 0}
    assert audit["robots_check"]["status"] == "not_performed_offline"
    assert audit["termination"] == "pagination_end"
    assert "not a complete inventory" in audit["completeness"]


def test_cross_page_duplicate_latest_validated_encounter_wins():
    pages = [html(state(1, items=[ad(10), ad(11)])),
             html(state(2, items=[ad(10, 650000), ad(12)]))]
    rows, audit = collect(pages)
    assert [row["listing_id"] for row in rows] == ["10", "11", "12"]
    assert rows[0]["price_pln"] == 650000
    assert audit["parsed_listings"] == 4
    assert audit["exported_listings"] == 3
    assert audit["duplicate_encounters"] == 1
    assert audit["cross_page_conflicts"] == 1


def test_same_page_exact_duplicates_are_reported_and_collapsed():
    rows, audit = collect([html(state(items=[ad(1), ad(1)], total_pages=1))])
    assert len(rows) == 1
    assert audit["page_metadata"][0]["duplicates_on_page"] == 1


@pytest.mark.parametrize("pages,listings", [(0, 10), (1001, 10), (True, 10), (1.5, 10),
                                           (1, 0), (1, 50001), (1, True), (1, 1.5)])
def test_collection_budgets_fail_before_session_creation(pages, listings):
    with pytest.raises(collector.OtodomError):
        collector.collect_otodom_search(max_pages=pages, max_listings=listings)


@pytest.mark.parametrize("pages", [[], "html", ("html",), [None], [123], [bytearray(b"html")]])
def test_offline_input_types_are_validated_before_io(pages):
    with pytest.raises(collector.OtodomError):
        collect(pages)


def test_page_budget_avoids_reading_or_parsing_an_extra_page():
    rows, audit = collect([html(state()), "invalid html must not be parsed"], max_pages=1)
    assert len(rows) == 1
    assert audit["pages_read"] == 1
    assert audit["termination"] == "max_pages"


def test_listing_budget_avoids_following_pages_but_parses_full_last_page():
    rows, audit = collect([html(state(items=[ad(1), ad(2)])), "invalid"], max_listings=1)
    assert [row["listing_id"] for row in rows] == ["1"]
    assert audit["parsed_listings"] == 2
    assert audit["pages_read"] == 1
    assert audit["termination"] == "max_listings"


def test_later_bad_html_is_partial_and_preserves_first_page():
    rows, audit = collect([html(state()), "<title>Private secret injected challenge</title>"])
    assert len(rows) == 1
    assert audit["status"] == "partial"
    assert audit["termination"] == "page_error"
    assert audit["error"] == {"page": 2, "reason": "page_access_or_validation_failed"}
    assert "Private secret" not in json.dumps(audit)


def test_first_bad_html_raises_instead_of_returning_empty_success():
    with pytest.raises(collector.OtodomError):
        collect(["<title>Verify you are human</title>"])


def test_offline_pages_exhausted_is_partial():
    rows, audit = collect([html(state(total_pages=10))])
    assert len(rows) == 1
    assert audit["status"] == "partial"
    assert audit["termination"] == "offline_pages_exhausted"


def test_changed_total_is_reported_without_claiming_complete_inventory():
    rows, audit = collect([html(state()), html(state(2, total_pages=3))], max_pages=2)
    assert len(rows) == 2
    assert audit["page_metadata"][1]["pagination_changed"] is True
    assert audit["termination"] == "max_pages"


def test_live_search_reads_one_robots_and_two_public_html_pages():
    responses = [Response(ROBOTS), Response(html(state()).encode()), Response(html(state(2)).encode())]
    session, sleeps = Session(responses), []
    rows, audit = collector.collect_otodom_search(session=session, sleep=sleeps.append, observed_at=MOMENT)
    assert len(rows) == 2
    assert [url for url, _ in session.calls] == [collector.ROBOTS_URL, page_url(1), page_url(2)]
    assert audit["request_counts"] == {"robots": 1, "html": 2}
    assert audit["robots_check"]["content_signals"] == {"search": "yes", "ai-input": "no", "ai-train": "no"}
    assert sleeps == [2, 2, 2]
    assert all(response.closed for response in responses)
    assert session.closed is False
    for _, options in session.calls:
        assert options["headers"]["User-Agent"] == collector.USER_AGENT
        assert options["allow_redirects"] is False
        assert options["discard_cookies"] is True


def test_filters_are_preserved_and_page_one_redirect_becomes_pagination_base():
    responses = [Response(ROBOTS), Response(status=301, headers={"Location": collector.DEFAULT_URL + "?priceMax=900000"}),
                 Response(html(state()).encode()), Response(html(state(2)).encode())]
    session = Session(responses)
    rows, audit = collector.collect_otodom_search(
        url="https://www.otodom.pl" + collector.LEGACY_SEARCH_PATH + "?priceMax=900000",
        session=session, sleep=lambda _: None, observed_at=MOMENT,
    )
    assert len(rows) == 2
    assert session.calls[-1][0] == collector.DEFAULT_URL + "?priceMax=900000&page=2"
    assert audit["request_counts"] == {"robots": 1, "html": 3}


def test_robots_policy_is_checked_for_each_page():
    robots = b"User-agent: *\nAllow: /\nDisallow: /*?page=2$\n"
    session = Session([Response(robots), Response(html(state()).encode())])
    rows, audit = collector.collect_otodom_search(session=session, sleep=lambda _: None, observed_at=MOMENT)
    assert len(rows) == 1
    assert len(session.calls) == 2
    assert audit["status"] == "partial"
    assert audit["error"]["page"] == 2


@pytest.mark.parametrize("status", [403, 429])
def test_later_access_block_stops_without_retries_or_extra_pages(status):
    session = Session([Response(ROBOTS), Response(html(state(total_pages=5)).encode()), Response(status=status)])
    rows, audit = collector.collect_otodom_search(session=session, sleep=lambda _: None, observed_at=MOMENT)
    assert len(rows) == 1
    assert audit["status"] == "partial"
    assert len(session.calls) == 3
    assert audit["request_counts"] == {"robots": 1, "html": 2}


def test_later_redirect_to_wrong_page_is_partial_before_target_request():
    session = Session([Response(ROBOTS), Response(html(state()).encode()),
                       Response(status=302, headers={"Location": collector.DEFAULT_URL})])
    rows, audit = collector.collect_otodom_search(session=session, sleep=lambda _: None, observed_at=MOMENT)
    assert len(rows) == 1
    assert len(session.calls) == 3
    assert audit["status"] == "partial"


def test_first_access_failure_raises():
    session = Session([Response(ROBOTS), Response(status=403)])
    with pytest.raises(collector.OtodomError):
        collector.collect_otodom_search(session=session, sleep=lambda _: None)
    assert len(session.calls) == 2


def test_owned_session_closes_after_later_failure(monkeypatch):
    from curl_cffi import requests
    session = Session([Response(ROBOTS), Response(html(state()).encode()), Response(status=429)])
    options = {}

    def create(**kwargs):
        options.update(kwargs)
        return session

    monkeypatch.setattr(requests, "Session", create)
    rows, audit = collector.collect_otodom_search(sleep=lambda _: None, observed_at=MOMENT)
    assert len(rows) == 1 and audit["status"] == "partial"
    assert session.closed
    assert options["trust_env"] is False


def test_source_exception_text_is_not_leaked_in_partial_audit():
    session = Session([Response(ROBOTS), Response(html(state()).encode()),
                       RuntimeError("private-token-do-not-display")])
    rows, audit = collector.collect_otodom_search(session=session, sleep=lambda _: None, observed_at=MOMENT)
    assert len(rows) == 1
    assert "private-token" not in json.dumps(audit)


def test_repeated_eligible_page_stops_but_retains_audited_results():
    documents = [html(state(1, [ad(1)], 3)), html(state(2, [ad(1)], 3)), html(state(3, [ad(3)], 3))]
    rows, audit = collect(documents, max_pages=3)
    assert len(rows) == 1
    assert audit["status"] == "partial"
    assert audit["termination"] == "repeated_page"
    assert audit["pages_read"] == 2


def test_same_id_changed_price_is_kept_and_does_not_count_as_repeated_page():
    documents = [html(state(1, [ad(1, 700000)])), html(state(2, [ad(1, 650000)]))]
    rows, audit = collect(documents, max_pages=2)
    assert len(rows) == 1 and rows[0]["price_pln"] == 650000
    assert audit["status"] == "ok" and audit["cross_page_conflicts"] == 1


def test_conflicting_url_identity_on_later_page_preserves_first_page():
    first = ad(1)
    other = ad(2)
    other["slug"], other["href"] = first["slug"], first["href"]
    rows, audit = collect([html(state(1, [first])), html(state(2, [other]))])
    assert len(rows) == 1 and rows[0]["listing_id"] == "1"
    assert audit["status"] == "partial" and audit["pages_read"] == 1
