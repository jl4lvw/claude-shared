"""cpu_priority_guard の判定(どのプロセスをどの優先度にするか)の試験。実プロセスは触らない。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cpu_priority_guard as g  # noqa: E402

SESSION_EXE = r"c:\users\user\appdata\roaming\claude\claude-code\2.1.308\x\claude.exe"
DESKTOP_EXE = r"c:\users\user\appdata\local\anthropicclaude\app-3.6556.2\claude.exe"


def proc(pid: int, ppid: int, name: str, exe: str = "", nice: int = g.NORMAL, created: float = 10.0) -> g.Proc:
    return g.Proc(pid=pid, ppid=ppid, name=name, exe=exe, nice=nice, created=created)


def table(*ps: g.Proc) -> dict[int, g.Proc]:
    return {p.pid: p for p in ps}


def test_caddy_goes_above_normal() -> None:
    p = proc(1, 0, "caddy.exe", nice=g.BELOW)
    assert g.decide(p, set(), set()) == g.ABOVE


def test_pwa_server_raised_from_below_to_normal() -> None:
    p = proc(2, 0, "pythonw.exe", nice=g.BELOW)
    assert g.decide(p, {2}, set()) == g.NORMAL


def test_server_already_above_normal_is_left_alone() -> None:
    p = proc(2, 0, "python.exe", nice=g.ABOVE)
    assert g.decide(p, {2}, set()) is None


def test_non_listening_python_is_left_alone() -> None:
    # 定期タスクの一括処理などは BelowNormal のままでよい
    p = proc(3, 0, "python.exe", nice=g.BELOW)
    assert g.decide(p, set(), set()) is None


def test_session_tree_lowered_but_desktop_ui_untouched() -> None:
    procs = table(
        proc(10, 1, "claude.exe", DESKTOP_EXE, created=1.0),
        proc(11, 10, "claude.exe", SESSION_EXE, created=2.0),
        proc(12, 11, "node.exe", created=3.0),
        proc(13, 12, "python.exe", created=4.0),
    )
    bg = g.background_pids(procs)
    assert bg == {11, 12, 13}
    assert g.decide(procs[10], set(), bg) is None
    assert g.decide(procs[13], set(), bg) == g.BELOW


def test_dev_server_inside_session_stays_low() -> None:
    procs = table(proc(11, 1, "claude.exe", SESSION_EXE, created=1.0), proc(14, 11, "python.exe", nice=g.BELOW, created=2.0))
    bg = g.background_pids(procs)
    assert g.decide(procs[14], {14}, bg) is None


def test_idle_process_is_not_raised() -> None:
    procs = table(proc(11, 1, "claude.exe", SESSION_EXE, nice=g.psutil.IDLE_PRIORITY_CLASS))
    bg = g.background_pids(procs)
    assert g.decide(procs[11], set(), bg) is None


def test_codex_app_lowered() -> None:
    procs = table(proc(20, 1, "chatgpt.exe"), proc(21, 20, "git.exe", created=11.0))
    bg = g.background_pids(procs)
    assert bg == {20, 21}


def test_reused_parent_pid_is_not_followed() -> None:
    # PID 11 は今はセッションだが、PWA(30)より後に起動した = 本当の親は既に終了して番号が使い回された
    procs = table(proc(11, 1, "claude.exe", SESSION_EXE, created=50.0), proc(30, 11, "pythonw.exe", nice=g.BELOW, created=5.0))
    bg = g.background_pids(procs)
    assert 30 not in bg
    assert g.decide(procs[30], {30}, bg) == g.NORMAL
