"""CPU優先度の見張り役: PWA を先に、Claude/Codex の作業を後に回す。

2026-10-26 導入。端末 A は CPU が 100% に張り付き、PWA が反応しなくなっていた。
実測の原因: タスクスケジューラの初期値(優先度 7)のため、Caddy と業務 PWA のサーバーは
すべて BelowNormal で動き、Claude Code のセッションと MCP は Normal だった
→ 混むと PWA が必ず後回しになる。

この道具が 30 秒ごとに直す(変えるのは優先度だけ。プロセスは止めない):
- caddy                                   → AboveNormal(入口なので最優先)
- TCP で待ち受けている python/pythonw/node → Normal(PWA のサーバー。RustDesk の hbbs/hbbr も)
- Claude Code のセッション・Codex とその子孫 → BelowNormal(混んだときだけ後回し)
Claude のセッションの子孫が待ち受けていても(試験用のサーバー等)PWA 扱いにはしない。
今より高い優先度に下げ直すことはあっても、Idle などもっと低いものを上げることはしない。

使い方:
    pythonw cpu_priority_guard.py            # 常駐(タスク CpuPriorityGuard から起動)
    python  cpu_priority_guard.py --once --dry-run   # 何を変えるかだけ表示
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import os
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

import psutil

LOG_FILE = Path(r"C:\ClaudeCode\.claude\incidents\cpu_priority_guard.log")
LOCK_FILE = Path(r"C:\ClaudeCode\.claude\incidents\cpu_priority_guard.pid")
LOG_MAX_BYTES = 2 * 1024 * 1024
INTERVAL_SEC = 30

ABOVE = psutil.ABOVE_NORMAL_PRIORITY_CLASS
NORMAL = psutil.NORMAL_PRIORITY_CLASS
BELOW = psutil.BELOW_NORMAL_PRIORITY_CLASS

# 低い → 高い の順。下げるときは「今より低くなる場合だけ」変える。
RANK = {
    psutil.IDLE_PRIORITY_CLASS: 0,
    BELOW: 1,
    NORMAL: 2,
    ABOVE: 3,
    psutil.HIGH_PRIORITY_CLASS: 4,
    psutil.REALTIME_PRIORITY_CLASS: 5,
}
NAMES = {0: "Idle", 1: "BelowNormal", 2: "Normal", 3: "AboveNormal", 4: "High", 5: "Realtime"}

SERVER_EXES = {"python.exe", "pythonw.exe", "node.exe", "hbbs.exe", "hbbr.exe"}  # hbbs/hbbr = RustDesk 中継


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    name: str  # 小文字の exe 名
    exe: str  # 小文字のフルパス(読めなければ空)
    nice: int
    created: float = 0.0  # 起動時刻(PID の使い回しの判定用)


def is_background_root(p: Proc) -> bool:
    """後回しにする作業の起点か(Claude Code のセッション・Codex)。"""
    if p.name == "claude.exe" and "\\claude\\claude-code\\" in p.exe:
        return True  # デスクトップアプリの画面側(AnthropicClaude\app-*)は対象外
    return p.name in {"codex.exe", "chatgpt.exe"}


def background_pids(procs: dict[int, Proc]) -> set[int]:
    """起点とその子孫の PID。"""
    children: dict[int, list[int]] = {}
    for p in procs.values():
        parent = procs.get(p.ppid)
        # 親が子より後に起動していれば、その PID は使い回された別物(本当の親は終了済み)
        if parent is None or parent.created > p.created:
            continue
        children.setdefault(p.ppid, []).append(p.pid)
    result: set[int] = set()
    stack = [p.pid for p in procs.values() if is_background_root(p)]
    while stack:
        pid = stack.pop()
        if pid in result:
            continue
        result.add(pid)
        stack.extend(children.get(pid, []))
    return result


def decide(p: Proc, listening: set[int], background: set[int]) -> int | None:
    """付けるべき優先度。変えなくてよければ None。"""
    if p.name == "caddy.exe":
        target = ABOVE
    elif p.pid in background:
        if RANK.get(p.nice, 2) <= RANK[BELOW]:
            return None  # Idle 等もっと低いものは上げない
        target = BELOW
    elif p.name in SERVER_EXES and p.pid in listening:
        if RANK.get(p.nice, 2) >= RANK[NORMAL]:
            return None  # 既に Normal 以上(誰かが意図して上げたものは下げない)
        target = NORMAL
    else:
        return None
    return None if p.nice == target else target


class _ProcessEntry32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def toolhelp_table() -> list[tuple[int, int, str]]:
    """(pid, ppid, 小文字の exe 名) を 1 回の一覧取得で読む。

    psutil の ppid/name は 1 プロセスごとに一覧を作り直すため、1,200 プロセスで
    CPU 56 秒かかった(2026-10-26 実測)。こちらは数十ミリ秒。
    """
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if snap in (None, wintypes.HANDLE(-1).value):
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot")
    rows: list[tuple[int, int, str]] = []
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            rows.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile.lower()))
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return rows


@dataclass
class _Known:
    handle: psutil.Process
    created: float
    exe: str


class Snapshotter:
    """プロセス一覧を作る。psutil.Process は作るたびに起動時刻を読むため(1,200 個で約 6 秒)、
    PID ごとに 1 回だけ作って使い回す。2 回目からは新しく現れたプロセスの分だけ読む。"""

    def __init__(self) -> None:
        self._known: dict[tuple[int, str], _Known] = {}
        # 変えられなかったプロセス(PID, 起動時刻)。管理者権限のタスクは毎回 AccessDenied に
        # なるので、ログには最初の 1 回だけ出す(30 秒ごとに同じ行が増えるのを防ぐ)
        self.denied: set[tuple[int, float]] = set()

    def _get(self, pid: int, name: str) -> _Known | None:
        key = (pid, name)
        if key not in self._known:
            try:
                h = psutil.Process(pid)
                exe = h.exe().lower() if name == "claude.exe" else ""  # パスで見分けるのは claude.exe だけ
                self._known[key] = _Known(handle=h, created=h.create_time(), exe=exe)
            except psutil.Error:
                return None
        return self._known[key]

    def handle(self, p: Proc) -> psutil.Process:
        known = self._known.get((p.pid, p.name))
        return known.handle if known else psutil.Process(p.pid)

    def take(self) -> dict[int, Proc]:
        rows = toolhelp_table()
        alive = {(pid, name) for pid, _, name in rows}
        self._known = {k: v for k, v in self._known.items() if k in alive}
        live = {(pid, v.created) for (pid, _), v in self._known.items()}
        self.denied &= live
        procs: dict[int, Proc] = {}
        for pid, ppid, name in rows:
            if pid in (0, 4):
                continue
            known = self._get(pid, name)
            if known is None:
                continue
            try:
                nice = known.handle.nice()
            except psutil.Error:
                continue
            procs[pid] = Proc(pid=pid, ppid=ppid, name=name, exe=known.exe, nice=nice, created=known.created)
        return procs


def listening_pids() -> set[int]:
    try:
        conns = psutil.net_connections(kind="tcp")
    except (psutil.AccessDenied, OSError):
        return set()
    return {c.pid for c in conns if c.status == psutil.CONN_LISTEN and c.pid}


def log(msg: str) -> None:
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n"
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            LOG_FILE.replace(LOG_FILE.with_suffix(".log.1"))
        with LOG_FILE.open("a", encoding="utf-8", newline="") as f:
            f.write(line)
    except OSError:
        pass
    if sys.stdout is not None:
        print(line, end="")


def run_once(snap: Snapshotter, dry_run: bool) -> int:
    procs = snap.take()
    listening = listening_pids()
    background = background_pids(procs)
    changed = 0
    for p in procs.values():
        target = decide(p, listening, background)
        if target is None:
            continue
        label = f"{p.name}({p.pid}) {NAMES[RANK.get(p.nice, 2)]} -> {NAMES[RANK[target]]}"
        if dry_run:
            log("[dry-run] " + label)
            changed += 1
            continue
        try:
            snap.handle(p).nice(target)
            changed += 1
            log(label)
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            key = (p.pid, p.created)
            if key not in snap.denied:
                snap.denied.add(key)
                log(f"skip {label}: {type(e).__name__}(以後この PID は記録しない)")
    return changed


def another_instance_running() -> bool:
    """二重起動の防止。PID ファイルの持ち主が生きていて、この道具なら True。"""
    try:
        pid = int(LOCK_FILE.read_text(encoding="utf-8").strip())
        if pid != os.getpid() and psutil.pid_exists(pid):
            cmd = " ".join(psutil.Process(pid).cmdline())
            if "cpu_priority_guard" in cmd:
                return True
    except (OSError, ValueError, psutil.Error):
        pass
    LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
    return False


def main() -> int:
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--once", action="store_true", help="1回だけ直して終わる")
    ap.add_argument("--dry-run", action="store_true", help="変えずに表示だけ")
    ap.add_argument("--interval", type=int, default=INTERVAL_SEC)
    args = ap.parse_args()

    if args.once or args.dry_run:
        n = run_once(Snapshotter(), args.dry_run)
        print(f"対象 {n} 件")
        return 0
    if another_instance_running():
        return 0
    # 見張り役自身は後回しでよい(タスクの既定どおり BelowNormal のまま)
    log("start")
    snap = Snapshotter()
    while True:
        try:
            run_once(snap, dry_run=False)
        except Exception as e:  # 1 回の失敗で常駐を止めない
            log(f"error {type(e).__name__}: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
