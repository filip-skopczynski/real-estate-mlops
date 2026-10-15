"""Display tested model candidates from synthetic CSVs without training or I/O services."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal
import pytest

from src import show_deals


NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize("field,left,right", [
    ("floor", 1, 8), ("build_year", 2000, 2020),
    ("latitude", 52.1, 52.2), ("longitude", 21.0, 21.1),
    ("distance_km", 2, 3), ("rooms_min", 1, 2),
])
def test_latest_tied_rows_with_different_property_features_are_not_ranked(field, left, right):
    selected, report = select(row(**{field: left}), row(**{field: right}))
    assert selected.empty
    assert report["eligible_listings"] == 0
    assert sum(report["excluded_counts"].values()) == 1


@pytest.mark.parametrize("field,value", [("floor", 2), ("build_year", 2000),
    ("latitude", 52.1), ("longitude", 21.0), ("distance_km", 2), ("rooms_min", 1)])
def test_numeric_property_feature_spelling_does_not_create_false_latest_conflict(field, value):
    selected, report = select(row(**{field: value}), row(**{field: str(float(value))}))
    assert len(selected) == 1
    assert report["eligible_listings"] == 1


def test_cli_rejects_conflicting_duplicate_prediction_headers_before_pandas_mangles_them(tmp_path, capsys):
    path = tmp_path / "ambiguous.csv"
    record = row()
    columns = list(record) + ["predicted_price_pln"]
    values = [str(record[column]) for column in record] + ["750000"]
    path.write_text(",".join(columns) + "\n" + ",".join(values) + "\n", encoding="utf-8")
    before = path.read_bytes()
    assert show_deals.main(["--input", str(path)]) == 2
    output = capsys.readouterr().out.casefold()
    assert "duplicate" in output and "traceback" not in output
    assert path.read_bytes() == before


def row(identity="00123", source="www.olx.pl", **changes):
    value = dict(source=source, listing_id=identity,
        url=(f"https://www.olx.pl/d/oferta/apartment-ID{identity}.html" if source == "www.olx.pl"
             else f"https://www.otodom.pl/pl/oferta/apartment-ID{identity}"),
        city="Warszawa", district="Mokotów", price_pln=800_000, area_m2=50,
        rooms=2, observed_at=(NOW - timedelta(days=1)).isoformat(),
        predicted_price_pln=1_000_000, is_deal=True,
        seen_in_training=False, partition="unseen")
    value.update(changes)
    return value


def select(*records, **options):
    return show_deals.select_deals(pd.DataFrame(records), now=NOW, **options)


def test_price_alone_does_not_rank_offers_and_original_frame_is_immutable():
    original = pd.DataFrame([row("1", price_pln=1000, predicted_price_pln=1_000_000),
        row("2"), row("3", price_pln=600_000, predicted_price_pln=650_000)],
        index=["z", "a", "a"])
    before = original.copy(deep=True)
    selected, report = show_deals.select_deals(original, now=NOW)
    assert_frame_equal(original, before)
    assert list(selected["listing_id"]) == ["2"]
    assert selected.iloc[0]["discount"] == pytest.approx(0.2)
    assert selected.iloc[0]["model_gap_pln"] == 200_000
    assert report["input_rows"] == 3 and report["eligible_listings"] == report["shown_listings"] == 1


def test_stored_discount_is_recomputed_and_tampering_cannot_make_an_offer_win():
    selected, report = select(row("1", discount=999, price_pln=900_000),
        row("2", discount=-999))
    assert list(selected["listing_id"]) == ["2"]
    assert selected.iloc[0]["discount"] == pytest.approx(0.2)
    assert report["threshold_fraction"] == 0.15


def test_rank_uses_relative_gap_then_absolute_gap_then_source_and_id():
    selected, report = select(row("3", source="www.otodom.pl"), row("2"), row("1"),
        row("4", price_pln=1_600_000, predicted_price_pln=2_000_000, area_m2=80),
        row("5", price_pln=700_000), top=4)
    assert list(selected["listing_id"]) == ["5", "4", "1", "2"]
    assert report["eligible_listings"] == 5 and report["shown_listings"] == 4


@pytest.mark.parametrize("is_deal,seen", [(True, False), ("True", "False"),
    ("true", "false"), (1, 0), ("1", "0")])
def test_explicit_boolean_and_zero_one_provenance_is_accepted(is_deal, seen):
    selected, _ = select(row(is_deal=is_deal, seen_in_training=seen))
    assert len(selected) == 1


@pytest.mark.parametrize("field", ["is_deal", "seen_in_training"])
@pytest.mark.parametrize("bad", [None, pd.NA, np.nan, "", "yes", "truthy", 2, -1, 0.5])
def test_ambiguous_boolean_values_are_excluded(field, bad):
    selected, report = select(row(**{field: bad}))
    assert selected.empty and report["eligible_listings"] == 0


@pytest.mark.parametrize("changes", [dict(is_deal=False), dict(seen_in_training=True),
    dict(partition="train"), dict(partition=None), dict(partition=""),
    dict(partition="unknown"), dict(partition="validation", seen_in_training=True)])
def test_training_or_missing_contradictory_provenance_is_excluded(changes):
    selected, report = select(row(**changes))
    assert selected.empty and report["shown_listings"] == 0


@pytest.mark.parametrize("partition", ["validation", "test", "unseen"])
def test_only_supported_nontraining_partitions_can_be_shown(partition):
    selected, _ = select(row(partition=partition))
    assert len(selected) == 1


@pytest.mark.parametrize("changes", [dict(price_pln=10_000), dict(price_pln=True), dict(price_pln=float("inf")),
    dict(price_pln=None), dict(area_m2=0), dict(area_m2=1000), dict(area_m2=None), dict(area_m2=True),
    dict(rooms=None), dict(rooms=2.5), dict(rooms=0), dict(rooms=True), dict(city="Kraków"),
    dict(predicted_price_pln=0), dict(predicted_price_pln=-1),
    dict(predicted_price_pln=float("inf")), dict(predicted_price_pln=float("nan")),
    dict(predicted_price_pln="bad"), dict(predicted_price_pln=True)])
def test_invalid_or_quality_review_candidates_are_not_shown(changes):
    selected, report = select(row(**changes))
    assert selected.empty and report["eligible_listings"] == 0


@pytest.mark.parametrize("age,accepted", [(0, True), (7, True), (7 + 1 / 86400, False), (-1 / 86400, False)])
def test_timestamp_age_boundary_and_future_are_enforced(age, accepted):
    selected, _ = select(row(observed_at=(NOW - timedelta(days=age)).isoformat()))
    assert len(selected) == int(accepted)


def test_aware_offset_timestamp_is_normalized_without_changing_original_value():
    original = pd.DataFrame([row(observed_at="2026-10-14T14:00:00+02:00")])
    before = original.copy(deep=True)
    selected, _ = show_deals.select_deals(original, now=NOW)
    assert len(selected) == 1
    assert pd.Timestamp(selected.iloc[0]["observed_at"]) == pd.Timestamp(NOW - timedelta(days=1))
    assert_frame_equal(original, before)


@pytest.mark.parametrize("bad", [None, pd.NA, "", "not-a-time", "2026-10-14T12:00:00", datetime(2026, 10, 14, 12)])
def test_invalid_and_timezone_naive_times_are_excluded(bad):
    selected, _ = select(row(observed_at=bad))
    assert selected.empty


def test_latest_nondeal_prevents_fallback_to_older_cheap_observation():
    selected, report = select(row(observed_at=(NOW - timedelta(days=2)).isoformat()),
        row(observed_at=(NOW - timedelta(days=1)).isoformat(), price_pln=980_000, is_deal=False))
    assert selected.empty and report["unique_listings"] == 1


@pytest.mark.parametrize("latest", ["invalid", "2026-10-15T11:00:00", "2026-10-16T12:00:00Z"])
def test_untrusted_or_future_time_for_identity_prevents_fallback_to_valid_old_offer(latest):
    selected, report = select(row(), row(observed_at=latest, price_pln=950_000))
    assert selected.empty and report["unique_listings"] == 1


@pytest.mark.parametrize("changes", [dict(price_pln=750_000), dict(area_m2=51), dict(rooms=3),
    dict(district="Wola"), dict(url="https://www.olx.pl/d/oferta/other-title-ID123.html"),
    dict(predicted_price_pln=1_200_000), dict(is_deal=False), dict(seen_in_training=True), dict(partition="test")])
def test_ambiguous_latest_ties_are_excluded_as_one_identity(changes):
    selected, report = select(row(), row(**changes))
    assert selected.empty and report["unique_listings"] == 1


def test_identical_latest_tie_is_one_listing_and_newer_valid_record_wins():
    selected, report = select(row(), row(), row("2", observed_at=(NOW - timedelta(days=2)).isoformat()),
        row("2", price_pln=700_000))
    assert list(selected["listing_id"]) == ["2", "00123"]
    assert report["input_rows"] == 4 and report["unique_listings"] == 2
    assert report["eligible_listings"] == 2


def test_latest_ties_ignore_untrusted_stored_discount_when_model_inputs_agree():
    selected, report = select(row(discount=-999), row(discount=999))
    assert len(selected) == report["eligible_listings"] == 1
    assert selected.iloc[0]["discount"] == pytest.approx(0.2)


@pytest.mark.parametrize("url", ["http://www.olx.pl/d/oferta/test.html",
    "javascript:alert(1)", "https://example.test/d/oferta/test.html",
    "https://www.otodom.pl/pl/oferta/test", "https://user@www.olx.pl/d/oferta/test.html",
    "https://www.olx.pl:443/d/oferta/test.html", "https://www.olx.pl/d/oferta/test.html?token=secret",
    "https://www.olx.pl/d/oferta/test.html#fragment", "https://www.olx.pl/not-an-offer",
    "https://www.olx.pl/d/oferta/test\nFAKE.html", "https://www.olx.pl/d/oferta/test%0aFAKE.html",
    "https://www.olx.pl/d/oferta/test%00.html", "https://www.olx.pl/d/oferta/test\x1b.html"])
def test_unsafe_or_noncanonical_links_are_excluded(url):
    selected, _ = select(row(url=url))
    assert selected.empty


@pytest.mark.parametrize("changes", [dict(source="example.test"), dict(source=None),
    dict(listing_id=""), dict(listing_id="0"), dict(listing_id="-1"),
    dict(listing_id="12.5"), dict(listing_id="not-an-id"), dict(listing_id="１２３"), dict(listing_id=True)])
def test_unknown_sources_and_nonpositive_nondigit_identities_are_excluded(changes):
    selected, _ = select(row(**changes))
    assert selected.empty


def test_threshold_is_inclusive_and_can_be_changed_without_using_stored_flag_score():
    selected, report = select(row(price_pln=850_000), discount_threshold=0.15)
    assert len(selected) == 1 and selected.iloc[0]["discount"] == pytest.approx(0.15)
    selected, _ = select(row(price_pln=850_001), discount_threshold=0.15)
    assert selected.empty
    selected, _ = select(row(price_pln=850_001), discount_threshold=0.1)
    assert len(selected) == 1


def test_zero_threshold_allows_zero_gap_but_never_negative_model_gap():
    selected, _ = select(row("1", price_pln=1_000_000), row("2", price_pln=1_000_001), discount_threshold=0)
    assert list(selected["listing_id"]) == ["1"]
    assert selected.iloc[0]["discount"] == selected.iloc[0]["model_gap_pln"] == 0


def test_existing_quality_annotations_are_rechecked_instead_of_trusted():
    original = pd.DataFrame([row("1", price_pln=1000, quality_status="pass", quality_flags="", audit_price_per_m2=20_000),
        row("2", quality_status="review", quality_flags="stale_label", audit_price_per_m2=-1)])
    before = original.copy(deep=True)
    selected, _ = show_deals.select_deals(original, now=NOW)
    assert list(selected["listing_id"]) == ["2"]
    assert_frame_equal(original, before)


@pytest.mark.parametrize("settings", [dict(top=True), dict(top=0), dict(top=51), dict(top=1.5),
    dict(max_age_days=True), dict(max_age_days=0), dict(max_age_days=-1),
    dict(max_age_days=float("nan")), dict(max_age_days=float("inf")),
    dict(discount_threshold=True), dict(discount_threshold=-0.1), dict(discount_threshold=1),
    dict(discount_threshold=float("nan")), dict(discount_threshold=float("inf"))])
def test_invalid_display_settings_fail(settings):
    with pytest.raises(ValueError):
        select(row(), **settings)


def test_empty_well_formed_input_and_report_counts_are_consistent():
    selected, report = show_deals.select_deals(pd.DataFrame(columns=row()), now=NOW)
    assert selected.empty
    assert report["input_rows"] == report["unique_listings"] == report["eligible_listings"] == report["shown_listings"] == 0
    assert report["max_age_days"] == 7 and report["threshold_fraction"] == 0.15
    assert pd.Timestamp(report["as_of"]) == pd.Timestamp(NOW)
    json.dumps(report, allow_nan=False)


def test_exclusion_accounting_is_per_identity_and_top_limit_is_separate():
    selected, report = select(row("1"), row("1"), row("2", price_pln=700_000),
        row("3", seen_in_training=True), row("4", is_deal=False), top=1)
    assert len(selected) == report["shown_listings"] == 1
    assert report["input_rows"] == 5 and report["unique_listings"] == 4
    assert report["eligible_listings"] == 2
    assert sum(report["excluded_counts"].values()) == 2
    assert all(type(value) is int and value >= 0 for value in report["excluded_counts"].values())


@pytest.mark.parametrize("field", [field for field in row() if field != "district"])
def test_missing_required_schema_fails_without_mutating_input(field):
    original = pd.DataFrame([row()]).drop(columns=field)
    before = original.copy(deep=True)
    with pytest.raises(ValueError):
        show_deals.select_deals(original, now=NOW)
    assert_frame_equal(original, before)


def test_duplicate_column_names_are_rejected():
    original = pd.DataFrame([row()])
    original = pd.concat([original, original[["is_deal"]]], axis=1)
    with pytest.raises(ValueError):
        show_deals.select_deals(original, now=NOW)


def test_formatter_shows_price_size_freshness_model_gap_and_full_safe_link():
    selected, report = select(row(), row("2", source="www.otodom.pl", price_pln=700_000), top=1)
    text = show_deals.format_deals(selected, report)
    assert row("2", source="www.otodom.pl")["url"] in text
    assert row()["url"] not in text
    assert "Mokotów" in text and "50" in text and "2026-10-14" in text
    compact = text.replace(" ", " ").replace(" ", "").replace(",", "")
    assert "700000" in compact and "1000000" in compact and "300000" in compact


def test_optional_district_is_not_required_to_display_offer_link():
    original = pd.DataFrame([row()]).drop(columns="district")
    selected, report = show_deals.select_deals(original, now=NOW)
    assert len(selected) == 1
    assert row()["url"] in show_deals.format_deals(selected, report)


def test_formatter_sanitizes_district_newlines_ansi_and_control_characters():
    selected, report = select(row(district="Mokotów\nFAKE OFFER\x1b[31m\x00"))
    assert len(selected) == 1
    text = show_deals.format_deals(selected, report)
    assert "\x1b" not in text and "\x00" not in text and "Mokotów\nFAKE OFFER" not in text
    assert row()["url"] in text


def test_cli_missing_input_returns_two_without_creating_files(tmp_path, capsys):
    absent = tmp_path / "deals.csv"
    assert show_deals.main(["--input", str(absent)]) == 2
    message = capsys.readouterr()
    assert message.out.strip() or message.err.strip()
    assert "Traceback" not in message.out + message.err
    assert list(tmp_path.iterdir()) == []


def test_import_and_missing_input_cli_do_not_import_database_module(tmp_path):
    command = ("import sys; from src import show_deals; "
        "assert show_deals.main(['--input', sys.argv[1]]) == 2; "
        "assert 'src.database' not in sys.modules")
    result = subprocess.run([sys.executable, "-c", command, str(tmp_path / "missing.csv")],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stdout + result.stderr


def test_cli_prints_top_links_preserves_zero_prefixed_ids_and_input(tmp_path, capsys, monkeypatch):
    path = tmp_path / "deals.csv"
    pd.DataFrame([row(), row("00042", price_pln=700_000)]).to_csv(path, index=False)
    before = path.read_bytes()
    real_select = show_deals.select_deals
    observed = []
    def fixed_clock(frame, **options):
        observed.append(frame.copy(deep=True))
        options["now"] = NOW
        return real_select(frame, **options)
    monkeypatch.setattr(show_deals, "select_deals", fixed_clock)
    assert show_deals.main(["--input", str(path), "--top", "1", "--discount-threshold", "15"]) == 0
    text = capsys.readouterr().out
    assert row("00042")["url"] in text and row()["url"] not in text
    assert list(observed[0]["listing_id"]) == ["00123", "00042"]
    assert path.read_bytes() == before and list(tmp_path.iterdir()) == [path]


def test_cli_zero_eligible_candidates_is_success_and_explains_empty_result(tmp_path, capsys):
    path = tmp_path / "deals.csv"
    pd.DataFrame([row(seen_in_training=True)]).to_csv(path, index=False)
    assert show_deals.main(["--input", str(path)]) == 0
    message = capsys.readouterr().out
    assert message.strip() and row()["url"] not in message
