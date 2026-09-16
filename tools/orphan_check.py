"""孤児化して CPU を焼き続けているプロセスを洗い出す (読み取り専用・kill はしない)。

2026-09-16 に同種の事故が 2 件見つかったことを受けて追加:

  - `pytest tests/test_lcl_provision.py` が Excel COM のハングで **25.5 時間** 3 コアを焼いた
  - `find / -maxdepth 6` が MSYS2 の /proc/registry を走査し続け **51 分** 1 コアを焼いた

どちらも Claude Code の Bash 呼び出しが親で、セッション終了後も子だけが残っていた。
この種のプロセスは自然には止まらず、放置すると累積する。

判定の考え方:
    「親が死んでいる」だけでは駄目。タスクスケジューラ起動の PWA サーバー群は
    全て親が消えており、それらを毎回誤検出してしまう。実際に両者を分けたのは

        (1) LISTEN ポートを持たない     … 常駐サーバーは必ず待ち受けている
        (2) 画面にウィンドウを持たない  … タスクマネージャ等の操作中アプリを除く
        (3) 生涯平均 CPU が高い          … 事故の 2 件は 79% と 284%、
                                           正常な常駐サーバーは 25% 以下だった

    の 3 点なので、(1)(2) を満たす孤児を 🔴、それ以外を 🟡 にする。

プロセス一覧は WMI (Win32_Process) を 1 回だけ引く。psutil.process_iter で
名前・親・起動時刻を取ると、1000 プロセス規模で 67 秒かかった (WMI は 1 秒未満)。

使い方:
    python .claude/tools/orphan_check.py              # 既定のしきい値で点検
    python .claude/tools/orphan_check.py --min-cpu 30 # 甘めに拾う
    python .claude/tools/orphan_check.py --min-age 60 # 60 分以上のものだけ
"""
from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime, timezone

import psutil

sys.stdout.reconfigure(encoding="utf-8")

# 生涯平均でこの割合 (1 コア = 100%) を超えていたら「焼いている」とみなす。
# 事故の 2 件が 79% / 284%、正常な常駐サーバーの最大が約 25% だったので 50% に置く。
DEFAULT_MIN_CPU_PCT = 50.0
DEFAULT_MIN_AGE_MIN = 10.0

# 親を持たないのが正常なもの。誤検出を避けるため最初から除外する。
SYSTEM_NAMES = frozenset(
    {
        "System", "System Idle Process", "Idle", "Registry", "Memory Compression",
        "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
        "lsass.exe", "svchost.exe", "fontdrvhost.exe", "dwm.exe", "explorer.exe",
        "spoolsv.exe", "SearchIndexer.exe", "MsMpEng.exe", "audiodg.exe",
        "RuntimeBroker.exe", "sihost.exe", "taskhostw.exe", "ctfmon.exe",
        "WmiPrvSE.exe",
    }
)

# Win32_Process を JSON で 1 回だけ取る。CreationDate は UTC エポック秒に直してから渡す。
# 出力は UTF-8 に固定する。既定の CP932 だと「表」等の 2 バイト目が 0x5C (\) になり、
# UTF-8 として読んだ時点で JSON のエスケープが壊れる (日本語パスで実際に壊れた)。
_PS_QUERY = (
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
    "Get-CimInstance Win32_Process "
    "-Property ProcessId,ParentProcessId,Name,CreationDate,KernelModeTime,UserModeTime,CommandLine "
    "| ForEach-Object { [pscustomobject]@{ "
    "id=[int]$_.ProcessId; ppid=[int]$_.ParentProcessId; name=$_.Name; "
    "created=$(if ($_.CreationDate) { "
    "[DateTimeOffset]::new($_.CreationDate).ToUnixTimeSeconds() } else { 0 }); "
    "cpu=([double]$_.KernelModeTime + [double]$_.UserModeTime) / 1e7; "
    "cmd=$_.CommandLine } } | ConvertTo-Json -Compress"
)


@dataclass
class Proc:
    """WMI から読んだ 1 プロセス分の生データ。"""

    proc_id: int
    parent_id: int
    name: str
    created: float
    cpu_sec: float
    cmdline: str


@dataclass
class Finding:
    """点検で引っかかった 1 プロセス。"""

    proc_id: int
    name: str
    cmdline: str
    age_min: float
    cpu_sec: float
    avg_core_pct: float
    now_core_pct: float
    parent_dead: bool
    parent_id: int
    has_window: bool
    listening: list[int] = field(default_factory=list)

    @property
    def severity(self) -> str:
        """RED = 止めてよい可能性が高い / YELLOW = 一応見ておく。"""
        if self.parent_dead and not self.listening and not self.has_window:
            return "RED"
        return "YELLOW"


def snapshot() -> dict[int, Proc]:
    """全プロセスを WMI で 1 回だけ読む。"""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", _PS_QUERY],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, check=True,
    ).stdout
    rows = json.loads(out) if out.strip() else []
    if isinstance(rows, dict):  # 1 件だけだと配列にならない
        rows = [rows]
    return {
        int(r["id"]): Proc(
            proc_id=int(r["id"]),
            parent_id=int(r["ppid"]),
            name=r["name"] or "",
            created=float(r["created"]),
            cpu_sec=float(r["cpu"]),
            cmdline=r["cmd"] or r["name"] or "",
        )
        for r in rows
        if int(r["id"]) not in (0, 4)
    }


def listening_map() -> dict[int, list[int]]:
    """PID -> LISTEN ポートの対応表を 1 回で作る (1 プロセスずつ引くと極端に遅い)。"""
    ports: dict[int, list[int]] = {}
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, OSError):
        return ports
    for conn in conns:
        if conn.status == psutil.CONN_LISTEN and conn.pid is not None:
            ports.setdefault(conn.pid, []).append(conn.laddr.port)
    return {k: sorted(set(v)) for k, v in ports.items()}


def windowed_pids() -> set[int]:
    """画面に見えるウィンドウを持つ PID。ユーザーが操作中の GUI アプリを 🔴 から外す。"""
    user32 = ctypes.windll.user32
    found: set[int] = set()
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _callback(hwnd: int, _lparam: int) -> bool:
        if user32.IsWindowVisible(hwnd):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            found.add(owner.value)
        return True

    user32.EnumWindows(enum_proc(_callback), 0)
    return found


def collect(min_cpu_pct: float, min_age_min: float, sample_sec: float) -> list[Finding]:
    """条件に合うプロセスを集める。現在値を見るため 2 回スナップショットを取る。"""
    before = snapshot()
    t0 = time.time()

    candidates: dict[int, Proc] = {}
    for p in before.values():
        if p.name in SYSTEM_NAMES or p.created <= 0:
            continue
        age_sec = t0 - p.created
        if age_sec < min_age_min * 60.0:
            continue
        if p.cpu_sec / age_sec * 100.0 < min_cpu_pct:
            continue
        candidates[p.proc_id] = p

    if not candidates:
        return []

    time.sleep(sample_sec)
    after = snapshot()
    elapsed = time.time() - t0
    ports = listening_map()
    windows = windowed_pids()

    findings: list[Finding] = []
    for proc_id, p0 in candidates.items():
        p1 = after.get(proc_id)
        # 途中で終了した、または PID が別プロセスに再利用された
        if p1 is None or p1.created != p0.created:
            continue
        age_sec = time.time() - p1.created
        findings.append(
            Finding(
                proc_id=proc_id,
                name=p1.name,
                cmdline=p1.cmdline,
                age_min=age_sec / 60.0,
                cpu_sec=p1.cpu_sec,
                avg_core_pct=p1.cpu_sec / age_sec * 100.0,
                now_core_pct=(p1.cpu_sec - p0.cpu_sec) / elapsed * 100.0,
                parent_dead=_parent_dead(p1, after),
                parent_id=p1.parent_id,
                has_window=proc_id in windows,
                listening=ports.get(proc_id, []),
            )
        )

    findings.sort(key=lambda f: (f.severity != "RED", -f.cpu_sec))
    return findings


def _parent_dead(p: Proc, procs: dict[int, Proc]) -> bool:
    """親が居ないか。PID は再利用されるので「親の方が後から生まれた」も死亡扱い。"""
    parent = procs.get(p.parent_id)
    return parent is None or parent.created > p.created


def _fmt_age(minutes: float) -> str:
    if minutes < 60:
        return f"{minutes:.0f}分"
    if minutes < 60 * 24:
        return f"{minutes / 60:.1f}時間"
    return f"{minutes / 60 / 24:.1f}日"


def _print_one(f: Finding) -> None:
    mark = "親が消滅" if f.parent_dead else f"親 PID {f.parent_id} は生存"
    ports = f"LISTEN {f.listening}" if f.listening else "待ち受けなし"
    window = " / ウィンドウあり" if f.has_window else ""
    print(f"  PID {f.proc_id}  {f.name}")
    print(f"    経過 {_fmt_age(f.age_min)} / 累積CPU {f.cpu_sec:,.0f}秒 "
          f"/ 生涯平均 {f.avg_core_pct:.0f}% / 現在 {f.now_core_pct:.0f}%")
    print(f"    {mark} / {ports}{window}")
    print(f"    {f.cmdline[:150]}")
    if "python" in f.name.lower():
        print(f"    調べる: py-spy dump --pid {f.proc_id}")
    print()


def report(findings: list[Finding], min_cpu_pct: float, min_age_min: float) -> int:
    """見つかったものを表示する。戻り値は 🔴 の件数。"""
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] 条件: 起動から {min_age_min:.0f} 分以上 / "
          f"生涯平均 CPU {min_cpu_pct:.0f}% 以上 (1コア=100%)")
    print()

    if not findings:
        print("該当なし。焼き続けているプロセスはありません。")
        return 0

    red = [f for f in findings if f.severity == "RED"]
    yellow = [f for f in findings if f.severity == "YELLOW"]

    if red:
        print("=" * 74)
        print(f"🔴 孤児 + 待ち受けなし + ウィンドウなし ... 止めてよい可能性が高い ({len(red)} 件)")
        print("=" * 74)
        for f in red:
            _print_one(f)

    if yellow:
        print("=" * 74)
        print(f"🟡 参考 ... 待ち受けあり / ウィンドウあり / 親が生存 ({len(yellow)} 件)")
        print("=" * 74)
        for f in yellow:
            _print_one(f)

    if red:
        print("-" * 74)
        print("止める場合 (内容を確認してから実行してください):")
        for f in red:
            print(f"  taskkill /F /T /PID {f.proc_id}")
    return len(red)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="孤児化して CPU を焼いているプロセスを探す (読み取り専用)"
    )
    parser.add_argument("--min-cpu", type=float, default=DEFAULT_MIN_CPU_PCT,
                        help=f"生涯平均 CPU のしきい値%% (既定 {DEFAULT_MIN_CPU_PCT:.0f})")
    parser.add_argument("--min-age", type=float, default=DEFAULT_MIN_AGE_MIN,
                        help=f"起動からの経過分のしきい値 (既定 {DEFAULT_MIN_AGE_MIN:.0f})")
    parser.add_argument("--sample", type=float, default=3.0,
                        help="現在値を測るサンプリング秒数 (既定 3)")
    args = parser.parse_args()

    findings = collect(args.min_cpu, args.min_age, args.sample)
    red = report(findings, args.min_cpu, args.min_age)
    # 🔴 があれば 1 を返す (定期実行して拾いたくなったとき用)
    return 1 if red else 0


if __name__ == "__main__":
    raise SystemExit(main())
