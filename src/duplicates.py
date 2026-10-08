"""Bounded, offline cross-portal similarity candidates for manual review.

Matching advertised prices and features cannot establish property identity:
different flats in one development can share all these attributes. This module
never removes rows, confirms duplicates, creates transitive groups, or produces
groups for train/test splitting. Price similarity would leak the target there.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
import math
from numbers import Integral, Real
import re
import unicodedata
from urllib.parse import urlsplit

import pandas as pd

SOURCES = ("www.olx.pl", "www.otodom.pl")
MAX_PAIRS = 10000
MAX_COMPARISONS = 1000000
AREA_ABSOLUTE_TOLERANCE = 0.1
AREA_RELATIVE_TOLERANCE = 0.005
PRICE_RELATIVE_TOLERANCE = 0.01
CANDIDATE_COLUMNS = (
    "left_source", "left_listing_id", "left_url", "right_source", "right_listing_id",
    "right_url", "district", "rooms", "left_area_m2", "right_area_m2",
    "left_price_pln", "right_price_pln", "area_difference_m2",
    "price_difference_fraction", "floor_evidence", "evidence", "status",
)
REQUIRED_COLUMNS = {"source", "listing_id", "price_pln", "area_m2", "rooms", "observed_at"}


def _text(value):
    if not isinstance(value, str):
        return None
    normalized = " ".join(unicodedata.normalize("NFC", value).split()).casefold()
    return normalized if normalized and normalized not in {"nan", "none", "null", "unknown", "brak", "-"} else None


def _number(value):
    """Accept numeric scalars or plain decimal text, never currency/exponents."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value.strip()):
            return None
        value = value.strip()
    elif not isinstance(value, (Real, Decimal)):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _integer(value, *, signed=False):
    if signed and isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,200}", value.strip()):
        return int(value.strip())
    number = _number(value)
    return int(number) if number is not None and number.is_integer() else None


def _identity(source, identifier):
    source = _text(source)
    if source not in SOURCES:
        return None, "unsupported_source"
    if isinstance(identifier, bool):
        return None, "invalid_listing_id"
    if isinstance(identifier, Integral):
        identifier = str(int(identifier))
    if not isinstance(identifier, str) or not re.fullmatch(r"[0-9]{1,200}", identifier.strip()):
        return None, "invalid_listing_id"
    identifier = identifier.strip()
    # Identifiers are opaque strings within the source namespace. Positivity
    # validates the portal format; leading zeros never rewrite an identity.
    return ((source, identifier), None) if int(identifier) > 0 else (None, "invalid_listing_id")


def _moment(value):
    if not isinstance(value, (str, datetime, pd.Timestamp)):
        return None
    try:
        timestamp = pd.Timestamp(value)
        if pd.isna(timestamp) or timestamp.tzinfo is None or timestamp.utcoffset() is None:
            return None
        timestamp = timestamp.tz_convert("UTC")
        # Keep nanosecond ties exact and reject dates outside pandas' range.
        return timestamp.value
    except (ValueError, TypeError, OverflowError):
        return None


def _url(value, source):
    if not isinstance(value, str) or re.search(r"[\x00-\x20\x7f\\]", value):
        return None
    try:
        parsed = urlsplit(value)
        pattern = r"/d/oferta/[^/]+\.html" if source == SOURCES[0] else r"/pl/oferta/[^/]+"
        valid = parsed.scheme == "https" and parsed.netloc == source and not parsed.query and not parsed.fragment and re.fullmatch(pattern, parsed.path)
    except ValueError:
        return None
    return value if valid else None


def _features(row):
    """Normalize match inputs without changing the caller's dataframe."""
    city = _text(row.get("city"))
    if city == "warsaw":
        city = "warszawa"
    raw_floor = row.get("floor")
    floor = _integer(raw_floor, signed=True)
    floor_missing = raw_floor is None or raw_floor is pd.NA or raw_floor is pd.NaT
    if isinstance(raw_floor, str) and not raw_floor.strip():
        floor_missing = True
    if isinstance(raw_floor, Real) and not isinstance(raw_floor, bool):
        try:
            floor_missing = floor_missing or math.isnan(float(raw_floor))
        except (ValueError, OverflowError):
            pass
    return {
        "city": city, "district": _text(row.get("district")),
        "price": _number(row.get("price_pln")), "area": _number(row.get("area_m2")),
        "rooms": _integer(row.get("rooms")), "rooms_min": _integer(row.get("rooms_min")),
        "floor": floor, "floor_invalid": floor is None and not floor_missing,
    }


def _within(difference, tolerance):
    # Decimal CSV values have binary floating-point representation error at
    # exact boundaries, e.g. 50.1 - 50.0. This allowance is numerical only.
    return difference <= tolerance or math.isclose(difference, tolerance, rel_tol=1e-12, abs_tol=0.0)


def find_duplicate_candidates(frame: pd.DataFrame, *, max_pairs: int = 10000):
    """Return similar cross-source listing pairs and bounded-search metadata.

    A source/ID's latest timezone-aware observation is used. Conflicting match
    features at that exact latest time exclude the identity as ambiguous. A
    missing district excludes an identity; an unknown floor is weaker evidence
    and remains unknown. The comparison budget bounds dense matching windows.
    """
    if not isinstance(frame, pd.DataFrame):
        raise ValueError("Duplicate candidate input must be a pandas DataFrame.")
    if type(max_pairs) is not int or not 1 <= max_pairs <= MAX_PAIRS:
        raise ValueError("max_pairs must be an integer from 1 to 10000.")
    if not frame.columns.is_unique:
        raise ValueError("Duplicate candidate columns must be unique.")
    missing = REQUIRED_COLUMNS.difference(frame.columns)
    if missing:
        raise ValueError("Missing duplicate candidate columns: " + ", ".join(sorted(missing)))
    counts = Counter()
    identities, latest = set(), {}
    columns = list(frame.columns)
    for values in frame.itertuples(index=False, name=None):
        row = dict(zip(columns, values))
        identity, error = _identity(row.get("source"), row.get("listing_id"))
        if error:
            counts[error + "_rows"] += 1
            continue
        identities.add(identity)
        moment = _moment(row.get("observed_at"))
        if moment is None:
            counts["invalid_timestamp_rows"] += 1
            continue
        feature = _features(row)
        signature = tuple(feature.items())
        url = _url(row.get("url"), identity[0])
        prior = latest.get(identity)
        if prior is None or moment > prior["moment"]:
            if prior is not None:
                counts["older_history_rows"] += prior["rows"]
            latest[identity] = {"moment": moment, "features": feature, "signatures": {signature},
                                "urls": {url} if url else set(), "rows": 1}
        elif moment == prior["moment"]:
            prior["signatures"].add(signature)
            prior["rows"] += 1
            if url:
                prior["urls"].add(url)
        else:
            counts["older_history_rows"] += 1
    counts["identities_without_valid_timestamp"] = len(identities.difference(latest))
    blocks = defaultdict(lambda: {source: [] for source in SOURCES})
    ambiguous = eligible = 0
    for identity in sorted(latest):
        item = latest[identity]
        if len(item["signatures"]) != 1:
            ambiguous += 1
            counts["ambiguous_latest_match_features"] += 1
            continue
        counts["repeated_latest_rows"] += item["rows"] - 1
        feature = item["features"]
        reasons = []
        if feature["city"] != "warszawa":
            reasons.append("missing_or_non_warsaw_city")
        if feature["district"] is None:
            reasons.append("unknown_district")
        for key in ("price", "area"):
            if feature[key] is None or feature[key] <= 0:
                reasons.append("invalid_" + key)
        if feature["rooms"] is None or feature["rooms"] <= 0:
            reasons.append("unknown_exact_rooms")
        elif feature["rooms_min"] is not None and feature["rooms_min"] > feature["rooms"]:
            reasons.append("contradictory_rooms_min")
        if reasons:
            counts.update(reasons)
            continue
        eligible += 1
        record = {**feature, "source": identity[0], "id": identity[1],
                  "url": min(item["urls"]) if item["urls"] else None}
        if record["floor"] is None:
            counts["unknown_floor_allowed"] += 1
        if record["floor_invalid"]:
            counts["invalid_floor_treated_unknown"] += 1
        if record["url"] is None:
            counts["url_unavailable"] += 1
        blocks[(record["district"], record["rooms"])][identity[0]].append(record)
    pairs, comparisons, truncated, termination = [], 0, False, "search_exhausted"
    for block in sorted(blocks):
        if truncated:
            break
        left = sorted(blocks[block][SOURCES[0]], key=lambda row: (row["area"], row["id"]))
        right = sorted(blocks[block][SOURCES[1]], key=lambda row: (row["area"], row["id"]))
        lower_pointer = upper_pointer = 0
        for first in left:
            if truncated:
                break
            area = first["area"]
            window = max(AREA_ABSOLUTE_TOLERANCE, AREA_RELATIVE_TOLERANCE * area)
            # The lower window is conservative; the exact smaller-area rule is
            # checked below. No Cartesian pair table is ever materialized.
            while lower_pointer < len(right) and right[lower_pointer]["area"] < area - window - 1e-9:
                lower_pointer += 1
            upper_pointer = max(upper_pointer, lower_pointer)
            while upper_pointer < len(right) and right[upper_pointer]["area"] <= area + window + 1e-9:
                upper_pointer += 1
            for index in range(lower_pointer, upper_pointer):
                if len(pairs) >= max_pairs:
                    truncated, termination = True, "pair_budget"
                    break
                if comparisons >= MAX_COMPARISONS:
                    truncated, termination = True, "comparison_budget"
                    break
                second = right[index]
                comparisons += 1
                area_difference = abs(area - second["area"])
                tolerance = max(AREA_ABSOLUTE_TOLERANCE, AREA_RELATIVE_TOLERANCE * min(area, second["area"]))
                if not _within(area_difference, tolerance):
                    counts["pairs_outside_area_tolerance"] += 1
                    continue
                first_floor, second_floor = first["floor"], second["floor"]
                if first_floor is not None and second_floor is not None and first_floor != second_floor:
                    counts["pairs_with_conflicting_known_floor"] += 1
                    continue
                price_difference = abs(first["price"] - second["price"])
                lower_price = min(first["price"], second["price"])
                if not _within(price_difference, PRICE_RELATIVE_TOLERANCE * lower_price):
                    counts["pairs_outside_price_tolerance"] += 1
                    continue
                pairs.append({
                    "left_source": first["source"], "left_listing_id": first["id"], "left_url": first["url"],
                    "right_source": second["source"], "right_listing_id": second["id"], "right_url": second["url"],
                    "district": block[0], "rooms": block[1], "left_area_m2": area, "right_area_m2": second["area"],
                    "left_price_pln": first["price"], "right_price_pln": second["price"],
                    "area_difference_m2": area_difference, "price_difference_fraction": price_difference / lower_price,
                    "floor_evidence": "both_known_equal" if first_floor is not None and second_floor is not None else "unknown",
                    "evidence": "similar_district_rooms_area_price", "status": "candidate_needs_review",
                })
    result = pd.DataFrame(pairs, columns=CANDIDATE_COLUMNS)
    if not result.empty:
        result = result.sort_values(["left_source", "left_listing_id", "right_source", "right_listing_id"], kind="stable").reset_index(drop=True)
    metadata = {
        "method": "cross_source_exact_district_rooms_sorted_area_window",
        "supported_sources": list(SOURCES), "input_rows": len(frame),
        "unique_listings": len(identities), "eligible_listings": eligible,
        "candidate_pairs": len(result), "truncated": truncated,
        "comparisons": comparisons, "comparison_budget": MAX_COMPARISONS,
        "max_pairs": max_pairs, "termination": termination,
        "ambiguous_identity_count": ambiguous, "counts_by_reason": dict(sorted(counts.items())),
        "thresholds": {"area_absolute_m2": AREA_ABSOLUTE_TOLERANCE,
                       "area_relative_smaller": AREA_RELATIVE_TOLERANCE,
                       "price_relative_smaller": PRICE_RELATIVE_TOLERANCE},
        "district_matching": "entire NFC/casefold/whitespace-normalized value; no fuzzy or prefix matching",
        "recency": "latest timezone-aware valid timestamp per source/ID; conflicting match features at latest ties excluded",
        "identity_namespace": "source/ID; equal numeric IDs in different sources are independent identities",
        "city_aliases": {"warsaw": "warszawa"},
        "optional_missing_columns": sorted({"city", "district", "floor", "rooms_min", "url"}.difference(frame.columns)),
        "interpretation": "similarity candidates requiring review; no confirmed duplicates, removals or transitive property groups",
        "training_split_use": "unsupported: price-based similarity must not define train/test groups",
    }
    return result, metadata
