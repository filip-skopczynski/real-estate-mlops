"""Generate reproducible fictional observations; never use as market evidence."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def demo_observations(seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    start = pd.Timestamp("2026-09-01T08:00:00Z")
    districts = ["Mokotów", "Wola", "Praga-Południe", "Ursynów", "Śródmieście"]
    district_prices = [16500, 17000, 14500, 15000, 22000]
    rows = []
    for index in range(180):
        district_index = int(rng.integers(len(districts)))
        area = round(float(rng.uniform(25, 110)), 2)
        distance = round(float(rng.uniform(0.5, 14)), 2)
        rooms = min(5, max(1, int(area // 23)))
        price = round(area * (district_prices[district_index] - 180 * distance)
                      + rooms * 12000 + float(rng.normal(0, 35000)), 2)
        rows.append({
            "source": "fictional-demo", "listing_id": f"demo-{index:04d}",
            "url": f"https://demo.invalid/apartment/{index}", "city": "Warszawa",
            "district": districts[district_index], "price_pln": price,
            "area_m2": area, "rooms": rooms, "floor": int(rng.integers(0, 12)),
            "build_year": int(rng.integers(1960, 2025)),
            "latitude": None, "longitude": None, "distance_km": distance,
            "observed_at": (start + pd.Timedelta(days=index // 6)).isoformat(),
        })
    # Later observations of existing apartments test history and deduplication.
    for index in range(0, 180, 9):
        updated = dict(rows[index])
        updated["observed_at"] = (start + pd.Timedelta(days=31)).isoformat()
        updated["price_pln"] = round(updated["price_pln"] * 0.75, 2)
        rows.append(updated)
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/demo_observations.csv"))
    parser.add_argument("--sqlite", type=Path, help="Also insert into a local SQLite demo database")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    frame = demo_observations()
    frame.to_csv(args.output, index=False)
    print(f"Created {len(frame)} FICTIONAL observations: {args.output}")
    if args.sqlite:
        from src.database import get_engine, init_db, upsert_listings

        args.sqlite.parent.mkdir(parents=True, exist_ok=True)
        engine = get_engine("sqlite:///" + args.sqlite.resolve().as_posix())
        try:
            init_db(engine)
            inserted = upsert_listings(engine, frame.to_dict(orient="records"))
            print(f"Inserted {inserted} new fictional observations into local SQLite")
        finally:
            engine.dispose()


if __name__ == "__main__":
    main()
