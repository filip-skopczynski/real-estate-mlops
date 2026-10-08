"""Audit quality without discarding observations or touching production data."""
from dataclasses import asdict
from datetime import datetime, timezone
import json

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal
import pytest

from src import quality


def row(identity="00123", **changes):
    value = dict(source="www.olx.pl", listing_id=identity, city="Warszawa",
        price_pln=900_000, area_m2=50.0, rooms=2,
        observed_at="2026-10-08T04:35:00+00:00")
    value.update(changes)
    return value


def flags(frame, position=0):
    value = frame.iloc[position]["quality_flags"]
    return set(value.split("|")) if value else set()


def audit(*records, **options):
    return quality.audit_catalog(pd.DataFrame(records), **options)


def test_audit_preserves_input_values_row_order_duplicate_index_and_optional_fields():
    original = pd.DataFrame([row("1", price_pln=None), row("2"),
        row("3", custom_note="Keep this text", floor=7)], index=["z", "a", "a"])
    original["district"] = [pd.NA, "Mokotów", "Wola"]
    before = original.copy(deep=True)
    annotated, report = quality.audit_catalog(original)
    assert_frame_equal(original, before)
    assert_frame_equal(annotated[original.columns], before)
    assert list(annotated.index) == ["z", "a", "a"]
    assert list(annotated["quality_status"]) == ["incomplete", "pass", "pass"]
    assert flags(annotated, 0) == {"missing_price"} and flags(annotated, 1) == set()
    assert report["input_rows"] == 3 and report["unique_identities"] == 3


def test_minimal_columns_need_no_district_url_or_optional_features():
    annotated, report = audit(row())
    assert annotated.iloc[0]["quality_status"] == "pass"
    assert annotated.iloc[0]["audit_price_per_m2"] == 18_000
    assert report["status_counts"] == {"pass": 1, "incomplete": 0, "review": 0}
    assert report["thresholds"] == asdict(quality.QualityThresholds())
    assert report["schema_version"] == 1
    assert isinstance(report["warnings"], list) and report["warnings"]


@pytest.mark.parametrize("missing", [None, pd.NA, np.nan])
@pytest.mark.parametrize("field,flag", [("price_pln", "missing_price"),
    ("area_m2", "missing_area"), ("rooms", "missing_exact_rooms")])
def test_missing_values_are_incomplete_without_becoming_invalid(field, flag, missing):
    annotated, report = audit(row(**{field: missing}))
    assert flags(annotated) == {flag}
    assert annotated.iloc[0]["quality_status"] == "incomplete"
    assert report["missing_by_source"]["www.olx.pl"][field] == 1
    assert report["flag_counts"][flag] == 1


@pytest.mark.parametrize("field,flag", [("price_pln", "invalid_price"), ("area_m2", "invalid_area")])
@pytest.mark.parametrize("value", ["not-a-number", float("inf"), float("-inf"), 0, -1, True])
def test_present_invalid_prices_and_areas_are_reviewed_and_have_no_ratio(field, flag, value):
    annotated, report = audit(row(**{field: value}))
    assert flag in flags(annotated)
    assert annotated.iloc[0]["quality_status"] == "review"
    assert pd.isna(annotated.iloc[0]["audit_price_per_m2"])
    assert report["numeric_summary"][field]["count"] == 0
    assert report["numeric_summary"]["price_per_m2"]["count"] == 0
    json.dumps(report, allow_nan=False)


def test_ratio_overflow_from_finite_positive_inputs_is_not_exported_as_infinity():
    annotated, report = audit(row(price_pln=1e308, area_m2=1e-308))
    assert annotated.iloc[0]["price_pln"] == 1e308
    assert annotated.iloc[0]["area_m2"] == 1e-308
    assert pd.isna(annotated.iloc[0]["audit_price_per_m2"])
    assert report["numeric_summary"]["price_per_m2"]["count"] == 0
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("value", ["two", 2.5, 0, -1, float("inf"), True])
def test_invalid_exact_room_values_are_reviewed(value):
    annotated, _ = audit(row(rooms=value))
    assert flags(annotated) == {"invalid_rooms"}
    assert annotated.iloc[0]["quality_status"] == "review"


def test_lower_bound_rooms_preserves_four_or_more_as_incomplete():
    annotated, _ = audit(row(rooms=None, rooms_min=4), row("2", rooms=2, rooms_min=3))
    assert flags(annotated, 0) == {"missing_exact_rooms"}
    assert annotated.iloc[0]["rooms_min"] == 4
    assert flags(annotated, 1) == {"rooms_below_minimum"}
    assert list(annotated["quality_status"]) == ["incomplete", "review"]


@pytest.mark.parametrize("price,area", [(100_000, 20), (10_000_000, 500),
    (100_000, 10), (1_500_000, 500), (1_000_000, 20)])
def test_default_review_boundaries_are_inclusive(price, area):
    annotated, _ = audit(row(price_pln=price, area_m2=area))
    assert flags(annotated) == set() and annotated.iloc[0]["quality_status"] == "pass"


@pytest.mark.parametrize("price,area,expected", [
    (99_999, 20, {"price_below_review_min"}),
    (10_000_001, 500, {"price_above_review_max"}),
    (500_000, 9, {"area_below_review_min", "price_per_m2_above_review_max"}),
    (1_500_000, 501, {"area_above_review_max", "price_per_m2_below_review_min"}),
    (1_499_999, 500, {"price_per_m2_below_review_min"}),
    (1_000_001, 20, {"price_per_m2_above_review_max"}),
])
def test_suspicious_values_are_flagged_without_deleting_or_changing_them(price, area, expected):
    annotated, report = audit(row(price_pln=price, area_m2=area))
    assert flags(annotated) == expected
    assert annotated.iloc[0]["price_pln"] == price and annotated.iloc[0]["area_m2"] == area
    assert report["numeric_summary"]["price_pln"]["count"] == 1
    assert report["status_counts"]["review"] == 1


def test_review_thresholds_are_configuration_not_automatic_training_decisions():
    source = pd.DataFrame([row(price_pln=90_000, area_m2=50)])
    original = source.copy(deep=True)
    default, _ = quality.audit_catalog(source)
    custom = quality.QualityThresholds(price_min=80_000, price_per_m2_min=1500)
    relaxed, report = quality.audit_catalog(source, thresholds=custom)
    assert default.iloc[0]["quality_status"] == "review"
    assert relaxed.iloc[0]["quality_status"] == "pass"
    assert "eligible_for_training" not in relaxed.columns
    assert report["thresholds"] == asdict(custom)
    assert_frame_equal(source, original)


@pytest.mark.parametrize("field", ["price_min", "price_max", "area_min", "area_max", "price_per_m2_min", "price_per_m2_max"])
@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), float("-inf"), "100", -1])
def test_thresholds_reject_nonfinite_nonnumeric_bool_and_negative(field, value):
    with pytest.raises((ValueError, TypeError)):
        quality.QualityThresholds(**{field: value})


@pytest.mark.parametrize("values", [dict(price_min=20_000_000), dict(area_min=501), dict(price_per_m2_min=50_001)])
def test_thresholds_reject_reversed_ranges(values):
    with pytest.raises(ValueError):
        quality.QualityThresholds(**values)


@pytest.mark.parametrize("field,flag", [("source", "invalid_source"), ("listing_id", "invalid_listing_id")])
@pytest.mark.parametrize("value", [None, pd.NA, "", "   "])
def test_empty_source_identity_is_reviewed(field, flag, value):
    annotated, _ = audit(row(**{field: value}))
    assert flag in flags(annotated) and annotated.iloc[0]["quality_status"] == "review"


@pytest.mark.parametrize("city", [None, pd.NA, "Kraków", ""])
def test_non_warsaw_records_are_retained_for_review(city):
    annotated, _ = audit(row(city=city))
    assert flags(annotated) == {"not_warsaw"}
    assert len(annotated) == 1


@pytest.mark.parametrize("timestamp", [None, pd.NA, "", "not-a-date"])
def test_invalid_observation_times_are_reviewed(timestamp):
    annotated, report = audit(row(observed_at=timestamp))
    assert flags(annotated) == {"invalid_observed_at"}
    assert report["observation_range"]["first"] is None
    assert report["observation_range"]["last"] is None


@pytest.mark.parametrize("timestamp", ["2026-10-08T04:35:00", datetime(2026, 10, 8, 4, 35), pd.Timestamp("2026-10-08")])
def test_parseable_naive_times_require_timezone_review(timestamp):
    annotated, _ = audit(row(observed_at=timestamp))
    assert flags(annotated) == {"unverified_timestamp_timezone"}
    assert annotated.iloc[0]["quality_status"] == "review"


def test_mixed_timezone_inputs_report_true_utc_days_and_range():
    records = [row("1", observed_at="2026-10-08T00:30:00+02:00"),
        row("2", observed_at="2026-10-07T22:30:00Z"),
        row("3", observed_at=datetime(2026, 10, 8, 4, 35, tzinfo=timezone.utc))]
    annotated, report = audit(*records)
    assert list(annotated["quality_status"]) == ["pass"] * 3
    extent = report["observation_range"]
    assert pd.Timestamp(extent["first"]) == pd.Timestamp("2026-10-07T22:30:00Z")
    assert pd.Timestamp(extent["last"]) == pd.Timestamp("2026-10-08T04:35:00Z")
    assert extent["utc_days"] == 2


def test_repeated_identity_history_is_reviewed_without_claiming_price_conflict():
    annotated, report = audit(row(), row(observed_at="2026-10-09T04:35:00Z", price_pln=850_000))
    assert [flags(annotated, index) for index in range(2)] == [{"repeated_identity"}] * 2
    assert report["unique_identities"] == 1 and report["duplicate_identity_rows"] == 1
    assert report["flag_counts"]["repeated_identity"] == 2
    assert report["status_counts"] == {"pass": 0, "incomplete": 0, "review": 2}


def test_same_id_on_two_sources_does_not_create_repeated_identity():
    annotated, report = audit(row(), row(source="www.otodom.pl"))
    assert list(annotated["quality_status"]) == ["pass", "pass"]
    assert report["unique_identities"] == 2 and report["duplicate_identity_rows"] == 0


@pytest.mark.parametrize("changed", [dict(price_pln=850_000), dict(area_m2=51),
    dict(rooms=3), dict(district="Wola"), dict(url="https://www.olx.pl/d/oferta/other.html")])
def test_conflicting_same_instant_identity_flags_every_member_of_group(changed):
    base = row(district="Mokotów", url="https://www.olx.pl/d/oferta/apartment.html")
    later = dict(base, observed_at="2026-10-08T06:35:00+02:00", **changed)
    annotated, report = audit(base, later)
    expected = {"repeated_identity", "conflicting_identity_timestamp"}
    assert flags(annotated, 0) == flags(annotated, 1) == expected
    assert report["flag_counts"]["conflicting_identity_timestamp"] == 2


def test_identical_missing_optional_fields_and_unrelated_feature_changes_do_not_conflict():
    annotated, _ = audit(row(district=pd.NA, floor=2), row(district=pd.NA, floor=3))
    assert flags(annotated, 0) == flags(annotated, 1) == {"repeated_identity"}


def test_counts_missingness_numeric_summaries_and_deterministic_flag_order_reconcile():
    records = [row("1", price_pln=100_000, area_m2=20),
        row("2", source="www.otodom.pl", price_pln=None, rooms=None),
        row("3", source="www.otodom.pl", price_pln=float("inf"), area_m2=None),
        row("4", price_pln=900_000, area_m2=50)]
    annotated, report = audit(*records)
    assert report["input_rows"] == sum(report["status_counts"].values()) == 4
    assert report["status_counts"] == {"pass": 2, "incomplete": 1, "review": 1}
    assert report["missing_by_source"]["www.otodom.pl"]["price_pln"] == 1
    assert report["missing_by_source"]["www.otodom.pl"]["area_m2"] == 1
    assert report["missing_by_source"]["www.otodom.pl"]["rooms"] == 1
    assert report["numeric_summary"]["price_pln"]["count"] == 2
    assert report["numeric_summary"]["price_pln"]["min"] == 100_000
    assert report["numeric_summary"]["price_pln"]["median"] == 500_000
    assert report["numeric_summary"]["price_per_m2"]["median"] == 11_500
    for field in ("price_pln", "area_m2", "price_per_m2"):
        values = report["numeric_summary"][field]
        assert values["min"] <= values["p01"] <= values["median"] <= values["p99"] <= values["max"]
    for flag, expected in report["flag_counts"].items():
        assert sum(flag in flags(annotated, position) for position in range(4)) == expected
    for encoded in annotated["quality_flags"]:
        assert encoded.split("|") == sorted(encoded.split("|"))
    json.dumps(report, allow_nan=False)


def test_empty_well_formed_catalogue_has_safe_empty_report():
    original = pd.DataFrame(columns=row().keys())
    annotated, report = quality.audit_catalog(original)
    assert_frame_equal(annotated[original.columns], original)
    assert report["input_rows"] == report["unique_identities"] == report["duplicate_identity_rows"] == 0
    assert report["status_counts"] == {"pass": 0, "incomplete": 0, "review": 0}
    assert report["flag_counts"] == {}
    assert report["observation_range"] == {"first": None, "last": None, "utc_days": 0}
    for values in report["numeric_summary"].values():
        assert values == {"count": 0, "min": None, "p01": None, "median": None, "p99": None, "max": None}
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("column", list(row()))
def test_missing_required_column_fails_without_modifying_input(column):
    frame = pd.DataFrame([row()]).drop(columns=column)
    before = frame.copy(deep=True)
    with pytest.raises(ValueError):
        quality.audit_catalog(frame)
    assert_frame_equal(frame, before)


@pytest.mark.parametrize("column", ["quality_flags", "quality_status", "audit_price_per_m2"])
def test_reserved_annotation_collision_fails_without_overwriting_user_data(column):
    frame = pd.DataFrame([row(**{column: "Existing user value"})])
    before = frame.copy(deep=True)
    with pytest.raises(ValueError):
        quality.audit_catalog(frame)
    assert_frame_equal(frame, before)


def test_cli_writes_reports_preserves_leading_zero_ids_and_keeps_input(monkeypatch, tmp_path):
    input_path, output_dir = tmp_path / "catalog.csv", tmp_path / "quality"
    pd.DataFrame([row(), row("00042", source="www.otodom.pl", price_pln=None)]).to_csv(input_path, index=False)
    before = input_path.read_bytes()
    assert quality.main(["--input", str(input_path), "--output-dir", str(output_dir)]) == 0
    assert input_path.read_bytes() == before
    expected = {"annotated_catalog.csv", "quality_report.json", "duplicate_candidates.csv", "quality_report.md"}
    assert {path.name for path in output_dir.iterdir()} == expected
    saved = pd.read_csv(output_dir / "annotated_catalog.csv", dtype={"listing_id": "string"})
    assert list(saved["listing_id"]) == ["00123", "00042"]
    assert list(saved["quality_status"]) == ["pass", "incomplete"]
    report = json.loads((output_dir / "quality_report.json").read_text(encoding="utf-8"))
    assert report["input_rows"] == 2 and report["status_counts"]["incomplete"] == 1
    assert (output_dir / "quality_report.md").read_text(encoding="utf-8").strip()


@pytest.mark.parametrize("input_name", ["annotated_catalog.csv", "quality_report.json", "duplicate_candidates.csv", "quality_report.md"])
def test_cli_rejects_input_output_collision_before_writing(tmp_path, input_name):
    input_path = tmp_path / input_name
    pd.DataFrame([row()]).to_csv(input_path, index=False)
    before = input_path.read_bytes()
    with pytest.raises(SystemExit) as stopped:
        quality.main(["--input", str(input_path), "--output-dir", str(tmp_path)])
    assert stopped.value.code != 0
    assert input_path.read_bytes() == before
    assert {path.name for path in tmp_path.iterdir()} == {input_name}


def test_cli_malformed_numeric_values_still_produce_finite_json_report(tmp_path):
    input_path, output_dir = tmp_path / "catalog.csv", tmp_path / "quality"
    pd.DataFrame([row(price_pln=float("inf")), row("2", area_m2="bad"),
        row("3", rooms=None)]).to_csv(input_path, index=False)
    assert quality.main(["--input", str(input_path), "--output-dir", str(output_dir)]) == 0
    def reject_nonfinite(value):
        raise AssertionError("Non-finite JSON constant: " + value)
    report = json.loads((output_dir / "quality_report.json").read_text(encoding="utf-8"), parse_constant=reject_nonfinite)
    assert report["status_counts"] == {"pass": 0, "incomplete": 1, "review": 2}
    assert report["numeric_summary"]["price_per_m2"]["count"] == 1


@pytest.mark.parametrize("failed_name", ["annotated_catalog.csv", "duplicate_candidates.csv", "quality_report.json", "quality_report.md"])
def test_failed_report_replacement_restores_previous_complete_bundle(tmp_path, monkeypatch, failed_name):
    input_path, output_dir = tmp_path / "catalog.csv", tmp_path / "quality"
    pd.DataFrame([row()]).to_csv(input_path, index=False)
    quality.main(["--input", str(input_path), "--output-dir", str(output_dir)])
    previous = {path.name: path.read_bytes() for path in output_dir.iterdir()}
    pd.DataFrame([row(price_pln=3200)]).to_csv(input_path, index=False)
    unchanged_input = input_path.read_bytes()
    replace = quality.os.replace
    failed = False

    def fail_one_replace(source, destination):
        nonlocal failed
        if not failed and str(destination) == str(output_dir / failed_name):
            failed = True
            raise OSError("simulated report write failure")
        return replace(source, destination)

    monkeypatch.setattr(quality.os, "replace", fail_one_replace)
    with pytest.raises(SystemExit) as stopped:
        quality.main(["--input", str(input_path), "--output-dir", str(output_dir)])
    assert stopped.value.code != 0
    assert failed
    assert {path.name: path.read_bytes() for path in output_dir.iterdir()} == previous
    assert input_path.read_bytes() == unchanged_input
