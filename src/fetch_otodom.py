"""Read one Otodom search page into a local, price-only sampled preview.

No database, training, LLM, detail requests or access-control workarounds are
used. The separate collect_otodom_search API opts into bounded pagination.
Live reads require a fresh, readable robots.txt.
The reusable REP and bounded-content helpers are already tested by OLX.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

from src.fetch_data import FIELDS, _write_csv
from src import fetch_olx as _shared

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SEARCH_PATH = "/pl/wyniki/sprzedaz/mieszkanie/mazowieckie/warszawa/warszawa/warszawa"
LEGACY_SEARCH_PATH = "/pl/oferty/sprzedaz/mieszkanie/warszawa"
DEFAULT_URL = "https://www.otodom.pl" + SEARCH_PATH
ROBOTS_URL = "https://www.otodom.pl/robots.txt"
USER_AGENT = "WarsawRealEstatePortfolio/0.1"
REQUEST_TIMEOUT = 20
MAX_ATTEMPTS = 3
MAX_REDIRECTS = 3
MAX_HTML_BYTES = 8 * 1024 * 1024
MAX_ROBOTS_BYTES = 256 * 1024
MAX_LISTINGS = 100
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class OtodomError(ValueError):
    """The preview cannot be collected or validated safely."""


def _source_neutral(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except _shared.OLXError as error:
        raise OtodomError(str(error)) from None


def _timestamp(value=None):
    return _source_neutral(_shared._timestamp, value)


def _decode(content):
    return _source_neutral(_shared._decode, content)


def _offline_content(html):
    return _source_neutral(_shared._offline_content, html)


def _robots_policy(content):
    return _source_neutral(_shared._robots_policy, content)


def _read_stream(response, maximum):
    return _source_neutral(_shared._read_stream, response, maximum)


def _validate_origin(url):
    if not isinstance(url, str) or not url or re.search(r"[\x00-\x20\x7f\\]", url):
        raise OtodomError("Adres źródła jest nieprawidłowy.")
    try:
        parts = urlsplit(url)
        valid = (
            parts.scheme == "https" and parts.hostname == "www.otodom.pl"
            and parts.username is None and parts.password is None
            and parts.port is None and not parts.fragment
            and parts.netloc.casefold() == "www.otodom.pl"
        )
    except ValueError:
        raise OtodomError("Adres źródła jest nieprawidłowy.") from None
    if not valid or "#" in url:
        raise OtodomError("Dozwolony jest wyłącznie adres HTTPS www.otodom.pl bez loginu, portu i fragmentu.")
    return parts


def validate_search_url(url):
    """Allow only equivalent Warsaw apartment-sale searches and first-page filters."""
    return _validate_search_page_url(url, expected_page=1)


def _validate_search_page_url(url, *, expected_page):
    """Validate one exact page without relaxing the ordinary preview API."""
    parts = _validate_origin(url)
    if parts.path not in {SEARCH_PATH, SEARCH_PATH + "/", LEGACY_SEARCH_PATH, LEGACY_SEARCH_PATH + "/"}:
        raise OtodomError("Wybierz stronę sprzedaży mieszkań Otodom w Warszawie.")
    try:
        query = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=100)
    except ValueError:
        raise OtodomError("Parametry adresu źródła są nieprawidłowe.") from None
    page_values = []
    for key, value in query:
        decoded = key + " " + value
        for _ in range(3):
            decoded = unquote(decoded)
        if re.search(r"wynajem|wynaj[eę]|rental|\brent\b|lease|[\x00-\x1f\x7f\\]", decoded, re.IGNORECASE):
            raise OtodomError("Adres podglądu nie może wybierać najmu.")
        if key.casefold() == "page":
            page_values.append(value)
    if len(page_values) > 1:
        raise OtodomError("Adres zawiera powtórzony parametr strony Otodom.")
    if page_values and page_values[0] != str(expected_page):
        raise OtodomError("Adres wskazuje inną stronę niż żądana.")
    if expected_page != 1 and not page_values:
        raise OtodomError("Adres nie potwierdza numeru żądanej strony Otodom.")
    return urlunsplit(("https", "www.otodom.pl", parts.path, parts.query, ""))


def _limits(max_listings, delay):
    if isinstance(max_listings, bool) or not isinstance(max_listings, int) or not 1 <= max_listings <= MAX_LISTINGS:
        raise OtodomError("Limit ofert musi być liczbą całkowitą od 1 do 100.")
    if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not math.isfinite(delay) or delay < 2:
        raise OtodomError("Opóźnienie musi być skończone i wynosić co najmniej 2 sekundy.")


def _fetch_resource(session, url, *, maximum, delay, sleep, counts, kind, policy=None, expected_page=1):
    """Bound retries and streaming; check REP before each redirected HTML URL."""
    from curl_cffi.requests.exceptions import Timeout as CurlTimeout

    current = url
    for redirect in range(MAX_REDIRECTS + 1):
        if kind == "html":
            current = _validate_search_page_url(current, expected_page=expected_page)
            if policy is None or not policy.can_fetch(USER_AGENT, current):
                raise OtodomError("robots.txt nie zezwala temu kolektorowi na wybrany adres; pobieranie zatrzymano.")
        else:
            _validate_origin(current)
        for attempt in range(MAX_ATTEMPTS):
            response = None
            sleep(delay * 2**attempt)
            counts[kind] += 1
            try:
                response = session.get(
                    current, impersonate="chrome120", default_headers=False,
                    headers={"User-Agent": USER_AGENT, "Accept": "text/plain" if kind == "robots" else "text/html", "Cookie": ""},
                    timeout=REQUEST_TIMEOUT, allow_redirects=False, stream=True,
                    discard_cookies=True, proxies={"all": ""},
                )
                status = response.status_code
                if status in {403, 429}:
                    raise OtodomError(f"Otodom zwrócił HTTP {status}; pobieranie zatrzymano.")
                if 500 <= status < 600:
                    if attempt == MAX_ATTEMPTS - 1:
                        raise OtodomError("Źródło wielokrotnie zwróciło błąd serwera; pobieranie zatrzymano.")
                    continue
                if status in REDIRECT_STATUSES:
                    location = _shared._header(response, "Location")
                    if not location or redirect == MAX_REDIRECTS:
                        raise OtodomError("Źródło zwróciło nieprawidłowe lub zbyt liczne przekierowania.")
                    target = urljoin(current, location)
                    _validate_origin(target)
                    if kind == "robots":
                        raise OtodomError("robots.txt musi być dostępny bez przekierowania z HTTP 200.")
                    current = _validate_search_page_url(target, expected_page=expected_page)
                    break
                if status != 200:
                    raise OtodomError(f"Źródło zwróciło HTTP {status}; pobieranie zatrzymano.")
                content_type = (_shared._header(response, "Content-Type") or "").split(";")[0].strip().casefold()
                expected = {"text/plain"} if kind == "robots" else {"text/html", "application/xhtml+xml"}
                if content_type and content_type not in expected:
                    raise OtodomError("Źródło zwróciło nieoczekiwany typ odpowiedzi.")
                return _read_stream(response, maximum), current
            except (CurlTimeout, TimeoutError):
                if attempt == MAX_ATTEMPTS - 1:
                    raise OtodomError("Źródło nie odpowiedziało w limicie czasu po 3 próbach.") from None
            except OtodomError:
                raise
            except Exception:
                raise OtodomError("Nie udało się odczytać źródła; pobieranie zatrzymano.") from None
            finally:
                if response is not None:
                    _shared._close_response(response)
        else:
            raise OtodomError("Nie udało się odczytać źródła w dozwolonej liczbie prób.")
    raise OtodomError("Źródło przekroczyło limit przekierowań.")


def _content_signals(content):
    """Preserve optional source-use signals in the audit without inventing consent."""
    signals = {}
    for line in _decode(content).splitlines():
        directive = line.split("#", 1)[0].strip()
        name, separator, value = directive.partition(":")
        if separator and name.strip().casefold() == "content-signal":
            for item in value.split(","):
                key, assignment, setting = item.strip().partition("=")
                if assignment and key in {"search", "ai-input", "ai-train"} and setting in {"yes", "no"}:
                    # Record conflicts conservatively rather than silently replacing a No.
                    signals[key] = "no" if "no" in {setting, signals.get(key)} else "yes"
    return signals


def _parse_html(html, page_url, observed_at):
    try:
        from src.otodom import parse_otodom_search

        return parse_otodom_search(html, page_url, observed_at=observed_at)
    except ValueError:
        raise OtodomError("HTML Otodom zawiera blokadę dostępu lub nieoczekiwany format; nic nie zapisano.") from None
    except (ImportError, TypeError, KeyError):
        raise OtodomError("Nie udało się odczytać formatu Otodom; nic nie zapisano.") from None


def _validate_records(records):
    if not isinstance(records, list) or not records:
        raise OtodomError("Brak poprawnych ofert z ceną; nic nie zapisano.")
    identities = set()
    for row in records:
        if not isinstance(row, dict) or set(row) != set(FIELDS):
            raise OtodomError("Parser zwrócił nieprawidłowy format oferty; nic nie zapisano.")
        for field, label in (("price_pln", "cenę"), ("area_m2", "metraż")):
            value = row[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise OtodomError(f"Parser zwrócił nieprawidłowy {label}; nic nie zapisano.")
        if type(row["rooms"]) is not int or row["rooms"] not in {1, 2, 3, 4}:
            raise OtodomError("Parser zwrócił nieprawidłową dokładną liczbę pokoi; nic nie zapisano.")
        if row["source"] != "www.otodom.pl" or not isinstance(row["listing_id"], str) or not row["listing_id"].strip() or row["city"] != "Warszawa":
            raise OtodomError("Parser zwrócił nieprawidłową tożsamość oferty; nic nie zapisano.")
        parts = _validate_origin(row["url"])
        if not re.fullmatch(r"/pl/oferta/[^/]+", parts.path):
            raise OtodomError("Parser zwrócił nieprawidłowy adres oferty Otodom; nic nie zapisano.")
        identity = (row["source"], row["listing_id"])
        if identity in identities:
            raise OtodomError("Parser zwrócił powtórzone tożsamości ofert; nic nie zapisano.")
        identities.add(identity)
        row["observed_at"] = _timestamp(row["observed_at"])


def collect_otodom(*, url=DEFAULT_URL, html=None, session=None, delay=2.0, sleep=time.sleep, observed_at=None, max_listings=20):
    """Return (records, audit); an offline HTML input makes no network requests."""
    requested_url = validate_search_url(url)
    _limits(max_listings, delay)
    timestamp = _timestamp(observed_at) if observed_at is not None else None
    counts = {"robots": 0, "html": 0}
    own_session = False
    effective_delay = delay
    if html is not None:
        content = _offline_content(html)
        final_url = requested_url
        robots_audit = {"status": "not_performed_offline"}
    else:
        try:
            if session is None:
                from curl_cffi import requests

                session = requests.Session(trust_env=False, default_headers=False, discard_cookies=True, retry=0)
                own_session = True
            robots_content, robots_url = _fetch_resource(
                session, ROBOTS_URL, maximum=MAX_ROBOTS_BYTES, delay=delay, sleep=sleep,
                counts=counts, kind="robots",
            )
            policy = _robots_policy(robots_content)
            crawl_delay = policy.crawl_delay(USER_AGENT)
            rate = policy.request_rate(USER_AGENT)
            effective_delay = max(delay, crawl_delay or 0, rate.seconds / rate.requests if rate else 0)
            checked_at = datetime.now(timezone.utc)
            content, final_url = _fetch_resource(
                session, requested_url, maximum=MAX_HTML_BYTES, delay=effective_delay, sleep=sleep,
                counts=counts, kind="html", policy=policy,
            )
            robots_audit = {
                "status": "allowed", "url": robots_url, "checked_at": checked_at.isoformat(),
                "user_agent": USER_AGENT, "sha256": hashlib.sha256(robots_content).hexdigest(),
                "content_signals": _content_signals(robots_content),
            }
        except OtodomError:
            raise
        except Exception:
            raise OtodomError("Nie udało się przygotować odczytu Otodom; pobieranie zatrzymano.") from None
        finally:
            if own_session:
                try:
                    session.close()
                except Exception:
                    pass
    timestamp = timestamp or _timestamp()
    records = _parse_html(_decode(content), final_url, timestamp)
    _validate_records(records)
    parsed_count = len(records)
    records = records[:max_listings]
    report = {
        "source": "www.otodom.pl", "collection_mode": "offline_preview" if html is not None else "live_preview",
        "requested_url": requested_url, "final_url": final_url, "observed_at": timestamp.isoformat(),
        "parsed_listings": parsed_count, "exported_listings": len(records), "max_listings": max_listings,
        "page_limit": 1, "detail_pages_fetched": 0,
        "html_bytes": len(content), "html_sha256": hashlib.sha256(content).hexdigest(),
        "max_html_bytes": MAX_HTML_BYTES, "max_robots_bytes": MAX_ROBOTS_BYTES,
        "max_attempts_per_url": MAX_ATTEMPTS, "max_redirects": MAX_REDIRECTS,
        "request_delay_seconds": effective_delay,
        "request_counts": counts, "robots_check": robots_audit,
        "price_interpretation": "advertised asking price; not transaction price",
        "completeness": "sampled preview; not a complete inventory",
        "availability_inference": False, "training_performed": False,
        "data_use": "local price-field preview; no AI input or training",
        "source_use_permission": "not established by robots.txt",
        "scope": "one Warsaw apartment-sale search page; no market-wide conclusions",
    }
    return records, report


def _search_limits(max_pages, max_listings, delay):
    if type(max_pages) is not int or not 1 <= max_pages <= 1000:
        raise OtodomError("Limit stron musi być liczbą całkowitą od 1 do 1000.")
    if type(max_listings) is not int or not 1 <= max_listings <= 50000:
        raise OtodomError("Limit ofert musi być liczbą całkowitą od 1 do 50000.")
    _limits(1, delay)


def _page_url(base_url, number):
    if number == 1:
        return _validate_search_page_url(base_url, expected_page=1)
    parts = urlsplit(base_url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True, max_num_fields=100)
             if key.casefold() != "page"]
    query.append(("page", str(number)))
    return _validate_search_page_url(
        urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), "")), expected_page=number,
    )


def _parse_search_page(html, page_url, observed_at, number):
    try:
        from src.otodom import parse_otodom_search_page

        return parse_otodom_search_page(html, page_url, observed_at=observed_at, expected_page=number)
    except (ValueError, TypeError, KeyError, ImportError):
        # Never include a provider payload, title or exception in a saved audit.
        raise OtodomError("HTML Otodom nie potwierdza żądanej strony lub zawiera blokadę albo zmieniony format.") from None


def collect_otodom_search(*, url=DEFAULT_URL, max_pages=5, max_listings=500, delay=2.0,
                          sleep=time.sleep, observed_at=None, session=None, html_pages=None):
    """Collect bounded public result pages; return validated rows and an audit.

    HTML pages passed as a list provide an entirely offline test path. The live
    path reads robots.txt once in one session and checks its policy for every
    page and redirect. No advertised sort order is assumed to be chronological.
    A later-page failure preserves earlier validated pages with status partial;
    a first-page failure raises. Even reaching the advertised final page cannot
    establish a complete inventory or the disappearance of an apartment.
    """
    requested_url = validate_search_url(url)
    _search_limits(max_pages, max_listings, delay)
    timestamp = _timestamp(observed_at)
    if html_pages is not None and (
        not isinstance(html_pages, list) or not html_pages
        or any(not isinstance(page, (str, bytes)) for page in html_pages)
    ):
        raise OtodomError("Tryb offline wymaga niepustej listy stron HTML jako tekstu lub bajtów.")
    counts = {"robots": 0, "html": 0}
    metadata, records = [], {}
    urls, page_fingerprints = {}, set()
    parsed_count = duplicate_count = conflict_count = 0
    effective_delay, own_session, policy = delay, False, None
    robots_audit = {"status": "not_performed_offline"}
    status, termination, sanitized_error = "ok", "max_pages", None
    final_url, base_url = requested_url, requested_url
    try:
        if html_pages is None:
            try:
                if session is None:
                    from curl_cffi import requests

                    session = requests.Session(trust_env=False, default_headers=False, discard_cookies=True, retry=0)
                    own_session = True
                robots_content, robots_url = _fetch_resource(
                    session, ROBOTS_URL, maximum=MAX_ROBOTS_BYTES, delay=delay, sleep=sleep,
                    counts=counts, kind="robots",
                )
                policy = _robots_policy(robots_content)
                crawl_delay, rate = policy.crawl_delay(USER_AGENT), policy.request_rate(USER_AGENT)
                effective_delay = max(delay, crawl_delay or 0, rate.seconds / rate.requests if rate else 0)
                robots_audit = {
                    "status": "allowed", "url": robots_url,
                    "checked_at": datetime.now(timezone.utc).isoformat(), "user_agent": USER_AGENT,
                    "sha256": hashlib.sha256(robots_content).hexdigest(),
                    "content_signals": _content_signals(robots_content),
                }
            except OtodomError:
                raise
            except Exception:
                raise OtodomError("Nie udało się przygotować odczytu Otodom; pobieranie zatrzymano.") from None
        for number in range(1, max_pages + 1):
            if html_pages is not None and number > len(html_pages):
                status, termination = "partial", "offline_pages_exhausted"
                break
            try:
                page_url = _page_url(base_url, number)
                if html_pages is None:
                    content, current_url = _fetch_resource(
                        session, page_url, maximum=MAX_HTML_BYTES, delay=effective_delay, sleep=sleep,
                        counts=counts, kind="html", policy=policy, expected_page=number,
                    )
                else:
                    content, current_url = _offline_content(html_pages[number - 1]), page_url
                page_records, page_metadata = _parse_search_page(_decode(content), current_url, timestamp, number)
                _validate_records(page_records)
                for row in page_records:
                    if row["url"] in urls and urls[row["url"]] != row["listing_id"]:
                        raise OtodomError("Sprzeczne identyfikatory adresu Otodom między stronami.")
                page_metadata.update(
                    requested_url=page_url, final_url=current_url,
                    html_bytes=len(content), html_sha256=hashlib.sha256(content).hexdigest(),
                )
                pagination = page_metadata["pagination"]
                if metadata:
                    baseline = metadata[0]["pagination"]
                    page_metadata["pagination_changed"] = any(
                        pagination[key] != baseline[key] for key in ("totalItems", "totalPages", "itemsPerPage")
                    )
                else:
                    page_metadata["pagination_changed"] = False
            except OtodomError:
                if not metadata:
                    raise
                status, termination = "partial", "page_error"
                sanitized_error = {"page": number, "reason": "page_access_or_validation_failed"}
                break
            except Exception:
                if not metadata:
                    raise OtodomError("Nie udało się zweryfikować pierwszej strony Otodom.") from None
                status, termination = "partial", "page_error"
                sanitized_error = {"page": number, "reason": "page_access_or_validation_failed"}
                break
            # Only commit an entire verified page to the result set. Later
            # encounters replace earlier source IDs while keeping stable order.
            metadata.append(page_metadata)
            final_url = current_url
            if number == 1:
                base_url = current_url
            parsed_count += len(page_records)
            fingerprint = tuple(sorted((row["listing_id"], tuple(row[field] for field in FIELDS))
                                       for row in page_records))
            repeated = fingerprint in page_fingerprints
            page_fingerprints.add(fingerprint)
            budget_reached = False
            for row in page_records:
                identifier = row["listing_id"]
                if identifier in records:
                    duplicate_count += 1
                    if records[identifier] != row:
                        conflict_count += 1
                    if records[identifier]["url"] != row["url"]:
                        urls.pop(records[identifier]["url"], None)
                    records[identifier] = row
                elif len(records) < max_listings:
                    records[identifier] = row
                else:
                    budget_reached = True
                    continue
                urls[row["url"]] = identifier
            if repeated:
                status, termination = "partial", "repeated_page"
                sanitized_error = {"page": number, "reason": "repeated_eligible_results"}
                break
            if budget_reached or len(records) >= max_listings:
                termination = "max_listings"
                break
            if number >= pagination["totalPages"]:
                termination = "pagination_end"
                break
    finally:
        if own_session:
            try:
                session.close()
            except Exception:
                pass
    report = {
        "source": "www.otodom.pl", "status": status,
        "collection_mode": "offline_search" if html_pages is not None else "live_search",
        "requested_url": requested_url, "final_url": final_url, "observed_at": timestamp.isoformat(),
        "pages_read": len(metadata), "page_metadata": metadata,
        "parsed_listings": parsed_count, "exported_listings": len(records),
        "max_pages": max_pages, "max_listings": max_listings, "termination": termination,
        "duplicate_encounters": duplicate_count, "cross_page_conflicts": conflict_count,
        "duplicate_resolution": "last validated encounter by source listing ID; stable first-seen order",
        "request_counts": counts, "request_delay_seconds": effective_delay, "robots_check": robots_audit,
        "max_html_bytes": MAX_HTML_BYTES, "max_robots_bytes": MAX_ROBOTS_BYTES,
        "max_attempts_per_url": MAX_ATTEMPTS, "max_redirects": MAX_REDIRECTS,
        "detail_pages_fetched": 0, "availability_inference": False, "training_performed": False,
        "price_interpretation": "advertised asking price; not transaction price",
        "completeness": "bounded eligible search results; not a complete inventory",
        "sorting_interpretation": "source-reported sorting; publication chronology is not verified",
        "data_use": "price-field collection; no AI input or training",
        "source_use_permission": "not established by robots.txt",
        "scope": "Warsaw apartment-sale searches; verified priced apartments with 1-4 exact rooms",
    }
    if sanitized_error is not None:
        report["error"] = sanitized_error
    return list(records.values()), report


def _write_preview(records, report, output, audit_output):
    """Stage files and restore their previous pair after a handled commit failure.

    Replacement is atomic per file; this is not a crash-safe two-file transaction.
    """
    _validate_records(records)
    output, audit_output = Path(output), Path(audit_output)
    if output.resolve() == audit_output.resolve():
        raise OtodomError("CSV i raport muszą mieć różne ścieżki.")
    report_text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    staged, backups, replaced, retained = [], {}, [], set()

    def temporary_path(destination):
        handle, name = tempfile.mkstemp(prefix="." + destination.name + ".", suffix=".tmp", dir=destination.parent)
        os.close(handle)
        path = Path(name)
        staged.append(path)
        return path

    try:
        for destination in (output, audit_output):
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary_path(destination)
        _write_csv(records, staged[0])
        staged[1].write_text(report_text, encoding="utf-8")
        for destination in (output, audit_output):
            if destination.exists():
                backup = temporary_path(destination)
                shutil.copy2(destination, backup)
                backups[destination] = backup
            else:
                backups[destination] = None
        try:
            for temporary, destination in zip(staged[:2], (output, audit_output)):
                os.replace(temporary, destination)
                replaced.append(destination)
        except OSError:
            rollback_failed = False
            for destination in reversed(replaced):
                backup = backups[destination]
                try:
                    if backup is None:
                        destination.unlink(missing_ok=True)
                    else:
                        os.replace(backup, destination)
                except OSError:
                    rollback_failed = True
                    if backup is not None:
                        retained.add(backup)
            if rollback_failed:
                raise OtodomError("Nie udało się przywrócić spójnej pary plików po błędzie zapisu; zachowano kopie poprzednich plików.") from None
            raise
    finally:
        for temporary in staged:
            if temporary not in retained:
                temporary.unlink(missing_ok=True)


def _read_offline_file(path):
    with Path(path).open("rb") as stream:
        content = stream.read(MAX_HTML_BYTES + 1)
    return _offline_content(content)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Zapisz lokalny podgląd cen z jednej strony sprzedaży mieszkań Otodom w Warszawie.")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--html", type=Path, help="Lokalny HTML do testu bez sieci.")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "data" / "otodom_preview.csv")
    parser.add_argument("--audit", type=Path, default=PROJECT_ROOT / "data" / "otodom_preview_audit.json")
    parser.add_argument("--max-listings", type=int, default=20)
    parser.add_argument("--delay", type=float, default=2.0)
    args = parser.parse_args(argv)
    try:
        records, report = collect_otodom(
            url=args.url, html=_read_offline_file(args.html) if args.html is not None else None,
            delay=args.delay, max_listings=args.max_listings,
        )
        _write_preview(records, report, args.output, args.audit)
        print(f"Zapisano podgląd cen: {len(records)} ofert. To próbka jednej strony, bez oceny dostępności.")
        print(f"CSV: {args.output}")
        print(f"Raport: {args.audit}")
        return 0
    except OtodomError as error:
        print(f"Błąd Otodom: {error}")
        return 1
    except (ValueError, OSError):
        print("Nie udało się odczytać lokalnego pliku lub zapisać podglądu; sprawdź ścieżki i format danych.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
