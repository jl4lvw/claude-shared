from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Sequence

import pytest

import cgd_cx


def init_git_repo(repo: Path) -> None:
    commands = (
        ["git", "init"],
        ["git", "config", "user.name", "cx-test"],
        ["git", "config", "user.email", "cx-test@example.invalid"],
        ["git", "add", "."],
        ["git", "commit", "-m", "initial"],
    )
    try:
        for command in commands:
            subprocess.run(command, cwd=repo, check=True, capture_output=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"git を用意できないためスキップ: {type(exc).__name__}")


class FakeRunner:
    def __init__(self, repo: Path, artifact_parent: Path) -> None:
        self.repo = repo
        self.artifact_parent = artifact_parent
        self.calls: list[tuple[list[str], Path, int, dict[str, str]]] = []
        self.dirty = b""

    def __call__(
        self, args: Sequence[str], cwd: Path, timeout: int, env: dict[str, str]
    ) -> dict[str, object]:
        command = list(args)
        self.calls.append((command, cwd, timeout, env))
        if command[:2] == ["git", "rev-parse"] and command[-1] == "--show-toplevel":
            return self._ok((str(self.repo) + "\n").encode())
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return self._ok(b"0123456789012345678901234567890123456789\n")
        if command[:2] == ["git", "branch"]:
            return self._ok(b"main\n")
        if command[:2] == ["git", "status"]:
            return self._ok(self.dirty)
        if "cgd_lv0_codex.py" in " ".join(command):
            run_id = command[-1]
            artifact = self.artifact_parent / f"cgd_lv0_{run_id}"
            artifact.mkdir(parents=True)
            (artifact / "before").mkdir()
            (artifact / "prepare.json").write_text(
                json.dumps({"workdir": str(cwd), "run": run_id, "secrets": []}),
                encoding="utf-8",
            )
            (artifact / "manifest.json").write_text("{}", encoding="utf-8")
            return self._ok()
        return self._ok()

    @staticmethod
    def _ok(stdout: bytes = b"") -> dict[str, object]:
        return {"exit_code": 0, "stdout": stdout, "stderr": b"", "timeout": False, "duration_ms": 1}


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, Path, Path, FakeRunner]:
    repo = tmp_path / "repo"
    project = repo / "project"
    project.mkdir(parents=True)
    (project / "original.txt").write_text("before\n", encoding="utf-8", newline="")
    artifacts = tmp_path / "artifacts"
    runner = FakeRunner(repo, artifacts)
    return repo, project, artifacts, runner


def make_run(
    workspace: tuple[Path, Path, Path, FakeRunner], raw: bytes = b"debug this"
) -> tuple[Path, Path, FakeRunner]:
    repo, project, artifacts, runner = workspace
    request = repo.parent / "request.txt"
    request.write_bytes(raw)
    base = repo.parent / "runs"
    request_md = cgd_cx.new_run(
        str(project), request, None, base, repo.parent / "memory-missing",
        runner=runner,
        clock=lambda: datetime(2026, 9, 21, 12, 34, 56),
        random_hex=iter(("abc123", "def456")).__next__,
        artifact_resolver=lambda run_id: artifacts / f"cgd_lv0_{run_id}",
    )
    return request_md.parent, project, runner


def test_new_preserves_original_bytes(
    workspace: tuple[Path, Path, Path, FakeRunner]
) -> None:
    raw = ("  引用 \"x\"\r\n" + "行\n" * 100 + "末尾 ").encode("utf-8")
    run_dir, _project, _runner = make_run(workspace, raw)
    document = (run_dir / "request.md").read_bytes()
    start = document.index(b"--- BEGIN ORIGINAL REQUEST ---\n") + len(b"--- BEGIN ORIGINAL REQUEST ---\n")
    end = document.index(b"\n--- END ORIGINAL REQUEST ---", start)
    assert document[start:end] == raw
    assert (run_dir / "state.json").is_file()
    assert (run_dir / "context" / "manifest.txt").is_file()


def test_request_document_contains_required_safety_instructions(
    workspace: tuple[Path, Path, Path, FakeRunner]
) -> None:
    run_dir, project, _runner = make_run(workspace)
    document = (run_dir / "request.md").read_text(encoding="utf-8")
    required = (
        "context/AGENTS.md、context/CLAUDE.md、context/rules/",
        "context/memory/MEMORY.md",
        ".claude/skills/",
        "README と仕様書",
        "plan.md の内容をユーザーに見せ、OK をもらうまで",
        ".bak_YYYYMMDD_HHMMSS",
        "git commit/push/reset/stash/clean/checkout",
        "その都度ユーザーの承認",
        "detail/",
        "result.md の構成（40行以内）",
        "1. 要約（1行）",
        "7. 戻し方",
        "ヘッドレス Playwright",
        "長く待つコマンドにはタイムアウト",
        "承認ゲート（hook）があるが、こちら側には無い",
        "完了。結果: <result.md のフルパス>",
        project.as_posix(),
        run_dir.as_posix(),
    )
    for text in required:
        assert text in document


def test_go_replaces_plan_approval_instruction_and_is_recorded(
    workspace: tuple[Path, Path, Path, FakeRunner]
) -> None:
    repo, project, artifacts, runner = workspace
    request_md = cgd_cx.new_run(
        str(project), None, None, repo.parent / "go-runs", repo.parent / "memory-missing",
        runner=runner,
        clock=lambda: datetime(2026, 9, 21, 12, 34, 56),
        random_hex=lambda: "abcdef",
        artifact_resolver=lambda run_id: artifacts / f"cgd_lv0_{run_id}",
        request="go",
        go=True,
    )
    document = request_md.read_text(encoding="utf-8")
    state = json.loads((request_md.parent / "state.json").read_text(encoding="utf-8"))
    assert "OK を待たずに進めてよい（plan.md は残す）" in document
    assert "OK をもらうまで対象ファイルを変更しない" not in document
    assert state["arguments"]["go"] is True


def test_new_rejects_empty_non_utf8_and_preserves_100k_characters(
    workspace: tuple[Path, Path, Path, FakeRunner]
) -> None:
    _repo, project, _artifacts, runner = workspace
    for label, raw in (("empty", b""), ("non-utf8", b"\xff")):
        request = project.parent.parent / f"bad-{label}.txt"
        request.write_bytes(raw)
        with pytest.raises(cgd_cx.CxError) as error:
            cgd_cx.new_run(str(project), request, None, project.parent.parent / "runs", None, runner=runner)
        assert error.value.code == 1
    raw = b"x" * 100_000
    run_dir, _project, _runner = make_run(workspace, raw)
    document = (run_dir / "request.md").read_bytes()
    start = document.index(b"--- BEGIN ORIGINAL REQUEST ---\n") + len(b"--- BEGIN ORIGINAL REQUEST ---\n")
    end = document.index(b"\n--- END ORIGINAL REQUEST ---", start)
    assert document[start:end] == raw


def test_inline_request_has_no_length_limit(
    workspace: tuple[Path, Path, Path, FakeRunner]
) -> None:
    repo, project, artifacts, runner = workspace
    raw = "x" * 100_000
    request_md = cgd_cx.new_run(
        str(project), None, None, repo.parent / "inline-runs", repo.parent / "memory-missing",
        runner=runner,
        clock=lambda: datetime(2026, 9, 21, 12, 34, 56),
        random_hex=lambda: "abcdef",
        artifact_resolver=lambda run_id: artifacts / f"cgd_lv0_{run_id}",
        request=raw,
    )
    assert raw.encode("utf-8") in request_md.read_bytes()


def test_slug_collision_uses_next_random(
    workspace: tuple[Path, Path, Path, FakeRunner]
) -> None:
    repo, project, artifacts, runner = workspace
    request = repo.parent / "request.txt"
    request.write_text("x", encoding="utf-8")
    base = repo.parent / "runs"
    (base / "20260921_123456_title_aaaaaa").mkdir(parents=True)
    request_md = cgd_cx.new_run(
        str(project), request, "title", base, repo.parent / "memory",
        runner=runner, clock=lambda: datetime(2026, 9, 21, 12, 34, 56),
        random_hex=iter(("aaaaaa", "bbbbbb", "cccccc")).__next__,
        artifact_resolver=lambda run_id: artifacts / f"cgd_lv0_{run_id}",
    )
    assert request_md.parent.name.endswith("_bbbbbb")


def test_lv0_id_is_timestamp_only_and_advances_past_existing_artifact(
    workspace: tuple[Path, Path, Path, FakeRunner]
) -> None:
    repo, project, artifacts, runner = workspace
    (artifacts / "cgd_lv0_20260921_123456").mkdir(parents=True)
    request = repo.parent / "request.txt"
    request.write_text("x", encoding="utf-8")
    request_md = cgd_cx.new_run(
        str(project), request, None, repo.parent / "runs", repo.parent / "memory-missing",
        runner=runner,
        clock=lambda: datetime(2026, 9, 21, 12, 34, 56),
        random_hex=lambda: "abcdef",
        artifact_resolver=lambda run_id: artifacts / f"cgd_lv0_{run_id}",
    )
    state = json.loads((request_md.parent / "state.json").read_text(encoding="utf-8"))
    assert state["lv0_run_id"] == "20260921_123457"
    assert len(state["lv0_run_id"]) == 15


def test_two_new_runs_at_same_time_reserve_different_lv0_ids(
    workspace: tuple[Path, Path, Path, FakeRunner]
) -> None:
    repo, project, artifacts, runner = workspace
    base = repo.parent / "runs"
    ids: list[str] = []
    randoms = iter(("aaaaaa", "bbbbbb"))
    for request in ("first", "second"):
        request_md = cgd_cx.new_run(
            str(project), None, None, base, repo.parent / "memory-missing",
            runner=runner,
            clock=lambda: datetime(2026, 9, 21, 12, 34, 56),
            random_hex=randoms.__next__,
            artifact_resolver=lambda run_id: artifacts / f"cgd_lv0_{run_id}",
            request=request,
        )
        state = json.loads((request_md.parent / "state.json").read_text(encoding="utf-8"))
        ids.append(state["lv0_run_id"])
    assert ids == ["20260921_123456", "20260921_123457"]
    assert all((base / ".lv0_ids" / run_id).is_dir() for run_id in ids)


def test_child_env_is_allowlist_and_rejects_proxy_credentials() -> None:
    result = cgd_cx.child_env(
        {"PATH": "bin", "API_TOKEN": "secret", "CGD_MODE": "test", "HTTPS_PROXY": "https://u:p@example.test"}
    )
    assert result["PATH"] == "bin"
    assert result["CGD_MODE"] == "test"
    assert "API_TOKEN" not in result
    assert "HTTPS_PROXY" not in result


def test_child_failure_detail_decodes_and_redacts_output() -> None:
    secret = b'token = "12345678-secret-value"'
    detail = cgd_cx._failure_detail(
        {"exit_code": 7, "stdout": b"bad:\xff " + secret, "stderr": b"failed"}
    )
    assert "終了コード=7" in detail
    assert "stdout=" in detail and "stderr=" in detail
    assert "[REDACTED]" in detail
    assert "12345678-secret-value" not in detail


def test_git_state_filters_generated_paths_before_limit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    artifacts = tmp_path / "artifacts"
    runner = FakeRunner(repo, artifacts)
    generated = b"".join(
        f"?? runs/item-{index:05d}.txt\0".encode("utf-8")
        for index in range(cgd_cx.MAX_DIRTY_PATHS + 1)
    )
    runner.dirty = generated + b" M tracked.txt\0"
    state = cgd_cx.git_state(runner, repo, (repo / "runs",))
    assert state["dirty_paths"] == ["tracked.txt"]
    status_call = next(command for command, _cwd, _timeout, _env in runner.calls if command[:2] == ["git", "status"])
    assert "--untracked-files=normal" in status_call


@pytest.mark.parametrize("project_kind", ("repo", "outside"))
def test_validate_project_rejects_repo_or_outside_with_exit_2(
    tmp_path: Path, project_kind: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    project = repo if project_kind == "repo" else tmp_path / "outside"
    project.mkdir(exist_ok=True)
    runner = FakeRunner(repo, tmp_path / "artifacts")
    with pytest.raises(cgd_cx.CxError) as error:
        cgd_cx.validate_project(str(project), runner)
    assert error.value.code == 2


def test_cli_new_accepts_exactly_one_request_source() -> None:
    parser = cgd_cx.build_parser()
    from_file = parser.parse_args(["new", "--project", "p", "--request-file", "request.txt"])
    inline = parser.parse_args(["new", "--project", "p", "--request", " text "])
    assert from_file.request_file == Path("request.txt")
    assert inline.request == " text "
    assert not inline.go
    with_go = parser.parse_args(["new", "--project", "p", "--request", "x", "--go"])
    assert with_go.go
    for argv in (
        ["new", "--project", "p"],
        ["new", "--project", "p", "--request", "x", "--request-file", "request.txt"],
    ):
        with pytest.raises(SystemExit) as error:
            parser.parse_args(argv)
        assert error.value.code == 1


def test_cli_receipt_and_status_contract() -> None:
    parser = cgd_cx.build_parser()
    received = parser.parse_args([
        "receipt", "--run", "run", "--timeout", "17", "--checks", "checks.json",
    ])
    shown = parser.parse_args(["status", "--run", "run"])
    assert received.run == "run"
    assert received.timeout == 17
    assert received.checks == Path("checks.json")
    assert shown.run == "run"
    for argv in (["receipt", "run"], ["status", "run"]):
        with pytest.raises(SystemExit) as error:
            parser.parse_args(argv)
        assert error.value.code == 1


def test_new_stdout_is_exactly_two_lines(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    request_md = tmp_path / "run" / "request.md"
    request_md.parent.mkdir()
    (request_md.parent / "state.json").write_text('{"warnings": []}', encoding="utf-8")
    monkeypatch.setattr(cgd_cx, "new_run", lambda *args, **kwargs: request_md)
    code = cgd_cx.main(["new", "--project", "p", "--request", "x"])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.err == ""
    assert captured.out.splitlines() == [
        "デバッグを開始してください。最初に次のファイル全体を読み、その指示に従ってください。",
        request_md.resolve().as_posix(),
    ]


def test_lv0_contract_rejects_value_field(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "before").mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (artifact / "manifest.json").write_text("{}", encoding="utf-8")
    (artifact / "prepare.json").write_text(
        json.dumps({"workdir": str(project), "run": "id", "secrets": [{"path": "x", "kind": "key", "value": "no"}]}),
        encoding="utf-8",
    )
    with pytest.raises(cgd_cx.CxError):
        cgd_cx.load_lv0_prepare_contract("id", artifact, project)


def test_new_contract_with_real_lv0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lv0_script = Path(__file__).parents[2] / "ref" / "cgd_lv0_codex.py"
    if os.name != "nt" or not lv0_script.is_file():
        pytest.skip("実物の cgd_lv0_codex.py が見つからない")
    repo = tmp_path / "repo"
    project = repo / "project"
    project.mkdir(parents=True)
    (project / "sample.txt").write_text("sample\n", encoding="utf-8", newline="")
    init_git_repo(repo)
    original_new_run = cgd_cx.new_run

    def new_run_with_real_lv0(*args: object, **kwargs: object) -> Path:
        kwargs["lv0_script"] = lv0_script
        return original_new_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cgd_cx, "new_run", new_run_with_real_lv0)
    code = cgd_cx.main([
        "new", "--project", str(project), "--request", "contract test", "--title", "contract",
        "--base-dir", str(tmp_path / "runs"), "--memory-dir", str(tmp_path / "memory-missing"),
    ])
    captured = capsys.readouterr()
    assert code == 0
    request_md = Path(captured.out.splitlines()[1])
    run_dir = request_md.parent
    assert request_md.is_file()
    assert (run_dir / "context").is_dir()
    assert (run_dir / "baseline.json").is_file()


def test_new_hashes_all_project_files_and_only_dirty_paths_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    project = repo / "project"
    project.mkdir(parents=True)
    (project / "inside.txt").write_text("inside\n", encoding="utf-8")
    for index in range(2_000):
        (repo / f"tracked-{index:04d}.txt").write_text("clean\n", encoding="utf-8")
    init_git_repo(repo)
    changed = ["tracked-0001.txt", "tracked-1000.txt", "tracked-1999.txt"]
    for relative in changed:
        (repo / relative).write_text("changed\n", encoding="utf-8")
    untracked = project / "bulk-data"
    untracked.mkdir()
    for index in range(3_000):
        (untracked / f"item-{index:04d}.txt").write_text("data\n", encoding="utf-8")

    artifacts = tmp_path / "artifacts"

    def runner(
        args: Sequence[str], cwd: Path, timeout: int, env: dict[str, str]
    ) -> dict[str, object]:
        command = list(args)
        if "cgd_lv0_codex.py" in " ".join(command):
            run_id = command[-1]
            artifact = artifacts / f"cgd_lv0_{run_id}"
            artifact.mkdir(parents=True)
            (artifact / "before").mkdir()
            (artifact / "prepare.json").write_text(
                json.dumps({"workdir": str(cwd), "run": run_id, "secrets": []}),
                encoding="utf-8",
            )
            (artifact / "manifest.json").write_text("{}", encoding="utf-8")
            return FakeRunner._ok()
        completed = subprocess.run(
            command, cwd=cwd, env=env, timeout=timeout, capture_output=True, check=False
        )
        return {
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "timeout": False,
            "duration_ms": 1,
        }

    fingerprinted: list[str] = []
    original_fingerprint = cgd_cx._path_fingerprint

    def record_fingerprint(path: Path) -> dict[str, object]:
        fingerprinted.append(path.relative_to(repo).as_posix())
        return original_fingerprint(path)

    monkeypatch.setattr(cgd_cx, "_path_fingerprint", record_fingerprint)
    request_md = cgd_cx.new_run(
        str(project), None, None, tmp_path / "runs", tmp_path / "memory-missing",
        runner=runner,
        clock=lambda: datetime(2026, 9, 21, 12, 34, 56),
        random_hex=lambda: "abcdef",
        artifact_resolver=lambda run_id: artifacts / f"cgd_lv0_{run_id}",
        request="scan contract",
    )
    baseline = json.loads((request_md.parent / "baseline.json").read_text(encoding="utf-8"))
    assert len(baseline["files"]) == 3_001
    assert "inside.txt" in baseline["files"]
    assert "bulk-data/item-0000.txt" in baseline["files"]
    assert "bulk-data/item-2999.txt" in baseline["files"]
    assert fingerprinted == changed
    assert "project/bulk-data/" in baseline["git"]["dirty_paths"]

    fingerprinted.clear()
    (request_md.parent / "result.md").write_text("done\n", encoding="utf-8")
    code, _output = cgd_cx.receipt(request_md.parent.as_posix(), None, runner=runner)
    assert code == 0
    assert fingerprinted == changed


def test_receipt_two_stage_judgement_and_secret_redaction(
    workspace: tuple[Path, Path, Path, FakeRunner]
) -> None:
    run_dir, project, runner = make_run(workspace)
    secret = "ghp_abcdefghijklmnopqrstuvwxyzABCDEF"
    (project / "changed.py").write_text(f"token = '{secret}'\n", encoding="utf-8", newline="")
    (run_dir / "plan.md").write_text("plan\n", encoding="utf-8")
    (run_dir / "result.md").write_text("done\n", encoding="utf-8")
    code, output = cgd_cx.receipt(run_dir.as_posix(), None, runner=runner)
    assert code == 20
    assert "機械検査: 要確認" in output
    assert "依頼の修正: 未確認" in output
    assert secret not in output
    receipt = (run_dir / "receipt.json").read_text(encoding="utf-8")
    assert secret not in receipt
    assert json.loads(receipt)["secret_scan"]["hits"] >= 1
    assert len(output.splitlines()) <= 12


@pytest.mark.parametrize(
    "secret",
    (
        "sk-placeholderabcdefghijklmnop",
        "AKIAABCDEFGHIJKLMNOP",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "ghp_abcdefghijklmnopqrstuvwxyzABCD",
        'api_key = "12345678-example-value"',
    ),
)
def test_secret_scan_uses_all_required_patterns_without_value_exclusions(
    workspace: tuple[Path, Path, Path, FakeRunner], secret: str
) -> None:
    run_dir, project, runner = make_run(workspace)
    (project / "candidate.txt").write_text(f"dummy-example-placeholder {secret}\n", encoding="utf-8")
    (run_dir / "result.md").write_text("done\n", encoding="utf-8")
    code, _output = cgd_cx.receipt(run_dir.as_posix(), None, runner=runner)
    data = json.loads((run_dir / "receipt.json").read_text(encoding="utf-8"))
    assert code == 20
    assert data["secret_scan"]["hits"] == 1
    assert data["secret_scan"]["files"] == ["candidate.txt"]


def test_receipt_counts_timestamped_backups_without_failing(
    workspace: tuple[Path, Path, Path, FakeRunner]
) -> None:
    run_dir, project, runner = make_run(workspace)
    (project / "original.txt.bak_20260921_123456").write_text("before\n", encoding="utf-8")
    (project / ".bak_wrong").write_text("not a backup\n", encoding="utf-8")
    (run_dir / "result.md").write_text("done\n", encoding="utf-8")
    code, output = cgd_cx.receipt(run_dir.as_posix(), None, runner=runner)
    data = json.loads((run_dir / "receipt.json").read_text(encoding="utf-8"))
    assert code == 0
    assert data["backup_count"] == 1
    assert "バックアップ: 1" in output
    assert "バックアップ生成あり" not in data["judgement"]["reasons"]


def test_receipt_applies_timeout_and_checks_file(
    workspace: tuple[Path, Path, Path, FakeRunner]
) -> None:
    run_dir, project, runner = make_run(workspace)
    (project / "changed.py").write_text("value = 1\n", encoding="utf-8")
    (run_dir / "result.md").write_text("done\n", encoding="utf-8")
    checks = run_dir / "checks.json"
    checks.write_text(
        json.dumps({"commands": [{"name": "extra", "cmd": ["tool", "arg"], "timeout": 9}]}),
        encoding="utf-8",
    )
    code, _output = cgd_cx.receipt(run_dir.as_posix(), checks, 17, runner=runner)
    assert code == 0
    calls = [(command, timeout) for command, _cwd, timeout, _env in runner.calls]
    assert any(command[1:5] == ["-m", "ruff", "check", "--no-cache"] and timeout == 17 for command, timeout in calls)
    assert (["tool", "arg"], 9) in calls


def test_ensure_lf_is_skipped_when_there_are_no_text_targets(
    workspace: tuple[Path, Path, Path, FakeRunner]
) -> None:
    repo, _project, _artifacts, runner = workspace
    tool = repo / ".claude" / "tools" / "ensure_lf.py"
    tool.parent.mkdir(parents=True)
    tool.write_text("pass\n", encoding="utf-8")
    run_dir, _project, runner = make_run(workspace)
    (run_dir / "result.md").write_text("done\n", encoding="utf-8")

    code, output = cgd_cx.receipt(run_dir.as_posix(), None, runner=runner)
    data = json.loads((run_dir / "receipt.json").read_text(encoding="utf-8"))
    ensure = next(item for item in data["checks"] if item["name"] == "ensure_lf")
    assert code == 0
    assert ensure["status"] == "skipped"
    assert ensure["reason"] == "対象なし"
    assert data["judgement"]["machine_checks"] == "変更なし（検査対象なし）"
    assert "機械検査: 変更なし（検査対象なし）" in output
    assert "検査: 実行なし" in output
    assert not any("ensure_lf.py" in " ".join(command) for command, _cwd, _timeout, _env in runner.calls)


def test_ensure_lf_exit_2_is_reported_as_invocation_error(
    workspace: tuple[Path, Path, Path, FakeRunner]
) -> None:
    repo, project, _artifacts, runner = workspace
    tool = repo / ".claude" / "tools" / "ensure_lf.py"
    tool.parent.mkdir(parents=True)
    tool.write_text("pass\n", encoding="utf-8")
    run_dir, project, runner = make_run(workspace)
    (project / "changed.txt").write_text("changed\n", encoding="utf-8")
    (run_dir / "result.md").write_text("done\n", encoding="utf-8")

    def exit_2_runner(
        args: Sequence[str], cwd: Path, timeout: int, env: dict[str, str]
    ) -> dict[str, object]:
        if "ensure_lf.py" in " ".join(args):
            return {"exit_code": 2, "stdout": b"", "stderr": b"usage", "timeout": False, "duration_ms": 1}
        return runner(args, cwd, timeout, env)

    code, output = cgd_cx.receipt(run_dir.as_posix(), None, runner=exit_2_runner)
    data = json.loads((run_dir / "receipt.json").read_text(encoding="utf-8"))
    ensure = next(item for item in data["checks"] if item["name"] == "ensure_lf")
    assert code == 20
    assert ensure["reason"] == "検査の実行方法の異常"
    assert "要確認: 検査の実行方法の異常" in output
    assert "要確認: 検査失敗" not in output


def test_unstable_project_and_outside_files_complete_receipt(
    workspace: tuple[Path, Path, Path, FakeRunner], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, project, _artifacts, runner = workspace
    outside = repo / "live.txt"
    outside.write_text("before\n", encoding="utf-8")
    runner.dirty = b" M live.txt\0"
    run_dir, project, runner = make_run(workspace)
    changing = project / "changing.txt"
    changing.write_text("changing\n", encoding="utf-8")
    (run_dir / "result.md").write_text("done\n", encoding="utf-8")
    original_snapshot = cgd_cx.snapshot
    original_fingerprint = cgd_cx._path_fingerprint

    def unstable_snapshot(root: Path, **kwargs: object) -> dict[str, dict[str, object]]:
        result = original_snapshot(root, **kwargs)  # type: ignore[arg-type]
        if root.resolve() == project.resolve():
            result["changing.txt"] = {"type": "unstable"}
        return result

    def unstable_fingerprint(path: Path) -> dict[str, object]:
        if path.resolve() == outside.resolve():
            return {"type": "unstable"}
        return original_fingerprint(path)

    original_receipt = cgd_cx.receipt
    monkeypatch.setattr(cgd_cx, "snapshot", unstable_snapshot)
    monkeypatch.setattr(cgd_cx, "_path_fingerprint", unstable_fingerprint)
    monkeypatch.setattr(
        cgd_cx, "receipt",
        lambda run, checks, timeout=300: original_receipt(run, checks, timeout, runner=runner),
    )
    code = cgd_cx.main(["receipt", "--run", run_dir.as_posix()])
    captured = capsys.readouterr()
    data = json.loads((run_dir / "receipt.json").read_text(encoding="utf-8"))
    assert code == 20
    assert "Traceback" not in captured.err
    assert data["unstable"]["project"] == ["changing.txt"]
    assert data["unstable"]["outside"] == ["live.txt"]
    assert data["outside_changes"]["count"] == 0
    assert "対象外: 0" in captured.out
    assert not any("unstable" in reason.lower() for reason in data["judgement"]["reasons"])
    assert any("走査中に変更あり" in reason for reason in data["judgement"]["reasons"])


def test_main_redacts_unexpected_error_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cgd_cx, "status",
        lambda _run: (_ for _ in ()).throw(RuntimeError('token = "12345678-secret-value"')),
    )
    code = cgd_cx.main(["status", "--run", "unused"])
    captured = capsys.readouterr()
    assert code == 1
    assert "予期しないエラー: RuntimeError: [REDACTED]" in captured.err
    assert "12345678-secret-value" not in captured.err
    assert "Traceback" not in captured.err


def test_receipt_is_generation_appending(
    workspace: tuple[Path, Path, Path, FakeRunner]
) -> None:
    run_dir, _project, runner = make_run(workspace)
    (run_dir / "result.md").write_text("done\n", encoding="utf-8")
    first_code, _ = cgd_cx.receipt(run_dir.as_posix(), None, runner=runner)
    second_code, _ = cgd_cx.receipt(run_dir.as_posix(), None, runner=runner)
    assert first_code == second_code == 0
    assert (run_dir / "receipt.json").is_file()
    assert (run_dir / "receipt.0002.json").is_file()


def test_receipt_detects_added_modified_deleted_and_protected_change(
    workspace: tuple[Path, Path, Path, FakeRunner]
) -> None:
    repo, project, _artifacts, runner = workspace
    (repo / ".claude").mkdir()
    (repo / ".claude" / "fixed.txt").write_text("before", encoding="utf-8")
    runner.dirty = b" M .claude/fixed.txt\0"
    run_dir, project, runner = make_run(workspace)
    (project / "original.txt").write_text("after\n", encoding="utf-8")
    (project / "added.txt").write_text("new\n", encoding="utf-8")
    (repo / ".claude" / "fixed.txt").write_text("after", encoding="utf-8")
    (run_dir / "result.md").write_text("done\n", encoding="utf-8")
    code, _ = cgd_cx.receipt(run_dir.as_posix(), None, runner=runner)
    data = json.loads((run_dir / "receipt.json").read_text(encoding="utf-8"))
    assert code == 20
    assert data["changes"]["added"] == ["added.txt"]
    assert data["changes"]["modified"] == ["original.txt"]
    assert data["protected_changes"]["content"]["modified"] == [".claude/fixed.txt"]


def test_receipt_scans_files_in_new_nested_project_directory(
    workspace: tuple[Path, Path, Path, FakeRunner]
) -> None:
    repo, project, _artifacts, runner = workspace
    run_dir, project, runner = make_run(workspace)
    nested = project / "new-dir" / "depth"
    nested.mkdir(parents=True)
    (nested / "bad.py").write_text("def broken(:\n", encoding="utf-8")
    (nested / "secret.txt").write_text(
        'token = "12345678-secret-value"\n', encoding="utf-8"
    )
    (nested / "note.md").write_text("new\n", encoding="utf-8")
    (run_dir / "result.md").write_text("done\n", encoding="utf-8")
    runner.dirty = b"?? project/new-dir/\0"

    def failing_python_checks(
        args: Sequence[str], cwd: Path, timeout: int, env: dict[str, str]
    ) -> dict[str, object]:
        command = list(args)
        if command[:3] == [sys.executable, "-m", "ruff"] or command[:3] == [
            sys.executable, "-m", "py_compile",
        ]:
            return {
                "exit_code": 1, "stdout": b"", "stderr": b"invalid syntax",
                "timeout": False, "duration_ms": 1,
            }
        return runner(args, cwd, timeout, env)

    code, output = cgd_cx.receipt(
        run_dir.as_posix(), None, runner=failing_python_checks
    )
    data = json.loads((run_dir / "receipt.json").read_text(encoding="utf-8"))
    assert code == 20
    assert data["changes"]["added"] == [
        "new-dir/depth/bad.py", "new-dir/depth/note.md", "new-dir/depth/secret.txt",
    ]
    assert any(item["name"].startswith("ruff-") for item in data["checks"])
    assert any(item["name"] == "py_compile:new-dir/depth/bad.py" for item in data["checks"])
    assert data["secret_scan"]["files"] == ["new-dir/depth/secret.txt"]
    assert data["judgement"]["machine_checks"] == "要確認"
    assert "変更: 追加3" in output


def test_baseline_unstable_project_file_is_incomparable_and_requires_review(
    workspace: tuple[Path, Path, Path, FakeRunner]
) -> None:
    run_dir, project, runner = make_run(workspace)
    baseline_path = run_dir / "baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline["files"]["original.txt"] = {"type": "unstable"}
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    (project / "original.txt").write_text("after\n", encoding="utf-8")
    (run_dir / "result.md").write_text("done\n", encoding="utf-8")

    code, _output = cgd_cx.receipt(run_dir.as_posix(), None, runner=runner)
    data = json.loads((run_dir / "receipt.json").read_text(encoding="utf-8"))
    assert code == 20
    assert data["changes"]["incomparable"] == ["original.txt"]
    assert data["changes"]["modified"] == []
    assert any(
        reason == "基準時点で不安定だったファイル 1 件は比較できない"
        for reason in data["judgement"]["reasons"]
    )


def test_ignored_outside_changes_do_not_require_review(
    workspace: tuple[Path, Path, Path, FakeRunner]
) -> None:
    repo, _project, _artifacts, runner = workspace
    run_dir, _project, runner = make_run(workspace)
    incident = repo / ".claude" / "incidents" / "cpu_snapshots.csv"
    incident.parent.mkdir(parents=True)
    incident.write_text("sample\n", encoding="utf-8")
    journal = repo / "live.db-wal"
    journal.write_text("sample\n", encoding="utf-8")
    runner.dirty = b" M .claude/incidents/cpu_snapshots.csv\0 M live.db-wal\0"
    (run_dir / "result.md").write_text("done\n", encoding="utf-8")

    code, output = cgd_cx.receipt(run_dir.as_posix(), None, runner=runner)
    data = json.loads((run_dir / "receipt.json").read_text(encoding="utf-8"))
    assert code == 0
    assert data["judgement"]["machine_checks"] == "変更なし（検査対象なし）"
    assert data["outside_changes"]["count"] == 0
    assert data["protected_changes"]["count"] == 0
    assert data["ignored"] == {
        "count": 2,
        "paths": [".claude/incidents/cpu_snapshots.csv", "live.db-wal"],
    }
    assert "無視 2 件" in output


@pytest.mark.parametrize(
    "path",
    (
        ".hq/cache/item.json",
        ".ctx/state.txt",
        ".claude/incidents/cpu_snapshots.csv",
        ".claude/incidents/telemetry.jsonl",
        ".claude/tools/.worker_usage_session.json",
        ".claude/tools/cgd_usage_current.sqlite3",
        "data/live.db-shm",
        "data/live.db-wal",
        "data/store.db-journal",
        "data/store.sqlite-journal",
        "build/__pycache__/module.pyc",
    ),
)
def test_default_ignored_change_patterns(path: str) -> None:
    assert cgd_cx._ignored_change(path)


@pytest.mark.parametrize("path", ("docs/daily-journal.md", "notes/my-journal", "src/journal.py"))
def test_ordinary_files_named_like_journal_are_not_ignored(path: str) -> None:
    assert not cgd_cx._ignored_change(path)


def test_outside_and_protected_changes_print_first_three_paths(
    workspace: tuple[Path, Path, Path, FakeRunner]
) -> None:
    repo, _project, _artifacts, runner = workspace
    run_dir, _project, runner = make_run(workspace)
    paths = ["z.txt", "a.txt", ".claude/fixed.txt", "m.txt"]
    for relative in paths:
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("changed\n", encoding="utf-8")
    runner.dirty = b"?? z.txt\0?? a.txt\0?? .claude/fixed.txt\0?? m.txt\0"
    (run_dir / "result.md").write_text("done\n", encoding="utf-8")

    code, output = cgd_cx.receipt(run_dir.as_posix(), None, runner=runner)
    assert code == 20
    path_line = next(
        line for line in output.splitlines() if line.startswith("対象外・保護領域のパス:")
    )
    assert path_line == "対象外・保護領域のパス: .claude/fixed.txt, a.txt, m.txt"
    assert "z.txt" not in path_line
    assert len(output.splitlines()) <= 12


def test_status_reads_only_metadata(workspace: tuple[Path, Path, Path, FakeRunner]) -> None:
    run_dir, _project, _runner = make_run(workspace)
    before = (run_dir / "state.json").read_bytes()
    output = cgd_cx.status(run_dir.as_posix())
    assert "phase: created" in output
    assert "request.md: yes" in output
    assert (run_dir / "state.json").read_bytes() == before


def test_snapshot_hashes_large_files(tmp_path: Path) -> None:
    data = b"x" * (2 * 1024 * 1024 + 1)
    (tmp_path / "large.bin").write_bytes(data)
    item = cgd_cx.snapshot(tmp_path)["large.bin"]
    assert item["sha256"] == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("limit", ("files", "bytes"))
def test_snapshot_limit_fails_closed_with_narrow_project_message(
    tmp_path: Path, limit: str
) -> None:
    (tmp_path / "sample.txt").write_text("sample\n", encoding="utf-8")
    arguments = {"max_files": 0} if limit == "files" else {"max_bytes": 0}
    with pytest.raises(cgd_cx.CxError) as error:
        cgd_cx.snapshot(tmp_path, **arguments)
    assert error.value.code == 3
    assert str(error.value) == "対象が大きすぎる。--project を狭く"


@pytest.mark.parametrize("project_kind", ("repo", "outside"))
def test_receipt_rejects_invalid_project_from_state_with_exit_2(
    workspace: tuple[Path, Path, Path, FakeRunner], project_kind: str,
) -> None:
    repo, _project, _artifacts, _runner = workspace
    run_dir, _project, runner = make_run(workspace)
    invalid_project = repo
    if project_kind == "outside":
        invalid_project = repo.parent / "other-project"
        invalid_project.mkdir()
    state_path = run_dir / "state.json"
    baseline_path = run_dir / "baseline.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    state["project"] = str(invalid_project.resolve())
    baseline["project"] = str(invalid_project.resolve())
    state_path.write_text(json.dumps(state), encoding="utf-8")
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")

    with pytest.raises(cgd_cx.CxError) as error:
        cgd_cx.receipt(run_dir.as_posix(), None, runner=runner)
    assert error.value.code == 2


def test_path_fingerprint_retries_three_times_then_marks_unstable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "moving.txt"
    path.write_bytes(b"x")
    original_read_bytes = Path.read_bytes
    reads = 0

    def changing_read_bytes(candidate: Path) -> bytes:
        nonlocal reads
        data = original_read_bytes(candidate)
        if candidate == path:
            reads += 1
            candidate.write_bytes(data + b"x")
        return data

    monkeypatch.setattr(Path, "read_bytes", changing_read_bytes)
    assert cgd_cx._path_fingerprint(path) == {"type": "unstable"}
    assert reads == 3


def test_source_has_no_restore_or_destructive_api() -> None:
    source = Path(cgd_cx.__file__).read_text(encoding="utf-8")
    assert "add_parser(\"restore\")" not in source
    assert "shutil.rmtree" not in source
    assert "os.remove" not in source
    assert "Path.unlink" not in source
    assert "os.unlink" not in source
    assert not source.startswith("#!")
