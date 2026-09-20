"""cgd_lv5a のテスト。Codex・DeepSeek・Lv0/Lv3A の実物は呼ばない (偽の実行器を注入する)。

偽の実行器は本物と同じ形のファイルを作る: Lv0 = report.md / outcome.json / overall.patch (+ 作業フォルダに設計ファイル)、
Lv3A = run.json / questions.json / report.md / レビュアーの生ログ。実物を呼ぶのは契約テスト (--help) と run_cli の
プロセス起動 (一時スクリプト) だけ。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import cgd_lv5a as target  # noqa: E402
import cgd_lv5a_io as lio  # noqa: E402
import cgd_lv5a_text as ltext  # noqa: E402
from cgd_lv5a_io import CliResult, Drivers  # noqa: E402

BRIEF = "# 依頼: slugify を足す\n\n## 目的\nslugify を追加する\n\n## 確認済みの事実\n- strutil.py がある\n\n## 制約\nPython 3.12\n"
DESIGN_MARK, FIX_MARK = "【Lv5A 設計案の作成】", "自動修正（この 1 周だけ）"
MAPPING = {"R1": "codex_tech", "R2": "ds_tech", "R3": "codex_crit", "R4": "ds_crit"}


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return path


def cl(title: str, sev: str = "🔴", adopt: str = "採用", kind: str = "technical", members: tuple[str, ...] = ("R1#1",)) -> dict[str, Any]:
    return {"id": "C1", "kind": kind, "title": title, "members": list(members), "proposal": f"{title}を直す", "adopt": adopt,
            "adopt_reason": "根拠", "severity": sev, "vendors": ["Codex"], "single_source": True}


def question(qid: str = "Q1") -> dict[str, Any]:
    return {"id": qid, "question": "どちらにしますか？", "recommended": "案A", "recommended_reason": "R1#1 による",
            "options": [{"label": "案A", "description": "a"}, {"label": "案B", "description": "b"}], "reason_ok": True}


@dataclass
class Lv0Plan:
    """偽 Lv0 の `run` 1 回分の振る舞い。"""

    exit: int = 0
    status: str = "ok"
    tokens: int = 1000
    rounds: int = 1
    counts: tuple[int, int, int] = (1, 0, 0)
    plus: int = 10
    minus: int = 2
    checks: str = "lf=ok / ruff=ok / pytest=ok"
    files: list[str] = field(default_factory=lambda: ["modified:proj/strutil.py"])
    patch: str | None = "--- a/strutil.py\n+++ b/strutil.py\n@@ -1 +1 @@\n-a\n+b\n"
    record: bool = True
    make_design: bool = True
    review_line: str = ""
    frozen: str = "なし"
    questions: str = ""
    design_text: str ="\n".join(f"設計の行 {i}" for i in range(1, 40)) + "\n"


class FakeLv0:
    def __init__(self, sc: Scenario) -> None:
        self.sc, self.n = sc, 0
        self.calls: list[list[str]] = []
        self.runs: list[tuple[str, list[str], str]] = []
        self.plans: dict[str, list[Lv0Plan]] = {"design": [], "impl": [], "fix": []}
        self.plan_exit = 0

    def argvs(self, kind: str) -> list[list[str]]:
        return [a for k, a, _ in self.runs if k == kind]

    def __call__(self, argv: list[str], timeout: int) -> CliResult:
        self.calls.append(list(argv))
        if argv[0] == "plan":
            return CliResult(self.plan_exit, "作業フォルダ: x\n送信先: Codex（OpenAI）\n", "")
        spec = Path(argv[argv.index("--spec") + 1]).read_text(encoding="utf-8")
        kind = "design" if DESIGN_MARK in spec else "fix" if FIX_MARK in spec else "impl"
        self.runs.append((kind, list(argv), spec))
        plan = self.plans[kind].pop(0) if self.plans[kind] else Lv0Plan()
        self.n += 1
        autodir = self.sc.tmp / f"lv0auto_{self.n}"
        files = list(plan.files)
        if kind == "design":
            label = re.search(r"docs/lv5a_design_([A-Za-z0-9_.-]+)\.md", spec).group(1)  # type: ignore[union-attr]
            if plan.make_design:
                write(self.sc.workdir / f"docs/lv5a_design_{label}.md", plan.design_text)
            if plan.files == Lv0Plan().files:
                files = [f"added:docs/lv5a_design_{label}.md"]
        autodir.mkdir()
        rounds = [{"index": i, "run": f"20260920_00000{i}", "effort": "medium", "seconds": 60, "tokens": plan.tokens // plan.rounds,
                   "plus": plan.plus, "minus": plan.minus, "codex_exit": 0, "failed": [], "frozen_restored": [], "sandbox_warning": ""}
                  for i in range(plan.rounds)]
        write(autodir / "outcome.json", json.dumps({"status": plan.status, "note": "n" if plan.exit else "", "base_run": f"20260920_{self.n:06d}", "rounds": rounds}))
        if plan.patch is not None:
            write(autodir / "overall.patch", plan.patch)
        m, a, d = plan.counts
        lines = ["# Lv0 自動実行レポート — OK", f"作業フォルダ: {self.sc.workdir}"]
        if plan.record:
            lines.append(f"基準の RUN: 20260920_{self.n:06d}（記録: {autodir}）")
        lines += ["", f"最終の検査: {plan.checks}", "", f"変更: 更新 {m} / 新規 {a} / 削除 {d}  +{plan.plus} -{plan.minus} 行（全周の合計）",
                  "  " + ", ".join(files), "", f"凍結ファイルの違反（写しから戻した）: {plan.frozen}"]
        if plan.review_line:
            lines.append(f"DeepSeek レビュー: {plan.review_line}")
        if plan.questions:
            lines += ["", "Codex の最終報告（抜粋）:", "```", plan.questions, "```"]
        report = "\n".join(lines) + "\n"
        write(autodir / "report.md", report)
        return CliResult(plan.exit, report, "")


@dataclass
class Lv3Plan:
    exit: int = 0
    clusters: list[dict[str, Any]] = field(default_factory=list)
    questions: list[dict[str, Any]] = field(default_factory=lambda: [question()])
    costs: dict[str, Any] = field(default_factory=lambda: {"codex_calls": 3, "codex_tokens": 60000, "ds_calls": 2, "ds_yen": 1.5})
    stderr: str = ""
    retried: tuple[str, ...] = ()
    partial: list[dict[str, str]] = field(default_factory=list)  # exit 20 のとき、欠けたレビュアー (run.json の partial.missing)


class FakeLv3a:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.plans: dict[str, list[Lv3Plan]] = {"design": [], "impl": [], "impl2": []}
        self.plan_exit = 0

    def runs(self, suffix: str) -> list[list[str]]:
        return [a for a in self.calls if a[0] == "run" and a[a.index("--label") + 1].endswith("-" + suffix)]

    def __call__(self, argv: list[str], timeout: int) -> CliResult:
        self.calls.append(list(argv))
        if argv[0] == "plan":
            return CliResult(self.plan_exit, "依頼文先頭 200 字:\n...\n送信先: Codex（OpenAI）\n", "")
        label = argv[argv.index("--label") + 1]
        plan = self.plans[label.rsplit("-", 1)[1]].pop(0) if self.plans[label.rsplit("-", 1)[1]] else Lv3Plan()
        if plan.exit in (1, 2, 11, 30):
            return CliResult(plan.exit, "", plan.stderr or "前段で停止")
        rd = Path(argv[argv.index("--work-root") + 1]) / f"{label}_20260920_000000_abcdef"
        rd.mkdir(parents=True)
        reviewers = {n: {"returncode": 0, "attempts": [{}, {}] if n in plan.retried else [{}]} for n in MAPPING.values()}
        body: dict[str, Any] = {"status": "success" if plan.exit == 0 else "partial" if plan.exit == 20 else "failed", "exit_code": plan.exit,
                                "mapping": MAPPING, "reviewers": reviewers, "clusters": plan.clusters, "costs": plan.costs}
        if plan.partial:
            body["partial"] = {"missing": plan.partial}
        write(rd / "run.json", json.dumps(body, ensure_ascii=False))
        for name in MAPPING.values():
            log = f"{name}.retry1.md" if name in plan.retried else f"{name}.md"
            heads = [{"id": f"X{i}", "severity": "🔴", "headline": f"{name}の指摘{i}"} for i in (1, 2, 3)]
            write(rd / log, "本文" * 100 + "\n```json\n" + json.dumps({"findings": heads}, ensure_ascii=False) + "\n```\n")
        if plan.exit in (0, 20):
            write(rd / "questions.json", json.dumps(plan.questions, ensure_ascii=False))
            write(rd / "report.md", "# report\n\n## 総評\n\n総評です。\n")
        return CliResult(plan.exit, "レポート\n", plan.stderr)


class Scenario:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.workdir = tmp / "repo" / "proj"
        self.workdir.mkdir(parents=True)
        write(self.workdir / "strutil.py", "def a():\n    return 1\n")
        self.checks = write(tmp / "checks.json", json.dumps({"checks": [{"name": "lf", "type": "lf"}], "frozen": ["tests/**"]}))
        self.brief = write(tmp / "brief.md", BRIEF)
        self.ref = write(tmp / "ref.md", "参考ファイル\n")
        self.work_root = tmp / "runs"
        self.lv0, self.lv3a = FakeLv0(self), FakeLv3a()
        self.drivers = Drivers(self.lv0, self.lv3a)

    def main(self, argv: list[str]) -> int:
        return target.main(argv, drivers=self.drivers)

    def consult(self, *extra: str, label: str = "x") -> int:
        return self.main(["consult", "--brief", str(self.brief), "--workdir", str(self.workdir), "--checks", str(self.checks),
                          "--files", str(self.ref), "--label", label, "--work-root", str(self.work_root), *extra])

    @property
    def run_dir(self) -> Path:
        return sorted(self.work_root.iterdir())[-1]

    def state(self) -> dict[str, Any]:
        return json.loads((self.run_dir / "state.json").read_text(encoding="utf-8"))

    def write_answers(self, g1: str = ltext.G1_YES, extra: dict[str, str] | None = None, notes: str = "") -> Path:
        data: dict[str, Any] = {"answers": {"G1": g1, **(extra or {})}}
        if notes:
            data["notes"] = notes
        return write(self.tmp / "answers.json", json.dumps(data, ensure_ascii=False))

    def implement(self, answers: Path | None = None, *extra: str) -> int:
        return self.main(["implement", "--run", str(self.run_dir), "--answers", str(answers or self.write_answers()), *extra])


@pytest.fixture()
def sc(tmp_path: Path) -> Scenario:
    return Scenario(tmp_path)


@pytest.fixture()
def consulted(sc: Scenario) -> Scenario:
    sc.lv3a.plans["design"] = [Lv3Plan(clusters=[
        cl("採用の重大", members=("R1#1", "R2#2")), cl("採用の重要", "🟠", "部分採用", members=("R2#1",)),
        cl("見送りの重大", "🔴", "見送り"), cl("軽微", "🟡", "採用"), cl("見送りの重要", "🟠", "見送り"),
        cl("批評の高", "高", "採用", "critic")])]
    assert sc.consult() == 20
    return sc


# ---------------------------------------------------------------- plan


def test_plan_prints_both_and_returns_first_nonzero(sc: Scenario, capsys) -> None:
    args = ["plan", "--brief", str(sc.brief), "--workdir", str(sc.workdir), "--checks", str(sc.checks), "--files", str(sc.ref)]
    sc.lv0.plan_exit, sc.lv3a.plan_exit = 1, 11
    assert sc.main(args) == 1
    out = capsys.readouterr().out
    assert "cgd_lv0_auto.py plan" in out and "cgd_lv3a.py plan" in out and "送信先" in out
    sc.lv0.plan_exit = 0
    assert sc.main(args) == 11
    assert sc.lv0.calls[-1] == ["plan", "--workdir", str(sc.workdir), "--checks", str(sc.checks), "--review", "deepseek"]
    assert sc.lv3a.calls[-1] == ["plan", "--brief", str(sc.brief), "--files", str(sc.ref)]
    assert not sc.work_root.exists()  # 何も書かない


def test_plan_no_ds_drops_review_flag(sc: Scenario) -> None:
    sc.main(["plan", "--brief", str(sc.brief), "--workdir", str(sc.workdir), "--checks", str(sc.checks), "--no-ds"])
    assert "--review" not in sc.lv0.calls[-1] and "--no-ds" in sc.lv3a.calls[-1]


def test_plan_rejects_inputs_that_consult_would_reject(sc: Scenario) -> None:
    """承認の前に、consult が最初に止まる不備を見せる (検査定義が作業フォルダの中・欄なし)。下請けの点検は呼ばない。"""
    inside = write(sc.workdir / "checks_inside.json", "{}")
    assert sc.main(["plan", "--brief", str(sc.brief), "--workdir", str(sc.workdir), "--checks", str(inside)]) == 1
    write(sc.tmp / "nofacts.md", "# 依頼\n")
    assert sc.main(["plan", "--brief", str(sc.tmp / "nofacts.md"), "--workdir", str(sc.workdir), "--checks", str(sc.checks)]) == 1
    assert sc.lv0.calls == [] and sc.lv3a.calls == []


# ---------------------------------------------------------------- consult


def test_consult_happy_path(consulted: Scenario) -> None:
    sc, st = consulted, consulted.state()
    assert st["phase"] == "awaiting_direction" and st["exit_code"] == 20
    for name in ("consult_report.md", "questions.json", "state.json", "brief.md", "spec_design.txt", "checks_design.json"):
        assert (sc.run_dir / name).is_file(), name
    assert (sc.workdir / "docs/lv5a_design_x.md").is_file()
    questions = json.loads((sc.run_dir / "questions.json").read_text(encoding="utf-8"))
    assert [q["id"] for q in questions] == ["Q1", "G1"]
    g1 = questions[-1]
    assert [o["label"] for o in g1["options"]] == [ltext.G1_YES, ltext.G1_REDO, ltext.G1_STOP]
    assert g1["recommended"] == ltext.G1_YES and g1["recommended_reason"] == "" and g1["recommended"] in [o["label"] for o in g1["options"]]
    design = json.loads((sc.run_dir / "checks_design.json").read_text(encoding="utf-8"))
    assert design["frozen"] == ["*"] and [c["type"] for c in design["checks"]] == ["lf"]
    spec = (sc.run_dir / "spec_design.txt").read_text(encoding="utf-8")
    assert "slugify を追加する" in spec and "docs/lv5a_design_x.md" in spec and "コードは書かない" in spec
    argv = sc.lv0.argvs("design")[0]
    assert argv[argv.index("--max-fix-rounds") + 1] == "1" and argv[argv.index("--workdir") + 1] == str(sc.workdir)
    review = sc.lv3a.runs("design")[0]
    files = review[review.index("--files") + 1: review.index("--label")]
    assert files == [str(sc.workdir / "docs/lv5a_design_x.md"), str(sc.ref)]
    assert review[review.index("--work-root") + 1] == str(sc.run_dir / "review_design")
    assert "--redact" not in review and "--no-ds" not in review
    assert [s["exit"] for s in st["stages"]] == [0, 0] and all(s["record_dir"] for s in st["stages"])
    brief_review = (sc.run_dir / "brief_review.md").read_text(encoding="utf-8")
    assert "## レビュー対象" in brief_review and "設計案" in brief_review and "## 確認済みの事実" in brief_review


def test_consult_report_within_limit_and_has_key_parts(sc: Scenario, capsys) -> None:
    sc.lv3a.plans["design"] = [Lv3Plan(clusters=[cl("採用の重大"), cl("見送りの重大", "🔴", "見送り")])]
    assert sc.consult() == 20
    text = (sc.run_dir / "consult_report.md").read_text(encoding="utf-8")
    assert len(text.splitlines()) <= 60
    assert "設計の行 25" in text and "設計の行 26" not in text  # 冒頭 25 行
    assert "見送りの重大" in text and "採用の重大" in text and "G1" in text and "Codex: 4 回 / 61,000 tokens" in text and "¥1.500" in text
    assert capsys.readouterr().out == text  # 標準出力に出る = Claude が読む


def test_consult_design_excerpt_skips_blank_lines(sc: Scenario) -> None:
    sc.lv0.plans["design"] = [Lv0Plan(design_text="".join(f"行{i}\n\n" for i in range(1, 40)))]
    assert sc.consult() == 20
    lines = (sc.run_dir / "consult_report.md").read_text(encoding="utf-8").splitlines()
    assert "> 行25" in lines and "> 行26" not in lines and "> " not in lines  # 空行で 25 行を使い切らない


def test_consult_flags_are_passed_through(sc: Scenario) -> None:
    assert sc.consult("--no-ds", "--effort", "high") == 20
    review = sc.lv3a.runs("design")[0]
    assert "--no-ds" in review and "--redact" not in review and review[review.index("--effort") + 1] == "high"
    assert "--no-ds" in sc.lv3a.calls[0] and "--review" not in sc.lv0.calls[0]
    assert sc.lv0.argvs("design")[0][sc.lv0.argvs("design")[0].index("--effort") + 1] == "high"


@pytest.mark.parametrize("bad", [{"make_design": False}, {"design_text": "  \n"}])
def test_consult_fails_when_design_missing_or_empty(sc: Scenario, bad: dict[str, Any]) -> None:
    sc.lv0.plans["design"] = [Lv0Plan(**bad)]
    assert sc.consult() == 3
    st = sc.state()
    assert st["phase"] == "consult_failed" and st["failure"]["exit"] == 3 and not sc.lv3a.runs("design")


@pytest.mark.parametrize("code,expected", [(1, 1), (2, 2), (3, 3), (10, 10), (11, 11), (30, 30), (99, 1)])
def test_consult_maps_lv0_exit(sc: Scenario, code: int, expected: int) -> None:
    sc.lv0.plans["design"] = [Lv0Plan(exit=code, status="codex_failed")]
    assert sc.consult() == expected
    assert sc.state()["phase"] == "consult_failed" and not sc.lv3a.runs("design")


@pytest.mark.parametrize("code,expected", [(1, 1), (2, 2), (10, 12), (11, 11), (12, 12), (13, 12), (30, 30), (7, 1)])
def test_consult_maps_lv3a_exit(sc: Scenario, code: int, expected: int) -> None:
    sc.lv3a.plans["design"] = [Lv3Plan(exit=code, stderr="理由あり")]
    assert sc.consult() == expected
    st = sc.state()
    assert st["phase"] == "consult_failed" and st["failure"]["exit"] == expected and "理由あり" in st["failure"]["reason"]
    assert not (sc.run_dir / "questions.json").exists()


def test_failed_lv3a_cost_is_counted(sc: Scenario) -> None:
    sc.lv0.plans["design"] = [Lv0Plan(tokens=500)]
    sc.lv3a.plans["design"] = [Lv3Plan(exit=10, costs={"codex_calls": 2, "codex_tokens": 7000, "ds_calls": 1, "ds_yen": 0.75})]
    assert sc.consult() == 12
    costs = sc.state()["costs"]
    assert (costs["codex_calls"], costs["codex_tokens"], costs["ds_calls"], costs["ds_yen"]) == (3, 7500, 1, 0.75)


def test_lv3a_exit_20_is_success_with_warning(sc: Scenario) -> None:
    sc.lv3a.plans["design"] = [Lv3Plan(exit=20, partial=[{"name": "ds_tech", "reason": "タイムアウト"}])]
    assert sc.consult() == 20
    st = sc.state()
    assert st["phase"] == "awaiting_direction" and st["consult"]["lv3a_exit"] == 20
    assert any("暫定成功" in w and "ds_tech（タイムアウト）" in w for w in st["consult"]["warnings"])  # 欠けた者と理由を出す
    assert "ds_tech（タイムアウト）" in (sc.run_dir / "consult_report.md").read_text(encoding="utf-8")
    assert (sc.run_dir / "questions.json").is_file()


def test_lv3a_exit_20_without_partial_info_still_success(sc: Scenario) -> None:
    sc.lv3a.plans["design"] = [Lv3Plan(exit=20)]  # run.json に partial が無い (形が変わった) 場合でも成功扱い・警告は「不明」
    assert sc.consult() == 20
    assert any("暫定成功" in w and "不明" in w for w in sc.state()["consult"]["warnings"])


def test_design_extra_files_and_frozen_violation_warn(sc: Scenario) -> None:
    sc.lv0.plans["design"] = [Lv0Plan(files=["added:docs/lv5a_design_x.md", "added:src/extra.py"], frozen="strutil.py")]
    assert sc.consult() == 20
    warnings = sc.state()["consult"]["warnings"]
    assert any("src/extra.py" in w for w in warnings) and any("既存ファイルを変更" in w for w in warnings)


def test_consult_stops_before_spending_on_bad_inputs(sc: Scenario) -> None:
    write(sc.tmp / "nofacts.md", "# 依頼\n本文だけ\n")
    base = ["consult", "--workdir", str(sc.workdir), "--checks", str(sc.checks), "--work-root", str(sc.work_root)]
    assert sc.main([*base, "--brief", str(sc.tmp / "nofacts.md"), "--label", "x"]) == 1
    assert sc.main([*base, "--brief", str(sc.brief), "--label", "bad label!"]) == 1
    assert sc.main([*base, "--brief", str(sc.brief), "--label", "x", "--files", str(sc.tmp / "none.md")]) == 1
    inside = write(sc.workdir / "checks_inside.json", "{}")
    assert sc.main(["consult", "--brief", str(sc.brief), "--workdir", str(sc.workdir), "--checks", str(inside), "--label", "x",
                    "--work-root", str(sc.work_root)]) == 1
    assert sc.lv0.calls == [] and sc.lv3a.calls == [] and not sc.work_root.exists()


def test_consult_refuses_existing_design_file(sc: Scenario) -> None:
    write(sc.workdir / "docs/lv5a_design_x.md", "既存")
    assert sc.consult() == 1
    assert sc.lv0.calls == [] and (sc.workdir / "docs/lv5a_design_x.md").read_text(encoding="utf-8") == "既存"


def test_consult_preflight_plan_failure_writes_nothing(sc: Scenario) -> None:
    sc.lv3a.plan_exit = 11
    assert sc.consult() == 11
    assert not sc.work_root.exists() and not sc.lv0.runs


def test_consult_lv3a_question_id_collision_fails(sc: Scenario) -> None:
    sc.lv3a.plans["design"] = [Lv3Plan(questions=[question("G1")])]
    assert sc.consult() == 12 and "G1" in sc.state()["failure"]["reason"]


def test_consult_fails_when_review_dir_is_ambiguous(sc: Scenario) -> None:
    class Twin(FakeLv3a):
        def __call__(self, argv: list[str], timeout: int) -> CliResult:
            result = super().__call__(argv, timeout)
            if argv[0] == "run":
                (Path(argv[argv.index("--work-root") + 1]) / "other").mkdir()
            return result

    sc.drivers = Drivers(sc.lv0, Twin())
    assert sc.consult() == 12
    assert "唯一" in sc.state()["failure"]["reason"]


def test_executor_exception_is_recorded_not_swallowed(sc: Scenario) -> None:
    def boom(argv: list[str], timeout: int) -> CliResult:
        if argv[0] == "run":
            raise RuntimeError("実行器が壊れた")
        return CliResult(0, "ok")

    sc.drivers = Drivers(sc.lv0, boom)
    assert sc.consult() == 1  # 設計は成功し、Lv3A の実行器が例外 → 記録して consult_failed
    st = sc.state()
    assert st["phase"] == "consult_failed" and "実行器が例外" in st["stages"][-1]["stderr_tail"]


def test_unexpected_exception_is_recorded_with_traceback(sc: Scenario, monkeypatch) -> None:
    monkeypatch.setattr(target, "design_spec", lambda *a: (_ for _ in ()).throw(ValueError("壊れた")))
    assert sc.consult() == 1
    st = sc.state()
    assert st["phase"] == "consult_failed" and "ValueError" in st["crash"] and "想定外" in st["failure"]["reason"]


# ---------------------------------------------------------------- implement: 状態遷移と回答


def test_implement_only_from_awaiting_direction(consulted: Scenario) -> None:
    sc = consulted
    assert sc.implement() == 0 and sc.state()["phase"] == "awaiting_acceptance"
    n = len(sc.lv0.runs)
    assert sc.implement() == 1  # 二度目は phase 違反
    assert len(sc.lv0.runs) == n


@pytest.mark.parametrize("phase", ["consulting", "implementing", "implement_failed", "consult_failed", "awaiting_acceptance"])
def test_implement_rejects_wrong_phases(consulted: Scenario, phase: str) -> None:
    sc = consulted
    st = sc.state()
    st["phase"] = phase
    write(sc.run_dir / "state.json", json.dumps(st))
    n = len(sc.lv0.runs)
    assert sc.implement() == 1
    assert len(sc.lv0.runs) == n and sc.state()["phase"] == phase


@pytest.mark.parametrize("g1", [ltext.G1_REDO, ltext.G1_STOP])
def test_implement_requires_g1_yes(consulted: Scenario, g1: str, capsys) -> None:
    sc = consulted
    assert sc.implement(sc.write_answers(g1)) == 1
    assert sc.state()["phase"] == "awaiting_direction" and len(sc.lv0.runs) == 1  # 設計の 1 回だけ
    assert "実装しない" in capsys.readouterr().err


@pytest.mark.parametrize("data,needle", [
    ({"answers": {}}, "G1"),
    ({"answers": {"G1": ltext.G1_YES, "Q9": "x"}}, "存在しない質問"),
    ({"answers": {"G1": "この設計で実装します"}}, "完全一致"),
    ({"answers": {"G1": ltext.G1_YES, "Q1": "案a"}}, "完全一致"),
    ({"answers": {"G1": ltext.G1_YES, "Q1": 1}}, "完全一致"),
    ({"answers": ["G1"]}, "形"),
    ({"answers": {"G1": ltext.G1_YES}, "notes": 3}, "notes"),
])
def test_answers_are_validated(consulted: Scenario, data: dict[str, Any], needle: str, capsys) -> None:
    sc = consulted
    assert sc.implement(write(sc.tmp / "a.json", json.dumps(data, ensure_ascii=False))) == 1
    assert needle in capsys.readouterr().err and len(sc.lv0.runs) == 1


def test_answers_file_problems(consulted: Scenario) -> None:
    sc = consulted
    assert sc.implement(sc.tmp / "missing.json") == 1
    assert sc.implement(write(sc.tmp / "bad.json", "{not json")) == 1
    assert len(sc.lv0.runs) == 1


def test_implement_checks_same_inputs_as_consult(consulted: Scenario) -> None:
    sc = consulted
    write(sc.checks, json.dumps({"checks": [{"name": "lf", "type": "lf"}], "frozen": []}))
    assert sc.implement() == 1
    write(sc.checks, json.dumps({"checks": [{"name": "lf", "type": "lf"}], "frozen": ["tests/**"]}))
    (sc.workdir / "docs/lv5a_design_x.md").write_text("書き換えられた設計", encoding="utf-8")
    assert sc.implement() == 1
    assert len(sc.lv0.runs) == 1 and sc.state()["phase"] == "awaiting_direction"


# ---------------------------------------------------------------- implement: 仕様・自動修正・レビュー


def test_impl_spec_contents(consulted: Scenario) -> None:
    sc = consulted
    assert sc.implement(sc.write_answers(notes="補足のことば")) == 0
    spec = (sc.run_dir / "spec_impl.txt").read_text(encoding="utf-8")
    assert len(spec.encode("utf-8")) <= 12 * 1024
    assert "slugify を追加する" in spec and "docs/lv5a_design_x.md" in spec
    assert "採用の重大" in spec and "採用の重要" in spec and "R1#1 (Codex): codex_techの指摘1" in spec and "R2#2 (DeepSeek): ds_techの指摘2" in spec
    for absent in ("見送りの重大", "軽微", "見送りの重要", "批評の高"):
        assert absent not in spec, absent
    assert spec.index("採用の重大") < spec.index("採用の重要")  # 🔴 が先
    assert "- G1 この設計で実装に進みますか？ → この設計で実装する" in spec
    assert "- Q1 どちらにしますか？ → （未回答・推奨を採用）案A" in spec
    assert "補足: 補足のことば" in spec and "凍結ファイル" in spec and "tests/**" in spec and "作業フォルダの外に書かない" in spec
    assert "## Lv5A の制約（必ず守る）" in spec and spec.count("## 制約") == 1  # 依頼文の「## 制約」と見出しが重ならない
    # 回答は設計より優先し、設計書の該当箇所も更新してよい (E2E で判明: 回答と設計が食い違うと Codex が実装を止めて質問した)
    assert "ユーザーの回答は設計より優先する" in spec and "設計ファイルの該当箇所" in spec
    argv = sc.lv0.argvs("impl")[0]
    assert argv[argv.index("--max-fix-rounds") + 1] == "2" and argv[argv.index("--review") + 1] == "deepseek"


def test_answered_question_is_quoted_verbatim(consulted: Scenario) -> None:
    sc = consulted
    assert sc.implement(sc.write_answers(extra={"Q1": "案B"})) == 0
    assert "- Q1 どちらにしますか？ → 案B" in (sc.run_dir / "spec_impl.txt").read_text(encoding="utf-8")


def test_impl_spec_limit_with_long_brief_and_many_clusters(consulted: Scenario) -> None:
    brief = "長い依頼 " * 6000
    blocks = [f"- [🔴] 指摘{i}\n  対応案: 直す" * 3 for i in range(80)]
    spec = ltext.build_impl_spec(brief, "docs/d.md", blocks, ["- G1 質問 → この設計で実装する"], "", ["tests/**"])
    assert len(spec.encode("utf-8")) <= 12 * 1024
    assert "指摘0" in spec and "- G1 質問 → この設計で実装する" in spec and "docs/d.md" in spec
    tiny = ltext.build_impl_spec("短い", "docs/d.md", [], ["- G1 質問 → 答え"], "", [])
    assert "省略" not in tiny and "短い" in tiny


def test_lv3a_review_brief_has_target_and_facts(consulted: Scenario) -> None:
    sc = consulted
    assert sc.implement() == 0
    brief = (sc.run_dir / "brief_review_impl.md").read_text(encoding="utf-8")
    facts = brief.split("## 確認済みの事実", 1)[1].split("\n## ", 1)[0]
    assert "strutil.py がある" in facts and "機械検査は全部合格した" in facts and "lf=ok" in facts and "+10 -2 行" in facts
    assert "## レビュー対象" in brief and "実装差分" in brief and brief.index("## 制約") < brief.index("## レビュー対象")
    # ユーザーの決定も事実として渡す (渡さないと、レビューが「未決のまま実装で確定した」と誤って指摘する: E2E で判明)
    assert "ユーザーが方向性を確認済み" in facts and "G1 この設計で実装に進みますか？ → この設計で実装する" in facts
    assert "Q1 どちらにしますか？ → （未回答・推奨を採用）案A" in facts
    review = sc.lv3a.runs("impl")[0]
    files = review[review.index("--files") + 1: review.index("--label")]
    assert files == [str(sc.run_dir / "impl_overall.patch"), str(sc.workdir / "docs/lv5a_design_x.md")]
    assert (sc.run_dir / "impl_overall.patch").read_text(encoding="utf-8").startswith("--- a/strutil.py")
    assert "--redact" not in review and "--no-ds" not in review


def test_no_red_means_no_autofix_and_exit_0(consulted: Scenario) -> None:
    sc = consulted
    sc.lv3a.plans["impl"] = [Lv3Plan(clusters=[cl("批評の高", "高", "採用", "critic"), cl("重要", "🟠"), cl("軽微", "🟡")])]
    assert sc.implement() == 0
    assert not sc.lv0.argvs("fix") and not sc.lv3a.runs("impl2")
    st = sc.state()
    assert st["phase"] == "awaiting_acceptance" and st["exit_code"] == 0 and st["result"]["autofix"]["rounds"] == 0
    report = (sc.run_dir / "final_report.md").read_text(encoding="utf-8")
    assert "実装結果" in report and "レビュー結果" in report and "自動修正の記録" in report and "実施せず" in report and "G2" in report
    assert "差分レビュー（Lv3A）:" in report and "review_impl" in report and "lv0auto_2" in report  # 各記録の場所
    g2 = json.loads((sc.run_dir / "questions_final.json").read_text(encoding="utf-8"))[0]
    assert g2["id"] == "G2" and [o["label"] for o in g2["options"]] == [ltext.G2_ACCEPT, ltext.G2_REDO, ltext.G2_DISCARD]
    assert g2["recommended"] == ltext.G2_ACCEPT


def test_autofix_success_path(consulted: Scenario) -> None:
    sc = consulted
    sc.lv3a.plans["impl"] = [Lv3Plan(clusters=[
        cl("直すべき重大", members=("R1#1",)), cl("部分採用の重大", "🔴", "部分採用", members=("R2#1",)),
        cl("批評の高", "高", "採用", "critic"), cl("重要だけ", "🟠")], costs={"codex_calls": 3, "codex_tokens": 100, "ds_calls": 2, "ds_yen": 1.0})]
    sc.lv3a.plans["impl2"] = [Lv3Plan(clusters=[cl("重要だけ", "🟠")], costs={"codex_calls": 3, "codex_tokens": 50, "ds_calls": 0, "ds_yen": 0.0})]
    assert sc.implement() == 0
    assert len(sc.lv0.argvs("fix")) == 1
    fix_argv = sc.lv0.argvs("fix")[0]
    assert fix_argv[fix_argv.index("--max-fix-rounds") + 1] == "1" and "--review" not in fix_argv
    fix_spec = (sc.run_dir / "spec_fix.txt").read_text(encoding="utf-8")
    assert "直すべき重大" in fix_spec and "部分採用の重大" in fix_spec and "R1#1 (Codex): codex_techの指摘1" in fix_spec
    for absent in ("見送り", "批評の高", "重要だけ"):
        assert absent not in fix_spec
    assert "- G1 この設計で実装に進みますか？ → この設計で実装する" in fix_spec and "ユーザーの回答は設計より優先する" in fix_spec
    re_review = sc.lv3a.runs("impl2")
    assert len(re_review) == 1 and "--no-ds" in re_review[0]
    files = re_review[0][re_review[0].index("--files") + 1: re_review[0].index("--label")]
    assert [Path(f).name for f in files] == ["impl_overall.patch", "fix_overall.patch", "lv5a_design_x.md"]
    af = sc.state()["result"]["autofix"]
    assert af["rounds"] == 1 and af["resolved"] == 2 and af["remaining"] == [] and af["stop_reason"] == ""
    report = (sc.run_dir / "final_report.md").read_text(encoding="utf-8")
    assert "解消した 🔴: 2 件" in report and "周回数: 1" in report
    assert "修正の差分: 更新 1 / 新規 0 / 削除 0、+10 -2 行" in report and "自動修正 Lv0:" in report
    facts2 = (sc.run_dir / "brief_review_impl2.md").read_text(encoding="utf-8").split("## 確認済みの事実", 1)[1].split("\n## ", 1)[0]
    assert "自動修正させた" in facts2 and "ユーザーが方向性を確認済み" in facts2 and "機械検査は全部合格" in facts2


def test_autofix_never_runs_a_second_round(consulted: Scenario) -> None:
    sc = consulted
    sc.lv3a.plans["impl"] = [Lv3Plan(clusters=[cl("同じ重大")])]
    sc.lv3a.plans["impl2"] = [Lv3Plan(clusters=[cl("同じ重大！")]), Lv3Plan(clusters=[])]
    assert sc.implement() == 21
    assert len(sc.lv0.argvs("fix")) == 1 and len(sc.lv3a.runs("impl2")) == 1
    assert len(sc.lv3a.plans["impl2"]) == 1  # 2 つ目のレビュー計画は使われていない (2 周目を回していない)
    st = sc.state()
    af = st["result"]["autofix"]
    assert st["phase"] == "awaiting_acceptance" and af["remaining"] == ["同じ重大！"] and af["recurred"] == ["同じ重大！"]
    assert "再発" in af["stop_reason"] and "2 周目は回さない" in af["stop_reason"]
    report = (sc.run_dir / "final_report.md").read_text(encoding="utf-8")
    assert "要ユーザー判断" in report and "同じ重大！" in report
    assert json.loads((sc.run_dir / "questions_final.json").read_text(encoding="utf-8"))[0]["recommended"] == ltext.G2_REDO


def test_autofix_function_refuses_a_second_call(sc: Scenario, tmp_path: Path) -> None:
    """構造的な歯止め: 1 周済み (rounds=1) の状態で autofix_once を呼んでも Lv0 を起動しない。"""
    state = {"label": "x", "design_rel": "docs/d.md", "args": {"workdir": str(sc.workdir), "checks": str(sc.checks)}, "stages": [], "history": []}
    sess = target.Session(tmp_path, state, sc.drivers)
    review1 = target.Review(True, "", lio.Lv3aRun(0, run_dir=tmp_path, clusters=[cl("重大")]))
    done = {"rounds": 1, "targets": [], "resolved": 0, "remaining": [], "recurred": [], "stop_reason": ""}
    with pytest.raises(target.DriverError, match="1 周まで"):
        target.autofix_once(sess, done, review1, "依頼", tmp_path / "d.md", "medium", [], "確認済み")
    assert sc.lv0.calls == [] and sc.lv3a.calls == []


def test_new_red_after_autofix_also_stops(consulted: Scenario) -> None:
    sc = consulted
    sc.lv3a.plans["impl"] = [Lv3Plan(clusters=[cl("最初の重大")])]
    sc.lv3a.plans["impl2"] = [Lv3Plan(clusters=[cl("まったく別の新しい重大な問題")])]
    assert sc.implement() == 21
    af = sc.state()["result"]["autofix"]
    assert af["remaining"] and af["recurred"] == [] and len(sc.lv0.argvs("fix")) == 1
    assert af["resolved"] == 0  # 残りがあるときは件数で数える (下限)。題名が違っても「解消」と言い切らない


def test_reworded_red_is_not_reported_as_resolved(consulted: Scenario) -> None:
    """実走 3 回目: 同じ問題が別の題名で残った。題名照合では「解消 1 件」と出て、実態 (何も直っていない) と食い違った。"""
    sc = consulted
    sc.lv3a.plans["impl"] = [Lv3Plan(clusters=[cl("空白のみ入力と原文保持の矛盾")])]
    sc.lv3a.plans["impl2"] = [Lv3Plan(clusters=[cl("空白のみ入力が原文保持されない")])]
    assert sc.implement() == 21
    af = sc.state()["result"]["autofix"]
    assert af["resolved"] == 0 and af["remaining"] == ["空白のみ入力が原文保持されない"]
    report = (sc.run_dir / "final_report.md").read_text(encoding="utf-8")
    assert "解消した 🔴: 0 件（下限・件数で数えた）" in report and "空白のみ入力が原文保持されない" in report


def test_resolved_count_with_two_targets_and_one_left(consulted: Scenario) -> None:
    sc = consulted
    sc.lv3a.plans["impl"] = [Lv3Plan(clusters=[cl("重大その一"), cl("まったく別の重大その二")])]
    sc.lv3a.plans["impl2"] = [Lv3Plan(clusters=[cl("別の言い回しの重大")])]
    assert sc.implement() == 21
    assert sc.state()["result"]["autofix"]["resolved"] == 1  # 2 件のうち 1 件が残った → 下限 1


def test_unadopted_red_is_not_fixed_but_needs_judgment(consulted: Scenario) -> None:
    sc = consulted
    sc.lv3a.plans["impl"] = [Lv3Plan(clusters=[cl("見送りの重大", "🔴", "見送り")])]
    assert sc.implement() == 21
    assert not sc.lv0.argvs("fix") and not sc.lv3a.runs("impl2")
    assert "見送りの重大" in (sc.run_dir / "final_report.md").read_text(encoding="utf-8")


def test_autofix_check_failure_stops_without_rereview(consulted: Scenario) -> None:
    sc = consulted
    sc.lv3a.plans["impl"] = [Lv3Plan(clusters=[cl("直すべき重大")])]
    sc.lv0.plans["fix"] = [Lv0Plan(exit=10, status="checks_failed")]
    assert sc.implement() == 21
    assert not sc.lv3a.runs("impl2")
    st = sc.state()
    assert st["phase"] == "awaiting_acceptance" and "自動修正が失敗" in st["result"]["autofix"]["stop_reason"]
    assert "自動修正が失敗" in (sc.run_dir / "final_report.md").read_text(encoding="utf-8")


def test_rereview_failure_stops(consulted: Scenario) -> None:
    sc = consulted
    sc.lv3a.plans["impl"] = [Lv3Plan(clusters=[cl("直すべき重大")])]
    sc.lv3a.plans["impl2"] = [Lv3Plan(exit=13)]
    assert sc.implement() == 21
    assert "再レビューを確認できない" in sc.state()["result"]["autofix"]["stop_reason"]


@pytest.mark.parametrize("code,word", [(1, "レビュー未実施"), (10, "レビュー失敗"), (11, "レビュー失敗"), (12, "レビュー失敗"), (13, "レビュー失敗"), (30, "レビュー失敗")])
def test_review_problem_keeps_implementation_result(consulted: Scenario, code: int, word: str) -> None:
    sc = consulted
    sc.lv3a.plans["impl"] = [Lv3Plan(exit=code, stderr="入力合計が 200KB を超えています")]
    assert sc.implement() == 21
    st = sc.state()
    assert st["phase"] == "awaiting_acceptance" and st["result"]["impl"]["status"] == "ok" and st["result"]["review1"]["performed"] is False
    assert word in st["result"]["review1"]["reason"]
    report = (sc.run_dir / "final_report.md").read_text(encoding="utf-8")
    assert word in report and "lf=ok" in report
    assert "None" not in report  # Lv3A の run が無いとき「差分レビュー（Lv3A）: None」と出していた (2026-09-20 の検証)
    if code == 1:
        assert "自動レビューしていない" in report and "impl_overall.patch" in report and "/lv3a" in report
    assert not sc.lv0.argvs("fix")


def test_lv3a_exit_20_in_review_is_success_with_note(consulted: Scenario) -> None:
    sc = consulted
    sc.lv3a.plans["impl"] = [Lv3Plan(exit=20, partial=[{"name": "codex_crit", "reason": "実行失敗"}])]
    assert sc.implement() == 0
    rec = sc.state()["result"]["review1"]
    assert rec["performed"] is True and "暫定" in rec["reason"] and "codex_crit（実行失敗）" in rec["reason"]
    assert "codex_crit（実行失敗）" in (sc.run_dir / "final_report.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("code", [3, 10, 11, 30, 1])
def test_impl_failure_maps_exit_and_phase(consulted: Scenario, code: int) -> None:
    sc = consulted
    sc.lv0.plans["impl"] = [Lv0Plan(exit=code, status="checks_failed")]
    assert sc.implement() == code
    st = sc.state()
    assert st["phase"] == "implement_failed" and st["failure"]["exit"] == code
    assert not sc.lv3a.runs("impl") and st["result"]["impl"]["autodir"]
    assert sc.implement() == 1  # implement_failed からは再開できない


REAL_QUESTION = "1. 変更・作成したファイル\nなし（設計と回答が矛盾するため実装を保留）\n\n3. 未解決事項・質問\n設計は「削除」ですが回答は「例外」です。どちらですか"


def test_no_change_shows_codex_questions(consulted: Scenario, capsys) -> None:
    """実走 2 回目: Codex が「設計と回答が矛盾」と質問を返して何も変更しなかった。質問を見えるところへ出す。"""
    sc = consulted
    sc.lv0.plans["impl"] = [Lv0Plan(exit=3, status="no_change", questions=REAL_QUESTION)]
    capsys.readouterr()
    assert sc.implement() == 3
    err = capsys.readouterr().err
    assert "Codex の最終報告（抜粋）" in err and "設計と回答が矛盾するため実装を保留" in err and "どちらですか" in err
    failure = sc.state()["failure"]
    assert failure["exit"] == 3 and "どちらですか" in failure["detail"] and "no_change" in failure["reason"]


def test_design_no_change_shows_codex_questions(sc: Scenario, capsys) -> None:
    sc.lv0.plans["design"] = [Lv0Plan(exit=3, status="no_change", questions=REAL_QUESTION)]
    assert sc.consult() == 3
    assert "どちらですか" in capsys.readouterr().err and "どちらですか" in sc.state()["failure"]["detail"]


def test_parse_lv0_codex_note() -> None:
    report = "# レポート\n\nCodex の最終報告（抜粋）:\n```\n" + REAL_QUESTION + "\n```\n\n次にすること: 最終報告の質問に答える\n"
    assert lio.parse_lv0(CliResult(3, report)).codex_note == REAL_QUESTION
    assert lio.parse_lv0(CliResult(0, "# レポート\n")).codex_note == ""


def test_no_ds_choices(consulted: Scenario) -> None:
    sc = consulted
    assert sc.implement(None, "--no-ds") == 0
    assert "--review" not in sc.lv0.argvs("impl")[0] and "--no-ds" in sc.lv3a.runs("impl")[0]


def test_no_ds_from_consult_is_inherited(sc: Scenario) -> None:
    assert sc.consult("--no-ds") == 20
    assert sc.implement() == 0
    assert "--review" not in sc.lv0.argvs("impl")[0] and "--no-ds" in sc.lv3a.runs("impl")[0]


def test_redact_is_refused_before_anything_runs(sc: Scenario, capsys) -> None:
    """--redact は設計段階の Codex (Lv0) に効かない (依頼文がそのまま渡る。2026-09-20 の検証でダミー鍵が
    Codex のセッションログに残った)。plan も consult も、何も呼ばず・何も書かずに拒否する。"""
    plan_args = ["plan", "--brief", str(sc.brief), "--workdir", str(sc.workdir), "--checks", str(sc.checks), "--redact"]
    assert sc.main(plan_args) == 1
    assert sc.consult("--redact") == 1
    err = capsys.readouterr().err
    assert err.count("--redact を受け付けません") == 2 and "/lv3a" in err
    assert sc.lv0.calls == [] and sc.lv3a.calls == []
    assert not sc.work_root.exists() and not (sc.workdir / "docs").exists()


def test_no_redact_flag_reaches_lv3a_in_any_stage(consulted: Scenario) -> None:
    sc = consulted
    assert sc.implement() == 0
    assert sc.lv3a.calls and all("--redact" not in call for call in sc.lv3a.calls)
    assert "redact" not in sc.state()["args"]


def test_scrub_replaces_the_lv3a_redact_advice() -> None:
    """Lv3A の「--redact を付けて続行」は Lv5A では従えない案内。Lv5A で通る案内へ差し替え、候補の行番号は残す。"""
    hint = "伏字して続行するには --redact を付け、ユーザーの承認を取ってください（plan --redact で伏字プレビューを確認できます）"
    text = "秘匿情報候補を検出しました（内容は非表示）:\nx.md: api-key / 行 3\n" + hint
    out = lio.scrub_redact_hint(text)
    assert "--redact" not in out and lio.REDACT_UNAVAILABLE in out
    assert "x.md: api-key / 行 3" in out and out.count("\n") == text.count("\n")
    assert lio.scrub_redact_hint("何も無い") == "何も無い"
    assert "--redact" not in lio.scrub_redact_hint("停止: 行 7 " + hint)  # 空白を潰した 1 行の形でも効く


def test_scrub_wrapper_cleans_stdout_and_stderr() -> None:
    hint = "伏字して続行するには --redact を付け、ユーザーの承認を取ってください"
    wrapped = lio.without_redact_hint(lambda args, timeout: CliResult(1, "out\n" + hint, "err\n" + hint, 1.5))
    result = wrapped(["plan"], 10)
    assert result.returncode == 1 and result.elapsed == 1.5
    assert "--redact" not in result.stdout + result.stderr and result.stdout.startswith("out\n") and result.stderr.startswith("err\n")


def test_default_lv3a_driver_scrubs_the_advice_from_the_real_plan(tmp_path: Path) -> None:
    """実物の Lv3A plan が秘匿候補で止まったとき、Lv5A 経由の出力に「--redact を付けて」が出ない (中身も出ない)。"""
    secret = 'api_key = "' + "sk-" + "DUMMY" * 4 + '0123"'
    brief = write(tmp_path / "brief.md", f"# 依頼\n\n## 確認済みの事実\n- ダミーの依頼です\n\n{secret}\n")
    result = lio.default_drivers().lv3a(["plan", "--brief", str(brief), "--no-ds"], 120)
    output = result.stdout + result.stderr
    assert result.returncode == 1 and "秘匿情報候補" in output
    assert "--redact" not in output and lio.REDACT_UNAVAILABLE in output
    assert "DUMMYDUMMY" not in output


def test_same_workdir_and_checks_in_every_lv0_call(consulted: Scenario) -> None:
    sc = consulted
    sc.lv3a.plans["impl"] = [Lv3Plan(clusters=[cl("直すべき重大")])]
    sc.lv3a.plans["impl2"] = [Lv3Plan()]
    assert sc.implement() == 0
    state_args = sc.state()["args"]
    for kind in ("impl", "fix"):
        argv = sc.lv0.argvs(kind)[0]
        assert argv[argv.index("--workdir") + 1] == state_args["workdir"] and argv[argv.index("--checks") + 1] == state_args["checks"]
    assert sc.lv0.argvs("design")[0][sc.lv0.argvs("design")[0].index("--workdir") + 1] == state_args["workdir"]


def test_max_fix_rounds_and_effort_options(consulted: Scenario) -> None:
    sc = consulted
    assert sc.implement(None, "--max-fix-rounds", "0", "--effort", "high") == 0
    argv = sc.lv0.argvs("impl")[0]
    assert argv[argv.index("--max-fix-rounds") + 1] == "0" and argv[argv.index("--effort") + 1] == "high"
    assert sc.lv3a.runs("impl")[0][sc.lv3a.runs("impl")[0].index("--effort") + 1] == "high"


# ---------------------------------------------------------------- 費用・レポート・状態


def test_costs_are_summed_across_lv0_and_lv3a_runs_including_failures(consulted: Scenario) -> None:
    sc = consulted  # design: Lv0 1000tok + Lv3A(60000tok, 3 回, DS 2 回 ¥1.5)
    sc.lv0.plans["impl"] = [Lv0Plan(tokens=3000, rounds=2, review_line="🔴0 🟠1 🟡0（全文: x）")]
    sc.lv3a.plans["impl"] = [Lv3Plan(exit=10, costs={"codex_calls": 2, "codex_tokens": 4000, "ds_calls": 1, "ds_yen": 0.25})]
    assert sc.implement() == 21
    costs = sc.state()["costs"]
    assert costs["codex_calls"] == 1 + 3 + 2 + 2 and costs["codex_tokens"] == 1000 + 60000 + 3000 + 4000
    assert costs["ds_calls"] == 3 and costs["ds_yen"] == pytest.approx(1.75) and costs["ds_yen_unknown_calls"] == 1
    report = (sc.run_dir / "final_report.md").read_text(encoding="utf-8")
    assert "68,000 tokens" in report and "¥1.750" in report and "費用不明" in report


def test_reports_stay_within_60_lines_under_stress(consulted: Scenario) -> None:
    sc = consulted
    many = [cl(f"長い題名の重大指摘その{i}" * 3, "🔴", "見送り") for i in range(60)] + [cl(f"重要{i}", "🟠") for i in range(40)]
    sc.lv3a.plans["impl"] = [Lv3Plan(clusters=many)]
    assert sc.implement() == 21
    assert len((sc.run_dir / "final_report.md").read_text(encoding="utf-8").splitlines()) <= 60
    st = {**sc.state(), "consult": {"warnings": ["w"] * 9}}
    rv = lio.Lv3aRun(0, clusters=many, questions=[question("Q1"), question("Q2"), question("Q3")])

    def build(n_design: int, n_red: int) -> list[str]:
        return ltext.consult_lines(st, "設計の行\n" * 200, rv, n_design, n_red)

    assert len(ltext.fit_report(build, [25, 8], [5, 1]).splitlines()) <= 60
    tight = ltext.fit_report(build, [25, 8], [5, 1], limit=40)
    assert len(tight.splitlines()) <= 40 and "冒頭 25 行" not in tight  # 設計案の抜粋から先に減らす
    assert "他 " in tight and "🔴" in tight  # 🔴 の存在は最後まで残す


def test_fit_report_last_resort_cut() -> None:
    out = ltext.fit_report(lambda: [f"行{i}" for i in range(500)], [], [])
    assert len(out.splitlines()) == 60 and "省略" in out.splitlines()[-1]


def test_state_is_complete_and_atomic(consulted: Scenario) -> None:
    sc = consulted
    assert sc.implement() == 0
    assert not (sc.run_dir / "state.json.tmp").exists()
    st = sc.state()
    for key in ("phase", "args", "stages", "costs", "created", "updated", "history", "design_rel", "design_sha256", "answers", "result"):
        assert key in st, key
    assert [s["name"] for s in st["stages"]] == ["design", "design_review", "impl", "review_impl"]
    assert all({"exit", "record_dir", "argv", "cost", "elapsed"} <= set(s) for s in st["stages"])
    assert [h["phase"] for h in st["history"]] == ["consulting", "awaiting_direction", "implementing", "awaiting_acceptance"]
    assert len(list((sc.run_dir / "logs").iterdir())) == 4


def test_status_is_read_only(consulted: Scenario, capsys) -> None:
    sc = consulted
    before = {p.name: p.stat().st_mtime_ns for p in sc.run_dir.iterdir() if p.is_file()}
    capsys.readouterr()
    assert sc.main(["status", "--run", str(sc.run_dir)]) == 0
    out = capsys.readouterr().out
    assert "awaiting_direction" in out and "design_review" in out and "questions.json" in out
    assert before == {p.name: p.stat().st_mtime_ns for p in sc.run_dir.iterdir() if p.is_file()}
    assert sc.main(["status", "--run", str(sc.tmp / "none")]) == 1


def test_design_file_is_never_deleted_and_run_dirs_remain(consulted: Scenario) -> None:
    sc = consulted
    sc.lv3a.plans["impl"] = [Lv3Plan(exit=10)]
    sc.implement()
    assert (sc.workdir / "docs/lv5a_design_x.md").is_file() and sc.run_dir.is_dir()
    report = (sc.run_dir / "final_report.md").read_text(encoding="utf-8")
    assert "docs/lv5a_design_x.md" in report and "削除しない" in report


# ---------------------------------------------------------------- 文面・解析の単体


def test_add_facts_variants() -> None:
    added = ltext.add_facts(BRIEF, ["追加A", "追加B"])
    facts = added.split("## 確認済みの事実", 1)[1].split("\n## ", 1)[0]
    assert "- 追加A" in facts and "- 追加B" in facts and "- strutil.py がある" in facts and "## 制約\nPython 3.12" in added
    tail_only = "# 依頼\n## 確認済みの事実\n- 元の事実\n"
    assert ltext.add_facts(tail_only, ["新"]).endswith("- 元の事実\n- 新\n")
    assert "## 確認済みの事実\n- 新" in ltext.add_facts("# 依頼\n", ["新"])


def test_same_title() -> None:
    assert ltext.same_title("同じ重大", "同じ 重大！".replace("！", "")) and ltext.same_title("エラー処理が抜けている", "エラー処理が抜けている。")
    assert not ltext.same_title("認証が抜けている", "ログの出力先が固定されている")


def test_technical_selection() -> None:
    clusters = [cl("a"), cl("b", "🟠"), cl("c", "🟡"), cl("d", "🔴", "見送り"), cl("e", "高", "採用", "critic")]
    assert [c["title"] for c in ltext.impl_targets(clusters)] == ["a", "b"]
    assert [c["title"] for c in ltext.fix_targets(clusters)] == ["a"]
    assert [c["title"] for c in ltext.technical(clusters, "🔴")] == ["a", "d"]


def test_g1_and_g2_questions_have_lv3a_shape() -> None:
    for q in (ltext.g1_question(), ltext.g2_question(True), ltext.g2_question(False)):
        labels = [o["label"] for o in q["options"]]
        assert len(labels) == 3 and len(set(labels)) == 3 and q["recommended"] in labels
        assert {"id", "question", "options", "recommended", "recommended_reason", "reason_ok"} <= set(q)
        assert all({"label", "description"} <= set(o) for o in q["options"])


LV0_REPORT_SAMPLE = """# Lv0 自動実行レポート — OK（全検査合格）
作業フォルダ: C:\\ClaudeCode\\.claude\\tools\\lv3a_build
基準の RUN: 20260920_090220（記録: {autodir}）

| 周 | RUN | 強度 | 秒 | Codex tokens | +行 | -行 | 失敗した検査 |
|---|---|---|---|---|---|---|---|
| 初回 | 20260920_090220 | medium | 440 | 99060 | 234 | 98 | - |
Codex 合計: 99,060 tokens / 440 秒 / 週の利用枠 0%

最終の検査: lf=ok / ruff=ok / help=ok / pytest=ok

変更: 更新 2 / 新規 1 / 削除 0  +234 -98 行（全周の合計）
  modified:tools/cgd_lv3a.py, modified:tools/tests/test_cgd_lv3a.py, added:tools/new.py …ほか 4 件
  注意(untracked_changed): tools/tests/test_cgd_lv3a_fixes.py（新規・中身に鍵らしき文字列・差分に含めない）

凍結ファイルの違反（写しから戻した）: なし
DeepSeek レビュー: 🔴7 🟠2 🟡2（全文: C:\\tmp-ai\\review_ds.txt）

次にすること: 変更一覧とスクリーンショットを確認
"""


def test_parse_lv0_real_report_shape(tmp_path: Path) -> None:
    autodir = tmp_path / "auto"
    write(autodir / "outcome.json", json.dumps({"status": "ok", "note": "", "base_run": "20260920_090220", "rounds": [
        {"index": 0, "run": "20260920_090220", "seconds": 440, "tokens": 99060, "plus": 234, "minus": 98, "codex_exit": 0}]}))
    write(autodir / "overall.patch", "--- a/x\n+++ b/x\n")
    run = lio.parse_lv0(CliResult(0, LV0_REPORT_SAMPLE.format(autodir=autodir)))
    assert (run.base_run, run.autodir, run.status, run.tokens, run.seconds) == ("20260920_090220", autodir, "ok", 99060, 440)
    assert run.counts == (2, 1, 0) and (run.plus, run.minus) == (234, 98)
    assert run.changed == ["tools/cgd_lv3a.py", "tools/tests/test_cgd_lv3a.py", "tools/new.py"]
    assert run.checks == {"lf": "ok", "ruff": "ok", "help": "ok", "pytest": "ok"}
    assert run.review_line.startswith("🔴7") and run.frozen_violation == "" and run.patch == autodir / "overall.patch"
    assert lio.lv0_cost(run) == {"codex_calls": 1, "codex_tokens": 99060, "ds_calls": 0, "ds_yen": 0.0, "ds_yen_unknown_calls": 1}


def test_parse_lv0_without_record_line_or_files(tmp_path: Path) -> None:
    run = lio.parse_lv0(CliResult(0, "# レポート\n変更: 更新 0 / 新規 0 / 削除 0  +0 -0 行\n"))
    assert run.autodir is None and run.rounds == [] and any("記録先" in w for w in run.warnings)
    quota = lio.parse_lv0(CliResult(11, "# Lv0 自動実行レポート — 利用枠で停止\n"))
    assert quota.autodir is None and quota.warnings == []  # 失敗時は記録先が無くて普通
    missing = tmp_path / "gone"
    run = lio.parse_lv0(CliResult(0, f"基準の RUN: 20260920_000001（記録: {missing}）\n"))
    assert run.autodir == missing and any("outcome.json" in w for w in run.warnings) and run.patch is None


def test_parse_lv3a_sample_shapes(tmp_path: Path) -> None:
    root = tmp_path / "root"
    rd = root / "x-design_20260920_091759_0c3a3f"
    write(rd / "run.json", json.dumps({"status": "success", "mapping": MAPPING, "costs": {"codex_calls": 3, "codex_tokens": 65441, "ds_calls": 3, "ds_yen": 3.09},
                                       "reviewers": {"ds_tech": {"attempts": [{"returncode": 0}, {"returncode": 0}]}, "codex_tech": {"attempts": [{}]}},
                                       "clusters": [cl("題")]}))
    write(rd / "questions.json", json.dumps([question()]))
    write(rd / "ds_tech.retry1.md", "本文\n```json\n" + json.dumps({"findings": [{"id": "DT1", "severity": "🔴", "headline": "再実行の見出し"}]}, ensure_ascii=False) + "\n```\n")
    write(rd / "ds_tech.md", "初回\n```json\n" + json.dumps({"findings": [{"id": "DT1", "severity": "🔴", "headline": "初回の見出し"}]}, ensure_ascii=False) + "\n```\n")
    run = lio.parse_lv3a(CliResult(0), root)
    assert run.ok and run.run_dir == rd and run.costs["codex_tokens"] == 65441 and len(run.questions) == 1
    assert run.finding("R2#1") == ("DeepSeek", "再実行の見出し")  # 採用した (再実行の) ログを指す
    assert run.finding("R1#1") == ("Codex", "")  # ログが無ければ見出しは空 (落ちない)
    assert run.finding("R9#1") == ("", "") and run.finding("R2#5") == ("DeepSeek", "")
    assert lio.lv3a_cost(run) == {"codex_calls": 3, "codex_tokens": 65441, "ds_calls": 3, "ds_yen": 3.09}
    failed = lio.parse_lv3a(CliResult(10), root)
    assert failed.questions == [] and failed.costs["ds_calls"] == 3 and not failed.ok
    assert lio.parse_lv3a(CliResult(0), tmp_path / "nothing").run_dir is None


@pytest.mark.parametrize("code,expected", [(0, 0), (1, 1), (2, 2), (3, 3), (10, 10), (11, 11), (30, 30), (5, 1), (124, 1)])
def test_map_lv0_exit(code: int, expected: int) -> None:
    assert lio.map_lv0_exit(code) == expected


@pytest.mark.parametrize("code,expected", [(0, 0), (20, 0), (1, 1), (2, 2), (10, 12), (12, 12), (13, 12), (11, 11), (30, 30), (5, 1)])
def test_map_lv3a_exit(code: int, expected: int) -> None:
    assert lio.map_lv3a_exit(code) == expected


# ---------------------------------------------------------------- 環境変数・タイムアウト・契約


def test_child_env_allowlist_and_prefixes() -> None:
    source = {"Path": "p", "SYSTEMROOT": "s", "PROGRAMFILES(X86)": "x", "DEEPSEEK_API_KEY": "d", "codex_home": "c", "CGD_SESSION": "g",
              "OPENAI_API_KEY": "no", "AWS_SECRET_ACCESS_KEY": "no", "GITHUB_TOKEN": "no", "DASHSCOPE_API_KEY": "no", "ANTHROPIC_API_KEY": "no",
              "MY_PASSWORD": "no", "NPM_TOKEN": "no"}
    env = lio.child_env(source)
    assert {"Path", "SYSTEMROOT", "PROGRAMFILES(X86)", "DEEPSEEK_API_KEY", "codex_home", "CGD_SESSION"} <= set(env)
    assert not {k for k in env if "no" == env[k]}
    assert env["PYTHONIOENCODING"] == "utf-8" and env["PYTHONUTF8"] == "1"


def test_run_cli_gives_child_only_allowed_env(tmp_path: Path, monkeypatch) -> None:
    script = write(tmp_path / "envdump.py", "import json, os, sys\nprint(json.dumps(sorted(os.environ)))\nprint('arg:' + ','.join(sys.argv[1:]))\n")
    for key, value in {"OPENAI_API_KEY": "sk-secret", "GITHUB_TOKEN": "t", "AWS_SECRET_ACCESS_KEY": "a", "DEEPSEEK_API_KEY": "d", "CODEX_HOME": "c", "CGD_X": "1"}.items():
        monkeypatch.setenv(key, value)
    result = lio.run_cli(script, ["a", "b"], 60)
    assert result.returncode == 0 and "arg:a,b" in result.stdout
    names = set(json.loads(result.stdout.splitlines()[0]))
    assert {"DEEPSEEK_API_KEY", "CODEX_HOME", "CGD_X"} <= names and not names & {"OPENAI_API_KEY", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY"}


def test_run_cli_timeout_returns_30(tmp_path: Path) -> None:
    script = write(tmp_path / "sleeper.py", "import time\ntime.sleep(60)\n")
    result = lio.run_cli(script, [], 2)
    assert result.returncode == 30 and "打ち切った" in result.stderr and result.elapsed < 40


def test_run_cli_reports_child_exit_and_streams(tmp_path: Path) -> None:
    script = write(tmp_path / "exit7.py", "import sys\nprint('こんにちは')\nprint('えらー', file=sys.stderr)\nsys.exit(7)\n")
    result = lio.run_cli(script, [], 60)
    assert (result.returncode, result.stdout.strip(), result.stderr.strip()) == (7, "こんにちは", "えらー")


def test_timeout_exit_30_propagates_from_both_drivers(sc: Scenario) -> None:
    sc.lv0.plans["design"] = [Lv0Plan(exit=30, status="")]
    assert sc.consult() == 30 and sc.state()["phase"] == "consult_failed"


def test_timeout_in_implementation_and_review(consulted: Scenario) -> None:
    sc = consulted
    sc.lv0.plans["impl"] = [Lv0Plan(exit=30)]
    assert sc.implement() == 30 and sc.state()["phase"] == "implement_failed"


def test_default_timeouts_are_passed_to_runners(consulted: Scenario) -> None:
    seen: list[int] = []
    inner = consulted.drivers.lv0

    def spy(argv: list[str], timeout: int) -> CliResult:
        seen.append(timeout)
        return inner(argv, timeout)

    consulted.drivers = Drivers(spy, consulted.lv3a)
    consulted.implement()
    assert seen and all(t > 3600 for t in seen)  # Codex 1 周ぶん (3000 秒) より長い


SOURCES = [TOOLS / "cgd_lv5a.py", TOOLS / "cgd_lv5a_io.py", TOOLS / "cgd_lv5a_text.py"]


def test_no_driver_imports() -> None:
    """他ドライバの内部関数を import しない (関数名の推測で黙って別経路へ落ちた 2026-09-20 の実害の再発防止)。"""
    for path in SOURCES:
        source = path.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import|from)\s+cgd_(lv0|lv3a)\w*", source, re.MULTILINE), path.name
        assert "importlib" not in source and "__import__" not in source, path.name


def test_sources_never_delete_files() -> None:
    for path in SOURCES:
        source = path.read_text(encoding="utf-8")
        assert not re.search(r"\.unlink\(|rmtree|os\.remove|os\.rmdir|\.rmdir\(", source), path.name


def test_sources_are_lf_utf8() -> None:
    for path in SOURCES:
        data = path.read_bytes()
        assert b"\r" not in data and not data.startswith(b"#!"), path.name
        data.decode("utf-8")


def _probe(script: str, *args: str) -> tuple[int, str]:
    """実物の CLI を、存在しない入力で起動する。引数の解析は通り、入力の検査で止まる (何も実行・書込しない)。"""
    proc = subprocess.run([sys.executable, str(TOOLS / script), *args], capture_output=True, timeout=120,
                          env={**lio.child_env(), "PYTHONIOENCODING": "utf-8"}, stdin=subprocess.DEVNULL)
    return proc.returncode, proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")


def _contract_cases(tmp: Path) -> list[tuple[str, list[str], str]]:
    nope = {"dir": str(tmp / "no_dir"), "json": str(tmp / "no.json"), "txt": str(tmp / "no.txt"), "md": str(tmp / "no.md")}
    return [
        ("cgd_lv0_auto.py", ["plan", "--workdir", nope["dir"], "--checks", nope["json"], "--max-fix-rounds", "1", "--review", "deepseek"], "検査定義"),
        ("cgd_lv0_auto.py", ["run", "--workdir", nope["dir"], "--checks", nope["json"], "--spec", nope["txt"], "--effort", "medium",
                             "--max-fix-rounds", "1", "--timeout", "60", "--review", "deepseek"], "検査定義"),
        ("cgd_lv3a.py", ["plan", "--brief", nope["md"], "--files", nope["md"], "--no-ds"], "存在しません"),
        ("cgd_lv3a.py", ["run", "--brief", nope["md"], "--files", nope["md"], "--label", "x", "--work-root", str(tmp / "wr"),
                         "--effort", "medium", "--no-ds"], "存在しません"),
    ]


@pytest.mark.parametrize("index", range(4))
def test_cli_contract_with_real_drivers(tmp_path: Path, index: int) -> None:
    """呼ぶ CLI の引数が実物のパーサに通る (名前を推測しない)。引数エラーなら usage / unrecognized が出て、入力の検査には届かない。"""
    script, args, needle = _contract_cases(tmp_path)[index]
    code, out = _probe(script, *args)
    assert "unrecognized arguments" not in out and "usage:" not in out, f"{script} {args[0]}: {out[:300]}"
    assert needle in out and code == 1, f"{script} {args[0]}: exit={code} {out[:300]}"
    assert not (tmp_path / "wr").exists()


def test_redact_flag_exists_in_lv3a_when_implemented(tmp_path: Path) -> None:
    md = str(tmp_path / "no.md")
    _, out = _probe("cgd_lv3a.py", "run", "--brief", md, "--label", "x", "--work-root", str(tmp_path / "wr"), "--redact")
    if "unrecognized arguments" in out or "usage:" in out:
        pytest.skip("Lv3A に --redact が未実装 (別エージェントが実装中)。実装後にこの契約が効く")
    assert "存在しません" in out
    _, out = _probe("cgd_lv3a.py", "plan", "--brief", md, "--redact")
    assert "unrecognized arguments" not in out and "存在しません" in out


def test_lv0_auto_help_works() -> None:
    code, out = _probe("cgd_lv0_auto.py", "plan", "--help")
    assert code == 0 and "--workdir" in out


def test_real_cli_smoke_status_and_help() -> None:
    proc = subprocess.run([sys.executable, str(TOOLS / "cgd_lv5a.py"), "status", "--run", str(TOOLS / "no_such_run")], capture_output=True, timeout=60)
    assert proc.returncode == 1 and "state.json" in proc.stderr.decode("utf-8", "replace")
    assert subprocess.run([sys.executable, str(TOOLS / "cgd_lv5a.py"), "--help"], capture_output=True, timeout=60).returncode == 0


def test_fix_spec_requires_code_and_tests_to_match_the_design() -> None:
    """実走 (repo3): 自動修正の Codex が設計書だけを書き換え、コード・テストを変えず、再レビューに 🔴 が残った。"""
    spec = ltext.build_fix_spec("依頼文", "docs/d.md", ["- [🔴] 指摘の題名: 対応案"], ["tests/**"], [])
    assert "設計書だけの修正で済ませない" in spec
    assert "コード" in spec and "テスト" in spec
    assert "指摘の題名" in spec  # 直すべき指摘は従来どおり載る
