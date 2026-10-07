"""Sparse OLX catalogue and cap traversal, using fictional HTML only."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from urllib.parse import parse_qsl, urlsplit

import pytest

from src import olx_catalog as collector
from src.fetch_olx import OLXError
from src.olx import CATALOG_FIELDS, parse_olx_catalog_page, parse_olx_search
from tests.test_olx import apartment, html_for, search_state, set_param
from tests.test_fetch_olx import Response, Session, ROBOTS

MOMENT = datetime(2026, 10, 7, 10, tzinfo=timezone.utc)


def change_param(ad, key, value):
    ad.update(set_param(ad, key, value))


def page(task=None, *, ids=(990001,), total=1, visible=None, pages=1, overrides=None):
    task = task or collector._task()
    url = collector.catalog_search_url(task)
    query = dict(parse_qsl(urlsplit(url).query))
    ads = [apartment(id=value, url=f"https://www.olx.pl/d/oferta/synthetic-ID{value}.html") for value in ids]
    state = search_state(ads)
    listing = state["listing"]["listing"]
    listing.update(
        pageNumber=task["page"] - 1, totalElements=total,
        visibleElements=total if visible is None else visible, totalPages=pages,
        params={"sort_by": "created_at:desc"},
    )
    listing["requestParams"].update(page=task["page"] - 1, params=query)
    for key in ("lower", "upper"):
        if task[key] is not None:
            suffix = "from" if key == "lower" else "to"
            if task[key] != 0:
                listing["params"][f"filter_float_price:{suffix}"] = str(task[key])
    if overrides:
        overrides(state)
    return html_for(state)


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    from curl_cffi import requests

    def forbidden(*args, **kwargs):
        raise AssertionError("Catalogue tests cannot use live network")

    monkeypatch.setattr(requests, "Session", forbidden)


def test_sparse_four_rooms_does_not_infer_publication_from_creation_time():
    def modify(state):
        ad = state["listing"]["listing"]["ads"][0]
        change_param(ad, "rooms", "four")
        ad["createdTime"] = "2025-01-01T01:30:00+01:00"
    html = page(overrides=modify)
    rows, meta = parse_olx_catalog_page(html, collector.catalog_search_url(collector._task()), MOMENT)
    row = rows[0]
    assert set(row) == set(CATALOG_FIELDS)
    assert row["rooms"] is None and row["rooms_min"] == 4
    assert row["published_at"] is None
    assert row["observed_at"] == MOMENT
    assert meta["skipped_items"] == 0
    with pytest.raises(ValueError):
        parse_olx_search(html, collector.DEFAULT_URL, MOMENT)


@pytest.mark.parametrize("change,field", [
    (lambda ad: ad.pop("price"), "price_pln"),
    (lambda ad: ad.update(price={"budget": True}), "price_pln"),
    (lambda ad: ad["price"]["regularPrice"].update(currencyCode="EUR"), "price_pln"),
    (lambda ad: ad["price"]["regularPrice"].update(value=-1), "price_pln"),
    (lambda ad: ad.update(params=None), "area_m2"),
    (lambda ad: change_param(ad, "m", "broken"), "area_m2"),
    (lambda ad: change_param(ad, "rooms", "unknown"), "rooms"),
    (lambda ad: ad.update(createdTime="2026-10-07T00:00:00"), "published_at"),
    (lambda ad: ad.update(createdTime="malformed"), "published_at"),
])
def test_missing_features_remain_catalogue_null(change, field):
    html = page(overrides=lambda state: change(state["listing"]["listing"]["ads"][0]))
    rows, _ = parse_olx_catalog_page(html, collector.catalog_search_url(collector._task()), MOMENT)
    assert len(rows) == 1 and rows[0][field] is None


@pytest.mark.parametrize("change", [
    lambda ad: ad.update(id=False),
    lambda ad: ad.update(category={"id": 15}),
    lambda ad: ad.update(location={"cityName": "Kraków"}),
    lambda ad: ad.update(url="https://evil.example/d/oferta/test.html"),
    lambda ad: ad.update(isActive=False),
])
def test_sparse_discovery_still_requires_verified_warsaw_sale_identity(change):
    rows, meta = parse_olx_catalog_page(
        page(overrides=lambda state: change(state["listing"]["listing"]["ads"][0])),
        collector.catalog_search_url(collector._task()), MOMENT,
    )
    assert rows == [] and meta["skipped_items"] == 1


@pytest.mark.parametrize("where,key", [
    ("params", "sort_by"), ("params", "filter_float_price:from"),
    ("params", "filter_float_price:to"), ("requestParams", "search[order]"),
    ("requestParams", "search[filter_float_price:from]"),
])
def test_applied_filters_and_sorting_must_be_confirmed(where, key):
    task = collector._task(900000, 1000000)
    def modify(state):
        l = state["listing"]["listing"]
        target = l["params"] if where == "params" else l["requestParams"]["params"]
        target[key] = "ignored"
    with pytest.raises(ValueError):
        parse_olx_catalog_page(page(task, overrides=modify), collector.catalog_search_url(task), MOMENT)


def test_verified_zero_result_partition_does_not_mean_format_failure():
    task = collector._task(0, 1)
    rows, report = collector.collect_olx_catalog(html_pages={collector.catalog_search_url(task): page(task, ids=(), total=0, pages=0)},
                                             checkpoint={"version":1,"source":"www.olx.pl","mode":"bootstrap","queue":[task],"unresolved_partitions":[]})
    assert rows == [] and report["completed"] is True and report["status"] == "ok"


@pytest.mark.parametrize("field,value", [("totalElements", -1), ("totalPages", True), ("visibleElements", "0"), ("totalPages", 0)])
def test_corrupt_pagination_is_never_a_successful_zero(field, value):
    html = page(overrides=lambda state: state["listing"]["listing"].update({field:value}))
    rows, report = collector.collect_olx_catalog(html_pages=[html])
    assert rows == [] and report["status"] == "partial"
    assert report["checkpoint"]["queue"] == [collector._task()]


def test_bisection_and_overlapping_partition_identity_dedup():
    tasks = [collector._task(), collector._task(None, 1000000), collector._task(None, 1000000, 2), collector._task(1000000, None)]
    html = [page(ids=(1,), total=1000, visible=1500, pages=25),
            page(tasks[1], ids=(1,2), total=500, pages=2),
            page(tasks[2], ids=(3,), total=500, pages=2),
            page(tasks[3], ids=(2,4), total=500, pages=1)]
    rows, audit = collector.collect_olx_catalog(html_pages=html, observed_at=MOMENT)
    assert {r["listing_id"] for r in rows} == {"1","2","3","4"}
    assert audit["completed"] and audit["status"] == "ok"
    assert audit["price_partitions_created"] == 2 and audit["cross_page_duplicates"] == 2
    assert audit["coverage_complete"] is False
    assert audit["checkpoint"]["queue"] == []
    json.dumps(audit, allow_nan=False)


@pytest.mark.parametrize("task", [collector._task(), collector._task(1000000,None),collector._task(0,100),collector._task(10,13)])
def test_price_partition_boundaries_cover_fractional_prices(task):
    first, second = collector._split(task)
    assert first["upper"] == second["lower"]
    midpoint = first["upper"]
    for price in (midpoint-0.5, midpoint, midpoint+0.5):
        def inside(t):
            return (t["lower"] is None or price >= t["lower"]) and (t["upper"] is None or price <= t["upper"])
        assert inside(first) or inside(second)


def test_identical_price_cap_is_reported_unresolved_without_recursive_loop():
    task = collector._task(10,11)
    checkpoint = {"version":1,"source":"www.olx.pl","mode":"bootstrap","queue":[task],"unresolved_partitions":[]}
    rows, audit = collector.collect_olx_catalog(checkpoint=checkpoint, html_pages=[page(task,total=1000,visible=5000)])
    assert len(rows)==1 and audit["completed"]
    assert audit["unresolved_partitions"] == [{"lower":10,"upper":11,"visible_elements":5000,"total_elements":1000}]
    assert audit["coverage_complete"] is False


def test_page_budget_continues_next_partition_instead_of_restarting():
    html = [page(total=1000,visible=1500,pages=25)]
    _, audit = collector.collect_olx_catalog(html_pages=html, max_pages=1)
    assert audit["status"] == "ok" and not audit["completed"]
    assert audit["checkpoint"]["queue"] == [collector._task(None,1000000),collector._task(1000000,None)]
    original = deepcopy(audit["checkpoint"])
    _, again = collector.collect_olx_catalog(checkpoint=original, html_pages=[page(collector._task(None,1000000))],max_pages=1)
    assert again["checkpoint"]["queue"] == [collector._task(1000000,None)]
    assert original == audit["checkpoint"]


def test_failure_does_not_skip_the_failed_page():
    rows, audit = collector.collect_olx_catalog(html_pages=[page(total=100,pages=2),"<html>captcha</html>"])
    assert len(rows)==1 and audit["status"]=="partial"
    assert audit["checkpoint"]["queue"] == [collector._task(page=2)]


def test_price_and_slug_changes_keep_last_validated_identity():
    def updated(state):
        ad=state["listing"]["listing"]["ads"][0]
        ad["price"]["regularPrice"]["value"]=1000000
        ad["url"]="https://www.olx.pl/d/oferta/renamed-ID1.html"
    rows,audit=collector.collect_olx_catalog(
        html_pages=[page(ids=(1,),total=100,pages=2),page(collector._task(page=2),ids=(1,2),total=100,pages=2,overrides=updated)])
    first=next(r for r in rows if r["listing_id"]=="1")
    assert first["price_pln"]==1000000 and first["url"].endswith("renamed-ID1.html")
    assert audit["changed_duplicates"]==1 and audit["cross_page_duplicates"]==1


def test_same_url_with_a_different_identity_preserves_earlier_page():
    def collide(state):
        state["listing"]["listing"]["ads"][0]["url"]="https://www.olx.pl/d/oferta/synthetic-ID1.html"
    rows,audit=collector.collect_olx_catalog(
        html_pages=[page(ids=(1,),total=100,pages=2),page(collector._task(page=2),ids=(2,),total=100,pages=2,overrides=collide)])
    assert [r["listing_id"] for r in rows]==["1"]
    assert audit["status"]=="partial" and audit["checkpoint"]["queue"]==[collector._task(page=2)]


def test_repeated_page_set_is_not_misread_as_search_end():
    html=[page(ids=(1,2),total=100,pages=3),page(collector._task(page=2),ids=(2,1),total=100,pages=3)]
    _,audit=collector.collect_olx_catalog(html_pages=html)
    assert audit["status"]=="partial" and audit["termination"]=="repeated_page"
    assert audit["checkpoint"]["queue"]==[collector._task(page=2)]


def test_listing_budget_does_not_advance_partially_exported_page():
    rows,audit=collector.collect_olx_catalog(html_pages=[page(ids=(1,2))],max_listings=1)
    assert len(rows)==1 and audit["termination"]=="listing_budget"
    assert audit["checkpoint"]["queue"]==[collector._task()]


def test_daily_newest_head_does_not_stop_on_known_promoted_id():
    checkpoint={"version":1,"source":"www.olx.pl","mode":"daily","queue":[collector._task(page=8)],"unresolved_partitions":[]}
    rows,audit=collector.collect_olx_catalog(mode="daily", checkpoint=checkpoint,known_ids={"1"},max_pages=2,
        html_pages=[page(ids=(1,),total=1000,visible=4400,pages=25),page(collector._task(page=2),ids=(2,),total=1000,visible=4400,pages=25)])
    assert {r["listing_id"] for r in rows}=={"1","2"}
    assert audit["known_identities"]==audit["new_identities"]==1
    assert audit["price_partitions_created"]==0 and not audit["completed"]
    assert audit["checkpoint"]["queue"]==[collector._task(page=3)]


def test_one_fresh_robots_policy_and_strict_request_budget_including_retry():
    session=Session([Response(content=ROBOTS),Response(status=503),Response(content=page().encode())])
    rows,audit=collector.collect_olx_catalog(session=session,max_requests=2,sleep=lambda _:None)
    assert rows==[] and audit["status"]=="ok" and audit["termination"]=="request_budget"
    assert len(session.calls)==2 and audit["request_counts"]=={"robots":1,"html":1}
    assert audit["checkpoint"]["queue"]==[collector._task()]


def test_live_transport_checks_policy_once_and_uses_requested_latest_query():
    session=Session([Response(content=ROBOTS),Response(content=page().encode())])
    rows,audit=collector.collect_olx_catalog(session=session,sleep=lambda _:None)
    assert len(rows)==1 and audit["completed"]
    assert len(session.calls)==2 and session.calls[0][0]==collector.ROBOTS_URL
    assert dict(parse_qsl(urlsplit(session.calls[1][0]).query))["search[order]"]=="created_at:desc"


def test_disallowed_catalogue_path_makes_no_html_request():
    session=Session([Response(content=b"User-agent: *\nDisallow: /nieruchomosci/\n"),Response(content=page().encode())])
    rows,audit=collector.collect_olx_catalog(session=session,sleep=lambda _:None)
    assert rows==[] and audit["status"]=="partial" and len(session.calls)==1


def test_redirect_cannot_silently_remove_latest_sort_or_price_filter():
    session=Session([Response(content=ROBOTS),Response(status=302,headers={"Location":collector.DEFAULT_URL}),Response(content=page().encode())])
    rows,audit=collector.collect_olx_catalog(session=session,sleep=lambda _:None)
    assert rows==[] and audit["status"]=="partial"
    assert audit["checkpoint"]["queue"]==[collector._task()]


@pytest.mark.parametrize("mutation", [
    lambda c:c.update(source="evil.example"),
    lambda c:c.update(mode="refresh"),
    lambda c:c.update(version=True),
    lambda c:c["queue"][0].update(page=True),
    lambda c:c["queue"][0].update(lower=1.5),
    lambda c:c["queue"][0].update(lower=-1),
    lambda c:c["queue"][0].update(lower=5,upper=4),
    lambda c:c["queue"][0].update(url="https://evil.example"),
    lambda c:c.update(unresolved_partitions=[{"bad":"format"}]),
])
def test_untrusted_checkpoint_cannot_choose_urls_or_invalid_bounds(mutation):
    c={"version":1,"source":"www.olx.pl","mode":"bootstrap","queue":[collector._task()],"unresolved_partitions":[]}
    mutation(c)
    with pytest.raises(OLXError):
        collector.collect_olx_catalog(checkpoint=c,html_pages=[page()])


@pytest.mark.parametrize("status",[403,429])
def test_source_access_rejection_preserves_cursor_and_stops(status):
    session=Session([Response(content=ROBOTS),Response(status=status),Response(content=page().encode())])
    rows,audit=collector.collect_olx_catalog(session=session,sleep=lambda _:None)
    assert rows==[] and audit["status"]=="partial" and len(session.calls)==2
    assert audit["checkpoint"]["queue"]==[collector._task()]


@pytest.mark.parametrize("kwargs", [{"max_pages":False},{"max_requests":0},{"max_listings":50001},{"delay":1},{"mode":"all"},{"known_ids":[1]},{"checkpoint":{"version":1}}])
def test_catalogue_input_validation_happens_before_network(kwargs):
    with pytest.raises(OLXError):
        collector.collect_olx_catalog(**kwargs)
