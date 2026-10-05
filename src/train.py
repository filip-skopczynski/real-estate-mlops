"""Train and evaluate an asking-price model, then score current listings."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from dotenv import load_dotenv
from sklearn.compose import ColumnTransformer
from sklearn.dummy import DummyRegressor
from sklearn.impute import SimpleImputer
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import GroupShuffleSplit
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder
import xgboost
from xgboost import XGBRegressor

from src.preprocess import IDENTITY_COLUMNS, PROJECT_DIR, clean_listings


NUMERIC_FEATURES = [
    "area_m2", "rooms", "distance_km", "floor", "build_year", "latitude", "longitude"
]
CATEGORICAL_FEATURES = ["district"]
FEATURES = NUMERIC_FEATURES + CATEGORICAL_FEATURES
TARGET = "price_pln"


class InsufficientDataError(ValueError):
    """Valid history is not yet large enough for the requested evaluation split."""


def _split_summary(frame: pd.DataFrame) -> dict:
    identities = sorted(
        json.dumps([str(source), str(listing_id)], ensure_ascii=False)
        for source, listing_id in frame[IDENTITY_COLUMNS].itertuples(index=False, name=None)
    )
    return {
        "rows": len(frame),
        "first_observation_utc": frame["observed_at"].min().isoformat(),
        "last_observation_utc": frame["observed_at"].max().isoformat(),
        "utc_days": sorted(frame["observed_at"].dt.strftime("%Y-%m-%d").unique().tolist()),
        "listing_keys_sha256": hashlib.sha256("\n".join(identities).encode()).hexdigest(),
        "listing_keys": [json.loads(identity) for identity in identities],
    }


def split_data(frame: pd.DataFrame, mode: str = "temporal", seed: int = 42):
    """Return train/validation/test and provenance; keep listing IDs disjoint.

    Temporal boundaries use whole UTC days nearest 60% and 80% of records.
    Actual ratios can differ because one day is never divided across splits.
    Group splitting is an explicitly exploratory alternative for one snapshot.
    """
    if mode not in {"temporal", "group"}:
        raise ValueError("Split must be 'temporal' or 'group'.")
    data = clean_listings(frame, keep="earliest")
    if len(data) < 30:
        raise InsufficientDataError("At least 30 valid, unique Warsaw sale listings are required.")

    if mode == "temporal":
        days = data["observed_at"].dt.floor("D")
        counts = days.value_counts().sort_index()
        if len(counts) < 3:
            raise InsufficientDataError(
                "Temporal evaluation requires at least 3 different UTC observation days. "
                "Collect a longer history or explicitly use --split group for exploration."
            )
        cumulative = counts.cumsum().to_numpy()
        train_cut = int(np.argmin(np.abs(cumulative[:-2] - 0.6 * len(data)))) + 1
        candidates = np.arange(train_cut, len(counts) - 1)
        val_cut = int(candidates[np.argmin(np.abs(cumulative[candidates] - 0.8 * len(data)))]) + 1
        train_days = counts.index[:train_cut]
        val_days = counts.index[train_cut:val_cut]
        test_days = counts.index[val_cut:]
        train = data.loc[days.isin(train_days)].copy()
        validation = data.loc[days.isin(val_days)].copy()
        test = data.loc[days.isin(test_days)].copy()
    else:
        groups = pd.MultiIndex.from_frame(data[IDENTITY_COLUMNS]).factorize()[0]
        initial = GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=seed)
        remaining_idx, test_idx = next(initial.split(data, groups=groups))
        remaining = data.iloc[remaining_idx]
        secondary = GroupShuffleSplit(n_splits=1, test_size=0.25, random_state=seed + 1)
        train_idx, val_idx = next(secondary.split(remaining, groups=groups[remaining_idx]))
        train = remaining.iloc[train_idx].copy()
        validation = remaining.iloc[val_idx].copy()
        test = data.iloc[test_idx].copy()

    if min(len(train), len(validation), len(test)) < 2:
        raise InsufficientDataError(
            "Each split needs at least 2 unique listings for evaluation. "
            "Collect more observations across days; current whole-day boundaries are too sparse."
        )
    identities = [
        set(part[IDENTITY_COLUMNS].itertuples(index=False, name=None))
        for part in (train, validation, test)
    ]
    if any(identities[a] & identities[b] for a, b in [(0, 1), (0, 2), (1, 2)]):
        raise ValueError("A listing identity leaked across evaluation splits.")
    metadata = {
        "type": mode,
        "exploratory": mode == "group",
        "seed": seed,
        "identity_columns": IDENTITY_COLUMNS,
        "deduplication": "earliest_observation_per_source_listing_id",
        "requested_fractions": {"train": 0.6, "validation": 0.2, "test": 0.2},
        "actual_fractions": {
            name: len(part) / len(data)
            for name, part in [("train", train), ("validation", validation), ("test", test)]
        },
        "train": _split_summary(train),
        "validation": _split_summary(validation),
        "test": _split_summary(test),
        "listing_identity_overlap": 0,
        "cross_source_property_matching": False,
    }
    return train, validation, test, metadata


def build_preprocessor() -> ColumnTransformer:
    """Fit imputers/encoding on training data; ignore unseen district categories."""
    return ColumnTransformer(
        [
            ("numeric", SimpleImputer(strategy="median", keep_empty_features=True), NUMERIC_FEATURES),
            (
                "district",
                Pipeline([
                    ("imputer", SimpleImputer(strategy="constant", fill_value="Unknown")),
                    ("encoder", OneHotEncoder(handle_unknown="ignore", sparse_output=False)),
                ]),
                CATEGORICAL_FEATURES,
            ),
        ],
        remainder="drop",
    )


def _metrics(actual, predicted) -> dict:
    return {
        "mae_pln": float(mean_absolute_error(actual, predicted)),
        "rmse_pln": float(np.sqrt(mean_squared_error(actual, predicted))),
        "r2": float(r2_score(actual, predicted)),
    }


def train_model(frame: pd.DataFrame, split: str = "temporal", seed: int = 42, n_estimators: int = 500):
    """Return the evaluated artifact and metrics, without refitting on held-out data."""
    if isinstance(n_estimators, bool) or not isinstance(n_estimators, (int, np.integer)) or n_estimators < 1:
        raise ValueError("n_estimators must be a positive integer.")
    train, validation, test, provenance = split_data(frame, mode=split, seed=seed)
    preprocessor = build_preprocessor()
    X_train = preprocessor.fit_transform(train[FEATURES])
    X_validation = preprocessor.transform(validation[FEATURES])
    X_test = preprocessor.transform(test[FEATURES])
    y_train, y_validation, y_test = (part[TARGET] for part in [train, validation, test])

    model = XGBRegressor(
        n_estimators=n_estimators,
        max_depth=4,
        learning_rate=0.05,
        min_child_weight=3,
        subsample=0.9,
        colsample_bytree=0.9,
        objective="reg:squarederror",
        tree_method="hist",
        n_jobs=2,
        random_state=seed,
        eval_metric="mae",
        early_stopping_rounds=30,
    )
    # Only validation controls early stopping. Test is never passed to fit.
    model.fit(X_train, y_train, eval_set=[(X_validation, y_validation)], verbose=False)
    baseline = DummyRegressor(strategy="median").fit(X_train, y_train)
    model_metrics = _metrics(y_test, model.predict(X_test))
    baseline_metrics = _metrics(y_test, baseline.predict(X_test))
    metadata = {
        "trained_at_utc": datetime.now(timezone.utc).isoformat(),
        "target": TARGET,
        "target_unit": "PLN",
        "target_interpretation": "asking_price; not market value or transaction price",
        "transaction_scope": "sale only; source must confirm sale when transaction_type is absent",
        "feature_columns": FEATURES,
        "excluded_target_derived_features": ["price_per_m2"],
        "preprocessing_fit_subset": "train_only",
        "early_stopping_subset": "validation_only",
        "model_fit_subset": "train_only; no refit after evaluation",
        "best_iteration": int(model.best_iteration),
        "n_estimators": int(n_estimators),
        "baseline_train_median_pln": float(y_train.median()),
        "split": provenance,
        "versions": {
            "numpy": np.__version__, "pandas": pd.__version__,
            "scikit-learn": sklearn.__version__, "xgboost": xgboost.__version__,
            "joblib": joblib.__version__,
        },
    }
    artifact = {"preprocessor": preprocessor, "model": model, "features": FEATURES.copy(), "metadata": metadata}
    metrics = {
        "xgboost": model_metrics,
        "baseline_median": baseline_metrics,
        "validation": {
            "xgboost": _metrics(y_validation, model.predict(X_validation)),
            "baseline_median": _metrics(y_validation, baseline.predict(X_validation)),
        },
        "primary_metrics_subset": "test",
        "split": provenance,
        "metadata": metadata,
    }
    return artifact, metrics


def score_listings(frame: pd.DataFrame, artifact: dict, discount_threshold: float = 0.15) -> pd.DataFrame:
    """Score latest listings using the exact evaluated model; return all candidates.

    'discount' means (predicted asking price - actual asking price) / prediction.
    It is a model discrepancy, not proof of an investment opportunity.
    """
    if not np.isfinite(discount_threshold) or not 0 <= discount_threshold < 1:
        raise ValueError("Discount threshold must be a finite fraction from 0 to less than 1.")
    result = clean_listings(frame, keep="latest")
    if result.empty:
        result["predicted_price_pln"] = pd.Series(dtype=float)
        result["discount"] = pd.Series(dtype=float)
        result["is_deal"] = pd.Series(dtype=bool)
        result["partition"] = pd.Series(dtype=str)
        result["seen_in_training"] = pd.Series(dtype=bool)
        return result
    transformed = artifact["preprocessor"].transform(result[artifact["features"]])
    predictions = np.asarray(artifact["model"].predict(transformed), dtype=float)
    if predictions.shape != (len(result),):
        raise ValueError("Model must return one predicted price for each listing.")
    valid_prediction = np.isfinite(predictions) & (predictions > 0)
    discount = np.full(len(result), np.nan)
    np.divide(
        predictions - result[TARGET].to_numpy(), predictions,
        out=discount, where=valid_prediction,
    )
    result["predicted_price_pln"] = predictions
    result["discount"] = discount
    result["is_deal"] = valid_prediction & (discount >= discount_threshold)
    provenance = artifact.get("metadata", {}).get("split", {})
    partitions = {
        tuple(key): partition
        for partition in ["train", "validation", "test"]
        for key in provenance.get(partition, {}).get("listing_keys", [])
    }
    result["partition"] = [
        partitions.get((str(source), str(listing_id)), "unseen")
        for source, listing_id in result[IDENTITY_COLUMNS].itertuples(index=False, name=None)
    ]
    result["seen_in_training"] = result["partition"].eq("train")
    return result


def main() -> None:
    load_dotenv(PROJECT_DIR / ".env")
    parser = argparse.ArgumentParser(description="Train a Warsaw listing asking-price model.")
    parser.add_argument("--input", type=Path, default=PROJECT_DIR / "data/training.csv")
    parser.add_argument("--latest", type=Path, default=PROJECT_DIR / "data/latest.csv")
    parser.add_argument("--split", choices=["temporal", "group"], default=os.getenv("TRAIN_SPLIT", "temporal"))
    parser.add_argument("--discount-threshold", type=float, help="Candidate threshold in percent; default DEAL_THRESHOLD * 100 or 15.")
    parser.add_argument("--n-estimators", type=int, default=500)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--model-output", type=Path, default=PROJECT_DIR / "models/model.joblib")
    parser.add_argument("--metrics-output", type=Path, default=PROJECT_DIR / "models/metrics.json")
    parser.add_argument("--deals-output", type=Path, default=PROJECT_DIR / "data/deals.csv")
    args = parser.parse_args()
    try:
        if args.discount_threshold is None:
            args.discount_threshold = float(os.getenv("DEAL_THRESHOLD", "0.15")) * 100
        if not np.isfinite(args.discount_threshold) or not 0 <= args.discount_threshold < 100:
            raise ValueError("--discount-threshold must be from 0 to less than 100 percent.")
        data = pd.read_csv(args.input, dtype={"source": "string", "listing_id": "string"})
        latest = pd.read_csv(args.latest, dtype={"source": "string", "listing_id": "string"})
        artifact, metrics = train_model(data, split=args.split, seed=args.seed, n_estimators=args.n_estimators)
        scored = score_listings(latest, artifact, args.discount_threshold / 100)
        deals = scored.loc[scored["is_deal"]].sort_values("discount", ascending=False)
        metrics["scoring"] = {
            "threshold_fraction": args.discount_threshold / 100,
            "latest_valid_listings": len(scored), "flagged_candidates": len(deals),
            "interpretation": "model discrepancy against asking prices; not market value",
            "seen_in_training": int(scored["seen_in_training"].sum()),
            "partition_counts": {str(name): int(count) for name, count in scored["partition"].value_counts().items()},
            "warning": "Scoring train listings is optimistic; use unseen listings for prospective candidates.",
        }
        metrics_json = json.dumps(metrics, indent=2, ensure_ascii=False, allow_nan=False)
        for target in [args.model_output, args.metrics_output, args.deals_output]:
            target.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(artifact, args.model_output)
        args.metrics_output.write_text(metrics_json + "\n", encoding="utf-8")
        deals.to_csv(args.deals_output, index=False)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if args.split == "group":
        print("Exploratory group split: this does not establish future-period performance.")
    print("Target: advertised sale asking prices in PLN, not transaction prices or market value.")
    print(f"XGBoost test MAE: {metrics['xgboost']['mae_pln']:,.0f} PLN")
    print(f"Median baseline test MAE: {metrics['baseline_median']['mae_pln']:,.0f} PLN")
    print(f"Flagged {len(deals)} candidates at {args.discount_threshold:g}% discrepancy.")
    print(f"Model: {args.model_output}; metrics: {args.metrics_output}; candidates: {args.deals_output}")


if __name__ == "__main__":
    main()
