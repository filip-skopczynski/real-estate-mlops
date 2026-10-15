"""Build experimental rankings from synthetic snapshots, never production data."""
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import json
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import joblib
import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal
import pytest
from sklearn.dummy import DummyRegressor
from sqlalchemy import text

from src import build_deals, database, show_deals, train


NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


def listing(identity="1", source="www.olx.pl", **changes):
    value = dict(source=source, listing_id=identity,
        url=(f"https://www.olx.pl/d/oferta/apartment-ID{identity}.html" if source == "www.olx.pl"
             else f"https://www.otodom.pl/pl/oferta/apartment-ID{identity}"),
        city="Warszawa", district="Mokotów", price_pln=800_000,
        area_m2=50.0, rooms=2, rooms_min=None, floor=2, build_year=2001,
        latitude=None, longitude=None, distance_km=None, published_at=None,
        observed_at=NOW - timedelta(days=1))
    value.update(changes)
    return value


def snapshots():
    observations, catalog = [], []
    for day in range(4):
        for position in range(12):
            number = day * 12 + position + 1
            source = "www.olx.pl" if number % 2 else "www.otodom.pl"
            initial = listing(str(number), source, price_pln=700_000 + 8000 * number,
                area_m2=45 + 2 * position, rooms=2 + position % 2,
                observed_at=NOW - timedelta(days=6 - day))
            observations.append(initial)
            catalog.append(dict(initial, price_pln=600_000 + 1000 * number,
                observed_at=NOW - timedelta(days=1)))
    return pd.DataFrame(observations), pd.DataFrame(catalog)


def identities(frame):
    return set(frame[["source", "listing_id"]].astype(str).itertuples(index=False, name=None))


@pytest.fixture
def fake_training(monkeypatch):
    calls = []
    def fit(frame, *, split="temporal", seed=42, n_estimators=500):
        calls.append((frame.copy(deep=True), dict(split=split, seed=seed, n_estimators=n_estimators)))
        training, validation, test, provenance = train.split_data(frame, mode=split, seed=seed)
        preprocessing = train.build_preprocessor()
        transformed = preprocessing.fit_transform(training[train.FEATURES])
        model = DummyRegressor(strategy="constant", constant=1_000_000).fit(transformed, training[train.TARGET])
        metadata = dict(split=provenance, preprocessing_fit_subset="train_only",
            early_stopping_subset="validation_only", model_fit_subset="train_only; no refit after evaluation",
            target_interpretation="asking_price; not market value or transaction price")
        artifact = dict(preprocessor=preprocessing, model=model, features=train.FEATURES.copy(), metadata=metadata)
        metrics = dict(xgboost=dict(mae_pln=100_000.0, rmse_pln=120_000.0, r2=0.2),
            baseline_median=dict(mae_pln=200_000.0, rmse_pln=220_000.0, r2=0.0),
            validation=dict(xgboost=dict(mae_pln=90_000.0), baseline_median=dict(mae_pln=190_000.0)),
            split=provenance, metadata=metadata, primary_metrics_subset="test")
        return artifact, metrics
    monkeypatch.setattr(build_deals, "train_model", fit)
    return calls


def build(observations=None, catalog=None, **settings):
    if observations is None:
        observations, catalog = snapshots()
    return build_deals.build_ranking(observations, catalog, now=NOW, **settings)


def test_build_preserves_raw_inputs_uses_temporal_split_and_excludes_training_candidates(fake_training):
    observations, catalog = snapshots()
    history_before, catalog_before = observations.copy(deep=True), catalog.copy(deep=True)
    bundle = build(observations, catalog, n_estimators=8)
    assert_frame_equal(observations, history_before)
    assert_frame_equal(catalog, catalog_before)
    assert len(fake_training) == 1 and fake_training[0][1]["split"] == "temporal"
    assert fake_training[0][1]["n_estimators"] == 8
    assert len(bundle["training"]) == len(bundle["latest"]) == 48
    report = bundle["report"]
    assert report["experimental"] is True and report["cross_source_property_matching"] is False
    assert pd.Timestamp(report["capture_time"]) == pd.Timestamp(NOW)
    assert report["baseline_comparison"]
    provenance = bundle["metrics"]["split"]
    trained = {tuple(key) for key in provenance["train"]["listing_keys"]}
    assert trained.isdisjoint(identities(bundle["deals"]))
    assert not bundle["deals"]["seen_in_training"].any()
    assert set(bundle["deals"]["partition"]) <= {"test", "unseen"}
    assert len(bundle["scored"]) == 48 and len(bundle["deals"]) > 0
    held_out = bundle["scored"].loc[bundle["scored"]["partition"].isin(["test", "unseen"])]
    checked, _ = show_deals.select_deals(held_out, top=50, now=NOW)
    assert list(bundle["deals"]["listing_id"]) == list(checked["listing_id"])
    json.dumps(report, allow_nan=False)


def test_legitimate_repeated_price_history_is_not_a_duplicate_quality_failure(fake_training):
    observations, catalog = snapshots()
    later = observations.copy(deep=True)
    later["observed_at"] = later["observed_at"] + pd.Timedelta(hours=12)
    later["price_pln"] = later["price_pln"] - 20_000
    repeated = pd.concat([later, observations], ignore_index=True)
    bundle = build(repeated, catalog)
    assert len(bundle["training"]) == 48
    actual = bundle["training"].set_index(["source", "listing_id"])["price_pln"].sort_index()
    normalized = observations.astype({"source": "string", "listing_id": "string"})
    expected = normalized.set_index(["source", "listing_id"])["price_pln"].astype(float).sort_index()
    pd.testing.assert_series_equal(actual, expected, check_names=False)


@pytest.mark.parametrize("bad", [dict(price_pln=1000), dict(price_pln=0), dict(rooms=None), dict(area_m2=None)])
def test_bad_earliest_raw_observation_never_falls_forward_to_a_later_valid_row(fake_training, bad):
    observations, catalog = snapshots()
    bad_first = observations.iloc[0].to_dict()
    original = dict(bad_first)
    bad_first.update(bad)
    observations.iloc[0] = pd.Series(bad_first)
    later_good = dict(original, observed_at=original["observed_at"] + timedelta(hours=1))
    raw = pd.concat([pd.DataFrame([later_good]), observations], ignore_index=True)
    bundle = build(raw, catalog)
    assert (original["source"], original["listing_id"]) not in identities(bundle["training"])
    assert len(bundle["training"]) == 47


@pytest.mark.parametrize("changes", [dict(price_pln=750_000), dict(area_m2=51), dict(rooms=3),
    dict(floor=8), dict(build_year=2002), dict(latitude=52.2), dict(longitude=21.1), dict(distance_km=3)])
def test_conflicting_same_time_earliest_observations_exclude_training_identity(fake_training, changes):
    observations, catalog = snapshots()
    original = observations.iloc[0].to_dict()
    conflict = dict(original, **changes)
    raw = pd.DataFrame([*observations.to_dict("records"), conflict])
    bundle = build(raw, catalog)
    assert (original["source"], original["listing_id"]) not in identities(bundle["training"])
    assert len(bundle["training"]) == 47


def test_same_earliest_observation_replayed_identically_is_not_a_conflict(fake_training):
    observations, catalog = snapshots()
    raw = pd.concat([observations, observations.iloc[[0]]], ignore_index=True)
    bundle = build(raw, catalog)
    assert len(bundle["training"]) == 48


@pytest.mark.parametrize("changes", [dict(price_pln=None), dict(price_pln=1000),
    dict(area_m2=None), dict(rooms=None), dict(observed_at=NOW - timedelta(days=8)),
    dict(observed_at=NOW + timedelta(seconds=1))])
def test_latest_sparse_catalogue_is_authoritative_and_never_uses_old_history_price(fake_training, changes):
    observations, catalog = snapshots()
    target = catalog.iloc[-1].to_dict()
    catalog.iloc[-1] = pd.Series(dict(target, **changes))
    bundle = build(observations, catalog)
    key = (target["source"], target["listing_id"])
    assert key in identities(bundle["training"])
    assert key not in identities(bundle["latest"])
    assert key not in identities(bundle["scored"])
    assert key not in identities(bundle["deals"])


def test_missing_newest_catalogue_price_does_not_fall_back_to_older_catalogue_row(fake_training):
    observations, catalog = snapshots()
    target = catalog.iloc[-1].to_dict()
    older = dict(target, price_pln=400_000, observed_at=NOW - timedelta(days=2))
    catalog.iloc[-1] = pd.Series(dict(target, price_pln=None))
    catalog = pd.DataFrame([older, *catalog.to_dict("records")])
    bundle = build(observations, catalog)
    assert (target["source"], target["listing_id"]) not in identities(bundle["latest"])


def test_offline_nonportal_rows_are_scoped_out_without_entering_training_or_ranking(fake_training):
    observations, catalog = snapshots()
    pilot = listing("999", source="developer.example", url="https://developer.example/apartment/999")
    observations = pd.DataFrame([*observations.to_dict("records"), pilot])
    catalog = pd.DataFrame([*catalog.to_dict("records"), pilot])
    bundle = build(observations, catalog)
    assert len(bundle["training"]) == len(bundle["latest"]) == 48
    assert identities(bundle["training"]) <= identities(snapshots()[0])
    assert bundle["report"]["raw_counts"]["observations"] == 49
    assert bundle["report"]["raw_counts"]["scoped_observations"] == 48


def test_fresh_catalogue_identity_without_history_can_be_scored_as_unseen(fake_training):
    observations, catalog = snapshots()
    fresh = listing("00999", price_pln=500_000, observed_at=NOW - timedelta(hours=1))
    catalog = pd.concat([catalog, pd.DataFrame([fresh])], ignore_index=True)
    bundle = build(observations, catalog, top=1)
    assert list(bundle["deals"]["listing_id"]) == ["00999"]
    assert bundle["deals"].iloc[0]["partition"] == "unseen"
    assert not bundle["deals"].iloc[0]["seen_in_training"]
    assert (fresh["source"], fresh["listing_id"]) not in identities(bundle["training"])


def test_high_gap_validation_candidates_used_for_early_stopping_are_not_emitted(fake_training):
    observations, catalog = snapshots()
    _, _, _, provenance = train.split_data(observations)
    validation_key = tuple(provenance["validation"]["listing_keys"][0])
    mask = catalog["source"].eq(validation_key[0]) & catalog["listing_id"].eq(validation_key[1])
    catalog.loc[mask, "price_pln"] = 200_000
    bundle = build(observations, catalog)
    candidate = bundle["scored"].loc[bundle["scored"]["source"].eq(validation_key[0])
        & bundle["scored"]["listing_id"].eq(validation_key[1])]
    assert len(candidate) == 1 and candidate.iloc[0]["partition"] == "validation"
    assert candidate.iloc[0]["discount"] == pytest.approx(0.8)
    assert validation_key not in identities(bundle["deals"])
    assert len(bundle["deals"]) > 0 and set(bundle["deals"]["partition"]) <= {"test", "unseen"}


def test_no_candidates_is_valid_and_does_not_invent_or_promote_training_rows(fake_training):
    observations, catalog = snapshots()
    catalog["price_pln"] = 1_000_000
    bundle = build(observations, catalog)
    assert len(bundle["training"]) == 48 and len(bundle["scored"]) == 48
    assert bundle["deals"].empty
    assert bundle["report"]["deal_selection"]["eligible_listings"] == 0


def test_model_worse_than_baseline_is_reported_truthfully_without_split_fallback(monkeypatch, fake_training):
    actual = build_deals.train_model
    def weaker(*arguments, **settings):
        artifact, metrics = actual(*arguments, **settings)
        metrics["xgboost"]["mae_pln"] = 300_000.0
        return artifact, metrics
    monkeypatch.setattr(build_deals, "train_model", weaker)
    bundle = build()
    comparison = bundle["report"]["baseline_comparison"]
    assert comparison["xgboost_has_lower_test_mae"] is False
    assert comparison["mae_improvement_pln"] == -100_000
    assert fake_training[0][1]["split"] == "temporal"
    assert bundle["report"]["experimental"] is True


def test_insufficient_temporal_history_does_not_fall_back_to_group_split(fake_training):
    observations, catalog = snapshots()
    observations["observed_at"] = NOW - timedelta(days=1)
    with pytest.raises(train.InsufficientDataError):
        build(observations, catalog)
    assert fake_training[0][1]["split"] == "temporal"


def test_one_real_tiny_xgboost_integration_preserves_evaluated_split_and_candidate_provenance():
    observations, catalog = snapshots()
    bundle = build_deals.build_ranking(observations, catalog, now=NOW, n_estimators=8)
    metadata = bundle["artifact"]["metadata"]
    assert metadata["early_stopping_subset"] == "validation_only"
    assert metadata["preprocessing_fit_subset"] == "train_only"
    assert metadata["split"]["type"] == "temporal"
    assert metadata["split"]["listing_identity_overlap"] == 0
    assert bundle["metrics"]["primary_metrics_subset"] == "test"
    assert np.isfinite(bundle["metrics"]["xgboost"]["mae_pln"])
    assert np.isfinite(bundle["metrics"]["baseline_median"]["mae_pln"])
    assert set(bundle["deals"]["partition"]) <= {"test", "unseen"}
    assert not bundle["deals"]["seen_in_training"].any()


def test_save_bundle_writes_raw_prepared_model_metrics_and_ranking_snapshots(tmp_path, fake_training):
    observations, catalog = snapshots()
    bundle = build(observations, catalog)
    data_dir, models_dir = tmp_path / "data", tmp_path / "models"
    build_deals.save_bundle(bundle, data_dir=data_dir, models_dir=models_dir)
    assert {path.name for path in data_dir.iterdir()} == {"observations.csv", "catalog_latest.csv",
        "training.csv", "latest.csv", "scored_listings.csv", "deals.csv", "build_deals_report.json", "catalog_snapshot.json", "catalog_latest.json"}
    assert {path.name for path in models_dir.iterdir()} == {"model.joblib", "metrics.json"}
    saved_deals = pd.read_csv(data_dir / "deals.csv", dtype={"source": "string", "listing_id": "string"})
    assert identities(saved_deals) == identities(bundle["deals"])
    assert len(pd.read_csv(data_dir / "observations.csv")) == len(observations)
    assert len(pd.read_csv(data_dir / "catalog_latest.csv")) == len(catalog)
    restored = joblib.load(models_dir / "model.joblib")
    assert restored["metadata"]["split"] == bundle["artifact"]["metadata"]["split"]
    for path in (data_dir / "build_deals_report.json", data_dir / "catalog_snapshot.json", models_dir / "metrics.json"):
        json.dumps(json.loads(path.read_text(encoding="utf-8")), allow_nan=False)
    assert (data_dir / "catalog_latest.json").read_bytes() == (data_dir / "catalog_snapshot.json").read_bytes()
    report = json.loads((data_dir / "build_deals_report.json").read_text(encoding="utf-8"))
    model_hash = hashlib.sha256((models_dir / "model.joblib").read_bytes()).hexdigest()
    assert report["provenance"]["model_sha256"] == model_hash
    assert json.loads((models_dir / "metrics.json").read_text(encoding="utf-8"))["model_file_sha256"] == model_hash
    for key, name in (("observations_csv_sha256", "observations.csv"), ("catalog_latest_csv_sha256", "catalog_latest.csv"),
        ("training_csv_sha256", "training.csv"), ("latest_csv_sha256", "latest.csv"),
        ("scored_listings_csv_sha256", "scored_listings.csv"), ("deals_csv_sha256", "deals.csv")):
        assert report["provenance"][key] == hashlib.sha256((data_dir / name).read_bytes()).hexdigest()


def test_staging_failure_preserves_all_previous_artifacts(monkeypatch, tmp_path, fake_training):
    bundle = build()
    data_dir, models_dir = tmp_path / "data", tmp_path / "models"
    build_deals.save_bundle(bundle, data_dir=data_dir, models_dir=models_dir)
    before = {path: path.read_bytes() for directory in (data_dir, models_dir) for path in directory.iterdir()}
    monkeypatch.setattr(build_deals.joblib, "dump", Mock(side_effect=OSError("Synthetic staging failure")))
    with pytest.raises(OSError):
        build_deals.save_bundle(bundle, data_dir=data_dir, models_dir=models_dir)
    assert {path: path.read_bytes() for directory in (data_dir, models_dir) for path in directory.iterdir()} == before


def test_late_replacement_failure_restores_coherent_model_metrics_and_csv_bundle(monkeypatch, tmp_path, fake_training):
    bundle = build()
    data_dir, models_dir = tmp_path / "data", tmp_path / "models"
    build_deals.save_bundle(bundle, data_dir=data_dir, models_dir=models_dir)
    before = {path: path.read_bytes() for directory in (data_dir, models_dir) for path in directory.iterdir()}
    observations, catalog = snapshots()
    observations["price_pln"] += 20_000
    catalog["price_pln"] -= 10_000
    changed_bundle = build(observations, catalog)
    assert changed_bundle["report"]["provenance"]["training_csv_sha256"] != bundle["report"]["provenance"]["training_csv_sha256"]
    real_replace = build_deals.os.replace
    failed = False
    def fail_once(source, destination):
        nonlocal failed
        if Path(destination) == models_dir / "metrics.json" and not failed:
            failed = True
            raise OSError("Synthetic late replacement failure")
        return real_replace(source, destination)
    monkeypatch.setattr(build_deals.os, "replace", fail_once)
    with pytest.raises(OSError):
        build_deals.save_bundle(changed_bundle, data_dir=data_dir, models_dir=models_dir)
    assert failed
    assert {path: path.read_bytes() for directory in (data_dir, models_dir) for path in directory.iterdir()} == before


def test_offline_cli_requires_both_snapshots_and_does_not_open_database(monkeypatch, tmp_path):
    observations, _ = snapshots()
    path = tmp_path / "observations.csv"
    observations.to_csv(path, index=False)
    forbidden = Mock(side_effect=AssertionError("Incomplete offline input must not read production database"))
    monkeypatch.setattr(build_deals, "read_snapshot", forbidden)
    result = build_deals.main(["--observations", str(path), "--data-dir", str(tmp_path / "data"), "--models-dir", str(tmp_path / "models")])
    assert result != 0
    forbidden.assert_not_called()
    assert not (tmp_path / "data").exists() and not (tmp_path / "models").exists()


@pytest.mark.parametrize("collision", ["history", "catalog"])
def test_offline_cli_rejects_input_matching_output_destination_before_training(monkeypatch, tmp_path, collision):
    observations, catalog = snapshots()
    data_dir, models_dir = tmp_path / "data", tmp_path / "models"
    data_dir.mkdir()
    history_path = data_dir / "observations.csv" if collision == "history" else tmp_path / "history.csv"
    catalog_path = data_dir / "catalog_latest.csv" if collision == "catalog" else tmp_path / "catalog.csv"
    observations.to_csv(history_path, index=False)
    catalog.to_csv(catalog_path, index=False)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    forbidden = Mock(side_effect=AssertionError("Input/output collision must fail before training/database"))
    monkeypatch.setattr(build_deals, "build_ranking", forbidden)
    monkeypatch.setattr(build_deals, "read_snapshot", forbidden)
    assert build_deals.main(["--observations", str(history_path), "--catalog", str(catalog_path),
        "--data-dir", str(data_dir), "--models-dir", str(models_dir)]) != 0
    forbidden.assert_not_called()
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
    assert not models_dir.exists()


def test_offline_cli_uses_both_csv_snapshots_preserves_inputs_and_produces_real_output_files(monkeypatch, tmp_path, fake_training):
    observations, catalog = snapshots()
    history_path, catalog_path = tmp_path / "history.csv", tmp_path / "catalog.csv"
    observations.to_csv(history_path, index=False)
    catalog.to_csv(catalog_path, index=False)
    before = (history_path.read_bytes(), catalog_path.read_bytes())
    forbidden = Mock(side_effect=AssertionError("Offline CLI must not open database"))
    monkeypatch.setattr(build_deals, "read_snapshot", forbidden)
    actual = build_deals.build_ranking
    def deterministic(history, latest, **settings):
        settings["now"] = NOW
        return actual(history, latest, **settings)
    monkeypatch.setattr(build_deals, "build_ranking", deterministic)
    data_dir, models_dir = tmp_path / "data", tmp_path / "models"
    result = build_deals.main(["--observations", str(history_path), "--catalog", str(catalog_path),
        "--data-dir", str(data_dir), "--models-dir", str(models_dir)])
    assert result == 0
    forbidden.assert_not_called()
    assert (history_path.read_bytes(), catalog_path.read_bytes()) == before
    assert (data_dir / "deals.csv").is_file() and (models_dir / "model.joblib").is_file()


def test_failed_training_cli_preserves_preexisting_model_and_deals(monkeypatch, tmp_path):
    observations, catalog = snapshots()
    observations["observed_at"] = NOW - timedelta(days=1)
    history_path, catalog_path = tmp_path / "history.csv", tmp_path / "catalog.csv"
    observations.to_csv(history_path, index=False)
    catalog.to_csv(catalog_path, index=False)
    data_dir, models_dir = tmp_path / "data", tmp_path / "models"
    data_dir.mkdir()
    models_dir.mkdir()
    (data_dir / "deals.csv").write_bytes(b"Previously reviewed candidates\n")
    (models_dir / "model.joblib").write_bytes(b"Previously evaluated model\n")
    monkeypatch.setattr(build_deals, "read_snapshot", Mock(side_effect=AssertionError("Must stay offline")))
    result = build_deals.main(["--observations", str(history_path), "--catalog", str(catalog_path),
        "--data-dir", str(data_dir), "--models-dir", str(models_dir)])
    assert result != 0
    assert (data_dir / "deals.csv").read_bytes() == b"Previously reviewed candidates\n"
    assert (models_dir / "model.joblib").read_bytes() == b"Previously evaluated model\n"
    assert {path.name for path in data_dir.iterdir()} == {"deals.csv"}
    assert {path.name for path in models_dir.iterdir()} == {"model.joblib"}


class FakePostgresConnection:
    def __init__(self, *, readonly="on", isolation="repeatable read"):
        self.readonly, self.isolation = readonly, isolation
        self.options, self.statements = {}, []
        self.in_transaction = False

    def __enter__(self):
        return self

    def __exit__(self, *error):
        return False

    def execution_options(self, **options):
        self.options.update(options)
        return self

    @contextmanager
    def begin(self):
        self.in_transaction = True
        try:
            yield self
        finally:
            self.in_transaction = False

    def execute(self, statement):
        assert self.in_transaction
        query = str(statement).strip().upper()
        self.statements.append(query)
        values = {"SET TRANSACTION READ ONLY": None, "SHOW TRANSACTION_READ_ONLY": self.readonly,
            "SHOW TRANSACTION_ISOLATION": self.isolation, "SELECT CURRENT_TIMESTAMP": NOW}
        assert query in values, "Snapshot unexpectedly attempted a different SQL operation"
        return SimpleNamespace(scalar_one=lambda: values[query])


def mocked_postgres(monkeypatch, observations=None, catalog=None, *, readonly="on", isolation="repeatable read"):
    connection = FakePostgresConnection(readonly=readonly, isolation=isolation)
    engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), connect=lambda: connection, dispose=Mock())
    captured_queries = []
    def read(query, actual_connection, *, params):
        assert actual_connection is connection and connection.in_transaction
        statement = str(query)
        captured_queries.append((statement, dict(params)))
        assert "WHERE source IN (:source_olx, :source_otodom)" in statement
        assert params["source_olx"] == "www.olx.pl" and params["source_otodom"] == "www.otodom.pl"
        if "public.listing_observations" in statement:
            return (observations if observations is not None else pd.DataFrame([listing("00123")])).copy(deep=True)
        assert "public.listing_catalog" in statement and "last_seen_at AS observed_at" in statement
        return (catalog if catalog is not None else pd.DataFrame([listing("00123", observed_at=NOW - timedelta(hours=1))])).copy(deep=True)
    monkeypatch.setattr(build_deals.pd, "read_sql_query", read)
    return engine, connection, captured_queries


def test_read_snapshot_requires_confirmed_readonly_repeatable_read_and_scopes_both_queries(monkeypatch):
    engine, connection, queries = mocked_postgres(monkeypatch)
    observations, catalog, metadata = build_deals.read_snapshot(engine=engine)
    assert identities(observations) == identities(catalog) == {("www.olx.pl", "00123")}
    assert pd.Timestamp(catalog.iloc[0]["observed_at"]) == pd.Timestamp(NOW - timedelta(hours=1))
    assert connection.options == {"isolation_level": "REPEATABLE READ", "postgresql_readonly": True}
    assert metadata["transaction_read_only"] is True and metadata["consistent_snapshot"] is True
    assert pd.Timestamp(metadata["capture_time"]) == pd.Timestamp(NOW)
    assert len(queries) == 2
    assert queries[0][1]["limit"] == build_deals.MAX_OBSERVATIONS + 1
    assert queries[1][1]["limit"] == build_deals.MAX_CATALOG + 1
    assert not connection.in_transaction
    engine.dispose.assert_not_called()


@pytest.mark.parametrize("readonly,isolation", [("off", "repeatable read"), ("on", "read committed")])
def test_snapshot_fails_when_database_does_not_confirm_required_transaction(monkeypatch, readonly, isolation):
    engine, _, queries = mocked_postgres(monkeypatch, readonly=readonly, isolation=isolation)
    with pytest.raises(ValueError):
        build_deals.read_snapshot(engine=engine)
    assert queries == []


@pytest.mark.parametrize("which", ["history", "catalog"])
def test_snapshot_hard_limit_fails_instead_of_returning_truncated_training_data(monkeypatch, which):
    observations = pd.DataFrame([listing(str(index)) for index in range(1, 4)])
    catalog = observations.copy(deep=True)
    if which == "history":
        monkeypatch.setattr(build_deals, "MAX_OBSERVATIONS", 2)
    else:
        monkeypatch.setattr(build_deals, "MAX_CATALOG", 2)
    engine, _, _ = mocked_postgres(monkeypatch, observations, catalog)
    with pytest.raises(ValueError):
        build_deals.read_snapshot(engine=engine)


def test_snapshot_errors_do_not_expose_database_exception_text_and_owned_engine_is_disposed(monkeypatch):
    engine, _, _ = mocked_postgres(monkeypatch)
    monkeypatch.setattr(database, "get_engine", lambda: engine)
    monkeypatch.setattr(build_deals.pd, "read_sql_query", Mock(side_effect=RuntimeError("PRIVATE DATABASE PASSWORD")))
    with pytest.raises(ValueError) as error:
        build_deals.read_snapshot()
    assert "PRIVATE" not in str(error.value)
    engine.dispose.assert_called_once()


def test_non_postgres_engine_is_not_used_or_disposed_by_snapshot_reader():
    engine = database.get_engine("sqlite:///:memory:")
    try:
        with pytest.raises(ValueError):
            build_deals.read_snapshot(engine=engine)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT 1")) == 1
    finally:
        engine.dispose()
