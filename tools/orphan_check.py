"""孤児化して CPU を焼き続けているプロセスを洗い出す (読み取り専用・kill はしない)。

2026-09-16 に同種の事故が 2 件見つかったことを受けて追加:

  - `pytest tests/test_lcl_provision.py` が Excel COM のハングで **25.5 時間** 3 コアを焼いた
  - `find / -maxdepth 6` が MSYS2 の /proc/registry を走査し続け **51 分** 1 コアを焼いた

どちらも Claude Code の Bash 呼び出しが親で、セッション終了後も子だけが残っていた。
この種のプロセスは自然には止まらず、放置すると累積する。

判定の考え方:
    「親が死んでいる」だけでは駄目。タスクスケジューラ起動の PWA サーバー群は
    全て親が消えており、それらを毎回誤検出してしまう。実際に両者を分けたのは

        (1) LISTEN ポートを持たない  … 常駐サーバーは必ず待ち受けている
        (2) 生涯平均 CPU が高い       … 事故の 2 件は 79% と 284%、
                                        正常な常駐サーバーは 25% 以下だった

    の 2 点だったので、これを組み合わせて 🔴 と 🟡 に分ける。

使い方:
    python .claude/tools/orphan_check.py              # 既定のしきい値で点検
    python .claude/tools/orphan_check.py --min-cpu 30 # 甘めに拾う
    python .claude/tools/orphan_check.py --min-age 60 # 60 分以上のものだけ
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field

import psutil

sys.stdout.reconfigure(encoding="utf-8")

# 生涯平均でこの割合 (1 コア = 100%) を超えていたら「焼いている」とみなす。
# 事故の 2 件が 79% / 284%、正常な常駐サーバーの最大が約 25% だったので 50% に置く。
DEFAULT_MIN_CPU_PCT = 50.0
DEFAULT_MIN_AGE_MIN = 10.0

# 親を持たないのが正常なもの。誤検出を避けるため最初から除外する。
SYSTEM_NAMES = frozenset(
    {
        "System", "System Idle Process", "Idle", "Registry", "MemCompression",
        "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
        "lsass.exe", "svchost.exe", "fontdrvhost.exe", "dwm.exe", "explorer.exe",
        "spoolsv.exe", "SearchIndexer.exe", "MsMpEng.exe", "audiodg.exe",
        "RuntimeBroker.exe", "sihost.exe", "taskhostw.exe", "ctfmon.exe",
    }
)


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
    listening: list[int] = field(default_factory=list)

    @property
    def severity(self) -> str:
        """🔴 = 止めるべき / 🟡 = 一応見ておく。"""
        if self.parent_dead and not self.listening:
            return "RED"
        return "YELLOW"


def listening_map() -> dict[int, list[int]]:
    """PID -> LISTEN ポートの対応表を「1 回で」作る。

    psutil の `Process.net_connections()` を 1 プロセスずつ呼ぶと、Windows では
    毎回コネクションテーブル全体を舐めるため候補数に比例して極端に遅くなる
    (1000 プロセス規模で 2 分以上かかった)。システム全体を 1 回だけ引く。
    """
    ports: dict[int, list[int]] = {}
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, OSError):
        return ports
    for conn in conns:
        if conn.status != psutil.CONN_LISTEN or conn.pid is None:
            continue
        ports.setdefault(conn.pid, []).append(conn.laddr.port)
    return {k: sorted(set(v)) for k, v in ports.items()}


def collect(min_cpu_pct: float, min_age_min: float, sample_sec: float) -> list[Finding]:
    """条件に合うプロセスを集める。現在値を見るため 2 点サンプリングする。"""
    now = time.time()
    alive: set[int] = set(psutil.pids())

    # 1 点目
    first: dict[int, tuple[psutil.Process, float]] = {}
    for proc in psutil.process_iter(["pid", "name", "ppid", "create_time"]):
        try:
            if proc.info["name"] in SYSTEM_NAMES or proc.info["pid"] in (0, 4):
                continue
            age_min = (now - proc.info["create_time"]) / 60.0
            if age_min < min_age_min:
                continue
            times = proc.cpu_times()
            cpu_sec = times.user + times.system
            if cpu_sec / (age_min * 60.0) * 100.0 < min_cpu_pct:
                continue
            first[proc.info["pid"]] = (proc, cpu_sec)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not first:
        return []

    time.sleep(sample_sec)

    ports_by_pid = listening_map()
    findings: list[Finding] = []
    for proc_id, (proc, cpu0) in first.items():
        try:
            times = proc.cpu_times()
            cpu1 = times.user + times.system
            age_min = (time.time() - proc.create_time()) / 60.0
            parent_id = proc.ppid()
            try:
                cmdline = " ".join(proc.cmdline()) or proc.name()
            except (psutil.AccessDenied, OSError):
                cmdline = proc.name()
            findings.append(
                Finding(
                    proc_id=proc_id,
                    name=proc.name(),
                    cmdline=cmdline,
                    age_min=age_min,
                    cpu_sec=cpu1,
                    avg_core_pct=cpu1 / (age_min * 60.0) * 100.0,
                    now_core_pct=(cpu1 - cpu0) / sample_sec * 100.0,
                    parent_dead=parent_id not in alive or parent_id == 0,
                    parent_id=parent_id,
                    listening=ports_by_pid.get(proc_id, []),
                )
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    findings.sort(key=lambda f: (f.severity != "RED", -f.cpu_sec))
    return findings


def _fmt_age(minutes: float) -> str:
    if minutes < 60:
        return f"{minutes:.0f}分"
    if minutes < 60 * 24:
        return f"{minutes / 60:.1f}時間"
    return f"{minutes / 60 / 24:.1f}日"


def report(findings: list[Finding], min_cpu_pct: float, min_age_min: float) -> int:
    """見つかったものを表示する。戻り値は 🔴 の件数。"""
    print(f"条件: 起動から {min_age_min:.0f} 分以上 / 生涯平均 CPU {min_cpu_pct:.0f}% 以上 (1コア=100%)")
    print()

    if not findings:
        print("該当なし。焼き続けているプロセスはありません。")
        return 0

    red = [f for f in findings if f.severity == "RED"]
    yellow = [f for f in findings if f.severity == "YELLOW"]

    if red:
        print("=" * 74)
        print(f"🔴 孤児 + 待ち受けなし ... 止めてよい可能性が高い ({len(red)} 件)")
        print("=" * 74)
        for f in red:
            _print_one(f)

    if yellow:
        print("=" * 74)
        print(f"🟡 参考 ... 待ち受けあり、または親が生存 ({len(yellow)} 件)")
        print("=" * 74)
        for f in yellow:
            _print_one(f)

    if red:
        print("-" * 74)
        print("止める場合 (内容を確認してから実行してください):")
        for f in red:
            print(f"  taskkill /F /T /PID {f.proc_id}")
    return len(red)


def _print_one(f: Finding) -> None:
    mark = "親が消滅" if f.parent_dead else f"親 PID {f.parent_id} は生存"
    ports = f"LISTEN {f.listening}" if f.listening else "待ち受けなし"
    print(f"  PID {f.proc_id}  {f.name}")
    print(f"    経過 {_fmt_age(f.age_min)} / 累積CPU {f.cpu_sec:,.0f}秒 "
          f"/ 生涯平均 {f.avg_core_pct:.0f}% / 現在 {f.now_core_pct:.0f}%")
    print(f"    {mark} / {ports}")
    print(f"    {f.cmdline[:150]}")
    if "python" in f.name.lower():
        print(f"    調べる: py-spy dump --pid {f.proc_id}")
    print()


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
