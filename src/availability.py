"""Atomic full-catalogue snapshots and availability history.

An absent apartment means ``missing``, never ``sold``. Unavailable apartments
can have an availability record without any price record.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any

import pandas as pd
from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, Integer, String, Table, func, select, text
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from . import database


STATUSES = ("available", "reserved", "sold", "missing")
REPORTED_STATUSES = set(STATUSES) - {"missing"}

inventory_snapshots = Table(
    "inventory_snapshots",
    database.metadata,
    Column("source", String(80), primary_key=True),
    Column("scope", String(200), primary_key=True),
    Column("observed_at", DateTime(timezone=True), primary_key=True),
    Column("listing_id_prefix", String(200), nullable=False),
    Column("snapshot_hash", String(64), nullable=False),
    Column("prices_complete", Boolean, nullable=False),
    Column("apartment_count", Integer, nullable=False),
    Column("missing_count", Integer, nullable=False),
    CheckConstraint("length(scope) BETWEEN 1 AND 200"),
    CheckConstraint("length(listing_id_prefix) BETWEEN 1 AND 200"),
    CheckConstraint("length(snapshot_hash) = 64"),
    CheckConstraint("apartment_count > 0 AND missing_count >= 0"),
)


def _availability_columns(*, history: bool):
    return [
        Column("source", String(80), primary_key=True),
        Column("listing_id", String(200), primary_key=True),
        Column("scope", String(200), nullable=False),
        Column("status", String(10), nullable=False),
        Column("observed_at", DateTime(timezone=True), primary_key=history, nullable=False),
        Column("last_seen_at", DateTime(timezone=True), nullable=False),
        CheckConstraint("status IN ('available', 'reserved', 'sold', 'missing')"),
        CheckConstraint("length(scope) BETWEEN 1 AND 200"),
    ]


listing_availability = Table(
    "listing_availability", database.metadata, *_availability_columns(history=False)
)
listing_availability_observations = Table(
    "listing_availability_observations", database.metadata, *_availability_columns(history=True)
)


def _identifier(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ValueError(f"{name} must be a nonempty string of at most {limit} characters.")
    return value.strip()


def _stored_utc(value: datetime) -> datetime:
    # SQLite DateTime strips tzinfo; stored values are always normalized UTC.
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return database._utc_timestamp(value)


def _advisory_key(parts: list[str]) -> int:
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big", signed=True)


def _counts(records) -> dict[str, int]:
    counts = dict.fromkeys(STATUSES, 0)
    for row in records:
        counts[row["status"]] += 1
    return counts


def save_inventory_snapshot(
    engine: Engine,
    rows: list[dict[str, Any]],
    availability: list[dict[str, Any]],
    *,
    source: str,
    scope: str,
    listing_id_prefix: str,
    observed_at: datetime,
    complete: bool = False,
    prices_complete: bool = True,
) -> dict[str, Any]:
    """Save a verified complete residential catalogue and verified price rows.

    Identical snapshots are harmless replays, including older snapshots. Replay
    counts describe the current scope state. A new older snapshot, a conflicting
    replay or a partial catalogue fails without changing prices or availability.
    ``last_seen_at`` means last presence in the catalogue, including sold/reserved.
    Catalogue completeness is independent of price completeness. By default,
    every available apartment requires a price. With prices_complete=False,
    prices may cover a subset (including none); retained prices remain intact.
    """
    if complete is not True:
        raise ValueError("A complete residential catalogue is required (complete=True).")
    if type(prices_complete) is not bool:
        raise ValueError("prices_complete must be exactly True or False.")
    source = _identifier(source, "source", 80)
    scope = _identifier(scope, "scope", 200)
    prefix = _identifier(listing_id_prefix, "listing_id_prefix", 200)
    timestamp = database._utc_timestamp(observed_at)
    if not isinstance(rows, list) or not isinstance(availability, list) or not availability:
        raise ValueError("rows must be a list and availability must be a nonempty list.")

    reported = {}
    for item in availability:
        if not isinstance(item, dict) or not {"listing_id", "status"}.issubset(item):
            raise ValueError("Each availability record requires listing_id and status.")
        listing_id = _identifier(item["listing_id"], "listing_id", 200)
        status = item["status"]
        if not isinstance(status, str) or status not in REPORTED_STATUSES:
            raise ValueError("Reported status must be available, reserved or sold; missing is inferred.")
        if listing_id in reported or not listing_id.startswith(prefix):
            raise ValueError("Availability identifiers must be unique and match listing_id_prefix.")
        if "source" in item and item["source"] != source:
            raise ValueError("Availability source does not match the snapshot.")
        if "scope" in item and item["scope"] != scope:
            raise ValueError("Availability scope does not match the snapshot.")
        if "observed_at" in item and database._utc_timestamp(item["observed_at"]) != timestamp:
            raise ValueError("Availability observed_at does not match the snapshot.")
        reported[listing_id] = status

    normalized = []
    row_ids = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Each price record must be a dictionary.")
        item = database._normalize_listing(row)
        listing_id = _identifier(item["listing_id"], "listing_id", 200)
        if item["source"] != source or not listing_id.startswith(prefix):
            raise ValueError("Price source or listing_id_prefix does not match the snapshot.")
        if item["observed_at"] != timestamp or ("scope" in row and row["scope"] != scope):
            raise ValueError("Price observed_at or scope does not match the snapshot.")
        if listing_id in row_ids:
            raise ValueError("Duplicate price listing_id in the snapshot.")
        if (
            item["price_pln"] <= 0 or item["area_m2"] <= 0
            or (item["rooms"] is not None and item["rooms"] <= 0)
            or (item["distance_km"] is not None and item["distance_km"] < 0)
        ):
            raise ValueError("Price, area and rooms must be positive; distance cannot be negative.")
        row_ids.add(listing_id)
        normalized.append(item)
    available_ids = {listing_id for listing_id, status in reported.items() if status == "available"}
    if prices_complete and available_ids != row_ids:
        raise ValueError("Available apartment identifiers must exactly match the verified price identifiers.")
    if not prices_complete and not row_ids.issubset(available_ids):
        raise ValueError("Verified price identifiers must be a subset of available apartment identifiers.")

    # Sorting removes input-order differences; normalized numbers and UTC dates
    # make equivalent input values produce the same snapshot hash.
    price_payload = [
        {**item, "observed_at": item["observed_at"].isoformat()}
        for item in sorted(normalized, key=lambda item: item["listing_id"])
    ]
    payload = {
        "source": source, "scope": scope, "listing_id_prefix": prefix,
        "prices_complete": prices_complete,
        "observed_at": timestamp.isoformat(), "rows": price_payload,
        "availability": sorted(reported.items()),
    }
    snapshot_hash = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    try:
        dialect_insert = {"postgresql": postgres_insert, "sqlite": sqlite_insert}[engine.dialect.name]
    except KeyError:
        raise ValueError("Inventory snapshots support PostgreSQL and SQLite.") from None

    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            # The source lock also serializes prefix allocation between scopes.
            # Always acquire source before scope to keep the lock order consistent.
            for lock_parts in (["inventory-source", source], ["inventory-scope", source, scope]):
                connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _advisory_key(lock_parts)})

        header = {
            "source": source, "scope": scope, "observed_at": timestamp,
            "listing_id_prefix": prefix, "snapshot_hash": snapshot_hash,
            "prices_complete": prices_complete,
            "apartment_count": len(reported), "missing_count": 0,
        }
        # SQLite must acquire its write lock BEFORE any old-state reads. Even a
        # conflicting INSERT does this; the whole transaction rolls back on error.
        inserted = connection.execute(
            dialect_insert(inventory_snapshots).values(**header).on_conflict_do_nothing(
                index_elements=["source", "scope", "observed_at"]
            )
        ).rowcount
        if inserted == 0:
            existing_hash = connection.scalar(
                select(inventory_snapshots.c.snapshot_hash).where(
                    inventory_snapshots.c.source == source,
                    inventory_snapshots.c.scope == scope,
                    inventory_snapshots.c.observed_at == timestamp,
                )
            )
            if existing_hash != snapshot_hash:
                raise ValueError("The same snapshot timestamp already has different contents.")
            current = connection.execute(select(listing_availability).where(
                listing_availability.c.source == source, listing_availability.c.scope == scope
            )).mappings().all()
            return {"new_observations": 0, "availability_counts": _counts(current), "snapshot_replayed": True}

        latest = connection.scalar(select(func.max(inventory_snapshots.c.observed_at)).where(
            inventory_snapshots.c.source == source, inventory_snapshots.c.scope == scope
        ))
        if _stored_utc(latest) > timestamp:
            raise ValueError("A new inventory snapshot cannot be older than the latest snapshot.")

        scope_prefixes = connection.execute(select(
            inventory_snapshots.c.scope, inventory_snapshots.c.listing_id_prefix
        ).where(inventory_snapshots.c.source == source).distinct()).all()
        for old_scope, old_prefix in scope_prefixes:
            if old_scope == scope and old_prefix != prefix:
                raise ValueError("listing_id_prefix must remain stable for a scope.")
            if old_scope != scope and (prefix.startswith(old_prefix) or old_prefix.startswith(prefix)):
                raise ValueError("Listing prefixes cannot overlap between scopes for the same source.")

        current_rows = connection.execute(select(listing_availability).where(
            listing_availability.c.source == source
        )).mappings().all()
        current_by_id = {item["listing_id"]: item for item in current_rows}
        known_prices = connection.execute(select(
            database.listings.c.listing_id, database.listings.c.last_seen_at
        ).where(
            database.listings.c.source == source,
            database.listings.c.listing_id.startswith(prefix, autoescape=True),
        )).mappings().all()
        if any(_stored_utc(item["last_seen_at"]) > timestamp for item in known_prices):
            raise ValueError("The inventory snapshot cannot precede an already observed price in its scope.")

        # Generic price ingestion retains the first payload for an ID/timestamp.
        # A complete catalogue must describe that exact payload, not silently
        # accept a conflicting price that the shared price upsert would ignore.
        if normalized:
            existing_observations = connection.execute(select(database.listing_observations).where(
                database.listing_observations.c.source == source,
                database.listing_observations.c.observed_at == timestamp,
                database.listing_observations.c.listing_id.in_(row_ids),
            )).mappings().all()
            incoming_by_id = {item["listing_id"]: item for item in normalized}
            for existing in existing_observations:
                stored = dict(existing)
                stored["observed_at"] = _stored_utc(stored["observed_at"])
                stored = database._normalize_listing(stored)
                if stored != incoming_by_id[stored["listing_id"]]:
                    raise ValueError("A retained price observation at this timestamp has different contents.")
        price_last_seen = {item["listing_id"]: item["last_seen_at"] for item in known_prices}
        known_ids = {item["listing_id"] for item in current_rows if item["scope"] == scope} | set(price_last_seen)
        for listing_id in known_ids | set(reported):
            old = current_by_id.get(listing_id)
            if old is not None and old["scope"] != scope:
                raise ValueError("A listing_id cannot move between inventory scopes.")
            if not listing_id.startswith(prefix):
                raise ValueError("Existing scope identifiers do not match its listing_id_prefix.")

        changes = []
        for listing_id in sorted(known_ids | set(reported)):
            if listing_id in reported:
                status = reported[listing_id]
                last_seen = timestamp
            else:
                status = "missing"
                old = current_by_id.get(listing_id)
                last_seen = _stored_utc(old["last_seen_at"] if old is not None else price_last_seen[listing_id])
            changes.append({
                "source": source, "listing_id": listing_id, "scope": scope,
                "status": status, "observed_at": timestamp, "last_seen_at": last_seen,
            })

        added = database._upsert_normalized(connection, normalized)
        for item in changes:
            # No foreign key to priced rows: a first-seen sold unit has no price.
            connection.execute(listing_availability_observations.insert().values(**item))
            current_insert = dialect_insert(listing_availability).values(**item)
            connection.execute(current_insert.on_conflict_do_update(
                index_elements=["source", "listing_id"],
                set_={field: current_insert.excluded[field] for field in ("scope", "status", "observed_at", "last_seen_at")},
            ))
        counts = _counts(changes)
        connection.execute(inventory_snapshots.update().where(
            inventory_snapshots.c.source == source,
            inventory_snapshots.c.scope == scope,
            inventory_snapshots.c.observed_at == timestamp,
        ).values(missing_count=counts["missing"]))
        return {"new_observations": added, "availability_counts": counts, "snapshot_replayed": False}


def read_availability(engine: Engine) -> pd.DataFrame:
    """Return current availability with a stable schema and UTC timestamps."""
    return database._read_table(engine, listing_availability, ("last_seen_at", "observed_at"))


def read_availability_observations(engine: Engine) -> pd.DataFrame:
    """Return availability history ordered by its UTC observation time."""
    return database._read_table(engine, listing_availability_observations, ("last_seen_at", "observed_at"))
