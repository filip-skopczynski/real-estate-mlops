"""Opt-in experimental model/ranking build from frozen portal observations.

Cloud access is a bounded, read-only PostgreSQL repeatable-read snapshot. The
offline route opens no database. Raw first observations establish the temporal
training cohort before quality filtering; current sparse catalogue state alone
establishes current prices. Cross-portal property identity remains unresolved.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile

import joblib
import numpy as np
import pandas as pd

from src.preprocess import clean_listings
from src.quality import RESERVED, audit_catalog
from src.show_deals import (_config, _identity, _property_signature, _safe_url,
                            _utc, format_deals, select_deals)
from src.train import InsufficientDataError, score_listings, train_model

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCES = ("www.olx.pl", "www.otodom.pl")
MAX_OBSERVATIONS = 250000
MAX_CATALOG = 50000
REQUIRED = {"source", "listing_id", "url", "city", "price_pln", "area_m2", "rooms", "observed_at"}
OBSERVATION_FIELDS = (
    "source", "listing_id", "url", "city", "district", "price_pln", "area_m2",
    "rooms", "floor", "build_year", "latitude", "longitude", "distance_km", "observed_at",
)
CATALOG_FIELDS = (
    "source", "listing_id", "url", "city", "district", "price_pln", "area_m2", "rooms",
    "rooms_min", "floor", "build_year", "latitude", "longitude", "distance_km", "published_at", "observed_at",
)
FRAME_FILES = {"observations.csv": "observations", "catalog_latest.csv": "catalog", "training.csv": "training",
               "latest.csv": "latest", "scored_listings.csv": "scored", "deals.csv": "deals"}


class BuildDealsError(ValueError):
    """A sanitized configuration, snapshot or data preparation error."""


def read_snapshot(*, engine=None):
    """Return observations, authoritative catalogue and snapshot metadata.

    Reads share one PostgreSQL transaction and snapshot. Both queries fetch at
    most the configured hard limit plus one, failing rather than truncating.
    No schema initialization, insert, update or portal request is performed.
    """
    own_engine = engine is None
    try:
        if engine is None:
            from src.database import get_engine

            engine = get_engine()
        if engine.dialect.name != "postgresql":
            raise BuildDealsError("Cloud snapshots require PostgreSQL; use both CSV inputs for offline work.")
        from sqlalchemy import text

        params = {"source_olx": SOURCES[0], "source_otodom": SOURCES[1]}
        with engine.connect() as connection:
            connection = connection.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
            with connection.begin():
                connection.execute(text("SET TRANSACTION READ ONLY"))
                readonly = connection.execute(text("SHOW transaction_read_only")).scalar_one()
                isolation = connection.execute(text("SHOW transaction_isolation")).scalar_one()
                if str(readonly).casefold() != "on" or str(isolation).casefold() != "repeatable read":
                    raise BuildDealsError("Database could not confirm a read-only repeatable-read transaction.")
                capture = _utc(connection.execute(text("SELECT CURRENT_TIMESTAMP")).scalar_one())
                if capture is None:
                    raise BuildDealsError("Database returned an invalid snapshot time.")
                scope = " WHERE source IN (:source_olx, :source_otodom) "
                observations = pd.read_sql_query(text(
                    "SELECT " + ", ".join(OBSERVATION_FIELDS) + " FROM public.listing_observations"
                    + scope + "ORDER BY source, listing_id, observed_at LIMIT :limit"
                ), connection, params={**params, "limit": MAX_OBSERVATIONS + 1})
                if len(observations) > MAX_OBSERVATIONS:
                    raise BuildDealsError("Observation snapshot exceeds the 250000-row safety limit.")
                catalog_columns = ["last_seen_at AS observed_at" if field == "observed_at" else field for field in CATALOG_FIELDS]
                catalog = pd.read_sql_query(text(
                    "SELECT " + ", ".join(catalog_columns) + " FROM public.listing_catalog"
                    + scope + "ORDER BY source, listing_id LIMIT :limit"
                ), connection, params={**params, "limit": MAX_CATALOG + 1})
                if len(catalog) > MAX_CATALOG:
                    raise BuildDealsError("Catalogue snapshot exceeds the 50000-row safety limit.")
        return observations, catalog, {
            "mode": "cloud_read_only_snapshot", "capture_time": capture.isoformat(),
            "sources": list(SOURCES), "transaction_read_only": True,
            "transaction_isolation": "REPEATABLE READ", "consistent_snapshot": True,
            "observations_rows": len(observations), "catalog_rows": len(catalog),
            "catalog_observation_time": "listing_catalog.last_seen_at; never retimed",
        }
    except BuildDealsError:
        raise
    except Exception:
        raise BuildDealsError("Could not read the cloud snapshot. Check database access and configuration; private details are not logged.") from None
    finally:
        if own_engine and engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass


def _frame(frame, limit):
    if not isinstance(frame, pd.DataFrame) or not frame.columns.is_unique:
        raise BuildDealsError("Snapshot input must be a DataFrame with unique columns.")
    if len(frame) > limit:
        raise BuildDealsError("Snapshot input exceeds its row safety limit.")
    missing = REQUIRED.difference(frame.columns)
    if missing:
        raise BuildDealsError("Missing snapshot columns: " + ", ".join(sorted(missing)))
    return frame.reset_index(drop=True).copy(deep=True)


def _prepare(frame, *, keep, now, max_age_days=None):
    raw = _frame(frame, MAX_OBSERVATIONS if keep == "earliest" else MAX_CATALOG)
    moment = _utc(datetime.now(timezone.utc) if now is None else now)
    if moment is None:
        raise BuildDealsError("Snapshot time must be timezone aware.")
    grouped, invalid_times = defaultdict(list), set()
    invalid_identity_rows = 0
    for position, (source, identifier, observed) in enumerate(zip(raw.source, raw.listing_id, raw.observed_at)):
        identity = _identity(source, identifier)
        if identity is None:
            invalid_identity_rows += 1
            continue
        stamp = _utc(observed)
        grouped[identity].append((position, stamp))
        if stamp is None:
            invalid_times.add(identity)
    chosen, excluded = {}, Counter()
    ignored_history_rows = 0
    for identity in sorted(grouped):
        if identity in invalid_times:
            excluded["unorderable_observation_time"] += 1
            continue
        history = grouped[identity]
        capture = (min if keep == "earliest" else max)(stamp for _, stamp in history)
        positions = [position for position, stamp in history if stamp == capture]
        ignored_history_rows += len(history) - len(positions)
        age_days = (int(moment.value) - int(capture.value)) / (86400 * 1000000000)
        if age_days < 0:
            excluded["future_observation"] += 1
        elif max_age_days is not None and age_days > max_age_days:
            excluded["stale_current_catalogue"] += 1
        else:
            chosen[identity] = positions
    positions = [position for identity in sorted(chosen) for position in chosen[identity]]
    flags = {}
    if positions:
        temporary = raw.iloc[positions].drop(columns=list(RESERVED), errors="ignore").copy(deep=True)
        position_keys = {position: identity for identity, indexes in chosen.items() for position in indexes}
        temporary["source"] = [position_keys[position][0] for position in positions]
        temporary["listing_id"] = [position_keys[position][1] for position in positions]
        annotated, _ = audit_catalog(temporary)
        flags = {position: set(value.split("|")) if value else set()
                 for position, value in zip(positions, annotated.quality_flags)}
    retained, retained_keys, quality_flags = [], {}, Counter()
    deduplicated_ties = 0
    for identity in sorted(chosen):
        indexes = chosen[identity]
        group_flags = set().union(*(flags[position] for position in indexes)).difference({"repeated_identity"})
        quality_flags.update(group_flags)
        if "conflicting_identity_timestamp" in group_flags:
            excluded["conflicting_selected_snapshot"] += 1
            continue
        if len({_property_signature(raw.iloc[position]) for position in indexes}) > 1:
            excluded["conflicting_optional_model_features"] += 1
            continue
        if group_flags:
            excluded["quality_not_pass"] += 1
            continue
        if any(not _safe_url(raw.iloc[position].url, identity[0]) for position in indexes):
            excluded["invalid_portal_offer_url"] += 1
            continue
        position = indexes[0] if len(indexes) == 1 else min(indexes, key=lambda index: tuple(repr(raw.iloc[index][column]) for column in sorted(raw.columns)))
        retained.append(position)
        retained_keys[position] = identity
        deduplicated_ties += len(indexes) - 1
    prepared = raw.iloc[retained].drop(columns=list(RESERVED), errors="ignore").copy(deep=True)
    prepared["source"] = [retained_keys[position][0] for position in retained]
    prepared["listing_id"] = [retained_keys[position][1] for position in retained]
    # Re-audit the selected unique snapshots: repeated price history is never a
    # quality rejection, and a later valid price never replaces an earlier bad one.
    unique_audit, unique_report = audit_catalog(prepared)
    if not unique_audit.quality_status.eq("pass").all():
        raise BuildDealsError("Selected unique snapshots failed their final quality audit.")
    cleaned = clean_listings(prepared, keep=keep)
    stats = {
        "input_rows": len(raw), "unique_supported_identities": len(grouped),
        "invalid_identity_rows": invalid_identity_rows, "retained_listings": len(cleaned),
        "excluded_counts": dict(sorted(excluded.items())),
        "quality_flag_counts": dict(sorted(quality_flags.items())),
        "selected_snapshot_quality": unique_report["status_counts"],
        "other_history_rows_ignored": ignored_history_rows,
        "equivalent_snapshot_ties_deduplicated": deduplicated_ties,
        "selection": "raw_" + keep + "_observation_first_then_quality_pass",
        "no_later_valid_fallback": keep == "earliest",
        "utc_day_counts": {str(day): int(count) for day, count in cleaned.observed_at.dt.strftime("%Y-%m-%d").value_counts().sort_index().items()},
    }
    return cleaned, stats


def prepare_training(observations, *, now=None):
    return _prepare(observations, keep="earliest", now=now)


def prepare_latest(catalog, *, now=None, max_age_days=7):
    _config(50, max_age_days, 0.15, now)
    return _prepare(catalog, keep="latest", now=now, max_age_days=max_age_days)


def _csv_bytes(frame):
    return frame.to_csv(index=False, lineterminator="\n").encode("utf-8")


def _sha256_frame(frame):
    return hashlib.sha256(_csv_bytes(frame)).hexdigest()


def _source_scope(frame):
    return frame.loc[frame.source.astype("string").str.strip().str.casefold().isin(SOURCES)].copy(deep=True)


def _baseline_comparison(metrics):
    model, baseline = metrics["xgboost"], metrics["baseline_median"]
    return {
        "subset": "test", "xgboost": dict(model), "baseline_median": dict(baseline),
        "mae_improvement_pln": float(baseline["mae_pln"] - model["mae_pln"]),
        "xgboost_has_lower_test_mae": bool(model["mae_pln"] < baseline["mae_pln"]),
        "interpretation": "experimental held-out comparison; no superiority or investment-value guarantee",
    }


def build_ranking(observations, catalog, *, now=None, discount_threshold=.15,
                  max_age_days=7, top=50, n_estimators=500):
    """Build a temporal, experimental artifact/ranking entirely in memory."""
    capture = _config(top, max_age_days, discount_threshold, now)
    if isinstance(n_estimators, (bool, np.bool_)) or not isinstance(n_estimators, (int, np.integer)) or n_estimators < 1:
        raise BuildDealsError("n_estimators must be a positive integer.")
    raw_observations = _frame(observations, MAX_OBSERVATIONS)
    raw_catalog = _frame(catalog, MAX_CATALOG)
    scoped_observations, scoped_catalog = _source_scope(raw_observations), _source_scope(raw_catalog)
    training, training_stats = prepare_training(scoped_observations, now=capture)
    latest, latest_stats = prepare_latest(scoped_catalog, now=capture, max_age_days=max_age_days)
    artifact, metrics = train_model(training, split="temporal", n_estimators=int(n_estimators))
    artifact["metadata"].update(experimental=True, cross_source_property_matching=False,
                                capture_time=capture.isoformat(), training_csv_sha256=_sha256_frame(training))
    metrics["experimental"] = True
    metrics["cross_source_property_matching"] = False
    scored = score_listings(latest, artifact, discount_threshold=discount_threshold)
    scored["ranking_status"] = "EXPERIMENTAL"
    scored["cross_source_property_matching"] = False
    scored["ranking_generated_at"] = capture.isoformat()
    candidate_input = scored.loc[scored.partition.isin(["test", "unseen"])].copy(deep=True)
    partition_exclusions = {
        "training_rows": int(scored.partition.eq("train").sum()),
        "validation_rows": int(scored.partition.eq("validation").sum()),
        "unknown_partition_rows": int((~scored.partition.isin(["train", "validation", "test", "unseen"])).sum()),
    }
    deals, selection_report = select_deals(candidate_input, top=top, max_age_days=max_age_days,
                                            discount_threshold=discount_threshold, now=capture)
    split = metrics["split"]
    training_days = training_stats["utc_day_counts"]
    first_day_count = next(iter(training_days.values()), 0)
    validation_rows = split["validation"]["rows"]
    warnings = [
        "EXPERIMENTAL: cross-portal property matching is unresolved; one property may occur in multiple source/ID splits.",
        "Model gaps are not verified savings, transaction values or proof that an offer is available.",
        "The test set was evaluated once; no threshold changes, tuning or split fallback were used to force results.",
    ]
    if training_days and first_day_count / len(training) > .8:
        warnings.append("The first observation day contains more than 80% of training-cohort identities; temporal evaluation is imbalanced.")
    if validation_rows < 100:
        warnings.append("The validation split has fewer than 100 listings; early-stopping and validation metrics may be unstable.")
    report = {
        "schema_version": 1, "capture_time": capture.isoformat(), "experimental": True,
        "source_scope": {"sources": list(SOURCES), "city": "Warszawa", "transaction": "apartment sale; asking prices"},
        "cross_source_property_matching": False, "cross_property_matching": False,
        "raw_counts": {"observations": len(raw_observations), "catalog": len(raw_catalog),
                       "scoped_observations": len(scoped_observations), "scoped_catalog": len(scoped_catalog)},
        "training_quality": training_stats, "latest_quality": latest_stats,
        "training_rows": len(training), "latest_rows": len(latest), "scored_rows": len(scored), "deals_rows": len(deals),
        "first_day_split": split, "split": split, "baseline_comparison": _baseline_comparison(metrics),
        "candidate_partition_policy": "test or unseen only; validation labels controlled early stopping",
        "candidate_partition_exclusions": partition_exclusions,
        "deal_selection": selection_report, "discount_threshold": float(discount_threshold),
        "max_age_days": float(max_age_days), "top": top, "n_estimators": int(n_estimators),
        "provenance": {"model_filename": "model.joblib", "observations_csv_sha256": _sha256_frame(scoped_observations),
                       "catalog_latest_csv_sha256": _sha256_frame(scoped_catalog), "training_csv_sha256": _sha256_frame(training),
                       "latest_csv_sha256": _sha256_frame(latest), "scored_listings_csv_sha256": _sha256_frame(scored),
                       "deals_csv_sha256": _sha256_frame(deals)},
        "warnings": warnings,
        "catalogue_authority": "current sparse catalogue only; missing current price never falls back to old price history",
        "output_commit": "all files staged before replacement; handled-failure rollback, not a crash-atomic multi-file transaction",
    }
    return {"artifact": artifact, "metrics": metrics, "training": training, "latest": latest,
            "scored": scored, "deals": deals, "report": report,
            "observations": scoped_observations, "catalog": scoped_catalog}


def _output_paths(data_dir=None, models_dir=None):
    data_dir = Path(data_dir) if data_dir is not None else PROJECT_ROOT / "data"
    models_dir = Path(models_dir) if models_dir is not None else PROJECT_ROOT / "models"
    destinations = {name: data_dir / name for name in FRAME_FILES}
    destinations.update({"model.joblib": models_dir / "model.joblib", "metrics.json": models_dir / "metrics.json",
                         "catalog_snapshot.json": data_dir / "catalog_snapshot.json",
                         "catalog_latest.json": data_dir / "catalog_latest.json",
                         "build_deals_report.json": data_dir / "build_deals_report.json"})
    return destinations


def save_bundle(bundle, *, data_dir=None, models_dir=None):
    """Stage every output before replacing files, restoring handled failures."""
    destinations = _output_paths(data_dir, models_dir)
    staged, backups, replaced, retained = {}, {}, [], set()

    def temporary(destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(prefix="." + destination.name + ".", suffix=".tmp", dir=destination.parent)
        os.close(handle)
        return Path(name)

    try:
        for name, destination in destinations.items():
            staged[name] = temporary(destination)
        for name, key in FRAME_FILES.items():
            staged[name].write_bytes(_csv_bytes(bundle[key]))
        joblib.dump(bundle["artifact"], staged["model.joblib"], compress=3)
        report = json.loads(json.dumps(bundle["report"], allow_nan=False))
        model_sha = hashlib.sha256(staged["model.joblib"].read_bytes()).hexdigest()
        report["provenance"].update(model_sha256=model_sha, model_path=str(destinations["model.joblib"].resolve()))
        metrics = dict(bundle["metrics"], model_file_sha256=model_sha, experimental=True)
        snapshot = {
            "capture_time": report["capture_time"], "source_scope": report["source_scope"],
            "snapshot": report.get("snapshot", {"mode": "provided_dataframes"}),
            "catalogue_rows": len(bundle["catalog"]), "catalog_observed_at": "original observation or last_seen_at; never retimed",
            "catalog_csv_sha256": report["provenance"]["catalog_latest_csv_sha256"],
        }
        for name, value in (("metrics.json", metrics), ("catalog_snapshot.json", snapshot),
                            ("catalog_latest.json", snapshot), ("build_deals_report.json", report)):
            staged[name].write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        for name, destination in destinations.items():
            backups[name] = temporary(destination) if destination.exists() else None
            if backups[name] is not None:
                shutil.copy2(destination, backups[name])
        try:
            for name, destination in destinations.items():
                os.replace(staged[name], destination)
                replaced.append(name)
        except OSError:
            rollback_failed = False
            for name in reversed(replaced):
                try:
                    if backups[name] is None:
                        destinations[name].unlink(missing_ok=True)
                    else:
                        os.replace(backups[name], destinations[name])
                except OSError:
                    rollback_failed = True
                    if backups[name] is not None:
                        retained.add(backups[name])
            if rollback_failed:
                raise BuildDealsError("Output replacement failed; some prior files could not be restored and their backups were retained.") from None
            raise
        bundle["report"] = report
        return {name: str(path.resolve()) for name, path in destinations.items()}
    finally:
        for path in [*staged.values(), *(path for path in backups.values() if path is not None)]:
            if path not in retained:
                path.unlink(missing_ok=True)


def _read_csv(path, limit):
    with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
        header = next(csv.reader(stream, strict=True), None)
        if header is None or len(header) != len(set(header)):
            raise BuildDealsError("CSV must have a nonempty, unique header.")
        stream.seek(0)
        frame = pd.read_csv(stream, dtype={"source": "string", "listing_id": "string"}, nrows=limit + 1)
    return _frame(frame, limit)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build an EXPERIMENTAL temporal price model and reviewed offers CSV.")
    parser.add_argument("--observations", type=Path)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    parser.add_argument("--models-dir", type=Path, default=PROJECT_ROOT / "models")
    parser.add_argument("--discount-threshold", type=float, default=15, help="Minimum model price gap in percent.")
    parser.add_argument("--max-age-days", type=float, default=7)
    parser.add_argument("--top", type=int, default=50)
    parser.add_argument("--n-estimators", type=int, default=500)
    parser.add_argument("--split", choices=["temporal"], default="temporal")
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code)
    try:
        if bool(args.observations) != bool(args.catalog):
            raise BuildDealsError("Offline mode requires both --observations and --catalog.")
        if not math.isfinite(args.discount_threshold) or not 0 <= args.discount_threshold < 100:
            raise BuildDealsError("--discount-threshold must be from 0 to less than 100 percent.")
        _config(args.top, args.max_age_days, args.discount_threshold / 100, None)
        if args.n_estimators < 1:
            raise BuildDealsError("--n-estimators must be positive.")
        if args.observations is not None:
            output_paths = {path.resolve() for path in _output_paths(args.data_dir, args.models_dir).values()}
            if args.observations.resolve() in output_paths or args.catalog.resolve() in output_paths:
                raise BuildDealsError("Offline input files must not overlap output files. Choose a separate --data-dir and, if needed, --models-dir.")
            observations = _read_csv(args.observations, MAX_OBSERVATIONS)
            catalog = _read_csv(args.catalog, MAX_CATALOG)
            snapshot = {"mode": "offline_csv", "capture_time": datetime.now(timezone.utc).isoformat(),
                        "observations_file": str(args.observations.resolve()), "catalog_file": str(args.catalog.resolve()),
                        "consistent_snapshot": "not independently established for supplied CSVs"}
        else:
            print("Reading a bounded, read-only cloud snapshot...", flush=True)
            observations, catalog, snapshot = read_snapshot()
        print("Training and evaluating the EXPERIMENTAL temporal model...", flush=True)
        bundle = build_ranking(observations, catalog, now=snapshot["capture_time"], discount_threshold=args.discount_threshold / 100,
                               max_age_days=args.max_age_days, top=args.top, n_estimators=args.n_estimators)
        bundle["report"]["snapshot"] = snapshot
        paths = save_bundle(bundle, data_dir=args.data_dir, models_dir=args.models_dir)
        print(format_deals(bundle["deals"], bundle["report"]["deal_selection"]))
        comparison = bundle["report"]["baseline_comparison"]
        print(f"Experimental test MAE: XGBoost {comparison['xgboost']['mae_pln']:,.2f} PLN; median baseline {comparison['baseline_median']['mae_pln']:,.2f} PLN.")
        print("Ranking saved: " + paths["deals.csv"])
        return 0
    except InsufficientDataError:
        print("Cannot train a temporal model yet: need at least 30 quality-pass unique listings across 3 first-observation UTC days and at least 2 listings per split. Existing outputs were preserved.")
        return 2
    except BuildDealsError as error:
        print(str(error))
        return 2
    except Exception:
        print("Could not build ranked offers. Check snapshot data and configuration; private details are not logged. Existing outputs were preserved when training failed.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
