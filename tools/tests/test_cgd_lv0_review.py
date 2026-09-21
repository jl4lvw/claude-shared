"""Lv0A のレビュー段。外部 AI は呼ばず、Drivers の偽物だけで分岐を確かめる。"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import cgd_lv0_review as review  # noqa: E402
import cgd_lv5a as lv5a  # noqa: E402 — フラグの契約比較だけで import してよい
from cgd_lv5a_io import CliResult, Drivers  # noqa: E402


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return path


def section(name: str, size: int) -> str:
    head = f"--- a/{name}\n+++ b/{name}\n"
    assert len(head.encode("utf-8")) < size
    return head + "x" * (size - len(head.encode("utf-8")) - 1) + "\n"


def cluster(title: str, severity: str = "🔴", adopt: str = "採用") -> dict[str, Any]:
    return {"id": title, "kind": "technical", "title": title, "members": ["R1#1"],
            "proposal": title + "を直す", "adopt": adopt, "severity": severity}


@dataclass
class Lv3Plan:
    exit: int = 0
    clusters: list[dict[str, Any]] = field(default_factory=list)
    roster: str | None = "lv3"
    stderr: str = ""
    partial: list[dict[str, str]] = field(default_factory=list)
    make_dir: bool = True


class FakeLv3a:
    def __init__(self, tmp_path: Path, plans: list[Lv3Plan] | None = None) -> None:
        self.tmp_path = tmp_path
        self.plans = list(plans or [])
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], timeout: int) -> CliResult:
        self.calls.append(list(argv))
        if argv[0] == "plan":
            return CliResult(0, "送信先: Codex / DeepSeek\n")
        plan = self.plans.pop(0) if self.plans else Lv3Plan()
        if plan.make_dir:
            root = Path(argv[argv.index("--work-root") + 1])
            run_dir = root / "fake_run"
            run_dir.mkdir(parents=True)
            body: dict[str, Any] = {
                "clusters": plan.clusters,
                "mapping": {"R1": "codex_tech"},
                "reviewers": {"codex_tech": {"vendor": "Codex", "attempts": [{}]}},
                "costs": {"codex_calls": 1, "codex_tokens": 300, "ds_calls": 1, "ds_yen": 0.1},
            }
            if plan.roster is not None:
                body["roster"] = plan.roster
            if plan.partial:
                body["partial"] = {"missing": plan.partial}
            write(run_dir / "run.json", json.dumps(body, ensure_ascii=False))
            write(run_dir / "questions.json", "[]")
            write(run_dir / "codex_tech.md", "```json\n{\"findings\":[{\"headline\":\"原文\"}]}\n```\n")
        return CliResult(plan.exit, "", plan.stderr)


@dataclass
class Lv0Plan:
    exit: int = 0
    patch: str | None = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a\n+b\n"


class FakeLv0:
    def __init__(self, tmp_path: Path, plans: list[Lv0Plan] | None = None) -> None:
        self.tmp_path = tmp_path
        self.plans = list(plans or [])
        self.calls: list[list[str]] = []
        self.count = 0

    def __call__(self, argv: list[str], timeout: int) -> CliResult:
        self.calls.append(list(argv))
        plan = self.plans.pop(0) if self.plans else Lv0Plan()
        self.count += 1
        autodir = self.tmp_path / f"fix_{self.count}"
        autodir.mkdir()
        run = f"20260921_00000{self.count}"
        rounds = [{"tokens": 120, "seconds": 2}]
        write(autodir / "outcome.json", json.dumps({"status": "ok" if plan.exit == 0 else "codex_failed",
                                                     "note": "", "base_run": run, "rounds": rounds}))
        if plan.patch is not None:
            write(autodir / "overall.patch", plan.patch)
        report = (f"# Lv0 自動実行レポート — OK\n作業フォルダ: x\n基準の RUN: {run}（記録: {autodir}）\n\n"
                  "最終の検査: lf=ok\n\n変更: 更新 1 / 新規 0 / 削除 0  +1 -1 行（全周の合計）\n"
                  "  modified:a.py\n\n凍結ファイルの違反（写しから戻した）: なし\n")
        write(autodir / "report.md", report)
        return CliResult(plan.exit, report)


def case(tmp_path: Path, patch: str | None = None, plus: int = 100) -> tuple[object, object]:
    autodir = tmp_path / "parent"
    patch_text = patch if patch is not None else section("a.py", 80)
    write(autodir / "overall.patch", patch_text)
    write(autodir / "spec_r0.txt", "仕様")
    cfg = SimpleNamespace(workdir=tmp_path / "work", spec="仕様", manifest=SimpleNamespace(frozen=["tests/**"]),
                          effort="medium", review_min_plus=100)
    out = SimpleNamespace(autodir=str(autodir), base_run="20260921_010101", rounds=[object()], results=[],
                          overall={"modified": ["a.py"], "added": [], "deleted": [], "untracked_changed": [],
                                   "notes": [], "plus": plus, "minus": 1})
    return cfg, out


def test_split_patch_boundaries_and_limits() -> None:
    exact = review.split_patch(section("exact.py", 50_000))
    assert len(exact.bundles) == 1 and exact.bundles[0].size == 50_000 and exact.unreviewed == []
    several = review.split_patch(section("a.py", 20_000) + section("b.py", 20_000) + section("c.py", 20_000))
    assert [b.files for b in several.bundles] == [["a.py", "b.py"], ["c.py"]]
    oversized = review.split_patch(section("huge.py", 50_001))
    assert oversized.bundles == [] and oversized.unreviewed == ["huge.py"]
    fourth = review.split_patch("".join(section(f"f{i}.py", 30_000) for i in range(4)))
    assert len(fourth.bundles) == 3 and fourth.unreviewed == ["f3.py"]
    assert review.split_patch("") == review.BundleSplit([], [])


@pytest.mark.parametrize("roster", ["lv3", "lv7", "lv8"])
@pytest.mark.parametrize("no_ds,no_qwen", [(False, False), (True, False), (False, True), (True, True)])
def test_flags_match_lv5a(roster: str, no_ds: bool, no_qwen: bool) -> None:
    assert review.roster_args(roster) == lv5a.roster_args(roster)
    assert review.flag_args(no_ds, no_qwen) == lv5a.flag_args(no_ds, no_qwen)
    assert review.review_flags(roster, "high", no_ds, no_qwen) == lv5a.review_flags(roster, "high", no_ds, no_qwen)
    assert review.rereview_flags("medium") == lv5a.rereview_flags("medium")


@pytest.mark.parametrize("plan,performed,word", [
    (Lv3Plan(), True, ""),
    (Lv3Plan(exit=20, partial=[{"name": "ds", "reason": "timeout"}]), True, "暫定成功"),
    (Lv3Plan(exit=10, stderr="failed"), False, "レビュー失敗"),
    (Lv3Plan(roster="lv7"), False, "要求: lv3"),
    (Lv3Plan(exit=1, stderr="秘匿情報候補", make_dir=False), False, "--redact"),
    (Lv3Plan(roster=None), True, ""),
])
def test_review_branches(tmp_path: Path, plan: Lv3Plan, performed: bool, word: str) -> None:
    cfg, out = case(tmp_path)
    lv3 = FakeLv3a(tmp_path, [plan])
    result = review.review_stage(cfg, out, checks_path="checks.json", no_autofix=True,
                                 drivers=Drivers(FakeLv0(tmp_path), lv3))
    assert result["bundles"][0]["performed"] is performed
    assert word in (result["bundles"][0]["reason"] + " ".join(result["partial_notes"]))
    assert result["needs_judgment"] is (not performed)


def test_executor_exception_is_recorded(tmp_path: Path) -> None:
    cfg, out = case(tmp_path)

    def boom(argv: list[str], timeout: int) -> CliResult:
        raise RuntimeError("broken")

    result = review.review_stage(cfg, out, checks_path="checks.json", drivers=Drivers(FakeLv0(tmp_path), boom))
    assert result["needs_judgment"] and "実行器が例外" in result["bundles"][0]["reason"]
    assert list((Path(out.autodir) / "review" / "logs").glob("*.txt"))


def test_review_is_skipped_below_threshold_without_judgment(tmp_path: Path) -> None:
    cfg, out = case(tmp_path, plus=99)
    lv3 = FakeLv3a(tmp_path)
    result = review.review_stage(cfg, out, checks_path="checks.json", drivers=Drivers(FakeLv0(tmp_path), lv3))
    assert not result["performed"] and "レビュー省略" in result["reason"] and not result["needs_judgment"]
    assert lv3.calls == []


def test_autofix_succeeds_and_rereview_resolves_red(tmp_path: Path) -> None:
    cfg, out = case(tmp_path)
    lv3 = FakeLv3a(tmp_path, [Lv3Plan(clusters=[cluster("重大")]), Lv3Plan()])
    lv0 = FakeLv0(tmp_path)
    result = review.review_stage(cfg, out, checks_path="checks.json", drivers=Drivers(lv0, lv3))
    assert result["autofix"]["rounds"] == 1 and result["autofix"]["resolved"] == 1
    assert result["autofix"]["remaining"] == [] and not result["needs_judgment"]
    assert result["autofix"]["fix_plus"] == 1 and result["autofix"]["fix_minus"] == 1
    assert result["autofix"]["fix_files"] == ["a.py"] and result["autofix"]["base_run"]
    assert len(lv0.calls) == 1 and len([a for a in lv3.calls if a[0] == "run"]) == 2
    argv = lv0.calls[0]
    assert argv[argv.index("--max-fix-rounds") + 1] == "1" and argv[argv.index("--review") + 1] == "none"


@pytest.mark.parametrize("fix_plan,needle", [
    (Lv0Plan(exit=3), "自動修正が失敗"),
    (Lv0Plan(patch=None), "差分なし"),
    (Lv0Plan(patch=section("large.py", 50_001)), "50,000"),
])
def test_autofix_stops_without_a_second_round(tmp_path: Path, fix_plan: Lv0Plan, needle: str) -> None:
    cfg, out = case(tmp_path)
    lv3 = FakeLv3a(tmp_path, [Lv3Plan(clusters=[cluster("重大")])])
    lv0 = FakeLv0(tmp_path, [fix_plan])
    result = review.review_stage(cfg, out, checks_path="checks.json", drivers=Drivers(lv0, lv3))
    assert needle in result["autofix"]["stop_reason"] and result["needs_judgment"]
    assert len(lv0.calls) == 1


def test_rereview_red_is_remaining_and_recurred(tmp_path: Path) -> None:
    cfg, out = case(tmp_path)
    lv3 = FakeLv3a(tmp_path, [Lv3Plan(clusters=[cluster("同じ重大")]), Lv3Plan(clusters=[cluster("同じ重大")])])
    result = review.review_stage(cfg, out, checks_path="checks.json", drivers=Drivers(FakeLv0(tmp_path), lv3))
    assert result["autofix"]["remaining"] == ["同じ重大"]
    assert result["autofix"]["recurred"] == ["同じ重大"] and result["needs_judgment"]


def test_declined_red_still_needs_judgment_and_says_why(tmp_path: Path) -> None:
    """統合者が見送った 🔴 も、Lv5A と同じく人の判断に回す（自動修正の対象ではないので子の Lv0 は呼ばない）。"""
    cfg, out = case(tmp_path)
    lv0 = FakeLv0(tmp_path)
    result = review.review_stage(cfg, out, checks_path="checks.json",
                                 drivers=Drivers(lv0, FakeLv3a(tmp_path, [Lv3Plan(clusters=[cluster("見送りの重大", adopt="見送り")])])))
    assert lv0.calls == [] and result["autofix"]["targets"] == [] and result["needs_judgment"]
    assert any("🔴 が 1 件残る" in r and "見送り等 1" in r for r in result["judgment_reasons"])
    clean = review.review_stage(*case(tmp_path / "clean"), checks_path="checks.json",
                                drivers=Drivers(FakeLv0(tmp_path), FakeLv3a(tmp_path)))
    assert not clean["needs_judgment"] and clean["judgment_reasons"] == []


def test_no_autofix_never_calls_child_lv0(tmp_path: Path) -> None:
    cfg, out = case(tmp_path)
    lv0 = FakeLv0(tmp_path)
    result = review.review_stage(cfg, out, checks_path="checks.json", no_autofix=True,
                                 drivers=Drivers(lv0, FakeLv3a(tmp_path, [Lv3Plan(clusters=[cluster("重大")])])))
    assert lv0.calls == [] and result["autofix"]["rounds"] == 0 and result["needs_judgment"]


def test_multi_bundle_review_marks_unreviewed_files_as_needing_judgment(tmp_path: Path) -> None:
    cfg, out = case(tmp_path, patch="".join(section(f"f{i}.py", 30_000) for i in range(4)))
    lv3 = FakeLv3a(tmp_path)
    result = review.review_stage(cfg, out, checks_path="checks.json", no_autofix=True,
                                 drivers=Drivers(FakeLv0(tmp_path), lv3))
    runs = [a for a in lv3.calls if a[0] == "run"]
    assert len(runs) == 3 and len({a[a.index("--files") + 1] for a in runs}) == 3
    assert [a[a.index("--label") + 1] for a in runs] == [f"lv0-20260921_010101-b{i}" for i in (1, 2, 3)]
    assert result["unreviewed"] == ["f3.py"] and result["needs_judgment"]
    assert "未レビューのファイルがある" in result["reason"]


def test_review_brief_tells_reviewers_about_files_without_diff(tmp_path: Path) -> None:
    cfg, out = case(tmp_path)
    out.overall["untracked_changed"] = [".env（新規・差分に含めない・5 bytes）"]
    out.overall["notes"] = ["blob.bin: バイナリのため差分なし（変化あり）"]
    review.review_stage(cfg, out, checks_path="checks.json", no_autofix=True,
                        drivers=Drivers(FakeLv0(tmp_path), FakeLv3a(tmp_path)))
    brief = (Path(out.autodir) / "review" / "brief_b1.md").read_text(encoding="utf-8")
    assert "## 確認済みの事実" in brief and "仕様" in brief and "第 1 束" in brief
    assert ".env" in brief and "blob.bin" in brief


def test_rereview_brief_describes_the_fix_run_not_the_first_run(tmp_path: Path) -> None:
    cfg, out = case(tmp_path)
    out.overall["modified"] = ["first_run_only.py"]
    lv3 = FakeLv3a(tmp_path, [Lv3Plan(clusters=[cluster("重大")]), Lv3Plan()])
    review.review_stage(cfg, out, checks_path="checks.json", drivers=Drivers(FakeLv0(tmp_path), lv3))
    brief = (Path(out.autodir) / "review" / "brief_rereview.md").read_text(encoding="utf-8")
    assert "a.py" in brief and "自動修正だけ" in brief and "重大" in brief
    assert "first_run_only.py" not in brief
    argv = [a for a in lv3.calls if a[0] == "run"][-1]
    assert argv[argv.index("--files") + 1].endswith("overall.patch") and "--no-ds" in argv


def test_static_contract() -> None:
    path = TOOLS / "cgd_lv0_review.py"
    source = path.read_text(encoding="utf-8")
    assert not re.search(r"^\s*(?:import|from)\s+cgd_(?:lv0|lv3a|lv5a)(?:\s|$)", source, re.MULTILINE)
    assert "importlib" not in source and "__import__" not in source
    assert not re.search(r"\.unlink\(|rmtree|os\.remove|os\.rmdir|\.rmdir\(", source)
    data = path.read_bytes()
    assert b"\r" not in data and not data.startswith(b"#!")
