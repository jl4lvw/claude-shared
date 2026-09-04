"""HQ ↔ 中継API(041) 連携 (hq_relay.py / hq_push.py / hq_board の push 判定 / CLI --remote) のテスト。

なぜ必要か (2026-09-04):
  別PC (A-desktop) のセッションを同じ状況板に載せるため、フックが中継APIへ状態を送る。
  送信は「切り離した別プロセス」「状態変化時だけ」「サーバーが hq 未対応なら hq 無しで再送」
  という取り決めで成り立っており、どれかが崩れると (a) フックが遅くなる (b) 送りすぎる
  (c) 422 で永久に登録されない、のいずれかになる。HTTP は request_json を差し替えて模擬する。

実行方法:
    python -m pytest .claude/tools/tests/test_hq_relay.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / ".claude" / "hooks"))
sys.path.insert(0, str(_ROOT / ".claude" / "tools"))

import hq_board
import hq_board_cli as cli
import hq_push
import hq_relay

SID = "11111111-aaaa-4bbb-8ccc-000000000001"
SETTINGS = {"base_url": "https://relay.example", "api_key": "k", "user_id": "TK", "pc_label": "TK-desktop"}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    hq = tmp_path / "hq"
    for mod in (hq_board, cli):
        monkeypatch.setattr(mod, "HQ_DIR", hq)
        monkeypatch.setattr(mod, "BOARD_DIR", hq / "board")
    monkeypatch.setattr(cli, "WATCH_STATE", hq / "watch_state.json")
    monkeypatch.setattr(hq_push, "BOARD_DIR", hq / "board")
    monkeypatch.setattr(hq_relay, "HQ_DIR", hq)
    monkeypatch.setattr(hq_relay, "HQ_REJECTED_MARK", hq / "hq_rejected.json")
    monkeypatch.setattr(hq_relay, "load_settings", lambda: dict(SETTINGS))
    monkeypatch.delenv("CLAUDE_HQ_NO_PUSH", raising=False)
    return hq / "board"


class FakeApi:
    """request_json の代役。呼び出しを記録し、あらかじめ決めた応答を返す。"""

    def __init__(self, responses: list[tuple[int, object]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, method, path, payload=None, params=None, settings=None, timeout=8):
        self.calls.append((method, path, payload))
        return self.responses.pop(0) if self.responses else (200, {})


def _state(**over: object) -> dict:
    base = {
        "session_id": SID,
        "status": "waiting_answer",
        "reason": "応答が質問で終了",
        "since": "2026-09-04T22:00:00+09:00",
        "title": "■外注手配",
        "last_prompt": "発注して",
        "last_assistant_tail": "番号で教えてください。",
        "cwd": r"C:\ClaudeCode\900.ClaudeCode",
        "pending_tool": {"name": "Bash", "detail": "git push", "tool_use_id": "t", "at": "x"},
        "events": {"Stop": 3},
    }
    base.update(over)
    return base


# ---------------------------------------------------------------- hq_relay


def test_push_state_sends_hq_and_marks_accepted(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeApi([(200, {"session_id": SID, "hq": {"status": "waiting_answer"}})])
    monkeypatch.setattr(hq_relay, "request_json", api)
    res = hq_relay.push_state(_state())
    assert res["ok"] and res["hq_accepted"] is True
    method, path, body = api.calls[0]
    assert (method, path) == ("PUT", f"/claude-sessions/{SID}")
    assert body["pc_label"] == "TK-desktop"
    assert body["hq"]["status"] == "waiting_answer"
    assert body["hq"]["pending_tool"] == {"name": "Bash", "detail": "git push", "at": "x"}
    assert "tool_use_id" not in json.dumps(body)
    assert "events" not in body["hq"]  # 内部カウンタは送らない
    assert not hq_relay.HQ_REJECTED_MARK.exists()


def test_push_state_silently_dropped_hq_is_reported_not_marked(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-09-04 時点の実サーバー挙動: 未知項目 hq は 200 で黙って捨てられる。拒否印は付けない (対応後すぐ乗るように)。"""
    api = FakeApi([(200, {"session_id": SID})])
    monkeypatch.setattr(hq_relay, "request_json", api)
    res = hq_relay.push_state(_state())
    assert res["ok"] and res["hq_accepted"] is False
    assert not hq_relay.HQ_REJECTED_MARK.exists()


def test_push_state_falls_back_without_hq_on_422(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeApi([(422, {"detail": "extra"}), (200, {})])
    monkeypatch.setattr(hq_relay, "request_json", api)
    res = hq_relay.push_state(_state())
    assert res["ok"] and res["hq_accepted"] is False
    assert "hq" in api.calls[0][2] and "hq" not in api.calls[1][2]
    assert hq_relay.HQ_REJECTED_MARK.exists()

    # 直後の送信は hq を付けない (毎回 422 を踏まない)
    api2 = FakeApi([(200, {})])
    monkeypatch.setattr(hq_relay, "request_json", api2)
    hq_relay.push_state(_state())
    assert "hq" not in api2.calls[0][2]


def test_push_state_end_posts_end(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeApi([(200, {})])
    monkeypatch.setattr(hq_relay, "request_json", api)
    res = hq_relay.push_state(_state(end_reason="other"), end=True)
    assert res["ok"] and res["end"]
    assert api.calls[0][:2] == ("POST", f"/claude-sessions/{SID}/end")
    assert api.calls[0][2] == {"pc_label": "TK-desktop", "reason": "other"}


def test_push_state_unconfigured_is_noop(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hq_relay, "load_settings", lambda: {"base_url": "", "api_key": "", "user_id": "", "pc_label": ""})
    api = FakeApi([])
    monkeypatch.setattr(hq_relay, "request_json", api)
    assert hq_relay.push_state(_state())["ok"] is False
    assert api.calls == []


def test_request_json_never_raises_on_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    code, body = hq_relay.request_json("GET", "/x", settings={"base_url": "http://127.0.0.1:9", "api_key": "k"}, timeout=0.5)
    assert code == 0 and "Error" in body


# ---------------------------------------------------------------- hq_push


def test_push_session_writes_record(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env.mkdir(parents=True)
    (env / f"{SID}.json").write_text(json.dumps(_state()), encoding="utf-8")
    api = FakeApi([(200, {})])
    monkeypatch.setattr(hq_relay, "request_json", api)
    res = hq_push.push_session(SID, board_dir=env)
    assert res["ok"]
    rec = json.loads((env / f"{SID}.push.json").read_text(encoding="utf-8"))
    assert rec["ok"] is True and rec["code"] == 200 and rec["hq_accepted"] is False  # 応答に hq 無し


def test_push_session_missing_state_is_soft(env: Path) -> None:
    assert hq_push.push_session("nope", board_dir=env)["ok"] is False


# ---------------------------------------------------------------- hq_board の push 判定


def _ev(name: str, **extra: object) -> dict:
    return {"hook_event_name": name, "session_id": SID, "cwd": "C:\\x", **extra}


def test_hook_spawns_push_only_on_state_change_or_heartbeat(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spawned: list[tuple[str, bool]] = []
    monkeypatch.setattr(hq_board, "_spawn_push", lambda sid, end: spawned.append((sid, end)))

    hq_board.handle(_ev("SessionStart"))  # ハートビート相当 → 送る
    hq_board.handle(_ev("UserPromptSubmit", prompt="x"))  # idle→running → 送る
    hq_board.handle(_ev("PreToolUse", tool_name="Bash", tool_input={"command": "ls"}, tool_use_id="1"))  # 変化なし
    hq_board.handle(_ev("PostToolUse", tool_name="Bash", tool_input={}, tool_use_id="1"))  # 変化なし
    hq_board.handle(_ev("PermissionRequest", tool_name="Bash", tool_input={"command": "rm"}, tool_use_id="2"))  # → 送る
    hq_board.handle(_ev("Stop", last_assistant_message="done."))  # → 送る
    hq_board.handle(_ev("SessionEnd", reason="other"))  # → end
    assert spawned == [(SID, False), (SID, False), (SID, False), (SID, False), (SID, True)]


def test_hook_push_can_be_disabled_by_env(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spawned: list = []
    monkeypatch.setattr(hq_board, "_spawn_push", lambda sid, end: spawned.append(sid))
    monkeypatch.setenv("CLAUDE_HQ_NO_PUSH", "1")
    hq_board.handle(_ev("Stop", last_assistant_message="x"))
    assert spawned == []


# ---------------------------------------------------------------- CLI --remote


def test_remote_rows_merge_and_skip_own_pc(env: Path) -> None:
    remote = [
        {"session_id": "aaaa1111-0000-4000-8000-000000000000", "pc_label": "A-desktop", "cwd": "C:\\ClaudeCode\\013.CONPHAS-PWA",
         "status": "active", "stale": False, "last_heartbeat_at": "2026-09-04T13:00:00+00:00"},
        {"session_id": "bbbb2222-0000-4000-8000-000000000000", "pc_label": "A-desktop", "cwd": "C:\\ClaudeCode",
         "status": "active", "stale": True, "last_heartbeat_at": "2026-09-04T10:00:00+00:00"},
        {"session_id": "cccc3333-0000-4000-8000-000000000000", "pc_label": "A-desktop", "cwd": "C:\\ClaudeCode",
         "status": "active", "stale": False, "last_heartbeat_at": "2026-09-04T13:00:00+00:00",
         "hq": {"status": "waiting_answer", "title": "■SGW経理", "since": "2026-09-04T21:50:00+09:00",
                "last_assistant_tail": "どちらにしますか？", "reason": "応答が質問で終了"}},
        {"session_id": "dddd4444-0000-4000-8000-000000000000", "pc_label": "TK-desktop", "cwd": "C:\\x", "status": "active", "stale": False,
         "last_heartbeat_at": "2026-09-04T13:00:00+00:00"},
        {"session_id": "eeee5555-0000-4000-8000-000000000000", "pc_label": "A-desktop", "cwd": "C:\\x", "status": "ended", "stale": False,
         "last_heartbeat_at": "2026-09-04T13:00:00+00:00"},
    ]
    rrows, hidden = cli.remote_rows(remote, "TK-desktop", include_all=False)
    rows = cli.sort_rows(rrows)
    assert [r["short"] for r in rows] == ["cccc3333", "aaaa1111"]  # 待ち → 稼働中。自PC・終了は除外、古い応答なしは畳む
    assert hidden == {"A-desktop": 1}
    assert rows[0]["waiting"] and rows[0]["title"] == "■SGW経理" and rows[0]["send_id"] is None and rows[0]["remote"]
    assert rows[1]["status"] == "remote_active" and rows[1]["title"] == "013.CONPHAS-PWA"
    assert all(r["pc"] == "A-desktop" for r in rows)

    rrows_all, hidden_all = cli.remote_rows(remote, "TK-desktop", include_all=True)
    assert hidden_all == {} and {r["short"] for r in rrows_all} >= {"bbbb2222", "eeee5555"}
