"""返答が英語なら差し戻す Stop フック(.claude/hooks/japanese_guard.py)のテスト。例文は 2026-10-20 に実際に書いた文。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hooks"))
import japanese_guard as g  # noqa: E402

ENGLISH = [
    "I'm calling now.",
    "Next, change the caller side (phone_call) to pass the approval list in a file.",
    "The watcher's own check last reached the phone at **20:23**. So something happened on the phone side after that.",
    "I have all the text gathered. Here's the Japanese draft.\n\n| When | Current |\n|---|---|\n| 最初 | 端末エーからの承認のお願いです。 |",
    "Opening the 集荷依頼 service (ご利用中のサービス) — navigation only, nothing is submitted.",
]
JAPANESE = [
    "電話はつながり、英語の案内は 2 回とも最後まで流れました。",
    "main.py の startup と invoice.py の `create_invoice_files` も見ます。",
    "CSS に sp-sales / sp-diff-line を追加します。",
    "試験一式が通りました(`a_browser_watch/tests` 110 件・toms_order 112 件)。",
    "| 場面 | 今（英語） | 日本語案 |\n|---|---|---|\n| 最初 | Approval request from terminal A. | 端末エーからの承認のお願いです。 |",
    "コードはまだ変えていません。\n\n```python\nfor item in items:\n    print(item)  # this is the code\n```",
    "OK",
    "",
]


@pytest.mark.parametrize("text", ENGLISH)
def test_english_is_caught(text):
    assert g.is_english(text)


@pytest.mark.parametrize("text", JAPANESE)
def test_japanese_passes(text):
    assert not g.is_english(text)


def write_transcript(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    return path


def asst(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def user(text: str) -> dict:
    return {"type": "user", "message": {"content": [{"type": "text", "text": text}]}}


def tool_result() -> dict:
    return {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}}


def test_turn_texts_stop_at_the_operator_message(tmp_path):
    tp = write_transcript(tmp_path / "t.jsonl", [
        asst("前のターンの文。This was in the previous turn, so it is not counted."),
        user("実装して"), asst("作ります。"), tool_result(), asst("試験が通りました。")])
    assert g.turn_texts(tp) == ["作ります。", "試験が通りました。"]


def test_english_final_is_blocked_once(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    out = g.judge({"last_assistant_message": ENGLISH[2]})
    assert out["decision"] == "block" and "日本語で書き直して" in out["reason"]
    assert g.judge({"last_assistant_message": ENGLISH[2], "stop_hook_active": True}) is None  # 2 回目は通す
    log = (tmp_path / ".claude" / "logs" / "japanese_guard.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(log) == 2 and json.loads(log[1])["retry"] is True


def test_english_midway_text_is_blocked(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    tp = write_transcript(tmp_path / "t.jsonl", [user("試して"), asst(ENGLISH[1]), tool_result(), asst("終わりました。")])
    out = g.judge({"last_assistant_message": "終わりました。", "transcript_path": str(tp)})
    assert out["decision"] == "block" and "途中で英語" in out["reason"]


def test_japanese_turn_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    tp = write_transcript(tmp_path / "t.jsonl", [user("試して"), asst(JAPANESE[1]), tool_result(), asst(JAPANESE[0])])
    assert g.judge({"last_assistant_message": JAPANESE[0], "transcript_path": str(tp)}) is None
    assert not (tmp_path / ".claude" / "logs" / "japanese_guard.jsonl").exists()


def test_broken_input_never_blocks(tmp_path):
    assert g.judge({"last_assistant_message": None, "transcript_path": str(tmp_path / "none.jsonl")}) is None
