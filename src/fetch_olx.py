"""Read public OLX search HTML with explicit bounded pagination support.

The default CLI remains a one-page, price-only preview. No database, LLM, paid
API, detail requests or access-control workarounds are used. A live read requires
a fresh, readable robots.txt.
An offline HTML input exercises the same parser without making requests.
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
from urllib.robotparser import RequestRate

from src.fetch_data import FIELDS, _write_csv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SEARCH_PATH = "/nieruchomosci/mieszkania/sprzedaz/warszawa/"
DEFAULT_URL = "https://www.olx.pl" + SEARCH_PATH
ROBOTS_URL = "https://www.olx.pl/robots.txt"
USER_AGENT = "WarsawRealEstatePortfolio/0.1"
REQUEST_TIMEOUT = 20
MAX_ATTEMPTS = 3
MAX_REDIRECTS = 3
MAX_HTML_BYTES = 8 * 1024 * 1024
MAX_ROBOTS_BYTES = 256 * 1024
MAX_LISTINGS = 100
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class OLXError(ValueError):
    """The preview cannot be collected or validated safely."""


def _validate_origin(url):
    if not isinstance(url, str) or not url or re.search(r"[\x00-\x20\x7f\\]", url):
        raise OLXError("Adres źródła jest nieprawidłowy.")
    try:
        parts = urlsplit(url)
        valid = (
            parts.scheme == "https" and parts.hostname == "www.olx.pl"
            and parts.username is None and parts.password is None
            and parts.port is None and not parts.fragment
            and parts.netloc.casefold() == "www.olx.pl"
        )
    except ValueError:
        raise OLXError("Adres źródła jest nieprawidłowy.") from None
    if not valid or "#" in url:
        raise OLXError("Dozwolony jest wyłącznie adres HTTPS www.olx.pl bez loginu, portu i fragmentu.")
    return parts


def validate_search_url(url, *, expected_page=1):
    """Allow the Warsaw sale search; pagination requires an expected page."""
    parts = _validate_origin(url)
    if type(expected_page) is not int or not 1 <= expected_page <= 1000:
        raise OLXError("Numer strony musi być liczbą całkowitą od 1 do 1000.")
    if parts.path not in {SEARCH_PATH, SEARCH_PATH.rstrip("/")}:
        raise OLXError("Wybierz stronę sprzedaży mieszkań OLX w Warszawie.")
    try:
        query = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=100)
    except ValueError:
        raise OLXError("Parametry adresu źródła są nieprawidłowe.") from None
    pages = [value for key, value in query if key.casefold() == "page"]
    if len(pages) > 1 or (pages and pages[0] != str(expected_page)) or (not pages and expected_page != 1):
        raise OLXError("Adres źródła nie potwierdza oczekiwanego numeru strony.")
    for key, value in query:
        decoded = key + " " + value
        for _ in range(3):
            decoded = unquote(decoded)
        if re.search(r"wynajem|wynaj[eę]|rental|\brent\b|lease|[\x00-\x1f\x7f\\]", decoded, re.IGNORECASE):
            raise OLXError("Adres podglądu nie może wybierać najmu.")
    return urlunsplit(("https", "www.olx.pl", parts.path, parts.query, ""))


def _timestamp(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise OLXError("Czas obserwacji musi zawierać poprawną strefę czasową.") from None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise OLXError("Czas obserwacji musi zawierać strefę czasową.")
    return value.astimezone(timezone.utc)


def _limits(max_listings, delay):
    if isinstance(max_listings, bool) or not isinstance(max_listings, int) or not 1 <= max_listings <= MAX_LISTINGS:
        raise OLXError("Limit ofert musi być liczbą całkowitą od 1 do 100.")
    if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not math.isfinite(delay) or delay < 2:
        raise OLXError("Opóźnienie musi być skończone i wynosić co najmniej 2 sekundy.")


def _decode(content):
    try:
        return content.decode("utf-8-sig")
    except UnicodeError:
        raise OLXError("Źródło nie zawiera oczekiwanego tekstu UTF-8.") from None


def _offline_content(html):
    if isinstance(html, str):
        try:
            content = html.encode("utf-8")
        except UnicodeError:
            raise OLXError("Lokalny HTML ma nieprawidłowe kodowanie.") from None
    elif isinstance(html, bytes):
        content = html
    else:
        raise OLXError("Lokalny HTML musi być tekstem lub bajtami.")
    if not content or len(content) > MAX_HTML_BYTES:
        raise OLXError("Lokalny HTML jest pusty lub przekracza limit 8 MiB.")
    return content


def _header(response, name):
    # Real curl_cffi headers are case-insensitive; this also accepts simple mocks.
    for key, value in response.headers.items():
        if str(key).casefold() == name.casefold():
            return str(value)
    return None


def _close_response(response):
    try:
        response.close()
    except Exception:
        # Cleanup must not replace a neutral source error with transport internals.
        pass


def _read_stream(response, maximum):
    length = _header(response, "Content-Length")
    if length is not None:
        try:
            declared = int(length)
        except ValueError:
            raise OLXError("Źródło podało nieprawidłowy rozmiar odpowiedzi.") from None
        if declared < 0 or declared > maximum:
            raise OLXError("Odpowiedź źródła przekracza dozwolony rozmiar.")
    content = bytearray()
    for chunk in response.iter_content():
        if not isinstance(chunk, bytes):
            raise OLXError("Źródło zwróciło nieoczekiwany format odpowiedzi.")
        if len(content) + len(chunk) > maximum:
            raise OLXError("Odpowiedź źródła przekracza dozwolony rozmiar.")
        content.extend(chunk)
    if not content:
        raise OLXError("Źródło zwróciło pustą odpowiedź.")
    return bytes(content)


def _fetch_resource(session, url, *, maximum, delay, sleep, counts, kind, policy=None, expected_page=1):
    """Bound streaming, retries and redirects; check robots before each HTML URL."""
    from curl_cffi.requests.exceptions import Timeout as CurlTimeout

    current = url
    for redirect in range(MAX_REDIRECTS + 1):
        if kind == "html":
            current = validate_search_url(current, expected_page=expected_page)
            if policy is None or not policy.can_fetch(USER_AGENT, current):
                raise OLXError("robots.txt nie zezwala temu kolektorowi na wybrany adres; pobieranie zatrzymano.")
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
                    raise OLXError(f"OLX zwrócił HTTP {status}; pobieranie zatrzymano.")
                if 500 <= status < 600:
                    if attempt == MAX_ATTEMPTS - 1:
                        raise OLXError("Źródło wielokrotnie zwróciło błąd serwera; pobieranie zatrzymano.")
                    continue
                if status in REDIRECT_STATUSES:
                    location = _header(response, "Location")
                    if not location or redirect == MAX_REDIRECTS:
                        raise OLXError("Źródło zwróciło nieprawidłowe lub zbyt liczne przekierowania.")
                    target = urljoin(current, location)
                    _validate_origin(target)
                    if kind == "robots":
                        # An unavailable robots.txt never means permission to read.
                        raise OLXError("robots.txt musi być dostępny bez przekierowania z HTTP 200.")
                    current = validate_search_url(target, expected_page=expected_page)
                    break
                if status != 200:
                    raise OLXError(f"Źródło zwróciło HTTP {status}; pobieranie zatrzymano.")
                content_type = (_header(response, "Content-Type") or "").split(";")[0].strip().casefold()
                expected = {"text/plain"} if kind == "robots" else {"text/html", "application/xhtml+xml"}
                if content_type and content_type not in expected:
                    raise OLXError("Źródło zwróciło nieoczekiwany typ odpowiedzi.")
                return _read_stream(response, maximum), current
            except (CurlTimeout, TimeoutError):
                if attempt == MAX_ATTEMPTS - 1:
                    raise OLXError("Źródło nie odpowiedziało w limicie czasu po 3 próbach.") from None
            except OLXError:
                raise
            except Exception:
                raise OLXError("Nie udało się odczytać źródła; pobieranie zatrzymano.") from None
            finally:
                if response is not None:
                    _close_response(response)
        else:
            raise OLXError("Nie udało się odczytać źródła w dozwolonej liczbie prób.")
    raise OLXError("Źródło przekroczyło limit przekierowań.")


def _robots_octets(value, *, pattern=False):
    """Normalize URI octets without decoding reserved percent-encoded octets.

    See RFC 9309 sections 2.2.2/2.2.3:
    https://www.rfc-editor.org/rfc/rfc9309.html#section-2.2.2
    Path/query delimiters retain their structural roles. Reserved characters
    inside query values and literal stars/dollars use percent encoding.
    """
    unreserved = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"

    def component(text, safe=""):
        result = []
        index = 0
        while index < len(text):
            character = text[index]
            encoded = text[index + 1:index + 3]
            if character == "%" and len(encoded) == 2 and re.fullmatch(r"[0-9A-Fa-f]{2}", encoded):
                decoded = chr(int(encoded, 16))
                result.append(decoded if decoded in unreserved else "%" + encoded.upper())
                index += 3
                continue
            if character in unreserved or character in safe or (pattern and character == "*"):
                result.append(character)
            else:
                result.extend(f"%{octet:02X}" for octet in character.encode("utf-8"))
            index += 1
        return "".join(result)

    path, separator, query = value.partition("?")
    normalized = component(path, "/")
    if separator:
        parameters = []
        for parameter in query.split("&"):
            key, assignment, item = parameter.partition("=")
            parameters.append(component(key) + assignment + component(item))
        normalized += "?" + "&".join(parameters)
    return normalized


def _robots_match(pattern, target, anchored):
    """Match literal segments and '*' without backtracking on untrusted rules."""
    segments = pattern.split("*")
    if len(segments) == 1:
        return target == pattern if anchored else target.startswith(pattern)
    if not target.startswith(segments[0]):
        return False
    position = len(segments[0])
    for segment in segments[1:-1]:
        found = target.find(segment, position)
        if found == -1:
            return False
        position = found + len(segment)
    tail = segments[-1]
    if anchored:
        return target.endswith(tail) and len(target) - len(tail) >= position
    return target.find(tail, position) != -1


class _RobotsPolicy:
    """REP rule matching plus conservative delays for the selected groups.

    This implements rule matching, not a general-purpose RFC 9309 client: the
    transport deliberately requires HTTP 200 and applies stricter size limits.
    """

    def __init__(self, groups):
        self.groups = groups

    def _selected(self, user_agent):
        token = user_agent.split("/", 1)[0].casefold()
        scores = [max((len(agent) for agent in group["agents"] if agent != "*" and agent.casefold() in token), default=0) for group in self.groups]
        best = max(scores, default=0)
        if best:
            return [group for group, score in zip(self.groups, scores) if score == best]
        return [group for group in self.groups if "*" in group["agents"]]

    def can_fetch(self, user_agent, url):
        parts = urlsplit(url)
        if parts.path == "/robots.txt":
            return True
        target = parts.path or "/"
        if parts.query or "?" in url.split("#", 1)[0]:
            target += "?" + parts.query
        target = _robots_octets(target)
        matches = []
        for group in self._selected(user_agent):
            for rule in group["rules"]:
                if _robots_match(rule["pattern"], target, rule["anchored"]):
                    matches.append((rule["octets"], rule["allow"]))
        return max(matches)[1] if matches else True  # Equal specificity prefers Allow.

    def crawl_delay(self, user_agent):
        delays = [group["delay"] for group in self._selected(user_agent) if group["delay"] is not None]
        return max(delays) if delays else None

    def request_rate(self, user_agent):
        rates = [group["rate"] for group in self._selected(user_agent) if group["rate"] is not None]
        return max(rates, key=lambda rate: rate.seconds / rate.requests) if rates else None


def _robots_policy(content):
    text = _decode(content)
    groups = []
    group = None
    has_rules = False
    for line in text.splitlines():
        directive = line.split("#", 1)[0].strip()
        if not directive:
            continue
        if ":" not in directive:
            raise OLXError("robots.txt ma nieoczekiwany format; pobieranie zatrzymano.")
        name, value = (part.strip() for part in directive.split(":", 1))
        if not re.fullmatch(r"[A-Za-z][A-Za-z-]*", name):
            raise OLXError("robots.txt ma nieoczekiwany format; pobieranie zatrzymano.")
        if name.casefold() == "user-agent":
            if not value or not re.fullmatch(r"[A-Za-z0-9*_.\-/]+", value):
                raise OLXError("robots.txt ma nieprawidłową grupę User-agent.")
            if group is None or has_rules:
                group = {"agents": [], "rules": [], "delay": None, "rate": None}
                groups.append(group)
                has_rules = False
            group["agents"].append(value)
        elif name.casefold() in {"allow", "disallow"}:
            if group is None:
                raise OLXError("robots.txt ma reguły bez grupy User-agent.")
            has_rules = True
            if not value:
                continue  # An empty pattern does not disallow every URL.
            if not value.startswith(("/", "*")) or re.search(r"[\x00-\x1f\x7f]", value):
                raise OLXError("robots.txt ma nieprawidłowy wzorzec ścieżki.")
            anchored = value.endswith("$")
            pattern = _robots_octets(value[:-1] if anchored else value, pattern=True)
            octets = len(re.findall(r"%[0-9A-F]{2}|[^*]", pattern))
            group["rules"].append({"allow": name.casefold() == "allow", "pattern": pattern, "anchored": anchored, "octets": octets})
        elif group is not None and name.casefold() == "crawl-delay":
            if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) or not math.isfinite(float(value)):
                raise OLXError("robots.txt ma nieprawidłowe opóźnienie.")
            group["delay"] = max(group["delay"] or 0, float(value))
        elif group is not None and name.casefold() == "request-rate":
            if not re.fullmatch(r"[0-9]+/[0-9]+", value):
                raise OLXError("robots.txt ma nieprawidłowy limit częstotliwości.")
            requests_count, seconds = (int(part) for part in value.split("/"))
            if requests_count <= 0 or seconds <= 0:
                raise OLXError("robots.txt ma nieprawidłowy limit częstotliwości.")
            rate = RequestRate(requests_count, seconds)
            if group["rate"] is None or rate.seconds / rate.requests > group["rate"].seconds / group["rate"].requests:
                group["rate"] = rate
    if not groups:
        raise OLXError("robots.txt nie zawiera poprawnej grupy User-agent; pobieranie zatrzymano.")
    return _RobotsPolicy(groups)


def _parse_html(html, page_url, observed_at):
    try:
        from src.olx import parse_olx_search

        return parse_olx_search(html, page_url, observed_at=observed_at)
    except ValueError:
        raise OLXError("HTML OLX zawiera blokadę dostępu lub nieoczekiwany format; nic nie zapisano.") from None
    except (ImportError, TypeError, KeyError):
        raise OLXError("Nie udało się odczytać formatu OLX; nic nie zapisano.") from None


def _validate_records(records):
    if not isinstance(records, list) or not records:
        raise OLXError("Brak poprawnych ofert z ceną; nic nie zapisano.")
    identities = set()
    for row in records:
        if not isinstance(row, dict) or set(row) != set(FIELDS):
            raise OLXError("Parser zwrócił nieprawidłowy format oferty; nic nie zapisano.")
        price = row["price_pln"]
        if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
            raise OLXError("Parser zwrócił nieprawidłową cenę; nic nie zapisano.")
        area = row["area_m2"]
        if isinstance(area, bool) or not isinstance(area, (int, float)) or not math.isfinite(area) or area <= 0:
            raise OLXError("Parser zwrócił nieprawidłowy metraż; nic nie zapisano.")
        if type(row["rooms"]) is not int or row["rooms"] not in {1, 2, 3}:
            raise OLXError("Parser zwrócił nieprawidłową dokładną liczbę pokoi; nic nie zapisano.")
        if row["source"] != "www.olx.pl" or not isinstance(row["listing_id"], str) or not row["listing_id"].strip() or row["city"] != "Warszawa":
            raise OLXError("Parser zwrócił nieprawidłową tożsamość oferty; nic nie zapisano.")
        parts = _validate_origin(row["url"])
        if not re.fullmatch(r"/d/oferta/[^/]+\.html", parts.path):
            raise OLXError("Parser zwrócił nieprawidłowy adres oferty OLX; nic nie zapisano.")
        identity = (row["source"], row["listing_id"])
        if identity in identities:
            raise OLXError("Parser zwrócił powtórzone tożsamości ofert; nic nie zapisano.")
        identities.add(identity)
        row["observed_at"] = _timestamp(row["observed_at"])


def collect_olx(*, url=DEFAULT_URL, html=None, session=None, delay=2.0, sleep=time.sleep, observed_at=None, max_listings=20):
    """Return (records, audit) for one search page; html bypasses all networking.

    Records pair advertised asking prices with area and exact room counts.
    The result is a sample; neither disappearance nor presence establishes
    availability or a sale.
    A session may be injected for offline transport tests; none is made for HTML.
    """
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
            }
        except OLXError:
            raise
        except Exception:
            raise OLXError("Nie udało się przygotować odczytu OLX; pobieranie zatrzymano.") from None
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
        "source": "www.olx.pl", "collection_mode": "offline_preview" if html is not None else "live_preview",
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
        "availability_inference": False,
        "scope": "one Warsaw apartment-sale search page; no market-wide conclusions",
    }
    return records, report


def _search_page_url(url, page):
    """Build public search URLs only; do not follow embedded API links."""
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key.casefold() != "page"]
    if page != 1:
        query.append(("page", str(page)))
    result = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
    return validate_search_url(result, expected_page=page)


def _parse_search_page(content, page_url, timestamp, page):
    try:
        from src.olx import parse_olx_search_page

        records, metadata = parse_olx_search_page(
            _decode(content), page_url, observed_at=timestamp, expected_page=page,
        )
    except (ValueError, TypeError, KeyError, ImportError):
        raise OLXError("HTML OLX nie potwierdza oczekiwanej strony lub poprawnej struktury; pobieranie zatrzymano.") from None
    if records:
        _validate_records(records)
    return records, metadata


def collect_olx_search(*, url=DEFAULT_URL, max_pages=5, max_listings=500, delay=2.0,
                       sleep=time.sleep, observed_at=None, session=None, html_pages=None):
    """Collect a bounded sequence of public Warsaw sale search pages.

    Pagination is explicit opt-in, separate from the original preview API.
    Every run starts at page one, with one fresh robots policy and one session.
    The source's default ordering is retained; no date or inventory inference is
    made. A later-page failure preserves earlier validated rows with a partial
    audit. ``html_pages`` exercises the whole flow without any network or file
    access. Only priced apartments with exact supported room counts qualify.
    """
    requested_url = validate_search_url(url)
    if type(max_pages) is not int or not 1 <= max_pages <= 1000:
        raise OLXError("Limit stron musi być liczbą całkowitą od 1 do 1000.")
    if type(max_listings) is not int or not 1 <= max_listings <= 50000:
        raise OLXError("Limit ofert musi być liczbą całkowitą od 1 do 50000.")
    _limits(1, delay)
    if html_pages is not None and (not isinstance(html_pages, list) or not html_pages):
        raise OLXError("Lokalne strony HTML muszą być niepustą listą.")
    timestamp = _timestamp(observed_at)
    counts = {"robots": 0, "html": 0}
    report = {
        "source": "www.olx.pl", "status": "ok", "observed_at": timestamp.isoformat(),
        "collection_mode": "offline_search" if html_pages is not None else "live_search",
        "pages_read": 0, "parsed_listings": 0, "exported_listings": 0,
        "max_pages": max_pages, "max_listings": max_listings, "request_counts": counts,
        "termination": "page_budget", "completeness": "incomplete: bounded eligible search results; not a full catalogue",
        "page_metadata": [], "cross_page_duplicates": 0, "cross_page_conflicts": 0,
        "duplicate_resolution": "last encountered validated record for each source listing ID",
        "ordering": "source default; chronological order is not established",
        "availability_inference": False, "training_performed": False, "detail_pages_fetched": 0,
        "price_interpretation": "advertised asking price; not transaction price",
    }
    own_session = False
    effective_delay = delay
    records_by_id, urls, page_fingerprints = {}, {}, set()
    policy = None
    try:
        if html_pages is None:
            try:
                if session is None:
                    from curl_cffi import requests

                    session = requests.Session(trust_env=False, default_headers=False, discard_cookies=True, retry=0)
                    own_session = True
                robots_content, robots_url = _fetch_resource(
                    session, ROBOTS_URL, maximum=MAX_ROBOTS_BYTES, delay=delay,
                    sleep=sleep, counts=counts, kind="robots",
                )
                policy = _robots_policy(robots_content)
                rate = policy.request_rate(USER_AGENT)
                effective_delay = max(delay, policy.crawl_delay(USER_AGENT) or 0, rate.seconds / rate.requests if rate else 0)
                report["robots_check"] = {
                    "status": "allowed", "url": robots_url, "user_agent": USER_AGENT,
                    "sha256": hashlib.sha256(robots_content).hexdigest(),
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }
            except OLXError:
                raise
            except Exception:
                raise OLXError("Nie udało się przygotować odczytu OLX; pobieranie zatrzymano.") from None
        else:
            report["robots_check"] = {"status": "not_performed_offline"}
        report["request_delay_seconds"] = effective_delay
        for page in range(1, max_pages + 1):
            if html_pages is not None and page > len(html_pages):
                report.update(status="partial", termination="offline_input_exhausted")
                break
            try:
                page_url = _search_page_url(requested_url, page)
                if html_pages is not None:
                    content, final_url = _offline_content(html_pages[page - 1]), page_url
                else:
                    content, final_url = _fetch_resource(
                        session, page_url, maximum=MAX_HTML_BYTES, delay=effective_delay,
                        sleep=sleep, counts=counts, kind="html", policy=policy, expected_page=page,
                    )
                report["pages_read"] += 1
                rows, metadata = _parse_search_page(content, final_url, timestamp, page)
                if page == 1 and not rows:
                    raise OLXError("Pierwsza strona OLX nie zawiera poprawnych ofert; pobieranie zatrzymano.")
                # Reject conflicting URL identities before modifying earlier results.
                for row in rows:
                    if row["url"] in urls and urls[row["url"]] != row["listing_id"]:
                        raise OLXError("Sprzeczne identyfikatory adresu OLX między stronami; pobieranie zatrzymano.")
                metadata["html_bytes"] = len(content)
                metadata["html_sha256"] = hashlib.sha256(content).hexdigest()
                report["page_metadata"].append(metadata)
                report["parsed_listings"] += len(rows)
                fingerprint = tuple(sorted(row["listing_id"] for row in rows))
                repeated = bool(rows) and fingerprint in page_fingerprints
                page_fingerprints.add(fingerprint)
                exceeded_listing_budget = False
                for row in rows:
                    identity = row["listing_id"]
                    previous = records_by_id.get(identity)
                    if previous is not None:
                        report["cross_page_duplicates"] += 1
                        report["cross_page_conflicts"] += int(previous != row)
                        if previous["url"] != row["url"]:
                            urls.pop(previous["url"], None)
                    elif len(records_by_id) >= max_listings:
                        exceeded_listing_budget = True
                        continue
                    records_by_id[identity] = row
                    urls[row["url"]] = identity
                if exceeded_listing_budget or len(records_by_id) >= max_listings:
                    report["termination"] = "listing_budget"
                    break
                if repeated:
                    report.update(status="partial", termination="repeated_page")
                    break
                if metadata["raw_items"] == 0:
                    report.update(status="partial", termination="empty_page")
                    break
                total_pages = metadata["total_pages"]
                if total_pages is None:
                    report.update(status="partial", termination="pagination_metadata_unavailable")
                    break
                if page >= total_pages:
                    report["termination"] = "reported_search_end"
                    break
            except OLXError as error:
                if page == 1:
                    raise
                report.update(status="partial", termination="page_error", failed_page=page, error=str(error))
                break
    finally:
        if own_session:
            try:
                session.close()
            except Exception:
                pass
    records = list(records_by_id.values())
    _validate_records(records)
    report["exported_listings"] = len(records)
    report["skipped_items"] = sum(item["skipped_items"] for item in report["page_metadata"])
    report["reported_result_cap"] = any(item["reported_result_cap"] for item in report["page_metadata"])
    return records, report


def _write_preview(records, report, output, audit_output):
    """Replace each staged file atomically; roll back both on commit failure.

    Backups protect handled write failures. This is not a crash-safe transaction
    across two files, so a machine/process crash can still interrupt the pair.
    """
    _validate_records(records)
    output, audit_output = Path(output), Path(audit_output)
    if output.resolve() == audit_output.resolve():
        raise OLXError("CSV i raport muszą mieć różne ścieżki.")
    report_text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    staged = []
    backups = {}
    replaced = []
    retained = set()

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
                raise OLXError("Nie udało się przywrócić spójnej pary plików po błędzie zapisu; zachowano kopie poprzednich plików.") from None
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
    parser = argparse.ArgumentParser(description="Zapisz lokalny podgląd cen z jednej strony sprzedaży mieszkań OLX w Warszawie.")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--html", type=Path, help="Lokalny HTML do testu bez sieci.")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "data" / "olx_preview.csv")
    parser.add_argument("--audit", type=Path, default=PROJECT_ROOT / "data" / "olx_preview_audit.json")
    parser.add_argument("--max-listings", type=int, default=20)
    parser.add_argument("--delay", type=float, default=2.0)
    args = parser.parse_args(argv)
    try:
        records, report = collect_olx(
            url=args.url, html=_read_offline_file(args.html) if args.html is not None else None,
            delay=args.delay, max_listings=args.max_listings,
        )
        _write_preview(records, report, args.output, args.audit)
        print(f"Zapisano podgląd cen: {len(records)} ofert. To próbka jednej strony, bez oceny dostępności.")
        print(f"CSV: {args.output}")
        print(f"Raport: {args.audit}")
        return 0
    except OLXError as error:
        print(f"Błąd OLX: {error}")
        return 1
    except (ValueError, OSError):
        print("Nie udało się odczytać lokalnego pliku lub zapisać podglądu; sprawdź ścieżki i format danych.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
