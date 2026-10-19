"""URL wording can indicate rights that differ from ownership of a whole flat."""
from urllib.parse import quote

import pytest

from src.listing_scope import rights_offer_flags


PARTIAL = "partial_ownership_indicated"
PARTICIPATION = "housing_participation_indicated"


def url(slug, source="otodom"):
    if source == "olx":
        return "https://www.olx.pl/d/oferta/" + slug + "-IDabc123.html"
    return "https://www.otodom.pl/pl/oferta/" + slug + "-IDabc123"


@pytest.mark.parametrize("source", ["olx", "otodom"])
@pytest.mark.parametrize("slug", [
    "udzial-1-2-w-mieszkaniu", "udzial-1-2-z-posiadaniem-w-kawalerce",
    "udzial-1-3-w-lokalu-mieszkalnym", "sprzedaz-udzialu-w-mieszkaniu",
    "sprzedam-udzial-w-mieszkaniu", "udzial-w-mieszkaniu-na-mokotowie",
    "udział-1-2-w-mieszkaniu", "SPRZEDAŻ-UDZIAŁU-W-MIESZKANIU",
    "udzial-1-2-w-mieszkaniu-z-udzialem-w-gruncie",
    "syndyk-sprzeda-udzial-we-wlasnosciowym-prawie-do-lokalu",
    "udzial-we-wlasnosciowym-prawie-do-lokalu",
    "udzial-w-spoldzielczym-wlasnosciowym-prawie-do-lokalu",
    "udzial-w-prawie-wlasnosci-mieszkania",
])
def test_share_in_apartment_wording_has_explicit_partial_ownership_flag(source, slug):
    assert rights_offer_flags(url(slug, source)) == {PARTIAL}


@pytest.mark.parametrize("slug", [
    "tbs-kawalerka-na-sprzedaz", "mieszkanie-TBS-2-pokoje",
    "sprzedam-partycypacje", "sprzedaz-partycypacji-w-tbs",
    "partycypacja-mieszkanie-warszawa", "partycypacją-w-mieszkaniu",
    "odstepne-za-mieszkanie", "odstępne-kawalerka-warszawa",
    "cesja-najmu-mieszkania", "cesja-umowy-najmu-mieszkania",
])
def test_tbs_participation_and_explicit_transfer_of_tenancy_are_flagged(slug):
    assert rights_offer_flags(url(slug)) == {PARTICIPATION}


@pytest.mark.parametrize("slug", [
    "udzial-1-2-w-mieszkaniu", "udział-1-2-w-mieszkaniu",
    "SPRZEDAŻ-UDZIAŁU-W-MIESZKANIU", "odstępne-kawalerka", "partycypacją-w-mieszkaniu",
])
def test_utf8_percent_encoded_slug_and_unicode_spelling_have_the_same_meaning(slug):
    expected = rights_offer_flags(url(slug))
    assert expected
    assert rights_offer_flags(url(quote(slug, safe=""))) == expected


@pytest.mark.parametrize("source", ["olx", "otodom"])
@pytest.mark.parametrize("slug", [
    "mieszkanie-z-udzialem-w-gruncie", "mieszkanie-z-udziałem-w-gruncie",
    "mieszkanie-z-udzialem-1-100-w-gruncie", "mieszkanie-udzial-w-drodze",
    "mieszkanie-z-udzialem-1-100-w-drodze-dojazdowej",
    "mieszkanie-z-udzialem-w-ogrodzie", "mieszkanie-z-ogrodkiem",
    "kawalerka-21-m2-warszawa", "mieszkanie-1-2-pokoje-na-sprzedaz",
    "mieszkanie-od-tbspecjalisty",
    "syndyk-sprzeda-mieszkanie-z-udzialem-w-gruncie",
    "mieszkanie-z-udzialem-w-prawie-wlasnosci-gruntu",
    "sprzeda-udzial-w-drodze-dojazdowej",
    "sprzeda-udzial-we-wlasnosci-gruntu",
])
def test_standard_flat_shared_land_road_and_garden_wording_is_not_rejected(source, slug):
    assert rights_offer_flags(url(slug, source)) == set()


def test_distinct_rights_indications_can_both_be_reported():
    assert rights_offer_flags(url("udzial-1-2-w-mieszkaniu-tbs-partycypacja")) == {PARTIAL, PARTICIPATION}


def test_query_or_fragment_alone_is_not_evidence_about_apartment_rights():
    ordinary = url("zwykle-mieszkanie-2-pokoje")
    assert rights_offer_flags(ordinary + "?search=tbs-partycypacja-udzial-1-2") == set()
    assert rights_offer_flags(ordinary + "#cesja-najmu") == set()


@pytest.mark.parametrize("value", [None, "", 0, float("nan")])
def test_absent_or_nontext_url_contains_no_positive_rights_evidence(value):
    assert rights_offer_flags(value) == set()
