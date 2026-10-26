"""boot_cpu_recorder の判定(どのプロセスの詳細を取るか)の試験。実プロセスは触らない。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boot_cpu_recorder as r  # noqa: E402


def usage(pid: int, cores: float) -> r.Usage:
    return r.Usage(pid=pid, name="python.exe", user_cores=cores * 0.2, kernel_cores=cores * 0.8, ctx_per_sec=5000)


def test_suspect_after_three_hot_samples() -> None:
    t = r.SuspectTracker()
    assert t.update([usage(1, 1.9)], 0) == []
    assert t.update([usage(1, 1.9)], 5) == []
    assert [u.pid for u in t.update([usage(1, 1.9)], 10)] == [1]


def test_streak_resets_when_cool() -> None:
    t = r.SuspectTracker()
    t.update([usage(1, 1.0)], 0)
    t.update([usage(1, 1.0)], 5)
    t.update([usage(1, 0.1)], 10)
    assert t.update([usage(1, 1.0)], 15) == []


def test_same_pid_not_dumped_again_within_repeat_interval() -> None:
    t = r.SuspectTracker()
    for i in range(3):
        due = t.update([usage(1, 2.0)], i * 5)
    assert [u.pid for u in due] == [1]
    assert t.update([usage(1, 2.0)], 15) == []
    assert [u.pid for u in t.update([usage(1, 2.0)], 10 + r.SUSPECT_REPEAT_SEC)] == [1]


def test_cores_is_user_plus_kernel() -> None:
    u = r.Usage(pid=1, name="x", user_cores=0.3, kernel_cores=1.5, ctx_per_sec=0)
    assert abs(u.cores - 1.8) < 1e-9


def test_prune_keeps_newest(tmp_path: Path) -> None:
    for name in ("20261026_100000", "20261026_110000", "20261026_120000"):
        (tmp_path / name).mkdir()
    r.prune_old_runs(tmp_path, 2)
    assert sorted(d.name for d in tmp_path.iterdir()) == ["20261026_110000", "20261026_120000"]


def snap(pid: int, user: float, kernel: float, ctx: int, created: int = 1) -> r.Snap:
    return r.Snap(pid=pid, ppid=0, name="python.exe", created=created, user_sec=user, kernel_sec=kernel, ctx=ctx, threads=1, handles=1)


def test_diff_usage_per_pid_and_pid_reuse() -> None:
    prev = {10: snap(10, 1.0, 1.0, 100), 20: snap(20, 5.0, 5.0, 0)}
    cur = {10: snap(10, 2.0, 9.0, 25100), 20: snap(20, 0.1, 0.1, 0, created=2), 30: snap(30, 9.0, 9.0, 0)}
    us = r.diff_usage(prev, cur, 5.0)
    assert [u.pid for u in us] == [10]  # 20 は PID の使い回し、30 は新顔なので差を出さない
    assert abs(us[0].user_cores - 0.2) < 1e-9 and abs(us[0].kernel_cores - 1.6) < 1e-9
    assert us[0].ctx_per_sec == 5000
