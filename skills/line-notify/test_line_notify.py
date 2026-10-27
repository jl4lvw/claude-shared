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


# ---------------------------------------------------------------- wait-approval の差し戻し(2026-10-27。承認の表・LINE には触れない)
def _wait(monkeypatch: pytest.MonkeyPatch, capsys, status: str, text: str | None) -> tuple[int, str]:
    import argparse
    import time
    row = {"status": status, "expires_at": int(time.time() * 1000) + 60000, "decision_text": text}
    monkeypatch.setattr(ln, "read_approval", lambda code: row)
    with pytest.raises(SystemExit) as e:
        ln.cmd_wait(argparse.Namespace(code="ABC234", timeout_min=0, poll=0))
    return e.value.code, capsys.readouterr().out


def test_wait_shows_the_return_reason(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    rc, out = _wait(monkeypatch, capsys, "revise", ln.RETURN_PREFIX + "画像の切り抜きを直して")
    assert rc == 11 and "差し戻されました" in out
    assert "--- 差し戻しの理由 ---\n画像の切り抜きを直して" in out and ln.RETURN_PREFIX not in out


def test_wait_keeps_the_lineworks_revise(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    rc, out = _wait(monkeypatch, capsys, "revise", "ABC234 数を2に")
    assert rc == 11 and "修正の依頼です" in out and "寺下さんの返信" in out and "数を2に" in out


def test_return_prefix_matches_the_watcher() -> None:
    sys.path.insert(0, r"C:\ClaudeCode\900.ClaudeCode")
    from a_browser_watch import approval_pwa
    assert ln.RETURN_PREFIX == approval_pwa.RETURN_PREFIX
