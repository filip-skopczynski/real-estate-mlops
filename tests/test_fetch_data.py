"""Offline behaviour tests: generated fixtures and fake HTTP responses only."""

import contextlib
import csv
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.fetch_data import (
    FetchError, canonical_url, fetch_listings, main, parse_listings,
)


FIXTURES = Path(__file__).parent / "fixtures"
PAGE_URL = "https://fixtures.example/warszawa"
OBSERVED_AT = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def html_for(node):
    return '<script type="application/ld+json">' + json.dumps(node) + "</script>"


def apartment(**updates):
    node = {
        "@type": "Apartment", "url": "/property/test?id=10",
        "address": {"addressLocality": "Warszawa"},
        "floorSize": {"value": 50, "unitCode": "MTK"},
        "numberOfRooms": 2,
        "offers": {"@type": "Offer", "price": 600000, "priceCurrency": "PLN"},
    }
    return {**node, **updates}


def response(status=200, html="", headers=None):
    return SimpleNamespace(status_code=status, text=html, headers=headers or {})


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class ParserTests(unittest.TestCase):
    def test_graph_references_produce_one_listing_with_correct_units(self):
        records = parse_listings(fixture("listings.html"), PAGE_URL, OBSERVED_AT)
        self.assertEqual(len(records), 1)
        row = records[0]
        self.assertEqual(row["listing_id"], "SALE-101")
        self.assertEqual(row["source"], "fixtures.example")
        self.assertEqual(row["city"], "Warszawa")
        self.assertEqual(row["district"], "Mokotów")
        self.assertEqual(row["rooms"], 3)  # numberOfBedrooms is deliberately 2.
        self.assertEqual(row["area_m2"], 60.5)
        self.assertEqual(row["price_pln"], 1200000.5)
        self.assertEqual(row["floor"], 2)
        self.assertEqual(row["build_year"], 2015)
        self.assertEqual(row["url"], "https://fixtures.example/property/101?id=101")
        self.assertGreater(row["distance_km"], 0)
        self.assertEqual(row["observed_at"], OBSERVED_AT)

    def test_embedded_offer_and_ground_floor(self):
        records = parse_listings(fixture("apartment.html"), PAGE_URL)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["area_m2"], 47.5)
        self.assertEqual(records[0]["price_pln"], 650000)
        self.assertEqual(records[0]["floor"], 0)
        self.assertIsNone(records[0]["distance_km"])
        self.assertEqual(records[0]["observed_at"].utcoffset().total_seconds(), 0)

    def test_listing_with_main_entity_own_offer(self):
        node = {"@type": "RealEstateListing", "identifier": "main-entity-sale", "url": "/sale/20", "mainEntity": apartment()}
        records = parse_listings(html_for(node), PAGE_URL)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["price_pln"], 600000)
        self.assertEqual(records[0]["listing_id"], "main-entity-sale")

    def test_explicit_house_is_not_accepted_as_apartment(self):
        house = {**apartment(), "@type": "House"}
        for node in [house, {"@type": "RealEstateListing", "mainEntity": house}, {"@type": "Offer", "price": 900000, "priceCurrency": "PLN", "itemOffered": house}]:
            with self.subTest(node=node):
                self.assertEqual(parse_listings(html_for(node), PAGE_URL), [])

    def test_price_per_square_metre_is_not_total_price(self):
        for spec in [
            {"price": 12000, "priceCurrency": "PLN", "unitText": "PLN/m²"},
            {"price": 12000, "priceCurrency": "PLN", "referenceQuantity": {"value": 1, "unitCode": "MTK"}},
        ]:
            with self.subTest(spec=spec):
                node = apartment(offers={"@type": "Offer", "priceSpecification": spec})
                self.assertEqual(parse_listings(html_for(node), PAGE_URL), [])

    def test_configured_centre_is_used_for_distance(self):
        node = apartment(geo={"latitude": 52.2, "longitude": 21.03})
        with patch.dict("os.environ", {"WARSAW_CENTER_LAT": "52.2", "WARSAW_CENTER_LON": "21.03"}):
            record = parse_listings(html_for(node), PAGE_URL)[0]
        self.assertEqual(record["distance_km"], 0)

    def test_explicit_identifier_wins_over_url_hash(self):
        row = parse_listings(html_for(apartment(identifier={"value": "abc123"})), PAGE_URL)[0]
        self.assertEqual(row["listing_id"], "abc123")

    def test_identifier_is_stable_when_only_trackers_or_observation_time_change(self):
        first = parse_listings(html_for(apartment(url="/property/test?id=10&utm_source=a#top")), PAGE_URL, OBSERVED_AT)[0]
        second = parse_listings(html_for(apartment(url="/property/test?id=10&fbclid=aaa&utm_source=b")), PAGE_URL)[0]
        other = parse_listings(html_for(apartment(url="/property/test?id=11")), PAGE_URL)[0]
        self.assertEqual(first["listing_id"], second["listing_id"])
        self.assertNotEqual(first["listing_id"], other["listing_id"])
        self.assertEqual(first["url"], "https://fixtures.example/property/test?id=10")

    def test_polish_number_formats(self):
        for price, expected in [("900\u00a0000,50 PLN", 900000.5), ("1.250.000,00", 1250000), ("1,250,000.00", 1250000), (1250000.75, 1250000.75)]:
            with self.subTest(price=price):
                records = parse_listings(html_for(apartment(offers={"@type": "Offer", "price": price, "priceCurrency": "PLN"})), PAGE_URL)
                self.assertEqual(records[0]["price_pln"], expected)

    def test_wrong_currency_and_unknown_currency_are_rejected(self):
        for currency in ("EUR", "USD", None):
            with self.subTest(currency=currency):
                offer = {"@type": "Offer", "price": 600000, "priceCurrency": currency}
                self.assertEqual(parse_listings(html_for(apartment(offers=offer)), PAGE_URL), [])

    def test_missing_rooms_are_not_replaced_with_bedrooms(self):
        self.assertEqual(parse_listings(html_for(apartment(numberOfRooms=None, numberOfBedrooms=2)), PAGE_URL), [])

    def test_missing_invalid_or_foreign_required_fields_are_rejected(self):
        changes = [
            {"address": {"addressLocality": "Kraków"}},
            {"floorSize": None}, {"floorSize": {"value": 500, "unitCode": "FTK"}},
            {"numberOfRooms": 1.5}, {"numberOfRooms": 0},
            {"offers": {"@type": "Offer", "price": "NaN", "priceCurrency": "PLN"}},
            {"offers": {"@type": "Offer", "price": -100, "priceCurrency": "PLN"}},
        ]
        for change in changes:
            with self.subTest(change=change):
                self.assertEqual(parse_listings(html_for(apartment(**change)), PAGE_URL), [])

    def test_malformed_jsonld_does_not_hide_other_valid_blocks(self):
        html = '<script type="application/ld+json">{broken}</script>' + html_for(apartment())
        with self.assertLogs("src.fetch_data", level="WARNING"):
            records = parse_listings(html, PAGE_URL)
        self.assertEqual(len(records), 1)
        self.assertEqual(parse_listings("<html>no structured data</html>", PAGE_URL), [])
        self.assertEqual(parse_listings(html_for({"@type": None}), PAGE_URL), [])

    def test_utc_timestamp_and_timezone_requirement(self):
        row = parse_listings(html_for(apartment()), PAGE_URL, "2026-10-05T10:00:00+02:00")[0]
        self.assertEqual(row["observed_at"], OBSERVED_AT)
        with self.assertRaises(ValueError):
            parse_listings(html_for(apartment()), PAGE_URL, datetime(2026, 10, 5))

    def test_url_keeps_listing_query_and_rejects_non_http(self):
        self.assertEqual(canonical_url("/offer?offer_id=1&page=2&utm_medium=email&gclid=x#top", PAGE_URL), "https://fixtures.example/offer?offer_id=1&page=2")
        with self.assertRaises(ValueError):
            canonical_url("javascript:alert(1)", PAGE_URL)


class NetworkTests(unittest.TestCase):
    def test_timeout_and_server_error_retry_with_required_impersonation(self):
        session = FakeSession([TimeoutError("temporary"), response(503), response(html=fixture("apartment.html"))])
        sleeps = []
        records = fetch_listings(PAGE_URL, delay=0.1, session=session, sleep=sleeps.append)
        self.assertEqual(len(records), 1)
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(sleeps, [0.1, 0.2, 0.4])
        for _, kwargs in session.calls:
            self.assertEqual(kwargs["impersonate"], "chrome120")
            self.assertEqual(kwargs["timeout"], 30)
            self.assertFalse(kwargs["allow_redirects"])

    def test_exhausted_timeout_raises_clear_error(self):
        session = FakeSession([TimeoutError()] * 3)
        with self.assertRaisesRegex(FetchError, "3 prób"):
            fetch_listings(PAGE_URL, delay=0, session=session)
        self.assertEqual(len(session.calls), 3)

    def test_access_denied_and_rate_limit_stop_without_retry(self):
        for status in (403, 429):
            with self.subTest(status=status):
                session = FakeSession([response(status)])
                with self.assertRaisesRegex(FetchError, str(status)):
                    fetch_listings(PAGE_URL, delay=0, session=session)
                self.assertEqual(len(session.calls), 1)

    def test_itemlist_fetches_only_same_origin_details_and_respects_limit(self):
        session = FakeSession([response(html=fixture("itemlist.html")), response(html=fixture("apartment.html"))])
        records = fetch_listings(PAGE_URL, max_pages=2, max_listings=1, delay=0, session=session)
        self.assertEqual(len(records), 1)
        self.assertEqual([call[0] for call in session.calls], [PAGE_URL, "https://fixtures.example/property/a?id=a"])

    def test_next_page_preserves_pagination_query(self):
        first = html_for(apartment(identifier="first")) + '<a rel="next" href="?page=2">next</a>'
        second = html_for(apartment(identifier="second", url="/property/second"))
        session = FakeSession([response(html=first), response(html=second)])
        records = fetch_listings(PAGE_URL, max_pages=2, delay=0, session=session)
        self.assertEqual(len(records), 2)
        self.assertEqual(session.calls[1][0], PAGE_URL + "?page=2")

    def test_cross_origin_redirect_is_not_followed(self):
        session = FakeSession([response(302, headers={"Location": "https://other.example/warszawa"})])
        with self.assertRaisesRegex(FetchError, "innej domeny"):
            fetch_listings(PAGE_URL, delay=0, session=session)
        self.assertEqual(len(session.calls), 1)

    def test_same_origin_redirect_uses_actual_page_as_relative_base(self):
        listing = html_for(apartment(url="property/test"))
        session = FakeSession([response(302, headers={"Location": "/sales/"}), response(html=listing)])
        row = fetch_listings(PAGE_URL, delay=0, session=session)[0]
        self.assertEqual(row["url"], "https://fixtures.example/sales/property/test")

    def test_no_structured_data_has_honest_adapter_error(self):
        session = FakeSession([response(html="<html>JavaScript only</html>")])
        with self.assertRaisesRegex(FetchError, "adaptera"):
            fetch_listings(PAGE_URL, delay=0, session=session)


class CliTests(unittest.TestCase):
    def test_offline_fixture_exports_csv_without_network_or_db(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "rows.csv"
            with patch("src.fetch_data.fetch_listings") as network, contextlib.redirect_stdout(io.StringIO()):
                code = main(["--html", str(FIXTURES / "listings.html"), "--url", PAGE_URL, "--source", "offline-fixture", "--no-db", "--output", str(output)])
            network.assert_not_called()
            self.assertEqual(code, 0)
            with output.open(encoding="utf-8", newline="") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["source"], "offline-fixture")
            self.assertEqual(rows[0]["price_pln"], "1200000.5")
            self.assertTrue(rows[0]["observed_at"].endswith("+00:00"))

    def test_no_source_config_requires_user_choice(self):
        with patch.dict("os.environ", {}, clear=True), patch("dotenv.load_dotenv"), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                main([])
        self.assertEqual(error.exception.code, 2)

    def test_dotenv_is_loaded_before_environment_defaults(self):
        def load_env(*args, **kwargs):
            import os
            os.environ["LISTINGS_URL"] = PAGE_URL
            os.environ["MAX_LISTINGS"] = "1"

        rows = parse_listings(fixture("apartment.html"), PAGE_URL)
        with patch.dict("os.environ", {}, clear=True), patch("dotenv.load_dotenv", side_effect=load_env) as dotenv, patch("src.fetch_data.fetch_listings", return_value=rows) as fetch, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--no-db"]), 0)
        self.assertFalse(dotenv.call_args.kwargs["override"])
        self.assertEqual(fetch.call_args.args, (PAGE_URL,))
        self.assertEqual(fetch.call_args.kwargs["max_listings"], 1)


if __name__ == "__main__":
    unittest.main()
