"""Read an existing model ranking and print eligible Warsaw offer links.

This viewer does not train, collect, query a database or write outputs. A price
gap is the difference from a model estimate; it is not a validated saving or
evidence that a property is still available.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
from decimal import Decimal
import math
from numbers import Integral, Real
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit

import numpy as np
import pandas as pd

from src.quality import RESERVED, audit_catalog
from src.listing_scope import rights_offer_flags

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = PROJECT_ROOT / "data" / "deals.csv"
SOURCES = {"www.olx.pl", "www.otodom.pl"}
REQUIRED_COLUMNS = {
    "source", "listing_id", "url", "city", "price_pln", "area_m2", "rooms",
    "observed_at", "predicted_price_pln", "is_deal", "seen_in_training", "partition",
}
PARTITIONS = {"train", "validation", "test", "unseen"}
OPTIONAL_MODEL_FIELDS = ("floor", "build_year", "latitude", "longitude", "distance_km", "rooms_min")


def _text(value):
    return " ".join(unicodedata.normalize("NFC", value).split()).casefold() if isinstance(value, str) else None


def _identity(source, identifier):
    source = _text(source)
    if source not in SOURCES or isinstance(identifier, (bool, np.bool_)):
        return None
    if isinstance(identifier, Integral):
        identifier = str(int(identifier))
    if not isinstance(identifier, str) or not re.fullmatch(r"[0-9]{1,200}", identifier.strip()):
        return None
    identifier = identifier.strip()
    return (source, identifier) if int(identifier) > 0 else None


def _utc(value):
    if not isinstance(value, (str, datetime, pd.Timestamp)):
        return None
    try:
        stamp = pd.Timestamp(value)
        if pd.isna(stamp) or stamp.tzinfo is None or stamp.utcoffset() is None:
            return None
        stamp = stamp.tz_convert("UTC")
        stamp.value  # Reject dates outside the supported nanosecond range.
        return stamp
    except (TypeError, ValueError, OverflowError):
        return None


def _boolean(value):
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, str):
        return {"true": True, "false": False, "1": True, "0": False}.get(value.strip().casefold())
    if isinstance(value, Real) and math.isfinite(value) and value in (0, 1):
        return bool(value)
    return None


def _positive(value):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (str, Real, Decimal)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _property_signature(row):
    """Compare optional model inputs semantically, including unknown values."""
    values = []
    for field in OPTIONAL_MODEL_FIELDS:
        value = row.get(field)
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            number = None
        values.append(number if number is not None and math.isfinite(number) else None)
    return tuple(values)


def _safe_url(value, source):
    if not isinstance(value, str) or re.search(r"[\x00-\x20\x7f-\x9f\\]", value):
        return False
    try:
        parsed = urlsplit(value)
        path_pattern = r"/d/oferta/[A-Za-z0-9_-]+\.html" if source == "www.olx.pl" else r"/pl/oferta/[A-Za-z0-9_-]+"
        return bool(
            parsed.scheme == "https" and parsed.netloc == source
            and parsed.username is None and parsed.password is None and parsed.port is None
            and not parsed.query and not parsed.fragment and "?" not in value and "#" not in value
            and re.fullmatch(path_pattern, parsed.path)
        )
    except ValueError:
        return False


def _config(top, max_age_days, discount_threshold, now):
    if type(top) is not int or not 1 <= top <= 50:
        raise ValueError("top must be an integer from 1 to 50.")
    if isinstance(max_age_days, (bool, np.bool_)) or not isinstance(max_age_days, Real) or not math.isfinite(max_age_days) or max_age_days <= 0:
        raise ValueError("max_age_days must be a finite positive number.")
    if isinstance(discount_threshold, (bool, np.bool_)) or not isinstance(discount_threshold, Real) or not math.isfinite(discount_threshold) or not 0 <= discount_threshold < 1:
        raise ValueError("discount_threshold must be a fraction from 0 to less than 1.")
    moment = _utc(datetime.now(timezone.utc) if now is None else now)
    if moment is None:
        raise ValueError("now must be a valid timezone-aware timestamp.")
    return moment


def select_deals(frame: pd.DataFrame, *, top=5, max_age_days=7,
                 discount_threshold=0.15, now=None):
    """Select reviewed, fresh, explicitly held-out model-gap candidates.

    The caller's dataframe is preserved. Exclusions are counted once per valid
    source/ID identity, at the first failing stage. Rows with an invalid identity
    are reported separately. Any unorderable time poisons that identity, so an
    older attractive score cannot substitute for an unknown latest observation.
    """
    moment = _config(top, max_age_days, discount_threshold, now)
    if not isinstance(frame, pd.DataFrame) or not frame.columns.is_unique:
        raise ValueError("Input must be a DataFrame with unique column names.")
    missing = sorted(REQUIRED_COLUMNS.difference(frame.columns))
    if missing:
        raise ValueError("Missing scoring columns: " + ", ".join(missing))
    raw = frame.reset_index(drop=True)
    grouped, invalid_time = defaultdict(list), set()
    invalid_identity_rows = 0
    for position, (source, identifier, observed) in enumerate(zip(raw.source, raw.listing_id, raw.observed_at)):
        identity = _identity(source, identifier)
        if identity is None:
            invalid_identity_rows += 1
            continue
        stamp = _utc(observed)
        grouped[identity].append((position, stamp))
        if stamp is None:
            invalid_time.add(identity)
    excluded, rights_flags, latest = Counter(), Counter(), {}
    older_rows = 0
    for identity in sorted(grouped):
        history = grouped[identity]
        if identity in invalid_time:
            excluded["unorderable_observation_time"] += 1
            continue
        newest = max(stamp for _, stamp in history)
        positions = [position for position, stamp in history if stamp == newest]
        older_rows += len(history) - len(positions)
        age_days = (int(moment.value) - int(newest.value)) / (86400 * 1000000000)
        if age_days < 0:
            excluded["future_observation"] += 1
        elif age_days > max_age_days:
            excluded["stale_observation"] += 1
        else:
            latest[identity] = positions
    positions = [position for identity in sorted(latest) for position in latest[identity]]
    flags_by_position = {}
    if positions:
        temporary = raw.iloc[positions].drop(columns=list(RESERVED), errors="ignore").copy(deep=True)
        temporary["source"] = temporary["source"].astype(object)
        temporary["listing_id"] = temporary["listing_id"].astype(object)
        # Source/ID normalization is confined to the audit copy. Raw listing
        # values and prior annotation columns are preserved in the returned rows.
        for identity, indexes in latest.items():
            temporary.loc[indexes, "source"] = identity[0]
            temporary.loc[indexes, "listing_id"] = identity[1]
        annotated, _ = audit_catalog(temporary)
        for position, value in zip(positions, annotated.quality_flags):
            flags_by_position[position] = set(value.split("|")) if value else set()
    selected_positions, discounts, gaps = [], {}, {}
    duplicate_latest_rows = 0
    for identity in sorted(latest):
        indexes = latest[identity]
        rows = [raw.iloc[position] for position in indexes]
        flags = set().union(*(flags_by_position[position] for position in indexes))
        if "conflicting_identity_timestamp" in flags:
            excluded["conflicting_latest_observation"] += 1
            continue
        # Repetition at one identical latest time is allowed after deduplication.
        # Every other audit flag still requires review and excludes the identity.
        if flags.difference({"repeated_identity"}):
            excluded["quality_not_pass"] += 1
            continue
        if len({_property_signature(row) for row in rows}) > 1:
            excluded["conflicting_latest_property_features"] += 1
            continue
        if any(not _safe_url(row.url, identity[0]) for row in rows):
            excluded["unsafe_or_noncanonical_url"] += 1
            continue
        indicated_rights = set().union(*(rights_offer_flags(row.url) for row in rows))
        if indicated_rights:
            excluded["nonstandard_rights_indicated"] += 1
            rights_flags.update(indicated_rights)
            continue
        provenance = [(_boolean(row.is_deal), _boolean(row.seen_in_training), _text(row.partition)) for row in rows]
        if any(deal is None or seen is None or partition not in PARTITIONS for deal, seen, partition in provenance):
            excluded["unknown_scoring_provenance"] += 1
            continue
        if any(seen != (partition == "train") for _, seen, partition in provenance):
            excluded["contradictory_scoring_provenance"] += 1
            continue
        predictions = [_positive(row.predicted_price_pln) for row in rows]
        if any(value is None for value in predictions):
            excluded["invalid_model_prediction"] += 1
            continue
        if len(set(zip(predictions, provenance))) > 1:
            excluded["conflicting_latest_score"] += 1
            continue
        deal, seen, partition = provenance[0]
        if seen or partition == "train":
            excluded["seen_in_training"] += 1
            continue
        if not deal:
            excluded["not_explicitly_flagged_as_deal"] += 1
            continue
        prediction = predictions[0]
        asking = _positive(rows[0].price_pln)
        model_gap = prediction - asking
        discount = model_gap / prediction
        if not math.isfinite(discount):
            excluded["invalid_model_gap"] += 1
            continue
        if discount < discount_threshold and not math.isclose(discount, discount_threshold, rel_tol=1e-12, abs_tol=0):
            excluded["below_discount_threshold"] += 1
            continue
        # Choose equivalent tied snapshots reproducibly, including when their
        # optional display fields have different whitespace or capitalization.
        chosen = min(indexes, key=lambda position: tuple(repr(raw.iloc[position][column]) for column in sorted(raw.columns)))
        duplicate_latest_rows += len(indexes) - 1
        selected_positions.append(chosen)
        discounts[chosen], gaps[chosen] = discount, model_gap
    ranked = sorted(selected_positions, key=lambda position: (
        -discounts[position], -gaps[position],
        _identity(raw.iloc[position].source, raw.iloc[position].listing_id),
    ))
    shown = ranked[:top]
    result = raw.iloc[shown].copy(deep=True)
    result["discount"] = [discounts[position] for position in shown]
    result["model_gap_pln"] = [gaps[position] for position in shown]
    result = result.reset_index(drop=True)
    report = {
        "input_rows": len(raw), "unique_listings": len(grouped),
        "eligible_listings": len(ranked), "shown_listings": len(result),
        "excluded_counts": dict(sorted(excluded.items())),
        "excluded_counts_unit": "unique supported source/ID identities; first failing stage only",
        "invalid_identity_rows": invalid_identity_rows,
        "older_history_rows_ignored": older_rows,
        "equivalent_latest_rows_deduplicated": duplicate_latest_rows,
        "threshold_fraction": float(discount_threshold), "max_age_days": float(max_age_days),
        "as_of": moment.isoformat(), "supported_sources": sorted(SOURCES),
        "not_shown_due_to_top": len(ranked) - len(result),
        "stored_quality_annotations_recomputed": bool(set(RESERVED).intersection(raw.columns)),
        "rights_filter": {
            "method": "decoded Polish-normalized URL slug indicators",
            "excluded_listings": int(excluded["nonstandard_rights_indicated"]),
            "flag_counts": dict(sorted(rights_flags.items())),
            "interpretation": "review indicators of fractional ownership or housing participation; not legal conclusions",
            "limitations": "slug only; descriptions and legal documents not checked; missing indicators do not establish whole-apartment ownership",
            "ancillary_share_policy": "plot, access-road and garden shares alone do not imply a fractional apartment",
        },
        "interpretation": "model price-gap candidates; no validated saving, availability or transaction value",
    }
    return result, report


def _display(value, fallback="unknown"):
    """Remove terminal control sequences and collapse untrusted display text."""
    if value is None or (not isinstance(value, (list, dict, tuple)) and pd.isna(value)):
        return fallback
    text = str(value)
    text = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = "".join(" " if unicodedata.category(char).startswith("C") else char for char in text)
    return " ".join(text.split()) or fallback


def format_deals(selected, report):
    """Format plain terminal links without implying guaranteed savings."""
    lines = ["Offers ranked by model price gap"]
    if "ranking_status" in selected and selected.ranking_status.astype("string").str.casefold().eq("experimental").any():
        lines.append("Ranking status: EXPERIMENTAL")
    lines.append(f"Showing {len(selected)} of {report.get('eligible_listings', len(selected))} eligible offers")
    if selected.empty:
        lines.append("No eligible offers under the current filters.")
        return "\n".join(lines)
    lines.append("Model estimates may be wrong; gaps do not establish savings or current availability.")
    for number, (_, row) in enumerate(selected.iterrows(), 1):
        district = _display(row.get("district"))
        lines.extend([
            f"{number}. {district} | {float(row.area_m2):g} m2 | {int(float(row.rooms))} rooms",
            f"   Asking: {float(row.price_pln):,.2f} PLN | Model estimate: {float(row.predicted_price_pln):,.2f} PLN",
            f"   Model price gap: {float(row.discount) * 100:.2f}% ({float(row.model_gap_pln):,.2f} PLN)",
            f"   Last observed: {_display(row.observed_at)}",
            f"   {_display(row.url)}",
        ])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Print fresh, reviewed offer links from an existing model ranking.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--max-age-days", type=float, default=7)
    parser.add_argument("--discount-threshold", type=float, default=15,
                        help="Minimum model price gap in percent, from 0 to less than 100.")
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code)
    try:
        if not math.isfinite(args.discount_threshold) or not 0 <= args.discount_threshold < 100:
            raise ValueError("--discount-threshold must be from 0 to less than 100 percent.")
        _config(args.top, args.max_age_days, args.discount_threshold / 100, None)
        if not args.input.is_file():
            print(f"No ranked offers yet. Expected: {_display(args.input)}")
            print("Generate and validate the model/ranking first; the collected catalogue is not an offer ranking.")
            return 2
        try:
            with args.input.open("r", encoding="utf-8-sig", newline="") as stream:
                header = next(csv.reader(stream, strict=True), None)
                if header is None:
                    raise ValueError("Ranked-offers CSV is empty.")
                if len(header) != len(set(header)):
                    raise ValueError("Ranked-offers CSV contains duplicate column names.")
                stream.seek(0)
                frame = pd.read_csv(stream, dtype={"source": "string", "listing_id": "string"})
        except (OSError, UnicodeError, csv.Error, pd.errors.ParserError, pd.errors.EmptyDataError):
            print("Could not read ranked-offers CSV. Check the path and CSV format.")
            return 2
        selected, report = select_deals(frame, top=args.top, max_age_days=args.max_age_days,
                                        discount_threshold=args.discount_threshold / 100)
        print(format_deals(selected, report))
        return 0
    except (ValueError, OSError) as error:
        message = str(error) if isinstance(error, ValueError) else "Could not access the ranked-offers file."
        print("Cannot show ranked offers: " + _display(message))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
