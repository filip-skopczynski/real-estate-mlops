"""Conservative URL-slug indicators of offers outside whole-flat pricing.

Indicators require review and are not legal conclusions. Only the URL path's
slug is inspected; absence of an indicator cannot establish ownership rights.
"""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import unquote, urlsplit

SHARE_WORDS = {"udzial", "udzialu", "udzialy", "udzialow", "udzialem", "udzialami", "udzialach"}
ANCILLARY = {"grunt", "gruncie", "gruntu", "drodze", "drogi", "droga", "ogrod", "ogrodzie", "ogrodu", "ogrodku", "ogrodka"}
DWELLING = {"mieszkanie", "mieszkaniu", "mieszkania", "lokal", "lokalu", "kawalerka", "kawalerce", "kawalerki"}
SALE_WORDS = {"sprzedaz", "sprzedazy", "sprzedam", "sprzedaje", "sprzedania", "odsprzedaz", "odsprzedam"}


def rights_offer_flags(url) -> set[str]:
    """Flag explicit share/participation language in a decoded listing slug."""
    if not isinstance(url, str):
        return set()
    try:
        slug = urlsplit(url).path.rsplit("/", 1)[-1]
    except ValueError:
        return set()
    for _ in range(3):
        decoded = unquote(slug, encoding="utf-8", errors="replace")
        if decoded == slug:
            break
        slug = decoded
    normalized = unicodedata.normalize("NFKD", slug.casefold().replace("ł", "l"))
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    tokens = re.findall(r"[a-z0-9]+", normalized.replace("%", " proc "))
    flags = set()
    if ("tbs" in tokens or "odstepne" in tokens
            or any(word.startswith("partycypac") for word in tokens)
            or any(tokens[index] == "cesja" and (tokens[index + 1:index + 2] == ["najmu"] or tokens[index + 1:index + 3] == ["umowy", "najmu"])
                   for index in range(len(tokens)))):
        flags.add("housing_participation_indicated")
    for index, word in enumerate(tokens):
        if word not in SHARE_WORDS:
            continue
        end = min(len(tokens), index + 10)
        for next_index in range(index + 1, end):
            if tokens[next_index] in SHARE_WORDS:
                end = next_index
                break
        tail = tokens[index + 1:end]
        target = next((part for part in tail if part in ANCILLARY or part in DWELLING), None)
        # A fraction in the plot/road/garden accompanying a complete apartment
        # does not establish that the apartment itself is a fractional offer.
        if target in ANCILLARY:
            continue
        fraction = False
        for position in range(min(3, len(tail) - 1)):
            first, second = tail[position:position + 2]
            if first.isdigit() and second.isdigit() and len(first) <= 6 and len(second) <= 6 and 0 < int(first) < int(second):
                fraction = True
            if first.isdigit() and len(first) <= 3 and second.startswith(("proc", "procent")) and 0 < int(first) < 100:
                fraction = True
        dwelling_share = any(tail[position] in {"w", "we"} and tail[position + 1] in DWELLING
                             for position in range(len(tail) - 1))
        sale_share = index > 0 and tokens[index - 1] in SALE_WORDS
        if fraction or dwelling_share or sale_share:
            flags.add("partial_ownership_indicated")
    return flags
