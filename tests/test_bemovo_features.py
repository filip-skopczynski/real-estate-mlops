"""Synthetic RSC fixtures; tests make no requests and run no JavaScript."""

import json
import unittest
from unittest.mock import patch

from src.bemovo_features import parse_bemovo_features


def apartment(**updates):
    return {
        "number": "A0/01", "slugNumber": "A0-01", "id": "1",
        "price": 989157, "area": 58.53, "rooms": 3, "floor": 0,
        "city": "Warszawa", "investment": "Bemovo PH1", "status": "available",
        "building": "A", "isCommercialUnit": False, **updates,
    }


def rsc_record(rows, record_id="4"):
    props = ["$", "$L14", None, {"apartments": rows, "data": {"title": "Public catalogue"}}]
    return record_id + ":" + json.dumps(props, ensure_ascii=False) + "\n"


def html_from_chunks(chunks):
    return "<html><body>" + "".join(
        "<script>self.__next_f.push(" + json.dumps([1, chunk], ensure_ascii=False) + ")</script>"
        for chunk in chunks
    ) + "</body></html>"


class BemovoFeatureTests(unittest.TestCase):
    def test_apartment_fields_and_ground_floor_are_taken_from_explicit_props(self):
        rows = parse_bemovo_features(html_from_chunks([rsc_record([apartment()])]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], {
            "number": "A0/01", "slug_number": "A0-01", "source_id": "1",
            "city": "Warszawa", "investment": "Bemovo PH1", "building": "A",
            "status": "available", "is_commercial_unit": False,
            "area_m2": 58.53, "rooms": 3, "floor": 0,
            "website_price_pln": 989157.0,
            "feature_url": "https://bemovo.pl/pl/mieszkanie/A0-01/",
        })

    def test_split_chunks_are_joined_before_rsc_record_decoding(self):
        stream = '1:"$Sreact.fragment"\n5:I[9665,[],"Boundary"]\n:HL["/style.css","style"]\n' + rsc_record([apartment()]) + "6:null\n"
        chunks = [stream[:8], stream[8:43], stream[43:89], stream[89:140], stream[140:]]
        rows = parse_bemovo_features(html_from_chunks(chunks))
        self.assertEqual(rows[0]["number"], "A0/01")

    def test_all_statuses_and_commercial_units_remain_for_audit(self):
        rows = [
            apartment(),
            apartment(number="A1/02", slugNumber="A1-02", id="2", status="sold", floor=1),
            apartment(number="B1/01", slugNumber="B1-01", id="3", building="B", status="reserved", floor=1),
            apartment(number="U1", slugNumber="U1", id="128", status="sold", rooms=None, isCommercialUnit=True),
        ]
        parsed = parse_bemovo_features(html_from_chunks([rsc_record(rows)]))
        self.assertEqual(len(parsed), 4)
        self.assertEqual([row["status"] for row in parsed], ["available", "sold", "reserved", "sold"])
        self.assertIsNone(parsed[-1]["rooms"])
        self.assertTrue(parsed[-1]["is_commercial_unit"])

    def test_residential_missing_rooms_are_not_guessed(self):
        with self.assertRaisesRegex(ValueError, "Liczba pokoi"):
            parse_bemovo_features(html_from_chunks([rsc_record([apartment(rooms=None)])]))

    def test_exact_duplicate_rows_are_counted_once(self):
        parsed = parse_bemovo_features(html_from_chunks([rsc_record([apartment(), apartment()])]))
        self.assertEqual(len(parsed), 1)

    def test_conflicting_number_or_id_duplicates_raise(self):
        for duplicate in (
            apartment(price=1000000),
            apartment(number="A0/02", slugNumber="A0-02", id="1"),
            apartment(id="2"),
        ):
            with self.subTest(duplicate=duplicate), self.assertRaisesRegex(ValueError, "duplikaty"):
                parse_bemovo_features(html_from_chunks([rsc_record([apartment(), duplicate])]))

    def test_identical_catalogues_are_allowed_but_different_catalogues_are_not(self):
        stream = rsc_record([apartment()], "4") + rsc_record([apartment()], "8")
        self.assertEqual(len(parse_bemovo_features(html_from_chunks([stream]))), 1)
        conflicting = rsc_record([apartment()], "4") + rsc_record([apartment(price=1000000)], "8")
        with self.assertRaisesRegex(ValueError, "różne listy"):
            parse_bemovo_features(html_from_chunks([conflicting]))

    def test_partial_catalogue_is_not_chosen_over_a_different_full_catalogue(self):
        other = apartment(number="B0/01", slugNumber="B0-01", id="2", building="B")
        stream = rsc_record([apartment(), other], "4") + rsc_record([apartment()], "8")
        with self.assertRaisesRegex(ValueError, "różne listy"):
            parse_bemovo_features(html_from_chunks([stream]))

    def test_schema_changes_and_unknown_status_raise(self):
        invalid = [
            apartment(status="mystery"), apartment(isCommercialUnit="false"),
            apartment(area="58.53"), apartment(rooms=True), apartment(rooms=3.0),
            apartment(floor=None), apartment(floor=-1), apartment(floor=True),
            apartment(price=0), apartment(area=-1), apartment(city="Kraków"),
            apartment(investment="Other PH1"), apartment(slugNumber="wrong"),
            apartment(slugNumber="../../secret"), apartment(id=""),
        ]
        missing = apartment()
        del missing["rooms"]
        invalid.append(missing)
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(ValueError):
                parse_bemovo_features(html_from_chunks([rsc_record([row])]))

    def test_empty_or_non_list_catalogue_raises(self):
        for value in ([], None, "private-api-not-used", {"nodes": []}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_bemovo_features(html_from_chunks([rsc_record(value)]))

    def test_malformed_push_and_rsc_json_raise(self):
        cases = [
            '<script>self.__next_f.push([1, "unfinished")</script>',
            '<script>self.__next_f.push([1, "4:{}\\n"], extra())</script>',
            '<script>self.__next_f.push({"1":"payload"})</script>',
            '<script>self.__next_f.push([1, 123])</script>',
            html_from_chunks(['4:{"apartments": [broken]}\n']),
            html_from_chunks(['4:{"apartments": []} unexpected\n']),
            html_from_chunks(['not-a-record\n']),
            html_from_chunks(['4:{"apartments": [], "apartments": []}\n']),
            html_from_chunks(['4:{"apartments": [], "number": NaN}\n']),
        ]
        for html in cases:
            with self.subTest(html=html), self.assertRaises(ValueError):
                parse_bemovo_features(html)

    def test_conflicting_rsc_record_ids_raise(self):
        stream = rsc_record([apartment()], "4") + rsc_record([apartment(price=1100000)], "4")
        with self.assertRaisesRegex(ValueError, "Sprzeczne rekordy RSC"):
            parse_bemovo_features(html_from_chunks([stream]))

    def test_javascript_is_never_executed(self):
        # Both arbitrary script contents and strings in the valid JSON remain
        # inert data. A non-JSON argument is rejected rather than evaluated.
        html = '<script>throw new Error("Do not run JavaScript");</script>' + html_from_chunks([rsc_record([apartment()])])
        with patch("builtins.eval", side_effect=AssertionError("eval must not be called")), patch("builtins.exec", side_effect=AssertionError("exec must not be called")):
            self.assertEqual(len(parse_bemovo_features(html)), 1)
            with self.assertRaises(ValueError):
                parse_bemovo_features('<script>self.__next_f.push(alert("must not run"))</script>')

    def test_no_public_hydration_and_no_apartment_props_raise(self):
        for html in ("", "<html>static only</html>", html_from_chunks(['4:{"featuredApartments": []}\n'])):
            with self.subTest(html=html), self.assertRaises(ValueError):
                parse_bemovo_features(html)


if __name__ == "__main__":
    unittest.main()
