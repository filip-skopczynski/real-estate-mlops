"""Clean Warsaw listing observations and produce leakage-safe CSV snapshots."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy.exc import SQLAlchemyError


PROJECT_DIR = Path(__file__).resolve().parents[1]
IDENTITY_COLUMNS = ["source", "listing_id"]
REQUIRED_COLUMNS = IDENTITY_COLUMNS + [
    "city", "price_pln", "area_m2", "rooms", "observed_at"
]
NUMERIC_COLUMNS = [
    "price_pln", "area_m2", "rooms", "floor", "build_year",
    "latitude", "longitude", "distance_km",
]
WARSAW_CENTRE = (52.2297, 21.0122)


def haversine_km(latitude, longitude):
    """Great-circle distance to the Warsaw reference point, in kilometres."""
    lat = np.radians(np.asarray(latitude, dtype=float))
    lon = np.radians(np.asarray(longitude, dtype=float))
    centre = (
        float(os.getenv("WARSAW_CENTER_LAT", str(WARSAW_CENTRE[0]))),
        float(os.getenv("WARSAW_CENTER_LON", str(WARSAW_CENTRE[1]))),
    )
    if not np.isfinite(centre).all() or not -90 <= centre[0] <= 90 or not -180 <= centre[1] <= 180:
        raise ValueError("Warsaw reference coordinates must be finite valid latitude/longitude.")
    centre_lat, centre_lon = np.radians(centre)
    term = (
        np.sin((lat - centre_lat) / 2) ** 2
        + np.cos(lat) * np.cos(centre_lat) * np.sin((lon - centre_lon) / 2) ** 2
    )
    return 6371.0088 * 2 * np.arcsin(np.sqrt(np.clip(term, 0, 1)))


def clean_listings(frame: pd.DataFrame, keep: str = "earliest") -> pd.DataFrame:
    """Validate observations and keep one observation per source/listing ID.

    Earliest observations are used for training, so later price changes cannot
    move an already seen listing into a validation or test period. Latest
    observations are used for current candidate scoring. Cross-source relisted
    properties cannot be identified reliably with source/listing IDs alone.
    """
    if keep not in {"earliest", "latest"}:
        raise ValueError("keep must be 'earliest' or 'latest'.")
    if not isinstance(frame, pd.DataFrame):
        raise ValueError("Input must be a pandas DataFrame.")
    if frame.columns.duplicated().any():
        raise ValueError("Input contains duplicate column names.")
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError("Missing listing columns: " + ", ".join(missing))

    cleaned = frame.copy()
    for column in ["url", "district"]:
        if column not in cleaned:
            cleaned[column] = np.nan
    for column in NUMERIC_COLUMNS:
        if column not in cleaned:
            cleaned[column] = np.nan
        cleaned[column] = pd.to_numeric(cleaned[column], errors="coerce").astype(float)
        cleaned[column] = cleaned[column].where(np.isfinite(cleaned[column]), np.nan)

    for column in IDENTITY_COLUMNS:
        cleaned[column] = cleaned[column].astype("string").str.strip()
    cleaned["city"] = cleaned["city"].astype("string").str.strip()
    cleaned["observed_at"] = pd.to_datetime(
        cleaned["observed_at"], errors="coerce", utc=True, format="mixed"
    )
    valid = (
        cleaned["city"].str.casefold().isin(["warsaw", "warszawa"])
        & cleaned["source"].notna() & cleaned["source"].ne("")
        & cleaned["listing_id"].notna() & cleaned["listing_id"].ne("")
        & cleaned["observed_at"].notna()
        & cleaned["price_pln"].gt(0) & cleaned["area_m2"].gt(0)
        & cleaned["rooms"].gt(0) & cleaned["rooms"].mod(1).eq(0)
    )
    if "transaction_type" in cleaned:
        transaction = cleaned["transaction_type"].astype("string").str.strip().str.casefold()
        valid &= transaction.isin(["sale", "sell", "for_sale", "sprzedaż", "sprzedaz"])
    cleaned = cleaned.loc[valid].copy()
    cleaned["city"] = "Warszawa"
    cleaned["rooms"] = cleaned["rooms"].astype(int)

    # Invalid optional coordinates become missing values for the train-only imputer.
    cleaned["latitude"] = cleaned["latitude"].where(cleaned["latitude"].between(-90, 90))
    cleaned["longitude"] = cleaned["longitude"].where(cleaned["longitude"].between(-180, 180))
    cleaned["distance_km"] = cleaned["distance_km"].where(cleaned["distance_km"].ge(0))
    can_compute = (
        cleaned["distance_km"].isna()
        & cleaned["latitude"].notna() & cleaned["longitude"].notna()
    )
    cleaned.loc[can_compute, "distance_km"] = haversine_km(
        cleaned.loc[can_compute, "latitude"], cleaned.loc[can_compute, "longitude"]
    )
    district = cleaned["district"].astype("string").str.strip().replace("", pd.NA)
    cleaned["district"] = district.astype(object).where(district.notna(), np.nan)

    cleaned = cleaned.sort_values("observed_at", kind="stable")
    cleaned = cleaned.drop_duplicates(
        IDENTITY_COLUMNS, keep="first" if keep == "earliest" else "last"
    ).reset_index(drop=True)
    # Useful for exploration; never supplied to the model because it contains the target.
    cleaned["price_per_m2"] = cleaned["price_pln"] / cleaned["area_m2"]
    cleaned.attrs["cleaning"] = {
        "input_observations": len(frame),
        "valid_observations": int(valid.sum()),
        "unique_listings": len(cleaned),
        "deduplication": keep,
    }
    return cleaned


def main() -> None:
    load_dotenv(PROJECT_DIR / ".env")
    parser = argparse.ArgumentParser(description="Clean Warsaw listing observations.")
    parser.add_argument("--input", type=Path, help="Raw CSV; default: database observations.")
    parser.add_argument("--output", type=Path, default=PROJECT_DIR / "data/training.csv")
    parser.add_argument("--latest-output", type=Path, default=PROJECT_DIR / "data/latest.csv")
    args = parser.parse_args()
    engine = None
    try:
        if args.input is not None:
            raw = pd.read_csv(args.input, dtype={"source": "string", "listing_id": "string"})
            latest_raw = raw
        else:
            from src.database import get_engine, read_current_listings, read_observations

            engine = get_engine()
            raw = read_observations(engine)
            latest_raw = read_current_listings(engine)
        training = clean_listings(raw, keep="earliest")
        latest = clean_listings(latest_raw, keep="latest")
        if training.empty:
            raise ValueError("No valid Warsaw listings remain after cleaning.")
        for target, frame in [(args.output, training), (args.latest_output, latest)]:
            target.parent.mkdir(parents=True, exist_ok=True)
            frame.to_csv(target, index=False)
    except SQLAlchemyError:
        parser.error("Could not read the listing database. Check connection settings and database availability.")
    except (OSError, ValueError) as error:
        parser.error(str(error))
    finally:
        if engine is not None:
            engine.dispose()
    print(f"Training: {len(training)} earliest listings -> {args.output}")
    print(f"Current: {len(latest)} latest listings -> {args.latest_output}")


if __name__ == "__main__":
    main()
