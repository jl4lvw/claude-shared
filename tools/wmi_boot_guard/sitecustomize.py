# CLAUDE-WMI-BOOT-GUARD v1 (正本: C:\ClaudeCode\.claude\tools\wmi_boot_guard\sitecustomize.py)
# 入れ外しは C:\ClaudeCode\.claude\tools\wmi_boot_guard\install.py (install / uninstall / status)
#
# パソコンの起動から 15 分以内に起動した Python では、platform の WMI 問い合わせを使わない。
# 2026-10-26: 再起動の直後、30 本近い PWA サーバーが一斉に起動して import sqlalchemy
# → platform.machine() → WMI に問い合わせ、起動したばかりの WMI に詰まった数本が
# カーネル側で空回りし(1 本 1〜2 コア)、CPU が 100% に張り付いた。
# platform は WMI が使えないと環境変数とレジストリから同じ情報を取るので、結果は変わらない。
#
# 止めたいとき: 環境変数 CLAUDE_WMI_BOOT_GUARD=0
# 時間を変えたいとき: 環境変数 CLAUDE_WMI_BOOT_GUARD_MINUTES=分
# ここで失敗しても Python の起動は止めない(例外はすべて握りつぶす)。

LIMIT_MINUTES_DEFAULT = 15


def _uptime_ms() -> int:
    import ctypes

    f = ctypes.windll.kernel32.GetTickCount64
    f.restype = ctypes.c_ulonglong
    return f()


def _should_block(uptime_ms: int, env: dict) -> bool:
    if env.get("CLAUDE_WMI_BOOT_GUARD", "1") == "0":
        return False
    try:
        minutes = float(env.get("CLAUDE_WMI_BOOT_GUARD_MINUTES", LIMIT_MINUTES_DEFAULT))
    except ValueError:
        minutes = LIMIT_MINUTES_DEFAULT
    return uptime_ms < minutes * 60_000


def _make_blocked_wmi():  # -> types.ModuleType(起動を軽くするため typing は読まない)
    import types

    mod = types.ModuleType("_wmi")
    mod.__doc__ = "CLAUDE-WMI-BOOT-GUARD: 起動直後のため WMI を使わない代わりの _wmi"

    def exec_query(query: str) -> str:
        raise OSError("WMI は起動直後のため使わない (CLAUDE-WMI-BOOT-GUARD)")

    mod.exec_query = exec_query
    mod.CLAUDE_WMI_BOOT_GUARD = True
    return mod


def _install() -> None:
    import os
    import sys

    if sys.platform != "win32" or "_wmi" in sys.modules:
        return
    if _should_block(_uptime_ms(), os.environ):
        sys.modules["_wmi"] = _make_blocked_wmi()


try:
    _install()
except Exception:  # noqa: BLE001 - Python の起動を絶対に止めない
    pass
