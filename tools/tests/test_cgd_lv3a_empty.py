"""レビュアーが「指摘なし」(findings=[]) を返したときのテストと、停止メッセージ・統合の指示の回帰。

2026-09-20 の残り検証 (V3a): Codex 技術が {"findings":[]} を返しただけで JSON ゲートが不合格にし、再実行も同じで、
DeepSeek 不在と重なって使える者が 1 者になり全体が止まった (Codex 約 6.8 万 tok の無駄)。指摘なしは正当な結果。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import cgd_lv3a as target  # noqa: E402
from test_cgd_lv3a import (  # noqa: E402
    FakeIntegrator,
    FakeReviewRunner,
    brief_text,
    invoke,
    load_run,
    run_args,
    spec,
    write,
)

NO_ISSUE_BODY = "確認した観点は入力検証と境界値で、指摘はありません。" * 20


def no_issue_output() -> str:
    return NO_ISSUE_BODY + "\n```json\n" + json.dumps({"findings": []}) + "\n```\n"


class EmptyFindingsRunner(FakeReviewRunner):
    """指定した者は「指摘なし」(findings=[]) を返す。実行失敗の者はそのまま失敗させる。"""

    def __init__(
        self,
        empty: set[str],
        invalid_counts: dict[str, int] | None = None,
        exit_codes: dict[str, int] | None = None,
    ) -> None:
        super().__init__(invalid_counts, exit_codes)
        self.empty = empty

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
        result = super().__call__(item, prompt, cwd, timeout, codex_path, tools, effort)
        if item.name in self.empty and result.returncode == 0 and not self.invalid_counts.get(item.name):
            return target.ExecResult(0, no_issue_output(), "")
        return result


def brief_file(tmp_path: Path) -> Path:
    return write(tmp_path / "brief.md", brief_text())


def test_parse_review_accepts_an_empty_findings_list() -> None:
    parsed = target.parse_review(no_issue_output(), spec("codex_tech"))
    assert parsed.findings == []
    assert "指摘はありません" in parsed.body


def test_parse_review_still_rejects_broken_json_shapes() -> None:
    item = spec("codex_tech")
    for payload in ('{"findings": "なし"}', "{}", '{"findings": null}', '["x"]'):
        with pytest.raises(ValueError, match="配列ではありません"):
            target.parse_review(NO_ISSUE_BODY + f"\n```json\n{payload}\n```\n", item)
    with pytest.raises(ValueError, match="200 バイト未満"):
        target.parse_review('短い\n```json\n{"findings": []}\n```\n', item)  # 中身のない回答は空配列でも通さない


def test_empty_findings_reviewer_is_not_retried_and_the_run_succeeds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = EmptyFindingsRunner({"codex_tech"})
    integrator = FakeIntegrator()
    work = tmp_path / "runs"
    assert invoke(run_args(brief_file(tmp_path), work), runner, integrator) == 0
    assert runner.calls["codex_tech"] == 1  # 再実行しない (費用を使わない)
    _, state = load_run(work)
    assert state["status"] == "success" and "partial" not in state
    assert state["no_findings"] == ["codex_tech"]
    finding_lines = [line for line in integrator.prompts[0].splitlines() if line.startswith("R") and ": [" in line]
    assert len(finding_lines) == 3  # 4 者のうち 1 者は指摘なし
    report = capsys.readouterr().out
    assert "指摘なし（JSON 0 件）: codex_tech（本文は生ログで確認できる）" in report


def test_the_field_scenario_partial_when_deepseek_is_missing_and_codex_tech_has_no_findings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """検証 V3a の再現: DeepSeek 2 者は実行失敗、Codex 技術は指摘なし。以前は使える者が 1 者で exit 10/12 だった。"""
    runner = EmptyFindingsRunner({"codex_tech"}, exit_codes={"ds_tech": 10, "ds_crit": 10})
    work = tmp_path / "runs"
    assert invoke(run_args(brief_file(tmp_path), work), runner) == 20
    assert runner.calls["codex_tech"] == 1
    _, state = load_run(work)
    assert state["status"] == "partial" and state["no_findings"] == ["codex_tech"]
    assert [item["name"] for item in state["partial"]["missing"]] == ["ds_tech", "ds_crit"]
    report = capsys.readouterr().out
    assert "暫定（欠落: ds_tech=実行失敗, ds_crit=実行失敗）" in report
    assert "指摘なし（JSON 0 件）: codex_tech" in report


def test_all_reviewers_without_findings_still_produces_a_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    every = {item.name for item in target.REVIEWERS}
    integrator = FakeIntegrator()
    work = tmp_path / "runs"
    assert invoke(run_args(brief_file(tmp_path), work), EmptyFindingsRunner(every), integrator) == 0
    _, state = load_run(work)
    assert state["clusters"] == [] and sorted(state["no_findings"]) == sorted(every)
    assert not [line for line in integrator.prompts[0].splitlines() if line.startswith("R") and ": [" in line]
    report = capsys.readouterr().out
    assert "指摘なし（JSON 0 件）:" in report and len(report.splitlines()) <= 90


def test_failure_message_shows_the_json_problem_next_to_the_execution_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """DeepSeek 2 者が実行失敗・Codex 技術が JSON 不正 (再実行も) で使える者が 1 者のとき、真因 (JSON 不正) も出す。"""
    runner = EmptyFindingsRunner(set(), invalid_counts={"codex_tech": 2}, exit_codes={"ds_tech": 10, "ds_crit": 10})
    work = tmp_path / "runs"
    assert invoke(run_args(brief_file(tmp_path), work), runner) == 10  # 実行失敗を優先する従来の終了コード
    assert runner.calls["codex_tech"] == 2  # 救済の見込みがあるので再実行はした
    err = capsys.readouterr().err
    assert "レビュアー実行失敗: ds_tech" in err and "codex_tech: " in err
    _, state = load_run(work)
    assert state["status"] == "reviewer_execution_failed"
    assert state["gate1"]["failed"] == ["ds_tech", "ds_crit"] and "codex_tech" in state["gate1"]["errors"]


def test_failure_without_json_problem_keeps_the_old_gate1_shape(tmp_path: Path) -> None:
    runner = FakeReviewRunner(exit_codes={"codex_crit": 1, "ds_tech": 1, "ds_crit": 124})
    work = tmp_path / "runs"
    assert invoke(run_args(brief_file(tmp_path), work), runner) == 10
    _, state = load_run(work)
    assert state["gate1"] == {"ok": False, "failed": ["codex_crit", "ds_tech", "ds_crit"]}


def test_prompts_tell_reviewers_how_to_report_no_findings_and_the_integrator_not_to_ask_decided_matters() -> None:
    assert "空配列" in target.JSON_INSTRUCTION
    assert "既に決まっていることは質問にしない" in target.INTEGRATOR_RULES
