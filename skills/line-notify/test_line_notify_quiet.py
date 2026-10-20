"""line_notify.py の夜間の抑止(request-approval・send)のテスト。時刻を差し替える。
relay・LINE・本物の承認の表・本物のキューには行かない。"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import line_notify as mod  # noqa: E402

SCHEMA = """CREATE TABLE line_approvals (code TEXT PRIMARY KEY, sha TEXT NOT NULL, target_name TEXT NOT NULL,
  user_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
  decided_at INTEGER, decision_text TEXT, used_at INTEGER)"""
GROUP = "制服のフジ情報共有"


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


@pytest.fixture()
def env(monkeypatch, tmp_path):
    sys.path.insert(0, str(mod.AGENT))
    from api import approvals  # noqa: E402

    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    clock = {"now": datetime(2026, 10, 20, 21, 0)}

    class FakeDb:
        @staticmethod
        @contextmanager
        def write_tx():
            yield conn

        @staticmethod
        @contextmanager
        def read_conn():
            yield conn

        @staticmethod
        def now_ms():
            return ms(clock["now"])

    sent: list = []
    pushed: list = []
    monkeypatch.setattr(mod, "QUEUE_DB", tmp_path / "queue.sqlite3")
    monkeypatch.setattr(mod, "LOG", tmp_path / "log.jsonl")
    monkeypatch.setattr(mod, "now_dt", lambda: clock["now"])
    monkeypatch.setattr(mod.phone_text, "TEXT_DIR", tmp_path / "phone_texts")
    monkeypatch.setattr(mod, "open_db", lambda: (approvals, FakeDb))
    monkeypatch.setattr(mod, "load_groups", lambda: {GROUP: "C" + "0" * 32, "例外": "C" + "1" * 32})
    monkeypatch.setattr(mod.lineworks, "require_watcher", lambda *a, **k: None)
    monkeypatch.setattr(mod.lineworks, "load_room", lambda *a, **k: mod.lineworks.Room("u-op", "user", "u-op"))
    monkeypatch.setattr(mod.lineworks, "send_text", lambda text, **k: sent.append(text) or 1)
    monkeypatch.setattr(mod.lineworks, "send_buttons", lambda *a, **k: sent.append("buttons"))
    monkeypatch.setattr(mod, "push_messages", lambda to, msgs: pushed.append((to, msgs)) or ["id1"])
    for name in mod.approval_channel.ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    body = tmp_path / "body.txt"
    body.write_text("◆10/29(木) 入荷予定\nG1004(パーカー) 150枚", encoding="utf-8")
    args = argparse.Namespace(group=GROUP, text_file=str(body), url="", mention="")
    return clock, conn, sent, pushed, args


def test_request_approval_at_night_queues_without_asking(env) -> None:
    clock, conn, sent, pushed, args = env
    with pytest.raises(SystemExit) as e:
        mod.cmd_request_approval(args)
    assert e.value.code == mod.EXIT["queued"]
    assert sent == [] and pushed == []  # 夜は寺下さんにも許可を求めない
    assert conn.execute("SELECT COUNT(*) FROM line_approvals").fetchone()[0] == 0
    items = mod.queue_store().items("queued")
    assert len(items) == 1 and items[0]["text"] == mod.build_text(args)


def test_request_approval_in_daytime_asks_as_before(env) -> None:
    clock, conn, sent, pushed, args = env
    clock["now"] = datetime(2026, 10, 21, 9, 0)
    mod.cmd_request_approval(args)
    assert conn.execute("SELECT COUNT(*) FROM line_approvals").fetchone()[0] == 1
    assert sent and mod.queue_store().items() == []


def test_exempt_group_is_not_held(env, monkeypatch) -> None:
    clock, conn, sent, pushed, args = env
    monkeypatch.setattr(mod.notify_queue, "load_config",
                        lambda: mod.notify_queue.Quiet(mod.notify_queue.dtime(19), mod.notify_queue.dtime(9), frozenset({"例外"})))
    args.group = "例外"
    mod.cmd_request_approval(args)
    assert conn.execute("SELECT COUNT(*) FROM line_approvals").fetchone()[0] == 1


def _approved(conn, args, decided: datetime) -> str:
    sha = mod.sha_of(args.group, mod.build_text(args))
    conn.execute("INSERT INTO line_approvals(code,sha,target_name,user_id,status,created_at,expires_at,decided_at)"
                 " VALUES ('ABC123',?,?,'lw:u-op','approved',?,?,?)",
                 (sha, args.group, ms(decided) - 600000, ms(decided) + 24 * 3600 * 1000, ms(decided)))
    return "ABC123"


def _send_args(args, code: str) -> argparse.Namespace:
    return argparse.Namespace(**vars(args), approval=code, send=True)


def test_send_after_19_with_approval_before_19_sends(env) -> None:
    clock, conn, sent, pushed, args = env
    code = _approved(conn, args, datetime(2026, 10, 20, 18, 55))
    clock["now"] = datetime(2026, 10, 20, 19, 2)
    mod.cmd_send(_send_args(args, code))
    assert len(pushed) == 1  # 19:00 前の承認なら送る(運用者決定)
    assert conn.execute("SELECT status FROM line_approvals").fetchone()[0] == "used"


def test_send_with_approval_after_19_is_held_and_sent_next_morning(env) -> None:
    clock, conn, sent, pushed, args = env
    code = _approved(conn, args, datetime(2026, 10, 20, 19, 30))
    clock["now"] = datetime(2026, 10, 20, 19, 31)
    with pytest.raises(SystemExit) as e:
        mod.cmd_send(_send_args(args, code))
    assert e.value.code == mod.EXIT["queued"] and pushed == []
    item = mod.queue_store().items("asking")[0]
    assert item["code"] == code
    # 翌朝 9:00 のまとめ送り: 承認を求め直さずに送る
    clock["now"] = datetime(2026, 10, 21, 9, 0)
    hooks = mod.flush_hooks()
    hooks.tell = lambda t: None
    hooks.now_ms = lambda: ms(clock["now"])
    mod.notify_queue.Flusher(mod.queue_store(), hooks, mod.notify_queue.load_config(), log=lambda t: None).run()
    assert len(pushed) == 1 and pushed[0][0] == "C" + "0" * 32
    assert mod.queue_store().items()[0]["status"] == "sent"
    assert "buttons" not in sent


def test_morning_flush_asks_then_sends_on_approval(env) -> None:
    clock, conn, sent, pushed, args = env
    with pytest.raises(SystemExit):
        mod.cmd_request_approval(args)  # 21:00 に積む
    clock["now"] = datetime(2026, 10, 21, 9, 0)
    hooks = mod.flush_hooks()
    hooks.tell = lambda t: None
    hooks.now_ms = lambda: ms(clock["now"])

    def sleep(sec):  # 1 回待つ間に寺下さんが承認する
        clock["now"] = clock["now"].replace(minute=clock["now"].minute + 1)
        conn.execute("UPDATE line_approvals SET status='approved', decided_at=? WHERE status='pending'", (ms(clock["now"]),))
    hooks.sleep = sleep
    mod.notify_queue.Flusher(mod.queue_store(), hooks, mod.notify_queue.load_config(), log=lambda t: None).run()
    assert "buttons" in sent and any("夜間に溜めた報告" in s for s in sent if s != "buttons")
    assert len(pushed) == 1
    assert mod.queue_store().items()[0]["status"] == "sent"


def test_queue_cancel_voids_approval(env, capsys) -> None:
    clock, conn, sent, pushed, args = env
    code = _approved(conn, args, datetime(2026, 10, 20, 20, 0))
    clock["now"] = datetime(2026, 10, 20, 20, 1)
    with pytest.raises(SystemExit):
        mod.cmd_send(_send_args(args, code))
    mod.cmd_queue_cancel(argparse.Namespace(id=1))
    assert conn.execute("SELECT status FROM line_approvals").fetchone()[0] == "rejected"
    assert mod.queue_store().items()[0]["status"] == "cancelled"
