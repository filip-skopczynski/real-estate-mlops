"""Sparse public Otodom catalogue discovery with a resumable page cursor.

Discovery preserves identified Warsaw apartments whose price, area or room
count is unavailable. It does not turn a promoted investment presentation into
a property, infer sales, or interpret the default ranking as publication order.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import re
import time
from urllib.parse import parse_qsl, urlsplit

from bs4 import BeautifulSoup

from . import fetch_otodom as transport
from . import otodom as parser

CATALOG_FIELDS = (
    "source", "listing_id", "url", "city", "district", "price_pln", "area_m2",
    "rooms", "rooms_min", "floor", "build_year", "latitude", "longitude",
    "distance_km", "published_at", "observed_at",
)
SCOPE_URL = transport.DEFAULT_URL + "?by=LATEST&direction=DESC"
# FIVE was confirmed against two visible "5 pokoi" cards in the public
# newest-search HTML on 2026-10-07. The ordinary 1-4 room preview stays stable.
CATALOG_ROOMS = parser.ROOMS | {"FIVE": 5}


def _positive(value):
    number = parser._number(value)
    return number if number is not None and number > 0 else None


def _record(ad, moment):
    if not isinstance(ad, dict) or ad.get("estate") != "FLAT" or ad.get("transaction") != "SELL":
        raise ValueError("not_individual_apartment_sale")
    identifier, slug = ad.get("id"), ad.get("slug")
    if type(identifier) is not int or identifier <= 0:
        raise ValueError("invalid_listing_identity")
    if not isinstance(slug, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*-ID[A-Za-z0-9]+", slug):
        raise ValueError("invalid_listing_url")
    if ad.get("href") != "[lang]/ad/" + slug:
        raise ValueError("investment_or_advertising_presentation")
    try:
        district = parser._location(ad)
    except (ValueError, TypeError):
        raise ValueError("unconfirmed_warsaw_location") from None
    price = ad.get("totalPrice")
    amount = None
    if ad.get("hidePrice", False) is False and isinstance(price, dict) and price.get("currency") == "PLN":
        amount = _positive(price.get("value"))
    code = ad.get("roomsNumber")
    floor_code = ad.get("floorNumber")
    values = {
        "source": parser.SOURCE, "listing_id": str(identifier),
        "url": "https://" + parser.SOURCE + "/pl/oferta/" + slug,
        "city": "Warszawa", "district": district, "price_pln": amount,
        "area_m2": _positive(ad.get("areaInSquareMeters")),
        "rooms": CATALOG_ROOMS.get(code) if isinstance(code, str) else None,
        "rooms_min": None,
        "floor": parser.FLOORS.get(floor_code) if isinstance(floor_code, str) else None,
        "build_year": None, "latitude": None, "longitude": None, "distance_km": None,
        # Search JSON exposes creation timestamps, but their public-publication
        # semantics are not established. In particular pushedUpAt is promotion.
        "published_at": None, "observed_at": moment,
    }
    return {field: values[field] for field in CATALOG_FIELDS}


def parse_otodom_catalog_page(html, page_url, observed_at=None, *, expected_page=1):
    """Return sparse identities and verified page metadata, including zero rows."""
    if type(expected_page) is not int or not 1 <= expected_page <= 1000:
        raise ValueError("Invalid requested Otodom catalogue page")
    parser._search_url(page_url, expected_page=expected_page)
    query = parse_qsl(urlsplit(page_url).query, keep_blank_values=True, max_num_fields=100)
    by = [value for key, value in query if key == "by"]
    direction = [value for key, value in query if key == "direction"]
    if by != ["LATEST"] or direction != ["DESC"] or any(key not in {"by", "direction", "page"} for key, _ in query):
        raise ValueError("Otodom catalogue URL does not confirm its newest scope")
    if not isinstance(html, str) or not html.strip():
        raise ValueError("Empty Otodom catalogue HTML")
    moment = transport._timestamp(observed_at)
    state = parser._state(BeautifulSoup(html, "html.parser"))
    items, metadata = parser._catalogue(state, expected_page=expected_page)
    page = state["props"]["pageProps"]
    pagination = page["data"]["searchAds"]["pagination"]
    for name, minimum in (("itemsPerPage", 1), ("totalItems", 0), ("totalPages", 0)):
        if type(pagination.get(name)) is not int or pagination[name] < minimum:
            raise ValueError("Invalid Otodom catalogue pagination")
    empty_result = not items and pagination["totalItems"] == 0 and pagination["totalPages"] in {0, 1} and expected_page == 1
    if pagination["itemsPerPage"] > 1000 or (expected_page > pagination["totalPages"] and not empty_result) or (items and not pagination["totalItems"]):
        raise ValueError("Conflicting Otodom catalogue pagination")
    metadata["pagination"] = {name: pagination[name] for name in ("currentPage", "itemsPerPage", "totalItems", "totalPages")}
    metadata["items_per_page_mismatch"] = len(items) != pagination["itemsPerPage"]
    sorting = page.get("sortingOption")
    metadata["sorting"] = {
        key: value for key, value in sorting.items() if key in {"by", "direction"}
        and isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value)
    } if isinstance(sorting, dict) else {}
    if metadata["sorting"] != {"by": "LATEST", "direction": "DESC"}:
        raise ValueError("Otodom catalogue must confirm requested newest sorting")
    rows, urls, skipped = {}, {}, {}
    duplicates = 0
    for ad in items:
        try:
            row = _record(ad, moment)
        except (ValueError, TypeError) as error:
            reason = str(error)
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        identifier = row["listing_id"]
        if identifier in rows and rows[identifier] != row:
            raise ValueError("Conflicting identity on Otodom catalogue page")
        if row["url"] in urls and urls[row["url"]] != identifier:
            raise ValueError("Conflicting URL on Otodom catalogue page")
        duplicates += identifier in rows
        rows[identifier], urls[row["url"]] = row, identifier
    metadata.update(
        parsed_listings=len(rows), skipped_items=sum(skipped.values()),
        skipped_by_reason=skipped, duplicates_on_page=duplicates,
        sparse_price_count=sum(row["price_pln"] is None for row in rows.values()),
        sparse_area_count=sum(row["area_m2"] is None for row in rows.values()),
        sparse_rooms_count=sum(row["rooms"] is None for row in rows.values()),
    )
    return list(rows.values()), metadata


def _limits(mode, max_pages, max_listings, max_requests, delay, max_duration_seconds):
    if mode not in {"bootstrap", "daily", "refresh"}:
        raise transport.OtodomError("Unknown catalogue mode")
    transport._search_limits(max_pages, max_listings, delay)
    if type(max_requests) is not int or not 2 <= max_requests <= 10000:
        raise transport.OtodomError("Request budget must be between 2 and 10000")
    if isinstance(max_duration_seconds, bool) or not isinstance(max_duration_seconds, (int, float)) or not math.isfinite(max_duration_seconds) or max_duration_seconds <= 0:
        raise transport.OtodomError("Duration budget must be positive and finite")


def _checkpoint(mode, value):
    if value is None:
        return 1
    if not isinstance(value, dict) or set(value) != {"version", "source", "mode", "scope_url", "next_page", "finished"}:
        raise transport.OtodomError("Invalid Otodom catalogue checkpoint")
    if value["version"] != 1 or type(value["version"]) is not int or value["source"] != parser.SOURCE or value["mode"] != mode or value["scope_url"] != SCOPE_URL:
        raise transport.OtodomError("Otodom checkpoint belongs to a different scope or mode")
    if type(value["finished"]) is not bool or type(value["next_page"]) is not int or not 1 <= value["next_page"] <= 1001:
        raise transport.OtodomError("Invalid Otodom checkpoint cursor")
    if value["finished"]:
        raise transport.OtodomError("Finished Otodom traversal must be started with a fresh checkpoint")
    if value["next_page"] > 1000:
        raise transport.OtodomError("Otodom checkpoint exceeds supported page limit")
    return value["next_page"]


class _BudgetReached(Exception):
    pass


class _BudgetSession:
    """Count every actual GET, including retries and redirects, before dispatch."""
    def __init__(self, session, maximum, deadline, clock):
        self.session, self.maximum, self.deadline, self.clock = session, maximum, deadline, clock
        self.requests = 0
        self.budget_hit = False

    def get(self, *args, **kwargs):
        if self.requests >= self.maximum or self.clock() >= self.deadline:
            self.budget_hit = True
            raise _BudgetReached()
        self.requests += 1
        return self.session.get(*args, **kwargs)


def _fetch_resource(session, url, *, maximum, delay, sleep, counts, kind, policy=None, expected_page=1):
    """Transport-preserving budget adapter: the helper sanitizes exceptions."""
    try:
        return transport._fetch_resource(session, url, maximum=maximum, delay=delay,
                                         sleep=sleep, counts=counts, kind=kind,
                                         policy=policy, expected_page=expected_page)
    except transport.OtodomError:
        if session.budget_hit:
            # The shared transport increments before dispatch; this attempt
            # never reached the network because the wrapper rejected it.
            counts[kind] -= 1
            raise _BudgetReached() from None
        raise


def collect_otodom_catalog(*, mode="bootstrap", max_pages=1000, max_listings=50000,
                           max_requests=700, delay=2.0, observed_at=None, session=None,
                           html_pages=None, known_ids=None, checkpoint=None,
                           sleep=time.sleep, max_duration_seconds=2700, clock=time.monotonic):
    """Collect contiguous verified pages; checkpoints resume at the next page.

    Offline HTML pages are the consecutive pages starting at the supplied
    checkpoint cursor. Budget pauses return status ok and completed false;
    access/format failures return partial and leave that failed page unadvanced.
    Daily mode reports known IDs and conservatively reads its complete bounded
    newest window; it never stops at the first known ID or promotional listing.
    """
    _limits(mode, max_pages, max_listings, max_requests, delay, max_duration_seconds)
    start_page = _checkpoint(mode, checkpoint)
    if html_pages is not None and (not isinstance(html_pages, list) or not html_pages or any(not isinstance(page, (str, bytes)) for page in html_pages)):
        raise transport.OtodomError("Offline catalogue requires a nonempty list of HTML pages")
    if known_ids is not None and (not isinstance(known_ids, (set, list, tuple, frozenset)) or any(not isinstance(identifier, str) for identifier in known_ids)):
        raise transport.OtodomError("Known catalogue identities must be strings")
    known = set(known_ids or ())
    moment = transport._timestamp(observed_at)
    counts = {"robots": 0, "html": 0}
    rows, urls, metadata, fingerprints = {}, {}, [], set()
    status, termination, error, completed = "ok", "max_pages", None, False
    next_page, effective_delay, own_session, policy = start_page, delay, False, None
    robots_audit = {"status": "not_performed_offline"}
    parsed_count = duplicate_count = changed_duplicates = 0
    deadline = clock() + max_duration_seconds
    budget_session = None
    try:
        if html_pages is None:
            if session is None:
                from curl_cffi import requests
                session = requests.Session(trust_env=False, default_headers=False, discard_cookies=True, retry=0)
                own_session = True
            budget_session = _BudgetSession(session, max_requests, deadline, clock)
            content, robots_url = _fetch_resource(
                budget_session, transport.ROBOTS_URL, maximum=transport.MAX_ROBOTS_BYTES,
                delay=delay, sleep=sleep, counts=counts, kind="robots",
            )
            policy = transport._robots_policy(content)
            crawl_delay, rate = policy.crawl_delay(transport.USER_AGENT), policy.request_rate(transport.USER_AGENT)
            effective_delay = max(delay, crawl_delay or 0, rate.seconds / rate.requests if rate else 0)
            robots_audit = {
                "status": "allowed", "url": robots_url, "sha256": hashlib.sha256(content).hexdigest(),
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "user_agent": transport.USER_AGENT, "content_signals": transport._content_signals(content),
            }
        for index in range(max_pages):
            number = start_page + index
            if number > 1000:
                termination = "supported_page_limit"
                break
            if clock() >= deadline:
                termination = "duration_budget"
                break
            if html_pages is not None and index >= len(html_pages):
                status, termination = "partial", "offline_pages_exhausted"
                break
            page_url = transport._page_url(SCOPE_URL, number)
            try:
                if html_pages is None:
                    content, final_url = _fetch_resource(
                        budget_session, page_url, maximum=transport.MAX_HTML_BYTES,
                        delay=effective_delay, sleep=sleep, counts=counts, kind="html",
                        policy=policy, expected_page=number,
                    )
                else:
                    content, final_url = transport._offline_content(html_pages[index]), page_url
                page_rows, details = parse_otodom_catalog_page(transport._decode(content), final_url,
                                                              moment, expected_page=number)
                # Validate a whole page before committing it or its cursor.
                for row in page_rows:
                    identifier, url = row["listing_id"], row["url"]
                    if url in urls and urls[url] != identifier:
                        raise transport.OtodomError("Conflicting catalogue URL across pages")
                fingerprint = tuple(sorted((row["listing_id"], tuple(row[field] for field in CATALOG_FIELDS))
                                           for row in page_rows))
                if fingerprint and fingerprint in fingerprints:
                    raise transport.OtodomError("Repeated catalogue page")
                new_count = sum(row["listing_id"] not in rows for row in page_rows)
                if len(rows) + new_count > max_listings:
                    termination = "listing_budget"
                    break
            except _BudgetReached:
                termination = "request_budget" if budget_session.requests >= max_requests else "duration_budget"
                break
            except (transport.OtodomError, ValueError, TypeError, KeyError):
                status, termination = "partial", "page_error"
                error = {"page": number, "reason": "page_access_or_validation_failed"}
                break
            details.update(requested_url=page_url, final_url=final_url,
                           html_bytes=len(content), html_sha256=hashlib.sha256(content).hexdigest())
            details["pagination_changed"] = bool(metadata) and any(
                details["pagination"][key] != metadata[0]["pagination"][key]
                for key in ("totalItems", "totalPages", "itemsPerPage")
            )
            metadata.append(details)
            fingerprints.add(fingerprint)
            parsed_count += len(page_rows)
            for row in page_rows:
                identifier = row["listing_id"]
                duplicate_count += identifier in rows
                if identifier in rows and rows[identifier] != row:
                    changed_duplicates += 1
                    urls.pop(rows[identifier]["url"], None)
                rows[identifier], urls[row["url"]] = row, identifier
            next_page = number + 1
            if number >= details["pagination"]["totalPages"]:
                completed, termination = True, "pagination_end"
                break
            if len(rows) >= max_listings:
                termination = "listing_budget"
                break
    except _BudgetReached:
        termination = "request_budget" if budget_session.requests >= max_requests else "duration_budget"
    except transport.OtodomError:
        status, termination = "partial", "robots_error"
        error = {"page": start_page, "reason": "robots_access_or_validation_failed"}
    except Exception:
        status, termination = "partial", "setup_error"
        error = {"page": start_page, "reason": "catalogue_setup_failed"}
    finally:
        if own_session:
            try:
                session.close()
            except Exception:
                pass
    audit = {
        "source": parser.SOURCE, "mode": mode, "status": status, "completed": completed,
        "collection_mode": "offline_catalog" if html_pages is not None else "live_catalog",
        "observed_at": moment.isoformat(), "requested_url": SCOPE_URL,
        "start_page": start_page, "pages_read": len(metadata), "page_metadata": metadata,
        "parsed_listings": parsed_count, "exported_listings": len(rows),
        "duplicate_encounters": duplicate_count, "termination": termination,
        "changed_duplicates": changed_duplicates,
        "duplicate_resolution": "last validated encounter by source listing ID; same URL with different source IDs remains fatal",
        "skipped_items": sum(details["skipped_items"] for details in metadata),
        "sparse_price_count": sum(row["price_pln"] is None for row in rows.values()),
        "sparse_area_count": sum(row["area_m2"] is None for row in rows.values()),
        "sparse_rooms_count": sum(row["rooms"] is None for row in rows.values()),
        "max_pages": max_pages, "max_listings": max_listings, "max_requests": max_requests,
        "max_duration_seconds": max_duration_seconds,
        "request_counts": counts, "requests_dispatched": budget_session.requests if budget_session else 0,
        "request_delay_seconds": effective_delay, "robots_check": robots_audit,
        "checkpoint": {"version": 1, "source": parser.SOURCE, "mode": mode,
                       "scope_url": SCOPE_URL, "next_page": next_page, "finished": completed},
        "known_listing_encounters": sum(identifier in known for identifier in rows),
        "new_to_known_ids": sum(identifier not in known for identifier in rows),
        "detail_pages_fetched": 0, "training_performed": False, "availability_inference": False,
        "sorting_verified_newest": bool(metadata),
        "sorting_interpretation": "requested and source-confirmed LATEST/DESC; initial visible newest ordering verified 2026-10-07; publication chronology not guaranteed",
        "daily_discovery_guarantee": False,
        "published_at_interpretation": "unavailable; provider creation fields not verified as publication",
        "completeness": "advertised accessible result traversal; changing pages and excluded presentations prevent full inventory guarantee",
        "scope": "Warsaw apartment sale identities, including sparse price, area and room features",
    }
    if error is not None:
        audit["error"] = error
    # Confirm that audit is safe for persistence before returning it.
    json.dumps(audit, allow_nan=False)
    return list(rows.values()), audit
