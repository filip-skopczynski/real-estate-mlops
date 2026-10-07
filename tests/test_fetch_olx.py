"""Synthetic transport tests: no OLX website or database requests are made."""
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest
from curl_cffi.requests.exceptions import Timeout

from src import fetch_olx as collector
from src.fetch_data import FIELDS

TIMESTAMP = datetime(2026, 10, 7, 10, tzinfo=timezone.utc)
ROBOTS = b"User-agent: *\nAllow: /\n"
HTML = b"<html><body>Synthetic OLX fixture</body></html>"


def record(identifier="SYNTHETIC1"):
    row = {field: None for field in FIELDS}
    row.update(
        source="www.olx.pl", listing_id=identifier,
        url=f"https://www.olx.pl/d/oferta/synthetic-ID{identifier}.html",
        city="Warszawa", price_pln=750000.0, area_m2=50.0, rooms=2, observed_at=TIMESTAMP,
    )
    return row


def synthetic_search_html():
    state = {
        "categories": {"list": {"14": {"path": "nieruchomosci/mieszkania/sprzedaz"}}},
        "listing": {"listing": {
            "categoryId": 14, "pageNumber": 0,
            "requestParams": {"categoryPath": collector.SEARCH_PATH.strip("/")},
            "ads": [{
                "id": 123, "url": "https://www.olx.pl/d/oferta/synthetic-IDTEST.html",
                "category": {"id": 14}, "location": {"cityName": "Warszawa"},
                "price": {"regularPrice": {"currencyCode": "PLN", "value": 750000}},
                "params": [{"key": "m", "normalizedValue": "50"}, {"key": "rooms", "normalizedValue": "two"}],
            }],
        }},
    }
    return '<html><script id="olx-init-config">window.__PRERENDERED_STATE__=' + json.dumps(state) + ";</script></html>"


class Response:
    def __init__(self, status=200, content=HTML, headers=None, chunks=None):
        self.status_code = status
        self.headers = headers or {}
        self.chunks = chunks if chunks is not None else [content]
        self.closed = False
        self.yielded = 0

    def iter_content(self):
        for chunk in self.chunks:
            self.yielded += 1
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected additional request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def forbid_real_network(monkeypatch):
    from curl_cffi import requests

    def fail(*args, **kwargs):
        raise AssertionError("Tests must not create a real network session")

    monkeypatch.setattr(requests, "Session", fail)


@pytest.fixture
def parsed(monkeypatch):
    monkeypatch.setattr(collector, "_parse_html", lambda html, url, observed_at: [record()])


@pytest.mark.parametrize("url", [
    "http://www.olx.pl" + collector.SEARCH_PATH,
    "https://olx.pl" + collector.SEARCH_PATH,
    "https://www.olx.pl.evil.example" + collector.SEARCH_PATH,
    "https://user:password@www.olx.pl" + collector.SEARCH_PATH,
    "https://www.olx.pl:443" + collector.SEARCH_PATH,
    collector.DEFAULT_URL + "#fragment",
    collector.DEFAULT_URL + "#",
    "https://www.olx.pl/nieruchomosci/mieszkania/wynajem/warszawa/",
    "https://www.olx.pl/nieruchomosci/domy/sprzedaz/warszawa/",
    "https://www.olx.pl/d/oferta/synthetic-IDTEST.html",
    collector.DEFAULT_URL + "?search[description]=wynajem",
    collector.DEFAULT_URL + "?q=%2577ynajem",
    collector.DEFAULT_URL + "?page=2",
    collector.DEFAULT_URL + "?page=0",
    collector.DEFAULT_URL + "?page=1&page=2",
    collector.DEFAULT_URL + "?q=%0AInjected",
    collector.DEFAULT_URL + "\\extra",
])
def test_rejects_urls_outside_first_page_sale_scope(url):
    with pytest.raises(collector.OLXError):
        collector.validate_search_url(url)


def test_allows_filters_on_first_page():
    url = collector.DEFAULT_URL + "?search%5Bfilter_float_price%3Ato%5D=900000&page=1"
    assert collector.validate_search_url(url) == url


@pytest.mark.parametrize("limit,delay", [(0, 2), (101, 2), (True, 2), (1.5, 2), (20, 0), (20, 1.9), (20, float("inf")), (20, float("nan"))])
def test_invalid_limits_fail_before_any_session(limit, delay):
    with pytest.raises(collector.OLXError):
        collector.collect_olx(max_listings=limit, delay=delay)


def test_offline_preview_does_not_create_session_and_reports_sample(monkeypatch):
    def parse(html, url, observed_at):
        assert html == HTML.decode()
        assert url == collector.DEFAULT_URL
        assert observed_at == TIMESTAMP
        return [record(str(index)) for index in range(3)]

    monkeypatch.setattr(collector, "_parse_html", parse)
    rows, audit = collector.collect_olx(html=HTML, observed_at=TIMESTAMP, max_listings=2)
    assert len(rows) == 2
    assert audit["parsed_listings"] == 3
    assert audit["exported_listings"] == 2
    assert audit["collection_mode"] == "offline_preview"
    assert audit["html_sha256"] == hashlib.sha256(HTML).hexdigest()
    assert audit["request_counts"] == {"robots": 0, "html": 0}
    assert audit["robots_check"]["status"] == "not_performed_offline"
    assert audit["availability_inference"] is False
    assert "not a complete inventory" in audit["completeness"]
    assert "not transaction price" in audit["price_interpretation"]


def test_offline_collection_integrates_real_parser():
    rows, audit = collector.collect_olx(html=synthetic_search_html(), observed_at=TIMESTAMP)
    assert len(rows) == 1
    assert rows[0]["listing_id"] == "123"
    assert rows[0]["price_pln"] == 750000
    assert rows[0]["area_m2"] == 50
    assert rows[0]["rooms"] == 2
    assert rows[0]["observed_at"] == TIMESTAMP
    assert audit["request_counts"] == {"robots": 0, "html": 0}


def test_http_200_challenge_is_rejected_by_real_parser():
    challenge = b"<html><head><title>Verify you are human</title></head></html>"
    response = Response(content=challenge)
    session = Session([Response(content=ROBOTS), response])
    with pytest.raises(collector.OLXError, match="blokad"):
        collector.collect_olx(session=session, sleep=lambda value: None)
    assert len(session.calls) == 2
    assert response.closed


def test_live_reads_only_robots_and_one_page(parsed):
    responses = [Response(content=ROBOTS, headers={"Content-Type": "text/plain"}), Response(headers={"Content-Type": "text/html; charset=utf-8"})]
    session = Session(responses)
    sleeps = []
    rows, audit = collector.collect_olx(session=session, sleep=sleeps.append, observed_at=TIMESTAMP)
    assert len(rows) == 1
    assert [url for url, options in session.calls] == [collector.ROBOTS_URL, collector.DEFAULT_URL]
    for _, options in session.calls:
        assert options["timeout"] == 20
        assert options["impersonate"] == "chrome120"
        assert options["default_headers"] is False
        assert options["headers"]["User-Agent"] == collector.USER_AGENT
        assert options["headers"]["Cookie"] == ""
        assert options["allow_redirects"] is False
        assert options["stream"] is True
        assert options["discard_cookies"] is True
        assert options["proxies"] == {"all": ""}
    assert sleeps == [2.0, 2.0]
    assert all(response.closed for response in responses)
    assert session.closed is False  # An injected session remains owned by its caller.
    assert audit["robots_check"]["sha256"] == hashlib.sha256(ROBOTS).hexdigest()
    assert audit["robots_check"]["user_agent"] == collector.USER_AGENT
    assert audit["request_counts"] == {"robots": 1, "html": 1}
    assert audit["detail_pages_fetched"] == 0


@pytest.mark.parametrize("robots", [
    b"User-agent: *\nDisallow: /\n",
    b"User-agent: WarsawRealEstatePortfolio\nDisallow: /nieruchomosci/\n\nUser-agent: *\nAllow: /\n",
])
def test_robots_disallow_stops_before_page(robots):
    session = Session([Response(content=robots)])
    with pytest.raises(collector.OLXError, match="robots.txt"):
        collector.collect_olx(session=session, sleep=lambda value: None)
    assert len(session.calls) == 1


def test_robots_crawl_delay_and_request_rate_are_respected(parsed):
    robots = b"User-agent: *\nAllow: /\nCrawl-delay: 6\nRequest-rate: 1/10\n"
    session = Session([Response(content=robots), Response()])
    sleeps = []
    _, audit = collector.collect_olx(session=session, sleep=sleeps.append)
    assert sleeps == [2.0, 10.0]
    assert audit["request_delay_seconds"] == 10


@pytest.mark.parametrize("rules,url,allowed", [
    ("Disallow: /nieruchomosci/*", collector.DEFAULT_URL, False),
    ("Disallow: /nieruchomosci/*sprzedaz/*", collector.DEFAULT_URL, False),
    ("Disallow: /nieruchomosci/*$", collector.DEFAULT_URL, False),
    ("Disallow: /nieruchomosci/*?blocked=1$", collector.DEFAULT_URL + "?blocked=1", False),
    ("Disallow: /nieruchomosci/*?blocked=1$", collector.DEFAULT_URL + "?blocked=1&more=1", True),
    ("Disallow: /nieruchomosci/*?blocked=1$", collector.DEFAULT_URL, True),
    ("Allow: /nieruchomosci/\nDisallow: /nieruchomosci/mieszkania/", collector.DEFAULT_URL, False),
    ("Disallow: /nieruchomosci/mieszkania/\nAllow: /nieruchomosci/", collector.DEFAULT_URL, False),
    ("Disallow: /nieruchomosci/\nAllow: /nieruchomosci/mieszkania/", collector.DEFAULT_URL, True),
    ("Disallow: /nieruchomosci/\nAllow: /nieruchomosci/", collector.DEFAULT_URL, True),
    ("Allow: /nieruchomosci/\nDisallow: /nieruchomosci/", collector.DEFAULT_URL, True),
    ("Disallow: /NIERUCHOMOSCI/", collector.DEFAULT_URL, True),
    ("Disallow:", collector.DEFAULT_URL, True),
    ("Disallow: /path/*file$", "https://www.olx.pl/path/file", False),
    ("Disallow: /path/*file$", "https://www.olx.pl/path/other/file", False),
    ("Disallow: /path/*file$", "https://www.olx.pl/path/file/more", True),
])
def test_robots_wildcards_anchors_longest_rule_and_allow_tie(rules, url, allowed):
    policy = collector._robots_policy(("User-agent: *\n" + rules + "\n").encode())
    assert policy.can_fetch(collector.USER_AGENT, url) is allowed


def test_wildcard_disallow_blocks_network_page_before_request():
    session = Session([Response(content=b"User-agent: *\nDisallow: /nieruchomosci/*\n")])
    with pytest.raises(collector.OLXError, match="robots.txt"):
        collector.collect_olx(session=session, sleep=lambda value: None)
    assert len(session.calls) == 1


@pytest.mark.parametrize("robots,allowed", [
    ("User-agent: *\nDisallow: /\nUser-agent: warsawrealestateportfolio\nAllow: /", True),
    ("User-agent: Warsaw\nAllow: /\nUser-agent: WarsawRealEstatePortfolio\nDisallow: /nieruchomosci/", False),
    ("User-agent: WarsawRealEstatePortfolio\nAllow: /\nUser-agent: WARSAWREALESTATEPORTFOLIO\nDisallow: /nieruchomosci/", False),
    ("User-agent: *\nAllow: /\nUser-agent: *\nDisallow: /nieruchomosci/", False),
    ("User-agent: OtherBot\nUser-agent: WarsawRealEstatePortfolio\nDisallow: /nieruchomosci/", False),
    ("User-agent: OtherBot\nDisallow: /", True),
    ("User-agent: *\nAllow: /\nSitemap: https://www.olx.pl/sitemap.xml\nDisallow: /nieruchomosci/", False),
])
def test_robots_selects_best_agent_and_merges_matching_groups(robots, allowed):
    policy = collector._robots_policy(robots.encode())
    assert policy.can_fetch(collector.USER_AGENT, collector.DEFAULT_URL) is allowed


def test_merged_groups_use_conservative_delay_and_rate(parsed):
    robots = b"User-agent: *\nAllow: /\nCrawl-delay: 100\nUser-agent: WarsawRealEstatePortfolio\nAllow: /\nCrawl-delay: 3\nRequest-rate: 1/4\nUser-agent: WarsawRealEstatePortfolio\nAllow: /\nCrawl-delay: 8\nRequest-rate: 1/9\n"
    session = Session([Response(content=robots), Response()])
    sleeps = []
    _, audit = collector.collect_olx(session=session, sleep=sleeps.append)
    assert sleeps == [2.0, 9.0]
    assert audit["request_delay_seconds"] == 9


@pytest.mark.parametrize("rule,target,allowed", [
    ("/path/%62%61%7A", "/path/baz", False),
    ("/path/baz", "/path/%62%61%7a", False),
    ("/path/żółw", "/path/%C5%BC%C3%B3%C5%82w", False),
    ("/path/%c5%bc%c3%b3%c5%82w", "/path/żółw", False),
    ("/path/foo/bar", "/path/foo%2Fbar", True),
    ("/path/foo%2Fbar", "/path/foo/bar", True),
    ("/path/file-%2A.html", "/path/file-*.html", False),
    ("/path/file-%2A.html", "/path/file-other.html", True),
    ("/path/foo-%24", "/path/foo-$", False),
    ("/path/?q=żółw", "/path/?q=%C5%BC%C3%B3%C5%82w", False),
    ("/path/?q=a", "/path/?q=%61", False),
    ("/path/?q=https%3A%2F%2Ffoo.bar", "/path/?q=https://foo.bar", False),
    ("/path/?search[filter]=50", "/path/?search%5Bfilter%5D=50", False),
    ("/path/?q=%2A", "/path/?q=*", False),
    ("/path/?q=%24", "/path/?q=$", False),
])
def test_robots_normalizes_uri_octets_without_decoding_reserved_paths(rule, target, allowed):
    policy = collector._robots_policy(("User-agent: *\nDisallow: " + rule + "\n").encode())
    assert policy.can_fetch(collector.USER_AGENT, "https://www.olx.pl" + target) is allowed


def test_wildcard_redirect_target_is_checked_against_robots():
    robots = b"User-agent: *\nDisallow: /*?*blocked=1$\n"
    session = Session([Response(content=robots), Response(302, headers={"Location": "?filter=1&blocked=1"})])
    with pytest.raises(collector.OLXError, match="robots.txt"):
        collector.collect_olx(session=session, sleep=lambda value: None)
    assert len(session.calls) == 2


@pytest.mark.parametrize("robots", [b"", b"<html>Access denied</html>", b"Disallow: /\n", b"User-agent:\nAllow: /\n", b"User-agent: *\nMalformed line\n"])
def test_missing_or_malformed_robots_is_not_permission(robots):
    session = Session([Response(content=robots)])
    with pytest.raises(collector.OLXError):
        collector.collect_olx(session=session, sleep=lambda value: None)
    assert len(session.calls) == 1


@pytest.mark.parametrize("status", [204, 301, 404, 403, 429])
def test_robots_requires_http_200(status):
    response = Response(status, ROBOTS, headers={"Location": collector.ROBOTS_URL})
    session = Session([response])
    with pytest.raises(collector.OLXError):
        collector.collect_olx(session=session, sleep=lambda value: None)
    assert len(session.calls) == 1
    assert response.closed


@pytest.mark.parametrize("status", [403, 429, 404, 204])
def test_page_denial_or_bad_status_is_not_retried(status):
    session = Session([Response(content=ROBOTS), Response(status)])
    with pytest.raises(collector.OLXError, match=f"HTTP {status}"):
        collector.collect_olx(session=session, sleep=lambda value: None)
    assert len(session.calls) == 2


def test_timeout_and_server_error_use_bounded_retries(parsed):
    session = Session([Response(content=ROBOTS), Timeout("secret transport details"), Response(503), Response()])
    sleeps = []
    _, audit = collector.collect_olx(session=session, sleep=sleeps.append)
    assert audit["request_counts"] == {"robots": 1, "html": 3}
    assert sleeps == [2.0, 2.0, 4.0, 8.0]


@pytest.mark.parametrize("failure", [Timeout("private token"), Response(500)])
def test_transient_failures_stop_after_three_attempts(failure):
    session = Session([Response(content=ROBOTS)] + [failure] * 3)
    with pytest.raises(collector.OLXError) as captured:
        collector.collect_olx(session=session, sleep=lambda value: None)
    assert "private token" not in str(captured.value)
    assert len(session.calls) == 4
    assert captured.value.__cause__ is None


def test_non_timeout_transport_errors_are_neutral_and_not_retried():
    session = Session([Response(content=ROBOTS), ConnectionError("password=secret")])
    with pytest.raises(collector.OLXError) as captured:
        collector.collect_olx(session=session, sleep=lambda value: None)
    assert len(session.calls) == 2
    assert "secret" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_allowed_redirect_is_checked_against_robots(parsed):
    robots = b"User-agent: *\nDisallow: /nieruchomosci/mieszkania/sprzedaz/warszawa/?blocked=1\n"
    session = Session([Response(content=robots), Response(302, headers={"Location": "?blocked=1"})])
    with pytest.raises(collector.OLXError, match="robots.txt"):
        collector.collect_olx(session=session, sleep=lambda value: None)
    assert len(session.calls) == 2


def test_same_host_first_page_redirect_is_audited(parsed):
    final_url = collector.DEFAULT_URL + "?search[filter_float_price:to]=900000"
    session = Session([Response(content=ROBOTS), Response(302, headers={"location": final_url}), Response()])
    _, audit = collector.collect_olx(session=session, sleep=lambda value: None)
    assert audit["requested_url"] == collector.DEFAULT_URL
    assert audit["final_url"] == final_url
    assert len(session.calls) == 3


@pytest.mark.parametrize("location", [
    "https://evil.example/", "http://www.olx.pl" + collector.SEARCH_PATH,
    "https://www.olx.pl/login/", "?page=2", "#fragment", "",
])
def test_unsafe_redirect_is_never_requested(location):
    session = Session([Response(content=ROBOTS), Response(302, headers={"Location": location})])
    with pytest.raises(collector.OLXError):
        collector.collect_olx(session=session, sleep=lambda value: None)
    assert len(session.calls) == 2


def test_redirect_limit_is_three(parsed):
    redirects = [Response(302, headers={"Location": f"?filter={index}"}) for index in range(4)]
    session = Session([Response(content=ROBOTS)] + redirects)
    with pytest.raises(collector.OLXError, match="przekierowania"):
        collector.collect_olx(session=session, sleep=lambda value: None)
    assert len(session.calls) == 5
    assert all(response.closed for response in redirects)


@pytest.mark.parametrize("kind,maximum", [("robots", collector.MAX_ROBOTS_BYTES), ("html", collector.MAX_HTML_BYTES)])
def test_stream_size_limits_stop_without_reading_remaining_chunks(kind, maximum):
    too_large = Response(chunks=[b"x" * maximum, b"x", b"remaining"])
    session = Session([too_large] if kind == "robots" else [Response(content=ROBOTS), too_large])
    with pytest.raises(collector.OLXError, match="rozmiar"):
        collector.collect_olx(session=session, sleep=lambda value: None)
    assert too_large.yielded == 2
    assert too_large.closed


def test_declared_excessive_content_length_stops_before_stream():
    response = Response(headers={"Content-Length": str(collector.MAX_HTML_BYTES + 1)})
    session = Session([Response(content=ROBOTS), response])
    with pytest.raises(collector.OLXError, match="rozmiar"):
        collector.collect_olx(session=session, sleep=lambda value: None)
    assert response.yielded == 0
    assert response.closed


def test_stream_timeout_retries_and_closes_failed_response(parsed):
    failed = Response(chunks=[b"partial", Timeout("sensitive")])
    session = Session([Response(content=ROBOTS), failed, Response()])
    _, audit = collector.collect_olx(session=session, sleep=lambda value: None)
    assert failed.closed
    assert audit["request_counts"]["html"] == 2


@pytest.mark.parametrize("bad_html", [b"", b"x" * (collector.MAX_HTML_BYTES + 1), b"\xff"], ids=["empty", "oversize", "invalid_utf8"])
def test_invalid_offline_input_fails_without_network(bad_html):
    with pytest.raises(collector.OLXError):
        collector.collect_olx(html=bad_html)


def test_naive_observation_timestamp_is_rejected(parsed):
    with pytest.raises(collector.OLXError, match="strefę"):
        collector.collect_olx(html=HTML, observed_at=datetime(2026, 10, 7))


def test_timestamp_is_normalized_to_utc(monkeypatch):
    expected = TIMESTAMP
    local = TIMESTAMP.astimezone(timezone(timedelta(hours=2)))

    def parse(html, url, observed_at):
        assert observed_at == expected
        return [record()]

    monkeypatch.setattr(collector, "_parse_html", parse)
    rows, audit = collector.collect_olx(html=HTML, observed_at=local)
    assert rows[0]["observed_at"].utcoffset() == timedelta(0)
    assert audit["observed_at"] == TIMESTAMP.isoformat()


@pytest.mark.parametrize("rows", [[], [record(), record()], [{"price_pln": 750000}], [{**record(), "price_pln": 0}], [{**record(), "price_pln": float("nan")}], [{**record(), "url": "https://evil.example/"}]])
def test_empty_or_invalid_records_never_become_success(monkeypatch, rows):
    monkeypatch.setattr(collector, "_parse_html", lambda html, url, observed_at: rows)
    with pytest.raises(collector.OLXError):
        collector.collect_olx(html=HTML)


@pytest.mark.parametrize("field,value", [
    ("area_m2", None), ("area_m2", 0), ("area_m2", -1), ("area_m2", True),
    ("area_m2", "50"), ("area_m2", float("nan")), ("area_m2", float("inf")),
    ("area_m2", float("-inf")),
    ("rooms", None), ("rooms", 0), ("rooms", -1), ("rooms", 4),
    ("rooms", True), ("rooms", False), ("rooms", 2.0), ("rooms", "2"), ("rooms", "four"),
    ("url", collector.DEFAULT_URL), ("url", "https://www.olx.pl/login/"),
    ("url", "https://www.olx.pl/d/oferta/"),
    ("url", "https://www.olx.pl/d/oferta/synthetic-IDTEST"),
    ("url", "https://www.olx.pl/d/oferta/nested/synthetic-IDTEST.html"),
])
def test_required_features_and_listing_path_are_enforced(monkeypatch, field, value):
    invalid = {**record(), field: value}
    monkeypatch.setattr(collector, "_parse_html", lambda html, url, observed_at: [invalid])
    with pytest.raises(collector.OLXError):
        collector.collect_olx(html=HTML)


def test_atomic_local_outputs_have_schema_and_audit(tmp_path, parsed):
    rows, report = collector.collect_olx(html=HTML, observed_at=TIMESTAMP)
    output = tmp_path / "olx_preview.csv"
    audit = tmp_path / "olx_preview_audit.json"
    collector._write_preview(rows, report, output, audit)
    with output.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        written = list(reader)
        assert tuple(reader.fieldnames) == FIELDS
    assert len(written) == 1
    assert written[0]["observed_at"] == TIMESTAMP.isoformat()
    assert written[0]["area_m2"] == "50.0"
    assert written[0]["rooms"] == "2"
    assert json.loads(audit.read_text(encoding="utf-8"))["availability_inference"] is False
    assert list(tmp_path.glob("*.tmp")) == []


def test_invalid_output_data_preserves_existing_files(tmp_path):
    output = tmp_path / "preview.csv"
    audit = tmp_path / "audit.json"
    output.write_text("previous csv", encoding="utf-8")
    audit.write_text("previous audit", encoding="utf-8")
    with pytest.raises(collector.OLXError):
        collector._write_preview([], {}, output, audit)
    assert output.read_text(encoding="utf-8") == "previous csv"
    assert audit.read_text(encoding="utf-8") == "previous audit"


@pytest.mark.parametrize("invalid", [
    {**record(), "area_m2": 0}, {**record(), "rooms": 4}, {**record(), "url": collector.DEFAULT_URL},
], ids=["invalid_area", "inexact_rooms", "search_instead_of_listing"])
def test_direct_write_rejects_invalid_features_and_preserves_previous_files(tmp_path, invalid):
    output = tmp_path / "preview.csv"
    audit = tmp_path / "audit.json"
    output.write_text("previous csv", encoding="utf-8")
    audit.write_text("previous audit", encoding="utf-8")
    with pytest.raises(collector.OLXError):
        collector._write_preview([invalid], {}, output, audit)
    assert output.read_text(encoding="utf-8") == "previous csv"
    assert audit.read_text(encoding="utf-8") == "previous audit"
    assert list(tmp_path.glob("*.tmp")) == []


def test_staging_failure_preserves_existing_files_and_removes_temps(tmp_path, parsed, monkeypatch):
    rows, report = collector.collect_olx(html=HTML)
    output = tmp_path / "preview.csv"
    audit = tmp_path / "audit.json"
    output.write_text("previous csv", encoding="utf-8")
    audit.write_text("previous audit", encoding="utf-8")

    def fail(*args, **kwargs):
        raise OSError("private filesystem detail")

    monkeypatch.setattr(collector, "_write_csv", fail)
    with pytest.raises(OSError):
        collector._write_preview(rows, report, output, audit)
    assert output.read_text(encoding="utf-8") == "previous csv"
    assert audit.read_text(encoding="utf-8") == "previous audit"
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize("csv_existed,audit_existed", [(True, True), (False, False), (True, False), (False, True)])
def test_second_replace_failure_rolls_back_pair_and_initial_absence(tmp_path, parsed, monkeypatch, csv_existed, audit_existed):
    rows, report = collector.collect_olx(html=HTML)
    output = tmp_path / "preview.csv"
    audit = tmp_path / "audit.json"
    if csv_existed:
        output.write_text("previous csv", encoding="utf-8")
    if audit_existed:
        audit.write_text("previous audit", encoding="utf-8")
    real_replace = collector.os.replace
    replace_calls = []

    def fail_second(source, destination):
        replace_calls.append((source, destination))
        if len(replace_calls) == 2:
            raise OSError("synthetic second commit failure")
        return real_replace(source, destination)

    monkeypatch.setattr(collector.os, "replace", fail_second)
    with pytest.raises(OSError, match="second commit failure"):
        collector._write_preview(rows, report, output, audit)
    assert output.exists() is csv_existed
    assert audit.exists() is audit_existed
    if csv_existed:
        assert output.read_text(encoding="utf-8") == "previous csv"
    if audit_existed:
        assert audit.read_text(encoding="utf-8") == "previous audit"
    assert list(tmp_path.glob("*.tmp")) == []


def test_successful_pair_replaces_existing_outputs_and_removes_backups(tmp_path, parsed):
    rows, report = collector.collect_olx(html=HTML)
    output = tmp_path / "preview.csv"
    audit = tmp_path / "audit.json"
    output.write_text("previous csv", encoding="utf-8")
    audit.write_text("previous audit", encoding="utf-8")
    collector._write_preview(rows, report, output, audit)
    assert output.read_text(encoding="utf-8").startswith("source,listing_id,")
    assert json.loads(audit.read_text(encoding="utf-8"))["exported_listings"] == 1
    assert list(tmp_path.glob("*.tmp")) == []


def test_csv_and_audit_cannot_share_destination(tmp_path, parsed):
    rows, report = collector.collect_olx(html=HTML)
    with pytest.raises(collector.OLXError, match="różne"):
        collector._write_preview(rows, report, tmp_path / "result", tmp_path / "result")


def test_cli_offline_writes_local_files(tmp_path, parsed, capsys):
    html = tmp_path / "synthetic.html"
    html.write_bytes(HTML)
    output = tmp_path / "preview.csv"
    audit = tmp_path / "audit.json"
    assert collector.main(["--html", str(html), "--output", str(output), "--audit", str(audit)]) == 0
    assert output.exists() and audit.exists()
    assert "próbka jednej strony" in capsys.readouterr().out


def test_cli_failure_does_not_write_or_leak_filesystem_details(tmp_path, capsys):
    output = tmp_path / "preview.csv"
    audit = tmp_path / "audit.json"
    assert collector.main(["--html", str(tmp_path / "secret-password.html"), "--output", str(output), "--audit", str(audit)]) == 1
    assert not output.exists() and not audit.exists()
    assert "secret-password" not in capsys.readouterr().out


def test_cli_empty_parser_result_preserves_existing_files(tmp_path, monkeypatch):
    html = tmp_path / "synthetic.html"
    html.write_bytes(HTML)
    output = tmp_path / "preview.csv"
    audit = tmp_path / "audit.json"
    output.write_text("previous", encoding="utf-8")
    audit.write_text("previous", encoding="utf-8")
    monkeypatch.setattr(collector, "_parse_html", lambda html, url, observed_at: [])
    assert collector.main(["--html", str(html), "--output", str(output), "--audit", str(audit)]) == 1
    assert output.read_text(encoding="utf-8") == "previous"
    assert audit.read_text(encoding="utf-8") == "previous"


def search_page(page=1, identifiers=(123,), *, total_pages=3, prices=None, invalid=False):
    """Generate fictional state pages with no dependency on provider content."""
    state = json.loads(synthetic_search_html().split("window.__PRERENDERED_STATE__=", 1)[1].split(";</script>", 1)[0])
    catalogue = state["listing"]["listing"]
    template = catalogue["ads"][0]
    catalogue.update(pageNumber=page - 1, totalPages=total_pages, totalElements=1000, visibleElements=4437)
    catalogue["requestParams"]["page"] = page - 1
    catalogue["ads"] = []
    for position, identifier in enumerate(identifiers):
        ad = json.loads(json.dumps(template))
        ad["id"] = identifier
        ad["url"] = f"https://www.olx.pl/d/oferta/synthetic-IDTEST{identifier}.html"
        if prices is not None:
            ad["price"]["regularPrice"]["value"] = prices[position]
        if invalid:
            ad["params"][1]["normalizedValue"] = "four"
        catalogue["ads"].append(ad)
    return '<html><script id="olx-init-config">window.__PRERENDERED_STATE__=' + json.dumps(state) + ";</script></html>"


def test_offline_search_multiple_pages_has_no_network_or_sleep():
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline pagination performs no I/O")

    rows, audit = collector.collect_olx_search(
        html_pages=[search_page(1, (123,)), search_page(2, (124,)), search_page(3, (125,))],
        sleep=forbidden, observed_at=TIMESTAMP,
    )
    assert [row["listing_id"] for row in rows] == ["123", "124", "125"]
    assert audit["status"] == "ok"
    assert audit["termination"] == "reported_search_end"
    assert audit["pages_read"] == audit["parsed_listings"] == audit["exported_listings"] == 3
    assert audit["request_counts"] == {"robots": 0, "html": 0}
    assert audit["reported_result_cap"] is True
    assert "incomplete" in audit["completeness"]
    assert audit["availability_inference"] is False
    json.dumps(audit, allow_nan=False)


def test_live_search_reuses_one_robots_policy_and_checks_exact_requested_pages():
    session = Session([
        Response(content=ROBOTS), Response(content=search_page(1).encode()),
        Response(content=search_page(2, (124,)).encode()),
    ])
    rows, audit = collector.collect_olx_search(session=session, sleep=lambda _: None, max_pages=2)
    assert len(rows) == 2
    assert [call[0] for call in session.calls] == [collector.ROBOTS_URL, collector.DEFAULT_URL, collector.DEFAULT_URL + "?page=2"]
    assert audit["request_counts"] == {"robots": 1, "html": 2}
    assert audit["status"] == "ok"
    assert audit["termination"] == "page_budget"
    assert not session.closed  # Caller owns the injected session.


def test_search_closes_owned_session(monkeypatch):
    from curl_cffi import requests

    session = Session([Response(content=ROBOTS), Response(content=search_page(total_pages=1).encode())])
    monkeypatch.setattr(requests, "Session", lambda **kwargs: session)
    _, audit = collector.collect_olx_search(sleep=lambda _: None)
    assert session.closed
    assert audit["termination"] == "reported_search_end"


@pytest.mark.parametrize("max_pages,max_listings", [
    (0, 1), (1001, 1), (True, 1), (1.0, 1), (1, 0), (1, 50001), (1, False), (1, 2.0),
])
def test_search_budget_validation_before_network(max_pages, max_listings):
    with pytest.raises(collector.OLXError):
        collector.collect_olx_search(max_pages=max_pages, max_listings=max_listings)


@pytest.mark.parametrize("pages", [[], "html", ("html",)])
def test_offline_search_requires_nonempty_list(pages):
    with pytest.raises(collector.OLXError):
        collector.collect_olx_search(html_pages=pages)


@pytest.mark.parametrize("query", ["?page=1&page=1", "?page=1&PAGE=1", "?%70age=1&page=1"])
def test_search_rejects_duplicate_page_parameters_before_network(query):
    with pytest.raises(collector.OLXError):
        collector.collect_olx_search(url=collector.DEFAULT_URL + query)


def test_search_budget_truncates_unique_listings_with_incomplete_coverage_audit():
    rows, audit = collector.collect_olx_search(html_pages=[search_page(1, (123, 124, 125))], max_listings=2)
    assert [row["listing_id"] for row in rows] == ["123", "124"]
    assert audit["parsed_listings"] == 3
    assert audit["exported_listings"] == 2
    assert audit["termination"] == "listing_budget"
    assert audit["status"] == "ok"
    assert "incomplete" in audit["completeness"]


def test_search_dedupes_across_pages_and_retains_later_price_deterministically():
    pages = [search_page(1, (123, 124)), search_page(2, (123, 125), prices=(700000, 900000))]
    rows, audit = collector.collect_olx_search(html_pages=pages, max_pages=2, observed_at=TIMESTAMP)
    assert [row["listing_id"] for row in rows] == ["123", "124", "125"]
    assert rows[0]["price_pln"] == 700000
    assert all(row["observed_at"] == TIMESTAMP for row in rows)
    assert audit["cross_page_duplicates"] == audit["cross_page_conflicts"] == 1
    assert audit["parsed_listings"] == 4


def test_repeated_result_page_stops_with_partial_coverage():
    rows, audit = collector.collect_olx_search(html_pages=[search_page(1), search_page(2), search_page(3, (124,))])
    assert len(rows) == 1
    assert audit["pages_read"] == 2
    assert audit["termination"] == "repeated_page"
    assert audit["status"] == "partial"


def test_no_supported_rows_on_later_page_does_not_hide_subsequent_results():
    rows, audit = collector.collect_olx_search(html_pages=[
        search_page(1), search_page(2, (124,), invalid=True), search_page(3, (125,)),
    ])
    assert [row["listing_id"] for row in rows] == ["123", "125"]
    assert audit["pages_read"] == 3
    assert audit["skipped_items"] == 1


@pytest.mark.parametrize("failure", [
    Response(403), Response(429), ConnectionError("password=secret"),
    Response(content=b'<html><title>Verify you are human</title></html>'),
    Response(content=search_page(1).encode()),
    Response(302, headers={"Location": "?page=1"}),
    Response(302, headers={"Location": "?page=2&page=2"}),
])
def test_later_search_page_failures_preserve_only_validated_prior_rows(failure):
    session = Session([Response(content=ROBOTS), Response(content=search_page(1).encode()), failure])
    rows, audit = collector.collect_olx_search(session=session, sleep=lambda _: None)
    assert [row["listing_id"] for row in rows] == ["123"]
    assert audit["status"] == "partial"
    assert audit["termination"] == "page_error"
    assert audit["failed_page"] == 2
    assert "secret" not in json.dumps(audit)
    assert len(session.calls) == 3


@pytest.mark.parametrize("page", ["", "<html>secret</html>", search_page(2), search_page(invalid=True)])
def test_first_search_page_failure_raises_instead_of_returning_empty_success(page):
    with pytest.raises(collector.OLXError):
        collector.collect_olx_search(html_pages=[page])


def test_page_specific_robots_disallow_stops_before_later_page_request():
    robots = b"User-agent: *\nDisallow: /*?page=2$\n"
    session = Session([Response(content=robots), Response(content=search_page(1).encode())])
    rows, audit = collector.collect_olx_search(session=session, sleep=lambda _: None)
    assert len(rows) == 1
    assert audit["failed_page"] == 2
    assert audit["termination"] == "page_error"
    assert audit["request_counts"] == {"robots": 1, "html": 1}


def test_missing_search_pagination_metadata_stops_without_guessing():
    rows, audit = collector.collect_olx_search(html_pages=[synthetic_search_html(), search_page(2, (124,))])
    assert len(rows) == 1
    assert audit["pages_read"] == 1
    assert audit["termination"] == "pagination_metadata_unavailable"


def test_offline_input_exhaustion_is_partial_not_source_end():
    _, audit = collector.collect_olx_search(html_pages=[search_page(1)])
    assert audit["termination"] == "offline_input_exhausted"
    assert audit["status"] == "partial"


def test_source_first_page_filter_is_preserved_in_later_page_request():
    url = collector.DEFAULT_URL + "?search%5Bfilter_float_price%3Ato%5D=900000&page=1"
    session = Session([Response(content=ROBOTS), Response(content=search_page(1).encode()), Response(content=search_page(2, (124,)).encode())])
    collector.collect_olx_search(url=url, session=session, sleep=lambda _: None, max_pages=2)
    assert session.calls[2][0] == collector.DEFAULT_URL + "?search%5Bfilter_float_price%3Ato%5D=900000&page=2"
