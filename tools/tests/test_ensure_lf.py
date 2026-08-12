"""test_ensure_lf — ensure_lf.py と preflight_inputs.py の単体テスト.

なぜ必要か:
    この 2 本は「CRLF 事故の再発防止」と「入力取り違えの検出」そのものが目的なので、
    退行に気づけないと存在意義が消える。2026-08-11 の Lv6 レビューで
    「再発防止が目的のツールでテスト不在は致命的」と 🟠 指摘された。

実行方法:
    python -m pytest .claude/tools/tests/test_ensure_lf.py -v
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import ensure_lf  # noqa: E402
import preflight_inputs  # noqa: E402

ENSURE_LF = TOOLS / "ensure_lf.py"
PREFLIGHT = TOOLS / "preflight_inputs.py"


def run(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


# ---------------------------------------------------------------- ensure_lf


def test_scan_counts_cr_but_ignores_lf_and_tab(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    p.write_bytes(b"a\r\nb\tc\nd")
    assert ensure_lf.scan(p) == {0x0D: 1}


def test_scan_detects_other_control_bytes(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    p.write_bytes(b"a\x00b\x7fc")
    assert ensure_lf.scan(p) == {0x00: 1, 0x7F: 1}


def test_scan_clean_file_is_empty(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    p.write_bytes(b"a\nb\tc\n")
    assert ensure_lf.scan(p) == {}


def test_fix_converts_crlf_and_keeps_backup(tmp_path: Path) -> None:
    p = tmp_path / "a.js"
    p.write_bytes(b"const a = 1\r\nconst b = 2\r\n")
    removed, backup = ensure_lf.fix(p)
    assert removed == 2
    assert p.read_bytes() == b"const a = 1\nconst b = 2\n"
    assert backup is not None and backup.exists()
    assert backup.read_bytes() == b"const a = 1\r\nconst b = 2\r\n"


def test_fix_counts_lone_cr_by_cr_count_not_length(tmp_path: Path) -> None:
    """lone CR は CR→LF 置換で長さが変わらない。バイト長差で数えると 0 と誤表示する。"""
    p = tmp_path / "a.txt"
    p.write_bytes(b"a\rb\rc")
    removed, _ = ensure_lf.fix(p)
    assert removed == 2
    assert p.read_bytes() == b"a\nb\nc"


def test_fix_on_clean_file_is_noop(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    p.write_bytes(b"a\nb\n")
    removed, backup = ensure_lf.fix(p)
    assert removed == 0
    assert backup is None
    assert not list(tmp_path.glob("*.bak_*"))


def test_backup_names_do_not_collide(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    for _ in range(3):
        p.write_bytes(b"x\r\ny\r\n")
        ensure_lf.fix(p)
    assert len(list(tmp_path.glob("a.txt.bak_*"))) == 3


# ---------------------------------------------------- ensure_lf: 終了コード


def test_check_clean_exits_ok(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    p.write_bytes(b"a\nb\n")
    assert run(ENSURE_LF, "--check", str(p)).returncode == ensure_lf.EXIT_OK


def test_check_crlf_exits_found(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    p.write_bytes(b"a\r\nb\r\n")
    assert run(ENSURE_LF, "--check", str(p)).returncode == ensure_lf.EXIT_FOUND


def test_fix_success_exits_ok(tmp_path: Path) -> None:
    """--fix の成功は 0。以前は 1 を返しており「検出」と区別できなかった。"""
    p = tmp_path / "a.txt"
    p.write_bytes(b"a\r\nb\r\n")
    r = run(ENSURE_LF, "--fix", str(p))
    assert r.returncode == ensure_lf.EXIT_OK
    assert p.read_bytes() == b"a\nb\n"


def test_fix_with_exit_on_change_exits_found(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    p.write_bytes(b"a\r\nb\r\n")
    r = run(ENSURE_LF, "--fix", "--exit-on-change", str(p))
    assert r.returncode == ensure_lf.EXIT_FOUND


def test_missing_file_exits_error(tmp_path: Path) -> None:
    r = run(ENSURE_LF, "--check", str(tmp_path / "nope.txt"))
    assert r.returncode == ensure_lf.EXIT_ERROR


def test_no_target_exits_usage() -> None:
    assert run(ENSURE_LF, "--check").returncode == ensure_lf.EXIT_USAGE


def test_warnings_go_to_stderr_not_stdout(tmp_path: Path) -> None:
    """stdout に混ぜるとパイプ利用を壊す。"""
    r = run(ENSURE_LF, "--check", str(tmp_path / "nope.txt"))
    assert "見つかりません" in r.stderr
    assert "見つかりません" not in r.stdout


def test_fix_is_atomic_and_leaves_no_tmp(tmp_path: Path) -> None:
    """書き戻し中に失敗しても元ファイルが壊れないこと（一時ファイル + os.replace）。"""
    p = tmp_path / "a.txt"
    original = b"a\r\nb\r\n"
    p.write_bytes(original)

    import os as _os
    real_replace = _os.replace

    def boom(src, dst):  # noqa: ANN001
        raise OSError("差し替え失敗（模擬）")

    _os.replace = boom
    try:
        with pytest.raises(OSError):
            ensure_lf.fix(p)
    finally:
        _os.replace = real_replace

    assert p.read_bytes() == original, "失敗時に元ファイルが壊れている"
    assert not list(tmp_path.glob("*.tmp")), "一時ファイルが残っている"


def test_directory_is_reported_as_directory(tmp_path: Path) -> None:
    """ディレクトリ指定を『見つかりません』で片付けない（切り分け不能になる）。"""
    r = run(ENSURE_LF, "--check", str(tmp_path))
    assert r.returncode == ensure_lf.EXIT_ERROR
    assert "ディレクトリ" in r.stderr


def test_fix_reports_residual_control_bytes(tmp_path: Path) -> None:
    """CR は直せても NUL は残る。『変換後の再検査』が効いていることを固定する。"""
    p = tmp_path / "a.txt"
    p.write_bytes(b"a\r\nb\x00c")
    r = run(ENSURE_LF, "--fix", str(p))
    assert r.returncode == ensure_lf.EXIT_ERROR
    assert "変換後も残存" in r.stderr
    assert p.read_bytes() == b"a\nb\x00c"


def test_preset_and_paths_are_combined(tmp_path: Path) -> None:
    """--preset と paths の併用は『両方』が対象（help の記述と一致すること）。"""
    p = tmp_path / "a.txt"
    p.write_bytes(b"x\n")
    r = run(ENSURE_LF, "--check", "--preset", "cgd-wf", str(p))
    assert r.returncode == ensure_lf.EXIT_OK
    assert str(p) in r.stdout
    assert "cgd_lv8_review.js" in r.stdout


def test_preset_resolves_relative_to_repo() -> None:
    for p in ensure_lf.PRESETS["cgd-wf"]:
        assert p.is_file(), f"{p} が解決できていない"


def test_cgd_workflows_are_lf() -> None:
    """本番の WF スクリプトが CRLF に戻っていないことを常時監視する。"""
    assert run(ENSURE_LF, "--check", "--preset", "cgd-wf").returncode == ensure_lf.EXIT_OK


# ------------------------------------------------------------ preflight_inputs


def test_inspect_normal_file(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    body = "こんにちは\nworld\n".encode("utf-8")
    p.write_bytes(body)
    info = preflight_inputs.inspect(str(p))
    assert info["exists"] is True
    assert info["is_file"] is True
    assert info["readable"] is True
    assert info["bytes"] == len(body)
    assert info["sha256"] == hashlib.sha256(body).hexdigest()
    assert "error" not in info
    assert info["head"].startswith("こんにちは")


def test_inspect_missing_file_is_all_false(tmp_path: Path) -> None:
    info = preflight_inputs.inspect(str(tmp_path / "nope.txt"))
    assert info["exists"] is False
    assert info["is_file"] is False
    assert info["readable"] is False
    assert info["bytes"] == 0
    assert info["sha256"] == ""


def test_inspect_directory_is_not_a_file(tmp_path: Path) -> None:
    """os.path.exists だけだとディレクトリが通過してしまう。"""
    info = preflight_inputs.inspect(str(tmp_path))
    assert info["exists"] is True
    assert info["is_file"] is False
    assert info["readable"] is False


def test_inspect_empty_file(tmp_path: Path) -> None:
    p = tmp_path / "empty.txt"
    p.write_bytes(b"")
    info = preflight_inputs.inspect(str(p))
    assert info["is_file"] is True
    assert info["readable"] is True
    assert info["bytes"] == 0


def test_inspect_mtime_is_iso_with_timezone(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    p.write_bytes(b"x")
    info = preflight_inputs.inspect(str(p))
    assert "T" in info["mtime"]
    # ISO8601 のオフセット (+09:00 等) が付いていること
    assert info["mtime"][-6] in "+-" or info["mtime"].endswith("Z")
    assert info["mtime_ns"] > 0


def test_inspect_sha256_streams_large_file(tmp_path: Path) -> None:
    """一括読みしていないこと(結果が一致すること)を確認する。"""
    p = tmp_path / "big.bin"
    body = bytes(range(256)) * 20000  # 約 5MB > CHUNK
    p.write_bytes(body)
    info = preflight_inputs.inspect(str(p))
    assert info["bytes"] == len(body)
    assert info["sha256"] == hashlib.sha256(body).hexdigest()


def test_inspect_head_does_not_lose_non_utf8(tmp_path: Path) -> None:
    """errors='replace' だと中身が U+FFFD に潰れて復元できない。"""
    p = tmp_path / "cp932.txt"
    p.write_bytes("日本語".encode("cp932"))
    info = preflight_inputs.inspect(str(p))
    assert "\ufffd" not in info["head"]
    assert "\\x" in info["head"]


def test_inspect_unreadable_file_sets_readable_false(monkeypatch, tmp_path: Path) -> None:
    """読めなかったら readable=false + error。これが 🔴 指摘の修正本体。

    stat は通るが read で落ちる状況を作る（Windows では権限で作りにくいので
    open を差し替えて再現する）。
    """
    p = tmp_path / "a.txt"
    p.write_bytes(b"hello")

    real_open = open

    def fake_open(file, *a, **kw):  # noqa: ANN001
        if str(file) == str(p):
            raise PermissionError("読取拒否（模擬）")
        return real_open(file, *a, **kw)

    monkeypatch.setattr("builtins.open", fake_open)
    info = preflight_inputs.inspect(str(p))

    assert info["exists"] is True
    assert info["is_file"] is True      # stat は成功しているので通常ファイルではある
    assert info["readable"] is False    # ここで止める
    assert info["sha256"] == ""
    assert "読取失敗" in info["error"]


def test_inspect_size_change_during_read_is_flagged(monkeypatch, tmp_path: Path) -> None:
    """stat のサイズと実読量が食い違ったら error を立てる（事実の合成を防ぐ）。"""
    p = tmp_path / "a.txt"
    p.write_bytes(b"0123456789")

    real_stat = os.stat

    def fat_stat(path, *a, **kw):  # noqa: ANN001
        st = real_stat(path, *a, **kw)
        if str(path) == str(p):
            class _S:
                st_mode = st.st_mode
                st_size = st.st_size + 100   # 実際より大きいサイズを申告
                st_mtime = st.st_mtime
                st_mtime_ns = st.st_mtime_ns
            return _S()
        return st

    monkeypatch.setattr(preflight_inputs.os, "stat", fat_stat)
    info = preflight_inputs.inspect(str(p))

    assert info["readable"] is True
    assert "読取中にサイズが変化" in info["error"]
    assert info["bytes"] == 10          # 実際に読めた量に直す


def test_inspect_invalid_path_does_not_raise() -> None:
    """NUL 入りパスでも例外を投げずに false を返す。"""
    info = preflight_inputs.inspect("C:/tmp-ai/no\x00pe.txt")
    assert info["exists"] is False
    assert info["is_file"] is False


def test_cli_always_emits_json(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    p.write_bytes(b"hello")
    r = run(PREFLIGHT, str(p), str(tmp_path / "nope.txt"), str(tmp_path))
    assert r.returncode == 0
    doc = json.loads(r.stdout)
    assert [f["path"] for f in doc["files"]] == [
        str(p), str(tmp_path / "nope.txt"), str(tmp_path)
    ]
    assert doc["files"][0]["readable"] is True
    assert doc["files"][1]["exists"] is False
    assert doc["files"][2]["is_file"] is False


# --------------------------------------------------------------- cgd_doctor
# 実際に人が叩くのは cgd_doctor なので、こちらにもテストを置く。
# 以前は同じ判定を doctor 側に書き写しており、写経側は一度も検証されていなかった。


def _doctor(monkeypatch, workflows_dir: Path):
    import cgd_doctor  # noqa: PLC0415
    monkeypatch.setattr(cgd_doctor, "WORKFLOWS_DIR", workflows_dir)
    return cgd_doctor.check_workflows()


def test_doctor_reports_lf_workflows_as_ok(monkeypatch, tmp_path: Path) -> None:
    for n in ("cgd_lv6_review.js", "cgd_lv7_review.js", "cgd_lv8_review.js"):
        (tmp_path / n).write_bytes(b"const a = 1\n")
    assert all(r[0].strip() == "OK" for r in _doctor(monkeypatch, tmp_path))


def test_doctor_flags_crlf_workflows(monkeypatch, tmp_path: Path) -> None:
    for n in ("cgd_lv6_review.js", "cgd_lv7_review.js", "cgd_lv8_review.js"):
        (tmp_path / n).write_bytes(b"const a = 1\r\n")
    results = _doctor(monkeypatch, tmp_path)
    assert all(r[0].strip() == "NG" for r in results)
    assert all("CRLF" in r[2] for r in results)


def test_doctor_flags_zero_byte_workflows(monkeypatch, tmp_path: Path) -> None:
    """0 バイトは制御バイト 0 件なので、サイズを見ないと『起動可』に化ける。"""
    for n in ("cgd_lv6_review.js", "cgd_lv7_review.js", "cgd_lv8_review.js"):
        (tmp_path / n).write_bytes(b"")
    results = _doctor(monkeypatch, tmp_path)
    assert all(r[0].strip() == "NG" for r in results)
    assert all("0 バイト" in r[2] for r in results)


def test_doctor_uses_the_same_scan_as_ensure_lf(monkeypatch, tmp_path: Path) -> None:
    """判定を写経せず ensure_lf.scan に一本化していること（片方だけ直る事故の防止）。"""
    import cgd_doctor  # noqa: PLC0415
    called = {"n": 0}
    real = ensure_lf.scan

    def counting(path):  # noqa: ANN001
        called["n"] += 1
        return real(path)

    monkeypatch.setattr(ensure_lf, "scan", counting)
    (tmp_path / "cgd_lv6_review.js").write_bytes(b"x\n")
    monkeypatch.setattr(cgd_doctor, "WORKFLOWS_DIR", tmp_path)
    cgd_doctor.check_workflows()
    assert called["n"] > 0, "doctor が ensure_lf.scan を使っていない（写経に戻っている）"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
