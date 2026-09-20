from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Sequence

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import cgd_lv3a as target  # noqa: E402


def brief_text(extra: str = "") -> str:
    return f"# 依頼\n\n## 確認済みの事実\n\n- 現行処理は手動です\n{extra}"


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8", newline="")
    return path


def spec(name: str) -> target.ReviewerSpec:
    return next(item for item in target.REVIEWERS if item.name == name)


def review_output(item: target.ReviewerSpec, *, valid: bool = True) -> str:
    body = "詳しい指摘です。" * 30
    if not valid:
        return body
    finding = {"id": f"{item.prefix}1", "severity": item.severities[0], "headline": f"{item.name} の指摘"}
    return body + "\n```json\n" + json.dumps({"findings": [finding]}, ensure_ascii=False) + "\n```\n"


def integration_output(prompt: str, *, omit_last: bool = False) -> str:
    ids: list[tuple[str, str]] = []
    current_kind = "technical"
    for line in prompt.splitlines():
        if line.startswith("## R"):
            current_kind = "critic" if "(critic)" in line else "technical"
        if line.startswith("R") and ": [" in line:
            ids.append((line.split(":", 1)[0], current_kind))
    if omit_last:
        ids = ids[:-1]
    clusters = [
        {
            "id": f"C{number}",
            "kind": kind,
            "title": f"統合指摘 {number}",
            "members": [finding_id],
            "proposal": "修正する",
            "adopt": "採用",
            "adopt_reason": "レビューに基づく",
        }
        for number, (finding_id, kind) in enumerate(ids, 1)
    ]
    payload = {"summary": "総評です。", "clusters": clusters, "questions_for_user": [], "next_actions": ["修正する"]}
    return json.dumps(payload, ensure_ascii=False)


class FakeReviewRunner:
    def __init__(self, invalid_counts: dict[str, int] | None = None, exit_codes: dict[str, int] | None = None) -> None:
        self.invalid_counts = invalid_counts or {}
        self.exit_codes = exit_codes or {}
        self.calls: dict[str, int] = {}

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
        del prompt, cwd, timeout, codex_path, tools, effort
        count = self.calls.get(item.name, 0) + 1
        self.calls[item.name] = count
        invalid = count <= self.invalid_counts.get(item.name, 0)
        return target.ExecResult(self.exit_codes.get(item.name, 0), review_output(item, valid=not invalid), "")


class FakeIntegrator:
    def __init__(self, invalid_count: int = 0) -> None:
        self.invalid_count = invalid_count
        self.calls = 0
        self.prompts: list[str] = []

    def __call__(self, prompt: str, cwd: Path, timeout: int, codex_path: str) -> target.ExecResult:
        del cwd, timeout, codex_path
        self.calls += 1
        self.prompts.append(prompt)
        return target.ExecResult(0, integration_output(prompt, omit_last=self.calls <= self.invalid_count), "")


def run_args(brief: Path, work_root: Path, *, no_ds: bool = False, extra: Sequence[str] = ()) -> list[str]:
    args = ["run", "--brief", str(brief), "--work-root", str(work_root), "--label", "test"]
    if no_ds:
        args.append("--no-ds")
    args.extend(extra)
    return args


def load_run(work_root: Path) -> tuple[Path, dict[str, Any]]:
    """work_root の唯一の run ディレクトリと、その run.json。"""
    run_dir = next(work_root.iterdir())
    return run_dir, json.loads((run_dir / "run.json").read_text(encoding="utf-8"))


def invoke(
    args: list[str],
    reviewer: FakeReviewRunner | None = None,
    integrator: FakeIntegrator | None = None,
    *,
    resolver: Any = lambda: "codex",
    weekly: Any = lambda: 0.0,
) -> int:
    return target.main(
        args,
        reviewer_runner=reviewer or FakeReviewRunner(),
        integrator_runner=integrator or FakeIntegrator(),
        usage_logger=lambda: None,
        resolver=resolver,
        weekly=weekly,
    )


def test_build_review_input_includes_file_heading(tmp_path: Path) -> None:
    source = write(tmp_path / "sample.py", "print('ok')\n")
    brief = write(tmp_path / "brief.md", brief_text())
    brief_value, loaded, _ = target.validate_inputs(brief, [source])
    packed = target.build_review_input(brief_value, loaded)
    assert f"### {source}" in packed
    assert "```python" in packed
    assert "print('ok')" in packed


@pytest.mark.parametrize("case", ["missing", "large", "no_facts"])
def test_front_validation_errors(tmp_path: Path, case: str) -> None:
    brief = write(tmp_path / "brief.md", brief_text())
    files: list[Path] = []
    if case == "missing":
        files = [tmp_path / "none.py"]
    elif case == "large":
        files = [write(tmp_path / "large.txt", "x" * (target.MAX_INPUT_BYTES + 1))]
    else:
        brief = write(tmp_path / "brief.md", "# 依頼\n")
    with pytest.raises(target.FrontError):
        target.validate_inputs(brief, files)


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ABCDEFGHIJKL",
        "AKIA1234567890AB",
        "api_key: hidden",
        "password=hidden",
        "passwd=hidden",
        "Bearer abcdefghij.12",
        "person@example.com",
    ],
)
def test_secret_scan_reports_only_line(tmp_path: Path, capsys: pytest.CaptureFixture[str], secret: str) -> None:
    brief = write(tmp_path / "brief.md", brief_text(f"\n{secret}\n"))
    code = invoke(["plan", "--brief", str(brief)])
    captured = capsys.readouterr()
    assert code == 1
    assert "行" in captured.err
    assert secret not in captured.err


def test_parse_review_valid() -> None:
    item = spec("codex_tech")
    parsed = target.parse_review(review_output(item), item)
    assert parsed.findings[0]["id"] == "CT1"


@pytest.mark.parametrize("variant", ["none", "two", "broken", "duplicate", "severity"])
def test_parse_review_rejects_invalid_json_forms(variant: str) -> None:
    item = spec("codex_tech")
    body = "本文。" * 80
    good = {"findings": [{"id": "CT1", "severity": "🔴", "headline": "指摘"}]}
    if variant == "none":
        output = body
    elif variant == "two":
        block = "```json\n{}\n```"
        output = body + block + block
    elif variant == "broken":
        output = body + "```json\n{broken}\n```"
    elif variant == "duplicate":
        good["findings"].append({"id": "CT1", "severity": "🟠", "headline": "重複"})
        output = body + "```json\n" + json.dumps(good, ensure_ascii=False) + "\n```"
    else:
        good["findings"][0]["severity"] = "高"
        output = body + "```json\n" + json.dumps(good, ensure_ascii=False) + "\n```"
    with pytest.raises(ValueError):
        target.parse_review(output, item)


def parsed_reviews() -> dict[str, target.ParsedReview]:
    return {item.name: target.parse_review(review_output(item), item) for item in target.REVIEWERS}


def test_anonymize_is_deterministic_and_records_mapping() -> None:
    parsed = parsed_reviews()
    first, mapping, _ = target.anonymize(parsed, target.REVIEWERS, "run-one")
    again, again_mapping, _ = target.anonymize(parsed, target.REVIEWERS, "run-one")
    assert first == again
    assert mapping == again_mapping
    assert set(mapping) == {"R1", "R2", "R3", "R4"}
    alternatives = {tuple(target.anonymize(parsed, target.REVIEWERS, f"other-{index}")[1].values()) for index in range(10)}
    assert tuple(mapping.values()) not in alternatives or len(alternatives) > 1


def integration_fixture() -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    metadata = {
        "R1#1": {"kind": "technical", "vendor": "Codex", "reviewer": "R1", "reviewer_name": "codex_tech", "severity": "🟠", "headline": "A"},
        "R2#1": {"kind": "technical", "vendor": "DeepSeek", "reviewer": "R2", "reviewer_name": "ds_tech", "severity": "🔴", "headline": "B"},
        "R3#1": {"kind": "critic", "vendor": "Codex", "reviewer": "R3", "reviewer_name": "codex_crit", "severity": "中", "headline": "C"},
    }
    payload = {
        "summary": "総評",
        "clusters": [
            {"id": "C1", "kind": "technical", "title": "技術", "members": ["R1#1", "R2#1"], "proposal": "直す", "adopt": "採用", "adopt_reason": "理由"},
            {"id": "C2", "kind": "critic", "title": "批評", "members": ["R3#1"], "proposal": "改善", "adopt": "部分採用", "adopt_reason": "理由"},
        ],
        "questions_for_user": [],
        "next_actions": [],
    }
    return metadata, payload


@pytest.mark.parametrize("case", ["missing", "duplicate", "unknown", "mixed", "recommended"])
def test_gate2_rejects_invalid_assignments(case: str) -> None:
    metadata, payload = integration_fixture()
    if case == "missing":
        payload["clusters"][0]["members"].remove("R2#1")
    elif case == "duplicate":
        payload["clusters"][1]["members"].append("R1#1")
    elif case == "unknown":
        payload["clusters"][0]["members"].append("R9#9")
    elif case == "mixed":
        payload["clusters"][0]["members"].append("R3#1")
        payload["clusters"][1]["members"] = []
    else:
        payload["questions_for_user"] = [{"id": "Q1", "question": "選ぶ?", "options": [{"label": "A"}, {"label": "B"}], "recommended": "C", "recommended_reason": "R1#1"}]
    assert target.validate_integration(payload, metadata)


def test_machine_enrichment_calculates_max_vendor_and_single_source() -> None:
    metadata, payload = integration_fixture()
    clusters = target.enrich_clusters(payload, metadata)
    assert clusters[0]["severity"] == "🔴"
    assert set(clusters[0]["vendors"]) == {"Codex", "DeepSeek"}
    assert clusters[0]["single_source"] is False
    assert clusters[1]["single_source"] is True


def test_report_has_tables_red_details_limit_and_missing_reason(tmp_path: Path) -> None:
    metadata, payload = integration_fixture()
    payload["questions_for_user"] = [
        {"id": "Q1", "question": "選ぶ?", "options": [{"label": "A", "description": "a"}, {"label": "B", "description": "b"}], "recommended": "A", "recommended_reason": "根拠不明"}
    ]
    questions = target.prepare_questions(payload, brief_text(), metadata)
    report = target.build_report(
        "run", 1.0, target.enrich_clusters(payload, metadata), metadata, payload, questions,
        {"codex_calls": 3, "codex_tokens": 100, "ds_calls": 2, "ds_yen": 1.5}, tmp_path, False,
    )
    # 採否は AI の提案で、最終判断は利用者。列見出しも「採否案」にする (JSON の項目名 adopt は据え置き)
    assert "| 指摘 | 重大度 | Codex | DeepSeek | 採否案 | 対応案 |" in report
    assert "| 観点 | 困り度 | Codex | DeepSeek | 採否案 | 改善の方向 |" in report
    assert "| 採否 |" not in report
    assert "R2#1 (DeepSeek): B" in report
    assert "(根拠なし)" in report
    assert len(report.splitlines()) <= 90


def test_end_to_end_success_and_mapping_in_run_json(tmp_path: Path) -> None:
    brief = write(tmp_path / "brief.md", brief_text())
    work = tmp_path / "runs"
    assert invoke(run_args(brief, work)) == 0
    run_dir = next(work.iterdir())
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] == "success"
    assert set(state["mapping"]) == {"R1", "R2", "R3", "R4"}
    assert (run_dir / "report.md").exists()
    assert (run_dir / "questions.json").exists()


def test_reviewer_invalid_json_retries_once_then_succeeds(tmp_path: Path) -> None:
    brief = write(tmp_path / "brief.md", brief_text())
    runner = FakeReviewRunner({"codex_tech": 1})
    assert invoke(run_args(brief, tmp_path / "runs"), runner) == 0
    assert runner.calls["codex_tech"] == 2
    assert all(count == 1 for name, count in runner.calls.items() if name != "codex_tech")


def test_reviewer_invalid_twice_returns_12(tmp_path: Path) -> None:
    """使える者が 2 者未満 (3 者が再実行後も JSON 不正) なら、従来どおり exit 12。"""
    brief = write(tmp_path / "brief.md", brief_text())
    runner = FakeReviewRunner({"codex_tech": 2, "codex_crit": 2, "ds_tech": 2})
    assert invoke(run_args(brief, tmp_path / "runs"), runner) == 12
    assert runner.calls["codex_tech"] == 2


def test_reviewer_nonzero_returns_10_without_retry(tmp_path: Path) -> None:
    """使える者が 2 者未満 (3 者が実行失敗) なら、従来どおり exit 10 で再実行しない。"""
    brief = write(tmp_path / "brief.md", brief_text())
    runner = FakeReviewRunner(exit_codes={"ds_crit": 124, "ds_tech": 1, "codex_crit": 1})
    assert invoke(run_args(brief, tmp_path / "runs"), runner) == 10
    assert runner.calls["ds_crit"] == 1


def test_integration_missing_retries_once_then_succeeds(tmp_path: Path) -> None:
    brief = write(tmp_path / "brief.md", brief_text())
    integrator = FakeIntegrator(1)
    assert invoke(run_args(brief, tmp_path / "runs"), integrator=integrator) == 0
    assert integrator.calls == 2


def test_integration_missing_twice_returns_13(tmp_path: Path) -> None:
    brief = write(tmp_path / "brief.md", brief_text())
    integrator = FakeIntegrator(2)
    assert invoke(run_args(brief, tmp_path / "runs"), integrator=integrator) == 13
    assert integrator.calls == 2


def test_weekly_limit_returns_11_without_creating_run(tmp_path: Path) -> None:
    brief = write(tmp_path / "brief.md", brief_text())
    work = tmp_path / "runs"
    assert invoke(run_args(brief, work), weekly=lambda: 80.0) == 11
    assert not work.exists()


def test_missing_codex_returns_2_without_creating_run(tmp_path: Path) -> None:
    brief = write(tmp_path / "brief.md", brief_text())
    work = tmp_path / "runs"
    assert invoke(run_args(brief, work), resolver=lambda: None) == 2
    assert not work.exists()


@pytest.mark.parametrize("command", ["plan", "run"])
def test_subcommand_help(command: str) -> None:
    assert target.main([command, "--help"]) == 0
