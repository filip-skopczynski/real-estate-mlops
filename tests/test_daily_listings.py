"""Daily runner tests use fake collectors and isolated SQLite, never Supabase."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from src import daily_listings, database
from src.availability import inventory_snapshots, listing_availability_observations
from src.fetch_olx import OLXError
from src.fetch_otodom import OtodomError

AT = datetime(2026, 10, 7, 10, tzinfo=timezone.utc)


def listing(source="www.olx.pl", identity="123", **changes):
    row = dict(source=source, listing_id=identity,
               url=("https://www.olx.pl/d/oferta/test-ID123.html" if source == "www.olx.pl" else "https://www.otodom.pl/pl/oferta/test-ID123"),
               city="Warszawa", district="Mokotów", price_pln=900_000,
               area_m2=50.0, rooms=2, floor=None, build_year=None,
               latitude=None, longitude=None, distance_km=None, observed_at=AT)
    row.update(changes)
    return row


def collector(name, *, partial=False, rows=None):
    def collect(**options):
        records = [listing(daily_listings.SOURCE_NAMES[name], observed_at=options["observed_at"])] if rows is None else rows
        return records, {"source": daily_listings.SOURCE_NAMES[name],
                         "status": "partial" if partial else "ok",
                         "observed_at": options["observed_at"].isoformat(),
                         "exported_listings": len(records), "pages_read": 1}
    return collect


@pytest.fixture
def engine():
    instance = database.get_engine("sqlite:///:memory:")
    database.init_db(instance)
    yield instance
    instance.dispose()


def test_repeated_capture_saves_prices_without_inventing_new_listings(engine):
    collectors = {name: collector(name) for name in ("olx", "otodom")}
    rows, report = daily_listings.run_collection(engine=engine, observed_at=AT, collectors=collectors)
    assert report["status"] == "ok"
    assert len(rows) == 2  # Same ID on different portals denotes two source identities.
    assert all(item["database_statistics"]["new_listings"] == 1 for item in report["sources"].values())
    _, replay = daily_listings.run_collection(engine=engine, observed_at=AT, collectors=collectors)
    assert all(item["database_statistics"]["observations_inserted"] == 0 for item in replay["sources"].values())
    _, later = daily_listings.run_collection(engine=engine, observed_at=AT + timedelta(days=1), collectors=collectors)
    assert all(item["database_statistics"]["new_listings"] == 0 for item in later["sources"].values())
    assert len(database.read_observations(engine)) == 4
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(inventory_snapshots)) == 0
        assert connection.scalar(select(func.count()).select_from(listing_availability_observations)) == 0


@pytest.mark.parametrize("failed_name,error_type", [("olx", OLXError), ("otodom", OtodomError)])
def test_source_failure_preserves_other_source_and_never_outputs_exception(engine, failed_name, error_type):
    collectors = {name: collector(name) for name in ("olx", "otodom")}
    collectors[failed_name] = Mock(side_effect=error_type("SECRET DATABASE URL"))
    rows, report = daily_listings.run_collection(engine=engine, observed_at=AT, collectors=collectors)
    assert report["status"] == "partial"
    assert report["sources"][failed_name]["status"] == "failed"
    assert len(rows) == len(database.read_observations(engine)) == 1
    assert "SECRET" not in json.dumps(report)


def test_later_page_failure_still_saves_validated_rows_and_marks_partial(engine):
    _, report = daily_listings.run_collection(source="olx", engine=engine, observed_at=AT,
                                             collectors={"olx": collector("olx", partial=True)})
    assert report["status"] == "partial"
    assert report["sources"]["olx"]["database_status"] == "saved"
    assert len(database.read_observations(engine)) == 1


def test_database_failure_keeps_csv_rows_sanitizes_error_and_other_source(monkeypatch, engine):
    actual = database.upsert_listings_report
    def save(instance, rows):
        if rows[0]["source"] == "www.olx.pl":
            raise OperationalError("SECRET POSTGRES CONNECTION", {}, Exception("SECRET"))
        return actual(instance, rows)
    monkeypatch.setattr(database, "upsert_listings_report", save)
    rows, report = daily_listings.run_collection(engine=engine, observed_at=AT,
                   collectors={name: collector(name) for name in ("olx", "otodom")})
    assert len(rows) == 2
    assert report["status"] == "partial"
    assert report["sources"]["olx"]["database_status"] == "failed_rolled_back"
    assert report["sources"]["otodom"]["database_status"] == "saved"
    assert "SECRET" not in json.dumps(report)


def test_without_save_request_no_engine_is_constructed(monkeypatch):
    mock = Mock(side_effect=AssertionError("Must not read environment"))
    monkeypatch.setattr(database, "get_engine", mock)
    _, report = daily_listings.run_collection(source="olx", observed_at=AT, collectors={"olx": collector("olx")})
    assert report["sources"]["olx"]["database_status"] == "not_requested"
    mock.assert_not_called()


@pytest.mark.parametrize("options", [dict(max_pages_olx=0), dict(max_pages_otodom=1001),
    dict(max_listings=True), dict(max_listings=50001), dict(delay=1), dict(delay=float("nan")),
    dict(delay=True), dict(source="rent"), dict(observed_at=datetime(2026, 1, 1))])
def test_invalid_configuration_does_not_start_sources(options):
    mock = Mock(side_effect=AssertionError("Must not fetch"))
    with pytest.raises(ValueError):
        daily_listings.run_collection(collectors={"olx": mock, "otodom": mock}, **options)
    mock.assert_not_called()


def test_offline_requires_pages_for_every_selected_source():
    mock = Mock(side_effect=AssertionError("Must not fetch"))
    with pytest.raises(ValueError):
        daily_listings.run_collection(offline_pages={"olx": ["HTML"]}, collectors={"olx": mock, "otodom": mock})
    mock.assert_not_called()


@pytest.mark.parametrize("changes", [{"rooms": 4}, {"observed_at": AT + timedelta(days=1)},
                                    {"source": "www.otodom.pl"}, {"price_pln": -1}])
def test_invalid_source_batch_is_not_written(engine, changes):
    _, report = daily_listings.run_collection(source="olx", observed_at=AT, engine=engine,
                  collectors={"olx": collector("olx", rows=[listing(**changes)])})
    assert report["status"] == "failed"
    assert database.read_observations(engine).empty


def test_all_source_failures_are_reported():
    rows, report = daily_listings.run_collection(collectors={
        "olx": Mock(side_effect=OLXError("failed")), "otodom": Mock(side_effect=OtodomError("failed"))})
    assert not rows
    assert report["status"] == "failed"


def test_artifacts_are_valid_even_for_failed_capture(tmp_path):
    output, audit = tmp_path / "rows.csv", tmp_path / "audit.json"
    daily_listings._write_outputs([], {"status": "failed"}, output, audit)
    assert "price_pln" in output.read_text(encoding="utf-8")
    assert json.loads(audit.read_text(encoding="utf-8"))["status"] == "failed"
    assert not list(tmp_path.glob(".*"))


def test_cli_can_test_sources_without_credentials(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(daily_listings, "run_collection", lambda **kw: ([], {"status": "failed", "sources": {}, "exported_listings": 0}))
    engine_mock = Mock(side_effect=AssertionError("No database"))
    monkeypatch.setattr(database, "get_engine", engine_mock)
    result = daily_listings.main(["--source", "olx", "--output", str(tmp_path / "rows.csv"), "--report", str(tmp_path / "audit.json")])
    assert result == 1
    assert (tmp_path / "audit.json").exists()
    engine_mock.assert_not_called()


def test_cli_refuses_mixed_offline_live_before_reading_database(monkeypatch, tmp_path):
    path = tmp_path / "olx.html"
    path.write_text("HTML", encoding="utf-8")
    mock = Mock(side_effect=AssertionError("No database or fetch"))
    monkeypatch.setattr(database, "get_engine", mock)
    monkeypatch.setattr(daily_listings, "run_collection", mock)
    assert daily_listings.main(["--offline-olx", str(path), "--save-db"]) == 1
    mock.assert_not_called()


def test_cli_never_prints_database_credentials(monkeypatch, capsys):
    monkeypatch.setattr(database, "get_engine", Mock(side_effect=OperationalError("SECRET URL", {}, Exception("PASSWORD"))))
    assert daily_listings.main(["--save-db"]) == 1
    output = capsys.readouterr().out
    assert "SECRET" not in output and "PASSWORD" not in output


def test_matching_output_paths_are_refused_before_fetch(monkeypatch, tmp_path):
    mock = Mock(side_effect=AssertionError("Must not fetch"))
    monkeypatch.setattr(daily_listings, "run_collection", mock)
    same = str(tmp_path / "same")
    assert daily_listings.main(["--output", same, "--report", same]) == 1
    mock.assert_not_called()


def test_fictional_html_flows_through_both_real_adapters_without_network(engine):
    fixtures = Path(__file__).parent / "fixtures"
    offline = {name: [(fixtures / (name + "_search.html")).read_text(encoding="utf-8")]
               for name in ("olx", "otodom")}
    rows, report = daily_listings.run_collection(offline_pages=offline, engine=engine,
                   observed_at=AT, max_pages_olx=1, max_pages_otodom=1)
    assert report["status"] == "ok"
    assert len(rows) == len(database.read_observations(engine)) == 4
    for source_report in report["sources"].values():
        assert source_report["request_counts"] == {"robots": 0, "html": 0}
        assert source_report["database_statistics"]["new_listings"] == 2


def test_lost_connection_is_retried_with_identical_timestamp_without_duplicates(monkeypatch, engine):
    actual = database.upsert_listings_report
    calls = []
    def save(instance, rows):
        calls.append(rows[0]["observed_at"])
        if len(calls) == 1:
            # Simulate lost acknowledgement after a successful commit.
            actual(instance, rows)
            raise OperationalError(None, None, Exception("private connection details"), connection_invalidated=True)
        return actual(instance, rows)
    monkeypatch.setattr(database, "upsert_listings_report", save)
    monkeypatch.setattr(engine, "dispose", Mock())  # Keep isolated in-memory SQLite available.
    sleep = Mock()
    monkeypatch.setattr(daily_listings.time, "sleep", sleep)
    _, report = daily_listings.run_collection(source="olx", engine=engine, observed_at=AT,
                                             collectors={"olx": collector("olx")})
    assert report["status"] == "ok" and calls == [AT, AT]
    assert report["sources"]["olx"]["database_write_attempts"] == 2
    assert report["sources"]["olx"]["database_statistics"]["observations_inserted"] == 0
    assert len(database.read_observations(engine)) == 1
    sleep.assert_called_once_with(2)


def test_repeated_connection_failure_stops_after_three_attempts(monkeypatch, engine):
    save = Mock(side_effect=OperationalError(None, None, Exception("PRIVATE"), connection_invalidated=True))
    monkeypatch.setattr(database, "upsert_listings_report", save)
    monkeypatch.setattr(daily_listings.time, "sleep", Mock())
    _, report = daily_listings.run_collection(source="olx", engine=engine, observed_at=AT,
                                             collectors={"olx": collector("olx")})
    assert save.call_count == 3 and report["status"] == "partial"
    assert "PRIVATE" not in json.dumps(report)


def test_cli_loads_local_limits_before_parsing_environment_defaults(monkeypatch, tmp_path):
    def dotenv(*args, **kwargs):
        assert kwargs["override"] is False
        monkeypatch.setenv("OLX_MAX_PAGES", "7")
        monkeypatch.setenv("PORTAL_MAX_LISTINGS", "80")
    monkeypatch.setattr(daily_listings, "load_dotenv", dotenv)
    captured = {}
    def run(**options):
        captured.update(options)
        return [], {"status": "ok", "sources": {}}
    monkeypatch.setattr(daily_listings, "run_collection", run)
    assert daily_listings.main(["--output", str(tmp_path / "rows.csv"), "--report", str(tmp_path / "audit.json")]) == 0
    assert captured["max_pages_olx"] == 7 and captured["max_listings"] == 80
