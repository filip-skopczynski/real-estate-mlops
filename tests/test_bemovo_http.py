"""Network failure behaviour using fake responses, without live requests."""
from collections import deque
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from src.fetch_bemovo import BemovoError, MAX_BYTES, _read_text, collect_bemovo


def response(status=200, content=b"ok", headers=None):
    return SimpleNamespace(status_code=status, content=content, headers=headers or {})


class Session:
    def __init__(self, outcomes):
        self.outcomes = deque(outcomes)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.outcomes.popleft()


@pytest.mark.parametrize("status", [403, 429])
def test_access_or_rate_limit_stops_without_retries(status):
    session = Session([response(status)])
    with pytest.raises(BemovoError, match=str(status)):
        _read_text(session, "https://bemovo.pl/pl/", delay=0, sleep=lambda _: None)
    assert len(session.calls) == 1


def test_foreign_redirect_is_not_followed():
    session = Session([response(302, headers={"Location": "https://untrusted.example/data"})])
    with pytest.raises(BemovoError, match="unexpected address"):
        _read_text(session, "https://bemovo.pl/pl/", delay=0, sleep=lambda _: None)
    assert len(session.calls) == 1


def test_server_errors_are_bounded_and_success_can_recover():
    session = Session([response(503), response(503), response(content=b"\xef\xbb\xbftest")])
    assert _read_text(session, "https://api.dane.gov.pl/1.4/datasets/39940", delay=0, sleep=lambda _: None) == "test"
    assert len(session.calls) == 3
    assert all(call[1]["impersonate"] == "chrome120" for call in session.calls)
    session = Session([response(503)] * 3)
    with pytest.raises(BemovoError, match="repeatedly"):
        _read_text(session, "https://bemovo.pl/pl/", delay=0, sleep=lambda _: None)
    assert len(session.calls) == 3


def test_source_response_is_size_limited():
    session = Session([response(content=b"x" * (MAX_BYTES + 1))])
    with pytest.raises(BemovoError, match="size limit"):
        _read_text(session, "https://bemovo.pl/pl/", delay=0, sleep=lambda _: None)


def test_stale_price_file_is_rejected_before_reading_csv_or_website():
    dataset = {"data": {"id": "39940", "attributes": {"license_name": "CC0 1.0"}}}
    resources = {"data": [{"id": "123", "attributes": {"data_date": "2026-10-04"}}]}
    session = Session([response(content=json.dumps(dataset).encode()), response(content=json.dumps(resources).encode())])
    with pytest.raises(BemovoError, match="current Warsaw day"):
        collect_bemovo(session=session, delay=0, observed_at=datetime(2026, 10, 5, 8, tzinfo=timezone.utc))
    assert len(session.calls) == 2
