"""一部のレビュアーが失敗したときの「暫定版」(exit 20) のテスト。

使える者 (実行成功かつ JSON ゲート合格) が 2 者以上なら、その者だけで統合して暫定版を返す。
2 者未満、または --no-partial なら従来どおり (実行失敗 exit 10 / JSON 不正 exit 12)。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import cgd_lv3a as target  # noqa: E402
from test_cgd_lv3a import (  # noqa: E402
    FakeIntegrator,
    FakeReviewRunner,
    brief_text,
    integration_output,
    invoke,
    load_run,
    review_output,
    run_args,
    write,
)
from test_cgd_lv3a_fixes import RecordingRunner  # noqa: E402


class ScriptedRunner:
    """呼出の n 回目ごとに (終了コード, JSON が正しいか) を台本どおりに返す。台本に無い呼出は成功。"""

    def __init__(self, script: dict[str, list[tuple[int, bool]]]) -> None:
        self.script = script
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
        index = self.calls.get(item.name, 0)
        self.calls[item.name] = index + 1
        steps = self.script.get(item.name, [])
        code, valid = steps[index] if index < len(steps) else (0, True)
        return target.ExecResult(code, review_output(item, valid=valid), "")


class RogueIntegrator(FakeIntegrator):
    """使える者に無い ID (欠けた者の R4#1) を勝手に足す統合者。ゲート 2 が止めること。"""

    def __call__(self, prompt: str, cwd: Path, timeout: int, codex_path: str) -> target.ExecResult:
        result = super().__call__(prompt, cwd, timeout, codex_path)
        payload = json.loads(result.stdout)
        payload["clusters"].append(
            {
                "id": "C99", "kind": "technical", "title": "欠けた者の指摘", "members": ["R4#1"],
                "proposal": "案", "adopt": "採用", "adopt_reason": "理由",
            }
        )
        return target.ExecResult(0, json.dumps(payload, ensure_ascii=False), "")


def brief_file(tmp_path: Path) -> Path:
    return write(tmp_path / "brief.md", brief_text())


def test_one_execution_failure_gives_partial_report_and_exit_20(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeReviewRunner(exit_codes={"ds_crit": 124})
    work = tmp_path / "runs"
    assert invoke(run_args(brief_file(tmp_path), work), runner) == 20
    assert runner.calls["ds_crit"] == 1  # 実行失敗は再実行しない
    _, state = load_run(work)
    assert state["status"] == "partial"
    assert state["exit_code"] == 20
    assert state["partial"] == {"missing": [{"name": "ds_crit", "reason": "タイムアウト"}]}
    assert set(state["mapping"]) == {"R1", "R2", "R3"}
    assert "ds_crit" not in state["mapping"].values()
    report = capsys.readouterr().out
    assert "状態: 暫定（欠落: ds_crit=タイムアウト）" in report
    assert "⚠ 暫定: ds_crit が欠けています。収束の判定が弱く、DeepSeek(批評) の指摘が出ていません" in report
    lines = report.splitlines()
    status_index = next(i for i, line in enumerate(lines) if "状態: 暫定" in line)
    assert lines[status_index + 1].startswith("⚠ 暫定:")  # 見出し行の直後


def test_exit_code_of_the_failed_run_is_the_reason(tmp_path: Path) -> None:
    runner = FakeReviewRunner(exit_codes={"codex_crit": 1})
    work = tmp_path / "runs"
    assert invoke(run_args(brief_file(tmp_path), work), runner) == 20
    _, state = load_run(work)
    assert state["partial"]["missing"] == [{"name": "codex_crit", "reason": "実行失敗(終了コード1)"}]


def test_two_execution_failures_still_give_partial(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    integrator = FakeIntegrator()
    work = tmp_path / "runs"
    code = invoke(
        run_args(brief_file(tmp_path), work),
        FakeReviewRunner(exit_codes={"ds_tech": 124, "ds_crit": 124}),
        integrator,
    )
    assert code == 20
    _, state = load_run(work)
    assert [item["name"] for item in state["partial"]["missing"]] == ["ds_tech", "ds_crit"]
    assert set(state["mapping"]) == {"R1", "R2"}
    assert sorted(state["mapping"].values()) == ["codex_crit", "codex_tech"]
    report = capsys.readouterr().out
    assert "状態: 暫定（欠落: ds_tech=タイムアウト, ds_crit=タイムアウト）" in report
    assert "DeepSeek(技術)、DeepSeek(批評)" in report


def test_integration_input_covers_only_the_usable_reviewers(tmp_path: Path) -> None:
    integrator = FakeIntegrator()
    runner = FakeReviewRunner(exit_codes={"ds_crit": 124})
    assert invoke(run_args(brief_file(tmp_path), tmp_path / "runs"), runner, integrator) == 20
    assert integrator.calls == 1
    prompt = integrator.prompts[0]
    assert "R1〜R3 の出所は伏せてある" in prompt
    assert "## R3 " in prompt and "## R4 " not in prompt
    assert "ds_crit" not in prompt  # 欠けた者の名前は統合者に渡さない (匿名性)


def test_full_review_keeps_r1_to_r4_in_the_integration_input(tmp_path: Path) -> None:
    integrator = FakeIntegrator()
    assert invoke(run_args(brief_file(tmp_path), tmp_path / "runs"), integrator=integrator) == 0
    assert "R1〜R4 の出所は伏せてある" in integrator.prompts[0]


def test_gate2_only_knows_the_usable_reviewers(tmp_path: Path) -> None:
    """統合者が欠けた者の ID を足しても、ゲート 2 が止める (メタデータは使える者だけ)。"""
    work = tmp_path / "runs"
    code = invoke(
        run_args(brief_file(tmp_path), work),
        FakeReviewRunner(exit_codes={"ds_crit": 124}),
        RogueIntegrator(),
    )
    assert code == 13
    _, state = load_run(work)
    assert state["status"] == "gate2_failed"
    assert state["partial"]["missing"][0]["name"] == "ds_crit"  # 失敗時の run.json にも欠落が残る


def test_json_invalid_after_retry_is_missing_with_reason(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeReviewRunner({"ds_tech": 2})
    work = tmp_path / "runs"
    assert invoke(run_args(brief_file(tmp_path), work), runner) == 20
    assert runner.calls["ds_tech"] == 2  # JSON 不正は 1 回だけ再実行する
    _, state = load_run(work)
    (missing,) = state["partial"]["missing"]
    assert missing["name"] == "ds_tech"
    assert missing["reason"].startswith("JSON不正: ")
    assert "状態: 暫定（欠落: ds_tech=JSON不正）" in capsys.readouterr().out


def test_two_json_failures_still_give_partial_with_both_reasons(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeReviewRunner({"codex_crit": 2, "ds_tech": 2})
    work = tmp_path / "runs"
    assert invoke(run_args(brief_file(tmp_path), work), runner) == 20
    _, state = load_run(work)
    missing = state["partial"]["missing"]
    assert [item["name"] for item in missing] == ["codex_crit", "ds_tech"]
    assert all(item["reason"].startswith("JSON不正: ") for item in missing)
    assert set(state["mapping"].values()) == {"codex_tech", "ds_crit"}
    assert "状態: 暫定（欠落: codex_crit=JSON不正, ds_tech=JSON不正）" in capsys.readouterr().out


def test_retry_that_fails_to_run_becomes_a_missing_reviewer(tmp_path: Path) -> None:
    runner = ScriptedRunner({"ds_tech": [(0, False), (124, True)]})
    work = tmp_path / "runs"
    assert invoke(run_args(brief_file(tmp_path), work), runner) == 20
    _, state = load_run(work)
    assert state["partial"]["missing"] == [{"name": "ds_tech", "reason": "タイムアウト"}]
    assert len(state["reviewers"]["ds_tech"]["attempts"]) == 2


def test_retry_that_recovers_is_not_partial(tmp_path: Path) -> None:
    runner = ScriptedRunner({"ds_tech": [(0, False), (0, True)]})
    work = tmp_path / "runs"
    assert invoke(run_args(brief_file(tmp_path), work), runner) == 0
    _, state = load_run(work)
    assert state["status"] == "success" and "partial" not in state


def test_three_execution_failures_keep_exit_10(tmp_path: Path) -> None:
    integrator = FakeIntegrator()
    work = tmp_path / "runs"
    runner = FakeReviewRunner(exit_codes={"codex_crit": 1, "ds_tech": 1, "ds_crit": 124})
    assert invoke(run_args(brief_file(tmp_path), work), runner, integrator) == 10
    assert integrator.calls == 0
    _, state = load_run(work)
    assert state["status"] == "reviewer_execution_failed"
    assert "partial" not in state


def test_three_json_failures_keep_exit_12(tmp_path: Path) -> None:
    integrator = FakeIntegrator()
    work = tmp_path / "runs"
    runner = FakeReviewRunner({"codex_tech": 2, "ds_tech": 2, "ds_crit": 2})
    assert invoke(run_args(brief_file(tmp_path), work), runner, integrator) == 12
    assert integrator.calls == 0
    _, state = load_run(work)
    assert state["status"] == "gate1_failed"


def test_mixed_causes_below_the_minimum_report_the_execution_failure(tmp_path: Path) -> None:
    """実行失敗と JSON 不正が混ざって使える者が 1 者なら、従来どおり実行失敗 (10) を優先する。"""
    runner = FakeReviewRunner({"ds_tech": 2, "ds_crit": 2}, exit_codes={"codex_tech": 1})
    assert invoke(run_args(brief_file(tmp_path), tmp_path / "runs"), runner) == 10
    assert runner.calls["ds_tech"] == 2  # 救済の見込みがあるので再実行はした


def test_no_retry_when_partial_is_out_of_reach(tmp_path: Path) -> None:
    """3 者が実行失敗で、残る 1 者が JSON 不正。再実行しても使える者は 1 者止まりなので費用を使わない。"""
    runner = FakeReviewRunner({"ds_crit": 1}, exit_codes={"codex_tech": 1, "codex_crit": 1, "ds_tech": 1})
    assert invoke(run_args(brief_file(tmp_path), tmp_path / "runs"), runner) == 10
    assert runner.calls["ds_crit"] == 1


@pytest.mark.parametrize(
    ("expected", "runner"),
    [
        (10, FakeReviewRunner(exit_codes={"ds_crit": 124})),
        (12, FakeReviewRunner({"ds_tech": 2})),
    ],
)
def test_no_partial_restores_the_strict_behaviour(tmp_path: Path, expected: int, runner: FakeReviewRunner) -> None:
    integrator = FakeIntegrator()
    work = tmp_path / "runs"
    assert invoke(run_args(brief_file(tmp_path), work, extra=("--no-partial",)), runner, integrator) == expected
    assert integrator.calls == 0
    _, state = load_run(work)
    assert state["no_partial"] is True and "partial" not in state


def test_no_partial_stops_at_the_first_pass_without_retrying_json(tmp_path: Path) -> None:
    """--no-partial の実行失敗は従来どおり即停止。JSON 不正の者も再実行しない。"""
    runner = FakeReviewRunner({"codex_tech": 1}, exit_codes={"ds_crit": 124})
    assert invoke(run_args(brief_file(tmp_path), tmp_path / "runs", extra=("--no-partial",)), runner) == 10
    assert runner.calls["codex_tech"] == 1


def test_no_ds_with_a_failing_reviewer_is_not_partial(tmp_path: Path) -> None:
    """--no-ds は 2 者だけ。1 者が欠けると使える者が 1 者なので暫定にならず、従来の 10。"""
    runner = FakeReviewRunner(exit_codes={"codex_crit": 1})
    assert invoke(run_args(brief_file(tmp_path), tmp_path / "runs", no_ds=True), runner) == 10


def test_no_ds_success_is_unchanged(tmp_path: Path) -> None:
    integrator = FakeIntegrator()
    assert invoke(run_args(brief_file(tmp_path), tmp_path / "runs", no_ds=True), integrator=integrator) == 0
    assert "R1〜R2 の出所は伏せてある" in integrator.prompts[0]


def test_partial_costs_include_the_failed_attempt(tmp_path: Path) -> None:
    """失敗した試行 (課金された DeepSeek 呼出) も費用に入る。"""
    runner = RecordingRunner(exit_codes={"ds_crit": 124})
    work = tmp_path / "runs"
    assert invoke(run_args(brief_file(tmp_path), work), runner) == 20
    _, state = load_run(work)
    assert state["costs"]["ds_calls"] == 2  # 成功した ds_tech + 失敗した ds_crit
    assert state["costs"]["ds_yen"] == pytest.approx(5.0)
    assert state["costs"]["codex_calls"] == 3  # レビュー 2 + 統合 1


def test_partial_run_still_logs_usage(tmp_path: Path) -> None:
    logged: list[int] = []
    code = target.main(
        run_args(brief_file(tmp_path), tmp_path / "runs"),
        reviewer_runner=FakeReviewRunner(exit_codes={"ds_crit": 124}),
        integrator_runner=FakeIntegrator(),
        usage_logger=lambda: logged.append(1),
        resolver=lambda: "codex",
        weekly=lambda: 0.0,
    )
    assert code == 20 and logged == [1]


def _report_inputs(count: int) -> tuple[dict[str, dict[str, str]], list[dict[str, Any]], dict[str, Any]]:
    metadata: dict[str, dict[str, str]] = {}
    clusters: list[dict[str, Any]] = []
    for number in range(count):
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
    return metadata, clusters, {"summary": "総評", "next_actions": ["次へ"], "questions_for_user": []}


def test_report_header_and_line_budget_with_partial_and_redactions(tmp_path: Path) -> None:
    metadata, clusters, payload = _report_inputs(90)
    missing = [{"name": "ds_tech", "reason": "JSON不正: 本文が 200 バイト未満"}, {"name": "ds_crit", "reason": "実行失敗(終了コード1)"}]
    report = target.build_report(
        "run", 1, clusters, metadata, payload, [],
        {"codex_calls": 1, "codex_tokens": 1, "ds_calls": 0, "ds_yen": 0}, tmp_path, False,
        partial_missing=missing, redaction_count=3,
    )
    assert "状態: 暫定（欠落: ds_tech=JSON不正, ds_crit=実行失敗）" in report
    assert "⚠ 暫定: ds_tech、ds_crit が欠けています。" in report
    assert "伏字: 3 件（詳細は redaction.json）" in report
    assert "※ 採否・対応案は統合 AI の提案です" in report
    assert len(report.splitlines()) <= 90  # 見出しが増えた分は表の行を減らして吸収する


def test_report_without_partial_keeps_the_success_header(tmp_path: Path) -> None:
    metadata, clusters, payload = _report_inputs(2)
    report = target.build_report(
        "run", 1, clusters, metadata, payload, [],
        {"codex_calls": 1, "codex_tokens": 1, "ds_calls": 0, "ds_yen": 0}, tmp_path, True,
    )
    assert "状態: 成功 / DS なし" in report
    assert "暫定" not in report and "伏字" not in report


def test_integration_output_helper_still_matches_the_prompt_shape() -> None:
    """テスト用の統合出力が、暫定版でも R<n>#<k> の行を拾えること (ヘルパの前提を固定)。"""
    prompt = (
        "## R1 (technical)\n本文\n指摘一覧:\nR1#1: [🔴] 見出し\n\n"
        "## R2 (critic)\n本文\n指摘一覧:\nR2#1: [高] 見出し\n"
    )
    clusters = json.loads(integration_output(prompt))["clusters"]
    assert [item["members"] for item in clusters] == [["R1#1"], ["R2#1"]]
