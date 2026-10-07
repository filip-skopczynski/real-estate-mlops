"""Bulk storage, transaction/cursor consistency and isolated lease contracts."""
from datetime import datetime, timedelta, timezone
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.exc import IntegrityError

from src import catalog_storage as storage, database
from src.availability import listing_availability_observations

AT = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
OWNER = "a" * 32


def row(identity="123", **changes):
    value = {key: None for key in storage.CATALOG_FIELDS}
    value.update(source="www.olx.pl", listing_id=identity,
                 url="https://www.olx.pl/d/oferta/fikcyjne-ID" + identity + ".html",
                 city="Warszawa", price_pln=800000, area_m2=50,
                 rooms=2, observed_at=AT)
    value.update(changes)
    return value


@pytest.fixture(params=["sqlite", "postgresql"])
def engine(request):
    if request.param == "postgresql" and not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL is not configured")
    instance = database.get_engine("sqlite:///:memory:" if request.param == "sqlite" else os.environ["TEST_DATABASE_URL"])
    assert instance.dialect.name == request.param
    database.init_db(instance)
    # This fixture only uses an isolated TEST_DATABASE_URL; portal-source rows
    # here must never be written to the production Supabase environment.
    with instance.begin() as connection:
        for table in (storage.collection_progress, storage.listing_catalog,
                      database.listing_observations, database.listings):
            connection.execute(delete(table).where(table.c.source.in_(storage.SOURCES)))
    yield instance
    with instance.begin() as connection:
        for table in (storage.collection_progress, storage.listing_catalog,
                      database.listing_observations, database.listings):
            connection.execute(delete(table).where(table.c.source.in_(storage.SOURCES)))
    instance.dispose()


def claim(engine, *, mode="bootstrap", at=AT, owner=OWNER):
    return storage.acquire_progress(engine, "www.olx.pl", mode, owner, at=at)


def save(engine, rows, state, *, at=AT, checkpoint=None, completed=False, owner=OWNER):
    return storage.save_capture(engine, rows, source="www.olx.pl", mode="bootstrap",
        owner=owner, generation=state["generation"], checkpoint=checkpoint or {"next_page": 2},
        completed=completed, at=at)


def count(engine, table):
    with engine.connect() as connection:
        return connection.scalar(select(func.count()).select_from(table))


def test_sparse_catalogue_and_price_history_are_distinct_and_atomic(engine):
    state = claim(engine)
    catalogue, prices = save(engine, [row("1"), row("2", rooms=None, rooms_min=4),
        row("3", price_pln=None), row("4", area_m2=None)], state)
    assert catalogue["new_catalog_listings"] == 4
    assert prices["new_listings"] == prices["observations_inserted"] == 2
    assert count(engine, storage.listing_catalog) == 4
    assert count(engine, database.listing_observations) == 2
    assert storage.read_progress(engine, "www.olx.pl", "bootstrap")["checkpoint"] == {"next_page": 2}
    with engine.connect() as connection:
        value = connection.execute(select(storage.listing_catalog).where(storage.listing_catalog.c.listing_id == "2")).mappings().one()
    assert value["rooms"] is None and value["rooms_min"] == 4
    assert count(engine, listing_availability_observations) == 0


def test_url_cannot_change_identity_across_separate_captures(engine):
    state = claim(engine)
    first = row("1")
    save(engine, [first], state)
    with pytest.raises(IntegrityError):
        save(engine, [row("2", url=first["url"])], state, checkpoint={"next_page": 3})
    assert count(engine, storage.listing_catalog) == 1
    assert count(engine, database.listing_observations) == 1
    assert storage.read_progress(engine, "www.olx.pl", "bootstrap")["checkpoint"] == {"next_page": 2}


def test_replay_does_not_change_prices_or_catalogue_at_same_timestamp(engine):
    state = claim(engine)
    save(engine, [row()], state)
    _, prices = save(engine, [row(price_pln=700000)], state)
    assert prices["observations_inserted"] == 0
    with engine.connect() as connection:
        value = connection.execute(select(storage.listing_catalog.c.price_pln)).scalar_one()
    assert float(value) == 800000
    assert count(engine, database.listing_observations) == 1


def test_failed_price_write_rolls_back_catalogue_and_checkpoint(engine, monkeypatch):
    state = claim(engine)
    monkeypatch.setattr(storage, "_save_prices", lambda *args: (_ for _ in ()).throw(ValueError("failed")))
    with pytest.raises(ValueError):
        save(engine, [row()], state)
    assert count(engine, storage.listing_catalog) == 0
    assert storage.read_progress(engine, "www.olx.pl", "bootstrap")["checkpoint"] == {}


def test_lease_blocks_overlap_and_expired_owner_cannot_advance(engine):
    state = claim(engine)
    other = "b" * 32
    assert claim(engine, owner=other) is None
    renewed = claim(engine, at=AT + timedelta(hours=3), owner=other)
    assert renewed is not None
    with pytest.raises(ValueError):
        save(engine, [row()], state, at=AT + timedelta(hours=3))
    assert count(engine, storage.listing_catalog) == 0
    storage.release_progress(engine, "www.olx.pl", "bootstrap", other)
    assert claim(engine, at=AT + timedelta(hours=3)) is not None


def test_bulk_insert_is_bounded_and_preserves_older_and_newer_prices(engine):
    state = claim(engine)
    calls = []
    def record(connection, cursor, statement, parameters, context, executemany):
        calls.append(statement)
    event.listen(engine, "before_cursor_execute", record)
    try:
        catalogue, prices = save(engine, [row(str(index)) for index in range(1, 701)], state)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert catalogue["new_catalog_listings"] == prices["observations_inserted"] == 700
    assert len(calls) < 25  # Hundreds of rows must not require hundreds of round trips.
    later = AT + timedelta(minutes=1)
    save(engine, [row("1", price_pln=750000, observed_at=later)], state, at=later)
    save(engine, [row("1", price_pln=900000, observed_at=AT - timedelta(days=1))], state, at=later)
    with engine.connect() as connection:
        value = connection.execute(select(database.listings).where(database.listings.c.listing_id == "1")).mappings().one()
    assert float(value["price_pln"]) == 750000
    assert count(engine, database.listing_observations) == 702


def test_checkpoint_survives_release_and_generation_restarts_explicitly(engine):
    state = claim(engine)
    save(engine, [row()], state, checkpoint={"next_page": 12})
    storage.release_progress(engine, "www.olx.pl", "bootstrap", OWNER)
    resumed = claim(engine)
    assert resumed["generation"] == state["generation"] and resumed["checkpoint"] == {"next_page": 12}
    restarted = storage.acquire_progress(engine, "www.olx.pl", "bootstrap", OWNER, at=AT, restart=True)
    assert restarted["generation"] != state["generation"] and restarted["checkpoint"] == {}


def test_new_postgres_tables_have_rls_enabled(engine):
    if engine.dialect.name != "postgresql":
        pytest.skip("RLS is a PostgreSQL feature")
    with engine.connect() as connection:
        flags = connection.execute(text("SELECT relname, relrowsecurity FROM pg_class WHERE relname IN ('listing_catalog','collection_progress') AND relnamespace = 'public'::regnamespace")).all()
    assert dict(flags) == {"listing_catalog": True, "collection_progress": True}
