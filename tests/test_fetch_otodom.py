"""Synthetic transport tests: no Otodom website or database requests are made."""
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest
from curl_cffi.requests.exceptions import Timeout

from src import fetch_otodom as collector
from src.fetch_data import FIELDS

TIMESTAMP = datetime(2026, 10, 7, 10, tzinfo=timezone.utc)
ROBOTS = b"User-agent: *\nAllow: /\n"
HTML = b"<html><body>Synthetic Otodom fixture</body></html>"


def record(identifier="SYNTHETIC1"):
    row = {field: None for field in FIELDS}
    row.update(
        source="www.otodom.pl", listing_id=identifier,
        url=f"https://www.otodom.pl/pl/oferta/synthetic-ID{identifier}",
        city="Warszawa", price_pln=750000.0, area_m2=50.0, rooms=2, observed_at=TIMESTAMP,
    )
    return row


def synthetic_search_html():
    state = {"props": {"pageProps": {
        "estate": "FLAT", "transaction": "SELL",
        "location": "mazowieckie/warszawa/warszawa/warszawa",
        "canonicalURL": collector.SEARCH_PATH, "filteringQueryParams": {"page": 1},
        "data": {"searchAds": {
            "pagination": {"currentPage": 1},
            "items": [{
                "id": 900001, "slug": "syntetyczne-mieszkanie-IDtestA",
                "href": "[lang]/ad/syntetyczne-mieszkanie-IDtestA",
                "estate": "FLAT", "transaction": "SELL", "hidePrice": False,
                "totalPrice": {"value": 750000, "currency": "PLN"},
                "areaInSquareMeters": 50, "roomsNumber": "TWO", "floorNumber": "FIRST",
                "location": {"reverseGeocoding": {"locations": [{
                    "id": "mazowieckie/warszawa/warszawa/warszawa",
                    "locationLevel": "city_or_village", "name": "Warszawa",
                }]}},
            }],
        }},
    }}}
    return '<html><script id="__NEXT_DATA__" type="application/json">' + json.dumps(state) + "</script></html>"


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
    "http://www.otodom.pl" + collector.SEARCH_PATH,
    "https://otodom.pl" + collector.SEARCH_PATH,
    "https://www.otodom.pl.evil.example" + collector.SEARCH_PATH,
    "https://user:password@www.otodom.pl" + collector.SEARCH_PATH,
    "https://www.otodom.pl:443" + collector.SEARCH_PATH,
    collector.DEFAULT_URL + "#fragment",
    collector.DEFAULT_URL + "#",
    "https://www.otodom.pl/pl/wyniki/wynajem/mieszkanie/mazowieckie/warszawa/warszawa/warszawa",
    "https://www.otodom.pl/pl/wyniki/sprzedaz/dom/mazowieckie/warszawa/warszawa/warszawa",
    "https://www.otodom.pl/pl/oferta/synthetic-IDTEST",
    collector.DEFAULT_URL + "?search[description]=wynajem",
    collector.DEFAULT_URL + "?q=%2577ynajem",
    collector.DEFAULT_URL + "?page=2",
    collector.DEFAULT_URL + "?page=0",
    collector.DEFAULT_URL + "?page=1&page=2",
    collector.DEFAULT_URL + "?q=%0AInjected",
    collector.DEFAULT_URL + "\\extra",
    collector.DEFAULT_URL + "//",
    "https://www.otodom.pl/pl/wyniki/sprzedaz/mieszkanie/malopolskie/krakow/krakow/krakow",
])
def test_rejects_urls_outside_first_page_sale_scope(url):
    with pytest.raises(collector.OtodomError):
        collector.validate_search_url(url)


def test_allows_filters_on_first_page():
    url = collector.DEFAULT_URL + "?priceMax=900000&page=1"
    assert collector.validate_search_url(url) == url


@pytest.mark.parametrize("path", [collector.SEARCH_PATH, collector.SEARCH_PATH + "/", collector.LEGACY_SEARCH_PATH, collector.LEGACY_SEARCH_PATH + "/"])
def test_equivalent_warsaw_sale_paths_are_supported(path):
    url = "https://www.otodom.pl" + path
    assert collector.validate_search_url(url) == url


@pytest.mark.parametrize("limit,delay", [(0, 2), (101, 2), (True, 2), (1.5, 2), (20, 0), (20, 1.9), (20, float("inf")), (20, float("nan"))])
def test_invalid_limits_fail_before_any_session(limit, delay):
    with pytest.raises(collector.OtodomError):
        collector.collect_otodom(max_listings=limit, delay=delay)


def test_offline_preview_does_not_create_session_and_reports_sample(monkeypatch):
    def parse(html, url, observed_at):
        assert html == HTML.decode()
        assert url == collector.DEFAULT_URL
        assert observed_at == TIMESTAMP
        return [record(str(index)) for index in range(3)]

    monkeypatch.setattr(collector, "_parse_html", parse)
    rows, audit = collector.collect_otodom(html=HTML, observed_at=TIMESTAMP, max_listings=2)
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
    rows, audit = collector.collect_otodom(html=synthetic_search_html(), observed_at=TIMESTAMP)
    assert len(rows) == 1
    assert rows[0]["listing_id"] == "900001"
    assert rows[0]["url"] == "https://www.otodom.pl/pl/oferta/syntetyczne-mieszkanie-IDtestA"
    assert rows[0]["price_pln"] == 750000
    assert rows[0]["area_m2"] == 50
    assert rows[0]["rooms"] == 2
    assert rows[0]["observed_at"] == TIMESTAMP
    assert audit["request_counts"] == {"robots": 0, "html": 0}


def test_http_200_challenge_is_rejected_by_real_parser():
    challenge = b"<html><head><title>Verify you are human</title></head></html>"
    response = Response(content=challenge)
    session = Session([Response(content=ROBOTS), response])
    with pytest.raises(collector.OtodomError, match="blokad"):
        collector.collect_otodom(session=session, sleep=lambda value: None)
    assert len(session.calls) == 2
    assert response.closed


def test_live_reads_only_robots_and_one_page(parsed):
    responses = [Response(content=ROBOTS, headers={"Content-Type": "text/plain"}), Response(headers={"Content-Type": "text/html; charset=utf-8"})]
    session = Session(responses)
    sleeps = []
    rows, audit = collector.collect_otodom(session=session, sleep=sleeps.append, observed_at=TIMESTAMP)
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
    b"User-agent: WarsawRealEstatePortfolio\nDisallow: /pl/\n\nUser-agent: *\nAllow: /\n",
])
def test_robots_disallow_stops_before_page(robots):
    session = Session([Response(content=robots)])
    with pytest.raises(collector.OtodomError, match="robots.txt"):
        collector.collect_otodom(session=session, sleep=lambda value: None)
    assert len(session.calls) == 1


def test_robots_crawl_delay_and_request_rate_are_respected(parsed):
    robots = b"User-agent: *\nAllow: /\nCrawl-delay: 6\nRequest-rate: 1/10\n"
    session = Session([Response(content=robots), Response()])
    sleeps = []
    _, audit = collector.collect_otodom(session=session, sleep=sleeps.append)
    assert sleeps == [2.0, 10.0]
    assert audit["request_delay_seconds"] == 10


def test_wildcard_disallow_blocks_network_page_before_request():
    session = Session([Response(content=b"User-agent: *\nDisallow: /pl/*\n")])
    with pytest.raises(collector.OtodomError, match="robots.txt"):
        collector.collect_otodom(session=session, sleep=lambda value: None)
    assert len(session.calls) == 1


def test_merged_groups_use_conservative_delay_and_rate(parsed):
    robots = b"User-agent: *\nAllow: /\nCrawl-delay: 100\nUser-agent: WarsawRealEstatePortfolio\nAllow: /\nCrawl-delay: 3\nRequest-rate: 1/4\nUser-agent: WarsawRealEstatePortfolio\nAllow: /\nCrawl-delay: 8\nRequest-rate: 1/9\n"
    session = Session([Response(content=robots), Response()])
    sleeps = []
    _, audit = collector.collect_otodom(session=session, sleep=sleeps.append)
    assert sleeps == [2.0, 9.0]
    assert audit["request_delay_seconds"] == 9


def test_wildcard_redirect_target_is_checked_against_robots():
    robots = b"User-agent: *\nDisallow: /*?*blocked=1$\n"
    session = Session([Response(content=robots), Response(302, headers={"Location": "?filter=1&blocked=1"})])
    with pytest.raises(collector.OtodomError, match="robots.txt"):
        collector.collect_otodom(session=session, sleep=lambda value: None)
    assert len(session.calls) == 2


@pytest.mark.parametrize("robots", [b"", b"<html>Access denied</html>", b"Disallow: /\n", b"User-agent:\nAllow: /\n", b"User-agent: *\nMalformed line\n"])
def test_missing_or_malformed_robots_is_not_permission(robots):
    session = Session([Response(content=robots)])
    with pytest.raises(collector.OtodomError):
        collector.collect_otodom(session=session, sleep=lambda value: None)
    assert len(session.calls) == 1


@pytest.mark.parametrize("status", [204, 301, 404, 403, 429])
def test_robots_requires_http_200(status):
    response = Response(status, ROBOTS, headers={"Location": collector.ROBOTS_URL})
    session = Session([response])
    with pytest.raises(collector.OtodomError):
        collector.collect_otodom(session=session, sleep=lambda value: None)
    assert len(session.calls) == 1
    assert response.closed


@pytest.mark.parametrize("status", [403, 429, 404, 204])
def test_page_denial_or_bad_status_is_not_retried(status):
    session = Session([Response(content=ROBOTS), Response(status)])
    with pytest.raises(collector.OtodomError, match=f"HTTP {status}"):
        collector.collect_otodom(session=session, sleep=lambda value: None)
    assert len(session.calls) == 2


def test_timeout_and_server_error_use_bounded_retries(parsed):
    session = Session([Response(content=ROBOTS), Timeout("secret transport details"), Response(503), Response()])
    sleeps = []
    _, audit = collector.collect_otodom(session=session, sleep=sleeps.append)
    assert audit["request_counts"] == {"robots": 1, "html": 3}
    assert sleeps == [2.0, 2.0, 4.0, 8.0]


@pytest.mark.parametrize("failure", [Timeout("private token"), Response(500)])
def test_transient_failures_stop_after_three_attempts(failure):
    session = Session([Response(content=ROBOTS)] + [failure] * 3)
    with pytest.raises(collector.OtodomError) as captured:
        collector.collect_otodom(session=session, sleep=lambda value: None)
    assert "private token" not in str(captured.value)
    assert len(session.calls) == 4
    assert captured.value.__cause__ is None


def test_non_timeout_transport_errors_are_neutral_and_not_retried():
    session = Session([Response(content=ROBOTS), ConnectionError("password=secret")])
    with pytest.raises(collector.OtodomError) as captured:
        collector.collect_otodom(session=session, sleep=lambda value: None)
    assert len(session.calls) == 2
    assert "secret" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_allowed_redirect_is_checked_against_robots(parsed):
    robots = ("User-agent: *\nDisallow: " + collector.SEARCH_PATH + "?blocked=1\n").encode()
    session = Session([Response(content=robots), Response(302, headers={"Location": "?blocked=1"})])
    with pytest.raises(collector.OtodomError, match="robots.txt"):
        collector.collect_otodom(session=session, sleep=lambda value: None)
    assert len(session.calls) == 2


def test_same_host_first_page_redirect_is_audited(parsed):
    final_url = collector.DEFAULT_URL + "?priceMax=900000"
    session = Session([Response(content=ROBOTS), Response(302, headers={"location": final_url}), Response()])
    _, audit = collector.collect_otodom(session=session, sleep=lambda value: None)
    assert audit["requested_url"] == collector.DEFAULT_URL
    assert audit["final_url"] == final_url
    assert len(session.calls) == 3


@pytest.mark.parametrize("location", [
    "https://evil.example/", "http://www.otodom.pl" + collector.SEARCH_PATH,
    "https://www.otodom.pl/login/", "?page=2", "#fragment", "",
])
def test_unsafe_redirect_is_never_requested(location):
    session = Session([Response(content=ROBOTS), Response(302, headers={"Location": location})])
    with pytest.raises(collector.OtodomError):
        collector.collect_otodom(session=session, sleep=lambda value: None)
    assert len(session.calls) == 2


def test_redirect_limit_is_three(parsed):
    redirects = [Response(302, headers={"Location": f"?filter={index}"}) for index in range(4)]
    session = Session([Response(content=ROBOTS)] + redirects)
    with pytest.raises(collector.OtodomError, match="przekierowania"):
        collector.collect_otodom(session=session, sleep=lambda value: None)
    assert len(session.calls) == 5
    assert all(response.closed for response in redirects)


@pytest.mark.parametrize("kind,maximum", [("robots", collector.MAX_ROBOTS_BYTES), ("html", collector.MAX_HTML_BYTES)])
def test_stream_size_limits_stop_without_reading_remaining_chunks(kind, maximum):
    too_large = Response(chunks=[b"x" * maximum, b"x", b"remaining"])
    session = Session([too_large] if kind == "robots" else [Response(content=ROBOTS), too_large])
    with pytest.raises(collector.OtodomError, match="rozmiar"):
        collector.collect_otodom(session=session, sleep=lambda value: None)
    assert too_large.yielded == 2
    assert too_large.closed


def test_declared_excessive_content_length_stops_before_stream():
    response = Response(headers={"Content-Length": str(collector.MAX_HTML_BYTES + 1)})
    session = Session([Response(content=ROBOTS), response])
    with pytest.raises(collector.OtodomError, match="rozmiar"):
        collector.collect_otodom(session=session, sleep=lambda value: None)
    assert response.yielded == 0
    assert response.closed


def test_stream_timeout_retries_and_closes_failed_response(parsed):
    failed = Response(chunks=[b"partial", Timeout("sensitive")])
    session = Session([Response(content=ROBOTS), failed, Response()])
    _, audit = collector.collect_otodom(session=session, sleep=lambda value: None)
    assert failed.closed
    assert audit["request_counts"]["html"] == 2


@pytest.mark.parametrize("bad_html", [b"", b"x" * (collector.MAX_HTML_BYTES + 1), b"\xff"], ids=["empty", "oversize", "invalid_utf8"])
def test_invalid_offline_input_fails_without_network(bad_html):
    with pytest.raises(collector.OtodomError):
        collector.collect_otodom(html=bad_html)


def test_naive_observation_timestamp_is_rejected(parsed):
    with pytest.raises(collector.OtodomError, match="strefę"):
        collector.collect_otodom(html=HTML, observed_at=datetime(2026, 10, 7))


def test_timestamp_is_normalized_to_utc(monkeypatch):
    expected = TIMESTAMP
    local = TIMESTAMP.astimezone(timezone(timedelta(hours=2)))

    def parse(html, url, observed_at):
        assert observed_at == expected
        return [record()]

    monkeypatch.setattr(collector, "_parse_html", parse)
    rows, audit = collector.collect_otodom(html=HTML, observed_at=local)
    assert rows[0]["observed_at"].utcoffset() == timedelta(0)
    assert audit["observed_at"] == TIMESTAMP.isoformat()


@pytest.mark.parametrize("rows", [[], [record(), record()], [{"price_pln": 750000}], [{**record(), "price_pln": 0}], [{**record(), "price_pln": float("nan")}], [{**record(), "url": "https://evil.example/"}]])
def test_empty_or_invalid_records_never_become_success(monkeypatch, rows):
    monkeypatch.setattr(collector, "_parse_html", lambda html, url, observed_at: rows)
    with pytest.raises(collector.OtodomError):
        collector.collect_otodom(html=HTML)


@pytest.mark.parametrize("field,value", [
    ("area_m2", None), ("area_m2", 0), ("area_m2", -1), ("area_m2", True),
    ("area_m2", "50"), ("area_m2", float("nan")), ("area_m2", float("inf")),
    ("area_m2", float("-inf")),
    ("rooms", None), ("rooms", 0), ("rooms", -1), ("rooms", 5), ("rooms", 11),
    ("rooms", True), ("rooms", False), ("rooms", 2.0), ("rooms", "2"), ("rooms", "four"),
    ("url", collector.DEFAULT_URL), ("url", "https://www.otodom.pl/login/"),
    ("url", "https://www.otodom.pl/pl/oferta/"),
    ("url", "https://www.otodom.pl/pl/oferta/synthetic-IDTEST#fragment"),
    ("url", "https://www.otodom.pl/pl/oferta/nested/synthetic-IDTEST"),
])
def test_required_features_and_listing_path_are_enforced(monkeypatch, field, value):
    invalid = {**record(), field: value}
    monkeypatch.setattr(collector, "_parse_html", lambda html, url, observed_at: [invalid])
    with pytest.raises(collector.OtodomError):
        collector.collect_otodom(html=HTML)


def test_atomic_local_outputs_have_schema_and_audit(tmp_path, parsed):
    rows, report = collector.collect_otodom(html=HTML, observed_at=TIMESTAMP)
    output = tmp_path / "otodom_preview.csv"
    audit = tmp_path / "otodom_preview_audit.json"
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
    with pytest.raises(collector.OtodomError):
        collector._write_preview([], {}, output, audit)
    assert output.read_text(encoding="utf-8") == "previous csv"
    assert audit.read_text(encoding="utf-8") == "previous audit"


@pytest.mark.parametrize("invalid", [
    {**record(), "area_m2": 0}, {**record(), "rooms": 11}, {**record(), "url": collector.DEFAULT_URL},
], ids=["invalid_area", "inexact_rooms", "search_instead_of_listing"])
def test_direct_write_rejects_invalid_features_and_preserves_previous_files(tmp_path, invalid):
    output = tmp_path / "preview.csv"
    audit = tmp_path / "audit.json"
    output.write_text("previous csv", encoding="utf-8")
    audit.write_text("previous audit", encoding="utf-8")
    with pytest.raises(collector.OtodomError):
        collector._write_preview([invalid], {}, output, audit)
    assert output.read_text(encoding="utf-8") == "previous csv"
    assert audit.read_text(encoding="utf-8") == "previous audit"
    assert list(tmp_path.glob("*.tmp")) == []


def test_staging_failure_preserves_existing_files_and_removes_temps(tmp_path, parsed, monkeypatch):
    rows, report = collector.collect_otodom(html=HTML)
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
    rows, report = collector.collect_otodom(html=HTML)
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
    rows, report = collector.collect_otodom(html=HTML)
    output = tmp_path / "preview.csv"
    audit = tmp_path / "audit.json"
    output.write_text("previous csv", encoding="utf-8")
    audit.write_text("previous audit", encoding="utf-8")
    collector._write_preview(rows, report, output, audit)
    assert output.read_text(encoding="utf-8").startswith("source,listing_id,")
    assert json.loads(audit.read_text(encoding="utf-8"))["exported_listings"] == 1
    assert list(tmp_path.glob("*.tmp")) == []


def test_csv_and_audit_cannot_share_destination(tmp_path, parsed):
    rows, report = collector.collect_otodom(html=HTML)
    with pytest.raises(collector.OtodomError, match="różne"):
        collector._write_preview(rows, report, tmp_path / "result", tmp_path / "result")


def test_cli_offline_writes_local_files(tmp_path, parsed, capsys):
    html = tmp_path / "synthetic"
    html.write_bytes(HTML)
    output = tmp_path / "preview.csv"
    audit = tmp_path / "audit.json"
    assert collector.main(["--html", str(html), "--output", str(output), "--audit", str(audit)]) == 0
    assert output.exists() and audit.exists()
    assert "próbka jednej strony" in capsys.readouterr().out


def test_cli_failure_does_not_write_or_leak_filesystem_details(tmp_path, capsys):
    output = tmp_path / "preview.csv"
    audit = tmp_path / "audit.json"
    assert collector.main(["--html", str(tmp_path / "secret-password"), "--output", str(output), "--audit", str(audit)]) == 1
    assert not output.exists() and not audit.exists()
    assert "secret-password" not in capsys.readouterr().out


def test_cli_empty_parser_result_preserves_existing_files(tmp_path, monkeypatch):
    html = tmp_path / "synthetic"
    html.write_bytes(HTML)
    output = tmp_path / "preview.csv"
    audit = tmp_path / "audit.json"
    output.write_text("previous", encoding="utf-8")
    audit.write_text("previous", encoding="utf-8")
    monkeypatch.setattr(collector, "_parse_html", lambda html, url, observed_at: [])
    assert collector.main(["--html", str(html), "--output", str(output), "--audit", str(audit)]) == 1
    assert output.read_text(encoding="utf-8") == "previous"
    assert audit.read_text(encoding="utf-8") == "previous"


def test_legacy_search_redirect_to_canonical_search_keeps_first_page(parsed):
    legacy = "https://www.otodom.pl" + collector.LEGACY_SEARCH_PATH + "?description=apartament"
    canonical = collector.DEFAULT_URL + "?description=apartament"
    session = Session([Response(content=ROBOTS), Response(301, headers={"Location": canonical}), Response()])
    _, audit = collector.collect_otodom(url=legacy, session=session, sleep=lambda value: None)
    assert [url for url, _ in session.calls] == [collector.ROBOTS_URL, legacy, canonical]
    assert audit["requested_url"] == legacy
    assert audit["final_url"] == canonical


def test_source_content_signals_are_preserved_and_do_not_imply_ml_permission(parsed):
    robots = b"User-agent: *\nAllow: /\nContent-Signal: search=yes, ai-input=no, ai-train=no\n"
    session = Session([Response(content=robots), Response()])
    _, audit = collector.collect_otodom(session=session, sleep=lambda value: None)
    assert audit["robots_check"]["content_signals"] == {"search": "yes", "ai-input": "no", "ai-train": "no"}
    assert audit["training_performed"] is False
    assert audit["data_use"] == "local price-field preview; no AI input or training"
    assert audit["source_use_permission"] == "not established by robots.txt"


def test_conflicting_content_signals_keep_explicit_no():
    robots = b"User-agent: *\nContent-Signal: ai-train=no, invalid=ignored\nContent-Signal: ai-train=yes, search=yes\n"
    assert collector._content_signals(robots) == {"ai-train": "no", "search": "yes"}


@pytest.mark.parametrize("rooms", [1, 4])
def test_collector_accepts_exact_room_count_above_olx_limit(monkeypatch, rooms):
    monkeypatch.setattr(collector, "_parse_html", lambda *args: [{**record(), "rooms": rooms}])
    rows, _ = collector.collect_otodom(html=HTML)
    assert rows[0]["rooms"] == rooms


@pytest.mark.parametrize("field,value", [
    ("source", "www.olx.pl"), ("source", "otodom.pl"),
    ("listing_id", ""), ("listing_id", "  "), ("listing_id", 123),
    ("city", "Kraków"), ("price_pln", True), ("price_pln", float("inf")),
    ("observed_at", "2026-10-07T10:00:00"),
])
def test_invalid_identity_price_and_timestamp_are_rejected(monkeypatch, field, value):
    monkeypatch.setattr(collector, "_parse_html", lambda *args: [{**record(), field: value}])
    with pytest.raises(collector.OtodomError):
        collector.collect_otodom(html=HTML)


@pytest.mark.parametrize("kind,content_type", [("robots", "text/html"), ("html", "application/json")])
def test_unexpected_content_type_is_rejected_and_response_closed(kind, content_type):
    bad = Response(headers={"Content-Type": content_type})
    session = Session([bad] if kind == "robots" else [Response(content=ROBOTS), bad])
    with pytest.raises(collector.OtodomError, match="typ"):
        collector.collect_otodom(session=session, sleep=lambda value: None)
    assert bad.closed


def test_internally_created_session_disables_environment_configuration_and_closes(parsed, monkeypatch):
    from curl_cffi import requests

    session = Session([Response(content=ROBOTS), Response()])
    calls = []

    def factory(**kwargs):
        calls.append(kwargs)
        return session

    monkeypatch.setattr(requests, "Session", factory)
    collector.collect_otodom(sleep=lambda value: None)
    assert calls == [{"trust_env": False, "default_headers": False, "discard_cookies": True, "retry": 0}]
    assert session.closed
