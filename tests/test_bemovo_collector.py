"""Offline source-pilot checks using synthetic records and HTTP responses."""

import csv
from datetime import date, datetime, timedelta, timezone
import json
from unittest.mock import Mock

import pytest

from src import fetch_bemovo as collector
from src.fetch_data import FIELDS


OBSERVED_AT = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
SNAPSHOT_DATE = date(2026, 10, 5)
VALID_FROM = datetime(2026, 10, 4, 22, tzinfo=timezone.utc)
VALID_UNTIL = datetime(2026, 10, 5, 21, 59, 59, tzinfo=timezone.utc)


def price(unit="A0/01", **changes):
    row = {
        "unit_number": unit,
        "price_pln": 1_200_000.50,
        "price_per_m2_pln": 24_564.00,
        "valid_from": VALID_FROM,
        "valid_until": VALID_UNTIL,
        "city": "Warszawa",
        "street": "Syntetyczna",
        "building_number": "95",
        "postal_code": "00-000",
        "developer_nip": "5252801624",
        "developer_url": "https://developer.example.test/",
    }
    row.update(changes)
    return row


def feature(unit="A0/01", **changes):
    row = {
        "number": unit,
        "slug_number": unit,
        "source_id": "synthetic-" + unit,
        "city": "Warszawa",
        "investment": "Bemovo PH1",
        "building": "A",
        "status": "available",
        "is_commercial_unit": False,
        "area_m2": 48.85,
        "rooms": 2,
        "floor": 0,
        "website_price_pln": 1_200_000.50,
        "feature_url": "https://features.example.test/apartment/" + unit.replace("/", "-"),
    }
    row.update(changes)
    return row


def combine(prices, features, **changes):
    options = {"observed_at": OBSERVED_AT, "snapshot_date": SNAPSHOT_DATE}
    options.update(changes)
    return collector.combine_bemovo_records(prices, features, **options)


def test_join_uses_exact_unit_id_and_website_features_not_price_derived_area():
    prices = [price("A0/02", price_per_m2_pln=1), price("A0/01")]
    features = [feature("A0/01"), feature("A0/02", area_m2=61.25, rooms=3, floor=1)]
    records, report = combine(prices, features)
    assert [row["listing_id"] for row in records] == [
        "5252801624:Bemovo PH1:A0/01", "5252801624:Bemovo PH1:A0/02",
    ]
    assert records[1]["area_m2"] == 61.25
    assert records[1]["rooms"] == 3
    assert records[1]["floor"] == 1
    assert records[1]["url"] == features[1]["feature_url"]
    for row in records:
        assert set(row) == set(FIELDS)
        assert row["source"] == "dane.gov.pl:39940"
        assert row["district"] == "Bemowo"
        assert row["observed_at"] == OBSERVED_AT
        assert all(row[name] is None for name in ["build_year", "latitude", "longitude", "distance_km"])
    assert report["matched_available_apartments"] == 2
    assert report["price_mismatches"] == 0
    assert report["area_provenance"] == "website area; never reconstructed from price"


@pytest.mark.parametrize("status", ["sold", "reserved"])
def test_unavailable_apartments_are_excluded(status):
    records, report = combine(
        [price("A0/01"), price("A0/02")],
        [feature("A0/01"), feature("A0/02", status=status)],
    )
    assert len(records) == 1
    assert records[0]["listing_id"].endswith("A0/01")
    assert report["website_status_counts"][status] == 1


def test_unpriced_commercial_unit_is_excluded():
    records, report = combine(
        [price()], [feature(), feature("U0/01", is_commercial_unit=True)],
    )
    assert len(records) == 1
    assert report["website_commercial_units"] == 1


def test_government_apartment_matching_commercial_unit_fails_closed():
    with pytest.raises(ValueError):
        combine([price(), price("U0/01")], [feature(), feature("U0/01", is_commercial_unit=True)])


@pytest.mark.parametrize("prices,features", [([], []), ([], [feature()]), ([price()], [])])
def test_empty_source_inventory_fails_closed(prices, features):
    with pytest.raises(ValueError):
        combine(prices, features)


def test_government_unit_without_matching_features_fails_closed():
    with pytest.raises(ValueError):
        combine([price(), price("A0/99")], [feature()])


def test_available_apartment_without_government_price_fails_closed():
    with pytest.raises(ValueError):
        combine([price()], [feature(), feature("A0/02")])


@pytest.mark.parametrize("duplicate_prices", [False, True])
def test_duplicate_unit_identities_fail_closed(duplicate_prices):
    prices = [price(), price()] if duplicate_prices else [price()]
    features = [feature()] if duplicate_prices else [feature(), feature()]
    with pytest.raises(ValueError):
        combine(prices, features)


def test_one_cent_difference_is_allowed():
    records, _ = combine([price()], [feature(website_price_pln=1_200_000.51)])
    assert len(records) == 1
    assert records[0]["price_pln"] == 1_200_000.50


def test_difference_above_one_cent_fails_closed():
    with pytest.raises(ValueError):
        combine([price()], [feature(website_price_pln=1_200_000.52)])


@pytest.mark.parametrize("changes", [
    {"valid_from": OBSERVED_AT + timedelta(minutes=1)},
    {"valid_until": OBSERVED_AT - timedelta(minutes=1)},
])
def test_price_interval_must_cover_observation_time(changes):
    with pytest.raises(ValueError):
        combine([price(**changes)], [feature()])


@pytest.mark.parametrize("changes", [
    {"developer_nip": "0000000000"}, {"building_number": "999"},
])
def test_wrong_developer_or_address_fails_closed(changes):
    with pytest.raises(ValueError):
        combine([price(**changes)], [feature()])


@pytest.mark.parametrize("changes", [{"city": "Kraków"}, {"investment": "Other investment"}])
def test_wrong_feature_city_or_investment_fails_closed(changes):
    with pytest.raises(ValueError):
        combine([price()], [feature(**changes)])


def test_snapshot_uses_warsaw_day_even_before_utc_midnight():
    capture = datetime(2026, 10, 4, 22, 30, tzinfo=timezone.utc)
    records, report = combine([price()], [feature()], observed_at=capture)
    assert records[0]["observed_at"] == capture
    assert report["snapshot_date"] == "2026-10-05"
    assert report["observed_at"] == capture.isoformat()


def test_snapshot_with_a_different_warsaw_day_fails_closed():
    with pytest.raises(ValueError):
        combine([price()], [feature()], snapshot_date=date(2026, 10, 4))


def test_naive_observation_time_is_rejected():
    with pytest.raises(ValueError):
        combine([price()], [feature()], observed_at=OBSERVED_AT.replace(tzinfo=None))


def test_aware_observation_is_normalized_to_utc():
    local_capture = OBSERVED_AT.astimezone(timezone(timedelta(hours=2)))
    records, _ = combine([price()], [feature()], observed_at=local_capture)
    assert records[0]["observed_at"] == OBSERVED_AT
    assert records[0]["observed_at"].tzinfo is timezone.utc


def test_snapshot_files_preserve_capture_time_and_report(tmp_path):
    records, report = combine([price()], [feature()])
    csv_path = tmp_path / "snapshots" / "observations.csv"
    report_path = tmp_path / "reports" / "audit.json"
    collector._write_snapshot(records, report, csv_path, report_path)
    with csv_path.open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    assert len(rows) == 1
    assert rows[0]["observed_at"] == OBSERVED_AT.isoformat()
    assert rows[0]["area_m2"] == "48.85"
    assert json.loads(report_path.read_text(encoding="utf-8")) == report


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def response(content, status=200, headers=None):
    return Mock(status_code=status, content=content.encode("utf-8"), headers=headers or {})


def source_responses(snapshot_date=SNAPSHOT_DATE):
    download_url = "https://api.dane.gov.pl/1.4/resources/synthetic-resource/data"
    dataset = {"data": {"id": "39940", "attributes": {"license_name": "CC0 1.0"}}}
    resources = {"data": [{"id": "synthetic-resource", "attributes": {
        "data_date": snapshot_date.isoformat(), "download_url": download_url,
    }}]}
    return [response(json.dumps(dataset)), response(json.dumps(resources)),
            response("synthetic csv"), response("<html>synthetic homepage</html>")], download_url


def test_collection_reads_four_public_resources_without_network(monkeypatch):
    responses, download_url = source_responses()
    session = FakeSession(responses)
    price_parser = Mock(return_value=[price()])
    feature_parser = Mock(return_value=[feature()])
    monkeypatch.setattr(collector, "parse_bemovo_prices", price_parser)
    monkeypatch.setattr(collector, "parse_bemovo_features", feature_parser)
    records, report = collector.collect_bemovo(session=session, delay=0, observed_at=OBSERVED_AT)
    assert len(records) == 1
    assert [url for url, _ in session.calls] == [
        collector.DATASET_URL, collector.RESOURCE_URL, download_url, collector.FEATURES_URL,
    ]
    for _, options in session.calls:
        assert options["timeout"] == 30
        assert options["allow_redirects"] is False
        assert options["impersonate"] == "chrome120"
    price_parser.assert_called_once_with("synthetic csv", as_of_date=SNAPSHOT_DATE)
    feature_parser.assert_called_once_with("<html>synthetic homepage</html>")
    assert report["resource_id"] == "synthetic-resource"
    assert len(report["csv_sha256"]) == len(report["html_sha256"]) == 64


def test_collection_stops_at_rate_limit_without_retrying():
    session = FakeSession([response("rate limited", status=429)])
    with pytest.raises(ValueError):
        collector.collect_bemovo(session=session, delay=0, observed_at=OBSERVED_AT)
    assert len(session.calls) == 1


def test_default_cli_writes_local_snapshot_without_database_access(monkeypatch, tmp_path):
    records, report = combine([price()], [feature()])
    dotenv = Mock()
    collect = Mock(return_value=(records, report))
    write_snapshot = Mock()
    get_engine = Mock(side_effect=AssertionError("Default collection must not open a database"))
    monkeypatch.setattr(collector, "load_dotenv", dotenv)
    monkeypatch.setattr(collector, "collect_bemovo", collect)
    monkeypatch.setattr(collector, "_write_snapshot", write_snapshot)
    monkeypatch.setattr("src.database.get_engine", get_engine)
    assert collector.main(["--output", str(tmp_path / "rows.csv"),
                           "--report-output", str(tmp_path / "audit.json")]) == 0
    write_snapshot.assert_called_once()
    get_engine.assert_not_called()


def test_failed_quality_check_does_not_save_a_snapshot_or_open_database(monkeypatch, tmp_path):
    write_snapshot = Mock()
    get_engine = Mock(side_effect=AssertionError("Failed collection must not open a database"))
    monkeypatch.setattr(collector, "load_dotenv", Mock())
    monkeypatch.setattr(collector, "collect_bemovo", Mock(side_effect=collector.BemovoError("Synthetic mismatch")))
    monkeypatch.setattr(collector, "_write_snapshot", write_snapshot)
    monkeypatch.setattr("src.database.get_engine", get_engine)
    assert collector.main(["--output", str(tmp_path / "rows.csv"),
                           "--report-output", str(tmp_path / "audit.json")]) == 1
    write_snapshot.assert_not_called()
    get_engine.assert_not_called()
