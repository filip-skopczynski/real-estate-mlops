"""Bounded Warsaw portal observations, optional storage and per-source audits.

Run with --save-db to store prices. Defaults produce local files only. This
runner neither infers availability nor trains a model from portal data.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import tempfile

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src import database
from src.fetch_data import _write_csv
from src.fetch_olx import MAX_HTML_BYTES, _offline_content, _timestamp

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_NAMES = {"olx": "www.olx.pl", "otodom": "www.otodom.pl"}


def _validate_limits(max_pages_olx, max_pages_otodom, max_listings, delay):
    for pages in (max_pages_olx, max_pages_otodom):
        if type(pages) is not int or not 1 <= pages <= 1000:
            raise ValueError("Limit stron musi wynosić od 1 do 1000.")
    if type(max_listings) is not int or not 1 <= max_listings <= 50000:
        raise ValueError("Limit ofert na źródło musi wynosić od 1 do 50000.")
    if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not math.isfinite(delay) or delay < 2:
        raise ValueError("Opóźnienie musi wynosić co najmniej 2 sekundy.")


def run_collection(*, source="both", max_pages_olx=5, max_pages_otodom=5,
                   max_listings=500, delay=2.0, observed_at=None, engine=None,
                   offline_pages=None, collectors=None):
    """Return rows and audit; a supplied engine enables atomic per-source writes.

    Each run uses one aware UTC timestamp. Replays with that same timestamp are
    idempotent in storage. Separate live captures use their actual capture time.
    A failure in one source preserves observations from the other source.
    """
    _validate_limits(max_pages_olx, max_pages_otodom, max_listings, delay)
    if source not in {"both", *SOURCE_NAMES}:
        raise ValueError("Wybierz źródło both, olx albo otodom.")
    timestamp = _timestamp(observed_at)
    selected = list(SOURCE_NAMES) if source == "both" else [source]
    if offline_pages is not None and set(offline_pages) != set(selected):
        raise ValueError("Tryb bez sieci wymaga HTML dla każdego wybranego źródła.")
    if collectors is None:
        from src.fetch_olx import collect_olx_search
        from src.fetch_otodom import collect_otodom_search

        collectors = {"olx": collect_olx_search, "otodom": collect_otodom_search}
    from src.fetch_olx import OLXError, _validate_records as validate_olx
    from src.fetch_otodom import OtodomError, _validate_records as validate_otodom

    validators = {"olx": validate_olx, "otodom": validate_otodom}
    report = {
        "observed_at": timestamp.isoformat(), "status": "ok",
        "scope": "Warszawa; apartment sales; bounded public search pages",
        "collection_mode": "offline" if offline_pages is not None else "live",
        "database_requested": engine is not None, "sources": {},
        "completeness": "incomplete; no full Warsaw catalogue guarantee",
        "new_listing_definition": "first source/ID stored by this system; not publication date or a new property",
        "cross_source_property_deduplication": False,
        "availability_inference": False, "training_performed": False,
    }
    all_rows = []
    for name in selected:
        pages_limit = max_pages_olx if name == "olx" else max_pages_otodom
        options = {"max_pages": pages_limit, "max_listings": max_listings,
                   "delay": delay, "observed_at": timestamp}
        if offline_pages is not None:
            options["html_pages"] = offline_pages[name]
        try:
            rows, source_report = collectors[name](**options)
            if rows:
                validators[name](rows)
                if any(row["observed_at"] != timestamp for row in rows):
                    raise ValueError("Niezgodny czas obserwacji.")
            if not isinstance(source_report, dict) or source_report.get("source") != SOURCE_NAMES[name]:
                raise ValueError("Nieprawidłowy raport źródła.")
            if source_report.get("status") not in {"ok", "partial"}:
                raise ValueError("Nieprawidłowy status źródła.")
            # Source reports contain counts/metadata only, never HTML or tokens.
            json.dumps(source_report, allow_nan=False)
        except (OLXError, OtodomError, ValueError, TypeError):
            report["sources"][name] = {
                "source": SOURCE_NAMES[name], "status": "failed",
                "error": "source_read_or_validation_failed", "exported_listings": 0,
                "database_status": "not_written",
            }
            report["status"] = "partial"
            continue
        source_report = dict(source_report)
        all_rows.extend(rows)
        if source_report["status"] == "partial":
            report["status"] = "partial"
        if engine is None:
            source_report["database_status"] = "not_requested"
        else:
            try:
                source_report["database_statistics"] = database.upsert_listings_report(engine, rows)
                source_report["database_status"] = "saved"
            except (SQLAlchemyError, ValueError):
                # SQLAlchemy exception text can contain credentials or SQL parameters.
                source_report["database_status"] = "failed_rolled_back"
                source_report["database_error"] = "database_write_failed"
                report["status"] = "partial"
        report["sources"][name] = source_report
    report["exported_listings"] = len(all_rows)
    if all(item["status"] == "failed" for item in report["sources"].values()):
        report["status"] = "failed"
    return all_rows, report


def _write_outputs(rows, report, output, audit):
    """Stage outputs; replace each file atomically, never write raw source HTML."""
    output, audit = Path(output), Path(audit)
    if output.resolve() == audit.resolve():
        raise ValueError("CSV i raport muszą mieć różne ścieżki.")
    staged = []
    try:
        for path in (output, audit):
            path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
            os.close(handle)
            staged.append(Path(temporary))
        _write_csv(rows, staged[0])
        staged[1].write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        for temporary, path in zip(staged, (output, audit)):
            os.replace(temporary, path)
    finally:
        for path in staged:
            path.unlink(missing_ok=True)


def _read_pages(paths, max_pages):
    if not paths or len(paths) > max_pages:
        raise ValueError("Liczba plików HTML musi mieścić się w limicie stron.")
    pages, total = [], 0
    for path in paths:
        with path.open("rb") as stream:
            content = _offline_content(stream.read(MAX_HTML_BYTES + 1))
        total += len(content)
        if total > 64 * 1024 * 1024:
            raise ValueError("Lokalne HTML przekraczają łączny limit 64 MiB na źródło.")
        pages.append(content)
    return pages


def main(argv=None):
    parser = argparse.ArgumentParser(description="Zbierz ograniczony zakres ofert sprzedaży mieszkań w Warszawie.")
    parser.add_argument("--source", choices=["both", "olx", "otodom"], default="both")
    parser.add_argument("--max-pages-olx", type=int, default=os.environ.get("OLX_MAX_PAGES", "5"))
    parser.add_argument("--max-pages-otodom", type=int, default=os.environ.get("OTODOM_MAX_PAGES", "5"))
    parser.add_argument("--max-listings", type=int, default=os.environ.get("PORTAL_MAX_LISTINGS", "500"))
    parser.add_argument("--delay", type=float, default=os.environ.get("REQUEST_DELAY_SECONDS", "2"))
    parser.add_argument("--observed-at", help="ISO 8601 ze strefą; ten sam czas dla odtworzenia pobrania.")
    parser.add_argument("--save-db", action="store_true")
    parser.add_argument("--offline-olx", type=Path, nargs="+")
    parser.add_argument("--offline-otodom", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "data" / "daily_listings.csv")
    parser.add_argument("--report", type=Path, default=PROJECT_ROOT / "data" / "daily_collection_audit.json")
    args = parser.parse_args(argv)
    engine = None
    try:
        _validate_limits(args.max_pages_olx, args.max_pages_otodom, args.max_listings, args.delay)
        if args.output.resolve() == args.report.resolve():
            raise ValueError("CSV i raport muszą mieć różne ścieżki.")
        timestamp = _timestamp(args.observed_at)
        offline = None
        if args.offline_olx or args.offline_otodom:
            offline = {}
            for name, paths, limit in (("olx", args.offline_olx, args.max_pages_olx),
                                       ("otodom", args.offline_otodom, args.max_pages_otodom)):
                if paths:
                    offline[name] = _read_pages(paths, limit)
            selected = set(SOURCE_NAMES) if args.source == "both" else {args.source}
            if set(offline) != selected:
                raise ValueError("Tryb bez sieci wymaga HTML dla każdego wybranego źródła.")
        if args.save_db:
            engine = database.get_engine()
            if offline is not None and engine.dialect.name != "sqlite":
                raise ValueError("Zapis lokalnych przykładów HTML wymaga demonstracyjnej bazy SQLite.")
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            database.init_db(engine)
        rows, report = run_collection(
            source=args.source, max_pages_olx=args.max_pages_olx,
            max_pages_otodom=args.max_pages_otodom, max_listings=args.max_listings,
            delay=args.delay, observed_at=timestamp, engine=engine, offline_pages=offline,
        )
        _write_outputs(rows, report, args.output, args.report)
        print(f"Wynik: {report['status']}; odczytane oferty: {len(rows)}. Zakres jest niepełny.")
        for name, source_report in report["sources"].items():
            statistics = source_report.get("database_statistics")
            if statistics:
                print(f"{name}: nowe ID {statistics['new_listings']}, znane ID {statistics['existing_listings']}, dopisane obserwacje {statistics['observations_inserted']}.")
            else:
                print(f"{name}: {source_report['status']}; zapis bazy: {source_report['database_status']}.")
        print(f"Raport: {args.report}")
        return 0 if report["status"] == "ok" else 1
    except (ValueError, OSError, SQLAlchemyError):
        print("Pobranie nie zostało ukończone. Sprawdź konfigurację, lokalne pliki i połączenie z bazą.")
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
