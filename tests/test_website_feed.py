"""Website feeds use only trusted fitted models and current synthetic rows."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock

import joblib
import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal
import pytest
from sklearn.dummy import DummyRegressor

from src import train, website_feed as feed

NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
DIGEST = "a" * 64


def listing(identifier="4", **changes):
    value = dict(source="www.olx.pl", listing_id=identifier,
        url=f"https://www.olx.pl/d/oferta/apartment-ID{identifier}.html",
        city="Warszawa", district="Mokotów", price_pln=600_000,
        area_m2=50, rooms=2, floor=2, build_year=2001,
        latitude=None, longitude=None, distance_km=None,
        observed_at=NOW - timedelta(hours=1))
    value.update(changes)
    return value


@pytest.fixture
def artifact():
    frame = pd.DataFrame([listing()])
    for column in train.NUMERIC_FEATURES:
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype(float)
    preprocessor = train.build_preprocessor()
    transformed = preprocessor.fit_transform(frame[train.FEATURES])
    model = DummyRegressor(strategy="constant", constant=1_000_000).fit(transformed, [600_000])
    return {"features": train.FEATURES.copy(), "preprocessor": preprocessor, "model": model,
        "metadata": {"trained_at_utc": (NOW - timedelta(days=4)).isoformat(),
            "capture_time": (NOW - timedelta(days=4, hours=1)).isoformat(),
            "split": {"type": "temporal", **{partition: {"listing_keys": [["www.olx.pl", identifier]]}
                for partition, identifier in (("train", "1"), ("validation", "2"), ("test", "3"))}}}}


def test_current_scoring_reuses_model_without_fit_and_excludes_train_and_validation(artifact, monkeypatch):
    catalog = pd.DataFrame([listing(str(number)) for number in range(1, 5)])
    before = catalog.copy(deep=True)
    monkeypatch.setattr(artifact["preprocessor"], "fit", Mock(side_effect=AssertionError("must not refit")))
    monkeypatch.setattr(artifact["model"], "fit", Mock(side_effect=AssertionError("must not refit")))
    monkeypatch.setattr(train, "train_model", Mock(side_effect=AssertionError("must not train")))
    result = feed.build_payload(catalog, artifact, DIGEST, now=NOW)
    assert_frame_equal(catalog, before)
    assert [offer["listing_id"] for offer in result["offers"]] == ["3", "4"]
    assert result["summary"] == dict(catalog_rows=4, scored_rows=4, candidates=2, showing=2, source_counts={"www.olx.pl": 2})
    assert result["experimental"] is True and result["schema_version"] == 1
    assert pd.Timestamp(result["capture_time"]) == pd.Timestamp(NOW)
    assert pd.Timestamp(result["model_trained_at"]) == pd.Timestamp(NOW - timedelta(days=4))
    assert result["model_sha256"] == DIGEST
    assert result["offers"][0]["model_gap_fraction"] == pytest.approx(.4)
    assert result["offers"][0]["model_gap_pln"] == pytest.approx(400_000)
    assert set(result["offers"][0]) == {"source", "listing_id", "url", "district", "price_pln", "area_m2", "rooms",
        "predicted_price_pln", "model_gap_fraction", "model_gap_pln", "observed_at"}
    assert json.loads(feed.encode_payload(result)) == result


@pytest.mark.parametrize("changes", [dict(price_pln=None), dict(observed_at=NOW - timedelta(days=8)),
    dict(observed_at=NOW + timedelta(seconds=1)), dict(area_m2=None), dict(rooms=None),
    dict(url="https://www.olx.pl/d/oferta/udzial-1-2-w-mieszkaniu-ID4.html"),
    dict(url="https://evil.example/oferta/4")])
def test_invalid_or_ineligible_newest_row_never_falls_back_to_older_valid_offer(artifact, changes):
    latest = listing(**changes)
    older = listing(observed_at=min(NOW - timedelta(days=2), latest["observed_at"] - timedelta(days=1)))
    result = feed.build_payload(pd.DataFrame([older, latest]), artifact, DIGEST, now=NOW)
    assert result["summary"]["showing"] == 0
    assert result["offers"] == []


def test_rank_order_top_50_and_gap_threshold_are_recomputed(artifact):
    catalog = pd.DataFrame([listing(str(index), price_pln=500_000 + index * 1000) for index in range(4, 70)]
        + [listing("90", price_pln=900_000)])
    result = feed.build_payload(catalog, artifact, DIGEST, now=NOW)
    assert result["summary"]["candidates"] == 66
    assert len(result["offers"]) == 50
    assert result["offers"][0]["listing_id"] == "4"
    assert result["offers"][-1]["listing_id"] == "53"
    assert all(offer["model_gap_fraction"] >= .15 for offer in result["offers"])


def test_nonportal_rows_are_scoped_out_and_string_identity_is_preserved(artifact):
    catalog = pd.DataFrame([listing("0004"), listing("9", source="developer.example")])
    result = feed.build_payload(catalog, artifact, DIGEST, now=NOW)
    assert result["summary"]["catalog_rows"] == 1
    assert result["offers"][0]["listing_id"] == "0004"


@pytest.mark.parametrize("mutation", [
    lambda a: a["metadata"].pop("split"),
    lambda a: a["metadata"]["split"]["validation"].update(listing_keys=[["www.olx.pl", "1"]]),
    lambda a: a["metadata"]["split"]["test"].update(listing_keys=[]),
    lambda a: a["metadata"]["split"].update(type="group"),
    lambda a: a["metadata"].update(trained_at_utc="2026-10-19"),
    lambda a: a.update(features=["price_per_m2"]),
    lambda a: a["metadata"]["split"]["train"].update(listing_keys=[["www.olx.pl", 1]]),
])
def test_model_with_missing_or_conflicting_provenance_is_rejected(artifact, mutation):
    mutation(artifact)
    with pytest.raises(feed.WebsiteFeedError):
        feed.build_payload(pd.DataFrame([listing()]), artifact, DIGEST, now=NOW)


def test_nan_json_and_oversized_payload_are_rejected(monkeypatch):
    with pytest.raises(feed.WebsiteFeedError):
        feed.encode_payload({"price": float("nan")})
    monkeypatch.setattr(feed, "MAX_FEED_BYTES", 3)
    with pytest.raises(feed.WebsiteFeedError):
        feed.encode_payload({"long": "text"})


class Connection:
    def __init__(self, readonly="on", isolation="repeatable read"):
        self.options, self.commands = {}, []
        self.active = False
        self.readonly, self.isolation = readonly, isolation

    def __enter__(self):
        return self

    def __exit__(self, *error):
        return False

    def execution_options(self, **options):
        self.options.update(options)
        return self

    @contextmanager
    def begin(self):
        self.active = True
        try:
            yield self
        finally:
            self.active = False

    def execute(self, statement):
        assert self.active
        query = str(statement)
        self.commands.append(query)
        values = {"SET TRANSACTION READ ONLY": None, "SHOW transaction_read_only": self.readonly,
            "SHOW transaction_isolation": self.isolation, "SELECT CURRENT_TIMESTAMP": NOW}
        assert query in values
        return SimpleNamespace(scalar_one=lambda: values[query])


def postgres(monkeypatch, readonly="on", isolation="repeatable read", rows=None):
    connection = Connection(readonly, isolation)
    engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), connect=lambda: connection, dispose=Mock())
    reads = []
    def read(query, actual, params):
        assert actual is connection and connection.active
        reads.append((str(query), params))
        return pd.DataFrame([listing()]) if rows is None else rows
    monkeypatch.setattr(feed.pd, "read_sql_query", read)
    return engine, connection, reads


def test_cloud_query_is_bounded_catalogue_only_and_confirmed_readonly(monkeypatch):
    engine, connection, queries = postgres(monkeypatch)
    catalog, capture = feed.read_catalog(engine=engine)
    assert len(catalog) == 1 and pd.Timestamp(capture) == pd.Timestamp(NOW)
    assert connection.options == {"isolation_level": "REPEATABLE READ", "postgresql_readonly": True}
    assert len(queries) == 1
    sql, params = queries[0]
    assert "FROM public.listing_catalog" in sql and "last_seen_at AS observed_at" in sql
    assert "listing_observations" not in sql
    assert params == dict(source_olx="www.olx.pl", source_otodom="www.otodom.pl", limit=50001)
    assert not connection.active
    engine.dispose.assert_not_called()


@pytest.mark.parametrize("readonly,isolation", [("off", "repeatable read"), ("on", "read committed")])
def test_unconfirmed_transaction_never_queries_catalogue(monkeypatch, readonly, isolation):
    engine, _, queries = postgres(monkeypatch, readonly, isolation)
    with pytest.raises(feed.WebsiteFeedError):
        feed.read_catalog(engine=engine)
    assert not queries


def test_cloud_limit_fails_instead_of_silently_truncating(monkeypatch):
    engine, _, _ = postgres(monkeypatch, rows=pd.DataFrame([listing(), listing("5")]))
    monkeypatch.setattr(feed, "MAX_CATALOG", 1)
    with pytest.raises(feed.WebsiteFeedError, match="safety limit"):
        feed.read_catalog(engine=engine)


def test_private_database_errors_are_sanitized():
    engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"),
        connect=Mock(side_effect=RuntimeError("postgresql://user:SECRET@private-host")))
    with pytest.raises(feed.WebsiteFeedError) as error:
        feed.read_catalog(engine=engine)
    assert "SECRET" not in str(error.value) and "private-host" not in str(error.value)


def test_offline_cli_writes_json_without_database_or_model_changes(tmp_path, artifact, monkeypatch):
    model, catalog, output = tmp_path / "model.joblib", tmp_path / "catalog.csv", tmp_path / "deals.json"
    joblib.dump(artifact, model)
    pd.DataFrame([listing(observed_at=datetime.now(timezone.utc) - timedelta(hours=1))]).to_csv(catalog, index=False)
    before = (model.read_bytes(), catalog.read_bytes())
    forbidden = Mock(side_effect=AssertionError("offline must not query database"))
    monkeypatch.setattr(feed, "read_catalog", forbidden)
    assert feed.main(["--model", str(model), "--catalog", str(catalog), "--output", str(output)]) == 0
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["summary"]["showing"] == 1
    assert result["model_sha256"] == hashlib.sha256(before[0]).hexdigest()
    assert (model.read_bytes(), catalog.read_bytes()) == before
    forbidden.assert_not_called()


@pytest.mark.parametrize("same", ["model", "catalog"])
def test_cli_refuses_output_input_collision(tmp_path, artifact, same):
    model, catalog = tmp_path / "model.joblib", tmp_path / "catalog.csv"
    joblib.dump(artifact, model)
    pd.DataFrame([listing()]).to_csv(catalog, index=False)
    before = (model.read_bytes(), catalog.read_bytes())
    assert feed.main(["--model", str(model), "--catalog", str(catalog), "--output", str(model if same == "model" else catalog)]) == 2
    assert (model.read_bytes(), catalog.read_bytes()) == before


def test_duplicate_csv_headers_rejected(tmp_path):
    path = tmp_path / "catalog.csv"
    path.write_text("source,listing_id,source\na,1,b\n", encoding="utf-8")
    with pytest.raises(feed.WebsiteFeedError, match="duplicate"):
        feed.read_csv_catalog(path)


@pytest.mark.parametrize("origin", ["http://example.com", "https://user:pass@example.com", "https://example.com/path",
    "https://example.com?token=secret", "https://example.com#secret", "https://example.com:444", "https://example.com\n"])
def test_website_only_accepts_https_origin(monkeypatch, origin):
    monkeypatch.setenv("WEBSITE_INGEST_TOKEN", "ingest-secret")
    monkeypatch.setenv("WEBSITE_SERVICE_TOKEN", "service-secret")
    with pytest.raises(feed.WebsiteFeedError):
        feed._website_request(origin, "/api/model")


def test_https_tokens_timeout_and_redirect_policy(monkeypatch):
    monkeypatch.setenv("WEBSITE_INGEST_TOKEN", "ingest-secret")
    monkeypatch.setenv("WEBSITE_SERVICE_TOKEN", "service-secret")
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.status, response.headers = 200, {}
    response.read.return_value = b'{}'
    opener = Mock()
    opener.open.return_value = response
    policies = []
    def make(*handlers):
        policies.extend(handlers)
        return opener
    monkeypatch.setattr(feed, "build_opener", make)
    feed._website_request("https://example.com/", "/api/ingest/deals", content=b'{}')
    request = opener.open.call_args.args[0]
    assert request.full_url == "https://example.com/api/ingest/deals"
    assert request.get_header("Authorization") == "Bearer ingest-secret"
    assert request.get_header("Oai-sites-authorization") == "Bearer service-secret"
    assert opener.open.call_args.kwargs["timeout"] == 30
    assert len(policies) == 1 and isinstance(policies[0], feed._NoRedirect)
    assert policies[0].redirect_request(None, None, 302, "redirect", {}, "https://evil.example") is None
    response.read.assert_called_once_with(feed.MAX_RESPONSE_BYTES + 1)


def test_download_verifies_digest_before_joblib_and_preserves_existing_file(monkeypatch, tmp_path):
    model = tmp_path / "model.joblib"
    model.write_bytes(b"old trusted model")
    monkeypatch.setattr(feed, "_website_request", lambda *a, **kw: (b"untrusted", {"X-Model-SHA256": "0" * 64}))
    forbidden = Mock(side_effect=AssertionError("must validate hash before load"))
    monkeypatch.setattr(feed.joblib, "load", forbidden)
    with pytest.raises(feed.WebsiteFeedError, match="SHA-256"):
        feed.download_model("https://example.com", model)
    forbidden.assert_not_called()
    assert model.read_bytes() == b"old trusted model"


def test_download_validates_trusted_model_then_atomically_replaces(monkeypatch, tmp_path, artifact):
    buffer = io.BytesIO()
    joblib.dump(artifact, buffer)
    content = buffer.getvalue()
    monkeypatch.setattr(feed, "_website_request", lambda *a, **kw: (content, {"X-Model-SHA256": hashlib.sha256(content).hexdigest()}))
    model = tmp_path / "model.joblib"
    feed.download_model("https://example.com", model)
    assert model.read_bytes() == content
    assert list(tmp_path.iterdir()) == [model]


def test_network_failure_never_exposes_tokens_or_remote_error(monkeypatch):
    monkeypatch.setenv("WEBSITE_INGEST_TOKEN", "ingest-secret")
    monkeypatch.setenv("WEBSITE_SERVICE_TOKEN", "service-secret")
    monkeypatch.setattr(feed, "build_opener", Mock(side_effect=RuntimeError("ingest-secret service-secret private response")))
    with pytest.raises(feed.WebsiteFeedError) as error:
        feed._website_request("https://example.com", "/api/model")
    assert all(secret not in str(error.value) for secret in ("ingest-secret", "service-secret", "private response"))


def test_publication_requires_exact_json_acknowledgment(artifact, monkeypatch):
    payload = feed.build_payload(pd.DataFrame([listing()]), artifact, DIGEST, now=NOW)
    ack = dict(ok=True, generated_at=payload["generated_at"], model_sha256=DIGEST, offers=1)
    request = Mock(return_value=(json.dumps(ack).encode("utf-8"), {}))
    monkeypatch.setattr(feed, "_website_request", request)
    feed.publish_payload(payload, "https://example.com")
    assert request.call_args.args == ("https://example.com", "/api/ingest/deals")
    assert json.loads(request.call_args.kwargs["content"]) == payload


@pytest.mark.parametrize("ack", [b"<html>Please sign in</html>", b"\xff", b"[]", b"null", b'{}',
    {"ok": False}, {"ok": 1}, {"generated_at": "2026-10-19T00:00:00Z"},
    {"model_sha256": "b" * 64}, {"offers": 0}, {"offers": True}, {"offers": 1.0}])
def test_false_or_mismatched_http_200_ack_does_not_report_publication_success(artifact, monkeypatch, ack):
    payload = feed.build_payload(pd.DataFrame([listing()]), artifact, DIGEST, now=NOW)
    valid = dict(ok=True, generated_at=payload["generated_at"], model_sha256=DIGEST, offers=1)
    if isinstance(ack, dict):
        valid.update(ack)
        raw = json.dumps(valid).encode("utf-8")
    else:
        raw = ack
    monkeypatch.setattr(feed, "_website_request", lambda *args, **kwargs: (raw, {}))
    with pytest.raises(feed.WebsiteFeedError, match="exact feed"):
        feed.publish_payload(payload, "https://example.com")
