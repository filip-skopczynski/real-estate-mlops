"""Resumable Warsaw catalogue bootstrap, newest discovery and price refresh.

Only --save-db enables persistence. Price observations and the cursor commit
together. Missing listings never imply a sale, and this runner never trains ML.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import time
from uuid import uuid4

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from src import catalog_storage as storage, database
from src.fetch_olx import _timestamp

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_NAMES = {"olx": "www.olx.pl", "otodom": "www.otodom.pl"}


def _retry(engine, operation):
    """Repeat identical writes after a lost connection; never log SQL parameters."""
    for attempt in range(1, 4):
        try:
            return operation(), attempt
        except OperationalError as error:
            code = getattr(error.orig, "pgcode", "") or ""
            if not (error.connection_invalidated or code.startswith("08") or code in {"40001", "40P01", "57P01"}) or attempt == 3:
                raise
            engine.dispose()
            time.sleep(2 ** attempt)


def _limits(mode, source, max_pages, max_listings, max_requests, delay):
    if mode not in storage.MODES or source not in {"both", *SOURCE_NAMES}:
        raise ValueError("Nieprawidłowy tryb lub źródło.")
    for value, lower, upper in ((max_pages, 1, 1000), (max_listings, 1, 50000), (max_requests, 2, 5000)):
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError("Nieprawidłowy limit pobrania.")
    if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not math.isfinite(delay) or delay < 2:
        raise ValueError("Opóźnienie musi wynosić co najmniej 2 sekundy.")


def _requests(audit):
    counts = audit.get("request_counts", {})
    if not isinstance(counts, dict) or any(type(value) is not int or value < 0 for value in counts.values()):
        raise ValueError("Nieprawidłowy licznik żądań.")
    return audit.get("requests_dispatched", sum(counts.values()))


def _validate_capture(rows, audit, name, mode, moment, pages_limit, requests_limit):
    storage.normalize_catalog(rows)
    if any(row["source"] != SOURCE_NAMES[name] or row["observed_at"] != moment for row in rows):
        raise ValueError("Niezgodne źródło lub czas obserwacji.")
    if not isinstance(audit, dict) or audit.get("source") != SOURCE_NAMES[name] or audit.get("mode") != mode:
        raise ValueError("Nieprawidłowy raport źródła.")
    if audit.get("status") not in {"ok", "partial"} or type(audit.get("completed")) is not bool or not isinstance(audit.get("checkpoint"), dict):
        raise ValueError("Nieprawidłowy postęp pobrania.")
    pages, requests = audit.get("pages_read"), _requests(audit)
    if type(pages) is not int or not 0 <= pages <= pages_limit or type(requests) is not int or not 0 <= requests <= requests_limit:
        raise ValueError("Źródło przekroczyło budżet pobrania.")
    json.dumps(audit, allow_nan=False)


def _add(target, values):
    for key, value in values.items():
        target[key] = target.get(key, 0) + value


def _phase(*, name, mode, collector, engine, owner, known, max_pages,
           max_listings, max_requests, delay, observed_at, emit):
    source = SOURCE_NAMES[name]
    state, claimed, checkpoint = None, False, None
    rows_by_id, urls, batches = {}, {}, []
    phase = {"phase": mode, "status": "ok", "completed": False,
             "database_status": "not_requested" if engine is None else "not_written",
             "catalogue_statistics": {}, "database_statistics": {}}
    error_code = None
    try:
        if engine is not None:
            previous, _ = _retry(engine, lambda: storage.read_progress(engine, source, mode))
            if mode == "bootstrap" and previous and previous["completed"]:
                phase.update(status="skipped", completed=True, database_status="already_completed",
                             audit={"pages_read": 0, "requests": 0, "checkpoint": previous["checkpoint"], "completed": True})
                return [], phase
            restart = mode == "daily" or bool(previous and previous["completed"])
            state, _ = _retry(engine, lambda: storage.acquire_progress(engine, source, mode, owner, restart=restart))
            if state is None:
                phase.update(status="leased", error="another_collection_owns_cursor",
                    audit={"pages_read": 0, "requests": 0, "checkpoint": previous["checkpoint"] if previous else {}, "completed": False})
                return [], phase
            claimed, checkpoint = True, state["checkpoint"] or None
        remaining_pages, remaining_requests = max_pages, max_requests
        # Broad traversals commit every 50 pages. The bounded daily head is one
        # capture because OLX deliberately restarts daily discovery at page one.
        while remaining_pages > 0 and remaining_requests >= 2 and len(rows_by_id) < max_listings:
            pages_limit = min(remaining_pages, 30 if mode == "daily" else 50)
            moment = _timestamp(observed_at) if observed_at is not None else datetime.now(timezone.utc)
            batch, audit = collector(mode=mode, max_pages=pages_limit,
                max_requests=remaining_requests, max_listings=max_listings - len(rows_by_id),
                delay=delay, observed_at=moment, known_ids=known, checkpoint=checkpoint)
            _validate_capture(batch, audit, name, mode, moment, pages_limit, remaining_requests)
            for row in batch:
                if row["url"] in urls and urls[row["url"]] != row["listing_id"]:
                    raise ValueError("Sprzeczne identyfikatory jednego adresu między partiami.")
            for row in batch:
                previous = rows_by_id.get(row["listing_id"])
                if previous and previous["url"] != row["url"]:
                    urls.pop(previous["url"], None)
                rows_by_id[row["listing_id"]] = row
                urls[row["url"]] = row["listing_id"]
            audit = dict(audit)
            batches.append(audit)
            if engine is not None:
                statistics, attempts = _retry(engine, lambda: storage.save_capture(
                    engine, batch, source=source, mode=mode, owner=owner,
                    generation=state["generation"], checkpoint=audit["checkpoint"], completed=audit["completed"]))
                catalogue, prices = statistics
                _add(phase["catalogue_statistics"], catalogue)
                _add(phase["database_statistics"], prices)
                phase["database_status"] = "saved"
                audit["database_write_attempts"] = attempts
            for row in batch:
                known.add(row["listing_id"])
            remaining_pages -= audit["pages_read"]
            remaining_requests -= _requests(audit)
            previous_checkpoint, checkpoint = checkpoint, audit["checkpoint"]
            phase["completed"] = audit["completed"]
            if emit:
                emit(f"{name}/{mode}: {sum(item['pages_read'] for item in batches)} stron, {len(rows_by_id)} ID; postęp {'zapisany' if engine is not None else 'w raporcie'}.")
            if audit["status"] == "partial":
                phase["status"], error_code = "partial", "source_failed"
                break
            if audit["completed"] or mode == "daily" or audit.get("termination") in {"listing_budget", "request_budget", "duration_budget", "supported_page_limit"}:
                break
            if checkpoint == previous_checkpoint or (audit["pages_read"] == 0 and _requests(audit) == 0):
                phase["status"], error_code = "partial", "source_failed"
                phase["error"] = "cursor_did_not_advance"
                break
        phase["audit"] = {
            "pages_read": sum(item["pages_read"] for item in batches),
            "requests": sum(_requests(item) for item in batches),
            "exported_listings": len(rows_by_id), "completed": phase["completed"],
            "checkpoint": checkpoint or {}, "batches": batches,
            "termination": batches[-1].get("termination") if batches else "budget_exhausted",
        }
    except SQLAlchemyError:
        phase.update(status="partial" if rows_by_id else "failed", database_status="failed_rolled_back", error="database_operation_failed")
        error_code = "database_failed"
    except Exception:
        # Portal adapters sanitize source errors. Unexpected errors are reduced
        # to a fixed code here; HTTP headers, tokens and SQL aren't audit data.
        phase.update(status="partial" if rows_by_id else "failed", error="source_read_or_validation_failed")
        error_code = "source_failed"
    finally:
        if claimed:
            try:
                _retry(engine, lambda: storage.release_progress(engine, source, mode, owner, error=error_code))
            except (SQLAlchemyError, ValueError):
                phase.update(status="partial", lease_release="failed_expires_automatically")
    if "audit" not in phase:
        phase["audit"] = {"pages_read": sum(item["pages_read"] for item in batches),
            "requests": sum(_requests(item) for item in batches), "checkpoint": checkpoint or {},
            "batches": batches, "exported_listings": len(rows_by_id), "completed": False}
    return list(rows_by_id.values()), phase


def run_collection(*, mode="daily", source="both", max_pages=1000,
                   max_listings=50000, max_requests=700, delay=2.0,
                   observed_at=None, engine=None, collectors=None, emit=None):
    _limits(mode, source, max_pages, max_listings, max_requests, delay)
    if observed_at is not None:
        _timestamp(observed_at)
    if collectors is None:
        from src.olx_catalog import collect_olx_catalog
        from src.otodom_catalog import collect_otodom_catalog
        collectors = {"olx": collect_olx_catalog, "otodom": collect_otodom_catalog}
    selected = list(SOURCE_NAMES) if source == "both" else [source]
    report = {"status": "ok", "mode": mode, "started_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Warszawa; apartment sales; advertised public search catalogue",
        "database_requested": engine is not None, "sources": {},
        "completeness": "changing pagination, source caps and missing-price partitions prevent a full market guarantee",
        "new_listing_definition": "source/ID first observed by this database; not publication date",
        "availability_inference": False, "training_performed": False,
        "cross_source_property_deduplication": False}
    all_rows, owner = {}, uuid4().hex
    for name in selected:
        entry = {"source": SOURCE_NAMES[name], "status": "ok", "phases": [],
                 "catalogue_statistics": {}, "database_statistics": {}}
        try:
            known = storage.known_ids(engine, SOURCE_NAMES[name]) if engine is not None else set()
            bootstrap = storage.read_progress(engine, SOURCE_NAMES[name], "bootstrap") if engine is not None else None
            phases = [mode]
            if mode == "daily" and engine is not None:
                phases = ["bootstrap"] if not bootstrap else ["daily", "refresh"] if bootstrap["completed"] else ["daily", "bootstrap"]
            pages_left, requests_left, listings_left = max_pages, max_requests, max_listings
            for phase_mode in phases:
                if pages_left < 1 or requests_left < 2 or listings_left < 1:
                    break
                bounded = mode == "daily" and phase_mode != "bootstrap"
                rows, phase = _phase(name=name, mode=phase_mode, collector=collectors[name],
                    engine=engine, owner=owner, known=known, max_pages=min(pages_left, 30) if bounded else pages_left,
                    max_requests=min(requests_left, 60) if bounded else requests_left,
                    max_listings=listings_left, delay=delay, observed_at=observed_at, emit=emit)
                entry["phases"].append(phase)
                pages_left -= phase["audit"]["pages_read"]
                requests_left -= phase["audit"]["requests"]
                listings_left -= len(rows)
                for row in rows:
                    all_rows[(row["source"], row["listing_id"])] = row
                _add(entry["catalogue_statistics"], phase["catalogue_statistics"])
                _add(entry["database_statistics"], phase["database_statistics"])
                if phase["status"] not in {"ok", "skipped"}:
                    entry["status"] = phase["status"]
                    break
            last = entry["phases"][-1] if entry["phases"] else None
            entry.update(executed_mode=last["phase"] if last else mode,
                completed=bool(last and last["completed"]), checkpoint=last["audit"]["checkpoint"] if last else {},
                database_status=last["database_status"] if last else "not_written",
                exported_listings=sum(row["source"] == SOURCE_NAMES[name] for row in all_rows.values()),
                pages_read=sum(phase["audit"]["pages_read"] for phase in entry["phases"]),
                requests=sum(phase["audit"]["requests"] for phase in entry["phases"]))
        except (SQLAlchemyError, ValueError):
            entry.update(status="failed", error="database_preparation_failed", completed=False, exported_listings=0)
        report["sources"][name] = entry
        if entry["status"] not in {"ok", "skipped"}:
            report["status"] = "partial"
    if all(entry["status"] == "failed" for entry in report["sources"].values()):
        report["status"] = "failed"
    report["exported_listings"] = len(all_rows)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    return list(all_rows.values()), report


def _write_outputs(rows, report, output, audit):
    """Stage the pair and restore prior files on handled replacement errors.

    An abrupt process/machine crash can still interrupt the two-file pair.
    Durable catalogue/cursor commits are independent PostgreSQL transactions.
    """
    output, audit = Path(output), Path(audit)
    if output.resolve() == audit.resolve():
        raise ValueError("CSV i raport muszą mieć różne ścieżki.")
    staged, backups, replaced, retained = [], {}, [], set()
    try:
        for path in (output, audit):
            path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
            os.close(handle)
            staged.append(Path(temporary))
        with staged[0].open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=storage.CATALOG_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: value.isoformat() if isinstance(value, datetime) else value for key, value in row.items()})
        staged[1].write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        for path in (output, audit):
            backups[path] = None
            if path.exists():
                handle, name = tempfile.mkstemp(prefix="." + path.name + ".backup.", dir=path.parent)
                os.close(handle)
                backup = Path(name)
                staged.append(backup)
                shutil.copy2(path, backup)
                backups[path] = backup
        try:
            for temporary, path in zip(staged[:2], (output, audit)):
                os.replace(temporary, path)
                replaced.append(path)
        except OSError:
            for path in reversed(replaced):
                backup = backups[path]
                try:
                    if backup is None:
                        path.unlink(missing_ok=True)
                    else:
                        os.replace(backup, path)
                except OSError:
                    if backup is not None:
                        retained.add(backup)
            raise
    finally:
        for path in staged:
            if path not in retained:
                path.unlink(missing_ok=True)


def main(argv=None):
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description="Wznawialny spis warszawskich mieszkań i aktualizacja cen.")
    parser.add_argument("--mode", choices=sorted(storage.MODES), default="daily")
    parser.add_argument("--source", choices=["both", "olx", "otodom"], default="both")
    parser.add_argument("--max-pages", type=int, default=os.environ.get("CATALOG_MAX_PAGES", "1000"))
    parser.add_argument("--max-listings", type=int, default=os.environ.get("CATALOG_MAX_LISTINGS", "50000"))
    parser.add_argument("--max-requests", type=int, default=os.environ.get("CATALOG_MAX_REQUESTS", "700"))
    parser.add_argument("--delay", type=float, default=os.environ.get("REQUEST_DELAY_SECONDS", "2"))
    parser.add_argument("--save-db", action="store_true")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "data/catalog.csv")
    parser.add_argument("--report", type=Path, default=PROJECT_ROOT / "data/catalog_audit.json")
    args = parser.parse_args(argv)
    engine = None
    try:
        _limits(args.mode, args.source, args.max_pages, args.max_listings, args.max_requests, args.delay)
        if args.output.resolve() == args.report.resolve():
            raise ValueError("CSV i raport muszą mieć różne ścieżki.")
        if args.save_db:
            engine = database.get_engine()
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            database.init_db(engine)
        rows, report = run_collection(mode=args.mode, source=args.source, max_pages=args.max_pages,
            max_listings=args.max_listings, max_requests=args.max_requests, delay=args.delay,
            engine=engine, emit=lambda message: print(message, flush=True))
        _write_outputs(rows, report, args.output, args.report)
        print(f"Wynik: {report['status']}; wyeksportowane ID: {len(rows)}. Szczegóły: {args.report}")
        return 0 if report["status"] == "ok" else 1
    except (ValueError, OSError, SQLAlchemyError):
        print("Nie udało się ukończyć pobrania. Sprawdź konfigurację i połączenie; prywatne szczegóły nie są logowane.")
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
