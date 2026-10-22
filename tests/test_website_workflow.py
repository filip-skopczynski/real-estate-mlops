"""Keep the privileged website publisher behind trusted branch/event boundaries."""
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def workflow():
    return yaml.load(
        (ROOT / ".github/workflows/website_feed.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )


def test_publisher_only_follows_trusted_successful_default_branch_collection():
    value = workflow()
    assert set(value["on"]) == {"workflow_run", "workflow_dispatch"}
    trigger = value["on"]["workflow_run"]
    assert trigger == {
        "workflows": ["Warsaw OLX and Otodom catalogue"],
        "types": ["completed"],
        "branches": ["main"],
    }
    job = value["jobs"]["publish"]
    guard = job["if"]
    for requirement in (
        "vars.WEBSITE_ENABLED == 'true'",
        "vars.WEBSITE_URL != ''",
        "github.ref == 'refs/heads/main'",
        "github.event.repository.default_branch == 'main'",
        "github.event_name == 'workflow_dispatch'",
        "github.event_name == 'workflow_run'",
        "github.event.workflow_run.conclusion == 'success'",
        "github.event.workflow_run.head_branch == 'main'",
        "github.event.workflow_run.head_repository.full_name == github.repository",
    ):
        assert requirement in guard
    checkout = next(step for step in job["steps"] if "actions/checkout@" in step.get("uses", ""))
    assert checkout["with"] == {"ref": "main", "persist-credentials": "false"}
    assert value["permissions"] == {"contents": "read", "actions": "read"}
    assert job["concurrency"]["cancel-in-progress"] == "false"
    assert int(job["timeout-minutes"]) <= 20


def test_only_curated_feed_is_uploaded_and_secrets_are_scoped_to_publisher():
    value = workflow()
    job = value["jobs"]["publish"]
    assert "env" not in value and "env" not in job
    publishers = [step for step in job["steps"] if "src.website_feed" in step.get("run", "")]
    assert len(publishers) == 1
    publish = publishers[0]
    assert set(publish["env"]) == {
        "DATABASE_URL", "WEBSITE_URL", "WEBSITE_INGEST_TOKEN", "WEBSITE_SERVICE_TOKEN", "WEBSITE_PROVIDER",
    }
    assert "--download-model" in publish["run"]
    assert publish["env"]["WEBSITE_PROVIDER"] == "${{ vars.WEBSITE_PROVIDER || 'sites' }}"
    assert '--provider "$WEBSITE_PROVIDER"' in publish["run"]
    assert '--publish-url "$WEBSITE_URL"' in publish["run"]
    for step in job["steps"]:
        if step is not publish:
            assert "env" not in step
        assert "src.train" not in step.get("run", "")
        assert "src.build_deals" not in step.get("run", "")
        assert "download-artifact" not in step.get("uses", "")
    uploads = [step for step in job["steps"] if "actions/upload-artifact@" in step.get("uses", "")]
    assert len(uploads) == 1
    assert uploads[0]["with"]["path"] == "data/website/deals.json"
    assert int(uploads[0]["with"]["retention-days"]) <= 7
