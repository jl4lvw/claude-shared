"""通知ルールの同期 (notify_sync.py) と、sound_notify からの起動条件のテスト。

固定したいこと (2026-10-17):
  - 取得: 変更があればサニタイズしてキャッシュに書く。変更なし/失敗/不正な応答ではキャッシュを壊さない
  - 送信: 未送信の判定だけを 50 件まで送り、送れたものにだけ「送信済み」の印を付ける。失敗なら再送する
  - 記録ファイルが切り詰められても二重に送らない (印は別ファイル)
  - CLAUDE_NOTIFY_UPLOAD=0 なら送らない (取得はする)。https 以外には鍵を送らない
  - sound_notify は、判定を記録したときは送信、ルールの取得は 5 分ごと、設定の無い端末では何もしない
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib import error

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hooks"))

import notify_rules as nr  # noqa: E402
import notify_sync as ns  # noqa: E402
import sound_notify as sn  # noqa: E402


class FakeServer:
    """lw._OPENER の代わり。受け取った依頼を記録し、決めておいた応答を返す。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.responses: list[object] = []

    def open(self, req, timeout):
        body = json.loads(req.data) if req.data else None
        self.calls.append((req.get_method(), req.full_url, body))
        out = self.responses.pop(0) if self.responses else {}
        if isinstance(out, Exception):
            raise out
        server = self

        class Resp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def read(self_inner):
                return json.dumps(out).encode("utf-8")

        _ = server
        return Resp()


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    server = FakeServer()
    monkeypatch.setattr(ns.lw, "_OPENER", server)
    monkeypatch.setattr(ns.lw, "load_env", lambda: ("https://relay.invalid", "key"))
    monkeypatch.setattr(ns, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(nr, "RULES_CACHE", tmp_path / "cache.json")
    monkeypatch.setattr(nr, "DECISIONS", tmp_path / "decisions.jsonl")
    monkeypatch.delenv("CLAUDE_NOTIFY_UPLOAD", raising=False)
    logs: list[str] = []
    monkeypatch.setattr(ns, "log", lambda m: logs.append(m))
    return server, tmp_path, logs


def add_decision(path: Path, n: int) -> None:
    nr.record_decision(f"s{n}", f"題名{n}", f"本文{n}。教えてください。", nr.Decision(True, "依頼語句", "教えてください"), path)


# --- 取得 -----------------------------------------------------------------------------------
def test_fetch_writes_sanitized_cache_and_remembers_version(env) -> None:
    server, tmp, _ = env
    server.responses.append({"version": 3, "unchanged": False,
                             "config": {"marker": "【要対応】", "rules": [{"type": "req", "phrase": "ご査収"}, {"type": "bad", "phrase": "x"}]}})
    assert ns.fetch("https://relay.invalid", "key") is True
    cached = json.loads((tmp / "cache.json").read_text(encoding="utf-8"))
    assert cached["version"] == 3 and cached["marker"] == "【要対応】" and [r["phrase"] for r in cached["rules"]] == ["ご査収"]
    assert nr.load_config(tmp / "none.json", tmp / "cache.json")["marker"] == "【要対応】"
    server.responses.append({"version": 3, "unchanged": True})
    assert ns.fetch("https://relay.invalid", "key") is False
    assert server.calls[1][1].endswith("/notify/config?since=3")


@pytest.mark.parametrize("response", [
    {"version": 3, "unchanged": True},
    {"version": "x", "config": {}},
    {"version": 4},
    {},
    error.HTTPError("u", 503, "x", {}, None),
    error.HTTPError("u", 404, "x", {}, None),
    OSError("down"),
])
def test_fetch_failure_or_garbage_never_breaks_the_cache(env, response) -> None:
    server, tmp, _ = env
    (tmp / "cache.json").write_text(json.dumps({"marker": "【既存】"}), encoding="utf-8")
    server.responses.append(response)
    assert ns.fetch("https://relay.invalid", "key") is False
    assert json.loads((tmp / "cache.json").read_text(encoding="utf-8"))["marker"] == "【既存】"


# --- 送信 -----------------------------------------------------------------------------------
def test_upload_sends_only_unsent_and_marks_them(env) -> None:
    server, tmp, _ = env
    for i in range(3):
        add_decision(tmp / "decisions.jsonl", i)
    assert ns.upload("https://relay.invalid", "key") == 3
    sent = server.calls[0][2]["items"]
    assert [x["sid"] for x in sent] == ["s0", "s1", "s2"] and all(k in sent[0] for k in ("at", "title", "notify", "reason", "rule", "tail"))
    assert ns.upload("https://relay.invalid", "key") == 0 and len(server.calls) == 1  # 二度目は送るものが無い
    add_decision(tmp / "decisions.jsonl", 9)
    assert ns.upload("https://relay.invalid", "key") == 1
    assert [x["sid"] for x in server.calls[1][2]["items"]] == ["s9"]


def test_upload_failure_keeps_items_for_retry(env) -> None:
    server, tmp, logs = env
    add_decision(tmp / "decisions.jsonl", 1)
    server.responses.append(error.HTTPError("u", 502, "x", {}, None))
    assert ns.upload("https://relay.invalid", "key") == 0
    assert any("HTTP 502" in m for m in logs)
    assert ns.upload("https://relay.invalid", "key") == 1  # 次の機会に再送される


def test_upload_is_chunked_to_50(env) -> None:
    server, tmp, _ = env
    for i in range(55):
        add_decision(tmp / "decisions.jsonl", i)
    assert ns.upload("https://relay.invalid", "key") == 50
    assert ns.upload("https://relay.invalid", "key") == 5


def test_trimming_the_decision_file_does_not_cause_resends(env) -> None:
    server, tmp, _ = env
    add_decision(tmp / "decisions.jsonl", 1)
    ns.upload("https://relay.invalid", "key")
    (tmp / "decisions.jsonl").write_text("", encoding="utf-8")   # 切り詰められた
    add_decision(tmp / "decisions.jsonl", 2)
    assert ns.upload("https://relay.invalid", "key") == 1
    assert [x["sid"] for x in server.calls[-1][2]["items"]] == ["s2"]


def test_upload_can_be_disabled_by_env(env, monkeypatch) -> None:
    server, tmp, _ = env
    add_decision(tmp / "decisions.jsonl", 1)
    monkeypatch.setenv("CLAUDE_NOTIFY_UPLOAD", "0")
    assert ns.upload("https://relay.invalid", "key") == 0 and server.calls == []


def test_broken_lines_in_the_decision_file_are_skipped(env) -> None:
    server, tmp, _ = env
    (tmp / "decisions.jsonl").write_text('not json\n[]\n{"at": 5}\n', encoding="utf-8")
    add_decision(tmp / "decisions.jsonl", 1)
    assert ns.upload("https://relay.invalid", "key") == 1


def test_state_file_garbage_is_tolerated(env) -> None:
    server, tmp, _ = env
    (tmp / "state.json").write_text('{"sent_ids": "bad", "config_version": true}', encoding="utf-8")
    add_decision(tmp / "decisions.jsonl", 1)
    assert ns.upload("https://relay.invalid", "key") == 1


# --- run: 設定・https ---------------------------------------------------------------------------
def test_run_refuses_non_https_and_missing_config(env, monkeypatch) -> None:
    server, tmp, logs = env
    add_decision(tmp / "decisions.jsonl", 1)
    monkeypatch.setattr(ns.lw, "load_env", lambda: ("http://relay.example", "key"))
    ns.run()
    monkeypatch.setattr(ns.lw, "load_env", lambda: ("", ""))
    ns.run()
    assert server.calls == [] and len(logs) == 2


def test_run_selects_fetch_and_upload(env) -> None:
    server, tmp, _ = env
    add_decision(tmp / "decisions.jsonl", 1)
    server.responses += [{"version": 1, "unchanged": True}, {"stored": 1}]
    ns.run(do_fetch=True, do_upload=False)
    assert [c[1].split("/notify/")[1].split("?")[0] for c in server.calls] == ["config"]
    ns.run(do_fetch=False, do_upload=True)
    assert [c[1].split("/notify/")[1] for c in server.calls][-1] == "decisions"


def test_main_never_raises(env, monkeypatch) -> None:
    monkeypatch.setattr(ns, "run", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    assert ns.main([]) == 0


# --- sound_notify からの起動条件 ------------------------------------------------------------------
@pytest.fixture()
def hook(tmp_path: Path, monkeypatch):
    spawned: list[tuple[bool, bool]] = []
    monkeypatch.setattr(sn, "SYNC_STAMP", tmp_path / "sync.json")
    monkeypatch.setattr(sn, "LOCAL", tmp_path)
    monkeypatch.setattr(sn, "spawn_sync", lambda upload, fetch: spawned.append((upload, fetch)))
    (tmp_path / ".env").write_text("RELAY_API_KEY=x\n", encoding="utf-8")
    return spawned, tmp_path


def test_sync_runs_upload_when_a_decision_was_recorded(hook) -> None:
    spawned, _ = hook
    sn.sync_if_needed(True)
    sn.sync_if_needed(True)
    assert spawned == [(True, True), (True, False)]   # 取得は最初の1回だけ (5 分に 1 回)


def test_sync_does_nothing_when_nothing_to_do(hook) -> None:
    spawned, _ = hook
    sn.sync_if_needed(False)   # 初回は取得の番
    sn.sync_if_needed(False)
    assert spawned == [(False, True), (False, False)]   # 2 回目は取得の時刻前なので何も起動しない(実際は spawn_sync が即戻る)


def test_sync_skipped_without_relay_config(hook) -> None:
    spawned, tmp = hook
    (tmp / ".env").unlink()
    sn.sync_if_needed(True)
    assert spawned == []


def test_spawn_sync_arguments(monkeypatch) -> None:
    got: list[list[str]] = []
    monkeypatch.setattr(sn, "_spawn", lambda args: got.append(args))
    sn.spawn_sync(upload=True, fetch=False)
    sn.spawn_sync(upload=False, fetch=True)
    sn.spawn_sync(upload=True, fetch=True)
    sn.spawn_sync(upload=False, fetch=False)
    assert [a[4:] for a in got] == [["--upload"], ["--fetch"], ["--fetch", "--upload"]]
