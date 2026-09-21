"""cgd_lv0_auto.py と Lv0A レビュー段の接続。実物の AI は呼ばない。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import cgd_lv0_auto as auto  # noqa: E402
import cgd_lv0_review as review  # noqa: E402
from cgd_lv5a_io import CliResult, Drivers, parse_lv0  # noqa: E402
from test_cgd_lv0_review import FakeLv0, Lv3Plan, cluster, section, write  # noqa: E402
from test_cgd_lv0_review import FakeLv3a as ReviewLv3a  # noqa: E402


class NoopLv0:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], timeout: int) -> CliResult:
        self.calls.append(list(argv))
        return CliResult(1, "", "呼ばない想定")


class FakeLv3a:
    def __init__(self, plan_exit: int = 0) -> None:
        self.plan_exit = plan_exit
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], timeout: int) -> CliResult:
        self.calls.append(list(argv))
        if argv[0] == "plan":
            return CliResult(self.plan_exit, "Lv3A plan OK\n", "失敗" if self.plan_exit else "")
        raise AssertionError("このテストではレビュー run を呼ばない")


def write_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    workdir = tmp_path / "repo" / "proj"
    workdir.mkdir(parents=True)
    checks = tmp_path / "checks.json"
    checks.write_text(json.dumps({"checks": [{"name": "lf", "type": "lf"}]}), encoding="utf-8", newline="")
    spec = tmp_path / "spec.txt"
    spec.write_text("仕様", encoding="utf-8", newline="")
    return workdir, checks, spec


@pytest.mark.parametrize("flag,value", [
    ("--roster", "lv7"), ("--no-ds", None), ("--no-qwen", None),
    ("--no-autofix", None), ("--lv3a-timeout", "20"),
])
def test_lv3a_only_options_are_rejected_elsewhere(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flag: str, value: str | None,
) -> None:
    workdir, checks, spec = write_inputs(tmp_path)
    argv = ["run", "--workdir", str(workdir), "--checks", str(checks), "--spec", str(spec), flag]
    if value is not None:
        argv.append(value)
    assert auto.main(argv) == 1
    assert f"NG: {flag} は --review lv3a のときだけ指定できる" in capsys.readouterr().out


def test_run_preflight_failure_does_not_start_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    workdir, checks, spec = write_inputs(tmp_path)
    monkeypatch.setattr(auto.engine, "RUNS_BASE", tmp_path / "runs")

    def forbidden(cfg: auto.Config, api: auto.Engine) -> auto.Outcome:
        raise AssertionError("preflight 失敗後に execute してはいけない")

    monkeypatch.setattr(auto, "execute", forbidden)
    drivers = Drivers(NoopLv0(), FakeLv3a(1))
    code = auto.main(["run", "--workdir", str(workdir), "--checks", str(checks), "--spec", str(spec),
                      "--review", "lv3a"], drivers=drivers)
    assert code == 1 and "実装を始めない" in capsys.readouterr().out


def test_run_non_ok_records_review_not_performed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workdir, checks, spec = write_inputs(tmp_path)
    monkeypatch.setattr(auto.engine, "RUNS_BASE", tmp_path / "runs")
    autodir = tmp_path / "auto"
    autodir.mkdir()

    def failed(cfg: auto.Config, api: auto.Engine) -> auto.Outcome:
        return auto.Outcome(status="checks_failed", workdir=str(cfg.workdir), base_run="R1", autodir=str(autodir))

    monkeypatch.setattr(auto, "execute", failed)
    drivers = Drivers(NoopLv0(), FakeLv3a())
    code = auto.main(["run", "--workdir", str(workdir), "--checks", str(checks), "--spec", str(spec),
                      "--review", "lv3a"], drivers=drivers)
    outcome = json.loads((autodir / "outcome.json").read_text(encoding="utf-8"))
    assert code == auto.EXIT_CHECKS_FAILED
    assert outcome["lv3a"] == {"performed": False, "reason": "実装が ok でないためレビューは未実施"}


def test_run_skips_small_review_and_keeps_exit_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir, checks, spec = write_inputs(tmp_path)
    autodir = tmp_path / "auto"
    autodir.mkdir()
    monkeypatch.setattr(auto.engine, "RUNS_BASE", tmp_path / "runs")

    def ok(cfg: auto.Config, api: auto.Engine) -> auto.Outcome:
        return auto.Outcome(status="ok", workdir=str(cfg.workdir), base_run="R1", autodir=str(autodir),
                            overall={"modified": ["a.py"], "added": [], "deleted": [], "plus": 99, "minus": 0})

    monkeypatch.setattr(auto, "execute", ok)
    lv3 = FakeLv3a()
    code = auto.main(["run", "--workdir", str(workdir), "--checks", str(checks), "--spec", str(spec),
                      "--review", "lv3a"], drivers=Drivers(NoopLv0(), lv3))
    outcome = json.loads((autodir / "outcome.json").read_text(encoding="utf-8"))
    assert code == 0 and "レビュー省略" in outcome["lv3a"]["reason"]
    assert len(lv3.calls) == 1 and lv3.calls[0][0] == "plan"


def test_plan_lv3a_prints_preflight_and_qwen_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    workdir, checks, _ = write_inputs(tmp_path)
    monkeypatch.setattr(auto.engine, "RUNS_BASE", tmp_path / "runs")
    monkeypatch.setattr(auto.engine, "validate_workdir", lambda path: [])
    monkeypatch.setattr(auto.engine, "candidate_bins", lambda: [])
    monkeypatch.setattr(auto.engine, "choose_bin", lambda bins: auto.engine.BinChoice(Path("codex.exe")))
    monkeypatch.setattr(auto.engine, "latest_rate_limits", lambda: [])
    monkeypatch.setattr(auto.engine, "scan_secrets", lambda path: [])
    monkeypatch.setattr(auto.engine, "excluded_dirs_present", lambda path: [])
    lv3 = FakeLv3a()
    code = auto.main(["plan", "--workdir", str(workdir), "--checks", str(checks),
                      "--review", "lv3a", "--roster", "lv7"], drivers=Drivers(NoopLv0(), lv3))
    output = capsys.readouterr().out
    assert code == 0 and "Qwen" in output and "Lv3A の点検" in output and "Lv3A plan OK" in output


def test_render_report_legacy_paths_are_unchanged_and_lazy() -> None:
    sys.modules.pop("cgd_lv0_review", None)
    out = auto.Outcome(status="ok", workdir="W", base_run="R", autodir="A")
    expected = ("# Lv0 自動実行レポート — OK（全検査合格）\n作業フォルダ: W\n基準の RUN: R（記録: A）\n"
                "\n凍結ファイルの違反（写しから戻した）: なし\n\n"
                "次にすること: 変更一覧とスクリーンショットを確認 → 画面はブラウザで要点だけ見る → 各プロジェクトの手順で反映\n")
    assert auto.render_report(out) == expected
    assert "cgd_lv0_review" not in sys.modules


def test_lv3a_report_is_bounded_with_many_findings() -> None:
    red = {"title": "重大" * 20, "adopt": "採用", "proposal": "直す" * 50}
    data = {
        "performed": True, "roster": "lv8",
        "bundles": [{"reds": [red] * 10, "oranges": [f"中{i}" for i in range(10)], "yellows": 7}],
        "autofix": {"rounds": 1, "targets": ["x"] * 10, "resolved": 0, "remaining": ["x"],
                    "recurred": ["x"], "stop_reason": "再レビューにも 🔴 が残る"},
        "unreviewed": ["huge.py"], "partial_notes": ["1 名欠落"], "cost_line": "Codex: 1 回",
        "record_dir": "records",
    }
    data["autofix"].update(base_run="20260921_020202", fix_plus=9, fix_minus=10, fix_files=["app/x.py"])
    data.update(needs_judgment=True, judgment_reasons=["🔴 が 1 件残る", "未レビューのファイルがある"])
    section = auto.render_lv3a_section(data)
    assert len(section) <= 25 and sum("[🔴]" in line for line in section) == 6
    assert any(line.startswith("- 要判断の理由: 🔴 が 1 件残る / 未レビューのファイルがある") for line in section)
    assert any("自動修正の変更: +9 -10 行（app/x.py）・基準 RUN 20260921_020202" in line for line in section)
    assert any(line.startswith("- 残った 🔴（再レビュー）: x") for line in section)
    out = auto.Outcome(status="review_needs_judgment", lv3a=data)
    assert "実装OK・レビューで要判断" in auto.render_report(out)


def test_new_report_is_still_parseable_by_lv5a(tmp_path: Path) -> None:
    autodir = tmp_path / "run"
    autodir.mkdir()
    out = auto.Outcome(
        status="ok", workdir="W", base_run="R", autodir=str(autodir),
        results=[auto.CheckResult("lf", "lf", "ok")],
        overall={"modified": ["a.py"], "added": [], "deleted": [], "plus": 5, "minus": 2},
        lv3a={"performed": False, "reason": "レビュー省略", "roster": "lv3", "bundles": [],
              "autofix": {}, "cost_line": "なし", "record_dir": "records"},
    )
    report = auto.render_report(out)
    (autodir / "report.md").write_text(report, encoding="utf-8", newline="")
    (autodir / "outcome.json").write_text(json.dumps({"status": "ok", "note": "", "base_run": "R", "rounds": []}),
                                           encoding="utf-8", newline="")
    parsed = parse_lv0(CliResult(0, report))
    assert parsed.base_run == "R" and parsed.checks == {"lf": "ok"}
    assert parsed.counts == (1, 0, 0) and (parsed.plus, parsed.minus) == (5, 2)


def ok_outcome(autodir: Path, plus: int = 200):
    """execute の代わり: 実装は成功し、レビュー段が読む記録 (全体の差分・仕様) だけを置く。"""

    def make(cfg: auto.Config, api: auto.Engine) -> auto.Outcome:
        write(autodir / "overall.patch", section("a.py", 80))
        write(autodir / "spec_r0.txt", "仕様")
        return auto.Outcome(status="ok", workdir=str(cfg.workdir), base_run="20260921_010101", autodir=str(autodir),
                            overall={"modified": ["a.py"], "added": [], "deleted": [], "untracked_changed": [],
                                     "notes": [], "plus": plus, "minus": 1})

    return make


def test_run_with_a_remaining_red_exits_21_and_reports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    workdir, checks, spec = write_inputs(tmp_path)
    autodir = tmp_path / "auto"
    monkeypatch.setattr(auto.engine, "RUNS_BASE", tmp_path / "runs")
    monkeypatch.setattr(auto, "execute", ok_outcome(autodir))
    lv3 = ReviewLv3a(tmp_path, [Lv3Plan(clusters=[cluster("重大")])])
    code = auto.main(["run", "--workdir", str(workdir), "--checks", str(checks), "--spec", str(spec),
                      "--review", "lv3a", "--no-autofix"], drivers=Drivers(FakeLv0(tmp_path), lv3))
    printed = capsys.readouterr().out
    outcome = json.loads((autodir / "outcome.json").read_text(encoding="utf-8"))
    assert code == auto.EXIT_NEEDS_JUDGMENT == 21
    assert outcome["status"] == "review_needs_judgment" and outcome["lv3a"]["needs_judgment"] is True
    assert "実装OK・レビューで要判断" in printed and "[🔴] 重大" in printed


def test_autofix_child_gets_an_absolute_checks_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir, _, spec = write_inputs(tmp_path)
    autodir = tmp_path / "auto"
    monkeypatch.chdir(tmp_path)  # 検査定義を相対パス (checks.json) で渡す
    monkeypatch.setattr(auto.engine, "RUNS_BASE", tmp_path / "runs")
    monkeypatch.setattr(auto, "execute", ok_outcome(autodir))
    lv0 = FakeLv0(tmp_path)
    lv3 = ReviewLv3a(tmp_path, [Lv3Plan(clusters=[cluster("重大")]), Lv3Plan()])
    code = auto.main(["run", "--workdir", str(workdir), "--checks", "checks.json", "--spec", str(spec),
                      "--review", "lv3a"], drivers=Drivers(lv0, lv3))
    argv = lv0.calls[0]
    assert code == 0 and Path(argv[argv.index("--checks") + 1]).is_absolute()


def test_plan_preflight_uses_dummy_brief_and_flags(tmp_path: Path) -> None:
    lv3 = FakeLv3a()
    result = review.preflight(tmp_path, "lv7", True, True, Drivers(NoopLv0(), lv3))
    assert result.returncode == 0
    argv = lv3.calls[0]
    brief = Path(argv[argv.index("--brief") + 1])
    assert "## 確認済みの事実" in brief.read_text(encoding="utf-8")
    assert ["--roster", "lv7"] == argv[argv.index("--roster"):argv.index("--roster") + 2]
    assert "--no-ds" in argv and "--no-qwen" in argv


@pytest.mark.skipif(not (TOOLS / "cgd_lv3a.py").is_file(), reason="cgd_lv3a.py がこの複製に無い")
@pytest.mark.parametrize("command", ["plan", "run"])
def test_real_lv3a_help_has_the_flags_we_pass(command: str) -> None:
    import subprocess  # noqa: PLC0415

    proc = subprocess.run([sys.executable, str(TOOLS / "cgd_lv3a.py"), command, "--help"],
                          capture_output=True, text=True, encoding="utf-8", timeout=30)
    text = proc.stdout + proc.stderr
    for flag in ("--brief", "--files", "--roster", "--no-ds", "--no-qwen"):
        assert flag in text
    if command == "run":
        for flag in ("--label", "--work-root", "--effort"):
            assert flag in text
