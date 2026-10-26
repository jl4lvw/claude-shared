"""win_proc_snapshot.parse の試験。NtQuerySystemInformation の形を作って解く。"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import win_proc_snapshot as w  # noqa: E402

BASE = 0x10000


def entry(pid: int, ppid: int, name: str, user: int, kernel: int, switches: list[int], name_off: int, nxt: int) -> bytearray:
    b = bytearray(w._PROC_FIXED + w._THREAD_SIZE * len(switches))
    struct.pack_into("<II", b, 0, nxt, len(switches))
    struct.pack_into("<qqq", b, 32, 777, user, kernel)
    struct.pack_into("<HH4xQ", b, 56, len(name) * 2, len(name) * 2, BASE + name_off)
    struct.pack_into("<QQ", b, 80, pid, ppid)
    struct.pack_into("<I", b, 96, 42)
    for i, s in enumerate(switches):
        struct.pack_into("<I", b, w._PROC_FIXED + i * w._THREAD_SIZE + 64, s)
    return b


def test_parse_two_processes() -> None:
    first_len = w._PROC_FIXED + w._THREAD_SIZE * 2
    second_len = w._PROC_FIXED + w._THREAD_SIZE
    names_at = first_len + second_len
    raw = entry(100, 4, "git.exe", 20_000_000, 5_000_000, [3, 4], names_at, first_len)
    raw += entry(200, 100, "conhost.exe", 0, 10_000_000, [7], names_at + 14, 0)
    raw += "git.exe".encode("utf-16-le") + "conhost.exe".encode("utf-16-le")
    out = w.parse(bytes(raw), BASE)
    assert out[100] == w.Snap(pid=100, ppid=4, name="git.exe", created=777, user_sec=2.0, kernel_sec=0.5, ctx=7, threads=2, handles=42)
    assert out[200].name == "conhost.exe" and out[200].ppid == 100 and out[200].kernel_sec == 1.0 and out[200].ctx == 7
