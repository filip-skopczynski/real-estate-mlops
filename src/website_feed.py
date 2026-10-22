"""Score the current catalogue with an existing trusted model for the website.

This opt-in command never trains a model, requests portal pages, initializes
tables or writes to PostgreSQL. Only its JSON output and optional authenticated
website publication are written. Joblib files must come from this project's
trusted local artifacts or its authenticated private model endpoint.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from dotenv import load_dotenv
import joblib
import pandas as pd

from src.build_deals import CATALOG_FIELDS, MAX_CATALOG, PROJECT_ROOT, SOURCES, prepare_latest
from src.show_deals import _identity, _utc, select_deals
from src.train import FEATURES, score_listings

MAX_MODEL_BYTES = 10 * 1024 * 1024
MAX_FEED_BYTES = 512 * 1024
MAX_RESPONSE_BYTES = 4096
MAX_CSV_BYTES = 32 * 1024 * 1024
NETWORK_TIMEOUT = 30
WEBSITE_PROVIDERS = ("sites", "cloudflare")


class WebsiteFeedError(ValueError):
    """Public, sanitized configuration or feed-generation error."""


def read_catalog(*, engine=None):
    """Read at most 50,001 current catalogue rows in one read-only snapshot."""
    own_engine = engine is None
    try:
        if engine is None:
            from src.database import get_engine
            engine = get_engine()
        if engine.dialect.name != "postgresql":
            raise WebsiteFeedError("Cloud website feeds require PostgreSQL; use --catalog for an offline CSV.")
        from sqlalchemy import text
        with engine.connect() as connection:
            connection = connection.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
            with connection.begin():
                connection.execute(text("SET TRANSACTION READ ONLY"))
                readonly = connection.execute(text("SHOW transaction_read_only")).scalar_one()
                isolation = connection.execute(text("SHOW transaction_isolation")).scalar_one()
                if str(readonly).casefold() != "on" or str(isolation).casefold() != "repeatable read":
                    raise WebsiteFeedError("Database could not confirm a read-only repeatable-read transaction.")
                capture = _utc(connection.execute(text("SELECT CURRENT_TIMESTAMP")).scalar_one())
                if capture is None:
                    raise WebsiteFeedError("Database returned an invalid snapshot time.")
                fields = ["last_seen_at AS observed_at" if field == "observed_at" else field for field in CATALOG_FIELDS]
                catalog = pd.read_sql_query(text(
                    "SELECT " + ", ".join(fields) + " FROM public.listing_catalog "
                    "WHERE source IN (:source_olx, :source_otodom) "
                    "ORDER BY source, listing_id LIMIT :limit"
                ), connection, params={"source_olx": SOURCES[0], "source_otodom": SOURCES[1], "limit": MAX_CATALOG + 1})
                if len(catalog) > MAX_CATALOG:
                    raise WebsiteFeedError("Catalogue exceeds the 50000-row safety limit.")
        return catalog, capture
    except WebsiteFeedError:
        raise
    except Exception:
        raise WebsiteFeedError("Could not read the current catalogue. Check database access; private details are not logged.") from None
    finally:
        if own_engine and engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass


def _validate_artifact(artifact):
    if not isinstance(artifact, dict) or artifact.get("features") != FEATURES:
        raise WebsiteFeedError("The model does not match this project's feature schema.")
    if not callable(getattr(artifact.get("preprocessor"), "transform", None)) or not callable(getattr(artifact.get("model"), "predict", None)):
        raise WebsiteFeedError("The trusted model artifact is incomplete.")
    metadata = artifact.get("metadata")
    if not isinstance(metadata, dict) or _utc(metadata.get("trained_at_utc")) is None:
        raise WebsiteFeedError("The model is missing a valid training timestamp.")
    if _utc(metadata.get("capture_time")) is None:
        raise WebsiteFeedError("The model is missing its training snapshot timestamp.")
    provenance = metadata.get("split")
    if not isinstance(provenance, dict) or provenance.get("type") != "temporal":
        raise WebsiteFeedError("The model needs explicit temporal evaluation provenance.")
    seen = set()
    for partition in ("train", "validation", "test"):
        summary = provenance.get(partition)
        keys = summary.get("listing_keys") if isinstance(summary, dict) else None
        if not isinstance(keys, list) or not keys:
            raise WebsiteFeedError("The model is missing evaluation identity provenance.")
        for key in keys:
            if not isinstance(key, (list, tuple)) or len(key) != 2:
                raise WebsiteFeedError("The model contains invalid evaluation identities.")
            identity = _identity(*key)
            if identity is None or tuple(key) != identity or identity in seen:
                raise WebsiteFeedError("The model contains invalid or overlapping evaluation identities.")
            seen.add(identity)
    return metadata


def load_model(path):
    """Load only a bounded, trusted project artifact; no arbitrary URL loading."""
    try:
        with Path(path).open("rb") as source:
            content = source.read(MAX_MODEL_BYTES + 1)
        if not content or len(content) > MAX_MODEL_BYTES:
            raise WebsiteFeedError("The trusted model is empty or exceeds the 10 MiB limit.")
        artifact = joblib.load(io.BytesIO(content))
        _validate_artifact(artifact)
        return artifact, hashlib.sha256(content).hexdigest()
    except WebsiteFeedError:
        raise
    except Exception:
        raise WebsiteFeedError("Could not load the trusted fitted model. Train or restore the project model first.") from None


def build_payload(catalog, artifact, model_sha256, *, now=None):
    """Score current rows without fit/refit and publish only test/unseen IDs."""
    metadata = _validate_artifact(artifact)
    capture = _utc(datetime.now(timezone.utc) if now is None else now)
    if capture is None or not isinstance(model_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", model_sha256):
        raise WebsiteFeedError("A valid snapshot timestamp and model SHA-256 are required.")
    if not isinstance(catalog, pd.DataFrame) or not catalog.columns.is_unique or "source" not in catalog:
        raise WebsiteFeedError("Catalogue must have unique column names and a source column.")
    if len(catalog) > MAX_CATALOG:
        raise WebsiteFeedError("Catalogue exceeds the 50000-row safety limit.")
    scoped = catalog.loc[catalog.source.astype("string").str.strip().str.casefold().isin(SOURCES)].copy(deep=True)
    latest, _ = prepare_latest(scoped, now=capture, max_age_days=7)
    scored = score_listings(latest, artifact, discount_threshold=.15)
    candidate_input = scored.loc[scored.partition.isin(["test", "unseen"])].copy(deep=True)
    deals, report = select_deals(candidate_input, top=50, max_age_days=7, discount_threshold=.15, now=capture)
    forbidden = {tuple(key) for partition in ("train", "validation") for key in metadata["split"][partition]["listing_keys"]}
    offers = []
    for row in deals.itertuples(index=False):
        if (str(row.source), str(row.listing_id)) in forbidden:
            raise WebsiteFeedError("Candidate selection contained a training or validation identity.")
        district = row.district if isinstance(row.district, str) else None
        if district is not None:
            district = " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", district).split())[:120] or None
        offers.append({
            "source": str(row.source), "listing_id": str(row.listing_id), "url": str(row.url),
            "district": district, "price_pln": float(row.price_pln), "area_m2": float(row.area_m2),
            "rooms": int(row.rooms), "predicted_price_pln": float(row.predicted_price_pln),
            "model_gap_fraction": float(row.discount), "model_gap_pln": float(row.model_gap_pln),
            "observed_at": _utc(row.observed_at).isoformat(),
        })
    payload = {
        "schema_version": 1, "generated_at": capture.isoformat(), "capture_time": capture.isoformat(),
        "model_trained_at": _utc(metadata["trained_at_utc"]).isoformat(),
        "model_capture_time": _utc(metadata["capture_time"]).isoformat(),
        "model_sha256": model_sha256, "experimental": True,
        "scope": {"city": "Warszawa", "transaction": "sale", "sources": list(SOURCES),
                  "max_age_days": 7, "threshold_fraction": .15},
        "summary": {"catalog_rows": len(scoped), "scored_rows": len(scored),
                    "candidates": report["eligible_listings"], "showing": len(offers),
                    "source_counts": {source: int(count) for source, count in sorted(Counter(offer["source"] for offer in offers).items())}},
        "offers": offers,
    }
    encode_payload(payload)
    return payload


def encode_payload(payload):
    try:
        content = (json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
    except (TypeError, ValueError):
        raise WebsiteFeedError("Website feed contains invalid JSON values.") from None
    if len(content) > MAX_FEED_BYTES:
        raise WebsiteFeedError("Website feed exceeds its size safety limit.")
    return content


def _atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".website-feed-", delete=False) as target:
            temporary = Path(target.name)
            target.write(content)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_csv_catalog(path):
    try:
        with Path(path).open("rb") as source:
            content = source.read(MAX_CSV_BYTES + 1)
        if len(content) > MAX_CSV_BYTES:
            raise WebsiteFeedError("Catalogue CSV exceeds the 32 MiB safety limit.")
        decoded = content.decode("utf-8-sig")
        headers = next(csv.reader(io.StringIO(decoded)), [])
        if not headers or len(headers) != len(set(headers)):
            raise WebsiteFeedError("Catalogue CSV has empty or duplicate headers.")
        catalog = pd.read_csv(io.StringIO(decoded), dtype={"source": "string", "listing_id": "string"}, nrows=MAX_CATALOG + 1)
        if len(catalog) > MAX_CATALOG:
            raise WebsiteFeedError("Catalogue exceeds the 50000-row safety limit.")
        return catalog
    except WebsiteFeedError:
        raise
    except Exception:
        raise WebsiteFeedError("Could not read the offline catalogue CSV.") from None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


def _website_request(origin, endpoint, *, content=None, limit=MAX_RESPONSE_BYTES, provider="sites"):
    """Authenticated HTTPS request; tokens never follow a redirect or enter logs."""
    try:
        if provider not in WEBSITE_PROVIDERS:
            raise WebsiteFeedError("Website provider must be sites or cloudflare.")
        parsed = urlsplit(origin)
        if (not isinstance(origin, str) or re.search(r"[\x00-\x20\x7f-\x9f\\]", origin)
                or parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise WebsiteFeedError("--publish-url must be a plain HTTPS website origin without a path or credentials.")
        ingest_token = os.getenv("WEBSITE_INGEST_TOKEN", "")
        if not ingest_token or re.search(r"[\x00-\x20\x7f-\x9f]", ingest_token):
            raise WebsiteFeedError("Set a valid WEBSITE_INGEST_TOKEN secret before website requests.")
        headers = {"Authorization": "Bearer " + ingest_token,
                   "Accept": "application/json" if content is not None else "application/octet-stream"}
        # A Sites service token authorizes only Sites hosting. Never read or send
        # that credential when publishing to the independently hosted Worker.
        if provider == "sites":
            service_token = os.getenv("WEBSITE_SERVICE_TOKEN", "")
            if not service_token or re.search(r"[\x00-\x20\x7f-\x9f]", service_token):
                raise WebsiteFeedError("Set a valid WEBSITE_SERVICE_TOKEN secret before Sites requests.")
            headers["OAI-Sites-Authorization"] = "Bearer " + service_token
        if content is not None:
            headers["Content-Type"] = "application/json"
        request = Request(origin.rstrip("/") + endpoint, data=content, headers=headers, method="POST" if content is not None else "GET")
        with build_opener(_NoRedirect()).open(request, timeout=NETWORK_TIMEOUT) as response:
            if response.status != 200:
                raise WebsiteFeedError("Website endpoint did not accept the request.")
            raw = response.read(limit + 1)
            if len(raw) > limit:
                raise WebsiteFeedError("Website response exceeded its size safety limit.")
            return raw, response.headers
    except WebsiteFeedError:
        raise
    except Exception:
        raise WebsiteFeedError("Website request failed. Check the HTTPS address, service access and ingestion secrets; private details are not logged.") from None


def download_model(origin, path, *, provider="sites"):
    content, headers = _website_request(origin, "/api/model", limit=MAX_MODEL_BYTES, provider=provider)
    expected = headers.get("X-Model-SHA256", "")
    actual = hashlib.sha256(content).hexdigest()
    if not content or not re.fullmatch(r"[a-f0-9]{64}", expected) or expected != actual:
        raise WebsiteFeedError("The authenticated model download failed its SHA-256 integrity check.")
    # Check integrity before joblib deserialization. This endpoint must contain
    # only the trusted model uploaded by the project owner, never user uploads.
    try:
        _validate_artifact(joblib.load(io.BytesIO(content)))
    except WebsiteFeedError:
        raise
    except Exception:
        raise WebsiteFeedError("The authenticated model artifact could not be loaded.") from None
    _atomic_write(path, content)


def publish_payload(payload, origin, *, provider="sites"):
    raw, _ = _website_request(origin, "/api/ingest/deals", content=encode_payload(payload), provider=provider)
    try:
        acknowledgment = json.loads(raw.decode("utf-8"))
        if (not isinstance(acknowledgment, dict) or acknowledgment.get("ok") is not True
                or acknowledgment.get("generated_at") != payload["generated_at"]
                or acknowledgment.get("model_sha256") != payload["model_sha256"]
                or type(acknowledgment.get("offers")) is not int
                or acknowledgment["offers"] != len(payload["offers"])):
            raise ValueError("Unexpected acknowledgement")
    except (UnicodeDecodeError, ValueError, KeyError, TypeError):
        raise WebsiteFeedError("The website did not confirm publication of this exact feed.") from None


def main(argv=None):
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description="Refresh the experimental website feed using an existing fitted model; no training.")
    parser.add_argument("--catalog", type=Path, help="Offline current catalogue CSV instead of cloud PostgreSQL.")
    parser.add_argument("--model", type=Path, default=PROJECT_ROOT / "models/model.joblib")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "data/website/deals.json")
    parser.add_argument("--publish-url", help="HTTPS website origin to receive the authenticated feed.")
    parser.add_argument("--provider", choices=WEBSITE_PROVIDERS, default="sites",
                        help="Website hosting provider; Cloudflare requires only the ingestion secret (default: sites).")
    parser.add_argument("--download-model", action="store_true", help="Restore the trusted model from the authenticated website endpoint.")
    args = parser.parse_args(argv)
    try:
        if args.output.resolve() == args.model.resolve() or (args.catalog is not None and args.output.resolve() == args.catalog.resolve()):
            raise WebsiteFeedError("Website output must not overwrite its catalogue or fitted model input.")
        if args.download_model:
            if not args.publish_url:
                raise WebsiteFeedError("--download-model requires --publish-url.")
            if args.catalog is not None and args.model.resolve() == args.catalog.resolve():
                raise WebsiteFeedError("Downloaded model must not overwrite the catalogue input.")
            download_model(args.publish_url, args.model, provider=args.provider)
        artifact, digest = load_model(args.model)
        if args.catalog is not None:
            catalog, capture = read_csv_catalog(args.catalog), datetime.now(timezone.utc)
        else:
            catalog, capture = read_catalog()
        payload = build_payload(catalog, artifact, digest, now=capture)
        _atomic_write(args.output, encode_payload(payload))
        if args.publish_url:
            publish_payload(payload, args.publish_url, provider=args.provider)
    except (WebsiteFeedError, ValueError, OSError):
        print("Website feed could not be refreshed. Check model provenance, catalogue input, database access and website configuration; private details are not logged.")
        return 2
    print(f"Website feed refreshed: {payload['summary']['showing']} experimental candidates; model reused without retraining.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
