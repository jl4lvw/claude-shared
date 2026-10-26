"""起動直後の CPU 記録係: 再起動のあと CPU を食い続けるプロセスの証拠を残す。

2026-10-26 導入。再起動の直後に、毎回違う PWA サーバーが何もしていないのに
カーネル側で空回りし(1 本あたり約 2 コア・切り替え毎秒約 5,000 回)、CPU が 100% に
張り付く現象が 3 回中 2 回起きた。既存の CpuUsageSnapshot はプロセス名の番号で数値を
割り当てるため、プロセスが入れ替わると別の PID に取り違え、犯人を特定できなかった。

この道具はログオンから DURATION_SEC の間だけ動き、次を残す(読むだけ。何も止めない):
- samples.jsonl  5 秒ごと: CPU 全体の内訳(ユーザー/カーネル/割り込み/DPC)と、
                 PID ごとに実測した CPU(ユーザー/カーネル別)・切り替え回数の上位
- spawns.jsonl   1 秒ごと: 新しく生まれたプロセスと親(git のような短命なものの起動元)
- suspects.jsonl 0.5 コア以上が 15 秒続いたプロセスの詳細(コマンドライン・親・ポート・
                 通信・スレッド・py-spy のスタック)。後から止めても証拠が残る
- summary.txt    終了時の累計の上位

使い方:
    pythonw boot_cpu_recorder.py                  # タスク BootCpuRecorder から(ログオン時)
    python  boot_cpu_recorder.py --duration 60    # 試しに 60 秒だけ
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from win_proc_snapshot import Snap, snapshot

OUT_ROOT = Path(r"C:\ClaudeCode\.claude\incidents\logs\boot_cpu")  # logs は /g-ul の対象外
PY_SPY = Path(r"C:\Users\user\AppData\Local\Programs\Python\Python312\Scripts\py-spy.exe")
DURATION_SEC = 40 * 60
SAMPLE_SEC = 5
SPAWN_POLL_SEC = 1.0
TOP_N = 15
SUSPECT_CORES = 0.5  # これ以上を
SUSPECT_STREAK = 3  # 連続 3 回(15 秒)続けたら詳細を取る
SUSPECT_REPEAT_SEC = 300  # 同じ PID はこの間隔より頻繁に取らない
KEEP_RUNS = 10
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class Usage:
    pid: int
    name: str
    user_cores: float
    kernel_cores: float
    ctx_per_sec: float

    @property
    def cores(self) -> float:
        return self.user_cores + self.kernel_cores


@dataclass
class SuspectTracker:
    """0.5 コア以上が何回続いたかを数え、詳細を取る PID を決める。"""

    streak: Counter[int] = field(default_factory=Counter)
    last_dump: dict[int, float] = field(default_factory=dict)

    def update(self, usages: list[Usage], now: float) -> list[Usage]:
        hot = {u.pid for u in usages if u.cores >= SUSPECT_CORES}
        for pid in list(self.streak):
            if pid not in hot:
                del self.streak[pid]
        due: list[Usage] = []
        for u in usages:
            if u.pid not in hot:
                continue
            self.streak[u.pid] += 1
            if self.streak[u.pid] >= SUSPECT_STREAK and now - self.last_dump.get(u.pid, -1e9) >= SUSPECT_REPEAT_SEC:
                self.last_dump[u.pid] = now
                due.append(u)
        return due


def diff_usage(prev: dict[int, Snap], cur: dict[int, Snap], interval: float) -> list[Usage]:
    """前回からの差で PID ごとの CPU を出す。PID の使い回しは起動時刻で見分ける。"""
    usages: list[Usage] = []
    for pid, st in cur.items():
        old = prev.get(pid)
        if old is None or old.created != st.created or pid == 0:
            continue
        usages.append(
            Usage(
                pid=pid,
                name=st.name or "?",
                user_cores=max(0.0, (st.user_sec - old.user_sec) / interval),
                kernel_cores=max(0.0, (st.kernel_sec - old.kernel_sec) / interval),
                ctx_per_sec=max(0.0, (st.ctx - old.ctx) / interval),
            )
        )
    usages.sort(key=lambda u: u.cores, reverse=True)
    return usages


def describe(pid: int) -> dict[str, object]:
    """怪しいプロセスの詳細。途中で消えても取れた分だけ返す。"""
    info: dict[str, object] = {"pid": pid}
    try:
        p = psutil.Process(pid)
        info["name"] = p.name()
        info["created"] = dt.datetime.fromtimestamp(p.create_time()).isoformat(timespec="seconds")
        for key, fn in (("cmdline", p.cmdline), ("cwd", p.cwd), ("exe", p.exe), ("nice", p.nice), ("threads", p.num_threads)):
            try:
                info[key] = fn()
            except (psutil.Error, OSError) as e:
                info[key] = f"({type(e).__name__})"
        chain = []
        for a in p.parents()[:4]:
            try:
                chain.append(f"{a.name()}({a.pid}) {' '.join(a.cmdline())[:200]}")
            except psutil.Error:
                chain.append(f"?({a.pid})")
        info["parents"] = chain
        try:
            conns = p.net_connections("all")
            info["listen"] = sorted({c.laddr.port for c in conns if c.status == psutil.CONN_LISTEN})
            info["conn_status"] = dict(Counter(c.status for c in conns))
            info["conn_sample"] = [f"{c.type.name} {c.laddr} -> {c.raddr or '-'} {c.status}" for c in conns[:20]]
        except (psutil.Error, OSError) as e:
            info["listen"] = f"({type(e).__name__})"
        try:
            ths = sorted(p.threads(), key=lambda t: t.user_time + t.system_time, reverse=True)[:5]
            info["top_threads"] = [(t.id, round(t.user_time, 1), round(t.system_time, 1)) for t in ths]
        except (psutil.Error, OSError):
            pass
        if info.get("name", "").lower().startswith("python"):
            info["py_spy"] = py_spy_dump(pid)
            # 空回りは Python 側では「待機中」に見えるので、C 側のどの呼び出しで回っているかも取る
            info["py_spy_native"] = py_spy_dump(pid, native=True)
    except psutil.Error as e:
        info["error"] = type(e).__name__
    return info


def py_spy_dump(pid: int, native: bool = False) -> str:
    if not PY_SPY.exists():
        return "(py-spy なし)"
    args = [str(PY_SPY), "dump", "--pid", str(pid)] + (["--native"] if native else ["--nonblocking"])
    try:
        r = subprocess.run(
            args,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20, creationflags=_NO_WINDOW,
        )
        return (r.stdout + r.stderr)[-6000:]
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"({type(e).__name__})"


def spawn_label(st: Snap, procs: dict[int, Snap]) -> str:
    parent = procs.get(st.ppid)
    return f"{st.name} <- {parent.name if parent else '(終了済み)'}"


def try_cmdline(pid: int) -> str:
    try:
        return " ".join(psutil.Process(pid).cmdline())[:300]
    except (psutil.Error, OSError):
        return ""


class Recorder:
    def __init__(self, out_dir: Path) -> None:
        self.out = out_dir
        self.out.mkdir(parents=True, exist_ok=True)
        self.files = {k: (self.out / f"{k}.jsonl").open("a", encoding="utf-8", newline="") for k in ("samples", "spawns", "suspects")}
        self.total = Counter()  # (pid, name) -> 累計コア秒
        self.cmds: dict[int, str] = {}
        self.spawn_count: Counter[str] = Counter()

    def write(self, kind: str, rec: dict[str, object]) -> None:
        f = self.files[kind]
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()

    def close(self) -> None:
        for f in self.files.values():
            f.close()


def write_summary(rec: Recorder, started: dt.datetime) -> None:
    lines = [f"開始 {started:%Y-%m-%d %H:%M:%S} / 終了 {dt.datetime.now():%H:%M:%S}", "", "== 累計 CPU の上位(コア秒)"]
    for (pid, name), sec in rec.total.most_common(20):
        lines.append(f"{sec:9.1f}  {name}({pid})  {rec.cmds.get(pid, '')[:160]}")
    lines += ["", "== 起動回数の多いプロセス(名前 <- 親)"]
    for k, n in rec.spawn_count.most_common(20):
        lines.append(f"{n:6d}  {k}")
    (rec.out / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")


def prune_old_runs(root: Path, keep: int) -> None:
    runs = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.name)
    for d in runs[:-keep]:
        shutil.rmtree(d, ignore_errors=True)


def run(duration: float) -> int:
    try:
        psutil.Process().nice(psutil.ABOVE_NORMAL_PRIORITY_CLASS)  # 100% でも記録が止まらないように
    except psutil.Error:
        pass
    started = dt.datetime.now()
    boot = dt.datetime.fromtimestamp(psutil.boot_time())
    rec = Recorder(OUT_ROOT / f"{started:%Y%m%d_%H%M%S}")
    rec.write("samples", {"ts": started.isoformat(timespec="seconds"), "event": "start", "boot": boot.isoformat(timespec="seconds"), "cpus": psutil.cpu_count()})
    tracker = SuspectTracker()
    psutil.cpu_times_percent(None)
    prev = last = snapshot()
    last_sample = time.monotonic()
    end = last_sample + duration
    try:
        while time.monotonic() < end:
            time.sleep(SPAWN_POLL_SEC)
            procs = snapshot()
            for pid, st in procs.items():
                old = last.get(pid)
                if old is not None and old.created == st.created:
                    continue
                label = spawn_label(st, procs)
                rec.spawn_count[label] += 1
                rec.write("spawns", {"ts": f"{dt.datetime.now():%H:%M:%S}", "pid": pid, "who": label, "ppid": st.ppid, "cmd": try_cmdline(pid)})
            last = procs
            now = time.monotonic()
            if now - last_sample < SAMPLE_SEC:
                continue
            interval, last_sample = now - last_sample, now
            usages, prev = diff_usage(prev, procs, interval), procs
            for u in usages[:5]:
                if u.pid not in rec.cmds:
                    rec.cmds[u.pid] = try_cmdline(u.pid)
            cpu = psutil.cpu_times_percent(None)
            ncpu = psutil.cpu_count() or 1
            attributed = sum(u.cores for u in usages) / ncpu * 100
            busy = 100 - cpu.idle
            for u in usages:
                rec.total[(u.pid, u.name)] += u.cores * interval
            rec.write("samples", {
                "ts": f"{dt.datetime.now():%H:%M:%S}",
                "busy": round(busy, 1), "user": round(cpu.user, 1), "kernel": round(cpu.system, 1),
                "interrupt": round(getattr(cpu, "interrupt", 0.0), 1), "dpc": round(getattr(cpu, "dpc", 0.0), 1),
                "unattributed": round(max(0.0, busy - attributed), 1),
                "top": [[u.pid, u.name, round(u.user_cores, 2), round(u.kernel_cores, 2), int(u.ctx_per_sec)] for u in usages[:TOP_N]],
            })
            for u in tracker.update(usages[:TOP_N], now):
                detail = describe(u.pid)
                detail.update({"ts": f"{dt.datetime.now():%H:%M:%S}", "user_cores": round(u.user_cores, 2), "kernel_cores": round(u.kernel_cores, 2), "ctx_per_sec": int(u.ctx_per_sec)})
                rec.write("suspects", detail)
    finally:
        write_summary(rec, started)
        rec.close()
        prune_old_runs(OUT_ROOT, KEEP_RUNS)
    return 0


def main() -> int:
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--duration", type=float, default=DURATION_SEC, help="記録する秒数")
    args = ap.parse_args()
    os.makedirs(OUT_ROOT, exist_ok=True)
    return run(args.duration)


if __name__ == "__main__":
    sys.exit(main())
