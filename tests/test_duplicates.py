"""Offline similarity review candidates; same attributes never prove identity."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal
import pytest

from src import duplicates

NOW = datetime(2026, 10, 8, 9, tzinfo=timezone.utc)


def listing(source="www.olx.pl", identifier="123", **changes):
    url = f"https://{source}/d/oferta/synthetic-ID{identifier}.html" if source == "www.olx.pl" else f"https://{source}/pl/oferta/synthetic-ID{identifier}"
    return dict(source=source, listing_id=identifier, url=url, city="Warszawa", district="Mokotów",
                price_pln=1000000.0, area_m2=50.0, rooms=2, rooms_min=None, floor=3,
                observed_at=NOW, **changes) if not changes else ({**listing(source, identifier), **changes})


def pair(**changes):
    return pd.DataFrame([listing(), listing("www.otodom.pl", "456", **changes)])


def find(frame=None, **kwargs):
    return duplicates.find_duplicate_candidates(pair() if frame is None else frame, **kwargs)


def test_identical_new_build_flats_are_candidates_only_and_input_is_immutable():
    frame = pair()
    before = frame.copy(deep=True)
    result, meta = find(frame)
    assert_frame_equal(frame, before)
    assert list(result.columns) == list(duplicates.CANDIDATE_COLUMNS)
    assert len(result) == 1 and result.iloc[0].status == "candidate_needs_review"
    assert result.iloc[0].evidence == "similar_district_rooms_area_price"
    assert meta["unique_listings"] == meta["eligible_listings"] == 2
    assert meta["candidate_pairs"] == 1 and not meta["truncated"]
    assert "no confirmed duplicates" in meta["interpretation"]
    assert "price-based" in meta["training_split_use"]
    json.dumps(meta, allow_nan=False)


def test_source_ids_are_namespaced_and_equal_numeric_ids_can_be_candidates():
    result, _ = find(pd.DataFrame([listing(identifier="123"), listing("www.otodom.pl", "123")]))
    assert len(result) == 1 and result.iloc[0].left_listing_id == result.iloc[0].right_listing_id


def test_same_source_never_produces_candidates():
    result, meta = find(pd.DataFrame([listing(identifier="1"), listing(identifier="2")]))
    assert result.empty and meta["comparisons"] == 0


def test_full_district_nfc_casefold_whitespace_and_city_normalization():
    frame = pd.DataFrame([listing(district="  Żoliborz  ", city=" WARSZAWA "),
                          listing("www.otodom.pl", "456", district="Z\u0307OLIBORZ", city="warszawa")])
    result, _ = find(frame)
    assert len(result) == 1 and result.iloc[0].district == "żoliborz"


@pytest.mark.parametrize("change", [
    {"district": "Mok"}, {"district": "Mokotów Północny"}, {"district": "Wola"},
    {"rooms": 3}, {"city": "Kraków"}, {"city": None},
])
def test_distinct_blocks_or_unconfirmed_city_do_not_match(change):
    result, _ = find(pair(**change))
    assert result.empty


@pytest.mark.parametrize("column", ["district", "floor"])
def test_missing_optional_columns_do_not_raise(column):
    result, meta = find(pair().drop(columns=[column]))
    assert column in meta["optional_missing_columns"]
    if column == "district":
        assert result.empty and meta["counts_by_reason"]["unknown_district"] == 2
    else:
        assert len(result) == 1 and result.iloc[0].floor_evidence == "unknown"


@pytest.mark.parametrize("bad", [None, np.nan, pd.NA, "", "  ", "NaN", "unknown"])
def test_unknown_district_excludes_identity(bad):
    result, meta = find(pair(district=bad))
    assert result.empty and meta["counts_by_reason"]["unknown_district"] == 1


@pytest.mark.parametrize("column", ["price_pln", "area_m2"])
@pytest.mark.parametrize("bad", [None, np.nan, pd.NA, float("inf"), -float("inf"), 0, -1, True,
                                "1 000 000 zł", "+1000000", "-1000000", "1e6", "1,000", Decimal("NaN")])
def test_nonfinite_nonpositive_or_malformed_features_do_not_match(column, bad):
    result, meta = find(pair(**{column: bad}))
    assert result.empty and meta["eligible_listings"] == 1


def test_plain_numeric_strings_decimal_and_numpy_scalars_are_supported():
    frame = pd.DataFrame([listing(price_pln="1000000.00", area_m2=Decimal("50.0"), rooms=np.int64(2)),
                          listing("www.otodom.pl", "456", price_pln=np.float64(1000000), area_m2="50.0", rooms=2.0)])
    result, _ = find(frame)
    assert len(result) == 1


@pytest.mark.parametrize("bad", [None, pd.NA, np.nan, 0, -1, 2.5, True, "4+", "FOUR"])
def test_rooms_min_never_invents_exact_rooms(bad):
    result, meta = find(pair(rooms=bad, rooms_min=4))
    assert result.empty and meta["counts_by_reason"]["unknown_exact_rooms"] == 1


def test_rooms_min_contradiction_is_excluded():
    result, meta = find(pair(rooms=2, rooms_min=4))
    assert result.empty and meta["counts_by_reason"]["contradictory_rooms_min"] == 1


@pytest.mark.parametrize("bad", [None, pd.NA, np.nan, "unknown", "3rd", 3.5, True])
def test_unknown_floor_keeps_candidate_with_explicit_weaker_evidence(bad):
    result, _ = find(pair(floor=bad))
    assert len(result) == 1 and result.iloc[0].floor_evidence == "unknown"


def test_known_floor_mismatch_rejects_pair_but_matching_floor_is_evidence():
    result, meta = find(pair(floor=4))
    assert result.empty and meta["counts_by_reason"]["pairs_with_conflicting_known_floor"] == 1
    result, _ = find(pair(floor="3"))
    assert result.iloc[0].floor_evidence == "both_known_equal"


def test_negative_floor_remains_a_known_integer():
    frame = pd.DataFrame([listing(floor=-1), listing("www.otodom.pl", "456", floor="-1")])
    result, _ = find(frame)
    assert len(result) == 1 and result.iloc[0].floor_evidence == "both_known_equal"


@pytest.mark.parametrize("first,second,expected", [
    (50.0, 50.25, True), (50.25, 50.0, True), (50.0, 50.25001, False),
    (10.0, 10.1, True), (10.1, 10.0, True), (10.0, 10.10001, False),
])
def test_area_threshold_uses_smaller_area_and_inclusive_boundary(first, second, expected):
    frame = pd.DataFrame([listing(area_m2=first), listing("www.otodom.pl", "456", area_m2=second)])
    result, _ = find(frame)
    assert bool(len(result)) == expected


@pytest.mark.parametrize("first,second,expected", [
    (1000000, 1010000, True), (1010000, 1000000, True),
    (1000000, 1010000.01, False), (1000000, 990000, False),
])
def test_price_threshold_uses_smaller_price_and_inclusive_boundary(first, second, expected):
    frame = pd.DataFrame([listing(price_pln=first), listing("www.otodom.pl", "456", price_pln=second)])
    result, _ = find(frame)
    assert bool(len(result)) == expected
    if expected:
        assert result.iloc[0].price_difference_fraction == pytest.approx(.01)


def test_tiny_positive_prices_still_require_one_percent_relative_similarity():
    frame=pd.DataFrame([listing(price_pln=1e-12),listing("www.otodom.pl","456",price_pln=9e-12)])
    result,meta=find(frame)
    assert result.empty and meta["counts_by_reason"]["pairs_outside_price_tolerance"]==1
    frame.loc[1,"price_pln"]=1.01e-12
    result,_=find(frame)
    assert len(result)==1


def test_unrepresentable_integer_floor_is_reported_unknown_without_crashing():
    frame=pair().astype({"floor":object})
    frame.at[1,"floor"]=10**400
    result,meta=find(frame)
    assert len(result)==1 and meta["counts_by_reason"]["invalid_floor_treated_unknown"]==1


@pytest.mark.parametrize("bad", [True, False, np.nan, np.inf, 0, -1, 1.5, "10", None, 10001])
def test_max_pairs_validation(bad):
    with pytest.raises(ValueError):
        find(max_pairs=bad)


@pytest.mark.parametrize("bad_id", [None, pd.NA, np.nan, True, False, 0, -1, 123.0, "-1", "+123", "1e3", "12.0", "１２３", "9" * 201])
def test_invalid_stable_listing_id_is_excluded(bad_id):
    result, meta = find(pair(listing_id=bad_id))
    assert result.empty and meta["counts_by_reason"]["invalid_listing_id_rows"] == 1


def test_integer_id_and_leading_zero_text_preserve_distinct_stable_identities():
    frame = pd.DataFrame([listing(identifier=np.int64(123)), listing(identifier="00123"), listing("www.otodom.pl", "456")])
    result, meta = find(frame)
    assert len(result) == 2 and meta["unique_listings"] == 3
    assert set(result.left_listing_id) == {"123", "00123"}


def test_english_warsaw_alias_is_accepted_and_does_not_make_latest_tie_ambiguous():
    frame=pd.DataFrame([listing(city="Warsaw"),listing(city="warszawa"),listing("www.otodom.pl","456",city="WARSAW")])
    result,meta=find(frame)
    assert len(result)==1 and meta["ambiguous_identity_count"]==0


def test_invalid_present_floor_is_explicitly_reported_as_weaker_evidence():
    result,meta=find(pair(floor="3rd"))
    assert len(result)==1 and result.iloc[0].floor_evidence=="unknown"
    assert meta["counts_by_reason"]["invalid_floor_treated_unknown"]==1


def test_unsupported_source_is_counted_without_becoming_a_portal_candidate():
    frame = pd.DataFrame([listing(source="another.example"), listing("www.otodom.pl", "456")])
    result, meta = find(frame)
    assert result.empty and meta["counts_by_reason"]["unsupported_source_rows"] == 1


@pytest.mark.parametrize("bad", [None, pd.NaT, pd.NA, "2026-10-08T09:00:00", "invalid", 0, NOW.replace(tzinfo=None)])
def test_invalid_or_naive_recency_is_not_guessed(bad):
    result, meta = find(pair(observed_at=bad))
    assert result.empty and meta["counts_by_reason"]["invalid_timestamp_rows"] == 1
    assert meta["counts_by_reason"]["identities_without_valid_timestamp"] == 1


def test_latest_valid_timestamp_is_used_and_invalid_timestamp_does_not_replace_it():
    frame = pd.DataFrame([listing(observed_at=NOW-timedelta(days=1), price_pln=1), listing(),
                          listing(observed_at="invalid", price_pln=1), listing("www.otodom.pl", "456")])
    result, meta = find(frame)
    assert len(result) == 1 and result.iloc[0].left_price_pln == 1000000
    assert meta["counts_by_reason"]["older_history_rows"] == 1
    assert meta["counts_by_reason"]["invalid_timestamp_rows"] == 1


def test_latest_observation_invalid_feature_does_not_fall_back_to_older_price():
    frame = pd.DataFrame([listing(observed_at=NOW-timedelta(days=1)), listing(price_pln=None), listing("www.otodom.pl", "456")])
    result, _ = find(frame)
    assert result.empty


@pytest.mark.parametrize("change", [
    {"price_pln":1000010}, {"area_m2":50.01}, {"rooms":3}, {"floor":4},
    {"district":"Wola"}, {"city":"Kraków"}, {"rooms":None,"rooms_min":4},
])
def test_tied_latest_conflicting_match_features_exclude_ambiguous_identity(change):
    frame = pd.DataFrame([listing(), listing(**change), listing("www.otodom.pl", "456")])
    result, meta = find(frame)
    assert result.empty and meta["ambiguous_identity_count"] == 1


def test_equal_utc_in_different_timezones_is_a_tie_and_latest_tie_resolves_deterministically():
    frame = pd.DataFrame([listing(observed_at="2026-10-08T11:00:00+02:00"), listing(), listing("www.otodom.pl", "456")])
    result, meta = find(frame)
    assert len(result) == 1 and meta["ambiguous_identity_count"] == 0
    assert meta["counts_by_reason"]["repeated_latest_rows"] == 1


def test_older_ambiguous_tie_is_replaced_by_later_unambiguous_observation():
    old=NOW-timedelta(days=1)
    frame=pd.DataFrame([listing(observed_at=old),listing(observed_at=old,price_pln=1),listing(),listing("www.otodom.pl","456")])
    result,meta=find(frame)
    assert len(result)==1 and meta["ambiguous_identity_count"]==0


def test_three_similar_ads_produce_review_pairs_without_transitive_groups():
    frame=pd.DataFrame([listing(identifier="1"),listing(identifier="2"),listing("www.otodom.pl","3")])
    result,meta=find(frame)
    assert len(result)==2
    assert "group_id" not in result and "confirmed" not in set(result.status)
    assert "transitive" in meta["interpretation"]


def test_output_and_metadata_are_deterministic_under_input_shuffle_including_history_ties():
    rows=[listing(identifier=str(i),area_m2=50+i*.01) for i in range(1,8)]
    rows += [listing("www.otodom.pl",str(i),area_m2=50+i*.01) for i in range(10,18)]
    rows += [listing(identifier="1",observed_at=NOW-timedelta(days=1),price_pln=1), listing(identifier="2",url=None)]
    frame=pd.DataFrame(rows)
    expected,meta=find(frame,max_pairs=5)
    for seed in range(5):
        actual,shuffled_meta=find(frame.sample(frac=1,random_state=seed),max_pairs=5)
        assert_frame_equal(actual,expected)
        assert shuffled_meta==meta
    assert meta["truncated"] and len(expected)==5


def test_dense_blocks_stop_at_comparison_budget_without_cartesian_materialization(monkeypatch):
    monkeypatch.setattr(duplicates,"MAX_COMPARISONS",7)
    rows=[listing(identifier=str(i)) for i in range(1,101)]
    rows += [listing("www.otodom.pl",str(i),price_pln=2000000) for i in range(101,201)]
    result,meta=find(pd.DataFrame(rows))
    assert result.empty and meta["comparisons"]==meta["comparison_budget"]==7
    assert meta["truncated"] and meta["termination"]=="comparison_budget"


def test_area_sorted_windows_skip_far_blocks_without_spending_comparisons():
    rows=[listing(identifier=str(i),area_m2=20+i) for i in range(1,101)]
    rows += [listing("www.otodom.pl",str(i),area_m2=1000+i) for i in range(101,201)]
    result,meta=find(pd.DataFrame(rows))
    assert result.empty and meta["comparisons"]==0 and not meta["truncated"]


def test_empty_input_preserves_fixed_schema_and_reports_no_candidates():
    frame=pair().iloc[:0]
    result,meta=find(frame)
    assert result.empty and list(result.columns)==list(duplicates.CANDIDATE_COLUMNS)
    assert meta["unique_listings"]==meta["eligible_listings"]==meta["candidate_pairs"]==0


def test_missing_optional_urls_are_not_fabricated_and_do_not_change_evidence():
    result,meta=find(pair().drop(columns=["url"]))
    assert len(result)==1 and result.iloc[0].left_url is None and result.iloc[0].right_url is None
    assert meta["counts_by_reason"]["url_unavailable"]==2


@pytest.mark.parametrize("column", sorted(duplicates.REQUIRED_COLUMNS))
def test_missing_essential_column_is_a_clear_input_error(column):
    with pytest.raises(ValueError,match="Missing duplicate candidate columns"):
        find(pair().drop(columns=[column]))


def test_non_dataframe_and_duplicate_column_names_are_errors():
    with pytest.raises(ValueError):duplicates.find_duplicate_candidates([])
    frame=pair();frame.columns=["source"]*len(frame.columns)
    with pytest.raises(ValueError):find(frame)
