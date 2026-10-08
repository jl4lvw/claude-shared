"""bo_setkey.py のテスト（実APIには接続しない・実際の鍵ファイルは触らない）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import bo_setkey  # noqa: E402

GOOD = "A" * 43


def test_normalize_key_rejects_bad_input() -> None:
    assert bo_setkey.normalize_key("  " + GOOD + "\r\n") == GOOD
    for bad in ("", "short", "has space " + GOOD, "BOXQR_ORDER_API_KEY=" + GOOD):
        with pytest.raises(bo_setkey.SetKeyError):
            bo_setkey.normalize_key(bad)


def test_validate_path_rejects_project(tmp_path: Path) -> None:
    with pytest.raises(bo_setkey.SetKeyError):
        bo_setkey.validate_path(Path(r"C:\ClaudeCode\900.ClaudeCode\key.txt"))
    bo_setkey.validate_path(tmp_path / "k.txt")   # 外なら通る


def test_write_key_and_overwrite_guard(tmp_path: Path) -> None:
    p = tmp_path / "d" / "k.txt"
    bo_setkey.write_key(GOOD, p)
    assert p.read_text(encoding="utf-8") == GOOD + "\n"
    with pytest.raises(bo_setkey.SetKeyError):
        bo_setkey.write_key("B" * 43, p)
    bo_setkey.write_key("B" * 43, p, force=True)
    assert p.read_text(encoding="utf-8").strip() == "B" * 43


def test_status_hides_value(tmp_path: Path) -> None:
    p = tmp_path / "k.txt"
    assert "なし" in "\n".join(bo_setkey.status(p))
    bo_setkey.write_key(GOOD, p)
    out = "\n".join(bo_setkey.status(p))
    assert GOOD not in out and "43文字" in out


def test_main_stdin_writes_without_echo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    p = tmp_path / "k.txt"
    monkeypatch.setenv("BOXQR_KEY_FILE", str(p))
    monkeypatch.setattr(bo_setkey, "restrict_acl", lambda path: "acl-skipped")
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(GOOD + "\n"))
    assert bo_setkey.main(["--stdin"]) == 0
    assert p.read_text(encoding="utf-8").strip() == GOOD
    assert GOOD not in capsys.readouterr().out
