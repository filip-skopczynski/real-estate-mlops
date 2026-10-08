"""Offline, non-destructive review of sparse Warsaw portal catalogues.

Rules flag records for inspection; they neither establish investment value nor
select a training set. Price-based similarity is never a confirmed property ID.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from numbers import Real
import os
from pathlib import Path
import shutil
import tempfile

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ("source", "listing_id", "city", "price_pln", "area_m2", "rooms", "observed_at")
RESERVED = ("quality_flags", "quality_status", "audit_price_per_m2")
INCOMPLETE_FLAGS = {"missing_price", "missing_area", "missing_exact_rooms"}
MISSING_FIELDS = ("price_pln", "area_m2", "rooms", "district", "floor", "build_year",
                  "latitude", "longitude", "distance_km", "published_at")


@dataclass(frozen=True)
class QualityThresholds:
    """Fixed review heuristics, not learned market bounds or deletion rules."""

    price_min: float = 100_000
    price_max: float = 10_000_000
    area_min: float = 10
    area_max: float = 500
    price_per_m2_min: float = 3_000
    price_per_m2_max: float = 50_000

    def __post_init__(self):
        for value in asdict(self).values():
            if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real) or not math.isfinite(value) or value <= 0:
                raise ValueError("Review thresholds must be finite positive numbers.")
        for name in ("price", "area", "price_per_m2"):
            if getattr(self, name + "_min") >= getattr(self, name + "_max"):
                raise ValueError("Each minimum review threshold must be below its maximum.")


def _missing(series):
    return (series.isna() | series.astype("string").str.strip().eq("")).fillna(True)


def _text(series):
    return series.astype("string").str.normalize("NFC").str.strip()


def _numeric(series):
    result = pd.to_numeric(series, errors="coerce").astype(float)
    boolean = series.map(lambda value: isinstance(value, (bool, np.bool_)))
    return result.where(np.isfinite(result) & ~boolean)


def _has_timezone(value):
    if not isinstance(value, (str, datetime, pd.Timestamp, np.datetime64)):
        return False
    try:
        stamp = pd.Timestamp(value)
        return stamp.tzinfo is not None and stamp.utcoffset() is not None
    except (TypeError, ValueError, OverflowError):
        return False


def _summary(series):
    values = series.loc[series.notna() & np.isfinite(series) & series.gt(0)]
    if values.empty:
        return dict(count=0, min=None, p01=None, median=None, p99=None, max=None)
    quantiles = values.quantile([0.01, 0.5, 0.99])
    return dict(count=len(values), min=float(values.min()), p01=float(quantiles.loc[0.01]),
                median=float(quantiles.loc[0.5]), p99=float(quantiles.loc[0.99]), max=float(values.max()))


def audit_catalog(frame: pd.DataFrame, *, thresholds=None):
    """Annotate every input row, preserving original values, ordering and index."""
    if not isinstance(frame, pd.DataFrame) or frame.columns.duplicated().any():
        raise ValueError("Input must be a DataFrame with unique column names.")
    absent = sorted(set(REQUIRED) - set(frame.columns))
    if absent:
        raise ValueError("Missing catalogue columns: " + ", ".join(absent))
    if set(RESERVED) & set(frame.columns):
        raise ValueError("Input already contains reserved quality annotation columns.")
    if thresholds is None:
        thresholds = QualityThresholds()
    if not isinstance(thresholds, QualityThresholds):
        raise ValueError("thresholds must be a QualityThresholds instance.")

    raw = frame.reset_index(drop=True)
    flags = [set() for _ in range(len(raw))]

    def flag(name, mask):
        for index in np.flatnonzero(mask.fillna(False).to_numpy(dtype=bool)):
            flags[index].add(name)

    source, identity = _text(raw["source"]), _text(raw["listing_id"])
    valid_identity = ~_missing(source) & ~_missing(identity)
    flag("invalid_source", _missing(source))
    flag("invalid_listing_id", _missing(identity))
    flag("not_warsaw", ~_text(raw["city"]).str.casefold().isin(["warszawa", "warsaw"]))

    numbers = {field: _numeric(raw[field]) for field in ("price_pln", "area_m2", "rooms")}
    for field, missing_name in (("price_pln", "missing_price"), ("area_m2", "missing_area"), ("rooms", "missing_exact_rooms")):
        absent_value = _missing(raw[field])
        valid = numbers[field].gt(0) & numbers[field].notna()
        if field == "rooms":
            valid &= numbers[field].mod(1).eq(0)
        flag(missing_name, absent_value)
        flag("invalid_" + ("price" if field == "price_pln" else "area" if field == "area_m2" else "rooms"), ~absent_value & ~valid)

    for field in ("rooms_min", "floor", "build_year", "latitude", "longitude", "distance_km"):
        if field not in raw:
            continue
        number = numbers[field] = _numeric(raw[field])
        valid = number.notna()
        if field in {"rooms_min", "floor", "build_year"}:
            valid &= number.mod(1).eq(0)
        if field in {"rooms_min", "build_year"}:
            valid &= number.gt(0)
        if field == "distance_km":
            valid &= number.ge(0)
        if field == "latitude":
            valid &= number.between(-90, 90)
        if field == "longitude":
            valid &= number.between(-180, 180)
        flag("invalid_" + field, ~_missing(raw[field]) & ~valid)
    if "rooms_min" in numbers:
        valid_min = numbers["rooms_min"].gt(0) & numbers["rooms_min"].mod(1).eq(0)
        flag("rooms_below_minimum", valid_min & numbers["rooms"].gt(0) & numbers["rooms"].lt(numbers["rooms_min"]))

    timestamps = pd.to_datetime(raw["observed_at"], errors="coerce", utc=True, format="mixed")
    timestamp_types = raw["observed_at"].map(lambda value: isinstance(value, (str, datetime, pd.Timestamp, np.datetime64)))
    valid_time = timestamps.notna() & timestamp_types
    aware = raw["observed_at"].map(_has_timezone)
    flag("invalid_observed_at", ~valid_time)
    flag("unverified_timestamp_timezone", valid_time & ~aware)

    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        ratio = numbers["price_pln"] / numbers["area_m2"]
    ratio = ratio.where(numbers["price_pln"].gt(0) & numbers["area_m2"].gt(0) & np.isfinite(ratio))
    for column, name in (("price_pln", "price"), ("area_m2", "area"), ("price_per_m2", "price_per_m2")):
        value = ratio if column == "price_per_m2" else numbers[column]
        valid = value.gt(0) & value.notna()
        flag(name + "_below_review_min", valid & value.lt(getattr(thresholds, name + "_min")))
        flag(name + "_above_review_max", valid & value.gt(getattr(thresholds, name + "_max")))

    keys = pd.DataFrame({"source": source, "listing_id": identity, "observed_at": timestamps})
    identity_rows = keys.loc[valid_identity, ["source", "listing_id"]]
    unique_identities = len(identity_rows.drop_duplicates())
    repeated = valid_identity & keys.duplicated(["source", "listing_id"], keep=False)
    flag("repeated_identity", repeated)

    # Compare same-time snapshots semantically; 800000 and 800000.0 agree.
    values = keys.copy()
    features = []
    for field in ("price_pln", "area_m2", "rooms", "district", "url"):
        if field not in raw:
            continue
        name = "value_" + field
        if field in numbers:
            normalized = numbers[field].map(lambda value: "number:" + repr(float(value)) if pd.notna(value) else "").astype("string")
            malformed = ~_missing(raw[field]) & numbers[field].isna()
            normalized.loc[malformed] = "invalid:" + _text(raw.loc[malformed, field])
        else:
            normalized = _text(raw[field]).fillna("")
            if field == "district":
                normalized = normalized.str.casefold().str.replace(r"\s+", " ", regex=True)
        values[name] = normalized
        features.append(name)
    eligible_conflicts = valid_identity & valid_time
    conflict_mask = pd.Series(False, index=raw.index)
    if features and eligible_conflicts.any():
        groups = values.loc[eligible_conflicts].groupby(["source", "listing_id", "observed_at"], dropna=False)
        varying = groups[features].nunique(dropna=False).gt(1).any(axis=1)
        bad_keys = set(varying.index[varying])
        conflict_mask = pd.Series([tuple(row) in bad_keys for row in keys.itertuples(index=False, name=None)], index=raw.index)
    flag("conflicting_identity_timestamp", conflict_mask)

    statuses = ["review" if row - INCOMPLETE_FLAGS else "incomplete" if row else "pass" for row in flags]
    annotated = frame.copy(deep=True)
    annotated["quality_flags"] = ["|".join(sorted(row)) for row in flags]
    annotated["quality_status"] = statuses
    annotated["audit_price_per_m2"] = ratio.to_numpy()
    all_flags = sorted(set().union(*flags)) if flags else []
    sources = source.mask(_missing(source), "[missing_source]")
    missing_by_source = {}
    for name in sorted(sources.unique()):
        subset = sources.eq(name)
        missing_by_source[name] = {field: int((_missing(raw[field]) & subset).sum()) if field in raw else int(subset.sum())
                                   for field in MISSING_FIELDS}
    known_times = timestamps.loc[valid_time & aware]
    report = {
        "schema_version": 1, "input_rows": len(raw), "unique_identities": unique_identities,
        "duplicate_identity_rows": len(identity_rows) - unique_identities,
        "status_counts": {status: statuses.count(status) for status in ("pass", "incomplete", "review")},
        "flag_counts": {name: sum(name in row for row in flags) for name in all_flags},
        "missing_by_source": missing_by_source,
        "numeric_summary": {field: _summary(numbers[field] if field != "price_per_m2" else ratio)
                            for field in ("price_pln", "area_m2", "price_per_m2")},
        "observation_range": {"first": known_times.min().isoformat() if len(known_times) else None,
                              "last": known_times.max().isoformat() if len(known_times) else None,
                              "utc_days": int(known_times.dt.normalize().nunique())},
        "thresholds": {name: float(value) for name, value in asdict(thresholds).items()},
        "warnings": ["Review thresholds are heuristics, not validated market bounds or deletion rules.",
                     "All input rows are preserved; pass is not approval for training or proof of a deal.",
                     "Repeated source/ID rows can be legitimate price history; review same-time conflicts.",
                     "Cross-source similarities are candidates for review, not confirmed property duplicates.",
                     "This audit does not train a model, establish availability or validate a train/test split."],
    }
    return annotated, report


def _markdown(report):
    counts, matching = report["status_counts"], report["duplicate_candidates"]
    lines = ["# Przegląd jakości katalogu", "", f"Utworzono: {report['generated_at']}", "",
             f"Wiersze: **{report['input_rows']}**. Unikalne pary źródło–ID: **{report['unique_identities']}**.", "",
             "| Wynik reguł | Wiersze |", "| --- | ---: |"]
    lines.extend(f"| {name} | {counts[name]} |" for name in ("pass", "incomplete", "review"))
    lines += ["", "`review` wymaga sprawdzenia reguł, `incomplete` oznacza brak ceny, metrażu lub dokładnych pokoi.",
              "`pass` oznacza tylko brak flag tego audytu; nie zatwierdza oferty do treningu ani jako okazji.", "",
              "## Progi przeglądu", "", "| Pole | Minimum | Maksimum |", "| --- | ---: | ---: |"]
    for key, label in (("price", "Cena PLN"), ("area", "Metraż m²"), ("price_per_m2", "Cena PLN/m²")):
        lower = format(report["thresholds"][key + "_min"], ",.12g").replace(",", " ")
        upper = format(report["thresholds"][key + "_max"], ",.12g").replace(",", " ")
        lines.append(f"| {label} | {lower} | {upper} |")
    lines += ["", "Progi są umownymi regułami przeglądu. Poprawna oferta może znajdować się poza nimi.", "",
              "## Brakujące cechy", "", "| Źródło | Pole | Brakujące wiersze |", "| --- | --- | ---: |"]
    for source, missing in report["missing_by_source"].items():
        lines.extend(f"| {source} | {field} | {count} |" for field, count in missing.items())
    lines += ["", "Brakujące cechy pozostają nieznane; raport nie odgaduje ich wartości.", "",
              "## Flagi", "", "| Flaga | Wiersze |", "| --- | ---: |"]
    lines.extend(f"| {name} | {count} |" for name, count in report["flag_counts"].items())
    lines += ["", "## Możliwe powtórzenia pomiędzy portalami", "",
              f"Pary do sprawdzenia: **{matching['candidate_pairs']}**.",
              f"Wyszukiwanie skrócone przez limit: **{'tak' if matching['truncated'] else 'nie'}**.",
              "Podobieństwo dzielnicy, pokoi, metrażu i ceny nie potwierdza wspólnej nieruchomości.",
              "Pary pozostają w osobnym CSV; niczego nie łączymy i nie usuwamy.", "",
              "## Zakres", "", f"Dni obserwacji UTC w tym pliku: {report['observation_range']['utc_days']}.",
              "Eksport ostatniego stanu nie zastępuje pełnej historii cen ani przyszłego zbioru testowego.",
              "Wszystkie surowe wiersze i ich daty zostały zachowane. Baza i model pozostają bez zmian.", "",
              "Dokładne progi, braki według portalu, rozkłady liczb i ograniczenia są w `quality_report.json`.", ""]
    return "\n".join(lines)


def _write_outputs(annotated, candidates, report, directory, input_path):
    names = ("annotated_catalog.csv", "duplicate_candidates.csv", "quality_report.json", "quality_report.md")
    targets = [directory / name for name in names]
    if input_path.resolve() in {target.resolve() for target in targets}:
        raise ValueError("The input CSV must not be one of the output files.")
    directory.mkdir(parents=True, exist_ok=True)
    staged, backups, replaced, retained = [], {}, [], set()
    try:
        for path in targets:
            handle, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=directory)
            os.close(handle)
            staged.append(Path(temporary))
        annotated.to_csv(staged[0], index=False)
        candidates.to_csv(staged[1], index=False)
        staged[2].write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        staged[3].write_text(_markdown(report), encoding="utf-8")
        for target in targets:
            backups[target] = None
            if target.exists():
                handle, temporary = tempfile.mkstemp(prefix="." + target.name + ".backup.", dir=directory)
                os.close(handle)
                backup = Path(temporary)
                staged.append(backup)
                shutil.copy2(target, backup)
                backups[target] = backup
        try:
            for stage, target in zip(staged[:4], targets):
                os.replace(stage, target)
                replaced.append(target)
        except OSError:
            for target in reversed(replaced):
                try:
                    if backups[target] is None:
                        target.unlink(missing_ok=True)
                    else:
                        os.replace(backups[target], target)
                except OSError:
                    if backups[target] is not None:
                        retained.add(backups[target])
            raise
    finally:
        for temporary in staged:
            if temporary not in retained:
                temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Review a Warsaw catalogue CSV without modifying raw data or the database.")
    parser.add_argument("--input", type=Path, default=PROJECT_ROOT / "data/catalog_latest.csv")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "data/quality")
    parser.add_argument("--max-pairs", type=int, default=10_000)
    for field, default in asdict(QualityThresholds()).items():
        parser.add_argument("--" + field.replace("_", "-"), type=float, default=default)
    args = parser.parse_args(argv)
    try:
        targets = [args.output_dir / name for name in ("annotated_catalog.csv", "duplicate_candidates.csv", "quality_report.json", "quality_report.md")]
        if args.input.resolve() in {target.resolve() for target in targets}:
            raise ValueError("The input CSV must not be one of the output files.")
        limits = QualityThresholds(**{field: getattr(args, field) for field in asdict(QualityThresholds())})
        with args.input.open(encoding="utf-8-sig", newline="") as stream:
            header = next(csv.reader(stream), [])
        if len(header) != len(set(header)):
            raise ValueError("CSV contains duplicate column names.")
        before = hashlib.sha256(args.input.read_bytes()).hexdigest()
        raw = pd.read_csv(args.input, dtype="string", keep_default_na=False)
        annotated, report = audit_catalog(raw, thresholds=limits)
        from src.duplicates import find_duplicate_candidates

        candidates, metadata = find_duplicate_candidates(raw, max_pairs=args.max_pairs)
        after = hashlib.sha256(args.input.read_bytes()).hexdigest()
        if before != after:
            raise ValueError("Input changed during the audit; rerun on a stable snapshot.")
        report.update(generated_at=datetime.now(timezone.utc).isoformat(), input_sha256=before,
                      input_filename=args.input.name, duplicate_candidates=metadata)
        _write_outputs(annotated, candidates, report, args.output_dir, args.input)
    except (OSError, ValueError, pd.errors.ParserError) as error:
        parser.error(str(error))
    print(f"Audit: {len(annotated)} rows; {report['status_counts']}; {len(candidates)} possible duplicate pairs.")
    print(f"Raw data preserved. Reports: {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
