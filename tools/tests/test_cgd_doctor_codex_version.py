"""cgd_doctor の codex CLI 版チェック(デスクトップ版との版ずれ検出)のテスト。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cgd_doctor  # noqa: E402


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("codex-cli 0.159.2", (0, 159, 2)),
        ("codex-cli 0.154.0\n", (0, 154, 0)),
        ("", None),
        ("unknown", None),
    ],
)
def test_parse_version(text: str, expected: tuple[int, ...] | None) -> None:
    assert cgd_doctor._parse_version(text) == expected


def _fake_cli(monkeypatch: pytest.MonkeyPatch, cli: str, desktop: str | None) -> None:
    monkeypatch.setattr(cgd_doctor.shutil, "which", lambda _: "codex")
    monkeypatch.setattr(
        cgd_doctor.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=cli + "\n", stderr=""),
    )
    monkeypatch.setattr(cgd_doctor, "_desktop_codex_version", lambda: desktop)


def test_cli_older_than_desktop_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_cli(monkeypatch, "codex-cli 0.154.0", "codex-cli 0.159.2")
    status, name, detail = cgd_doctor.check_codex_version()
    assert status == cgd_doctor.WARN
    assert "@openai/codex@0.159.2" in detail


def test_cli_same_as_desktop_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_cli(monkeypatch, "codex-cli 0.159.2", "codex-cli 0.159.2")
    status, _, detail = cgd_doctor.check_codex_version()
    assert status == cgd_doctor.OK
    assert "デスクトップ" in detail


def test_no_desktop_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_cli(monkeypatch, "codex-cli 0.154.0", None)
    status, _, detail = cgd_doctor.check_codex_version()
    assert status == cgd_doctor.OK
    assert detail == "codex-cli 0.154.0"
