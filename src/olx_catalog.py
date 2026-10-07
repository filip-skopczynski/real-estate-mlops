"""Resumable discovery through public OLX Warsaw sale search pages.

Price partitions use verified public search parameters. Inclusive boundaries
overlap deliberately: a fractional PLN price must never fall into an integer
gap. IDs are deduplicated after every validated page. This is a changing search
index, so completed traversal does not establish complete market coverage.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import time
from urllib.parse import parse_qsl, urlencode, urlsplit

from .fetch_olx import (
    DEFAULT_URL, ROBOTS_URL, MAX_HTML_BYTES, MAX_ROBOTS_BYTES, USER_AGENT,
    OLXError, OLXRequestBudget, _decode, _fetch_resource, _limits,
    _offline_content, _robots_policy, _timestamp,
)
from .olx import parse_olx_catalog_page

MAX_BOUND = 10**12


def _task(lower=None, upper=None, page=1):
    return {"lower": lower, "upper": upper, "page": page}


def catalog_search_url(task):
    """Build only verified public filter and sorting parameters."""
    query = {"search[order]": "created_at:desc"}
    if task["lower"] is not None:
        query["search[filter_float_price:from]"] = str(task["lower"])
    if task["upper"] is not None:
        query["search[filter_float_price:to]"] = str(task["upper"])
    if task["page"] != 1:
        query["page"] = str(task["page"])
    return DEFAULT_URL + "?" + urlencode(query)


def _checkpoint(value, mode):
    if value is None:
        return {"version": 1, "source": "www.olx.pl", "mode": mode,
                "queue": [_task()], "unresolved_partitions": []}
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1 or value.get("source") != "www.olx.pl" or value.get("mode") != mode:
        raise OLXError("Punkt wznowienia nie pasuje do katalogu i trybu OLX.")
    if set(value) != {"version", "source", "mode", "queue", "unresolved_partitions"}:
        raise OLXError("Nieznany format punktu wznowienia OLX.")
    queue = value.get("queue")
    if not isinstance(queue, list) or len(queue) > 10000:
        raise OLXError("Nieprawidłowa kolejka punktu wznowienia OLX.")
    for item in queue:
        if not isinstance(item, dict) or set(item) != {"lower", "upper", "page"}:
            raise OLXError("Nieprawidłowa partycja punktu wznowienia OLX.")
        if type(item["page"]) is not int or not 1 <= item["page"] <= 1000:
            raise OLXError("Nieprawidłowa strona punktu wznowienia OLX.")
        for key in ("lower", "upper"):
            if item[key] is not None and (type(item[key]) is not int or not 0 <= item[key] <= MAX_BOUND):
                raise OLXError("Nieprawidłowa cena graniczna punktu wznowienia OLX.")
        if item["lower"] is not None and item["upper"] is not None and item["lower"] >= item["upper"]:
            raise OLXError("Nieprawidłowy zakres ceny punktu wznowienia OLX.")
    unresolved = value.get("unresolved_partitions")
    if not isinstance(unresolved, list) or len(unresolved) > 10000:
        raise OLXError("Nieprawidłowe ograniczenia punktu wznowienia OLX.")
    # Persist only the validated data shape; never follow a checkpoint URL.
    for item in unresolved:
        if not isinstance(item, dict) or set(item) != {"lower", "upper", "visible_elements", "total_elements"}:
            raise OLXError("Nieprawidłowe ograniczenie partycji OLX.")
        for key in ("lower", "upper"):
            if item[key] is not None and (type(item[key]) is not int or not 0 <= item[key] <= MAX_BOUND):
                raise OLXError("Nieprawidłowa granica ograniczenia partycji OLX.")
        for key in ("visible_elements", "total_elements"):
            if type(item[key]) is not int or item[key] < 0:
                raise OLXError("Nieprawidłowa liczba ograniczenia partycji OLX.")
    return deepcopy(value)


def _split(item):
    lower, upper = item["lower"], item["upper"]
    if upper is None:
        midpoint = 1000000 if lower is None else max(lower * 2, lower + 1000000)
        if midpoint >= MAX_BOUND:
            return None
    else:
        midpoint = ((lower or 0) + upper) // 2
        if midpoint <= (lower or 0) or midpoint >= upper:
            return None
    return [_task(lower, midpoint), _task(midpoint, upper)]


def collect_olx_catalog(*, mode="bootstrap", max_pages=1000, max_listings=50000,
                        max_requests=700, delay=2.0, observed_at=None, session=None,
                        html_pages=None, known_ids=None, checkpoint=None,
                        sleep=time.sleep):
    """Return sparse rows and a JSON-safe report/checkpoint without storage.

    Bootstrap/refresh split capped searches, resuming the next unprocessed page
    on another invocation. Daily starts at the newest head, reads the full page
    budget, and never stops at the first known/promoted ID. ``html_pages`` may
    be a request-order list or a map from generated public URLs to saved HTML.
    Failed or partially exported pages do not advance the durable checkpoint.
    """
    if mode not in {"bootstrap", "daily", "refresh"}:
        raise OLXError("Tryb katalogu OLX musi być bootstrap, daily lub refresh.")
    for value, name, maximum in ((max_pages, "stron", 1000), (max_listings, "ofert", 50000), (max_requests, "żądań", 5000)):
        if type(value) is not int or not 1 <= value <= maximum:
            raise OLXError(f"Nieprawidłowy limit {name} katalogu OLX.")
    _limits(1, delay)
    if html_pages is not None and not isinstance(html_pages, (list, dict)):
        raise OLXError("Lokalne strony katalogu OLX muszą być listą lub mapą URL.")
    if known_ids is not None and (not isinstance(known_ids, (set, list, tuple, frozenset)) or any(not isinstance(v, str) or not v for v in known_ids)):
        raise OLXError("Znane ID katalogu OLX muszą być tekstowymi identyfikatorami.")
    known = set(known_ids or ())
    cursor = _checkpoint(checkpoint, mode)
    # The daily discovery window intentionally restarts every day. Longer-term
    # catch-up belongs to the independent bootstrap/refresh checkpoint.
    if mode == "daily":
        cursor = _checkpoint(None, mode)
    counts = {"robots": 0, "html": 0}
    timestamp = _timestamp(observed_at)
    report = {
        "source": "www.olx.pl", "mode": mode, "status": "ok", "completed": False,
        "observed_at": timestamp.isoformat(), "pages_read": 0, "parsed_listings": 0,
        "exported_listings": 0, "request_counts": counts, "page_metadata": [],
        "max_pages": max_pages, "max_requests": max_requests, "max_listings": max_listings,
        "termination": "page_budget", "cross_page_duplicates": 0, "cross_page_conflicts": 0,
        "price_partitions_created": 0, "ordering": "verified source newest option; promotions can overlap",
        "coverage_complete": False, "availability_inference": False, "training_performed": False,
        "unpriced_coverage": "unfiltered pages only; price partitions cannot establish coverage of unpriced ads",
        "price_interpretation": "advertised asking price; not transaction price",
        "new_identity_interpretation": "first encountered source ID; not necessarily newly published or a new property",
    }
    own_session, policy, effective_delay = False, None, delay
    rows_by_id, urls, page_sets = {}, {}, {}
    try:
        if cursor["queue"] and html_pages is None:
            if session is None:
                from curl_cffi import requests

                session = requests.Session(trust_env=False, default_headers=False, discard_cookies=True, retry=0)
                own_session = True
            content, robot_url = _fetch_resource(
                session, ROBOTS_URL, maximum=MAX_ROBOTS_BYTES, delay=delay, sleep=sleep,
                counts=counts, kind="robots", max_requests=max_requests,
            )
            policy = _robots_policy(content)
            rate = policy.request_rate(USER_AGENT)
            effective_delay = max(delay, policy.crawl_delay(USER_AGENT) or 0, rate.seconds / rate.requests if rate else 0)
            report["robots_check"] = {
                "status": "allowed", "url": robot_url, "user_agent": USER_AGENT,
                "sha256": hashlib.sha256(content).hexdigest(),
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
        else:
            report["robots_check"] = {"status": "not_performed_offline" if html_pages is not None else "no_pending_pages"}
        report["request_delay_seconds"] = effective_delay
        while cursor["queue"] and report["pages_read"] < max_pages:
            item = cursor["queue"][0]
            page_url = catalog_search_url(item)
            page = item["page"]
            if html_pages is not None:
                try:
                    html = html_pages[page_url] if isinstance(html_pages, dict) else html_pages[report["pages_read"]]
                except (KeyError, IndexError):
                    report.update(status="partial", termination="offline_input_exhausted", failed_url=page_url)
                    break
                content, final_url = _offline_content(html), page_url
            else:
                content, final_url = _fetch_resource(
                    session, page_url, maximum=MAX_HTML_BYTES, delay=effective_delay, sleep=sleep,
                    counts=counts, kind="html", policy=policy, expected_page=page,
                    max_requests=max_requests,
                )
            # Redirects must preserve the requested filter/sort query. The
            # parser validates applied state against the original public URL.
            if dict(parse_qsl(urlsplit(final_url).query)) != dict(parse_qsl(urlsplit(page_url).query)):
                raise OLXError("Przekierowanie OLX zmieniło filtry katalogu; punkt wznowienia zachowano.")
            try:
                rows, metadata = parse_olx_catalog_page(_decode(content), page_url, timestamp, expected_page=page)
            except (ValueError, TypeError, KeyError, ImportError):
                raise OLXError("OLX nie potwierdza strony, filtrów lub struktury katalogu; punkt wznowienia zachowano.") from None
            report["pages_read"] += 1
            metadata.update(url=page_url, lower=item["lower"], upper=item["upper"], html_sha256=hashlib.sha256(content).hexdigest())
            report["page_metadata"].append(metadata)
            report["parsed_listings"] += len(rows)
            for row in rows:
                if row["url"] in urls and urls[row["url"]] != row["listing_id"]:
                    raise OLXError("Sprzeczne identyfikatory adresu katalogu OLX; punkt wznowienia zachowano.")
            fingerprint = tuple(sorted(row["listing_id"] for row in rows))
            partition = (item["lower"], item["upper"])
            seen = page_sets.setdefault(partition, set())
            if rows and fingerprint in seen:
                report.update(status="partial", termination="repeated_page", failed_url=page_url)
                break
            seen.add(fingerprint)
            overflow = False
            for row in rows:
                identifier = row["listing_id"]
                previous = rows_by_id.get(identifier)
                if previous:
                    report["cross_page_duplicates"] += 1
                    report["cross_page_conflicts"] += int(previous != row)
                    if previous["url"] != row["url"]:
                        urls.pop(previous["url"], None)
                elif len(rows_by_id) >= max_listings:
                    overflow = True
                    continue
                rows_by_id[identifier], urls[row["url"]] = row, identifier
            if overflow:
                report["termination"] = "listing_budget"
                break
            if mode != "daily" and page == 1 and metadata["reported_result_cap"]:
                children = _split(item)
                if children:
                    cursor["queue"][:1] = children
                    report["price_partitions_created"] += len(children)
                    continue
                unresolved = {key: item[key] for key in ("lower", "upper")}
                unresolved.update(visible_elements=metadata["visible_elements"], total_elements=metadata["total_elements"])
                if unresolved not in cursor["unresolved_partitions"]:
                    cursor["unresolved_partitions"].append(unresolved)
            if metadata["verified_zero_results"] or page >= metadata["total_pages"]:
                cursor["queue"].pop(0)
            else:
                item["page"] += 1
            if len(rows_by_id) >= max_listings and cursor["queue"]:
                report["termination"] = "listing_budget"
                break
        if not cursor["queue"]:
            report.update(completed=True, termination="reported_search_end")
    except OLXRequestBudget:
        report["termination"] = "request_budget"
    except OLXError as exc:
        report.update(status="partial", termination="source_error", error=str(exc))
    except Exception:
        report.update(status="partial", termination="source_error", error="Nie udało się odczytać katalogu OLX; punkt wznowienia zachowano.")
    finally:
        if own_session:
            try:
                session.close()
            except Exception:
                pass
    rows = list(rows_by_id.values())
    report.update(
        exported_listings=len(rows), checkpoint=cursor,
        changed_duplicates=report["cross_page_conflicts"],
        known_identities=sum(row["listing_id"] in known for row in rows),
        new_identities=sum(row["listing_id"] not in known for row in rows),
        skipped_items=sum(m["skipped_items"] for m in report["page_metadata"]),
        reported_result_cap=any(m["reported_result_cap"] for m in report["page_metadata"]),
        unresolved_partitions=deepcopy(cursor["unresolved_partitions"]),
        completeness="bounded changing search traversal; absent/unpriced/unrecognized ads and identical-price caps remain possible",
    )
    return rows, report
