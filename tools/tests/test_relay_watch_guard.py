"""relay_watch_guard.py (PreToolUse: relay 待ち受けセッション専用の危険操作ガード) のテスト.

なぜ要るか (2026-09-30): 待ち受け(/m watch)と処理役は確認なし(bypass)で動かす。
確認の代わりにこのフックが危険な操作だけを止める。一番怖いのは
「止めるべきものを通してしまう」退行と「普段のセッションまで止めてしまう」誤爆。
ここでは次を固定する:
- 登録したセッションだけに効く(他のセッション・未登録では何もしない)
- GUI(041 agent_runner)と同じ語彙で止める / 日常のコマンドは止めない
- 動作確認の口(probe)が権限モードを返す
- 壊れた入力で作業を止めない(fail open)・終了コード 2 を使わない
- 登録済みのセッションで判定が壊れたら通さない(fail closed)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[2] / "hooks"
sys.path.insert(0, str(HOOKS))

import relay_watch_guard as G  # noqa: E402

SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture()
def armed(tmp_path: Path) -> Path:
    (tmp_path / G.GUARD_FILE).write_text(
        json.dumps({"session_id": SID}), encoding="utf-8"
    )
    return tmp_path


def _bash(command: str, sid: str = SID, tool: str = "Bash", **extra) -> dict:
    return {"session_id": sid, "tool_name": tool, "tool_input": {"command": command}, **extra}


def _denied(out: dict | None) -> bool:
    return bool(out) and out["hookSpecificOutput"]["permissionDecision"] == "deny"


DANGEROUS = [
    "git push --force origin main",
    "git push -f",
    "git reset --hard HEAD~1",
    "git clean -fd",
    "git branch -D feature",
    "rm -rf build",
    "rm -Rf /tmp/x",
    'sqlite3 app.db "DELETE FROM orders"',
    'sqlite3 app.db "drop table t"',
    "curl -X PUT https://example.invalid/api",
    "Remove-Item -Path C:\\x -Recurse -Force",
    "del /s /q C:\\x",
    "format D:",
    "taskkill /F /PID 1234",
    "Stop-Process -Id 1234",
    "Stop-ScheduledTask -TaskName RelayAPIServer",
    "Disable-ScheduledTask -TaskName RelayGui",
    "schtasks /delete /tn X",
    "sudo rm file",
    "killall python",
    "chmod -R 777 .",
    "osascript -e 'do shell script'",
    "curl -X POST https://example.invalid/hook -d x",
    "Invoke-RestMethod -Uri https://example.invalid -Method Post -Body x",
    'python relay_client.py send "作業を始めてください" --to TK',
    'python relay_client.py send "本文" --to TK --type task',
    "python relay_client.py answer 12 --decision yes",
    'python relay_client.py send "承認しました。進めてください" --to TK --type reply',
]

SAFE = [
    "git status",
    "git log --oneline --format=%h -5",
    "ruff format .",
    'python -c "print(\'{}\'.format(1))"',
    "python .claude/skills/relay/scripts/relay_client.py claim 2570",
    "python .claude/skills/relay/scripts/relay_client.py done 2570",
    "python .claude/skills/relay/scripts/relay_client.py unclaim 2570",
    'python .claude/skills/relay/scripts/relay_client.py send "調査結果です" --to TK --type result',
    "python .claude/skills/relay/scripts/relay_client.py handoff create --thread t --summary s",
    'pwsh -NoProfile -File ".claude\\skills\\message-check\\scripts\\relay_watch.ps1" -Mode ack -AckId 12',
    "curl -s https://example.invalid/health",
    "Invoke-RestMethod -Uri https://example.invalid/status",
    "Remove-Item C:\\tmp\\one.txt",
    "python -m pytest tests -q",
]


@pytest.mark.parametrize("command", DANGEROUS)
def test_stops_dangerous_commands(armed, command) -> None:
    out = G.handle_hook(_bash(command), armed)
    assert _denied(out), command
    assert "APPROVAL_NEEDED" in out["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("command", SAFE)
def test_leaves_everyday_commands_alone(armed, command) -> None:
    assert G.handle_hook(_bash(command), armed) is None, command


def test_the_powershell_tool_is_checked_too(armed) -> None:
    assert _denied(G.handle_hook(_bash("Stop-Process -Name uvicorn", tool="PowerShell"), armed))


def test_other_sessions_are_untouched(armed) -> None:
    """普段の対話セッションは人がその場で判断できるので止めない。"""
    other = "99999999-0000-0000-0000-000000000000"
    for command in DANGEROUS + [G.PROBE]:
        assert G.handle_hook(_bash(command, sid=other), armed) is None


def test_nothing_happens_before_arming(tmp_path) -> None:
    for command in DANGEROUS + [G.PROBE]:
        assert G.handle_hook(_bash(command), tmp_path) is None


def test_a_broken_guard_file_means_not_armed(tmp_path) -> None:
    (tmp_path / G.GUARD_FILE).write_text("{壊れた", encoding="utf-8")
    assert G.handle_hook(_bash("rm -rf x"), tmp_path) is None
    (tmp_path / G.GUARD_FILE).write_text(json.dumps({"session_id": ""}), encoding="utf-8")
    assert G.handle_hook(_bash("rm -rf x"), tmp_path) is None


def test_reads_a_guard_file_written_with_bom(tmp_path) -> None:
    """Windows PowerShell 5.1 の Set-Content -Encoding UTF8 は BOM を付ける。"""
    (tmp_path / G.GUARD_FILE).write_bytes(
        b"\xef\xbb\xbf" + json.dumps({"session_id": SID}).encode("utf-8")
    )
    assert _denied(G.handle_hook(_bash("rm -rf x"), tmp_path))


def test_the_probe_reports_armed_and_the_permission_mode(armed) -> None:
    out = G.handle_hook(
        _bash(f"echo {G.PROBE}", permission_mode="bypassPermissions"), armed
    )
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert _denied(out)
    assert "歯止めは有効です" in reason
    assert "bypassPermissions" in reason


def test_other_tools_are_ignored(armed) -> None:
    payload = {"session_id": SID, "tool_name": "Write",
               "tool_input": {"file_path": "x", "content": "rm -rf /"}}
    assert G.handle_hook(payload, armed) is None


def _read(path: Path) -> dict:
    return {"session_id": SID, "tool_name": "Read", "tool_input": {"file_path": str(path)}}


def test_stops_reading_relay_attachments_whole(armed, tmp_path) -> None:
    inbox = tmp_path / "relay_local" / "inbox"
    inbox.mkdir(parents=True)
    pdf = inbox / "spec.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    big = inbox / "log.txt"
    big.write_bytes(b"x" * (G.SAFE_READ_THRESHOLD_BYTES + 1))
    small = inbox / "note.txt"
    small.write_text("短いメモ", encoding="utf-8")
    elsewhere = tmp_path / "docs" / "spec.pdf"
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(b"%PDF-1.4")

    assert _denied(G.handle_hook(_read(pdf), armed))
    assert _denied(G.handle_hook(_read(big), armed))
    assert G.handle_hook(_read(small), armed) is None
    assert G.handle_hook(_read(elsewhere), armed) is None


def test_denials_are_logged_without_the_command(armed) -> None:
    G.handle_hook(_bash("rm -rf C:\\secret-path-xyz"), armed)
    log = (armed / G.LOG_FILE).read_text(encoding="utf-8")
    assert "削除" in log
    assert "secret-path-xyz" not in log


def test_fails_closed_inside_the_armed_session(armed, monkeypatch) -> None:
    def boom(_command: str) -> str | None:
        raise RuntimeError("判定が壊れた")

    monkeypatch.setattr(G, "classify_command", boom)
    out = G.handle_hook(_bash("git status"), armed)
    assert _denied(out)
    # 他のセッションは判定まで進まないので巻き込まない
    assert G.handle_hook(_bash("git status", sid="other"), armed) is None


def _run(stdin: bytes, state: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "RELAY_WATCH_STATE_DIR": str(state), "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, str(HOOKS / "relay_watch_guard.py")],
        input=stdin, capture_output=True, env=env, timeout=30,
    )


def test_the_script_denies_through_stdout(armed) -> None:
    r = _run(json.dumps(_bash("rm -rf x")).encode("utf-8"), armed)
    assert r.returncode == 0
    out = json.loads(r.stdout.decode("utf-8"))
    assert _denied(out)


@pytest.mark.parametrize("stdin", [b"", b"not json", b"[]", b"\xff\xfe"])
def test_never_blocks_on_bad_input(armed, stdin) -> None:
    """終了コード 2 は Claude Code では「ブロック」。壊れた入力で全部止めない。"""
    r = _run(stdin, armed)
    assert r.returncode == 0
    assert r.stdout == b""


def test_install_hooks_registers_it() -> None:
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "install_hooks.py"
    spec = importlib.util.spec_from_file_location("install_hooks_for_guard", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rows = [h for h in mod._HOOKS if h[2] == ".claude/hooks/relay_watch_guard.py"]
    assert rows, "install_hooks.py に登録がありません(他の端末に入らない)"
    by_event = {h[0]: h for h in rows}
    assert set(by_event) == {"PreToolUse", "Stop"}
    assert set(by_event["PreToolUse"][1].split("|")) == {"Bash", "PowerShell", "Read"}
    # Stop は見張りの起動を最大 WATCHER_START_GRACE_SEC 待つので、timeout はそれより長く
    assert by_event["Stop"][3] > G.WATCHER_START_GRACE_SEC


# --------------------------------------------------------------------- Stop


def _stop(sid: str = SID, **extra) -> dict:
    return {"session_id": sid, "hook_event_name": "Stop", "stop_hook_active": False, **extra}


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_stop_is_sent_back_when_the_watcher_is_stopped(armed) -> None:
    out = G.handle_stop(_stop(), armed, grace=0)
    assert out["decision"] == "block"
    assert "このセッションへの指示ではありません" in out["reason"]
    assert "W5" in out["reason"]


def test_stop_passes_while_the_watcher_runs(armed) -> None:
    (armed / G.LOCK_FILE).write_text(f"{os.getpid()} 2026-09-30T18:00:00", encoding="ascii")
    assert G.handle_stop(_stop(), armed, grace=0) is None


def test_a_stale_lock_does_not_count_as_running(armed) -> None:
    (armed / G.LOCK_FILE).write_text(f"{_dead_pid()} 2026-09-30T18:00:00", encoding="ascii")
    assert G.handle_stop(_stop(), armed, grace=0)["decision"] == "block"
    (armed / G.LOCK_FILE).write_text("", encoding="ascii")
    assert G.handle_stop(_stop(), armed, grace=0)["decision"] == "block"


def test_the_second_stop_is_allowed(armed) -> None:
    """差し戻しは1回だけ。正当に止める場面(exit 4・運用者の指示等)で閉じ込めない。"""
    assert G.handle_stop(_stop(stop_hook_active=True), armed, grace=0) is None


def test_stop_ignores_other_sessions_and_unarmed(armed, tmp_path) -> None:
    assert G.handle_stop(_stop(sid="other"), armed, grace=0) is None
    empty = tmp_path / "empty"
    empty.mkdir()
    assert G.handle_stop(_stop(), empty, grace=0) is None


def test_stop_waits_for_a_watcher_that_is_just_starting(armed, monkeypatch) -> None:
    """run_in_background 直後はロックがまだ無い。少し待って現れたら通す。"""
    calls = {"n": 0}

    def fake_alive(_d) -> bool:
        calls["n"] += 1
        return calls["n"] >= 3

    monkeypatch.setattr(G, "watcher_alive", fake_alive)
    monkeypatch.setattr(G.time, "sleep", lambda _s: None)
    assert G.handle_stop(_stop(), armed, grace=5) is None
    assert calls["n"] == 3


def test_checking_a_pid_never_kills_it() -> None:
    """Windows の os.kill(pid, 0) は TerminateProcess。生存確認で殺してはいけない。"""
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert G._pid_alive(p.pid) is True
        assert G._pid_alive(p.pid) is True
        assert p.poll() is None, "生存確認で見張りを終了させてしまいました"
    finally:
        p.kill()
        p.wait()
    assert G._pid_alive(p.pid) is False


def test_the_script_blocks_stop_through_stdout(armed) -> None:
    r = _run(json.dumps(_stop()).encode("utf-8"), armed)
    assert r.returncode == 0
    assert json.loads(r.stdout.decode("utf-8"))["decision"] == "block"


# ------------------------------------------------ 1台で複数名義(RC の Mac)


SID_RC = "aaaaaaaa-0000-0000-0000-00000000000c"
SID_RCS = "bbbbbbbb-0000-0000-0000-00000000000d"


@pytest.fixture()
def armed_multi(tmp_path: Path) -> Path:
    (tmp_path / G.GUARD_FILE).write_text(json.dumps({"sessions": {
        "RC": {"session_id": SID_RC, "registered_at": "2026-10-01T09:00:00"},
        "RCS": {"session_id": SID_RCS},
        "../evil": {"session_id": "cccccccc"},   # パスに使えない名義は読まない
    }}), encoding="utf-8")
    return tmp_path


def test_each_identity_session_is_guarded(armed_multi) -> None:
    assert _denied(G.handle_hook(_bash("rm -rf x", sid=SID_RC), armed_multi))
    assert _denied(G.handle_hook(_bash("rm -rf x", sid=SID_RCS), armed_multi))
    assert G.handle_hook(_bash("rm -rf x", sid="cccccccc"), armed_multi) is None
    assert G.handle_hook(_bash("rm -rf x", sid=SID), armed_multi) is None


def test_stop_looks_at_the_lock_of_that_identity(armed_multi) -> None:
    (armed_multi / "RC").mkdir()
    (armed_multi / "RC" / G.LOCK_FILE).write_text(f"{os.getpid()} x", encoding="ascii")
    # RC の見張りは動いている / RCS の見張りは止まっている
    assert G.handle_stop(_stop(sid=SID_RC), armed_multi, grace=0) is None
    assert G.handle_stop(_stop(sid=SID_RCS), armed_multi, grace=0)["decision"] == "block"
    # 直下の watch.lock は複数名義の登録では見ない
    (armed_multi / G.LOCK_FILE).write_text(f"{os.getpid()} x", encoding="ascii")
    assert G.handle_stop(_stop(sid=SID_RCS), armed_multi, grace=0)["decision"] == "block"


def test_both_shapes_can_coexist(tmp_path) -> None:
    (tmp_path / G.GUARD_FILE).write_text(json.dumps({
        "session_id": SID, "sessions": {"RC": {"session_id": SID_RC}},
    }), encoding="utf-8")
    assert G.armed_sessions(tmp_path) == {
        SID: tmp_path / G.LOCK_FILE,
        SID_RC: tmp_path / "RC" / G.LOCK_FILE,
    }
