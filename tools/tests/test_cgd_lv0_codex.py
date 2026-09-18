"""cgd Lv0（Codex 実装レーン）の補助 cgd_lv0_codex.py のテスト.

本物の Codex は呼ばない。判定・写し・差分・巻き戻し・記録の読み取りと、
run の前段で止めるべき場面（再実行・prepare 後の変化・起動失敗）を固定する。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import cgd_lv0_codex as lv0  # noqa: E402

RUN = "20260918_163936"
FAKE_KEY = "sk-" + "a1b2c3d4" * 4  # 鍵の形をした偽物


def _npm_prefix(root: Path, with_exe: bool = True) -> Path:
    """npm の shim・本体 exe・補助 exe を持つ偽のインストール先を作り、shim のパスを返す。"""
    shim = root / "codex.cmd"
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_text("@echo off\n", encoding="utf-8")
    vendor = root / lv0.NPM_PACKAGE_REL / lv0.NPM_VENDOR_REL
    (vendor / "codex-resources").mkdir(parents=True)
    (vendor / "codex-resources" / lv0.HELPER_NAME).write_bytes(b"")
    if with_exe:
        (vendor / "bin").mkdir()
        (vendor / "bin" / "codex.exe").write_bytes(b"")
    return shim


def _helper_of(shim: Path) -> Path:
    return shim.parent / lv0.NPM_PACKAGE_REL / lv0.NPM_VENDOR_REL / "codex-resources" / lv0.HELPER_NAME


# ---------------------------------------------------------------- CLI の選択


def test_choose_bin_skips_a_cli_whose_helper_path_is_too_long(tmp_path: Path) -> None:
    long_shim = _npm_prefix(tmp_path / ("x" * 40))
    short_shim = _npm_prefix(tmp_path / "s")
    limit = len(str(_helper_of(short_shim))) + 1
    choice = lv0.choose_bin([long_shim, short_shim], max_path=limit, windows=True)
    assert choice.source == short_shim
    assert choice.exe is not None and choice.exe.name == "codex.exe"
    assert choice.env["CODEX_MANAGED_BY_NPM"] == "1"
    assert any("文字" in r for r in choice.rejected)


def test_choose_bin_launches_the_native_exe_not_the_cmd_shim(tmp_path: Path) -> None:
    shim = _npm_prefix(tmp_path / "noexe", with_exe=False)
    choice = lv0.choose_bin([shim], windows=True)
    assert choice.exe is None
    assert any("本体" in r for r in choice.rejected)


def test_choose_bin_accepts_the_desktop_bundle_with_a_sibling_helper(tmp_path: Path) -> None:
    exe = tmp_path / "bin" / "codex.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    (exe.parent / lv0.HELPER_NAME).write_bytes(b"")
    choice = lv0.choose_bin([exe], windows=True)
    assert choice.exe == exe and choice.env == {}


# ---------------------------------------------------------------- 作業フォルダ


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "app").mkdir()
    return repo


def test_validate_workdir_accepts_a_subfolder_of_a_repo(tmp_path: Path) -> None:
    assert lv0.validate_workdir(_repo(tmp_path) / "app") == []


def test_validate_workdir_rejects_the_outermost_repo_root_and_outside_repos(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert lv0.validate_workdir(repo)
    outside = tmp_path / "plain"
    outside.mkdir()
    assert lv0.validate_workdir(outside)


def test_validate_workdir_allows_a_nested_repo_root_inside_another_repo(tmp_path: Path) -> None:
    nested = _repo(tmp_path) / "app"
    (nested / ".git").mkdir()
    assert lv0.validate_workdir(nested) == []


def test_validate_workdir_rejects_project_root_drive_root_and_missing(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "app" / ".claude").mkdir()
    assert lv0.validate_workdir(repo / "app")
    assert lv0.validate_workdir(Path(tmp_path.anchor))
    assert lv0.validate_workdir(repo / "nope")
    assert lv0.validate_workdir(Path(r"\\server\share\x"))


def test_run_id_must_be_a_timestamp() -> None:
    lv0.validate_run_id(RUN)
    with pytest.raises(ValueError):
        lv0.validate_run_id("../x")


def test_scan_secrets_by_name_and_by_content_without_revealing_values(tmp_path: Path) -> None:
    (tmp_path / ".env").write_bytes(b"KEY=x\n")
    (tmp_path / "config.env").write_bytes(b"x\n")
    (tmp_path / "data.sqlite3").write_bytes(b"")
    (tmp_path / "settings.py").write_bytes(f'OPENAI = "{FAKE_KEY}"\n'.encode())
    (tmp_path / "app.py").write_bytes(b"print(1)\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "id_rsa").write_bytes(b"x")
    hits = dict(lv0.scan_secrets(tmp_path))
    assert set(hits) == {".env", "config.env", "data.sqlite3", "settings.py"}
    assert "件" in hits["settings.py"]
    assert FAKE_KEY not in json.dumps(hits)


# ---------------------------------------------------------------- 写しと差分


def _workdir(tmp_path: Path) -> Path:
    # write_text は Windows で \n を \r\n に変えるので、前提ファイルは LF のバイト列で書く
    w = tmp_path / "work"
    (w / "pkg").mkdir(parents=True)
    (w / "pkg" / "keep.py").write_bytes(b"a = 1\n")
    (w / "pkg" / "edit.py").write_bytes(b"x = 1\ny = 2\n")
    (w / "gone.txt").write_bytes(b"bye\n")
    (w / "__pycache__").mkdir()
    (w / "__pycache__" / "c.pyc").write_bytes(b"\0\1")
    return w


def test_snapshot_then_diff_finds_modified_added_and_deleted(tmp_path: Path) -> None:
    w = _workdir(tmp_path)
    snap = tmp_path / "snap"
    manifest = lv0.take_snapshot(w, snap)
    assert "__pycache__/c.pyc" not in manifest["files"]

    (w / "pkg" / "edit.py").write_bytes(b"x = 1\ny = 3\nz = 4\n")
    (w / "pkg" / "new.py").write_bytes(b"n = 0\n")
    (w / "gone.txt").unlink()
    (w / "__pycache__" / "c.pyc").write_bytes(b"changed")

    r = lv0.diff_snapshot(w, snap, manifest)
    assert r["modified"] == ["pkg/edit.py"]
    assert r["added"] == ["pkg/new.py"]
    assert r["deleted"] == ["gone.txt"]
    assert "+z = 4" in r["patch"]
    assert (r["plus"], r["minus"]) == (3, 2)
    assert set(r["after"]) == {"pkg/edit.py", "pkg/new.py"}


def test_line_counts_include_lines_that_start_with_plus_or_minus(tmp_path: Path) -> None:
    w = _workdir(tmp_path)
    snap = tmp_path / "snap"
    manifest = lv0.take_snapshot(w, snap)
    (w / "pkg" / "keep.py").write_bytes(b"a = 1\n++i\n--j\n")
    r = lv0.diff_snapshot(w, snap, manifest)
    assert (r["plus"], r["minus"]) == (2, 0)


def test_newline_style_is_flagged_only_when_the_style_changes(tmp_path: Path) -> None:
    w = _workdir(tmp_path)
    (w / "win.txt").write_bytes(b"a\r\nb\r\n")
    snap = tmp_path / "snap"
    manifest = lv0.take_snapshot(w, snap)
    (w / "win.txt").write_bytes(b"a\r\nb\r\nc\r\n")  # CRLF のまま行を足すだけ
    (w / "pkg" / "keep.py").write_bytes(b"a = 1\r\n")  # LF → CRLF
    r = lv0.diff_snapshot(w, snap, manifest)
    flagged = [n for n in r["notes"] if "改行コード" in n]
    assert len(flagged) == 1 and "keep.py" in flagged[0]


def test_secret_files_are_not_copied_and_never_enter_the_patch(tmp_path: Path) -> None:
    w = _workdir(tmp_path)
    (w / ".env").write_bytes(b"KEY=old\n")
    snap = tmp_path / "snap"
    secrets = {p for p, _ in lv0.scan_secrets(w)}
    manifest = lv0.take_snapshot(w, snap, secrets=secrets)
    assert not (snap / ".env").exists()
    assert manifest["untracked"][".env"]["reason"] == "secret"

    (w / ".env").write_bytes(b"KEY=new\n")
    (w / "leak.py").write_bytes(f'k = "{FAKE_KEY}"\n'.encode())
    r = lv0.diff_snapshot(w, snap, manifest)
    assert "KEY=" not in r["patch"] and FAKE_KEY not in r["patch"]
    assert any(".env" in u for u in r["untracked_changed"])
    assert any("leak.py" in u for u in r["untracked_changed"])
    assert r["added"] == []


def test_large_files_are_tracked_by_size_but_not_copied(tmp_path: Path) -> None:
    w = _workdir(tmp_path)
    (w / "big.bin").write_bytes(b"x" * 64)
    snap = tmp_path / "snap"
    manifest = lv0.take_snapshot(w, snap, max_file=32)
    assert manifest["untracked"]["big.bin"]["reason"] == "large" and not (snap / "big.bin").exists()
    (w / "big.bin").write_bytes(b"y" * 65)
    r = lv0.diff_snapshot(w, snap, manifest, max_file=32)
    assert any("big.bin" in u for u in r["untracked_changed"])


def test_snapshot_refuses_an_oversized_workdir(tmp_path: Path) -> None:
    w = _workdir(tmp_path)
    with pytest.raises(ValueError):
        lv0.take_snapshot(w, tmp_path / "snap", max_total=4)


def test_baks_keep_the_pre_run_content_and_do_not_show_up_as_new_files(tmp_path: Path) -> None:
    w = _workdir(tmp_path)
    snap = tmp_path / "snap"
    manifest = lv0.take_snapshot(w, snap)
    (w / "pkg" / "edit.py").write_bytes(b"changed\n")
    made = lv0.write_baks(w, snap, ["pkg/edit.py"], RUN)
    assert made == [f"pkg/edit.py.bak_{RUN}"]
    assert (w / made[0]).read_bytes() == b"x = 1\ny = 2\n"
    assert lv0.write_baks(w, snap, ["pkg/edit.py"], RUN) == []
    again = lv0.diff_snapshot(w, snap, manifest)
    assert again["added"] == [] and again["modified"] == ["pkg/edit.py"]


def test_restore_is_a_dry_run_unless_applied_and_never_deletes_new_files(tmp_path: Path) -> None:
    w = _workdir(tmp_path)
    snap = tmp_path / "snap"
    manifest = lv0.take_snapshot(w, snap)
    (w / "pkg" / "edit.py").write_bytes(b"broken\n")
    (w / "gone.txt").unlink()
    (w / "pkg" / "new.py").write_bytes(b"n = 0\n")
    recorded = lv0.diff_snapshot(w, snap, manifest)

    lv0.restore(w, snap, recorded, apply=False)
    assert (w / "pkg" / "edit.py").read_bytes() == b"broken\n"

    actions = lv0.restore(w, snap, recorded, apply=True)
    assert (w / "pkg" / "edit.py").read_bytes() == b"x = 1\ny = 2\n"
    assert (w / "gone.txt").exists()
    assert (w / "pkg" / "new.py").exists()
    assert any("新規" in a and "pkg/new.py" in a for a in actions)


def test_restore_does_not_overwrite_changes_made_after_the_diff(tmp_path: Path) -> None:
    w = _workdir(tmp_path)
    snap = tmp_path / "snap"
    manifest = lv0.take_snapshot(w, snap)
    (w / "pkg" / "edit.py").write_bytes(b"by codex\n")
    recorded = lv0.diff_snapshot(w, snap, manifest)
    (w / "pkg" / "edit.py").write_bytes(b"by another session\n")
    actions = lv0.restore(w, snap, recorded, apply=True)
    assert (w / "pkg" / "edit.py").read_bytes() == b"by another session\n"
    assert any("diff 後に変わった" in a for a in actions)


def test_restore_reports_what_cannot_be_restored(tmp_path: Path) -> None:
    w = _workdir(tmp_path)
    (w / "big.bin").write_bytes(b"x" * 64)
    snap = tmp_path / "snap"
    manifest = lv0.take_snapshot(w, snap, max_file=32)
    (w / "big.bin").write_bytes(b"y" * 65)
    recorded = lv0.diff_snapshot(w, snap, manifest, max_file=32)
    actions = lv0.restore(w, snap, recorded, apply=False)
    assert any(a.startswith("戻せない") and "big.bin" in a for a in actions)


# ---------------------------------------------------------------- run の前段で止める場面


@pytest.fixture()
def prepared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    monkeypatch.setattr(lv0, "RUNS_BASE", tmp_path / "runs")
    w = _repo(tmp_path) / "app"
    (w / "m.py").write_bytes(b"v = 1\n")
    spec = tmp_path / "spec.txt"
    spec.write_text("v を 2 にする", encoding="utf-8")
    assert lv0.main(["prepare", "--workdir", str(w), "--run", RUN]) == lv0.EXIT_OK
    return w, spec


def test_prepare_refuses_to_reuse_a_run_id(prepared: tuple[Path, Path]) -> None:
    w, _ = prepared
    assert lv0.main(["prepare", "--workdir", str(w), "--run", RUN]) == lv0.EXIT_GENERIC


def test_run_stops_when_the_workdir_changed_after_prepare(prepared: tuple[Path, Path]) -> None:
    w, spec = prepared
    (w / "m.py").write_bytes(b"v = 99\n")
    assert lv0.main(["run", "--workdir", str(w), "--run", RUN, "--spec", str(spec)]) == lv0.EXIT_GENERIC
    assert not (lv0.RUNS_BASE / f"cgd_lv0_{RUN}" / "run.started").exists()


def test_run_refuses_a_second_run_with_the_same_id(prepared: tuple[Path, Path]) -> None:
    w, spec = prepared
    (lv0.RUNS_BASE / f"cgd_lv0_{RUN}" / "run.started").write_text("x", encoding="utf-8")
    assert lv0.main(["run", "--workdir", str(w), "--run", RUN, "--spec", str(spec)]) == lv0.EXIT_GENERIC


def test_run_codex_reports_a_launch_failure_instead_of_crashing(tmp_path: Path) -> None:
    prompt = tmp_path / "p.txt"
    prompt.write_text("x", encoding="utf-8")
    choice = lv0.BinChoice(exe=tmp_path / "missing" / "codex.exe")
    info = lv0.run_codex(choice, tmp_path, prompt, tmp_path, "low", 5)
    assert info["launch_error"] and info["exit"] is None


# ---------------------------------------------------------------- 依頼文と記録


def test_build_prompt_puts_the_fixed_rules_before_the_spec() -> None:
    text = lv0.build_prompt("  関数 f を足す  ")
    assert text.startswith(lv0.PREAMBLE)
    assert text.endswith("関数 f を足す\n")
    assert "テスト・python" in text and "node_modules" in text


def test_parse_codex_stderr_reads_tokens_session_and_sandbox_errors() -> None:
    sample = (
        "session id: 01a0b34c-b8ec-7350-b3bd-fe906f41b985\n"
        "ERROR ... CreateProcessAsUserW failed: 5\n"
        "tokens used\n7,085\n"
    )
    info = lv0.parse_codex_stderr(sample)
    assert info == {
        "tokens": 7085,
        "session_id": "01a0b34c-b8ec-7350-b3bd-fe906f41b985",
        "sandbox_errors": 1,
    }


def _rollout(home: Path, name: str, limits: list[dict]) -> None:
    day = home / "sessions" / "2026" / "09" / "18"
    day.mkdir(parents=True, exist_ok=True)
    rows = [{"type": "event_msg", "payload": {"type": "token_count", "info": {}, "rate_limits": rl}}
            for rl in limits]
    (day / name).write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_weekly_percent_uses_the_10080_minute_window_not_the_primary_slot(tmp_path: Path) -> None:
    sid = "01a0b34c-b8ec-7350-b3bd-fe906f41b985"
    _rollout(tmp_path, f"rollout-2026-09-18T16-00-00-{sid}.jsonl", [
        {"primary": {"used_percent": 4.0, "window_minutes": 10080}},
        {"primary": {"used_percent": 60.0, "window_minutes": 300},
         "secondary": {"used_percent": 5.0, "window_minutes": 10080}},
    ])
    windows = lv0.session_rate_limits(sid, codex_home=tmp_path)
    assert lv0.weekly_percent(windows) == 5.0
    assert "週 5%" in lv0.describe_limits(windows) and "5時間 60%" in lv0.describe_limits(windows)
    assert lv0.weekly_percent([{"window_minutes": 300, "used_percent": 60.0}]) is None
    assert lv0.session_rate_limits(None, codex_home=tmp_path) == []


def test_latest_rate_limits_reads_the_newest_session(tmp_path: Path) -> None:
    _rollout(tmp_path, "rollout-2026-09-18T10-00-00-a.jsonl", [{"primary": {"used_percent": 1.0, "window_minutes": 10080}}])
    _rollout(tmp_path, "rollout-2026-09-18T12-00-00-b.jsonl", [{"primary": {"used_percent": 7.0, "window_minutes": 10080}}])
    assert lv0.weekly_percent(lv0.latest_rate_limits(codex_home=tmp_path)) == 7.0


def test_doctor_offers_lv0_only_with_codex_and_the_deepseek_reviewer() -> None:
    from cgd_doctor import OK, WARN, judge_level

    login = [(OK, "codex login", ""), (OK, "DASHSCOPE_API_KEY", "")]
    ds = [(OK, "DEEPSEEK_API_KEY", "")]
    usable = [(OK, "Lv0 Codex CLI", "C:/tools/codex-cli/codex.cmd")]
    assert "Lv0 /" in judge_level(login + ds + usable)
    assert "Lv0(DSレビュー不可)" in judge_level(login + usable)
    assert "Lv0(DS/Qwen代替のみ)" in judge_level(login + ds + [(WARN, "Lv0 Codex CLI", "長い")])
