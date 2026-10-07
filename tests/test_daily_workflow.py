"""Exercise Actions configuration and actual summary/configuration scripts."""
import json
import datetime as datetime_module
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow():
    return yaml.load((ROOT / ".github/workflows/daily_portals.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def script(step_name):
    step = next(item for item in workflow()["jobs"]["collect"]["steps"] if item.get("name") == step_name)
    return step["run"].split("python - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]


def test_schedule_is_warsaw_daily_and_source_writes_are_separate_from_training():
    value = workflow()
    assert value["on"]["schedule"] == [{"cron": "15 6 * * *", "timezone": "Europe/Warsaw"}]
    assert value["on"]["workflow_dispatch"]["inputs"]["save_db"]["default"] == "false"
    assert value["on"]["workflow_dispatch"]["inputs"]["mode"]["default"] == "daily"
    assert "PORTAL_COLLECTION_ENABLED == 'true'" in value["jobs"]["collect"]["if"]
    body = json.dumps(value)
    assert "src.train" not in body and "src.fetch_data" not in body
    assert value["jobs"]["collect"]["concurrency"]["group"] == "daily-portal-collection"


@pytest.mark.parametrize("event,manual_mode,date,save,expected", [
    ("schedule", "daily", "2026-10-11T04:15:00", "true", "refresh"),
    ("schedule", "daily", "2026-10-12T04:15:00", "true", "daily"),
    ("workflow_dispatch", "bootstrap", "2026-10-11T04:15:00", "true", "bootstrap"),
    ("workflow_dispatch", "daily", "2026-10-11T04:15:00", "false", "daily"),
])
def test_actual_launch_script_selects_warsaw_mode_and_explicit_writes(monkeypatch, event, manual_mode, date, save, expected):
    class Clock(datetime):
        @classmethod
        def now(cls, tz):
            assert str(tz) == "Europe/Warsaw"
            return datetime.fromisoformat(date).replace(tzinfo=tz)

    calls = []
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setenv("CATALOG_MODE", manual_mode)
    monkeypatch.setenv("SAVE_TO_DB", save)
    monkeypatch.setattr(subprocess, "call", lambda command: calls.append(command) or 0)
    monkeypatch.setattr(datetime_module, "datetime", Clock)
    with pytest.raises(SystemExit) as outcome:
        exec(compile(script("Collect Warsaw catalogue and update observations"), "workflow-launch", "exec"), {})
    assert outcome.value.code == 0
    assert calls == [[sys.executable, "-m", "src.catalog_pipeline", "--mode", expected] + (["--save-db"] if save == "true" else [])]


@pytest.mark.parametrize("save,secret,raises", [("true", "", True), ("true", "private-url", False), ("false", "", False)])
def test_database_secret_is_required_only_for_requested_writes(monkeypatch, save, secret, raises):
    monkeypatch.setenv("SAVE_TO_DB", save)
    monkeypatch.setenv("DATABASE_URL", secret)
    code = compile(script("Validate database configuration when requested"), "workflow-config", "exec")
    if raises:
        with pytest.raises(SystemExit):
            exec(code, {})
    else:
        exec(code, {})


def test_summary_reports_partial_capture_counts_without_printing_raw_errors(monkeypatch, tmp_path):
    (tmp_path / "data").mkdir()
    report = {"status": "partial", "sources": {"olx": {"source": "www.olx.pl", "status": "partial",
        "pages_read": 2, "exported_listings": 80, "error": "PRIVATE URL",
        "database_statistics": {"new_listings": 70, "existing_listings": 10, "observations_inserted": 80}}}}
    report["sources"]["olx"]["checkpoint"] = {"next_page": 3, "private": "PRIVATE CHECKPOINT"}
    report["sources"]["olx"]["completed"] = False
    report["sources"]["olx"]["executed_mode"] = "bootstrap"
    report["sources"]["olx"]["catalogue_statistics"] = {"seen_listings": 80, "new_catalog_listings": 75, "catalog_rows_upserted": 80}
    report["sources"]["olx"]["phases"] = [{"phase": "bootstrap", "audit": {"pages_read": 2, "requests": 3,
        "checkpoint": {"next_page": 3, "private": "PRIVATE CHECKPOINT"}}}]
    (tmp_path / "data/catalog_audit.json").write_text(json.dumps(report), encoding="utf-8")
    summary = tmp_path / "summary.md"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    exec(compile(script("Summarize collection audit"), "workflow-summary", "exec"), {})
    body = summary.read_text(encoding="utf-8")
    assert "pages_read: 2" in body and "new_listings: 70" in body
    assert "Status: partial" in body and "PRIVATE URL" not in body
    assert "Next traversal page: 3" in body and "Advertised traversal completed: false" in body
    assert "PRIVATE CHECKPOINT" not in body
    assert "new_catalog_listings: 75" in body and "Mode: bootstrap" in body
    assert "Phase: bootstrap" in body and "requests: 3" in body


def test_summary_handles_missing_audit_and_missing_report(monkeypatch, tmp_path):
    summary = tmp_path / "summary.md"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    code = compile(script("Summarize collection audit"), "workflow-summary", "exec")
    exec(code, {})
    assert "No audit file was produced" in summary.read_text(encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data/catalog_audit.json").write_text(json.dumps({"sources": {
        "PRIVATE SOURCE": {"status": "failed", "audit": None, "database_statistics": None}}}), encoding="utf-8")
    summary.unlink()
    exec(code, {})
    body = summary.read_text(encoding="utf-8")
    assert "Unknown source" in body and "PRIVATE SOURCE" not in body
