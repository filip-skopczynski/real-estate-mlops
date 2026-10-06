"""Eligibility boundaries from a complete catalogue through candidate scoring."""
from datetime import timedelta
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy import inspect

from src import fetch_bemovo
from src.availability import read_availability, save_inventory_snapshot
from src.database import init_db, read_current_listings, read_observations
from src.preprocess import clean_listings, filter_available_listings
from src.train import FEATURES, score_listings
from tests.test_bemovo_collector import FakeSession, OBSERVED_AT, SNAPSHOT_DATE, feature, price, response
from tests.test_ml import listing_frame


def test_candidate_selection_uses_status_clock_and_never_resurrects_old_available_row():
    early = listing_frame(1)
    early["availability_status"] = "available"
    early["availability_observed_at"] = early["observed_at"]
    later = early.copy()
    later["availability_status"] = "sold"
    later["availability_observed_at"] = pd.Timestamp("2026-09-03", tz="UTC")
    raw = pd.concat([later, early], ignore_index=True)
    latest = clean_listings(raw, keep="latest")
    assert latest["availability_status"].tolist() == ["sold"]
    assert filter_available_listings(latest).empty
    # Current availability must not remove a historical asking-price target.
    assert len(clean_listings(raw, keep="earliest")) == 1


@pytest.mark.parametrize("status", ["sold", "reserved", "missing"])
def test_unavailable_candidates_do_not_reach_the_prediction_model(status):
    raw = listing_frame(2)
    raw["availability_status"] = status
    preprocessor, model = Mock(), Mock()
    result = score_listings(raw, {"preprocessor": preprocessor, "model": model, "features": FEATURES})
    assert result.empty
    assert "is_deal" in result
    preprocessor.transform.assert_not_called()
    model.predict.assert_not_called()


def test_only_available_and_legacy_untracked_units_can_be_scored():
    raw = listing_frame(5)
    raw["availability_status"] = ["sold", "reserved", "missing", "available", None]
    preprocessor = Mock()
    preprocessor.transform.side_effect = lambda frame: np.ones((len(frame), 1))
    model = Mock()
    model.predict.return_value = np.array([1_000_000, 1_000_000])
    result = score_listings(raw, {"preprocessor": preprocessor, "model": model, "features": FEATURES})
    assert result["listing_id"].tolist() == ["3", "4"]
    assert len(preprocessor.transform.call_args.args[0]) == 2


def test_unknown_explicit_status_fails_instead_of_becoming_a_deal():
    raw = listing_frame(1)
    raw["availability_status"] = "unknown_new_source_value"
    with pytest.raises(ValueError, match="availability"):
        score_listings(raw, {})


def test_full_zero_available_catalogue_is_valid_but_empty_features_are_not():
    records, report = fetch_bemovo.combine_bemovo_records(
        [], [feature(status="sold"), feature("A0/02", status="reserved")],
        observed_at=OBSERVED_AT, snapshot_date=SNAPSHOT_DATE,
    )
    assert records == []
    assert report["inventory"]["complete"] is True
    assert [row["status"] for row in report["inventory"]["apartments"]] == ["sold", "reserved"]
    with pytest.raises(ValueError):
        fetch_bemovo.combine_bemovo_records([], [], observed_at=OBSERVED_AT, snapshot_date=SNAPSHOT_DATE)


def test_nonavailable_catalogue_still_validates_project_identity():
    with pytest.raises(ValueError, match="unexpected"):
        fetch_bemovo.combine_bemovo_records(
            [], [feature(status="sold", city="Kraków")],
            observed_at=OBSERVED_AT, snapshot_date=SNAPSHOT_DATE,
        )


def test_sale_removes_candidate_without_creating_a_transaction_price(tmp_path):
    engine = create_engine("sqlite:///" + (tmp_path / "eligibility.db").as_posix())
    init_db(engine)
    records, report = fetch_bemovo.combine_bemovo_records(
        [price()], [feature()], observed_at=OBSERVED_AT, snapshot_date=SNAPSHOT_DATE,
    )
    options = {key: report["inventory"][key] for key in ["source", "scope", "listing_id_prefix", "complete"]}
    save_inventory_snapshot(engine, records, report["inventory"]["apartments"], observed_at=OBSERVED_AT, **options)
    sold_time = OBSERVED_AT + timedelta(days=1)
    save_inventory_snapshot(engine, [], [{"listing_id": records[0]["listing_id"], "status": "sold"}],
                            observed_at=sold_time, **options)
    current = read_current_listings(engine)
    history = read_observations(engine)
    assert len(history) == 1
    assert history.iloc[0]["price_pln"] == records[0]["price_pln"]
    assert current.iloc[0]["observed_at"] == pd.Timestamp(OBSERVED_AT)
    assert current.iloc[0]["availability_observed_at"] == pd.Timestamp(sold_time)
    assert len(clean_listings(history, keep="earliest")) == 1
    assert read_current_listings(engine, available_only=True).empty
    assert score_listings(current, {}).empty
    engine.dispose()


def test_cloud_option_routes_complete_statuses_and_prices_through_one_snapshot(monkeypatch, tmp_path):
    database_path = tmp_path / "collector.db"
    engine = create_engine("sqlite:///" + database_path.as_posix())
    records, report = fetch_bemovo.combine_bemovo_records(
        [price()], [feature(), feature("A0/02", status="sold"), feature("A0/03", status="reserved")],
        observed_at=OBSERVED_AT, snapshot_date=SNAPSHOT_DATE,
    )
    monkeypatch.setattr(fetch_bemovo, "load_dotenv", Mock())
    monkeypatch.setattr(fetch_bemovo, "collect_bemovo", Mock(return_value=(records, report)))
    monkeypatch.setattr("src.database.get_engine", Mock(return_value=engine))
    assert fetch_bemovo.main(["--save-db", "--output", str(tmp_path / "prices.csv"),
                              "--report-output", str(tmp_path / "audit.json")]) == 0
    reading_engine = create_engine("sqlite:///" + database_path.as_posix())
    assert len(read_observations(reading_engine)) == 1
    assert read_availability(reading_engine)["status"].value_counts().to_dict() == {
        "available": 1, "sold": 1, "reserved": 1,
    }
    assert report["database_availability_counts"]["sold"] == 1
    reading_engine.dispose()


def test_availability_mode_reads_only_the_website_and_never_parses_prices(monkeypatch):
    session = FakeSession([response("<html>synthetic catalogue</html>")])
    monkeypatch.setattr(fetch_bemovo, "parse_bemovo_features", Mock(return_value=[feature(), feature("A0/02", status="sold")]))
    price_parser = Mock(side_effect=AssertionError("Availability-only collection must not fetch or parse prices"))
    monkeypatch.setattr(fetch_bemovo, "parse_bemovo_prices", price_parser)
    records, report = fetch_bemovo.collect_bemovo(
        session=session, availability_only=True, delay=0, observed_at=OBSERVED_AT,
    )
    assert records == []
    assert report["inventory"]["prices_complete"] is False
    assert report["capture_date"] == "2026-10-05"
    assert [url for url, _ in session.calls] == [fetch_bemovo.FEATURES_URL]
    assert "price_mismatches" not in report
    price_parser.assert_not_called()


def test_availability_only_cli_saves_statuses_without_overwriting_price_files(monkeypatch, tmp_path):
    database_path = tmp_path / "status_only.db"
    engine = create_engine("sqlite:///" + database_path.as_posix())
    session = FakeSession([response("<html>synthetic catalogue</html>")])
    monkeypatch.setattr(fetch_bemovo, "parse_bemovo_features", Mock(return_value=[feature(), feature("A0/02", status="sold")]))
    collected = fetch_bemovo.collect_bemovo(session=session, delay=0, observed_at=OBSERVED_AT, availability_only=True)
    old_prices = tmp_path / "bemovo.csv"
    old_prices.write_text("previous price snapshot", encoding="utf-8")
    monkeypatch.setattr(fetch_bemovo, "load_dotenv", Mock())
    monkeypatch.setattr(fetch_bemovo, "collect_bemovo", Mock(return_value=collected))
    monkeypatch.setattr(fetch_bemovo, "_write_snapshot", Mock(side_effect=AssertionError("Do not replace price files")))
    monkeypatch.setattr("src.database.get_engine", Mock(return_value=engine))
    status_csv, status_audit = tmp_path / "status.csv", tmp_path / "status_audit.json"
    assert fetch_bemovo.main(["--save-db", "--availability-only", "--output", str(status_csv),
                              "--report-output", str(status_audit)]) == 0
    assert old_prices.read_text(encoding="utf-8") == "previous price snapshot"
    assert pd.read_csv(status_csv)["status"].tolist() == ["available", "sold"]
    reading_engine = create_engine("sqlite:///" + database_path.as_posix())
    assert read_observations(reading_engine).empty
    assert len(read_availability(reading_engine)) == 2
    reading_engine.dispose()


def test_database_module_entrypoint_initializes_all_additive_tables(tmp_path):
    database_path = tmp_path / "entrypoint.db"
    project = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [sys.executable, "-m", "src.database", "init"], cwd=project,
        env={**os.environ, "DATABASE_URL": "sqlite:///" + database_path.as_posix()},
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0
    engine = create_engine("sqlite:///" + database_path.as_posix())
    assert set(inspect(engine).get_table_names()) == {
        "listings", "listing_observations", "inventory_snapshots",
        "listing_availability", "listing_availability_observations",
    }
    engine.dispose()
