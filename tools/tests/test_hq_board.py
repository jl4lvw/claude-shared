"""HQ 状態ボード (hq_board.py フック + hq_board_cli.py) のテスト。

なぜ必要か (2026-09-04):
  司令塔 (/hq) はこのボードだけを見て「どのセッションが何で止まっているか」を
  判断する。状態遷移が 1 つ狂うと「止まっていないのに通知する」「止まっているのに
  見えない」のどちらかになり、見張り番が狼少年になる。フックは exit 0 で例外を
  飲む設計なので、テストが無いと壊れても気付けない。

実行方法:
    python -m pytest .claude/tools/tests/test_hq_board.py -v
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / ".claude" / "hooks"))
sys.path.insert(0, str(_ROOT / ".claude" / "tools"))

import hq_board
import hq_board_cli as cli

SID = "11111111-aaaa-4bbb-8ccc-000000000001"
SID2 = "22222222-aaaa-4bbb-8ccc-000000000002"


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    hq = tmp_path / "hq"
    monkeypatch.setattr(hq_board, "HQ_DIR", hq)
    monkeypatch.setattr(hq_board, "BOARD_DIR", hq / "board")
    monkeypatch.setattr(cli, "HQ_DIR", hq)
    monkeypatch.setattr(cli, "BOARD_DIR", hq / "board")
    monkeypatch.setattr(cli, "WATCH_STATE", hq / "watch_state.json")
    monkeypatch.setenv("CLAUDE_HQ_NO_PUSH", "1")  # テストから中継APIへ送らない
    return hq / "board"


def _state(board: Path, sid: str = SID) -> dict:
    return json.loads((board / f"{sid}.json").read_text(encoding="utf-8"))


def _ev(name: str, sid: str = SID, **extra: object) -> dict:
    return {"hook_event_name": name, "session_id": sid, "cwd": r"C:\ClaudeCode\900.ClaudeCode", **extra}


def _transcript(tmp_path: Path, title: str, prompt: str, assistant_text: str) -> Path:
    p = tmp_path / f"{SID}.jsonl"
    lines = [
        {"type": "custom-title", "customTitle": title, "sessionId": SID},
        {"type": "user", "message": {"role": "user", "content": prompt}},
        {"type": "last-prompt", "lastPrompt": prompt, "sessionId": SID},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": "秘密の思考"}, {"type": "text", "text": assistant_text}],
            },
        },
        {"type": "attachment", "attachment": {}},
    ]
    p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------- フック側


def test_start_prompt_tool_permission_cycle(board: Path) -> None:
    hq_board.handle(_ev("SessionStart", reason="startup"))
    assert _state(board)["status"] == "idle"

    hq_board.handle(_ev("UserPromptSubmit", user_prompt="在庫を確認して password=abc12345 も見て"))
    st = _state(board)
    assert st["status"] == "running"
    assert "abc12345" not in st["last_prompt"]  # 伏字化
    assert st["last_prompt"].startswith("在庫を確認して")

    # 実環境のキーは "prompt" (2026-09-04 実測)。こちらも受ける
    hq_board.handle(_ev("UserPromptSubmit", prompt="/hq"))
    assert _state(board)["last_prompt"] == "/hq"

    hq_board.handle(_ev("PreToolUse", tool_name="Bash", tool_input={"command": "git push"}, tool_use_id="t1"))
    st = _state(board)
    assert st["status"] == "running"
    assert st["pending_tool"]["name"] == "Bash"
    assert st["pending_tool"]["detail"] == "git push"

    hq_board.handle(_ev("PermissionRequest", tool_name="Bash", tool_input={"command": "git push"}, tool_use_id="t1"))
    st = _state(board)
    assert st["status"] == "waiting_permission"
    waiting_since = st["since"]

    # 同じ状態の再通知では since を動かさない (見張り番の差分判定の前提)
    hq_board.handle(_ev("Notification", notification_type="permission_prompt", notification_text="needs approval"))
    st = _state(board)
    assert st["status"] == "waiting_permission"
    assert st["since"] == waiting_since

    hq_board.handle(_ev("PostToolUse", tool_name="Bash", tool_input={"command": "git push"}, tool_use_id="t1"))
    st = _state(board)
    assert st["status"] == "running"
    assert st["pending_tool"] is None
    assert st["events"]["PermissionRequest"] == 1


def test_ask_user_question_sets_waiting_question(board: Path) -> None:
    hq_board.handle(_ev("UserPromptSubmit", user_prompt="発注して"))
    hq_board.handle(
        _ev(
            "PreToolUse",
            tool_name="AskUserQuestion",
            tool_input={"questions": [{"question": "数量はどれにしますか?", "options": []}]},
            tool_use_id="q1",
        )
    )
    st = _state(board)
    assert st["status"] == "waiting_question"
    assert "数量はどれ" in st["question"]

    # デスクトップアプリでは選択肢ダイアログも PermissionRequest + permission_prompt を経由する (実測)。
    # それでも「質問待ち」のまま保つ
    hq_board.handle(_ev("PermissionRequest", tool_name="AskUserQuestion", tool_input={"questions": []}, tool_use_id="q1"))
    hq_board.handle(_ev("Notification", notification_type="permission_prompt"))
    st = _state(board)
    assert st["status"] == "waiting_question"
    assert "数量はどれ" in st["question"]

    hq_board.handle(_ev("PostToolUse", tool_name="AskUserQuestion", tool_input={}, tool_use_id="q1"))
    st = _state(board)
    assert st["status"] == "running"
    assert st["question"] is None


def test_stop_question_vs_done(board: Path) -> None:
    hq_board.handle(_ev("UserPromptSubmit", user_prompt="x"))
    hq_board.handle(
        _ev("Stop", last_assistant_message="数量は次のどれにしますか。\n1. 25枚\n2. 50枚\n番号で教えてください。")
    )
    st = _state(board)
    assert st["status"] == "waiting_answer"
    assert st["last_assistant_source"] == "payload"
    assert "番号で教えてください" in st["last_assistant_tail"]

    hq_board.handle(_ev("UserPromptSubmit", user_prompt="1"))
    hq_board.handle(_ev("Stop", last_assistant_message="push しました。", tool_calls_in_turn=[{"tool_name": "Bash"}]))
    st = _state(board)
    assert st["status"] == "done"
    assert st["tool_calls_last_turn"] == 1

    hq_board.handle(_ev("SessionEnd", reason="other"))
    assert _state(board)["status"] == "ended"


def test_stop_falls_back_to_transcript(board: Path, tmp_path: Path) -> None:
    tx = _transcript(tmp_path, "■外注手配", "刺繍依頼書を作って", "納期はいつにしますか？")
    hq_board.handle(_ev("SessionStart", transcript_path=str(tx)))
    st = _state(board)
    assert st["title"] == "■外注手配"
    assert st["last_prompt"].startswith("刺繍依頼書")

    hq_board.handle(_ev("Stop", transcript_path=str(tx)))  # last_assistant_message 無し
    st = _state(board)
    assert st["status"] == "waiting_answer"
    assert st["last_assistant_source"] == "transcript"
    assert st["last_assistant"] == "納期はいつにしますか？"
    assert "秘密の思考" not in json.dumps(st, ensure_ascii=False)  # thinking は拾わない
    assert "_tx_last_assistant" not in st


def test_tail_read_handles_large_transcript(board: Path, tmp_path: Path) -> None:
    tx = _transcript(tmp_path, "大きい", "p", "最後の応答です。")
    filler = json.dumps({"type": "attachment", "attachment": {"x": "y" * 4000}}) + "\n"
    with open(tx, "r+", encoding="utf-8") as f:
        head = f.read()
        f.seek(0)
        f.write(filler * 200 + head)  # 先頭に ~800KB の雑音
    hq_board.handle(_ev("Stop", transcript_path=str(tx)))
    st = _state(board)
    assert st["title"] == "大きい"
    assert st["last_assistant"] == "最後の応答です。"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("番号で教えてください。", True),
        ("どちらにしますか？", True),
        ("Which option do you prefer?", True),
        ("候補です。\n1. A案\n2. B案\n", True),
        ("完了しました。", False),
        ("push しました。ログは以下です。", False),
    ],
)
def test_looks_like_question(text: str, expected: bool) -> None:
    assert hq_board._looks_like_question(text) is expected


def test_bad_payloads_never_raise(board: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hq_board.handle({"hook_event_name": "Stop"})  # session 無し → ログのみ
    assert not (board / "None.json").exists()
    for raw in (b"", b"not json", b"[1,2]"):
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw)))
        assert hq_board.main() == 0


# ---------------------------------------------------------------- CLI 側


OLD_CWD = r"C:\Users\x\OneDrive\デスクトップ\900.ClaudeCode"


def _seed(board: Path) -> None:
    hq_board.handle(_ev("UserPromptSubmit", user_prompt="A の作業"))
    hq_board.handle(_ev("PermissionRequest", tool_name="Bash", tool_input={"command": "rm -rf build"}, tool_use_id="t"))
    hq_board.handle(_ev("UserPromptSubmit", sid=SID2, user_prompt="B の作業") | {"cwd": OLD_CWD})
    hq_board.handle(_ev("Stop", sid=SID2, last_assistant_message="完了しました。") | {"cwd": OLD_CWD})


def test_load_board_sorts_waiting_first_and_flags_old_path(board: Path) -> None:
    _seed(board)
    rows = cli.load_board(None, include_all=False)
    assert [r["status"] for r in rows] == ["waiting_permission", "done"]
    assert rows[0]["n"] == 1
    assert rows[0]["send_id"] == f"local_{SID}"
    assert rows[1]["old_path"] is True
    assert rows[1]["title"] == "B の作業"  # 無題なら依頼文を仮名にする

    assert [r["session_id"] for r in cli.load_board(SID, include_all=False)] == [SID2]

    hq_board.handle(_ev("SessionEnd", sid=SID2, reason="other"))
    assert len(cli.load_board(None, include_all=False)) == 1
    assert len(cli.load_board(None, include_all=True)) == 2


def _run(argv: list[str]) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert cli.main(argv) == 0
    return buf.getvalue()


def test_show_renders_table_and_details(board: Path) -> None:
    _seed(board)
    out = _run(["show"])
    assert "| # | PC | 状態 | 経過 |" in out
    assert "🔐許可待ち" in out
    assert "rm -rf build" in out
    assert "⚠旧パス" in out
    assert "次の一手" in out


def test_diff_new_then_nochange_then_resolved(board: Path) -> None:
    _seed(board)
    out = _run(["diff", "--commit"])
    assert out.startswith("NEW  [1] 🔐許可待ち")
    assert json.loads(out.strip().splitlines()[-1])["new"] == [SID]

    assert _run(["diff", "--commit"]).strip() == "NOCHANGE"

    hq_board.handle(_ev("PostToolUse", tool_name="Bash", tool_input={}, tool_use_id="t"))
    out = _run(["diff", "--commit"])
    assert "OK   [" in out
    assert json.loads(out.strip().splitlines()[-1])["resolved"] == [SID]
    assert _run(["diff", "--commit"]).strip() == "NOCHANGE"


def test_diff_renotifies_after_interval(board: Path) -> None:
    _seed(board)
    _run(["diff", "--commit"])
    assert _run(["diff", "--commit", "--renotify-min", "30"]).strip() == "NOCHANGE"
    out = _run(["diff", "--commit", "--renotify-min", "0"])
    assert out.startswith("STILL [1]")


def test_resolve_send_ids_by_id_title_or_unknown(board: Path, tmp_path: Path) -> None:
    """CCD の sessionId とフックの session_id が一致しないセッションはタイトルで突き合わせる (実測に基づく)。"""
    _seed(board)
    hq_board.handle(_ev("UserPromptSubmit", sid="33333333-aaaa-4bbb-8ccc-000000000003", prompt="C"))
    (board / "33333333-aaaa-4bbb-8ccc-000000000003.json").write_text(
        json.dumps({**_state(board, "33333333-aaaa-4bbb-8ccc-000000000003"), "title": "■外注手配"}, ensure_ascii=False),
        encoding="utf-8",
    )
    sessions = [
        {"sessionId": f"local_{SID}", "title": "A の作業 (CCD)", "cwd": "C:\\x", "isRunning": True, "lastActivityAt": "t"},
        {"sessionId": "local_ffffffff-0000-4000-8000-000000000000", "title": "■外注手配", "cwd": "C:\\x"},
        {"sessionId": "local_eeeeeeee-0000-4000-8000-000000000000", "title": "未計測のやつ", "cwd": "C:\\OneDrive\\y"},
    ]
    sf = tmp_path / "sessions.json"
    sf.write_text(json.dumps(sessions, ensure_ascii=False), encoding="utf-8")

    rows = cli.load_board(None, include_all=False)
    untracked = cli.resolve_send_ids(rows, cli.load_sessions(str(sf)))
    by_sid = {r["session_id"]: r for r in rows}
    assert by_sid[SID]["send_id"] == f"local_{SID}" and by_sid[SID]["send_how"] == "id"
    assert by_sid["33333333-aaaa-4bbb-8ccc-000000000003"]["send_id"].startswith("local_ffffffff")
    assert by_sid["33333333-aaaa-4bbb-8ccc-000000000003"]["send_how"] == "title"
    assert by_sid[SID2]["send_id"] is None and by_sid[SID2]["send_how"] == "未特定"
    assert [s["title"] for s in untracked] == ["未計測のやつ"]

    out = _run(["show", "--sessions", str(sf)])
    assert "未計測" in out and "未計測のやつ" in out and "⚠旧パス" in out
    assert "send_id local_" + SID + " (id)" in out


def test_push_records_are_not_sessions(board: Path) -> None:
    """hq_push が board/ に置く <sid>.push.json を状態ファイルと誤認しない (実障害: 幽霊の💤待機行が出た)。"""
    _seed(board)
    (board / f"{SID}.push.json").write_text(json.dumps({"at": "x", "ok": True, "code": 200}), encoding="utf-8")
    rows = cli.load_board(None, include_all=True)
    assert [r["session_id"] for r in rows] == sorted([SID, SID2], key=lambda s: [r["session_id"] for r in rows].index(s))
    assert len(rows) == 2


def test_prune_removes_old_files(board: Path) -> None:
    _seed(board)
    (board / "old.json").write_text(json.dumps({"session_id": "old", "updated_at": "2020-01-01T00:00:00+09:00"}), encoding="utf-8")
    out = _run(["prune", "--days", "7"])
    assert "pruned 1 file" in out
    assert not (board / "old.json").exists()
    assert (board / f"{SID}.json").exists()
