"""Exercise Actions configuration and actual summary/configuration scripts."""
import json
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
    assert "PORTAL_COLLECTION_ENABLED == 'true'" in value["jobs"]["collect"]["if"]
    body = json.dumps(value)
    assert "src.train" not in body and "src.fetch_data" not in body


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
    (tmp_path / "data/daily_collection_audit.json").write_text(json.dumps(report), encoding="utf-8")
    summary = tmp_path / "summary.md"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    exec(compile(script("Summarize collection audit"), "workflow-summary", "exec"), {})
    body = summary.read_text(encoding="utf-8")
    assert "pages_read: 2" in body and "new_listings: 70" in body
    assert "Status: partial" in body and "PRIVATE URL" not in body
