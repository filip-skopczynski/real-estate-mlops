"""PostgreSQL storage; SQLite is available for local demos and tests."""

import argparse
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import sys
from typing import Any

from dotenv import load_dotenv
import pandas as pd
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    case,
    create_engine,
    func,
    inspect,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import ArgumentError, OperationalError, SQLAlchemyError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
metadata = MetaData()


def _listing_columns():
    # Każda tabela otrzymuje własne obiekty Column.
    return [
        Column("source", String(80), primary_key=True),
        Column("listing_id", String(200), primary_key=True),
        Column("url", Text, nullable=False),
        Column("city", String(120), nullable=False),
        Column("district", String(120)),
        Column("price_pln", Numeric(14, 2), nullable=False),
        Column("area_m2", Float, nullable=False),
        Column("rooms", Integer),
        Column("floor", Integer),
        Column("build_year", Integer),
        Column("latitude", Float),
        Column("longitude", Float),
        Column("distance_km", Float),
    ]


def _constraints():
    return [
        CheckConstraint("price_pln > 0"),
        CheckConstraint("area_m2 > 0"),
        CheckConstraint("rooms IS NULL OR rooms > 0"),
        CheckConstraint("distance_km IS NULL OR distance_km >= 0"),
    ]


listings = Table(
    "listings",
    metadata,
    *_listing_columns(),
    Column("first_seen_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    *_constraints(),
)
Index("ix_listings_city_district", listings.c.city, listings.c.district)

listing_observations = Table(
    "listing_observations",
    metadata,
    *_listing_columns(),
    Column("observed_at", DateTime(timezone=True), primary_key=True, index=True),
    *_constraints(),
)

LISTING_FIELDS = tuple(column.name for column in listings.columns)[:-2]


def get_engine(database_url: str | None = None) -> Engine:
    """Read project .env and construct an engine without opening a connection."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    database_url = database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        raise ValueError("Ustaw DATABASE_URL w środowisku lub w pliku .env projektu.")
    try:
        url = make_url(database_url)
    except ArgumentError:
        raise ValueError("DATABASE_URL ma niepoprawny format.") from None

    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg2")
    if url.get_backend_name() not in {"postgresql", "sqlite"}:
        raise ValueError("Obsługiwane bazy to PostgreSQL oraz SQLite do lokalnych testów.")

    connect_args = {}
    if url.get_backend_name() == "postgresql":
        if url.host not in {None, "localhost", "127.0.0.1", "::1"} and "sslmode" not in url.query:
            url = url.update_query_dict({"sslmode": "require"})
        if "connect_timeout" not in url.query:
            connect_args["connect_timeout"] = 10
    return create_engine(
        url,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args=connect_args,
    )


def init_db(engine: Engine) -> None:
    """Create missing tables without replacing existing data."""
    # Additive availability tables share this metadata; no old table is altered.
    from src import availability

    # `python -m src.database` runs this file as __main__; use the canonical
    # imported module's metadata so the availability tables are included too.
    availability.inventory_snapshots.metadata.create_all(engine)


def _utc_timestamp(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError("observed_at musi być datą ISO 8601 ze strefą czasową.") from None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observed_at musi zawierać strefę czasową.")
    return value.astimezone(timezone.utc)


def _number(value: Any, *, optional: bool = False, integer: bool = False):
    if optional and value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("Pole liczbowe nie może zawierać wartości logicznej.")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Pole liczbowe musi zawierać poprawną liczbę.") from None
    if not math.isfinite(number):
        raise ValueError("Pola liczbowe muszą być skończone.")
    if integer:
        if not number.is_integer():
            raise ValueError("Liczba pokoi, piętro i rok budowy muszą być całkowite.")
        return int(number)
    return number


def _normalize_listing(row: dict[str, Any]) -> dict[str, Any]:
    required = {"source", "listing_id", "url", "city", "price_pln", "area_m2", "observed_at"}
    if not required.issubset(row):
        raise ValueError("Rekord oferty nie zawiera wszystkich wymaganych pól.")
    result = {}
    for field in ("source", "listing_id", "url", "city"):
        value = row[field]
        if value is None or not str(value).strip():
            raise ValueError("Identyfikator, źródło, URL i miasto nie mogą być puste.")
        result[field] = str(value).strip()
    result["district"] = None if row.get("district") is None else str(row["district"]).strip()
    for field in ("price_pln", "area_m2"):
        result[field] = _number(row[field])
    for field in ("rooms", "floor", "build_year"):
        result[field] = _number(row.get(field), optional=True, integer=True)
    for field in ("latitude", "longitude", "distance_km"):
        result[field] = _number(row.get(field), optional=True)
    result["observed_at"] = _utc_timestamp(row["observed_at"])
    return result


def _upsert_normalized(connection, normalized: list[dict[str, Any]]) -> int:
    """Save validated prices using the caller's transaction."""
    inserts = {"postgresql": postgres_insert, "sqlite": sqlite_insert}
    try:
        dialect_insert = inserts[connection.dialect.name]
    except KeyError:
        raise ValueError("Upsert obsługuje PostgreSQL i SQLite.") from None

    added = 0
    for row in normalized:
        history_insert = dialect_insert(listing_observations).values(**row)
        history_insert = history_insert.on_conflict_do_nothing(
            index_elements=["source", "listing_id", "observed_at"]
        )
        result = connection.execute(history_insert)
        if result.rowcount == 0:
            continue
        added += 1

        current = {field: row[field] for field in LISTING_FIELDS}
        current["first_seen_at"] = row["observed_at"]
        current["last_seen_at"] = row["observed_at"]
        current_insert = dialect_insert(listings).values(**current)
        incoming = current_insert.excluded
        is_latest = incoming.last_seen_at >= listings.c.last_seen_at
        updates = {
            field: case((is_latest, incoming[field]), else_=listings.c[field])
            for field in LISTING_FIELDS
            if field not in {"source", "listing_id"}
        }
        updates["first_seen_at"] = case(
            (incoming.first_seen_at < listings.c.first_seen_at, incoming.first_seen_at),
            else_=listings.c.first_seen_at,
        )
        updates["last_seen_at"] = case(
            (is_latest, incoming.last_seen_at), else_=listings.c.last_seen_at
        )
        connection.execute(
            current_insert.on_conflict_do_update(
                index_elements=["source", "listing_id"], set_=updates
            )
        )
    return added


def upsert_listings(engine: Engine, rows: list[dict[str, Any]]) -> int:
    """Atomically save price history and current prices without inferring status.

    Repeating a source/ID/timestamp is idempotent; older prices cannot overwrite
    newer ones. Partial or generic ingestion never retires or reactivates units.
    """
    if not rows:
        return 0
    normalized = [_normalize_listing(row) for row in rows]
    with engine.begin() as connection:
        return _upsert_normalized(connection, normalized)


def _read_table(engine: Engine, table: Table, timestamps: tuple[str, ...]) -> pd.DataFrame:
    query = select(table).order_by(table.c[timestamps[-1]], table.c.source, table.c.listing_id)
    with engine.connect() as connection:
        frame = pd.read_sql(query, connection)
    # SQLAlchemy może zwrócić quoted_name; sklearn wymaga zwykłych nazw str.
    frame.columns = [str(column) for column in frame.columns]
    for column in timestamps:
        frame[column] = pd.to_datetime(frame[column], utc=True)
    return frame


def read_observations(engine: Engine) -> pd.DataFrame:
    """Return asking-price and feature history, including UTC observed_at."""
    return _read_table(engine, listing_observations, ("observed_at",))


def read_current_listings(engine: Engine, available_only: bool = False) -> pd.DataFrame:
    """Return last prices with separately observed availability.

    Legacy sources without inventory tracking retain their previous behaviour.
    For tracked units, available_only excludes sold, reserved and missing units.
    The price timestamp is not replaced by the status-check timestamp.
    """
    from src.availability import listing_availability

    availability = listing_availability
    query = select(
        listings,
        availability.c.status.label("availability_status"),
        availability.c.observed_at.label("availability_observed_at"),
        availability.c.last_seen_at.label("availability_last_seen_at"),
    ).select_from(listings.outerjoin(availability, (
        (listings.c.source == availability.c.source)
        & (listings.c.listing_id == availability.c.listing_id)
    )))
    if available_only:
        query = query.where(or_(availability.c.status == "available", availability.c.status.is_(None)))
    query = query.order_by(listings.c.last_seen_at, listings.c.source, listings.c.listing_id)
    with engine.connect() as connection:
        frame = pd.read_sql(query, connection)
    frame.columns = [str(column) for column in frame.columns]
    for column in ("first_seen_at", "last_seen_at", "availability_observed_at", "availability_last_seen_at"):
        frame[column] = pd.to_datetime(frame[column], utc=True)
    frame["observed_at"] = frame["last_seen_at"].copy()
    return frame


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inicjalizacja i sprawdzanie bazy ofert.")
    parser.add_argument("command", choices=["init", "check"])
    args = parser.parse_args(argv)
    engine = None
    try:
        engine = get_engine()
        if args.command == "init":
            init_db(engine)
            print(f"Baza gotowa ({engine.dialect.name}).")
        else:
            from src.availability import inventory_snapshots, listing_availability

            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                database = inspect(connection)
                if not all(database.has_table(table.name) for table in inventory_snapshots.metadata.tables.values()):
                    print("Brakuje tabel. Uruchom: python -m src.database init", file=sys.stderr)
                    return 1
                current_count = connection.scalar(select(func.count()).select_from(listings))
                history_count = connection.scalar(select(func.count()).select_from(listing_observations))
                status_counts = dict(connection.execute(select(
                    listing_availability.c.status, func.count()
                ).group_by(listing_availability.c.status)).all())
            print(f"Połączenie działa. Oferty: {current_count}; obserwacje: {history_count}.")
            print(f"Śledzona dostępność: {status_counts}.")
        return 0
    except OperationalError:
        print("Nie udało się połączyć z bazą. Sprawdź host, port, SSL i dane dostępu.", file=sys.stderr)
        return 1
    except (SQLAlchemyError, ValueError, OSError):
        # Wyjątki silnika mogą zawierać URL i parametry; nie drukujemy ich treści.
        print("Operacja bazy nie powiodła się. Sprawdź DATABASE_URL i konfigurację bazy.", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
