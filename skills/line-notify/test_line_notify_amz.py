"""line_notify send-approved(amz_inbound の承認済みの申請から来た文だけを送る入口)の試験。
LINE・090・見張り役には行かない(表はすべて一時の SQLite、送信は偽物)。"""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import line_notify as ln  # noqa: E402

sys.path.insert(0, str(HERE.parents[2] / "900.ClaudeCode"))
from a_browser_common import approval_gate  # noqa: E402

FOLDER = "https://drive.google.com/drive/folders/1AbC-def_234"
FBA = "FBA15GKDQBQ0"
TEMPLATE = "セブンエス様\n納品番号: {fba}\nラベル:\n{folder_url}"
TEXT = f"セブンエス様\n納品番号: {FBA}\nラベル:\n{FOLDER}"
RID, CODE = 9, "K7Q2AB"


def payload():
    p = {"purpose": "7S 発送の FBA 納品を送るため", "expect": {"job_id": "abc123def456", "ship_from": "sevens"},
         "line": {"group": "セブンエス", "template": TEMPLATE}}
    p["sha"] = ln.amz_payload_sha(p)
    return p


@pytest.fixture
def tables(tmp_path, monkeypatch):
    q, w, a = tmp_path / "queue.sqlite3", tmp_path / "watch.sqlite3", tmp_path / "agent.db"
    text = json.dumps(payload(), ensure_ascii=False)
    sha = approval_gate.sha("amz_inbound", RID, text)
    with sqlite3.connect(q) as c:
        c.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY, site, idem_key, status, payload, result, created_at, updated_at)")
        c.execute("INSERT INTO requests VALUES (?, 'amz_inbound', 'job:x', 'running', ?, NULL, '', '')", (RID, text))
    with sqlite3.connect(w) as c:
        c.execute("CREATE TABLE approvals (target, request_id, sha, code, status, deadline, created_at, updated_at, note)")
        c.execute("INSERT INTO approvals VALUES ('amz_inbound', ?, ?, ?, 'launching', 0, '', '', '')", (RID, sha, CODE))
    with sqlite3.connect(a) as c:
        c.execute("CREATE TABLE line_approvals (code, sha, target_name, status, decided_at, used_at)")
        noon = int(datetime(2026, 10, 30, 12, 0).timestamp() * 1000)
        c.execute("INSERT INTO line_approvals VALUES (?, ?, ?, 'used', ?, ?)",
                  (CODE, sha, approval_gate.target_name("amz_inbound", RID), noon, noon))
    monkeypatch.setattr(ln, "now_dt", lambda: datetime(2026, 10, 30, 12, 5))
    monkeypatch.setattr(ln, "LOG", tmp_path / "log.jsonl")
    sent = []
    kw = dict(queue_db=q, watch_db=w, agent_db=a, used=ln.AmzUsed(tmp_path / "used.sqlite3"),
              groups={"セブンエス": "C" + "x" * 32}, push=lambda gid, text: sent.append(text) or ["m1"])
    return kw, sent, (q, w, a)


def test_sends_once_when_everything_matches(tables):
    kw, sent, _ = tables
    out = ln.send_approved(RID, CODE, TEXT, FBA, FOLDER, **kw)
    assert out["status"] == "sent" and out["ids"] == ["m1"] and sent == [TEXT]
    again = ln.send_approved(RID, CODE, TEXT, FBA, FOLDER, **kw)
    assert again["status"] == "rejected" and "すでに使いました" in again["problem"] and len(sent) == 1


def test_sha_matches_amz_inbound_cli():
    sys.path.insert(0, str(HERE.parents[2] / "900.ClaudeCode"))
    from amz_inbound import cli

    p = payload()
    assert cli.payload_sha(p) == p["sha"]


@pytest.mark.parametrize("change,word", [
    ("text", "一字一句"), ("fba", "納品番号の形"), ("folder", "URL の形"), ("code", "組がありません"),
    ("watch_status", "見張り役"), ("agent_status", "使われていません"), ("agent_target", "line_approvals"),
    ("payload", "ハッシュ"), ("queue_status", "実行中ではありません"),
])
def test_refuses_on_any_mismatch(tables, change, word):
    kw, sent, (q, w, a) = tables
    text, fba, folder, code = TEXT, FBA, FOLDER, CODE
    if change == "text":
        text = TEXT + "!"
    elif change == "fba":
        fba = "FBA1"
    elif change == "folder":
        folder = "https://drive.google.com/drive/folders/x?y"
    elif change == "code":
        code = "ZZZZZZ"
    elif change == "watch_status":
        with sqlite3.connect(w) as c:
            c.execute("UPDATE approvals SET status='pending'")
    elif change == "agent_status":
        with sqlite3.connect(a) as c:
            c.execute("UPDATE line_approvals SET status='approved'")
    elif change == "agent_target":
        with sqlite3.connect(a) as c:
            c.execute("UPDATE line_approvals SET target_name='A自動操作:amz_inbound#10'")
    elif change == "payload":
        p = payload()
        p["line"]["template"] += "追加"
        with sqlite3.connect(q) as c:
            c.execute("UPDATE requests SET payload=?", (json.dumps(p, ensure_ascii=False),))
    elif change == "queue_status":
        with sqlite3.connect(q) as c:
            c.execute("UPDATE requests SET status='done'")
    out = ln.send_approved(RID, code, text, fba, folder, **kw)
    assert out["status"] == "rejected" and word in out["problem"] and sent == []


def test_night_queues_without_code(tables, monkeypatch, tmp_path):
    kw, sent, (_, _, a) = tables
    with sqlite3.connect(a) as c:  # 19:30 に承認された
        c.execute("UPDATE line_approvals SET decided_at=?", (int(datetime(2026, 10, 30, 19, 30).timestamp() * 1000),))
    monkeypatch.setattr(ln, "now_dt", lambda: datetime(2026, 10, 30, 19, 35))
    store = ln.notify_queue.Store(tmp_path / "nq.sqlite3", lambda: datetime(2026, 10, 30, 19, 35))
    out = ln.send_approved(RID, CODE, TEXT, FBA, FOLDER, store=store, **kw)
    assert out["status"] == "queued" and sent == []
    item = store.items()[0]
    assert item["status"] == "queued" and item["code"] is None and item["text"] == TEXT and item["sha"] == out["sha"]
    assert ln.send_approved(RID, CODE, TEXT, FBA, FOLDER, store=store, **kw)["status"] == "rejected"


def test_before_19_approval_still_sends_after_19(tables, monkeypatch):
    kw, sent, _ = tables
    monkeypatch.setattr(ln, "now_dt", lambda: datetime(2026, 10, 30, 19, 5))  # 承認は 12:00
    assert ln.send_approved(RID, CODE, TEXT, FBA, FOLDER, **kw)["status"] == "sent"


def test_http_error_releases_but_unknown_keeps_mark(tables):
    kw, sent, _ = tables

    def http_error(gid, text):
        raise SystemExit("LINE への送信に失敗: HTTP 500")
    out = ln.send_approved(RID, CODE, TEXT, FBA, FOLDER, **{**kw, "push": http_error})
    assert out["status"] == "failed"

    def cut(gid, text):
        raise TimeoutError("timed out")
    assert ln.send_approved(RID, CODE, TEXT, FBA, FOLDER, **{**kw, "push": cut})["status"] == "unknown"
    assert ln.send_approved(RID, CODE, TEXT, FBA, FOLDER, **kw)["status"] == "rejected"  # 届いたか分からない分は送り直さない
    assert sent == []
