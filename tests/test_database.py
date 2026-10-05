from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from io import StringIO
import os
import runpy
import unittest
from unittest.mock import patch
from uuid import uuid4

import pandas as pd
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError, OperationalError

from src import database


class StorageContract:
    def setUp(self):
        self.engine = database.get_engine(self.database_url())
        self.assertEqual(self.engine.dialect.name, self.expected_dialect)
        database.init_db(self.engine)
        self.source = "contract_test_" + uuid4().hex
        self.sources = [self.source]
        self.timestamp = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

    def tearDown(self):
        try:
            with self.engine.begin() as connection:
                for table in (database.listing_observations, database.listings):
                    connection.execute(delete(table).where(table.c.source.in_(self.sources)))
        finally:
            self.engine.dispose()

    def listing(self, **changes):
        row = {
            "source": self.source,
            "listing_id": "123",
            "url": "https://example.test/listings/123",
            "city": "Warszawa",
            "district": "Mokotów",
            "price_pln": 1_200_000,
            "area_m2": 60,
            "rooms": 3,
            "floor": 2,
            "build_year": 2005,
            "latitude": 52.19,
            "longitude": 21.02,
            "distance_km": 4.0,
            "observed_at": self.timestamp,
        }
        row.update(changes)
        return row

    def observations(self):
        frame = database.read_observations(self.engine)
        return frame.loc[frame["source"].isin(self.sources)]

    def current(self):
        frame = database.read_current_listings(self.engine)
        return frame.loc[frame["source"].isin(self.sources)]

    def test_first_observation_creates_current_and_history(self):
        self.assertEqual(database.upsert_listings(self.engine, [self.listing()]), 1)
        current = self.current().iloc[0]
        history = self.observations()
        self.assertEqual(len(history), 1)
        self.assertEqual(float(current["price_pln"]), 1_200_000)
        self.assertEqual(current["first_seen_at"], pd.Timestamp(self.timestamp))
        self.assertEqual(current["last_seen_at"], pd.Timestamp(self.timestamp))
        self.assertEqual(current["observed_at"], current["last_seen_at"])
        self.assertEqual(history.iloc[0]["observed_at"], pd.Timestamp(self.timestamp))

    def test_repeat_is_idempotent_even_if_payload_changes(self):
        original = self.listing()
        self.assertEqual(database.upsert_listings(self.engine, [original, original]), 1)
        self.assertEqual(database.upsert_listings(self.engine, [original]), 0)
        self.assertEqual(database.upsert_listings(self.engine, [self.listing(price_pln=999_000)]), 0)
        self.assertEqual(len(self.observations()), 1)
        self.assertEqual(float(self.current().iloc[0]["price_pln"]), 1_200_000)

    def test_newer_price_updates_current_and_retains_history(self):
        newer_time = self.timestamp + timedelta(days=1)
        database.upsert_listings(self.engine, [self.listing()])
        database.upsert_listings(self.engine, [self.listing(price_pln=1_150_000, observed_at=newer_time)])
        current = self.current().iloc[0]
        self.assertEqual(len(self.observations()), 2)
        self.assertEqual(float(current["price_pln"]), 1_150_000)
        self.assertEqual(current["first_seen_at"], pd.Timestamp(self.timestamp))
        self.assertEqual(current["last_seen_at"], pd.Timestamp(newer_time))
        self.assertEqual(current["observed_at"], pd.Timestamp(newer_time))

    def test_out_of_order_observation_does_not_revert_current(self):
        newer_time = self.timestamp + timedelta(days=1)
        database.upsert_listings(self.engine, [self.listing(price_pln=1_150_000, observed_at=newer_time)])
        database.upsert_listings(self.engine, [self.listing()])
        current = self.current().iloc[0]
        self.assertEqual(len(self.observations()), 2)
        self.assertEqual(float(current["price_pln"]), 1_150_000)
        self.assertEqual(current["first_seen_at"], pd.Timestamp(self.timestamp))
        self.assertEqual(current["last_seen_at"], pd.Timestamp(newer_time))

    def test_same_listing_id_from_different_sources_is_distinct(self):
        second_source = self.source + "_other"
        self.sources.append(second_source)
        database.upsert_listings(self.engine, [self.listing(), self.listing(source=second_source)])
        self.assertEqual(len(self.current()), 2)
        self.assertEqual(len(self.observations()), 2)

    def test_failed_batch_rolls_back_both_tables(self):
        rows = [self.listing(), self.listing(listing_id="invalid", price_pln=-1)]
        with self.assertRaises(IntegrityError):
            database.upsert_listings(self.engine, rows)
        self.assertTrue(self.current().empty)
        self.assertTrue(self.observations().empty)

    def test_timestamps_are_normalized_to_utc(self):
        local_time = self.timestamp.astimezone(timezone(timedelta(hours=2))).isoformat()
        database.upsert_listings(self.engine, [self.listing(observed_at=local_time)])
        self.assertEqual(self.observations().iloc[0]["observed_at"], pd.Timestamp(self.timestamp))

    def test_naive_timestamp_is_rejected(self):
        with self.assertRaises(ValueError):
            database.upsert_listings(self.engine, [self.listing(observed_at=self.timestamp.replace(tzinfo=None))])
        self.assertTrue(self.current().empty)
        self.assertTrue(self.observations().empty)

    def test_empty_batch_and_readers_keep_the_schema(self):
        self.assertEqual(database.upsert_listings(self.engine, []), 0)
        self.assertIn("observed_at", self.observations().columns)
        self.assertIn("last_seen_at", self.current().columns)
        self.assertIn("observed_at", self.current().columns)
        for frame in (self.observations(), self.current()):
            self.assertTrue(all(type(column) is str for column in frame.columns))


class TestSQLiteStorage(StorageContract, unittest.TestCase):
    expected_dialect = "sqlite"

    def database_url(self):
        return "sqlite:///:memory:"


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not configured")
class TestPostgreSQLStorage(StorageContract, unittest.TestCase):
    expected_dialect = "postgresql"

    def database_url(self):
        return os.environ["TEST_DATABASE_URL"]


class TestDatabaseConfiguration(unittest.TestCase):
    def test_import_does_not_construct_an_engine_or_read_dotenv(self):
        with patch("sqlalchemy.create_engine") as engine_mock, patch("dotenv.load_dotenv") as dotenv_mock:
            runpy.run_path(database.__file__, run_name="database_import_test")
        engine_mock.assert_not_called()
        dotenv_mock.assert_not_called()

    def test_missing_database_url_gives_a_configuration_error(self):
        with patch("src.database.load_dotenv"), patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                database.get_engine()

    def test_environment_url_is_used_without_connecting(self):
        with patch("src.database.load_dotenv"), patch.dict(os.environ, {"DATABASE_URL": "sqlite:///:memory:"}):
            engine = database.get_engine()
        self.addCleanup(engine.dispose)
        self.assertEqual(engine.dialect.name, "sqlite")

    def test_postgres_url_and_remote_ssl_are_normalized(self):
        engine = database.get_engine("postgres://test:test@example.test/apartments")
        self.addCleanup(engine.dispose)
        self.assertEqual(engine.url.drivername, "postgresql+psycopg2")
        self.assertEqual(engine.url.query["sslmode"], "require")

    def test_explicit_ssl_setting_is_preserved(self):
        engine = database.get_engine("postgresql://test:test@example.test/apartments?sslmode=verify-full")
        self.addCleanup(engine.dispose)
        self.assertEqual(engine.url.query["sslmode"], "verify-full")

    def test_cli_does_not_print_operational_error_details(self):
        error = OperationalError("private statement", {}, Exception("postgres://private:secret@host/db"))
        output = StringIO()
        with patch("src.database.get_engine", side_effect=error), redirect_stderr(output):
            self.assertEqual(database.main(["check"]), 1)
        self.assertIn("Nie udało się połączyć", output.getvalue())
        self.assertNotIn("secret", output.getvalue())
        self.assertNotIn("private", output.getvalue())


if __name__ == "__main__":
    unittest.main()
