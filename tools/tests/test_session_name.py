"""session_name.py (セッション名の生成・検証・台帳) と session_name_prompt.py のテスト。

なぜ必要か (2026-09-21):
  名前の通し番号は「数字+記号ごとの最大値+1」で、4 からのハンドオフで 5 が既にあれば 6 になる。
  ハンドオフ側 (`次セッション名:` 行) と通常セッション側 (台帳) の両方を数えないと、
  番号が被る。書式の検証が緩いと、旧形式の名前が最大値に混ざって番号が狂う。

実行方法:
    python -m pytest .claude/tools/tests/test_session_name.py -v
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / ".claude" / "tools"))
sys.path.insert(0, str(_ROOT / ".claude" / "hooks"))

import session_name as sn  # noqa: E402
import session_name_prompt as prompt  # noqa: E402


def _handoff(dir_: Path, stamp: str, name: str | None) -> None:
    head = "# セッション引継ぎ — テスト\n\n"
    if name is not None:
        head += f"次セッション名: {name}\n"
    (dir_ / f"SESSION_{stamp}.md").write_text(head + "\n本文\n", encoding="utf-8")


# ---------------------------------------------------------------- 端末記号

@pytest.mark.parametrize(
    ("host", "expected"),
    [("DESKTOP-7OSURHD", "★"), ("desktop-7osurhd", "★"), ("RYZEN7-5800X", "■"), ("PC-FF11", "●"), ("OTHER-PC", None)],
)
def test_detect_symbol(host: str, expected: str | None) -> None:
    assert sn.detect_symbol(host) == expected


# ---------------------------------------------------------------- 書式

def test_parse_with_and_without_folder() -> None:
    assert sn.parse_name("072★写真アルバム 4") == sn.ParsedName("072", "★", "写真アルバム", 4)
    assert sn.parse_name("★セッション命名 1") == sn.ParsedName("", "★", "セッション命名", 1)


def test_parse_topic_may_contain_spaces_and_digits() -> None:
    p = sn.parse_name("042■Lv0 委譲 12")
    assert p is not None and (p.topic, p.serial) == ("Lv0 委譲", 12)


@pytest.mark.parametrize("name", ["写真アルバム", "072 写真アルバム 4", "072★写真アルバム", "072★ 4", "07★A 1"])
def test_parse_rejects_old_or_broken(name: str) -> None:
    assert sn.parse_name(name) is None


def test_problems() -> None:
    assert sn.problems("072★写真アルバム 4") == []
    assert any("上限" in p for p in sn.problems("072★" + "あ" * 40 + " 1"))
    assert any("制御文字" in p for p in sn.problems("072★写真\nアルバム 4"))
    assert sn.problems("")


# ---------------------------------------------------------------- 通し番号

def test_next_serial_is_max_plus_one_per_folder_and_symbol() -> None:
    names = ["072★写真アルバム 3", "072★写真登録 5", "072■写真アルバム 9", "042★別件 7", "旧形式の名前"]
    assert sn.next_serial("072", "★", names) == 6  # 名前が違っても同じ (数字, 記号) なら通し
    assert sn.next_serial("072", "■", names) == 10
    assert sn.next_serial("072", "●", names) == 1
    assert sn.next_serial("", "★", names) == 1  # 数字省略は別の範囲


def test_handoff_from_4_when_5_exists_gets_6(tmp_path: Path) -> None:
    _handoff(tmp_path, "20260921_1000_111", "072★写真アルバム 4")
    _handoff(tmp_path, "20260921_1100_222", "072★写真アルバム 5")  # 4 から作った 5
    assert sn.suggest("072", "写真アルバム", tmp_path, "★") == "072★写真アルバム 6"


def test_counts_both_handoff_names_and_ledger(tmp_path: Path) -> None:
    _handoff(tmp_path, "20260921_1000_111", "072★写真アルバム 2")
    assert sn.register("072★写真アルバム 3", tmp_path) is True
    assert sn.suggest("072", "写真アルバム", tmp_path, "★") == "072★写真アルバム 4"


def test_old_format_handoff_name_is_ignored(tmp_path: Path) -> None:
    _handoff(tmp_path, "20260921_1000_111", "丹青社売上連携 工程3〜6")
    _handoff(tmp_path, "20260921_1001_222", None)  # 名前行なし (旧形式ファイル)
    assert sn.suggest("072", "写真", tmp_path, "★") == "072★写真 1"


def test_name_line_only_read_from_head(tmp_path: Path) -> None:
    body = "# t\n\n" + "行\n" * 20 + "次セッション名: 072★深すぎる 9\n"
    (tmp_path / "SESSION_x.md").write_text(body, encoding="utf-8")
    assert sn.read_handoff_names(tmp_path) == []


# ---------------------------------------------------------------- 台帳

def test_register_rejects_duplicate_and_invalid(tmp_path: Path) -> None:
    assert sn.register("072★写真アルバム 1", tmp_path) is True
    assert sn.register("072★写真アルバム 1", tmp_path) is False
    with pytest.raises(ValueError):
        sn.register("写真アルバム", tmp_path)
    assert (tmp_path / sn.LEDGER_NAME).read_text(encoding="utf-8").count("\n") == 1


def test_register_duplicate_of_handoff_name(tmp_path: Path) -> None:
    _handoff(tmp_path, "20260921_1000_111", "072★写真アルバム 4")
    assert sn.register("072★写真アルバム 4", tmp_path) is False


def test_lock_is_released(tmp_path: Path) -> None:
    sn.register("072★写真アルバム 1", tmp_path)
    assert not list(tmp_path.glob("*.lock"))


def test_stale_lock_is_broken(tmp_path: Path) -> None:
    lock = tmp_path / "session_names.lock"
    lock.write_text("", encoding="utf-8")
    old = lock.stat().st_mtime - 3600
    import os

    os.utime(lock, (old, old))
    assert sn.register("072★写真アルバム 1", tmp_path) is True


def test_build_name_validation() -> None:
    assert sn.build_name("072", "★", "  写真   アルバム ", 4) == "072★写真 アルバム 4"
    for folder, symbol, topic in [("72", "★", "x"), ("072", "◎", "x"), ("072", "★", "  ")]:
        with pytest.raises(ValueError):
            sn.build_name(folder, symbol, topic, 1)
    with pytest.raises(ValueError):
        sn.build_name("072", "★", "あ" * 40, 1)


# ---------------------------------------------------------------- CLI

def test_cli_next_register_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = ["--handoff-dir", str(tmp_path)]
    assert sn.main([*base, "next", "--folder", "072", "--topic", "写真", "--symbol", "★"]) == 0
    assert capsys.readouterr().out.strip() == "072★写真 1"
    assert sn.main([*base, "register", "072★写真 1"]) == 0
    assert sn.main([*base, "register", "072★写真 1"]) == 3
    assert sn.main([*base, "register", "旧形式"]) == 2
    assert sn.main([*base, "check", "072★写真 1"]) == 0
    assert sn.main([*base, "check", "旧形式"]) == 2


def test_cli_unknown_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sn.socket, "gethostname", lambda: "UNKNOWN-PC")
    assert sn.main(["--handoff-dir", str(tmp_path), "symbol"]) == 1
    assert sn.main(["--handoff-dir", str(tmp_path), "next", "--topic", "x"]) == 1


# ---------------------------------------------------------------- フック

class _Stdin:
    def __init__(self, text: str) -> None:
        self.buffer = io.BytesIO(text.encode("utf-8"))


def _run_hook(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], payload: dict) -> str:
    monkeypatch.setattr(sys, "stdin", _Stdin(json.dumps(payload)))
    assert prompt.main() == 0
    return capsys.readouterr().out


def test_hook_injects_on_startup(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sn.socket, "gethostname", lambda: "DESKTOP-7OSURHD")
    out = json.loads(_run_hook(monkeypatch, capsys, {"source": "startup"}))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "★" in ctx and "AskUserQuestion" in ctx and "/handoff load" in ctx
    # 2026-09-26: 名前のために最初の作業を止めない (一段落してから・確認に同梱)
    assert "一段落" in ctx and "同梱" in ctx
    assert "最初の作業より前" not in ctx


@pytest.mark.parametrize("payload", [{"source": "resume"}, {"source": "compact"}, {"reason": "clear"}])
def test_hook_silent_when_not_startup(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], payload: dict
) -> None:
    assert _run_hook(monkeypatch, capsys, payload) == ""


def test_hook_survives_garbage_stdin(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "stdin", _Stdin("not json"))
    assert prompt.main() == 0
    assert capsys.readouterr().out == ""
