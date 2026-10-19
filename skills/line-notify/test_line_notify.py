"""line_notify.py のメンション部分のテスト(送信・DB には触れない)。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import line_notify as ln  # noqa: E402


def test_sha_without_mention_is_unchanged() -> None:
    # 既存の承認(メンションなし)と同じ値になること(後方互換)
    import hashlib
    expected = hashlib.sha256("g\nbody".encode("utf-8")).hexdigest()[:8]
    assert ln.sha_of("g", "body") == expected


def test_sha_changes_with_mention_target() -> None:
    assert ln.sha_of("g", "body", "U1") != ln.sha_of("g", "body")
    assert ln.sha_of("g", "body", "U1") != ln.sha_of("g", "body", "U2")


def test_text_message_plain() -> None:
    assert ln.text_message("abc") == {"type": "text", "text": "abc"}


def test_text_message_mention_escapes_braces() -> None:
    m = ln.text_message("a{b}c", "U1")
    assert m["type"] == "textV2"
    assert m["text"] == "{m} さん\na{{b}}c"
    assert m["substitution"]["m"]["mentionee"] == {"type": "user", "userId": "U1"}


def test_resolve_mention(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "roster.json").write_text(
        json.dumps({"U1": {"displayName": "山田"}, "U2": {"displayName": "佐藤"}, "U3": {"displayName": "佐藤"}},
                   ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(ln, "AGENT", tmp_path)
    assert ln.resolve_mention("") == ("", "")
    assert ln.resolve_mention("山田") == ("U1", "山田")
    with pytest.raises(SystemExit):
        ln.resolve_mention("佐藤")  # 同名が2人
    with pytest.raises(SystemExit):
        ln.resolve_mention("鈴木")  # いない
