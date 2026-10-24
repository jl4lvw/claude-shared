"""autosend_guard.decide のテスト(080 の data を Claude のツールから守る PreToolUse フック)。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hooks"))
from autosend_guard import decide  # noqa: E402

BASE = "C:/ClaudeCode/080.メール自動送信"


def _denied(tool: str, tool_input: dict) -> bool:
    result = decide(tool, tool_input)
    return bool(result) and result["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("path", [
    f"{BASE}/data/secret.key",
    f"{BASE}/data/logs/../secret.key",          # .. で logs から抜ける
    f"{BASE}/data/rejected/../../data/ledger.sqlite3",
    f"{BASE}/DATA/secret.key",                  # 大文字
    f"{BASE}/Data/Logs/../config.json",
    "C:\\ClaudeCode\\080.メール自動送信\\data\\logs\\..\\secret.key",
    "C:/ClaudeCode/077.メール自動送信/data/secret.key",
])
def test_read_outside_logs_is_denied(path: str) -> None:
    assert _denied("Read", {"file_path": path})


@pytest.mark.parametrize("path", [
    f"{BASE}/data/logs/host.log",
    f"{BASE}/DATA/LOGS/host.log",
    f"{BASE}/data/rejected/x.zip",
    f"{BASE}/autosend/watcher.py",
    "C:/ClaudeCode/041.Claude間連携API/data/x.json",
])
def test_allowed_reads(path: str) -> None:
    assert decide("Read", {"file_path": path}) is None


@pytest.mark.parametrize("tool", ["Write", "Edit"])
def test_write_to_logs_is_denied(tool: str) -> None:
    assert _denied(tool, {"file_path": f"{BASE}/data/logs/host.log"})
    assert _denied(tool, {"file_path": f"{BASE}/data/logs/../secret.key"})


def test_grep_data_and_project_root_denied() -> None:
    assert _denied("Grep", {"path": f"{BASE}/data", "pattern": "x"})
    assert _denied("Grep", {"path": f"{BASE}/data/logs/..", "pattern": "x"})
    assert _denied("Grep", {"path": BASE, "pattern": "x"})
    assert _denied("Grep", {"path": BASE + "/", "pattern": "x"})


def test_grep_allowed() -> None:
    assert decide("Grep", {"path": f"{BASE}/autosend", "pattern": "x"}) is None
    assert decide("Grep", {"path": f"{BASE}/data/logs", "pattern": "x"}) is None
    assert decide("Grep", {"pattern": "x"}) is None


def test_bash_unchanged() -> None:
    assert _denied("Bash", {"command": "type secret.key"})
    assert decide("Bash", {"command": "python cli.py status"}) is None
