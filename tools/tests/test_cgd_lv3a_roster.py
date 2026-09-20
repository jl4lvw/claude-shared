"""Lv3A の roster (レビュアーの組: lv3 / lv7 / lv8) のテスト。Codex・DeepSeek・Qwen の実物は呼ばない。

lv3 の出力が変わっていないことは test_cgd_lv3a_golden.py (拡張前のゴールデン) が守る。ここは新しい挙動:
組の内訳・強度・Qwen の実行器・--no-qwen・暫定版・費用・レポートの列・重点観点・plan の表示。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Sequence

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import cgd_lv3a as target  # noqa: E402
import lv3a_roster_harness as harness  # noqa: E402

LV7_NAMES = ["codex_tech", "codex_tech_high", "ds_tech", "qwen_tech"]
LV8_NAMES = [*LV7_NAMES, "codex_crit", "ds_crit"]


class Sandbox:
    """1 回分の作業場: brief・対象・偽の実行器を用意して main を呼ぶ。相対パスで渡す (レビュー入力を場所に依存させない)。"""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        self.tmp = tmp_path
        self.capsys = capsys
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(target, "create_run_dir", harness.fixed_run_dir)
        Path("brief.md").write_text(harness.BRIEF, encoding="utf-8", newline="")
        Path("sample.py").write_text(harness.SOURCE, encoding="utf-8", newline="")
        self.work = tmp_path / "runs"
        self.run_dir = self.work / "t_20260920_000000_abcdef"
        self.runner = harness.DeterministicRunner(target)
        self.integrator = harness.DeterministicIntegrator(target)

    def argv(self, command: str, *extra: str) -> list[str]:
        base = [command, "--brief", "brief.md", "--files", "sample.py"]
        return base + (["--work-root", str(self.work), "--label", "t"] if command == "run" else []) + list(extra)

    def run(self, *extra: str, **runner_kwargs: Any) -> int:
        self.runner = harness.DeterministicRunner(target, **runner_kwargs)
        return target.main(
            self.argv("run", *extra), reviewer_runner=self.runner, integrator_runner=self.integrator,
            usage_logger=lambda: None, resolver=lambda: "codex", weekly=lambda: 0.0,
        )

    def plan(self, *extra: str, weekly: Any = lambda: 0.0) -> tuple[int, str, str]:
        code = target.main(self.argv("plan", *extra), resolver=lambda: "codex", weekly=weekly)
        captured = self.capsys.readouterr()
        return code, captured.out, captured.err

    def state(self) -> dict[str, Any]:
        return json.loads((self.run_dir / "run.json").read_text(encoding="utf-8"))

    def report(self) -> str:
        return (self.run_dir / "report.md").read_text(encoding="utf-8")


@pytest.fixture()
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Sandbox:
    return Sandbox(tmp_path, monkeypatch, capsys)


# ------------------------------------------------------------------ 組の内訳と prefix


def summary(specs: Sequence[target.ReviewerSpec]) -> list[tuple[Any, ...]]:
    return [(s.name, s.vendor, s.kind, s.prefix, s.role, s.effort) for s in specs]


def test_lv3_is_the_existing_reviewers_and_lv7_lv8_match_the_spec() -> None:
    assert target.ROSTERS["lv3"] is target.REVIEWERS
    assert summary(target.ROSTERS["lv7"]) == [
        ("codex_tech", "Codex", "technical", "CT", "reviewer", "medium"),
        ("codex_tech_high", "Codex", "technical", "CH", "reviewer", "high"),
        ("ds_tech", "DeepSeek", "technical", "DT", "reviewer", None),
        ("qwen_tech", "Qwen", "technical", "QT", "reviewer", None),
    ]
    assert summary(target.ROSTERS["lv8"]) == [
        *summary(target.ROSTERS["lv7"]),
        ("codex_crit", "Codex", "critic", "CC", "critic", "high"),
        ("ds_crit", "DeepSeek", "critic", "DC", "critic", None),
    ]
    assert list(target.ROSTERS) == ["lv3", "lv7", "lv8"]


@pytest.mark.parametrize("name", ["lv3", "lv7", "lv8"])
def test_every_roster_has_unique_names_and_prefixes(name: str) -> None:
    specs = target.roster_specs(name)
    assert len({s.prefix for s in specs}) == len(specs)
    assert len({s.name for s in specs}) == len(specs)
    assert target.check_roster(specs) == []
    # 技術の severity は 🔴🟠🟡、批評は 高中低 (JSON ゲートが見る)
    assert all(s.severities == (("🔴", "🟠", "🟡") if s.kind == "technical" else ("高", "中", "低")) for s in specs)


def test_check_roster_reports_duplicates_and_unknown_roster_stops() -> None:
    twin = [
        target.ReviewerSpec("a", "Codex", "technical", "CT", ("🔴",), "reviewer"),
        target.ReviewerSpec("b", "Qwen", "technical", "CT", ("🔴",), "reviewer"),
    ]
    assert "prefix が重複: CT" in target.check_roster(twin)[0]
    assert target.check_roster([twin[0], twin[0]])  # name も重複
    with pytest.raises(target.FrontError, match="未知の組"):
        target.roster_specs("lv9")


def test_finding_ids_are_checked_against_each_reviewers_own_prefix() -> None:
    """ID の検査は spec.prefix から導出される (CT/DT の決め打ちが無い)。"""
    high, qwen = target.ROSTERS["lv7"][1], target.ROSTERS["lv7"][3]
    body = "詳しい指摘です。" * 30

    def block(finding_id: str) -> str:
        finding = {"id": finding_id, "severity": "🔴", "headline": "指摘"}
        return body + "\n```json\n" + json.dumps({"findings": [finding]}, ensure_ascii=False) + "\n```\n"

    assert target.parse_review(block("CH1"), high).findings[0]["id"] == "CH1"
    assert target.parse_review(block("QT1"), qwen).findings[0]["id"] == "QT1"
    for wrong, spec in (("CT1", high), ("CH1", qwen)):
        with pytest.raises(ValueError, match="連番でない"):
            target.parse_review(block(wrong), spec)


def test_roster_vendors_are_the_report_columns_in_first_appearance_order() -> None:
    assert target.roster_vendors(target.ROSTERS["lv3"]) == ["Codex", "DeepSeek"]
    assert target.roster_vendors(target.ROSTERS["lv7"]) == ["Codex", "DeepSeek", "Qwen"]
    assert target.roster_vendors(target.ROSTERS["lv8"]) == ["Codex", "DeepSeek", "Qwen"]


# ------------------------------------------------------------------ 強度


@pytest.fixture()
def commands(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    def fake(command: list[str], *, stdin: str | None, cwd: Path, timeout: int, env: dict[str, str]) -> Any:
        seen.append({"command": command, "stdin": stdin, "cwd": cwd, "timeout": timeout, "env": env})
        return target.ExecResult(0, "", "")

    monkeypatch.setattr(target, "_subprocess", fake)
    return seen


def effort_of(command: list[str]) -> str:
    return re.search(r'model_reasoning_effort="(\w+)"', command[command.index("-c") + 1]).group(1)  # type: ignore[union-attr]


def test_spec_effort_reaches_the_codex_command_and_overrides_the_argument(commands: list[dict[str, Any]], tmp_path: Path) -> None:
    lv7 = {s.name: s for s in target.ROSTERS["lv7"]}
    target.default_reviewer_runner(lv7["codex_tech"], "p", tmp_path, 5, "codex", TOOLS, "high")  # 引数は high でも組が medium
    target.default_reviewer_runner(lv7["codex_tech_high"], "p", tmp_path, 5, "codex", TOOLS, "medium")
    assert [effort_of(c["command"]) for c in commands] == ["medium", "high"]
    # lv3 の spec (effort=None) は従来どおり引数に従う
    target.default_reviewer_runner(target.REVIEWERS[0], "p", tmp_path, 5, "codex", TOOLS, "high")
    assert effort_of(commands[-1]["command"]) == "high"


@pytest.mark.parametrize(
    ("roster", "expected"),
    [
        ("lv7", {"codex_tech": "medium", "codex_tech_high": "high"}),
        ("lv8", {"codex_tech": "medium", "codex_tech_high": "high", "codex_crit": "high"}),
    ],
)
def test_the_runner_receives_the_fixed_effort_per_reviewer(box: Sandbox, roster: str, expected: dict[str, str]) -> None:
    assert box.run("--roster", roster) == 0
    for name, effort in expected.items():
        assert {c["effort"] for c in box.runner.calls_of(name)} == {effort}
    state = box.state()
    assert {n: state["reviewers"][n]["effort"] for n in expected} == expected
    assert all(state["reviewers"][n]["effort"] is None for n in state["reviewers"] if n not in expected)


def test_retry_uses_the_same_fixed_effort(box: Sandbox) -> None:
    assert box.run("--roster", "lv7", invalid_first={"codex_tech_high"}) == 0
    assert [(c["attempt"], c["effort"]) for c in box.runner.calls_of("codex_tech_high")] == [(1, "high"), (2, "high")]


@pytest.mark.parametrize("roster", ["lv7", "lv8"])
@pytest.mark.parametrize("effort", ["medium", "high"])
def test_roster_lv7_lv8_with_effort_is_rejected_with_exit_1(
    box: Sandbox, roster: str, effort: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert box.run("--roster", roster, "--effort", effort) == 1
    err = capsys.readouterr().err
    assert "--effort" in err and "併用できません" in err
    assert not box.work.exists() and not box.runner.log  # 何も作らず、何も呼ばない


def test_lv3_still_accepts_effort_and_defaults_to_medium(box: Sandbox) -> None:
    assert box.run("--effort", "high") == 0
    assert {c["effort"] for c in box.runner.log} == {"high"}
    assert box.state()["reviewers"]["codex_tech"]["effort"] == "high"


# ------------------------------------------------------------------ Qwen の実行器・環境変数


def test_qwen_runner_command_input_file_and_env(
    commands: list[dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("DASHSCOPE_API_KEY", "QWEN_BASE_URL", "QWEN_USD_TO_JPY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY",
                "GEMINI_API_KEY", "CODEX_HOME", "ANTHROPIC_API_KEY", "GITHUB_TOKEN", "PATH", "USERPROFILE"):
        monkeypatch.setenv(key, "v")
    qwen = next(s for s in target.ROSTERS["lv7"] if s.vendor == "Qwen")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    target.default_reviewer_runner(qwen, "レビュー入力", run_dir, 77, "codex", TOOLS, "medium")
    call = commands[0]
    assert call["command"] == [sys.executable, str(TOOLS / "qwen_advisor.py"), "--role", "reviewer", str(run_dir / "qwen_tech_input.txt")]
    assert (run_dir / "qwen_tech_input.txt").read_text(encoding="utf-8") == "レビュー入力"
    assert call["stdin"] is None and call["timeout"] == 77 and call["cwd"] == run_dir.parent
    names = {key.upper() for key in call["env"]}
    assert {"DASHSCOPE_API_KEY", "QWEN_BASE_URL", "QWEN_USD_TO_JPY", "PATH", "USERPROFILE"} <= names
    assert not names & {"DEEPSEEK_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "CODEX_HOME", "ANTHROPIC_API_KEY", "GITHUB_TOKEN"}


def test_other_vendors_do_not_receive_the_qwen_key(
    commands: list[dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("DASHSCOPE_API_KEY", "QWEN_BASE_URL", "DEEPSEEK_API_KEY", "CODEX_HOME"):
        monkeypatch.setenv(key, "v")
    lv7 = {s.name: s for s in target.ROSTERS["lv7"]}
    for name in ("codex_tech", "ds_tech"):
        target.default_reviewer_runner(lv7[name], "p", tmp_path, 5, "codex", TOOLS, "medium")
    target.default_integrator_runner("p", tmp_path, 5, "codex")
    codex_env, ds_env, integrator_env = (call["env"] for call in commands)
    for env in (codex_env, integrator_env):
        assert not {k.upper() for k in env} & {"DASHSCOPE_API_KEY", "QWEN_BASE_URL", "DEEPSEEK_API_KEY"}
    assert "DEEPSEEK_API_KEY" in ds_env and not {k.upper() for k in ds_env} & {"DASHSCOPE_API_KEY", "QWEN_BASE_URL"}
    assert commands[1]["command"][1] == str(TOOLS / "deepseek_coder.py")


def test_child_env_accepts_one_or_many_prefixes_and_ignores_empty_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("AAA_KEY", "BBB_KEY", "CCC_KEY", "PATH"):
        monkeypatch.setenv(key, "v")
    assert "AAA_KEY" in target.child_env("AAA_") and "BBB_KEY" not in target.child_env("AAA_")  # 従来の 1 個
    both = target.child_env(("AAA_", "BBB_"))
    assert {"AAA_KEY", "BBB_KEY", "PATH"} <= set(both) and "CCC_KEY" not in both
    assert "CCC_KEY" not in target.child_env(("AAA_", ""))  # 空の接頭辞で全変数が通らない
    assert "CCC_KEY" not in target.child_env("") and "PATH" in target.child_env("")


def test_unknown_vendor_fails_instead_of_falling_back_to_deepseek(commands: list[dict[str, Any]], tmp_path: Path) -> None:
    alien = target.ReviewerSpec("x_tech", "Alien", "technical", "XT", ("🔴",), "reviewer")
    result = target.default_reviewer_runner(alien, "p", tmp_path, 5, "codex", TOOLS, "medium")
    assert result.returncode == 127 and "Alien" in result.stderr and not commands


def test_qwen_timeout_option_reaches_the_runner_and_must_be_positive(box: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    assert box.run("--roster", "lv7") == 0
    assert {c["timeout"] for c in box.runner.calls_of("qwen_tech")} == {300}  # 既定は DeepSeek と同じ
    assert {c["timeout"] for c in box.runner.calls_of("ds_tech")} == {300}
    assert {c["timeout"] for c in box.runner.calls_of("codex_tech")} == {600}
    box.work.rename(box.tmp / "runs_first")
    assert box.run("--roster", "lv7", "--qwen-timeout", "45", "--ds-timeout", "50") == 0
    assert {c["timeout"] for c in box.runner.calls_of("qwen_tech")} == {45}
    assert {c["timeout"] for c in box.runner.calls_of("ds_tech")} == {50}
    capsys.readouterr()
    assert box.run("--roster", "lv7", "--qwen-timeout", "0") == 1
    assert "timeout" in capsys.readouterr().err


# ------------------------------------------------------------------ 実行全体 (組ごと)


@pytest.mark.parametrize(("roster", "names"), [("lv7", LV7_NAMES), ("lv8", LV8_NAMES)])
def test_run_json_records_roster_vendor_and_effort_and_keeps_existing_keys(box: Sandbox, roster: str, names: list[str]) -> None:
    assert box.run("--roster", roster) == 0
    state = box.state()
    assert state["roster"] == roster and state["status"] == "success" and state["no_qwen"] is False
    assert sorted(state["reviewers"]) == sorted(names)
    assert sorted(state["mapping"].values()) == sorted(names)
    assert set(state["mapping"]) == {f"R{n}" for n in range(1, len(names) + 1)}
    for name, item in state["reviewers"].items():
        spec = next(s for s in target.ROSTERS[roster] if s.name == name)
        assert item["vendor"] == spec.vendor
        assert {"returncode", "stdout", "stderr", "elapsed", "attempts"} <= set(item)  # 既存のキー
    for key in ("run_name", "weekly_percent", "no_ds", "no_partial", "redactions", "gate1", "gate2", "clusters", "costs"):
        assert key in state
    assert state["clusters"][0].keys() >= {"vendors", "single_source", "severity"}


def test_lv8_keeps_technical_and_critic_apart_and_uses_six_reviewers(box: Sandbox) -> None:
    assert box.run("--roster", "lv8") == 0
    assert len(box.runner.log) == 6
    prompt = box.integrator.prompts[0]
    assert prompt.count("## R") == 6 and prompt.count("(critic)") == 2 and prompt.count("(technical)") == 4
    assert {c["kind"] for c in box.state()["clusters"]} == {"technical", "critic"}
    report = box.report()
    assert "## 技術レビュー" in report and "## 批評レビュー" in report
    assert len(report.splitlines()) <= 90


def test_default_run_records_roster_lv3_and_no_qwen_key(box: Sandbox) -> None:
    assert box.run() == 0
    state = box.state()
    assert state["roster"] == "lv3" and "no_qwen" not in state
    assert not any(key.startswith("qwen") for key in state["costs"])
    assert all(item["vendor"] in ("Codex", "DeepSeek") for item in state["reviewers"].values())


# ------------------------------------------------------------------ --no-qwen / 暫定版


def test_no_qwen_success_is_not_partial_and_the_qwen_reviewer_is_never_called(box: Sandbox) -> None:
    assert box.run("--roster", "lv7", "--no-qwen") == 0
    state = box.state()
    assert state["status"] == "success" and "partial" not in state and state["no_qwen"] is True
    assert "qwen_tech" not in state["reviewers"] and not box.runner.calls_of("qwen_tech")
    assert sorted(state["mapping"].values()) == ["codex_tech", "codex_tech_high", "ds_tech"]
    assert "状態: 成功 / Qwen なし" in box.report()
    assert "欠け" not in box.report() and "暫定" not in box.report()
    assert state["costs"]["qwen_calls"] == 0 and state["costs"]["qwen_cost_known"] is True


def test_no_qwen_and_no_ds_together_leave_the_codex_pair_and_stay_successful(box: Sandbox) -> None:
    assert box.run("--roster", "lv7", "--no-qwen", "--no-ds") == 0
    assert sorted(box.state()["reviewers"]) == ["codex_tech", "codex_tech_high"]
    assert "状態: 成功 / DS なし / Qwen なし" in box.report()


def test_no_qwen_on_lv3_changes_nothing(box: Sandbox) -> None:
    assert box.run("--no-qwen") == 0
    assert "Qwen" not in box.report() and "no_qwen" not in box.state()


@pytest.mark.parametrize("failing", [["qwen_tech"], ["qwen_tech", "ds_tech"]])
def test_qwen_failure_still_gives_partial_exit_20_while_two_or_more_are_usable(box: Sandbox, failing: list[str]) -> None:
    assert box.run("--roster", "lv7", exit_codes={name: 1 for name in failing}) == 20
    state = box.state()
    assert state["status"] == "partial"
    assert [m["name"] for m in state["partial"]["missing"]] == [n for n in LV7_NAMES if n in failing]
    assert all(m["reason"] == "実行失敗(終了コード1)" for m in state["partial"]["missing"])
    assert "暫定（欠落: " in box.report()
    assert f"⚠ 暫定: {'、'.join(n for n in LV7_NAMES if n in failing)} が欠けています。" in box.report()


def test_partial_warning_names_the_missing_view_with_effort_when_twins_exist(box: Sandbox) -> None:
    assert box.run("--roster", "lv7", exit_codes={"codex_tech_high": 1, "qwen_tech": 1}) == 20
    assert "Codex(技術・high)、Qwen(技術) の指摘が出ていません" in box.report()
    # 組にいないもの・lv3 の表示は従来どおり
    assert target._missing_view("ds_crit") == "DeepSeek(批評)"
    assert target._missing_view("unknown") == "unknown"


def test_three_failures_of_four_keep_exit_10(box: Sandbox) -> None:
    assert box.run("--roster", "lv7", exit_codes={"qwen_tech": 1, "ds_tech": 1, "codex_tech": 1}) == 10
    assert box.state()["status"] == "reviewer_execution_failed"


def test_qwen_json_invalid_is_retried_once_and_the_retry_is_the_adopted_log(box: Sandbox) -> None:
    assert box.run("--roster", "lv7", invalid_first={"qwen_tech"}) == 0
    assert [c["attempt"] for c in box.runner.calls_of("qwen_tech")] == [1, 2]
    retry_prompt = box.runner.calls_of("qwen_tech")[1]["prompt"]
    assert target.RETRY_NOTE in retry_prompt and target.INTEGRATION_FOCUS in retry_prompt
    assert (box.run_dir / "qwen_tech.retry1.md").exists()
    assert len(box.state()["reviewers"]["qwen_tech"]["attempts"]) == 2
    assert box.state()["costs"]["qwen_calls"] == 2  # 再実行の分も費用に入る


# ------------------------------------------------------------------ 費用


def test_qwen_usage_is_parsed_from_the_real_stderr_format() -> None:
    assert target.qwen_usage(harness.STDERR["Qwen"]) == (1500, 1.25)  # 入力 800 (miss) + 200 (hit) + 出力 500
    line = "warn\n[Qwen Usage] 今回: 入力 12,345 (miss) + 1,000 (hit) / 出力 2,000 tok (¥12.50 / $0.0833) [model=x]\n"
    assert target.qwen_usage(line) == (15345, 12.5)
    for broken in ("", "[Qwen Usage] WARN: usage 情報がレスポンスに含まれていません", "[DS Usage] 今回: ¥1.0"):
        assert target.qwen_usage(broken) is None


def test_costs_include_qwen_only_when_asked_and_mark_unknown_usage() -> None:
    qwen = target.ROSTERS["lv7"][3]
    ok = target.ExecResult(0, "", harness.STDERR["Qwen"])
    lost = target.ExecResult(0, "", "")
    codex = target.ExecResult(0, "", "tokens used\n1,000\n")
    plain = target.calculate_costs([(target.REVIEWERS[0], codex)], [])
    assert set(plain) == {"codex_calls", "codex_tokens", "ds_calls", "ds_yen"}  # 従来のキーだけ
    known = target.calculate_costs([(qwen, ok), (qwen, ok)], [codex], with_qwen=True)
    assert (known["qwen_calls"], known["qwen_tokens"], known["qwen_yen"], known["qwen_cost_known"]) == (2, 3000, 2.5, True)
    assert known["codex_calls"] == 1 and known["codex_tokens"] == 1000
    unknown = target.calculate_costs([(qwen, ok), (qwen, lost)], [], with_qwen=True)
    assert (unknown["qwen_calls"], unknown["qwen_tokens"], unknown["qwen_cost_known"]) == (2, 1500, False)
    none_called = target.calculate_costs([], [], with_qwen=True)
    assert none_called["qwen_calls"] == 0 and none_called["qwen_cost_known"] is True


def test_attempt_record_marks_cost_known_only_for_qwen() -> None:
    qwen = target.ROSTERS["lv7"][3]
    known = target.attempt_record(qwen, target.ExecResult(0, "", harness.STDERR["Qwen"]), None)
    lost = target.attempt_record(qwen, target.ExecResult(0, "", ""), None)
    assert (known["tokens"], known["yen"], known["cost_known"]) == (1500, 1.25, True)
    assert (lost["tokens"], lost["yen"], lost["cost_known"]) == (0, 0.0, False)
    assert "cost_known" not in target.attempt_record(target.REVIEWERS[0], target.ExecResult(0, "", ""), None)


def test_run_reports_qwen_tokens_and_yen_in_run_json_and_report(box: Sandbox) -> None:
    assert box.run("--roster", "lv7") == 0
    costs = box.state()["costs"]
    assert (costs["qwen_calls"], costs["qwen_tokens"], costs["qwen_yen"], costs["qwen_cost_known"]) == (1, 1500, 1.25, True)
    assert costs["codex_calls"] == 3 and costs["ds_calls"] == 1  # Codex 2 レビュー + 統合 1
    assert "Codex: 3 回 / 26,690 tokens、DeepSeek: 1 回 / ¥0.500、Qwen: 1 回 / 1,500 tokens / ¥1.25" in box.report()
    assert "費用は不明" not in box.report()


def test_report_says_when_qwen_usage_could_not_be_read(box: Sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(harness.STDERR, "Qwen", "")
    assert box.run("--roster", "lv7") == 0
    assert box.state()["costs"]["qwen_cost_known"] is False
    assert box.state()["reviewers"]["qwen_tech"]["attempts"][0]["cost_known"] is False
    assert "Qwen: 1 回 / 0 tokens / ¥0.00（usage を取得できない呼出があり、費用は不明を 0 として数えた）" in box.report()


# ------------------------------------------------------------------ レポートの列


def table_cells(line: str) -> int:
    return len(line.strip().strip("|").split("|"))


def test_report_columns_are_the_roster_vendors_and_every_row_has_the_same_width(box: Sandbox) -> None:
    assert box.run("--roster", "lv8") == 0
    report = box.report()
    assert "| 指摘 | 重大度 | Codex | DeepSeek | Qwen | 採否案 | 対応案 |" in report
    assert "| 観点 | 困り度 | Codex | DeepSeek | Qwen | 採否案 | 改善の方向 |" in report
    assert "|---|---|---|---|---|---|---|" in report
    widths = {table_cells(line) for line in report.splitlines() if line.startswith("|")}
    assert widths == {7}
    row = next(line for line in report.splitlines() if line.startswith("| 境界値で例外が出る"))
    assert row.split("|")[3:6] == [" ✅ ", " ✅ ", "  "]  # Codex ✅ / DeepSeek ✅ / Qwen なし
    qwen_row = next(line for line in report.splitlines() if line.startswith("| 型ヒントが無い"))
    assert qwen_row.split("|")[3:6] == ["  ", "  ", " ✅ "]


def test_lv7_report_has_no_empty_critic_table_and_names_the_roster(box: Sandbox) -> None:
    """実走 (lv7): 批評のレビュアーがいないのに、行が 0 の「批評レビュー」の表 (見出しだけ) が出ていた。"""
    assert box.run("--roster", "lv7") == 0
    report = box.report()
    assert "## 技術レビュー" in report and "批評" not in report.split("## 🔴 の詳細")[0]
    assert "組: lv7（codex_tech / codex_tech_high / ds_tech / qwen_tech）" in report
    assert "\n\n## 🔴 の詳細\n" in report  # 表の後ろの節の区切りは崩れない


def test_lv8_report_keeps_the_critic_table_and_lv3_has_no_roster_line(box: Sandbox) -> None:
    assert box.run("--roster", "lv8") == 0
    lv8 = box.report()
    assert "## 批評レビュー" in lv8 and f"組: lv8（{' / '.join(LV8_NAMES)}）" in lv8
    box.work.rename(box.tmp / "runs_lv8")
    assert box.run() == 0
    assert "組:" not in box.report() and "## 批評レビュー" in box.report()  # lv3 の見出し・節は従来どおり


def test_omitted_rows_have_the_same_width_as_the_table_for_every_roster(tmp_path: Path) -> None:
    metadata, clusters, payload = harness_inputs(90)
    for name, columns in (("lv3", 6), ("lv7", 7), ("lv8", 7)):
        report = target.build_report(
            "run", 1, clusters, metadata, payload, [], {"codex_calls": 1, "codex_tokens": 1, "ds_calls": 0, "ds_yen": 0},
            tmp_path, False, roster=target.ROSTERS[name],
        )
        rows = [line for line in report.splitlines() if line.startswith("|")]
        assert any(line.startswith("| 他 ") for line in rows)
        assert {table_cells(line) for line in rows} == {columns}
        assert len(report.splitlines()) <= 90


def harness_inputs(count: int) -> tuple[dict[str, dict[str, str]], list[dict[str, Any]], dict[str, Any]]:
    metadata: dict[str, dict[str, str]] = {}
    clusters: list[dict[str, Any]] = []
    for number in range(1, count + 1):
        member = f"R1#{number}"
        metadata[member] = {"kind": "technical", "vendor": "Qwen", "reviewer": "R1", "reviewer_name": "qwen_tech",
                            "log_file": "qwen_tech", "severity": "🟡", "headline": f"見出し{number:02d}"}
        clusters.append({"id": f"C{number}", "kind": "technical", "title": f"題名{number:02d}", "members": [member],
                         "proposal": "案", "adopt": "採用", "adopt_reason": "理由", "severity": "🟡",
                         "vendors": ["Qwen"], "single_source": False})
    return metadata, clusters, {"summary": "総評", "next_actions": ["次へ"], "questions_for_user": []}


# ------------------------------------------------------------------ 重点観点ブロック


@pytest.mark.parametrize(
    ("roster", "with_focus"),
    [("lv3", []), ("lv7", LV7_NAMES), ("lv8", LV7_NAMES)],
)
def test_focus_block_goes_only_to_the_technical_reviewers_of_lv7_lv8(box: Sandbox, roster: str, with_focus: list[str]) -> None:
    assert box.run("--roster", roster) == 0
    for call in box.runner.log:
        assert (target.INTEGRATION_FOCUS in call["prompt"]) == (call["name"] in with_focus), call["name"]


def test_focus_block_placement_and_content(box: Sandbox) -> None:
    assert box.run("--roster", "lv8") == 0
    focus = target.INTEGRATION_FOCUS
    assert 4 <= len(focus.splitlines()) <= 5
    for keyword in ("integration", "暗黙の前提", "状態管理", "呼出経路", "握り潰し"):
        assert keyword in focus
    codex = box.runner.calls_of("codex_tech_high")[0]["prompt"]
    assert codex.startswith(target.TECH_PROMPT) and codex.index(focus) == len(target.TECH_PROMPT) + 2  # 役割の文の直後
    for name in ("ds_tech", "qwen_tech"):
        assert box.runner.calls_of(name)[0]["prompt"].startswith(focus)  # system prompt を持つ側は先頭
    critic = box.runner.calls_of("codex_crit")[0]["prompt"]
    assert focus not in critic and critic.startswith(target.CRIT_PROMPT)
    # 重点観点の後ろは、lv3 と同じ依頼文・対象・JSON 指示 (足しただけ)
    packed = (box.run_dir / "review_input.txt").read_text(encoding="utf-8")
    tech = next(s for s in target.ROSTERS["lv8"] if s.name == "qwen_tech")
    assert box.runner.calls_of("qwen_tech")[0]["prompt"] == (
        f"{focus}\n\n{packed}\n\n{target.json_instruction(tech)}\n{target.qwen_format_note(tech)}\n"
    )
    ds = next(s for s in target.ROSTERS["lv8"] if s.name == "ds_tech")  # DeepSeek は JSON の指示までで、形式の注意は付かない
    assert box.runner.calls_of("ds_tech")[0]["prompt"] == f"{focus}\n\n{packed}\n\n{target.json_instruction(ds)}\n"


def test_only_qwen_gets_the_json_format_note(box: Sandbox) -> None:
    """実走 (Lv5A consult): Qwen が JSON の最上位を配列だけにして 2 回続けて不合格。形の注意を Qwen にだけ足す。"""
    assert box.run("--roster", "lv8") == 0
    qwen = next(s for s in target.ROSTERS["lv8"] if s.vendor == "Qwen")
    note = target.qwen_format_note(qwen)
    assert '"findings"' in note and '{"findings":[{"id":"QT1","severity":"🔴","headline":"…"}]}' in note and "[ ] の配列だけ" in note
    for call in box.runner.log:
        assert (note in call["prompt"]) == (call["name"] == "qwen_tech"), call["name"]
        assert "【形式の注意】" not in call["prompt"] or call["name"] == "qwen_tech"
    for spec in target.REVIEWERS:  # lv3 の入力には無い
        assert "【形式の注意】" not in target.reviewer_input(spec, "対象")


def test_the_gate_still_rejects_a_bare_array_from_qwen() -> None:
    """ゲート 1 は緩めない: 指摘だけの配列は不合格のまま (プロンプトで直し、ゲートは変えない)。"""
    qwen = target.ROSTERS["lv7"][3]
    text = "詳しい指摘です。" * 30 + '\n```json\n[{"id":"QT1","severity":"🔴","headline":"見出し"}]\n```\n'
    with pytest.raises(ValueError, match="findings が配列ではありません"):
        target.parse_review(text, qwen)


def test_qwen_retry_carries_the_gate_error_but_other_retries_do_not(box: Sandbox) -> None:
    assert box.run("--roster", "lv7", invalid_first={"qwen_tech", "codex_tech"}) == 0
    qwen_retry = box.runner.calls_of("qwen_tech")[1]["prompt"]
    assert qwen_retry.endswith(f"{target.RETRY_NOTE}\n前回の不合格の理由: JSON ブロック数が 0 件\n")
    codex_retry = box.runner.calls_of("codex_tech")[1]["prompt"]
    assert codex_retry.endswith(f"{target.RETRY_NOTE}\n") and "前回の不合格の理由" not in codex_retry  # 従来どおり
    assert target.retry_input(target.REVIEWERS[0], "P", "e") == f"P\n{target.RETRY_NOTE}\n"  # lv3 の再実行の入力は同じ


def test_reviewer_input_default_has_no_focus_for_any_spec() -> None:
    for specs in target.ROSTERS.values():
        for spec in specs:
            assert target.INTEGRATION_FOCUS not in target.reviewer_input(spec, "対象")


# ------------------------------------------------------------------ plan


@pytest.mark.parametrize(
    ("roster", "names", "codex_calls"),
    [("lv3", ["codex_tech", "codex_crit", "ds_tech", "ds_crit"], 3), ("lv7", LV7_NAMES, 3), ("lv8", LV8_NAMES, 4)],
)
def test_plan_shows_roster_breakdown_destinations_and_call_counts(
    box: Sandbox, monkeypatch: pytest.MonkeyPatch, roster: str, names: list[str], codex_calls: int
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sentinel-secret-value")
    monkeypatch.setenv("QWEN_BASE_URL", "https://user:pw-secret@dashscope-us.aliyuncs.com/compatible-mode/v1?k=q-secret")
    code, out, err = box.plan("--roster", roster)
    assert code == 0 and err == ""
    assert f"組: {roster}（{len(names)} 者）" in out
    for name in names:
        assert f"- {name}: " in out
    assert f"Codex {codex_calls} 回（" in out and f"約 {2 * codex_calls} 万" in out
    assert "Codex（OpenAI）" in out and "DeepSeek（中国本土サーバ）" in out
    assert ("Qwen（Alibaba DashScope・米国（バージニア）: dashscope-us.aliyuncs.com）" in out) == (roster != "lv3")
    for secret in ("sentinel-secret-value", "pw-secret", "q-secret", "?k="):  # 鍵・資格情報・クエリは出さない
        assert secret not in out


def test_plan_lv7_lists_effort_and_the_codex_call_formula(box: Sandbox) -> None:
    _, out, _ = box.plan("--roster", "lv7")
    assert "- codex_tech: Codex / 技術 / 強度 medium" in out
    assert "- codex_tech_high: Codex / 技術 / 強度 high" in out
    assert "- ds_tech: DeepSeek / 技術" in out and "- qwen_tech: Qwen / 技術" in out
    assert "呼出予定: Codex 3 回（技術[medium]・技術[high]・統合）、DS 1 回、Qwen 1 回" in out
    _, out8, _ = box.plan("--roster", "lv8")
    assert "呼出予定: Codex 4 回（技術[medium]・技術[high]・批評[high]・統合）、DS 2 回、Qwen 1 回" in out8


def test_plan_lv3_keeps_the_existing_lines(box: Sandbox) -> None:
    _, out, _ = box.plan()
    assert "送信先: Codex（OpenAI）、DeepSeek（中国本土サーバ）\n" in out
    assert "呼出予定: Codex 3 回（技術・批評・統合）、DS 2 回 / Codex 想定 tokens: 約 6 万\n" in out
    assert "Qwen" not in out and "DASHSCOPE" not in out
    _, out_no_ds, _ = box.plan("--no-ds")
    assert "送信先: Codex（OpenAI）\n" in out_no_ds
    assert "呼出予定: Codex 3 回（技術・批評・統合） / Codex 想定 tokens: 約 6 万\n" in out_no_ds


def test_plan_marks_excluded_reviewers_and_drops_their_destinations(box: Sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "x")
    _, out, _ = box.plan("--roster", "lv7", "--no-qwen")
    assert "組: lv7（3 者）" in out and "- qwen_tech: Qwen / 技術（除外: --no-ds / --no-qwen）" in out
    assert "Alibaba" not in out and "呼出予定: Codex 3 回（技術[medium]・技術[high]・統合）、DS 1 回 /" in out


def test_plan_default_qwen_destination_and_missing_key_warning(box: Sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QWEN_BASE_URL", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    _, out, _ = box.plan("--roster", "lv7")
    assert "Qwen（Alibaba DashScope・国際（シンガポール）: dashscope-intl.aliyuncs.com）" in out
    assert "⚠ DASHSCOPE_API_KEY が未設定です" in out and "--no-qwen" in out
    _, out_no_qwen, _ = box.plan("--roster", "lv7", "--no-qwen")
    assert "⚠" not in out_no_qwen  # 外していれば警告しない
    monkeypatch.setenv("DASHSCOPE_API_KEY", "x")
    assert "⚠" not in box.plan("--roster", "lv7")[1]


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("https://dashscope.aliyuncs.com/compatible-mode/v1", "中国本土（北京）: dashscope.aliyuncs.com"),
        ("https://dashscope-intl.aliyuncs.com/x", "国際（シンガポール）: dashscope-intl.aliyuncs.com"),
        ("https://example.invalid/v1", "リージョン不明: example.invalid"),
        ("", "国際（シンガポール）: dashscope-intl.aliyuncs.com"),
    ],
)
def test_qwen_destination_shows_the_region_of_qwen_base_url(base: str, expected: str) -> None:
    assert target.qwen_destination({"QWEN_BASE_URL": base}) == f"Qwen（Alibaba DashScope・{expected}）"


def test_plan_accepts_the_same_qwen_arguments_as_run(box: Sandbox) -> None:
    """plan は何も呼ばないが、run と同じ引数列 (--roster / --no-qwen / --qwen-timeout) をそのまま受け付ける。"""
    code, out, err = box.plan("--roster", "lv7", "--no-qwen", "--qwen-timeout", "30")
    assert code == 0 and "組: lv7（3 者）" in out and err == ""


def test_plan_rejects_a_roster_that_does_not_exist(box: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    assert box.plan("--roster", "lv9")[0] == 1
    assert box.run("--roster", "lv9") == 1 and not box.work.exists()
    capsys.readouterr()


# ------------------------------------------------------------------ 使用記録・ヘルプ


def test_default_usage_logger_notes_the_roster_but_always_records_level_3(monkeypatch: pytest.MonkeyPatch, box: Sandbox) -> None:
    """lv7/lv8 として記録すると、Workflow 必須のゲートが張られてしまう。レベルは 3 のまま、メモにだけ組を残す。"""
    recorded: list[list[str]] = []
    monkeypatch.setattr(target.subprocess, "run", lambda command, **kwargs: recorded.append(command))
    for roster in ("lv7", "lv3"):
        recorded.clear()
        code = target.main(
            box.argv("run", "--roster", roster), reviewer_runner=harness.DeterministicRunner(target),
            integrator_runner=harness.DeterministicIntegrator(target), usage_logger=target.default_usage_logger,
            resolver=lambda: "codex", weekly=lambda: 0.0,
        )
        assert code == 0
        command = recorded[0]
        assert command[command.index("--level") + 1] == "3"
        assert command[command.index("--note") + 1] == ("lv3a roster=lv7" if roster == "lv7" else "lv3a")
        box.work.rename(box.tmp / f"runs_{roster}")


@pytest.mark.parametrize("command", ["plan", "run"])
def test_help_lists_the_roster_options(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert target.main([command, "--help"]) == 0
    out = capsys.readouterr().out
    assert "--roster {lv3,lv7,lv8}" in out and "--no-qwen" in out and "--qwen-timeout" in out


# ------------------------------------------------------------------ 本物の実行器を通した全体 (subprocess だけ偽物)


class FakeProcess:
    """default_reviewer_runner / default_integrator_runner が組み立てたコマンドに答える。どのツールが呼ばれたかを記録する。"""

    def __init__(self, roster: str) -> None:
        self.by_prefix = {s.prefix: s for s in target.ROSTERS[roster]}
        self.calls: list[dict[str, Any]] = []
        self.integrator = harness.DeterministicIntegrator(target)

    def __call__(self, command: list[str], *, stdin: str | None, cwd: Path, timeout: int, env: dict[str, str]) -> Any:
        del cwd, env
        if command[0] == "codex":
            prompt = stdin or ""
            tool = "codex"
        else:
            prompt = Path(command[-1]).read_text(encoding="utf-8")
            tool = Path(command[1]).name
        if "あなたは複数のレビューを統合する議長" in prompt:
            self.calls.append({"tool": "integrator", "command": command, "timeout": timeout})
            return self.integrator(prompt, Path("."), timeout, "codex")
        match = re.search(r'"id":"([A-Z]{2})1"', prompt)
        assert match, prompt[-300:]
        spec = self.by_prefix[match.group(1)]
        self.calls.append({"tool": tool, "name": spec.name, "command": command, "timeout": timeout})
        return target.ExecResult(0, harness.review_text(spec.name, spec.prefix, spec.severities), harness.STDERR[spec.vendor])


@pytest.mark.parametrize(("roster", "names"), [("lv7", LV7_NAMES), ("lv8", LV8_NAMES)])
def test_full_run_through_the_real_runners_calls_the_right_tool_with_the_right_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], roster: str, names: list[str]
) -> None:
    box = Sandbox(tmp_path, monkeypatch, capsys)
    fake = FakeProcess(roster)
    monkeypatch.setattr(target, "_subprocess", fake)
    code = target.main(box.argv("run", "--roster", roster), usage_logger=lambda: None, resolver=lambda: "codex", weekly=lambda: 0.0)
    assert code == 0
    by_name = {c["name"]: c for c in fake.calls if "name" in c}
    assert sorted(by_name) == sorted(names)
    assert by_name["qwen_tech"]["tool"] == "qwen_advisor.py" and by_name["ds_tech"]["tool"] == "deepseek_coder.py"
    assert by_name["qwen_tech"]["command"][2:4] == ["--role", "reviewer"]
    assert effort_of(by_name["codex_tech"]["command"]) == "medium"
    assert effort_of(by_name["codex_tech_high"]["command"]) == "high"
    if roster == "lv8":
        assert effort_of(by_name["codex_crit"]["command"]) == "high"
        assert by_name["ds_crit"]["command"][2:4] == ["--role", "critic"]
    integrator = next(c for c in fake.calls if c["tool"] == "integrator")
    assert effort_of(integrator["command"]) == "medium"  # 統合者はどの組でも medium
    assert box.state()["costs"]["codex_calls"] == sum(n.startswith("codex") for n in names) + 1
