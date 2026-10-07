"""Orchestration tests use scripted portals and isolated SQLite only."""
from datetime import datetime, timedelta, timezone
import csv
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from src import catalog_pipeline as pipeline, catalog_storage as storage, database
from src.availability import inventory_snapshots, listing_availability_observations

AT = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)


def listing(source="www.olx.pl", identity="123", observed_at=AT, **changes):
    values = dict.fromkeys(storage.CATALOG_FIELDS)
    values.update(source=source, listing_id=identity,
        url=(f"https://www.olx.pl/d/oferta/apartment-ID{identity}.html" if source == "www.olx.pl"
             else f"https://www.otodom.pl/pl/oferta/apartment-ID{identity}"),
        city="Warszawa", district="Mokotów", price_pln=900_000,
        area_m2=50.0, rooms=2, observed_at=observed_at)
    values.update(changes)
    return values


def capture(name, options, *, identity="123", completed=True, partial=False,
            pages=1, requests=2, next_page=2, rows=None, **fields):
    source = pipeline.SOURCE_NAMES[name]
    batch = [listing(source, identity, options["observed_at"], **fields)] if rows is None else rows
    audit = {"source": source, "mode": options["mode"],
        "status": "partial" if partial else "ok", "completed": completed,
        "checkpoint": {"version": 1, "mode": options["mode"], "next_page": next_page},
        "pages_read": pages, "request_counts": {"robots": 1, "search": requests - 1}}
    return batch, audit


def collector(name, **settings):
    return Mock(side_effect=lambda **options: capture(name, options, **settings))


@pytest.fixture
def engine():
    value = database.get_engine("sqlite:///:memory:")
    database.init_db(value)
    yield value
    value.dispose()


def count(engine, table):
    with engine.connect() as connection:
        return connection.scalar(select(func.count()).select_from(table))


def mark_bootstrap_complete(engine, name="olx"):
    pipeline.run_collection(mode="bootstrap", source=name, engine=engine,
        observed_at=AT, collectors={name: collector(name)})


def test_daily_without_initial_progress_bootstraps_and_persists_sparse_catalogue(engine):
    collect = collector("olx", rooms=None, rooms_min=4)
    rows, report = pipeline.run_collection(source="olx", engine=engine,
        observed_at=AT, collectors={"olx": collect})
    assert collect.call_args.kwargs["mode"] == "bootstrap"
    assert report["sources"]["olx"]["executed_mode"] == "bootstrap"
    assert storage.read_progress(engine, "www.olx.pl", "bootstrap")["completed"]
    assert storage.known_ids(engine, "www.olx.pl") == {"123"}
    assert rows[0]["rooms"] is None and rows[0]["rooms_min"] == 4
    assert len(database.read_observations(engine)) == 1
    assert count(engine, inventory_snapshots) == count(engine, listing_availability_observations) == 0


def test_budget_pause_and_source_failure_keep_durable_resume_cursor(engine):
    bounded = collector("olx", completed=False, pages=2, requests=3, next_page=3)
    _, report = pipeline.run_collection(mode="bootstrap", source="olx", engine=engine,
        observed_at=AT, max_pages=2, collectors={"olx": bounded})
    assert report["status"] == "ok" and not report["sources"]["olx"]["completed"]
    assert storage.read_progress(engine, "www.olx.pl", "bootstrap")["checkpoint"]["next_page"] == 3
    failed = collector("olx", identity="124", completed=False, partial=True, next_page=4)
    _, second = pipeline.run_collection(mode="bootstrap", source="olx", engine=engine,
        observed_at=AT + timedelta(hours=1), collectors={"olx": failed})
    assert failed.call_args.kwargs["mode"] == "bootstrap"
    assert failed.call_args.kwargs["checkpoint"]["next_page"] == 3
    state = storage.read_progress(engine, "www.olx.pl", "bootstrap")
    assert state["checkpoint"]["next_page"] == 4 and state["last_error"] == "source_failed"
    assert state["lease_owner"] is None and second["status"] == "partial"
    finish = collector("olx", identity="125")
    pipeline.run_collection(mode="bootstrap", source="olx", engine=engine, observed_at=AT + timedelta(hours=2), collectors={"olx": finish})
    assert finish.call_args.kwargs["checkpoint"]["next_page"] == 4
    assert len(database.read_observations(engine)) == 3


def test_resumed_request_budget_pause_without_pages_is_not_a_source_failure(engine):
    pipeline.run_collection(mode="bootstrap", source="olx", engine=engine,
        observed_at=AT, max_pages=1, collectors={"olx": collector("olx", completed=False)})
    initial = storage.read_progress(engine, "www.olx.pl", "bootstrap")
    def exhausted(**options):
        rows, audit = capture("olx", options, rows=[], pages=0, requests=2, completed=False)
        audit.update(termination="request_budget", checkpoint=options["checkpoint"])
        return rows, audit
    rows, report = pipeline.run_collection(mode="bootstrap", source="olx", engine=engine,
        observed_at=AT + timedelta(hours=1), max_requests=2, collectors={"olx": exhausted})
    state = storage.read_progress(engine, "www.olx.pl", "bootstrap")
    assert report["status"] == "ok" and rows == []
    assert state["checkpoint"] == initial["checkpoint"]
    assert state["generation"] == initial["generation"] and not state["completed"]
    assert state["lease_owner"] is None and state["last_error"] is None
    assert count(engine, storage.listing_catalog) == 1


def test_completed_bootstrap_skips_fetch_and_preserves_generation(engine):
    mark_bootstrap_complete(engine)
    initial = storage.read_progress(engine, "www.olx.pl", "bootstrap")
    forbidden = Mock(side_effect=AssertionError("Completed bootstrap must not fetch"))
    rows, report = pipeline.run_collection(mode="bootstrap", source="olx", engine=engine,
        observed_at=AT, collectors={"olx": forbidden})
    forbidden.assert_not_called()
    assert rows == [] and report["status"] == "ok"
    assert report["sources"]["olx"]["phases"][0]["status"] == "skipped"
    assert storage.read_progress(engine, "www.olx.pl", "bootstrap")["generation"] == initial["generation"]


def test_daily_checks_newest_head_and_bounded_rotating_refresh(engine):
    mark_bootstrap_complete(engine)
    collect = collector("olx")
    _, report = pipeline.run_collection(source="olx", engine=engine,
        observed_at=AT + timedelta(days=1), collectors={"olx": collect})
    calls = [call.kwargs for call in collect.call_args_list]
    assert [call["mode"] for call in calls] == ["daily", "refresh"]
    assert all(call["max_pages"] == 30 and call["max_requests"] == 60 for call in calls)
    assert all("123" in call["known_ids"] for call in calls)
    assert [phase["phase"] for phase in report["sources"]["olx"]["phases"]] == ["daily", "refresh"]
    assert len(database.read_observations(engine)) == 2  # Same observation time replay within both phases.


def test_incomplete_bootstrap_does_not_starve_daily_newest_head(engine):
    pipeline.run_collection(mode="bootstrap", source="olx", max_pages=1, engine=engine,
        observed_at=AT, collectors={"olx": collector("olx", completed=False, next_page=2)})
    collect = collector("olx", identity="124")
    _, report = pipeline.run_collection(source="olx", engine=engine,
        observed_at=AT + timedelta(days=1), collectors={"olx": collect})
    calls = [call.kwargs for call in collect.call_args_list]
    assert [call["mode"] for call in calls] == ["daily", "bootstrap"]
    assert calls[0]["checkpoint"] is None and calls[0]["max_pages"] == 30
    assert calls[1]["checkpoint"]["next_page"] == 2
    assert report["sources"]["olx"]["completed"]


def test_broad_refresh_resumes_incomplete_cycle_then_restarts_completed_cycle(engine):
    first = collector("olx", completed=False, next_page=2)
    pipeline.run_collection(mode="refresh", source="olx", max_pages=1, engine=engine,
        observed_at=AT, collectors={"olx": first})
    initial_generation = storage.read_progress(engine, "www.olx.pl", "refresh")["generation"]
    finish = collector("olx", identity="124", next_page=3)
    pipeline.run_collection(mode="refresh", source="olx", engine=engine,
        observed_at=AT + timedelta(days=1), collectors={"olx": finish})
    assert finish.call_args.kwargs["checkpoint"]["next_page"] == 2
    assert storage.read_progress(engine, "www.olx.pl", "refresh")["generation"] == initial_generation
    restart = collector("olx", identity="125")
    pipeline.run_collection(mode="refresh", source="olx", engine=engine,
        observed_at=AT + timedelta(days=2), collectors={"olx": restart})
    assert restart.call_args.kwargs["checkpoint"] is None
    assert storage.read_progress(engine, "www.olx.pl", "refresh")["generation"] != initial_generation


@pytest.mark.parametrize("failed", ["olx", "otodom"])
def test_source_failure_isolation_and_private_error_sanitization(engine, failed):
    collects = {name: collector(name) for name in pipeline.SOURCE_NAMES}
    collects[failed] = Mock(side_effect=ValueError("PRIVATE DATABASE URL"))
    rows, report = pipeline.run_collection(mode="bootstrap", engine=engine,
        observed_at=AT, collectors=collects)
    assert report["status"] == "partial" and len(rows) == 1
    assert report["sources"][failed]["status"] == "failed"
    assert "PRIVATE" not in json.dumps(report)
    assert len(database.read_observations(engine)) == count(engine, storage.listing_catalog) == 1
    assert storage.read_progress(engine, pipeline.SOURCE_NAMES[failed], "bootstrap")["checkpoint"] == {}


def test_lease_contention_skips_owned_source_and_keeps_other_independent(engine):
    owner = "0" * 32
    initial = storage.acquire_progress(engine, "www.olx.pl", "bootstrap", owner)
    collects = {name: collector(name) for name in pipeline.SOURCE_NAMES}
    rows, report = pipeline.run_collection(mode="bootstrap", engine=engine,
        observed_at=AT, collectors=collects)
    collects["olx"].assert_not_called()
    assert collects["otodom"].call_count == 1 and len(rows) == 1
    assert report["sources"]["olx"]["status"] == "leased"
    assert report["sources"]["olx"]["pages_read"] == 0
    current = storage.read_progress(engine, "www.olx.pl", "bootstrap")
    assert current["lease_owner"] == owner and current["generation"] == initial["generation"]
    assert storage.known_ids(engine, "www.otodom.pl") == {"123"}


def test_preview_does_not_construct_engine_or_read_progress(monkeypatch):
    forbidden = Mock(side_effect=AssertionError("Preview must not use database"))
    monkeypatch.setattr(database, "get_engine", forbidden)
    monkeypatch.setattr(storage, "read_progress", forbidden)
    rows, report = pipeline.run_collection(mode="bootstrap", source="olx", observed_at=AT,
        collectors={"olx": collector("olx", price_pln=None, area_m2=None, rooms=None)})
    assert len(rows) == 1 and not report["database_requested"]
    assert report["sources"]["olx"]["database_status"] == "not_requested"
    forbidden.assert_not_called()


def test_broad_batches_commit_every_fifty_pages_before_next_fetch(engine):
    calls = []
    def collect(**options):
        calls.append(options)
        if len(calls) == 1:
            assert options["max_pages"] == 50 and options["checkpoint"] is None
            return capture("olx", options, completed=False, pages=50, requests=51, next_page=51)
        assert options["checkpoint"]["next_page"] == 51
        assert count(engine, storage.listing_catalog) == 1
        assert len(database.read_observations(engine)) == 1
        assert storage.read_progress(engine, "www.olx.pl", "bootstrap")["checkpoint"]["next_page"] == 51
        return capture("olx", options, identity="124", pages=50, requests=51, next_page=101)
    _, report = pipeline.run_collection(mode="bootstrap", source="olx", engine=engine,
        max_pages=100, max_requests=200, observed_at=AT, collectors={"olx": collect})
    assert len(calls) == 2 and calls[1]["max_requests"] == 149
    assert report["sources"]["olx"]["pages_read"] == 100
    assert report["sources"]["olx"]["requests"] == 102
    assert count(engine, storage.listing_catalog) == 2


def test_different_id_reusing_previous_batch_url_stops_before_cursor_commit(engine):
    calls = []
    def collect(**options):
        calls.append(options)
        if len(calls) == 1:
            return capture("olx", options, completed=False, pages=50, requests=51, next_page=51)
        return capture("olx", options, identity="124", pages=1, next_page=52,
            url=listing()["url"])
    rows, report = pipeline.run_collection(mode="bootstrap", source="olx", engine=engine,
        max_pages=100, observed_at=AT, collectors={"olx": collect})
    assert len(calls) == 2 and [row["listing_id"] for row in rows] == ["123"]
    assert report["status"] == "partial" and report["sources"]["olx"]["pages_read"] == 50
    state = storage.read_progress(engine, "www.olx.pl", "bootstrap")
    assert state["checkpoint"]["next_page"] == 51 and not state["completed"]
    assert count(engine, storage.listing_catalog) == len(database.read_observations(engine)) == 1


def test_same_id_changed_slug_between_batches_remains_one_catalogue_identity(monkeypatch, engine):
    class Clock(datetime):
        tick = 0
        @classmethod
        def now(cls, zone):
            cls.tick += 1
            return AT + timedelta(seconds=cls.tick)
    monkeypatch.setattr(pipeline, "datetime", Clock)
    calls = []
    changed_url = "https://www.olx.pl/d/oferta/new-title-ID123.html"
    def collect(**options):
        calls.append(options)
        if len(calls) == 1:
            return capture("olx", options, completed=False, pages=50, requests=51, next_page=51)
        return capture("olx", options, pages=1, next_page=52, url=changed_url)
    rows, report = pipeline.run_collection(mode="bootstrap", source="olx", engine=engine,
        max_pages=100, collectors={"olx": collect})
    assert report["status"] == "ok" and len(rows) == count(engine, storage.listing_catalog) == 1
    assert rows[0]["url"] == changed_url
    assert storage.known_ids(engine, "www.olx.pl") == {"123"}
    with engine.connect() as connection:
        assert connection.scalar(select(storage.listing_catalog.c.url)) == changed_url
    assert len(database.read_observations(engine)) == 2


def test_same_ids_across_batches_and_sources_have_precise_counts(engine):
    collects = {name: collector(name) for name in pipeline.SOURCE_NAMES}
    rows, first = pipeline.run_collection(mode="refresh", engine=engine, observed_at=AT, collectors=collects)
    assert len(rows) == 2 and first["exported_listings"] == 2
    assert all(item["catalogue_statistics"]["new_catalog_listings"] == 1 for item in first["sources"].values())
    rows, second = pipeline.run_collection(mode="refresh", engine=engine, observed_at=AT, collectors=collects)
    assert len(rows) == 2
    assert all(item["database_statistics"]["observations_inserted"] == 0 for item in second["sources"].values())
    assert all(item["catalogue_statistics"]["new_catalog_listings"] == 0 for item in second["sources"].values())
    assert len(database.read_observations(engine)) == 2


def test_sql_failure_rolls_back_catalogue_prices_and_cursor_then_other_source_succeeds(monkeypatch, engine):
    actual = storage._save_prices
    def fail(connection, rows):
        if rows and rows[0]["source"] == "www.olx.pl":
            raise OperationalError("PRIVATE SQL", {}, Exception("PRIVATE CREDENTIAL"))
        return actual(connection, rows)
    monkeypatch.setattr(storage, "_save_prices", fail)
    rows, report = pipeline.run_collection(mode="bootstrap", engine=engine, observed_at=AT,
        collectors={name: collector(name) for name in pipeline.SOURCE_NAMES})
    assert report["status"] == "partial"
    assert report["sources"]["olx"]["database_status"] == "failed_rolled_back"
    state = storage.read_progress(engine, "www.olx.pl", "bootstrap")
    assert state["checkpoint"] == {} and not state["completed"]
    assert state["lease_owner"] is None and state["last_error"] == "database_failed"
    with engine.connect() as connection:
        assert list(connection.execute(select(storage.listing_catalog.c.source)).scalars()) == ["www.otodom.pl"]
    assert list(database.read_observations(engine)["source"]) == ["www.otodom.pl"]
    assert "PRIVATE" not in json.dumps(report)
    assert len(rows) == 2  # Validated failed-DB rows remain inspectable in the CSV.


@pytest.mark.parametrize("changes", [
    {"price_pln": -1}, {"source": "www.otodom.pl"},
    {"observed_at": AT + timedelta(hours=1)}, {"published_at": datetime(2026, 1, 1)},
])
def test_invalid_capture_does_not_write_or_advance(engine, changes):
    collect = collector("olx", rows=[listing(**changes)])
    _, report = pipeline.run_collection(mode="bootstrap", source="olx", engine=engine,
        observed_at=AT, collectors={"olx": collect})
    assert report["status"] == "failed"
    assert count(engine, storage.listing_catalog) == 0
    assert database.read_observations(engine).empty
    assert storage.read_progress(engine, "www.olx.pl", "bootstrap")["checkpoint"] == {}


@pytest.mark.parametrize("over", ["pages", "requests"])
def test_audit_exceeding_caller_budget_is_rejected_before_persistence(engine, over):
    collect = collector("olx", pages=2 if over == "pages" else 1,
        requests=3 if over == "requests" else 2)
    _, report = pipeline.run_collection(mode="bootstrap", source="olx", engine=engine,
        observed_at=AT, max_pages=1, max_requests=2, collectors={"olx": collect})
    assert report["status"] == "failed" and count(engine, storage.listing_catalog) == 0
    assert storage.read_progress(engine, "www.olx.pl", "bootstrap")["checkpoint"] == {}


@pytest.mark.parametrize("options", [dict(max_pages=0), dict(max_requests=1),
    dict(max_listings=True), dict(delay=1), dict(delay=float("nan")), dict(mode="rent"),
    dict(source="country"), dict(observed_at=datetime(2026, 1, 1))])
def test_invalid_configuration_never_fetches(options):
    forbidden = Mock(side_effect=AssertionError("Invalid settings must not fetch"))
    with pytest.raises(ValueError):
        pipeline.run_collection(collectors={name: forbidden for name in pipeline.SOURCE_NAMES}, **options)
    forbidden.assert_not_called()


@pytest.mark.parametrize("save", [False, True])
def test_cli_explicit_database_flag_and_real_output_files(monkeypatch, tmp_path, engine, save):
    monkeypatch.setattr(pipeline, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline, "PROJECT_ROOT", tmp_path)
    for variable in ("CATALOG_MAX_PAGES", "CATALOG_MAX_LISTINGS", "CATALOG_MAX_REQUESTS", "REQUEST_DELAY_SECONDS"):
        monkeypatch.delenv(variable, raising=False)
    configured = Mock(return_value=engine) if save else Mock(side_effect=AssertionError("No --save-db means no engine"))
    monkeypatch.setattr(database, "get_engine", configured)
    run = Mock(return_value=([listing(price_pln=None, rooms=None)], {"status": "ok", "database_requested": save}))
    monkeypatch.setattr(pipeline, "run_collection", run)
    csv_path, audit_path = tmp_path / "preview.csv", tmp_path / "audit.json"
    arguments = ["--mode", "bootstrap", "--source", "olx", "--output", str(csv_path), "--report", str(audit_path)]
    if save:
        arguments.append("--save-db")
    assert pipeline.main(arguments) == 0
    assert run.call_args.kwargs["engine"] is (engine if save else None)
    assert run.call_args.kwargs["mode"] == "bootstrap"
    with csv_path.open(encoding="utf-8", newline="") as stream:
        values = list(csv.DictReader(stream))
    assert len(values) == 1 and values[0]["price_pln"] == "" and values[0]["rooms"] == ""
    assert json.loads(audit_path.read_text(encoding="utf-8"))["database_requested"] is save
    assert configured.call_count == int(save)


def test_cli_bad_paths_or_limits_do_not_open_database_or_fetch(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "load_dotenv", lambda *args, **kwargs: None)
    forbidden = Mock(side_effect=AssertionError("Configuration failure must precede network/database"))
    monkeypatch.setattr(database, "get_engine", forbidden)
    monkeypatch.setattr(pipeline, "run_collection", forbidden)
    same = str(tmp_path / "same")
    assert pipeline.main(["--save-db", "--output", same, "--report", same]) == 1
    assert pipeline.main(["--save-db", "--max-requests", "1"]) == 1
    forbidden.assert_not_called()


@pytest.mark.parametrize("prior_csv,prior_audit", [(False, False), (True, False), (False, True), (True, True)])
def test_pair_output_restores_prior_csv_when_second_replacement_fails(monkeypatch, tmp_path, prior_csv, prior_audit):
    csv_path, audit_path = tmp_path / "catalog.csv", tmp_path / "catalog_audit.json"
    if prior_csv:
        csv_path.write_bytes(b"original csv\n")
    if prior_audit:
        audit_path.write_bytes(b"original audit\n")
    real_replace = pipeline.os.replace
    blocked = False
    def fail_audit_once(source, target):
        nonlocal blocked
        if Path(target) == audit_path and not blocked:
            blocked = True
            raise OSError("Simulated second-file failure")
        return real_replace(source, target)
    monkeypatch.setattr(pipeline.os, "replace", fail_audit_once)
    with pytest.raises(OSError):
        pipeline._write_outputs([listing()], {"status": "ok"}, csv_path, audit_path)
    assert blocked
    assert csv_path.exists() is prior_csv and audit_path.exists() is prior_audit
    if prior_csv:
        assert csv_path.read_bytes() == b"original csv\n"
    if prior_audit:
        assert audit_path.read_bytes() == b"original audit\n"
    assert sorted(item.name for item in tmp_path.iterdir()) == sorted(
        (["catalog.csv"] if prior_csv else []) + (["catalog_audit.json"] if prior_audit else []))
