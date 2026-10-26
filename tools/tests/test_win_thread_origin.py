"""win_thread_origin の試験。自分自身のプロセスで、忙しいスレッドを見分けられるかを見る。"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import win_thread_origin as w  # noqa: E402


def test_locate_inside_and_outside() -> None:
    mods = [w.Module(base=0x1000, size=0x100, name="a.dll")]
    assert w.locate(0x1010, mods) == "a.dll+0x10"
    assert w.locate(0x5000, mods) == "0x5000"
    assert w.locate(0, mods) == "(不明)"


def test_hot_threads_finds_busy_thread() -> None:
    stop = time.monotonic() + 2.5

    def spin() -> None:
        while time.monotonic() < stop:
            pass

    th = threading.Thread(target=spin)
    th.start()
    time.sleep(0.2)
    uses, _ = w.hot_threads(os.getpid(), window=1.0)
    th.join()
    assert uses and uses[0].tid == th.native_id
    assert uses[0].user_cores > 0.3
    assert uses[0].origin.lower().startswith("ucrtbase.dll+")  # Python のスレッドは C ランタイムから始まる
