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
    event, matcher = rows[0][0], rows[0][1]
    assert event == "PreToolUse"
    assert set(matcher.split("|")) == {"Bash", "PowerShell", "Read"}
