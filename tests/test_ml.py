"""Checks for chronology, target leakage, preprocessing and saved-model scoring."""

import joblib
import numpy as np
import pandas as pd
import pytest

from src.preprocess import clean_listings
from src.train import (
    FEATURES, NUMERIC_FEATURES, InsufficientDataError,
    build_preprocessor, score_listings, split_data, train_model,
)


def listing_frame(count=60, single_day=False):
    """Artificial records used only for tests; not evidence of market accuracy."""
    rows = []
    start = pd.Timestamp("2026-09-01", tz="UTC")
    for index in range(count):
        area = 35 + (index % 20) * 3
        rows.append({
            "source": "fixture", "listing_id": str(index),
            "url": f"https://example.invalid/listing/{index}",
            "city": "Warszawa", "district": ["Mokotów", "Wola"][index % 2],
            "price_pln": 200_000 + 12_000 * area + 20_000 * (index % 4),
            "area_m2": area, "rooms": 1 + index % 4,
            "floor": index // 6, "build_year": 2000 + index % 20,
            "latitude": 52.2297 + (index % 10) * 0.002,
            "longitude": 21.0122, "distance_km": np.nan,
            "observed_at": start + pd.Timedelta(days=0 if single_day else index // 6),
            "transaction_type": "sale",
        })
    frame = pd.DataFrame(rows)
    frame[["price_pln", "rooms", "floor"]] = frame[["price_pln", "rooms", "floor"]].astype(float)
    frame["observed_at"] = frame["observed_at"].astype(object)
    return frame


def listing_ids(frame):
    return set(frame[["source", "listing_id"]].itertuples(index=False, name=None))


def test_validation_removes_invalid_required_values_and_rent():
    raw = listing_frame(12)
    raw.loc[0, "price_pln"] = np.inf
    raw.loc[1, "area_m2"] = 0
    raw.loc[2, "rooms"] = 1.5
    raw.loc[3, "rooms"] = np.nan
    raw.loc[4, "city"] = "Kraków"
    raw.loc[5, "observed_at"] = "not-a-date"
    raw.loc[6, "transaction_type"] = "rent"
    raw.loc[7, "city"] = " WARSAW "
    cleaned = clean_listings(raw)
    assert set(cleaned.listing_id) == {"7", "8", "9", "10", "11"}
    assert cleaned.city.eq("Warszawa").all()
    assert str(cleaned.observed_at.dtype) == "datetime64[ns, UTC]"


def test_optional_invalid_numbers_are_imputed_later_and_distance_is_geographic():
    raw = listing_frame(3)
    raw.loc[0, ["latitude", "longitude"]] = [52.2297, 21.0122]
    raw.loc[0, "floor"] = np.inf
    raw.loc[1, "distance_km"] = -1
    raw.loc[2, "latitude"] = 999
    cleaned = clean_listings(raw)
    assert len(cleaned) == 3
    assert cleaned.loc[0, "distance_km"] == pytest.approx(0, abs=1e-9)
    assert cleaned.loc[1, "distance_km"] > 0
    assert np.isnan(cleaned.loc[0, "floor"])
    assert np.isnan(cleaned.loc[2, "latitude"])
    assert np.isnan(cleaned.loc[2, "distance_km"])


def test_utc_conversion_and_dedup_uses_chronology_not_input_order():
    earlier = listing_frame(1)
    earlier.loc[0, "observed_at"] = "2026-09-01T02:00:00+02:00"
    later = earlier.copy()
    later.loc[0, "observed_at"] = "2026-09-04T00:00:00Z"
    later.loc[0, "price_pln"] = 1_100_000
    raw = pd.concat([later, earlier], ignore_index=True)
    first = clean_listings(raw, keep="earliest")
    last = clean_listings(raw, keep="latest")
    assert len(first) == len(last) == 1
    assert first.loc[0, "observed_at"] == pd.Timestamp("2026-09-01T00:00:00Z")
    assert first.loc[0, "price_pln"] == earlier.loc[0, "price_pln"]
    assert last.loc[0, "price_pln"] == 1_100_000


def test_missing_columns_and_invalid_keep_raise_clear_errors():
    with pytest.raises(ValueError, match="price_pln"):
        clean_listings(listing_frame().drop(columns="price_pln"))
    with pytest.raises(ValueError, match="keep"):
        clean_listings(listing_frame(), keep="random")


def test_preprocess_cli_preserves_leading_zero_listing_ids(tmp_path, monkeypatch):
    from src import preprocess

    raw = listing_frame(2)
    raw["listing_id"] = ["001", "1"]
    input_path, training_path, latest_path = (tmp_path / name for name in ["raw.csv", "training.csv", "latest.csv"])
    raw.to_csv(input_path, index=False)
    monkeypatch.setattr("sys.argv", [
        "preprocess", "--input", str(input_path), "--output", str(training_path),
        "--latest-output", str(latest_path),
    ])
    preprocess.main()
    output = pd.read_csv(training_path, dtype={"listing_id": "string"})
    assert output.listing_id.tolist() == ["001", "1"]


def test_train_cli_preserves_leading_zero_ids_in_both_snapshots(tmp_path, monkeypatch):
    from src import train as training_module

    raw = listing_frame(2)
    raw["listing_id"] = ["001", "1"]
    input_path = tmp_path / "data.csv"
    raw.to_csv(input_path, index=False)
    captured = {}

    def capture_training(frame, **kwargs):
        captured["training_ids"] = frame.listing_id.tolist()
        return {}, {}

    def capture_scoring(frame, artifact, threshold):
        captured["latest_ids"] = frame.listing_id.tolist()
        raise RuntimeError("reading complete; avoid writing artifacts in this boundary test")

    monkeypatch.setattr(training_module, "train_model", capture_training)
    monkeypatch.setattr(training_module, "score_listings", capture_scoring)
    monkeypatch.setattr("sys.argv", [
        "train", "--input", str(input_path), "--latest", str(input_path),
        "--discount-threshold", "15",
    ])
    with pytest.raises(RuntimeError, match="reading complete"):
        training_module.main()
    assert captured == {"training_ids": ["001", "1"], "latest_ids": ["001", "1"]}


def test_temporal_split_preserves_whole_days_and_disjoint_listing_identities():
    raw = listing_frame()
    repeated = raw.iloc[:10].copy()
    repeated["observed_at"] = pd.Timestamp("2026-09-30", tz="UTC")
    repeated["price_pln"] += 100_000
    train, validation, test, metadata = split_data(pd.concat([raw, repeated]))
    assert [len(train), len(validation), len(test)] == [36, 12, 12]
    assert train.observed_at.max().floor("D") < validation.observed_at.min().floor("D")
    assert validation.observed_at.max().floor("D") < test.observed_at.min().floor("D")
    for left, right in [(train, validation), (train, test), (validation, test)]:
        assert not listing_ids(left) & listing_ids(right)
        assert not set(left.observed_at.dt.date) & set(right.observed_at.dt.date)
    assert train.loc[train.listing_id.eq("0"), "price_pln"].iloc[0] == raw.price_pln.iloc[0]
    assert metadata["listing_identity_overlap"] == 0
    assert metadata["train"]["listing_keys_sha256"]
    assert metadata["train"]["listing_keys"]


def test_one_snapshot_requires_explicit_exploratory_group_split():
    raw = listing_frame(single_day=True)
    with pytest.raises(InsufficientDataError, match="3 different"):
        split_data(raw)
    train, validation, test, metadata = split_data(raw, mode="group")
    assert [len(train), len(validation), len(test)] == [36, 12, 12]
    assert metadata["exploratory"] is True
    assert not listing_ids(train) & listing_ids(test)
    again = split_data(raw, mode="group")
    assert listing_ids(train) == listing_ids(again[0])


def test_small_history_raises_readiness_error_but_bad_schema_does_not():
    with pytest.raises(InsufficientDataError, match="30"):
        split_data(listing_frame(20))
    with pytest.raises(ValueError) as error:
        split_data(listing_frame().drop(columns="source"))
    assert not isinstance(error.value, InsufficientDataError)


def test_encoder_accepts_unseen_district_and_excludes_price_features():
    data = clean_listings(listing_frame())
    preprocessor = build_preprocessor().fit(data[FEATURES])
    unseen = data.iloc[:1].copy()
    unseen["district"] = "Previously unseen district"
    original = preprocessor.transform(data.iloc[:1][FEATURES])
    transformed = preprocessor.transform(unseen[FEATURES])
    assert transformed.shape == original.shape
    assert np.isfinite(transformed).all()
    assert "price_pln" not in FEATURES
    assert "price_per_m2" not in FEATURES


def test_model_roundtrip_keeps_predictions_and_train_only_preprocessing(tmp_path, monkeypatch):
    from src import train as training_module

    fits = []
    real_fit = training_module.XGBRegressor.fit

    def capture_fit(model, X, y, **kwargs):
        fits.append((np.asarray(y).copy(), kwargs["eval_set"]))
        return real_fit(model, X, y, **kwargs)

    monkeypatch.setattr(training_module.XGBRegressor, "fit", capture_fit)
    raw = listing_frame()
    artifact, metrics = train_model(raw, n_estimators=40)
    path = tmp_path / "model.joblib"
    joblib.dump(artifact, path)
    loaded = joblib.load(path)
    before = score_listings(raw, artifact)
    after = score_listings(raw, loaded)
    np.testing.assert_allclose(before.predicted_price_pln, after.predicted_price_pln)
    train, validation, _, _ = split_data(raw)
    assert len(fits) == 1  # No hidden refit on validation or test after evaluation.
    np.testing.assert_array_equal(fits[0][0], train.price_pln)
    assert len(fits[0][1]) == 1
    np.testing.assert_array_equal(fits[0][1][0][1], validation.price_pln)
    np.testing.assert_allclose(
        fits[0][1][0][0], artifact["preprocessor"].transform(validation[FEATURES])
    )
    floor_index = NUMERIC_FEATURES.index("floor")
    imputer = artifact["preprocessor"].named_transformers_["numeric"]
    assert imputer.statistics_[floor_index] == pytest.approx(train.floor.median())
    assert imputer.statistics_[floor_index] != pytest.approx(clean_listings(raw).floor.median())
    assert artifact["metadata"]["model_fit_subset"] == "train_only; no refit after evaluation"
    assert artifact["metadata"]["early_stopping_subset"] == "validation_only"
    assert artifact["metadata"]["n_estimators"] == 40
    assert metrics["primary_metrics_subset"] == "test"
    for values in [metrics["xgboost"], metrics["baseline_median"], metrics["validation"]["xgboost"]]:
        assert np.isfinite(list(values.values())).all()
    assert before.seen_in_training.sum() == 36
    assert set(before.partition) == {"train", "validation", "test"}


class FixedPredictions:
    def __init__(self, predictions):
        self.predictions = np.asarray(predictions)

    def predict(self, transformed):
        assert len(transformed) == len(self.predictions)
        return self.predictions


def fixed_artifact(data, predictions):
    cleaned = clean_listings(data)
    return {
        "preprocessor": build_preprocessor().fit(cleaned[FEATURES]),
        "model": FixedPredictions(predictions), "features": FEATURES,
        "metadata": {},
    }


def test_scoring_threshold_uses_prediction_denominator_and_accepts_exact_boundary():
    raw = listing_frame(3)
    raw["price_pln"] = [850_000, 850_100, 1_100_000]
    scored = score_listings(raw, fixed_artifact(raw, [1_000_000] * 3), 0.15)
    np.testing.assert_allclose(scored.discount, [0.15, 0.1499, -0.1])
    assert scored.is_deal.tolist() == [True, False, False]
    assert scored.partition.eq("unseen").all()


def test_nonpositive_or_nonfinite_estimates_are_never_deals():
    raw = listing_frame(4)
    scored = score_listings(raw, fixed_artifact(raw, [0, -1, np.inf, np.nan]))
    assert not scored.is_deal.any()
    assert scored.discount.isna().all()


@pytest.mark.parametrize("threshold", [-0.1, 1.0, np.nan, np.inf])
def test_invalid_discount_threshold_is_rejected(threshold):
    raw = listing_frame(3)
    with pytest.raises(ValueError, match="threshold"):
        score_listings(raw, fixed_artifact(raw, [1_000_000] * 3), threshold)
