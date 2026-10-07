"""Sparse portal catalogue and durable, leased collection checkpoints.

Catalogue absence never changes availability. Source prices with a verified
positive area are also written to the existing price history, in bulk.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import (Boolean, CheckConstraint, Column, DDL, DateTime, Float,
                        Index, Integer, JSON, Numeric, String, Table, Text, UniqueConstraint,
                        case, event, or_, select, update)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src import database

SOURCES = {"www.olx.pl", "www.otodom.pl"}
MODES = {"bootstrap", "daily", "refresh"}
CATALOG_FIELDS = ("source", "listing_id", "url", "city", "district", "price_pln",
                  "area_m2", "rooms", "rooms_min", "floor", "build_year",
                  "latitude", "longitude", "distance_km", "published_at", "observed_at")

listing_catalog = Table(
    "listing_catalog", database.metadata,
    Column("source", String(80), primary_key=True),
    Column("listing_id", String(200), primary_key=True),
    Column("url", Text, nullable=False), Column("city", String(120), nullable=False),
    Column("district", String(120)), Column("price_pln", Numeric(14, 2)),
    Column("area_m2", Float), Column("rooms", Integer), Column("rooms_min", Integer),
    Column("floor", Integer), Column("build_year", Integer),
    Column("latitude", Float), Column("longitude", Float), Column("distance_km", Float),
    Column("published_at", DateTime(timezone=True)),
    Column("first_seen_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("source", "url", name="uq_catalog_source_url"),
    CheckConstraint("price_pln IS NULL OR price_pln > 0"),
    CheckConstraint("area_m2 IS NULL OR area_m2 > 0"),
    CheckConstraint("rooms IS NULL OR rooms > 0"),
    CheckConstraint("rooms_min IS NULL OR rooms_min > 0"),
)
Index("ix_catalog_source_last_seen", listing_catalog.c.source, listing_catalog.c.last_seen_at)

collection_progress = Table(
    "collection_progress", database.metadata,
    Column("source", String(80), primary_key=True),
    Column("mode", String(20), primary_key=True),
    Column("generation", String(32), nullable=False),
    Column("checkpoint", JSON, nullable=False),
    Column("completed", Boolean, nullable=False, default=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("lease_owner", String(32)), Column("lease_until", DateTime(timezone=True)),
    Column("last_error", String(80)),
)

for table in (listing_catalog, collection_progress):
    event.listen(table, "after_create", DDL(
        "ALTER TABLE %(fullname)s ENABLE ROW LEVEL SECURITY"
    ).execute_if(dialect="postgresql"))


def _insert(connection, table):
    implementations = {"postgresql": pg_insert, "sqlite": sqlite_insert}
    if connection.dialect.name not in implementations:
        raise ValueError("Katalog wymaga PostgreSQL lub demonstracyjnej bazy SQLite.")
    return implementations[connection.dialect.name](table)


def normalize_catalog(rows):
    result, identities = [], set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(CATALOG_FIELDS):
            raise ValueError("Nieprawidłowe pola rekordu katalogu.")
        source, identity = row["source"], row["listing_id"]
        if source not in SOURCES or not isinstance(identity, str) or not re.fullmatch(r"[0-9]{1,200}", identity) or int(identity) <= 0:
            raise ValueError("Nieprawidłowa tożsamość ogłoszenia.")
        if (source, identity) in identities:
            raise ValueError("Jedna partia katalogu wymaga unikalnych tożsamości.")
        identities.add((source, identity))
        if row["city"] != "Warszawa" or not isinstance(row["url"], str):
            raise ValueError("Katalog obejmuje mieszkania w Warszawie.")
        url = urlsplit(row["url"])
        expected = r"/d/oferta/[^/]+\.html" if source == "www.olx.pl" else r"/pl/oferta/[^/]+"
        if url.scheme != "https" or url.netloc != source or url.query or url.fragment or not re.fullmatch(expected, url.path):
            raise ValueError("Nieprawidłowy adres ogłoszenia.")
        normalized = dict(row)
        district = normalized["district"]
        if district is not None and (not isinstance(district, str) or len(district.strip()) > 120):
            raise ValueError("Nieprawidłowa dzielnica.")
        normalized["district"] = district.strip() or None if district is not None else None
        for field in ("price_pln", "area_m2", "latitude", "longitude", "distance_km"):
            normalized[field] = database._number(row[field], optional=True)
        for field in ("rooms", "rooms_min", "floor", "build_year"):
            normalized[field] = database._number(row[field], optional=True, integer=True)
        for field in ("price_pln", "area_m2", "rooms", "rooms_min"):
            if normalized[field] is not None and normalized[field] <= 0:
                raise ValueError("Cena, metraż i liczba pokoi muszą być dodatnie albo nieznane.")
        if normalized["rooms"] is not None and normalized["rooms_min"] is not None and normalized["rooms"] < normalized["rooms_min"]:
            raise ValueError("Sprzeczna dokładna i minimalna liczba pokoi.")
        for field in ("observed_at", "published_at"):
            normalized[field] = database._utc_timestamp(row[field]) if row[field] is not None else None
        if normalized["observed_at"] is None:
            raise ValueError("Czas obserwacji jest wymagany.")
        result.append(normalized)
    return result


def _chunks(rows, size=300):
    for index in range(0, len(rows), size):
        yield rows[index:index + size]


def _save_prices(connection, rows):
    """Use bounded multi-row statements rather than a round trip per listing."""
    eligible = [database._normalize_listing({field: row.get(field) for field in
                (*database.LISTING_FIELDS, "observed_at")})
                for row in rows if row["price_pln"] is not None and row["area_m2"] is not None]
    stats = {"seen_listings": len(eligible), "new_listings": 0,
             "existing_listings": 0, "observations_inserted": 0}
    history, current = database.listing_observations, database.listings
    for batch in _chunks(eligible):
        inserted = connection.execute(_insert(connection, history).values(batch)
            .on_conflict_do_nothing(index_elements=["source", "listing_id", "observed_at"])
            .returning(history.c.source, history.c.listing_id)).all()
        identities = set(inserted)
        stats["observations_inserted"] += len(identities)
        latest = [{**{key: row[key] for key in database.LISTING_FIELDS},
                   "first_seen_at": row["observed_at"], "last_seen_at": row["observed_at"]}
                  for row in batch if (row["source"], row["listing_id"]) in identities]
        if not latest:
            continue
        created = connection.execute(_insert(connection, current).values(latest)
            .on_conflict_do_nothing(index_elements=["source", "listing_id"])
            .returning(current.c.source, current.c.listing_id)).all()
        stats["new_listings"] += len(created)
        statement = _insert(connection, current).values(latest)
        incoming = statement.excluded
        is_latest = incoming.last_seen_at >= current.c.last_seen_at
        updates = {field: case((is_latest, incoming[field]), else_=current.c[field])
                   for field in database.LISTING_FIELDS if field not in {"source", "listing_id"}}
        updates["first_seen_at"] = case((incoming.first_seen_at < current.c.first_seen_at, incoming.first_seen_at), else_=current.c.first_seen_at)
        updates["last_seen_at"] = case((is_latest, incoming.last_seen_at), else_=current.c.last_seen_at)
        connection.execute(statement.on_conflict_do_update(index_elements=["source", "listing_id"], set_=updates))
    stats["existing_listings"] = len(eligible) - stats["new_listings"]
    return stats


def _save_catalog(connection, rows):
    created_count = 0
    fields = [key for key in CATALOG_FIELDS if key != "observed_at"]
    for batch in _chunks(rows):
        values = [{**{key: row[key] for key in fields}, "first_seen_at": row["observed_at"],
                   "last_seen_at": row["observed_at"]} for row in batch]
        created_count += len(connection.execute(_insert(connection, listing_catalog).values(values)
            .on_conflict_do_nothing(index_elements=["source", "listing_id"])
            .returning(listing_catalog.c.source, listing_catalog.c.listing_id)).all())
        statement = _insert(connection, listing_catalog).values(values)
        incoming = statement.excluded
        is_latest = incoming.last_seen_at > listing_catalog.c.last_seen_at
        updates = {field: case((is_latest, incoming[field]), else_=listing_catalog.c[field])
                   for field in fields if field not in {"source", "listing_id"}}
        updates["first_seen_at"] = case((incoming.first_seen_at < listing_catalog.c.first_seen_at, incoming.first_seen_at), else_=listing_catalog.c.first_seen_at)
        updates["last_seen_at"] = case((is_latest, incoming.last_seen_at), else_=listing_catalog.c.last_seen_at)
        connection.execute(statement.on_conflict_do_update(index_elements=["source", "listing_id"], set_=updates))
    return {"seen_listings": len(rows), "new_catalog_listings": created_count,
            "catalog_rows_upserted": len(rows)}


def _scope(source, mode):
    if source not in SOURCES or mode not in MODES:
        raise ValueError("Nieprawidłowy zakres postępu pobrania.")
    return (collection_progress.c.source == source) & (collection_progress.c.mode == mode)


def read_progress(engine, source, mode):
    with engine.connect() as connection:
        value = connection.execute(select(collection_progress).where(_scope(source, mode))).mappings().first()
    return dict(value) if value else None


def known_ids(engine, source):
    if source not in SOURCES:
        raise ValueError("Nieprawidłowe źródło.")
    query = select(listing_catalog.c.listing_id).where(listing_catalog.c.source == source).union(
        select(database.listings.c.listing_id).where(database.listings.c.source == source))
    with engine.connect() as connection:
        return set(connection.execute(query).scalars())


def acquire_progress(engine, source, mode, owner, *, at=None, restart=False):
    """Claim a source/mode cursor atomically, allowing crash recovery after expiry."""
    _scope(source, mode)
    if not isinstance(owner, str) or not re.fullmatch(r"[0-9a-f]{32}", owner):
        raise ValueError("Nieprawidłowy identyfikator pobrania.")
    at = database._utc_timestamp(at or datetime.now(timezone.utc))
    with engine.begin() as connection:
        connection.execute(_insert(connection, collection_progress).values(
            source=source, mode=mode, generation=uuid4().hex, checkpoint={}, completed=False,
            updated_at=at, lease_owner=None, lease_until=None).on_conflict_do_nothing(index_elements=["source", "mode"]))
        claim = connection.execute(update(collection_progress).where(
            _scope(source, mode) & or_(collection_progress.c.lease_owner.is_(None),
                collection_progress.c.lease_owner == owner, collection_progress.c.lease_until <= at)
        ).values(lease_owner=owner, lease_until=at + timedelta(hours=2)))
        if claim.rowcount != 1:
            return None
        current = dict(connection.execute(select(collection_progress).where(_scope(source, mode))).mappings().one())
        if restart:
            current.update(checkpoint={}, completed=False, generation=uuid4().hex)
            connection.execute(update(collection_progress).where(_scope(source, mode)).values(
                checkpoint={}, completed=False, generation=current["generation"], updated_at=at, last_error=None))
    return current


def save_capture(engine, rows, *, source, mode, owner, generation, checkpoint, completed, at=None):
    """Commit catalogue, valid prices and cursor together; failed commits don't advance."""
    normalized = normalize_catalog(rows)
    if any(row["source"] != source for row in normalized):
        raise ValueError("Partia zawiera inne źródło niż checkpoint.")
    if not isinstance(checkpoint, dict) or type(completed) is not bool:
        raise ValueError("Nieprawidłowy checkpoint.")
    encoded = json.dumps(checkpoint, allow_nan=False)
    if len(encoded.encode()) > 256 * 1024:
        raise ValueError("Checkpoint przekracza limit rozmiaru.")
    at = database._utc_timestamp(at or datetime.now(timezone.utc))
    with engine.begin() as connection:
        guarded = connection.execute(update(collection_progress).where(
            _scope(source, mode) & (collection_progress.c.lease_owner == owner)
            & (collection_progress.c.generation == generation)
            & (collection_progress.c.lease_until > at)
        ).values(updated_at=at, checkpoint=checkpoint, completed=completed, last_error=None))
        if guarded.rowcount != 1:
            raise ValueError("Utracono dzierżawę checkpointu; postęp nie został zapisany.")
        catalogue = _save_catalog(connection, normalized)
        prices = _save_prices(connection, normalized)
    return catalogue, prices


def release_progress(engine, source, mode, owner, *, error=None):
    if error not in {None, "source_failed", "database_failed"}:
        raise ValueError("Nieprawidłowy kod błędu.")
    with engine.begin() as connection:
        connection.execute(update(collection_progress).where(_scope(source, mode)
            & (collection_progress.c.lease_owner == owner)).values(lease_owner=None, lease_until=None, last_error=error))
