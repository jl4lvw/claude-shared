"""conphas_search の取得間隔と 429 の取り直しの試験(本番 API には届かない)."""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from datetime import date
from email.message import Message
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conphas_search as cs  # noqa: E402


def _http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(cs.URL, code, "err", headers, io.BytesIO(b"too many"))


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ok(rows: list[dict]) -> _Resp:
    return _Resp(json.dumps({"rows": rows}).encode())


def _opener(responses: list):
    calls: list = []

    def urlopen(req, timeout):
        calls.append(req)
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    return urlopen, calls


def test_default_30_days_is_one_request_without_wait():
    sleeps: list[float] = []
    fetched: list = []

    def fake_fetch(key, mode, s, e, customer):
        fetched.append((mode, s, e))
        return [{"SNA": "制帽"}]

    hits, total = cs.collect("k", ["purchase"], date(2026, 9, 27), date(2026, 10, 27), "", [],
                             fetch_fn=fake_fetch, sleep=sleeps.append)
    assert len(fetched) == 1 and sleeps == [] and total == 1 and len(hits) == 1


def test_long_range_waits_between_every_request():
    sleeps: list[float] = []
    fetched: list = []

    def fake_fetch(key, mode, s, e, customer):
        fetched.append((mode, s, e))
        return []

    # 約1年半を both で: 7分割 × 2 = 14 回
    cs.collect("k", ["purchase", "sales"], date(2025, 4, 1), date(2026, 10, 1), "", [],
               fetch_fn=fake_fetch, sleep=sleeps.append)
    assert len(fetched) == 14
    assert sleeps == [cs.REQUEST_INTERVAL_SEC] * 13
    assert cs.REQUEST_INTERVAL_SEC * 10 > 60  # 11回目は最初から60秒より後になる(60秒に10回まで)


def test_429_retries_with_retry_after():
    sleeps: list[float] = []
    urlopen, calls = _opener([_http_error(429, "7"), _ok([{"SNA": "a"}])])
    rows = cs.fetch("k", "purchase", date(2026, 1, 1), date(2026, 1, 2), "", urlopen=urlopen, sleep=sleeps.append)
    assert rows == [{"SNA": "a"}] and sleeps == [7.0] and len(calls) == 2


def test_429_gives_up_after_max_retries():
    sleeps: list[float] = []
    urlopen, calls = _opener([_http_error(429) for _ in range(cs.MAX_RETRIES + 1)])
    with pytest.raises(SystemExit, match="429"):
        cs.fetch("k", "purchase", date(2026, 1, 1), date(2026, 1, 2), "", urlopen=urlopen, sleep=sleeps.append)
    assert len(calls) == cs.MAX_RETRIES + 1
    assert sleeps == [cs.RETRY_DEFAULT_SEC] * cs.MAX_RETRIES


def test_other_http_error_is_not_retried():
    sleeps: list[float] = []
    urlopen, calls = _opener([_http_error(500)])
    with pytest.raises(SystemExit, match="500"):
        cs.fetch("k", "purchase", date(2026, 1, 1), date(2026, 1, 2), "", urlopen=urlopen, sleep=sleeps.append)
    assert len(calls) == 1 and sleeps == []


@pytest.mark.parametrize("value,expected", [
    (None, cs.RETRY_DEFAULT_SEC),
    ("12", 12.0),
    ("Wed, 21 Oct 2026 07:28:00 GMT", cs.RETRY_DEFAULT_SEC),
    ("-5", cs.RETRY_DEFAULT_SEC),
    ("nan", cs.RETRY_DEFAULT_SEC),
    ("9999", cs.RETRY_MAX_SEC),
])
def test_retry_after_seconds(value, expected):
    assert cs.retry_after_seconds(value) == expected
