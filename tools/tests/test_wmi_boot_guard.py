"""wmi_boot_guard の判定と、入れ外しの道具の試験。実際の Python には入れない。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

GUARD_DIR = Path(__file__).resolve().parents[1] / "wmi_boot_guard"


def load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, GUARD_DIR / file)
    mod = importlib.util.module_from_spec(spec)
    saved = sys.modules.get("_wmi")
    spec.loader.exec_module(mod)
    if saved is not None:
        sys.modules["_wmi"] = saved  # 読み込みの副作用でこの試験プロセスの _wmi を変えない
    else:
        sys.modules.pop("_wmi", None)
    return mod


sc = load("guard_sitecustomize", "sitecustomize.py")
inst = load("guard_install", "install.py")


def test_blocks_only_within_window() -> None:
    assert sc._should_block(5 * 60_000, {}) is True
    assert sc._should_block(16 * 60_000, {}) is False


def test_env_can_disable_and_change_window() -> None:
    assert sc._should_block(1000, {"CLAUDE_WMI_BOOT_GUARD": "0"}) is False
    assert sc._should_block(20 * 60_000, {"CLAUDE_WMI_BOOT_GUARD_MINUTES": "30"}) is True
    assert sc._should_block(1000, {"CLAUDE_WMI_BOOT_GUARD_MINUTES": "abc"}) is True  # 壊れた値は既定の 15 分


def test_blocked_wmi_raises_oserror() -> None:
    mod = sc._make_blocked_wmi()
    with pytest.raises(OSError):
        mod.exec_query("SELECT Architecture FROM Win32_Processor")


def test_install_and_uninstall_own_file(tmp_path: Path) -> None:
    target = tmp_path / "sitecustomize.py"
    assert inst.status(target) == "未導入"
    assert inst.install(target) == 0
    assert inst.status(target) == "導入済み(最新)"
    assert inst.uninstall(target) == 0
    assert not target.exists()


def test_never_touch_foreign_sitecustomize(tmp_path: Path) -> None:
    target = tmp_path / "sitecustomize.py"
    target.write_text("print('someone else')\n", encoding="utf-8")
    assert inst.install(target) == 2
    assert inst.uninstall(target) == 2
    assert target.read_text(encoding="utf-8") == "print('someone else')\n"
