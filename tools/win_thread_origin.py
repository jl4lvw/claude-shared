"""プロセスの各スレッドが、どの部品(DLL)のどこから始まったかを調べる(Windows・読むだけ)。

boot_cpu_recorder が、空回りしているプロセスの重いスレッドの正体を残すために使う。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass

import psutil

THREAD_QUERY_INFORMATION = 0x0040
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
THREAD_QUERY_WIN32_START_ADDRESS = 9
LIST_MODULES_ALL = 3
WMI_MODULES = ("_wmi.pyd", "fastprox.dll", "wbemprox.dll", "wbemcomn.dll", "wbemsvc.dll")

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenThread.restype = wintypes.HANDLE
_k32.OpenProcess.restype = wintypes.HANDLE
_ntdll = ctypes.WinDLL("ntdll")
_psapi = ctypes.WinDLL("psapi")


class _ModuleInfo(ctypes.Structure):
    _fields_ = [("lpBaseOfDll", ctypes.c_void_p), ("SizeOfImage", wintypes.DWORD), ("EntryPoint", ctypes.c_void_p)]


@dataclass(frozen=True)
class Module:
    base: int
    size: int
    name: str


@dataclass(frozen=True)
class ThreadUse:
    tid: int
    user_cores: float
    kernel_cores: float
    origin: str  # 例: combase.dll+0x1121e0


def list_modules(pid: int) -> list[Module]:
    h = _k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return []
    try:
        arr = (ctypes.c_void_p * 4096)()
        need = wintypes.DWORD()
        if not _psapi.EnumProcessModulesEx(h, arr, ctypes.sizeof(arr), ctypes.byref(need), LIST_MODULES_ALL):
            return []
        out: list[Module] = []
        for i in range(min(need.value // ctypes.sizeof(ctypes.c_void_p), len(arr))):
            mi = _ModuleInfo()
            name = ctypes.create_unicode_buffer(260)
            _psapi.GetModuleInformation(h, ctypes.c_void_p(arr[i]), ctypes.byref(mi), ctypes.sizeof(mi))
            _psapi.GetModuleBaseNameW(h, ctypes.c_void_p(arr[i]), name, 260)
            out.append(Module(base=mi.lpBaseOfDll or 0, size=mi.SizeOfImage, name=name.value))
        return out
    finally:
        _k32.CloseHandle(h)


def thread_start_address(tid: int) -> int:
    h = _k32.OpenThread(THREAD_QUERY_INFORMATION, False, tid)
    if not h:
        return 0
    try:
        addr = ctypes.c_void_p()
        _ntdll.NtQueryInformationThread(h, THREAD_QUERY_WIN32_START_ADDRESS, ctypes.byref(addr), ctypes.sizeof(addr), None)
        return addr.value or 0
    finally:
        _k32.CloseHandle(h)


def locate(addr: int, modules: list[Module]) -> str:
    """アドレスを「部品名+ずれ」にする。どの部品にも入らなければ生のアドレス。"""
    for m in modules:
        if m.base <= addr < m.base + m.size:
            return f"{m.name}+0x{addr - m.base:x}"
    return f"0x{addr:x}" if addr else "(不明)"


def hot_threads(pid: int, window: float = 1.0, top: int = 10) -> tuple[list[ThreadUse], list[str]]:
    """window 秒の間に CPU を使ったスレッドを重い順に返す。あわせて読み込み中の WMI 関係の部品名。"""
    p = psutil.Process(pid)
    before = {t.id: t for t in p.threads()}
    time.sleep(window)
    after = p.threads()
    modules = list_modules(pid)
    uses: list[ThreadUse] = []
    for t in after:
        o = before.get(t.id)
        if o is None:
            continue
        uses.append(
            ThreadUse(
                tid=t.id,
                user_cores=round(max(0.0, t.user_time - o.user_time) / window, 2),
                kernel_cores=round(max(0.0, t.system_time - o.system_time) / window, 2),
                origin=locate(thread_start_address(t.id), modules),
            )
        )
    uses.sort(key=lambda u: u.user_cores + u.kernel_cores, reverse=True)
    wmi = sorted({m.name for m in modules if m.name.lower() in WMI_MODULES})
    return uses[:top], wmi
