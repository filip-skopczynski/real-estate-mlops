"""Availability contract, exercised in SQLite and an optional isolated PG schema.

PostgreSQL tests use only TEST_DATABASE_URL from the process environment. They
never load the project's .env and never create or delete tables in public.
"""

from datetime import datetime, timedelta, timezone
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

import pandas as pd
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from src import database
from src.availability import (
    inventory_snapshots,
    listing_availability,
    listing_availability_observations,
    read_availability,
    read_availability_observations,
    save_inventory_snapshot,
)


class AvailabilityContract:
    def setUp(self):
        self.create_test_engine()
        database.init_db(self.engine)
        self.source = "dane.gov.pl:39940"
        self.scope = "Bemovo PH1"
        self.prefix = "5252801624:Bemovo PH1:"
        self.timestamp = datetime(2026, 10, 5, 8, tzinfo=timezone.utc)

    def listing(self, unit="A.0.1", *, at=None, **changes):
        row = {
            "source": self.source,
            "listing_id": self.prefix + unit,
            "url": "https://example.test/apartments/" + unit.replace(".", "-"),
            "city": "Warszawa",
            "district": "Bemowo",
            "price_pln": 600_000,
            "area_m2": 50,
            "rooms": 2,
            "floor": 0,
            "build_year": None,
            "latitude": 52.24,
            "longitude": 20.90,
            "distance_km": 8.1,
            "observed_at": self.timestamp if at is None else at,
        }
        row.update(changes)
        return row

    def status(self, unit="A.0.1", status="available", **changes):
        record = {"listing_id": self.prefix + unit, "status": status}
        record.update(changes)
        return record

    def save(self, rows, availability, *, at=None, **changes):
        arguments = {
            "source": self.source,
            "scope": self.scope,
            "listing_id_prefix": self.prefix,
            "observed_at": self.timestamp if at is None else at,
            "complete": True,
        }
        arguments.update(changes)
        return save_inventory_snapshot(self.engine, rows, availability, **arguments)

    def stored_state(self):
        """Compare all persisted values, rather than checking only row counts."""
        tables = (
            database.listings,
            database.listing_observations,
            inventory_snapshots,
            listing_availability,
            listing_availability_observations,
        )
        with self.engine.connect() as connection:
            return {
                table.name: [dict(row) for row in connection.execute(
                    select(table).order_by(*table.primary_key.columns)
                ).mappings()]
                for table in tables
            }

    def current(self, *, available_only=False):
        return database.read_current_listings(self.engine, available_only=available_only)

    def test_initial_catalogue_saves_prices_and_all_explicit_statuses(self):
        result = self.save([self.listing()], [
            self.status(), self.status("A.0.2", "reserved"), self.status("A.0.3", "sold"),
        ])
        self.assertEqual(result, {
            "new_observations": 1,
            "availability_counts": {"available": 1, "reserved": 1, "sold": 1, "missing": 0},
            "snapshot_replayed": False,
        })
        self.assertEqual(len(database.read_observations(self.engine)), 1)
        self.assertEqual(len(read_availability(self.engine)), 3)
        self.assertEqual(len(read_availability_observations(self.engine)), 3)
        current = self.current().iloc[0]
        self.assertEqual(current["floor"], 0)
        self.assertEqual(current["availability_status"], "available")
        for column in ("observed_at", "availability_observed_at", "availability_last_seen_at"):
            self.assertEqual(current[column], pd.Timestamp(self.timestamp))

    def test_zero_available_catalogue_preserves_archived_price_timestamps(self):
        self.save([self.listing(), self.listing("A.0.2")], [self.status(), self.status("A.0.2")])
        later = self.timestamp + timedelta(days=1)
        result = self.save([], [self.status(status="reserved"), self.status("A.0.2", "sold")], at=later)
        self.assertEqual(result["new_observations"], 0)
        self.assertEqual(result["availability_counts"], {
            "available": 0, "reserved": 1, "sold": 1, "missing": 0,
        })
        self.assertTrue(self.current(available_only=True).empty)
        self.assertEqual(len(database.read_observations(self.engine)), 2)
        self.assertEqual(len(read_availability_observations(self.engine)), 4)
        for _, row in self.current().iterrows():
            self.assertEqual(float(row["price_pln"]), 600_000)
            self.assertEqual(row["observed_at"], pd.Timestamp(self.timestamp))
            self.assertEqual(row["last_seen_at"], pd.Timestamp(self.timestamp))
            self.assertEqual(row["availability_observed_at"], pd.Timestamp(later))
            self.assertEqual(row["availability_last_seen_at"], pd.Timestamp(later))

    def test_absent_unit_is_missing_and_can_reappear_with_a_new_price(self):
        self.save([self.listing(), self.listing("A.0.2")], [self.status(), self.status("A.0.2")])
        later = self.timestamp + timedelta(days=1)
        result = self.save([self.listing(at=later)], [self.status()], at=later)
        self.assertEqual(result["availability_counts"]["missing"], 1)
        missing = self.current().set_index("listing_id").loc[self.prefix + "A.0.2"]
        self.assertEqual(missing["availability_status"], "missing")
        self.assertEqual(missing["availability_observed_at"], pd.Timestamp(later))
        self.assertEqual(missing["availability_last_seen_at"], pd.Timestamp(self.timestamp))
        self.assertEqual(missing["observed_at"], pd.Timestamp(self.timestamp))
        self.assertEqual(len(self.current(available_only=True)), 1)

        newest = later + timedelta(days=1)
        result = self.save([
            self.listing(at=newest), self.listing("A.0.2", at=newest, price_pln=580_000),
        ], [self.status(), self.status("A.0.2")], at=newest)
        self.assertEqual(result["new_observations"], 2)
        self.assertEqual(result["availability_counts"]["missing"], 0)
        returned = self.current(available_only=True).set_index("listing_id").loc[self.prefix + "A.0.2"]
        self.assertEqual(returned["availability_status"], "available")
        self.assertEqual(float(returned["price_pln"]), 580_000)
        self.assertEqual(returned["first_seen_at"], pd.Timestamp(self.timestamp))
        self.assertEqual(returned["observed_at"], pd.Timestamp(newest))
        self.assertEqual(len(database.read_observations(self.engine)), 5)
        self.assertEqual(len(read_availability_observations(self.engine)), 6)

    def test_sold_unit_without_a_prior_price_has_only_availability_history(self):
        result = self.save([], [self.status(status="sold")])
        self.assertEqual(result["availability_counts"]["sold"], 1)
        self.assertEqual(result["new_observations"], 0)
        self.assertTrue(self.current().empty)
        self.assertTrue(database.read_observations(self.engine).empty)
        self.assertEqual(read_availability(self.engine).iloc[0]["status"], "sold")

    def test_partial_or_empty_catalogue_never_changes_existing_data(self):
        self.save([self.listing()], [self.status()])
        before = self.stored_state()
        later = self.timestamp + timedelta(days=1)
        for complete in (False, 1, "true", None):
            with self.subTest(complete=complete), self.assertRaises(ValueError):
                self.save([self.listing(at=later)], [self.status()], at=later, complete=complete)
            self.assertEqual(self.stored_state(), before)
        with self.assertRaises(ValueError):
            self.save([], [], at=later)
        self.assertEqual(self.stored_state(), before)

    def test_invalid_catalogue_or_price_alignment_never_changes_existing_data(self):
        self.save([self.listing()], [self.status()])
        before = self.stored_state()
        later = self.timestamp + timedelta(days=1)
        valid_row = self.listing(at=later)
        cases = {
            "unknown status": ([valid_row], [self.status(status="unknown")]),
            "missing is inferred": ([], [self.status(status="missing")]),
            "duplicate catalogue id": ([valid_row], [self.status(), self.status(status="sold")]),
            "duplicate price id": ([valid_row, valid_row.copy()], [self.status()]),
            "missing available price": ([], [self.status()]),
            "price for a reserved unit": ([valid_row], [self.status(status="reserved")]),
            "wrong price id": ([self.listing("A.0.2", at=later)], [self.status()]),
            "wrong price source": ([self.listing(at=later, source="another-source")], [self.status()]),
            "wrong price timestamp": ([self.listing()], [self.status()]),
            "wrong catalogue timestamp": ([valid_row], [self.status(observed_at=self.timestamp)]),
            "wrong catalogue scope": ([valid_row], [self.status(scope="another-scope")]),
            "id outside prefix": ([valid_row], [self.status(listing_id="unrelated-id")]),
            "negative second price": (
                [valid_row, self.listing("A.0.2", at=later, price_pln=-1)],
                [self.status(), self.status("A.0.2")],
            ),
        }
        for label, (rows, availability) in cases.items():
            with self.subTest(case=label), self.assertRaises(ValueError):
                self.save(rows, availability, at=later)
            self.assertEqual(self.stored_state(), before)

    def test_database_failure_rolls_back_prices_snapshot_and_statuses(self):
        # The input validator already rejects negative prices. Inject a database
        # rejection after the first write to also verify the transaction boundary.
        before = self.stored_state()
        original_upsert = database._upsert_normalized

        def reject_second_price(connection, normalized):
            database_rows = [dict(row) for row in normalized]
            database_rows[1]["price_pln"] = -1
            return original_upsert(connection, database_rows)

        with patch("src.database._upsert_normalized", side_effect=reject_second_price):
            with self.assertRaises(IntegrityError):
                self.save([self.listing(), self.listing("A.0.2")], [self.status(), self.status("A.0.2")])
        self.assertEqual(self.stored_state(), before)

    def test_replay_is_order_independent_and_conflicting_replay_is_rejected(self):
        rows = [self.listing(), self.listing("A.0.2")]
        availability = [self.status(), self.status("A.0.2")]
        self.save(rows, availability)
        before = self.stored_state()
        replay = self.save(list(reversed(rows)), list(reversed(availability)))
        self.assertTrue(replay["snapshot_replayed"])
        self.assertEqual(replay["new_observations"], 0)
        self.assertEqual(self.stored_state(), before)
        with self.assertRaises(ValueError):
            self.save([self.listing(price_pln=599_000), rows[1]], availability)
        with self.assertRaises(ValueError):
            self.save([rows[0]], [self.status(), self.status("A.0.2", "sold")])
        self.assertEqual(self.stored_state(), before)

    def test_new_stale_snapshot_fails_but_known_older_replay_does_not_revert_status(self):
        rows = [self.listing()]
        availability = [self.status()]
        self.save(rows, availability)
        later = self.timestamp + timedelta(days=1)
        self.save([], [self.status(status="sold")], at=later)
        before = self.stored_state()
        earlier = self.timestamp - timedelta(days=1)
        with self.assertRaises(ValueError):
            self.save([self.listing(at=earlier)], availability, at=earlier)
        self.assertEqual(self.stored_state(), before)
        replay = self.save(rows, availability)
        self.assertTrue(replay["snapshot_replayed"])
        self.assertEqual(replay["availability_counts"], {
            "available": 0, "reserved": 0, "sold": 1, "missing": 0,
        })
        self.assertEqual(self.stored_state(), before)

    def test_first_snapshot_backfills_missing_without_retiring_other_sources_or_scopes(self):
        other_source = "legacy-jsonld"
        other_prefix = "different-investment:"
        database.upsert_listings(self.engine, [
            self.listing(), self.listing("A.0.2"), self.listing("A.0.2", source=other_source),
        ])
        later = self.timestamp + timedelta(days=1)
        other_row = self.listing(at=later, listing_id=other_prefix + "1")
        self.save([other_row], [{"listing_id": other_row["listing_id"], "status": "available"}],
                  at=later, scope="Another investment", listing_id_prefix=other_prefix)
        result = self.save([self.listing(at=later)], [self.status()], at=later)
        self.assertEqual(result["availability_counts"]["missing"], 1)
        current = self.current().set_index(["source", "listing_id"])
        absent = current.loc[(self.source, self.prefix + "A.0.2")]
        self.assertEqual(absent["availability_status"], "missing")
        self.assertEqual(absent["availability_last_seen_at"], pd.Timestamp(self.timestamp))
        self.assertTrue(pd.isna(current.loc[(other_source, self.prefix + "A.0.2"), "availability_status"]))
        self.assertEqual(current.loc[(self.source, other_prefix + "1"), "availability_status"], "available")
        active = self.current(available_only=True)
        self.assertEqual(set(zip(active["source"], active["listing_id"])), {
            (self.source, self.prefix + "A.0.1"),
            (other_source, self.prefix + "A.0.2"),
            (self.source, other_prefix + "1"),
        })

    def test_scope_prefix_is_stable_and_overlapping_scopes_are_rejected(self):
        self.save([self.listing()], [self.status()])
        before = self.stored_state()
        later = self.timestamp + timedelta(days=1)
        changed_prefix = "replacement:"
        replacement = self.listing(at=later, listing_id=changed_prefix + "1")
        with self.assertRaises(ValueError):
            self.save([replacement], [{"listing_id": replacement["listing_id"], "status": "available"}],
                      at=later, listing_id_prefix=changed_prefix)
        with self.assertRaises(ValueError):
            self.save([], [self.status(status="sold")], at=later,
                      scope="Different scope", listing_id_prefix=self.prefix + "A.")
        self.assertEqual(self.stored_state(), before)

    def test_catalogue_older_than_an_existing_partial_price_does_not_infer_missing(self):
        later = self.timestamp + timedelta(days=1)
        database.upsert_listings(self.engine, [self.listing(), self.listing("A.0.2", at=later)])
        before = self.stored_state()
        with self.assertRaises(ValueError):
            self.save([self.listing()], [self.status()])
        self.assertEqual(self.stored_state(), before)
        self.assertTrue(read_availability(self.engine).empty)
        self.assertEqual(len(self.current(available_only=True)), 2)

    def test_existing_same_time_price_must_match_verified_snapshot(self):
        database.upsert_listings(self.engine, [self.listing()])
        before = self.stored_state()
        for changes in ({"price_pln": 599_000}, {"area_m2": 49}, {"rooms": 3}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.save([self.listing(**changes)], [self.status()])
            self.assertEqual(self.stored_state(), before)

        equivalent = self.listing(price_pln="600000", area_m2=50.0,
                                  observed_at=self.timestamp.astimezone(timezone(timedelta(hours=2))))
        result = self.save([equivalent], [self.status()])
        self.assertEqual(result["new_observations"], 0)
        self.assertFalse(result["snapshot_replayed"])
        self.assertEqual(len(database.read_observations(self.engine)), 1)
        self.assertEqual(self.current().iloc[0]["availability_status"], "available")

    def test_availability_only_preserves_prices_and_allows_a_verified_price_subset(self):
        self.save([self.listing(), self.listing("A.0.2")], [
            self.status(), self.status("A.0.2"), self.status("A.0.3", "sold"),
        ])
        before = self.stored_state()
        later = self.timestamp + timedelta(days=1)
        catalogue = [self.status(), self.status("A.0.2"), self.status("A.0.3")]
        # Default mode still requires a verified price for every available unit.
        with self.assertRaises(ValueError):
            self.save([], catalogue, at=later)
        self.assertEqual(self.stored_state(), before)

        result = self.save([], catalogue, at=later, prices_complete=False)
        self.assertEqual(result["new_observations"], 0)
        self.assertEqual(result["availability_counts"], {
            "available": 3, "reserved": 0, "sold": 0, "missing": 0,
        })
        after = self.stored_state()
        self.assertEqual(after["listings"], before["listings"])
        self.assertEqual(after["listing_observations"], before["listing_observations"])
        self.assertEqual(len(read_availability(self.engine)), 3)
        for _, row in read_availability(self.engine).iterrows():
            self.assertEqual(row["observed_at"], pd.Timestamp(later))
            self.assertEqual(row["last_seen_at"], pd.Timestamp(later))
        for _, row in self.current(available_only=True).iterrows():
            self.assertEqual(row["observed_at"], pd.Timestamp(self.timestamp))
            self.assertEqual(row["availability_observed_at"], pd.Timestamp(later))
        # A newly available unit without a verified price stays status-only.
        self.assertNotIn(self.prefix + "A.0.3", set(self.current()["listing_id"]))

        newest = later + timedelta(days=1)
        result = self.save([self.listing(at=newest, price_pln=590_000)], catalogue,
                           at=newest, prices_complete=False)
        self.assertEqual(result["new_observations"], 1)
        self.assertEqual(len(database.read_observations(self.engine)), 3)
        current = self.current().set_index("listing_id")
        self.assertEqual(current.loc[self.prefix + "A.0.1", "observed_at"], pd.Timestamp(newest))
        self.assertEqual(current.loc[self.prefix + "A.0.2", "observed_at"], pd.Timestamp(self.timestamp))

    def test_partial_prices_still_reject_unavailable_ids_and_nonboolean_mode(self):
        self.save([self.listing()], [self.status()])
        before = self.stored_state()
        later = self.timestamp + timedelta(days=1)
        for status in ("reserved", "sold"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.save([self.listing(at=later)], [self.status(status=status)],
                          at=later, prices_complete=False)
            self.assertEqual(self.stored_state(), before)
        with self.assertRaises(ValueError):
            self.save([self.listing(at=later)], [self.status("A.0.2")],
                      at=later, prices_complete=False)
        self.assertEqual(self.stored_state(), before)
        for mode in (0, 1, "false", None):
            with self.subTest(prices_complete=mode), self.assertRaises(ValueError):
                self.save([self.listing(at=later)], [self.status()], at=later, prices_complete=mode)
            self.assertEqual(self.stored_state(), before)

    def test_replay_with_changed_price_completeness_conflicts(self):
        rows = [self.listing()]
        catalogue = [self.status()]
        self.save(rows, catalogue, prices_complete=False)
        before = self.stored_state()
        replay = self.save(rows, catalogue, prices_complete=False)
        self.assertTrue(replay["snapshot_replayed"])
        self.assertEqual(replay["new_observations"], 0)
        with self.assertRaises(ValueError):
            self.save(rows, catalogue, prices_complete=True)
        self.assertEqual(self.stored_state(), before)

    def test_empty_readers_keep_availability_columns_and_utc_timestamps(self):
        current = self.current(available_only=True)
        for column in ("availability_status", "availability_observed_at", "availability_last_seen_at"):
            self.assertIn(column, current.columns)
        for frame in (read_availability(self.engine), read_availability_observations(self.engine)):
            self.assertTrue(frame.empty)
            self.assertIn("status", frame.columns)
            self.assertEqual(str(frame["observed_at"].dtype), "datetime64[ns, UTC]")
            self.assertEqual(str(frame["last_seen_at"].dtype), "datetime64[ns, UTC]")


class TestSQLiteAvailability(AvailabilityContract, unittest.TestCase):
    def create_test_engine(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:", hide_parameters=True)
        self.addCleanup(self.engine.dispose)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not configured")
class TestPostgreSQLAvailability(AvailabilityContract, unittest.TestCase):
    def create_test_engine(self):
        url = make_url(os.environ["TEST_DATABASE_URL"])
        if url.drivername in {"postgres", "postgresql"}:
            url = url.set(drivername="postgresql+psycopg2")
        self.assertEqual(url.get_backend_name(), "postgresql", "TEST_DATABASE_URL must be a PostgreSQL test database")
        if url.host not in {None, "localhost", "127.0.0.1", "::1"} and "sslmode" not in url.query:
            url = url.update_query_dict({"sslmode": "require"})
        base_engine = create_engine(url, hide_parameters=True, connect_args={"connect_timeout": 10})
        self.addCleanup(base_engine.dispose)
        schema = "test_availability_" + uuid4().hex
        # The identifier is generated here, never taken from external input.
        with base_engine.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + schema + '"'))

        def drop_test_schema():
            with base_engine.begin() as connection:
                connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))

        self.addCleanup(drop_test_schema)
        self.engine = base_engine.execution_options(schema_translate_map={None: schema})


if __name__ == "__main__":
    unittest.main()
