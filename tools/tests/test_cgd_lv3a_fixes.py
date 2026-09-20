from __future__ import annotations

import importlib.util
import io
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import cgd_lv3a as target  # noqa: E402
from test_cgd_lv3a import (  # noqa: E402
    FakeIntegrator,
    brief_text,
    integration_fixture,
    invoke,
    review_output,
    run_args,
    spec,
    write,
)


class RecordingRunner:
    def __init__(
        self,
        invalid: set[str] | None = None,
        exit_codes: dict[str, int] | None = None,
        always_invalid: bool = False,
    ) -> None:
        self.invalid = invalid or set()
        self.exit_codes = exit_codes or {}
        self.always_invalid = always_invalid
        self.calls: dict[str, int] = {}
        self.efforts: list[str] = []
        self.retry_barrier: threading.Barrier | None = None

    def __call__(
        self,
        item: target.ReviewerSpec,
        prompt: str,
        cwd: Path,
        timeout: int,
        codex_path: str,
        tools: Path,
        effort: str,
    ) -> target.ExecResult:
        del prompt, cwd, timeout, codex_path, tools
        count = self.calls.get(item.name, 0) + 1
        self.calls[item.name] = count
        self.efforts.append(effort)
        if count == 2 and self.retry_barrier is not None:
            self.retry_barrier.wait(timeout=1)
        invalid = item.name in self.invalid and (count == 1 or self.always_invalid)
        stderr = "tokens used\n1,234\n" if item.vendor == "Codex" else "[DS Usage] 今回: ¥2.5 / $0.1"
        return target.ExecResult(
            self.exit_codes.get(item.name, 0),
            review_output(item, valid=not invalid),
            stderr,
        )


def load_state(work: Path) -> tuple[Path, dict[str, Any]]:
    run_dir = next(work.iterdir())
    return run_dir, json.loads((run_dir / "run.json").read_text(encoding="utf-8"))


def test_retry_preserves_logs_and_records_attempts(tmp_path: Path) -> None:
    brief = write(tmp_path / "brief.md", brief_text())
    runner = RecordingRunner({"codex_tech"})
    work = tmp_path / "runs"
    assert invoke(run_args(brief, work), reviewer=runner) == 0
    run_dir, state = load_state(work)
    assert (run_dir / "codex_tech.md").read_text(encoding="utf-8") != (
        run_dir / "codex_tech.retry1.md"
    ).read_text(encoding="utf-8")
    attempts = state["reviewers"]["codex_tech"]["attempts"]
    assert len(attempts) == 2
    assert attempts[0]["gate_error"]
    assert attempts[1]["gate_error"] is None
    assert attempts[0]["tokens"] == 1234


def test_report_cites_the_retry_log_for_a_retried_reviewer(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """回帰 (2026-09-20 の実走): 再実行した者の指摘が、再実行前 (=ゲート不合格) のログを指していた。"""
    brief = write(tmp_path / "brief.md", brief_text())
    work = tmp_path / "runs"
    assert invoke(run_args(brief, work), reviewer=RecordingRunner({"codex_tech"})) == 0
    report = capsys.readouterr().out
    assert "codex_tech.retry1.md" in report
    assert "codex_tech.md " not in report and "codex_tech.md\n" not in report
    for untouched in ("codex_crit", "ds_tech", "ds_crit"):  # 再実行していない者は元のログを指す
        assert f"{untouched}.retry1" not in report


def test_trailing_text_after_the_json_block_is_accepted() -> None:
    """回帰 (実走): JSON ブロックの後ろに一言添えただけで再実行になり、費用を無駄にしていた。"""
    item = next(reviewer for reviewer in target.REVIEWERS if reviewer.name == "codex_tech")
    good = review_output(item, valid=True)
    parsed = target.parse_review(good + "\n以上です。ご確認ください。\n", item)
    assert parsed.findings
    assert "以上です" in parsed.body
    with pytest.raises(ValueError):  # ブロックが 2 つ・壊れた JSON などは従来どおり不合格
        target.parse_review(good + "\n```json\n{}\n```\n", item)


def test_codex_children_get_a_least_privilege_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """回帰 (実走のレビュー): Codex へ全環境変数 (DeepSeek 等の鍵) を継承していた。"""
    captured: list[dict[str, str]] = []

    def fake_subprocess(command: list[str], *, stdin: Any, cwd: Path, timeout: int, env: dict[str, str]) -> Any:
        captured.append(env)
        return target.ExecResult(0, "", "")

    monkeypatch.setattr(target, "_subprocess", fake_subprocess)
    for key, value in {
        "DEEPSEEK_API_KEY": "d", "DASHSCOPE_API_KEY": "q", "OPENAI_API_KEY": "o",
        "GEMINI_API_KEY": "g", "CODEX_HOME": "h", "USERPROFILE": "u", "PATH": "p",
    }.items():
        monkeypatch.setenv(key, value)
    codex_spec = next(item for item in target.REVIEWERS if item.vendor == "Codex")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    target.default_reviewer_runner(codex_spec, "入力", run_dir, 5, "codex", TOOLS, "medium")
    target.default_integrator_runner("入力", run_dir, 5, "codex")
    assert len(captured) == 2
    for env in captured:
        names = {key.upper() for key in env}
        assert {"CODEX_HOME", "USERPROFILE", "PATH"} <= names
        assert not names & {"DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"}


def test_report_lists_titles_of_the_rows_it_had_to_omit(tmp_path: Path) -> None:
    """Claude はレポートしか読まない。表から外した指摘も、存在と題名は見えること。"""
    metadata: dict[str, dict[str, str]] = {}
    clusters: list[dict[str, Any]] = []
    for number in range(90):
        finding_id = f"R1#{number}"
        metadata[finding_id] = {
            "kind": "technical", "vendor": "Codex", "reviewer": "R1",
            "reviewer_name": "codex_tech", "severity": "🟡", "headline": "見出し",
        }
        clusters.append({
            "id": f"C{number}", "kind": "technical", "title": f"題名{number:02d}",
            "members": [finding_id], "proposal": "案", "adopt": "採用", "adopt_reason": "理由",
            "severity": "🟡", "vendors": ["Codex"], "single_source": False,
        })
    payload = {"summary": "総評", "next_actions": ["次へ"], "questions_for_user": []}
    report = target.build_report(
        "run", 1, clusters, metadata, payload, [],
        {"codex_calls": 1, "codex_tokens": 1, "ds_calls": 0, "ds_yen": 0}, tmp_path, True,
    )
    omitted_rows = [line for line in report.splitlines() if line.startswith("| 他 ")]
    assert omitted_rows and "run.json 参照" in omitted_rows[0]
    assert "題名" in omitted_rows[0]
    assert "※ 採否・対応案は統合 AI の提案です" in report


def test_integration_retry_preserves_first_log(tmp_path: Path) -> None:
    brief = write(tmp_path / "brief.md", brief_text())
    work = tmp_path / "runs"
    assert invoke(run_args(brief, work), integrator=FakeIntegrator(1)) == 0
    run_dir, _ = load_state(work)
    assert (run_dir / "integration.md").exists()
    assert (run_dir / "integration.retry1.md").exists()


@pytest.mark.parametrize(
    ("expected", "runner", "integrator", "extra"),
    [
        # 使える者が 2 者未満 (3 者が実行失敗) なら従来どおり 10
        (10, RecordingRunner(exit_codes={"ds_crit": 124, "ds_tech": 1, "codex_crit": 1}), FakeIntegrator(), ()),
        # --no-partial なら 1 者の実行失敗でも従来どおり 10
        (10, RecordingRunner(exit_codes={"ds_crit": 124}), FakeIntegrator(), ("--no-partial",)),
        (12, RecordingRunner(set(item.name for item in target.REVIEWERS), always_invalid=True), FakeIntegrator(), ()),
        (13, RecordingRunner(), FakeIntegrator(2), ()),
    ],
)
def test_failures_include_all_attempt_costs(
    tmp_path: Path,
    expected: int,
    runner: RecordingRunner,
    integrator: FakeIntegrator,
    extra: tuple[str, ...],
) -> None:
    brief = write(tmp_path / "brief.md", brief_text())
    work = tmp_path / "runs"
    assert invoke(run_args(brief, work, extra=extra), reviewer=runner, integrator=integrator) == expected
    _, state = load_state(work)
    assert "costs" in state
    assert state["costs"]["codex_calls"] >= 2
    assert state["costs"]["codex_tokens"] >= 2468


def test_report_keeps_sections_red_clusters_and_marks_overflow(tmp_path: Path) -> None:
    metadata: dict[str, dict[str, str]] = {}
    clusters: list[dict[str, Any]] = []
    for number in range(70):
        finding_id = f"R1#{number}"
        metadata[finding_id] = {
            "kind": "technical", "vendor": "Codex", "reviewer": "R1",
            "reviewer_name": "codex_tech", "severity": "🔴", "headline": "重大指摘",
        }
        clusters.append({
            "id": f"C{number}", "kind": "technical", "title": f"赤{number}",
            "members": [finding_id], "proposal": "直す", "adopt": "採用",
            "adopt_reason": "理由", "severity": "🔴", "vendors": ["Codex"],
            "single_source": True,
        })
    payload = {"summary": "総評", "next_actions": ["次へ"], "questions_for_user": []}
    report = target.build_report(
        "run", 1, clusters, metadata, payload, [],
        {"codex_calls": 1, "codex_tokens": 1, "ds_calls": 0, "ds_yen": 0},
        tmp_path, True,
    )
    for heading in ("## 総評", "## ユーザーへの質問", "## 次アクション", "## 費用", "## 生ログ"):
        assert heading in report
    assert report.count("codex_tech.md") == 70
    assert "⚠ 行数超過" in report


def test_report_trim_keeps_red_rows_and_their_proposals_in_the_table(tmp_path: Path) -> None:
    """回帰: 行数削減が 🔴 の行まで表から外し、採否・対応案が消えていた。外すのは 🔴 以外だけ。"""
    metadata: dict[str, dict[str, str]] = {}
    clusters: list[dict[str, Any]] = []
    for number in range(90):
        severity = "🔴" if number < 3 else "🟡"
        finding_id = f"R1#{number}"
        metadata[finding_id] = {
            "kind": "technical", "vendor": "Codex", "reviewer": "R1",
            "reviewer_name": "codex_tech", "severity": severity, "headline": "見出し",
        }
        clusters.append({
            "id": f"C{number}", "kind": "technical", "title": f"指摘{number}",
            "members": [finding_id], "proposal": f"対応案{number}", "adopt": "採用",
            "adopt_reason": "理由", "severity": severity, "vendors": ["Codex"],
            "single_source": False,
        })
    payload = {"summary": "総評", "next_actions": ["次へ"], "questions_for_user": []}
    report = target.build_report(
        "run", 1, clusters, metadata, payload, [],
        {"codex_calls": 1, "codex_tokens": 1, "ds_calls": 0, "ds_yen": 0},
        tmp_path, True,
    )
    for number in range(3):
        assert f"| 指摘{number} |" in report
        assert f"対応案{number}" in report
    assert "他 " in report and "run.json 参照" in report
    assert len(report.splitlines()) <= 90
    assert "⚠ 行数超過" not in report


def test_engine_api_contract() -> None:
    """回帰 (2026-09-20 の実走): エンジンの関数名を推測で呼び、例外を握りつぶして PATH の npm shim と
    週枠「不明」へ黙って落ちていた。実在する API を名指しで守る (名前が変わったらここで落ちる)。"""
    import cgd_lv0_codex

    for name in target.ENGINE_API:
        assert callable(getattr(cgd_lv0_codex, name, None)), f"cgd_lv0_codex.{name} が無い"


def test_resolve_and_weekly_use_the_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    class Choice:
        exe = Path("C:/tools/codex-cli/codex.exe")

    class FakeEngine:
        @staticmethod
        def candidate_bins() -> list[str]:
            return ["c"]

        @staticmethod
        def choose_bin(candidates: list[str]) -> Choice:
            assert candidates == ["c"]
            return Choice()

        @staticmethod
        def latest_rate_limits() -> list[dict[str, Any]]:
            return [{"w": 1}]

        @staticmethod
        def weekly_percent(windows: list[dict[str, Any]]) -> float:
            assert windows == [{"w": 1}]
            return 25.0

    monkeypatch.setitem(sys.modules, "cgd_lv0_codex", FakeEngine)
    assert target.resolve_codex() == str(Choice.exe)
    assert target.get_weekly_percent() == 25.0


def test_weekly_unknown_when_engine_has_no_data(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeEngine:
        @staticmethod
        def latest_rate_limits() -> list[dict[str, Any]]:
            return []

        @staticmethod
        def weekly_percent(windows: list[dict[str, Any]]) -> None:
            return None

    monkeypatch.setitem(sys.modules, "cgd_lv0_codex", FakeEngine)
    assert target.get_weekly_percent() is None


def test_deepseek_env_allow_list_keeps_windows_essentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """許可リストが狭すぎて DeepSeek の起動・通信が環境次第で失敗しないこと。鍵は渡さない。"""
    captured: dict[str, Any] = {}

    def fake_subprocess(command: list[str], *, stdin: Any, cwd: Path, timeout: int, env: dict[str, str]) -> Any:
        captured["env"] = env
        return target.ExecResult(0, "", "")

    monkeypatch.setattr(target, "_subprocess", fake_subprocess)
    for key, value in {
        "USERNAME": "u", "HOMEDRIVE": "C:", "HOMEPATH": "\\Users\\u", "ALL_PROXY": "p",
        "SSL_CERT_FILE": "c", "OPENAI_API_KEY": "x", "DASHSCOPE_API_KEY": "y", "DEEPSEEK_API_KEY": "z",
    }.items():
        monkeypatch.setenv(key, value)
    spec_item = next(item for item in target.REVIEWERS if item.vendor == "DeepSeek")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    target.default_reviewer_runner(spec_item, "入力", run_dir, 5, "codex", TOOLS, "medium")
    env = {key.upper(): value for key, value in captured["env"].items()}
    for key in ("USERNAME", "HOMEDRIVE", "HOMEPATH", "ALL_PROXY", "SSL_CERT_FILE", "DEEPSEEK_API_KEY"):
        assert key in env, key
    assert "OPENAI_API_KEY" not in env and "DASHSCOPE_API_KEY" not in env


@pytest.mark.parametrize(
    "secret",
    [
        "-----BEGIN RSA PRIVATE KEY-----", "eyJabcdefghijk.eyJabcdefghijk",
        "postgres://user:password@host/db", "aws_secret_access_key = value",
        "secret: abcdefgh", "token=abcdefghijklmnop",
    ],
)
def test_new_secret_patterns(secret: str) -> None:
    hits = target.secret_hits("brief", "safe\n" + secret)
    assert hits and "行 2" in hits[0]
    assert secret not in hits[0]


def test_passwd_requires_assignment() -> None:
    assert target.secret_hits("brief", "uses getpwnam from passwd module") == []
    assert target.secret_hits("brief", "passwd = hidden")


def test_weekly_unknown_continues_and_is_recorded(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    brief = write(tmp_path / "brief.md", brief_text())
    work = tmp_path / "runs"
    assert invoke(run_args(brief, work), weekly=lambda: None) == 0
    captured = capsys.readouterr()
    assert "週枠: 不明（取得失敗）" in captured.out
    _, state = load_state(work)
    assert state["weekly_percent"] is None


def test_plan_reports_unknown_weekly_percent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    brief = write(tmp_path / "brief.md", brief_text())
    assert invoke(["plan", "--brief", str(brief)], weekly=lambda: None) == 0
    assert "週枠: 不明（取得失敗）" in capsys.readouterr().out


def test_deepseek_environment_is_allowlisted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def fake_subprocess(
        command: list[str], *, stdin: str | None, cwd: Path, timeout: int, env: dict[str, str]
    ) -> target.ExecResult:
        del command, stdin, cwd, timeout
        captured.update(env)
        return target.ExecResult(0, "", "")

    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    monkeypatch.setenv("OPENAI_API_KEY", "openai")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dash")
    monkeypatch.setattr(target, "_subprocess", fake_subprocess)
    target.default_reviewer_runner(spec("ds_tech"), "input", tmp_path, 1, "codex", tmp_path, "high")
    assert captured["DEEPSEEK_API_KEY"] == "ds"
    assert "OPENAI_API_KEY" not in captured
    assert "DASHSCOPE_API_KEY" not in captured


@pytest.mark.parametrize("case", ["actions", "adopt", "cluster_id", "question_id", "title", "summary"])
def test_integration_new_validation(case: str) -> None:
    metadata, payload = integration_fixture()
    if case == "actions":
        payload["next_actions"] = [1]
    elif case == "adopt":
        payload["clusters"][0]["adopt"] = "保留"
    elif case == "cluster_id":
        payload["clusters"][1]["id"] = payload["clusters"][0]["id"]
    elif case == "question_id":
        question = {"id": "Q1", "question": "?", "options": [{"label": "A"}, {"label": "B"}], "recommended": "A"}
        payload["questions_for_user"] = [question, dict(question)]
    elif case == "title":
        payload["clusters"][0]["title"] = "x" * 61
    else:
        payload["summary"] = 1
    assert target.validate_integration(payload, metadata)


def test_import_has_no_process_wide_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    class StdoutProbe:
        def __init__(self) -> None:
            self.reconfigure_calls = 0

        def reconfigure(self, **kwargs: Any) -> None:
            del kwargs
            self.reconfigure_calls += 1

    probe = StdoutProbe()
    path = TOOLS / "cgd_lv3a.py"
    before = sys.dont_write_bytecode
    monkeypatch.setattr(sys, "stdout", probe)
    module_spec = importlib.util.spec_from_file_location("cgd_lv3a_side_effect_test", path)
    assert module_spec and module_spec.loader
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    assert probe.reconfigure_calls == 0
    assert sys.dont_write_bytecode is before


def test_size_limit_is_checked_before_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    brief = write(tmp_path / "brief.md", brief_text())
    large = write(tmp_path / "large.txt", "x" * target.MAX_INPUT_BYTES)
    monkeypatch.setattr(target, "read_utf8", lambda path: pytest.fail(f"read: {path}"))
    with pytest.raises(target.FrontError, match="200KB"):
        target.validate_inputs(brief, [large])


def test_run_directory_suffix_is_six_hex_digits(tmp_path: Path) -> None:
    run_dir = target.create_run_dir(tmp_path, "label")
    suffix = run_dir.name.rsplit("_", 1)[1]
    assert len(suffix) == 6
    assert all(character in "0123456789abcdef" for character in suffix)


def test_retries_are_parallel_and_effort_is_argument(tmp_path: Path) -> None:
    brief = write(tmp_path / "brief.md", brief_text())
    runner = RecordingRunner({"codex_tech", "ds_tech"})
    runner.retry_barrier = threading.Barrier(2)
    started = time.monotonic()
    assert invoke(run_args(brief, tmp_path / "runs"), reviewer=runner) == 0
    assert time.monotonic() - started < 1
    assert runner.efforts and set(runner.efforts) == {"medium"}
    assert not hasattr(target.default_reviewer_runner, "effort")


def test_stderr_is_utf8_even_when_the_console_code_page_is_not(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """回帰 (実走): stdout だけ UTF-8 にしていたため、停止理由 (stderr) が cp932 のままで文字化けした。"""
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(raw, encoding="cp932", write_through=True))
    assert target.main(["plan", "--brief", str(tmp_path / "none.md")]) == 1
    assert "依頼文が存在しません" in raw.getvalue().decode("utf-8")
