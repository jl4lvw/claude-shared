"""sound_notify.py (対応待ちの音声通知フック) と lw_phone_notify.py のテスト。

なぜ必要か (2026-10-10):
  - AskUserQuestion は PreToolUse / PermissionRequest / Notification の 3 経路で発火する。
    どれも「選択肢」と判定し、権限待ちを二重に鳴らさないことを固定する (実機で二重通知が出た)
  - Stop は質問・依頼で終わったときだけ鳴らす (通常の完了報告で鳴ると使い物にならない)
  - 読み上げ文は PowerShell に埋め込むので、波括弧を str.format に通すと KeyError で
    無言で落ちる (実機で発生)。replace で差し込めていることを固定する
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hooks"))

import lw_phone_notify  # noqa: E402
import sound_notify as sn  # noqa: E402


def ev(name: str, **kw: object) -> dict:
    return {"hook_event_name": name, **kw}


@pytest.mark.parametrize(
    "event, expected",
    [
        (ev("PermissionRequest", tool_name="Bash"), sn.PERMISSION),
        (ev("PermissionRequest", tool_name="AskUserQuestion"), sn.QUESTION),
        (ev("PreToolUse", tool_name="AskUserQuestion"), sn.QUESTION),
        (ev("Notification", notification_type="permission_prompt",
            message="Claude needs your permission to use AskUserQuestion"), sn.QUESTION),
        (ev("Notification", notification_type="permission_prompt",
            message="Claude needs your permission to use Bash"), sn.PERMISSION),
        (ev("Notification", notification_type="idle_prompt"), "入力待ちです"),
        (ev("Stop", last_assistant_message="どれにしますか？"), sn.REPLY),
    ],
)
def test_classify_text(event: dict, expected: str) -> None:
    hit = sn.classify(event)
    assert hit is not None and hit[1] == expected


@pytest.mark.parametrize(
    "event",
    [
        ev("PreToolUse", tool_name="Bash"),
        ev("Notification", notification_type="auth_success"),
        ev("Stop", last_assistant_message="修正しました。テストも通っています。"),
        ev("SessionStart"),
    ],
)
def test_classify_ignored(event: dict) -> None:
    assert sn.classify(event) is None


def test_every_text_has_english_speech() -> None:
    texts = {sn.PERMISSION, sn.QUESTION, sn.REPLY, "入力待ちです", "確認待ちです"}
    assert texts <= set(sn.SPEECH_EN)


def test_speak_template_survives_braces() -> None:
    """PS_SPEAK は波括弧を含む。format を使うと KeyError になる退行を防ぐ。"""
    out = sn.PS_SPEAK.replace("__TEXT__", sn.SPEECH_EN[sn.PERMISSION])
    assert "Permission needed" in out and "__TEXT__" not in out


def test_debounce_same_kind_and_question_then_permission(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sn, "STAMP", tmp_path / "stamp.json")
    assert sn.debounced(sn.QUESTION) is False
    assert sn.debounced(sn.QUESTION) is True      # 同じ種類の連続は捨てる
    assert sn.debounced(sn.PERMISSION) is True    # 選択肢の直後の権限通知は同じ待ち
    assert sn.debounced(sn.REPLY) is False        # 別の種類は鳴らす


def test_debounce_permission_alone_passes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sn, "STAMP", tmp_path / "stamp.json")
    assert sn.debounced(sn.PERMISSION) is False


def test_session_title_from_board_and_fallback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sn, "BOARD_DIR", tmp_path)
    (tmp_path / "abc.json").write_text(json.dumps({"title": "★テスト 1"}), encoding="utf-8")
    assert sn.session_title({"session_id": "abc"}) == "★テスト 1"
    assert sn.session_title({"session_id": "zzzzzzzzzz"}) == "(セッション zzzzzzzz)"


def test_phone_skipped_without_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sn, "PHONE_CONFIG", tmp_path / "none.json")
    called: list[object] = []
    monkeypatch.setattr(sn.subprocess, "Popen", lambda *a, **k: called.append(a))
    sn.phone(sn.PERMISSION, {"session_id": "x"})
    assert called == []


def test_phone_kind_drops_desu(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "p.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sn, "PHONE_CONFIG", cfg)
    monkeypatch.delenv("CLAUDE_PHONE_NOTIFY", raising=False)
    seen: list[list[str]] = []
    monkeypatch.setattr(sn.subprocess, "Popen", lambda args, **k: seen.append(args))
    sn.phone(sn.QUESTION, {"session_id": "x"})
    assert seen and seen[0][-2] == "選択肢の確認待ち"


def test_phone_too_soon_per_kind(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(lw_phone_notify, "STAMP", tmp_path / "last.json")
    assert lw_phone_notify.too_soon("権限の確認待ち") is False
    assert lw_phone_notify.too_soon("権限の確認待ち") is True
    assert lw_phone_notify.too_soon("返信待ち") is False


def test_phone_send_without_room_id_does_nothing(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "phone_notify.json"
    cfg.write_text(json.dumps({"room_id": ""}), encoding="utf-8")
    monkeypatch.setattr(lw_phone_notify, "CONFIG", cfg)
    monkeypatch.setattr(lw_phone_notify, "load_env", lambda: pytest.fail("送信まで進んではいけない"))
    lw_phone_notify.send("権限の確認待ち", "t")
