"""全プロセスの CPU 時間・切り替え回数・親を、NtQuerySystemInformation 1 回で読む(Windows x64)。

psutil は権限の無いプロセスごとにシステム全体の一覧を取り直すため、400 プロセスで
約 2 秒・約 1 コアかかる。こちらは 1 回の呼び出しで全プロセス分が取れ、管理者権限で
動くプロセスも読める。boot_cpu_recorder から使う。
"""

from __future__ import annotations

import ctypes
import struct
from dataclasses import dataclass

SYSTEM_PROCESS_INFORMATION = 5
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
_PROC_FIXED = 256  # SYSTEM_PROCESS_INFORMATION の固定部(x64)
_THREAD_SIZE = 80  # SYSTEM_THREAD_INFORMATION(x64)
_TICKS = 10_000_000  # 100ns 単位 → 秒


@dataclass(frozen=True)
class Snap:
    pid: int
    ppid: int
    name: str
    created: int  # 100ns 単位(PID の使い回しの判定用)
    user_sec: float
    kernel_sec: float
    ctx: int  # 全スレッドの切り替え回数の合計
    threads: int
    handles: int


def _query() -> tuple[ctypes.Array[ctypes.c_char], int]:
    ntdll = ctypes.WinDLL("ntdll")
    size = 1 << 20
    while True:
        buf = ctypes.create_string_buffer(size)
        need = ctypes.c_ulong()
        status = ntdll.NtQuerySystemInformation(SYSTEM_PROCESS_INFORMATION, buf, size, ctypes.byref(need)) & 0xFFFFFFFF
        if status == STATUS_INFO_LENGTH_MISMATCH:
            size = max(size * 2, need.value + 65536)
            continue
        if status != 0:
            raise OSError(f"NtQuerySystemInformation failed: 0x{status:08X}")
        return buf, ctypes.addressof(buf)


def parse(raw: bytes, base_addr: int) -> dict[int, Snap]:
    """バッファを解く。base_addr はバッファ先頭のアドレス(名前の位置を求めるのに使う)。"""
    out: dict[int, Snap] = {}
    off = 0
    while True:
        nxt, nthreads = struct.unpack_from("<II", raw, off)
        created, user, kernel = struct.unpack_from("<qqq", raw, off + 32)
        name_len, _, name_ptr = struct.unpack_from("<HH4xQ", raw, off + 56)
        pid, ppid = struct.unpack_from("<QQ", raw, off + 80)
        (handles,) = struct.unpack_from("<I", raw, off + 96)
        name = "System Idle Process" if pid == 0 else ""
        if name_ptr and name_len:
            start = name_ptr - base_addr
            if 0 <= start <= len(raw) - name_len:
                name = raw[start : start + name_len].decode("utf-16-le", "replace")
        ctx = 0
        t_off = off + _PROC_FIXED
        for i in range(nthreads):
            (switches,) = struct.unpack_from("<I", raw, t_off + i * _THREAD_SIZE + 64)
            ctx += switches
        out[pid] = Snap(
            pid=pid, ppid=ppid, name=name, created=created,
            user_sec=user / _TICKS, kernel_sec=kernel / _TICKS,
            ctx=ctx, threads=nthreads, handles=handles,
        )
        if nxt == 0:
            return out
        off += nxt


def snapshot() -> dict[int, Snap]:
    buf, addr = _query()
    return parse(buf.raw, addr)
