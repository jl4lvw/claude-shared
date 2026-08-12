"""deepseek_coder の「予算切れで本文が空」→ 増枠リトライのテスト.

INC-20260812-1117419055b1: 26KB 差分の reviewer 役で reasoning が 111,701 文字に達し、
32,000 tok の出力予算を使い切って `finish_reason=length` / 本文が空になった。
同じ差分でも JSON 出力を要求する pv review モードは完走しており、
**自由記述の reviewer 役だけが暴発する**。事前に読めない挙動なので、
呼び出し側が --max-tokens を付け直すのではなく、その場で 1 回だけ増枠して取り直す。

API を叩かずに検証したいので、client を差し替えて応答を作る。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import deepseek_coder as D  # noqa: E402


def _response(content: str, finish: str):
    message = types.SimpleNamespace(content=content, reasoning_content="x" * 100)
    choice = types.SimpleNamespace(message=message, finish_reason=finish)
    return types.SimpleNamespace(choices=[choice], usage=None)


class _FakeClient:
    """create() が呼ばれた予算を記録し、用意した応答を順に返す。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.budgets: list[int] = []
        self.chat = types.SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.budgets.append(kwargs["max_tokens"])
        return self._responses.pop(0)


@pytest.fixture()
def patched(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dummy")
    holder: dict = {}

    def factory(responses):
        client = _FakeClient(responses)
        holder["client"] = client
        monkeypatch.setattr(D, "OpenAI", lambda **kw: client)
        monkeypatch.setattr(D, "_track_usage", lambda *a, **k: None)
        return client

    return factory


def test_empty_body_due_to_length_is_retried_with_a_bigger_budget(patched) -> None:
    client = patched([_response("", "length"), _response("本文が返った", "stop")])
    out = D.call_deepseek("差分をレビューして", role="reviewer", track=False)
    assert out == "本文が返った"
    assert client.budgets == [D.DEFAULT_MAX_TOKENS, D.API_MAX_TOKENS]


def test_retry_happens_only_once(patched) -> None:
    """増枠しても空なら**諦めて落ちる**。無限に金を使わない。"""
    client = patched([_response("", "length"), _response("", "length")])
    with pytest.raises(SystemExit) as exc:
        D.call_deepseek("差分をレビューして", role="reviewer", track=False)
    assert exc.value.code != 0
    assert client.budgets == [D.DEFAULT_MAX_TOKENS, D.API_MAX_TOKENS]


def test_no_retry_when_body_is_present(patched) -> None:
    """打ち切られていても本文があるなら取り直さない (二重課金しない)。"""
    client = patched([_response("途中まで書けた指摘", "length")])
    out = D.call_deepseek("差分をレビューして", role="reviewer", track=False)
    assert out == "途中まで書けた指摘"
    assert client.budgets == [D.DEFAULT_MAX_TOKENS]


def test_no_retry_for_other_finish_reasons(patched) -> None:
    """空でも length 以外 (content_filter 等) は増枠しても直らない。"""
    client = patched([_response("", "content_filter")])
    with pytest.raises(SystemExit):
        D.call_deepseek("差分をレビューして", role="reviewer", track=False)
    assert client.budgets == [D.DEFAULT_MAX_TOKENS]


def test_no_retry_when_already_at_api_max(patched) -> None:
    """呼び出し側が既に上限を指定しているなら、増枠の余地はない。"""
    client = patched([_response("", "length")])
    with pytest.raises(SystemExit):
        D.call_deepseek("差分", role="reviewer", track=False, max_tokens=D.API_MAX_TOKENS)
    assert client.budgets == [D.API_MAX_TOKENS]


def test_api_max_is_above_default() -> None:
    assert D.API_MAX_TOKENS > D.DEFAULT_MAX_TOKENS
