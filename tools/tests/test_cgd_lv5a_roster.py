"""cgd_lv5a の roster (レビュアーの組) の透過のテスト。Codex・DeepSeek・Qwen・Lv0/Lv3A の実物は呼ばない (偽の実行器)。

守ること:
  - 設計レビュー・差分レビューの Lv3A 呼び出しに --roster を透過する (lv3 は今までの呼び出しのまま)
  - --effort は lv3 のときだけ Lv3A へ渡す (lv7/lv8 は組が強度を固定し、Lv3A が併用を拒否する)。Lv0 へは今までどおり
  - 🔴 の自動修正のあとの再レビューは軽い構成のまま (--roster を渡さない)
  - state.json に roster を記録し、implement は consult の値を使う (implement に --roster は無い)
  - Lv5A は Lv3A の内部関数を import しない (CLI とファイルだけ)。--no-qwen は --no-ds と同様に透過する
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import cgd_lv3a  # noqa: E402  (契約テストのためだけ。cgd_lv5a 本体は import しない)
import cgd_lv5a as target  # noqa: E402
import cgd_lv5a_io as lio  # noqa: E402
import cgd_lv5a_text as ltext  # noqa: E402
from cgd_lv5a_io import CliResult, Drivers  # noqa: E402
from test_cgd_lv5a import FakeLv3a, Lv3Plan, Scenario, cl, write  # noqa: E402

ROSTER_NAMES = {
    "lv3": ["codex_tech", "codex_crit", "ds_tech", "ds_crit"],
    "lv7": ["codex_tech", "codex_tech_high", "ds_tech", "qwen_tech"],
    "lv8": ["codex_tech", "codex_tech_high", "ds_tech", "qwen_tech", "codex_crit", "ds_crit"],
}
QWEN_COSTS = {"codex_calls": 3, "codex_tokens": 60000, "ds_calls": 1, "ds_yen": 0.5,
              "qwen_calls": 1, "qwen_tokens": 1500, "qwen_yen": 1.25, "qwen_cost_known": True}


def vendor_of(name: str) -> str:
    return "Codex" if name.startswith("codex") else "DeepSeek" if name.startswith("ds") else "Qwen"


class RosterLv3a(FakeLv3a):
    """FakeLv3a に、argv の --roster を反映した run.json (roster・mapping・reviewers.vendor・生ログ) を書かせる。"""

    def __init__(self) -> None:
        super().__init__()
        self.recorded: dict[str, str] = {}  # ラベルの末尾 (design / impl / impl2) → run.json に書く roster を上書き (不一致を作る)

    def __call__(self, argv: list[str], timeout: int) -> CliResult:
        result = super().__call__(argv, timeout)
        if argv[0] != "run" or result.returncode not in (0, 20):
            return result
        label = argv[argv.index("--label") + 1]
        run_dir = Path(argv[argv.index("--work-root") + 1]) / f"{label}_20260920_000000_abcdef"
        roster = argv[argv.index("--roster") + 1] if "--roster" in argv else "lv3"
        names = ROSTER_NAMES[roster]
        data = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        data["roster"] = self.recorded.get(label.rsplit("-", 1)[1], roster)
        data["mapping"] = {f"R{index}": name for index, name in enumerate(names, 1)}
        data["reviewers"] = {name: {"returncode": 0, "attempts": [{}], "vendor": vendor_of(name)} for name in names}
        write(run_dir / "run.json", json.dumps(data, ensure_ascii=False))
        for name in names:
            heads = [{"id": f"X{i}", "severity": "🔴", "headline": f"{name}の指摘{i}"} for i in (1, 2, 3)]
            write(run_dir / f"{name}.md", "本文" * 100 + "\n```json\n" + json.dumps({"findings": heads}, ensure_ascii=False) + "\n```\n")
        return result


class RosterScenario(Scenario):
    def __init__(self, tmp: Path) -> None:
        super().__init__(tmp)
        self.lv3a = RosterLv3a()
        self.drivers = Drivers(self.lv0, self.lv3a)

    def plan(self, *extra: str) -> int:
        return self.main(["plan", "--brief", str(self.brief), "--workdir", str(self.workdir), "--checks", str(self.checks),
                          "--files", str(self.ref), *extra])


@pytest.fixture()
def rs(tmp_path: Path) -> RosterScenario:
    return RosterScenario(tmp_path)


def consulted_with(rs: RosterScenario, *extra: str) -> RosterScenario:
    """consult まで進める。組に Qwen がいれば (lv7/lv8) Lv3A の費用に Qwen の項目が付く (実物と同じ)。"""
    roster = extra[extra.index("--roster") + 1] if "--roster" in extra else "lv3"
    costs = dict(QWEN_COSTS) if roster != "lv3" else {"codex_calls": 3, "codex_tokens": 60000, "ds_calls": 2, "ds_yen": 1.5}
    rs.lv3a.plans["design"] = [Lv3Plan(clusters=[cl("採用の重大", members=("R4#1",))], costs=costs)]
    assert rs.consult(*extra) == 20
    return rs


def flag_values(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, item in enumerate(argv) if item == flag]


# ------------------------------------------------------------------ plan


def test_plan_passes_roster_and_no_qwen_to_lv3a_and_shows_the_roster(rs: RosterScenario, capsys: pytest.CaptureFixture[str]) -> None:
    assert rs.plan("--roster", "lv7", "--no-qwen") == 0
    call = rs.lv3a.calls[-1]
    assert call == ["plan", "--brief", str(rs.brief), "--files", str(rs.ref), "--roster", "lv7", "--no-qwen"]
    out = capsys.readouterr().out
    assert "組: lv7（Codex medium+high・DeepSeek・Qwen の 技術 4 者" in out
    assert "再レビューは、軽い構成 = lv3・DeepSeek なし" in out
    assert "Qwen（Alibaba DashScope" not in out  # --no-qwen なら送信先に出さない
    assert not rs.work_root.exists()  # 何も書かない


def test_plan_lists_qwen_as_a_destination_for_lv7_and_lv8_only(rs: RosterScenario, capsys: pytest.CaptureFixture[str]) -> None:
    for roster, has_qwen in (("lv3", False), ("lv7", True), ("lv8", True)):
        assert rs.plan("--roster", roster) == 0
        out = capsys.readouterr().out
        assert (f"組: {ltext.ROSTER_LABELS[roster]}" in out) and (("Qwen（Alibaba DashScope" in out) == has_qwen), roster
    assert rs.plan("--roster", "lv8", "--no-ds") == 0
    assert "DeepSeek（中国本土サーバ）" not in capsys.readouterr().out.split("## 3.")[1]


def test_plan_default_roster_calls_lv3a_exactly_as_before(rs: RosterScenario) -> None:
    assert rs.plan() == 0
    assert rs.lv3a.calls[-1] == ["plan", "--brief", str(rs.brief), "--files", str(rs.ref)]  # --roster を足さない


# ------------------------------------------------------------------ consult


def test_consult_lv7_passes_roster_but_not_effort_to_the_design_review(rs: RosterScenario) -> None:
    consulted_with(rs, "--roster", "lv7", "--no-ds", "--no-qwen", "--effort", "high")
    review = rs.lv3a.runs("design")[0]
    assert flag_values(review, "--roster") == ["lv7"] and "--effort" not in review
    assert "--no-ds" in review and "--no-qwen" in review and "--redact" not in review
    design = rs.lv0.argvs("design")[0]
    assert flag_values(design, "--effort") == ["high"]  # Lv0 へは今までどおり
    assert flag_values(rs.lv3a.calls[0], "--roster") == ["lv7"] and "--no-qwen" in rs.lv3a.calls[0]  # consult 冒頭の plan も同じ


def test_consult_default_roster_calls_lv3a_exactly_as_before(rs: RosterScenario) -> None:
    consulted_with(rs, "--effort", "high", "--no-ds")
    review = rs.lv3a.runs("design")[0]
    assert "--roster" not in review and flag_values(review, "--effort") == ["high"] and "--no-qwen" not in review


@pytest.mark.parametrize("roster", ["lv3", "lv7", "lv8"])
def test_state_records_the_roster_and_the_lv3a_run_confirms_it(rs: RosterScenario, roster: str) -> None:
    consulted_with(rs, "--roster", roster)
    state = rs.state()
    assert state["roster"] == roster and state["args"]["roster"] == roster and state["args"]["no_qwen"] is False
    review_root = Path(state["consult"]["review_root"])
    lv3a_run = json.loads(next(review_root.iterdir()).joinpath("run.json").read_text(encoding="utf-8"))
    assert lv3a_run["roster"] == roster  # state.json と Lv3A の run.json の組が一致
    report = (rs.run_dir / "consult_report.md").read_text(encoding="utf-8")
    assert f"組: {ltext.ROSTER_LABELS[roster]}（設計レビューに適用）" in report
    assert len(report.splitlines()) <= 60


def test_consult_report_names_what_was_switched_off(rs: RosterScenario) -> None:
    consulted_with(rs, "--roster", "lv7", "--no-ds", "--no-qwen")
    assert "（DeepSeek・Qwen なし）（設計レビューに適用）" in (rs.run_dir / "consult_report.md").read_text(encoding="utf-8")
    assert rs.state()["args"]["no_qwen"] is True


def test_qwen_cost_of_the_review_is_summed_and_shown(rs: RosterScenario) -> None:
    consulted_with(rs, "--roster", "lv7")
    costs = rs.state()["costs"]
    assert (costs["qwen_calls"], costs["qwen_tokens"], costs["qwen_yen"], costs["qwen_unknown_runs"]) == (1, 1500, 1.25, 0)
    assert costs["codex_calls"] == 4 and costs["ds_calls"] == 1  # Lv0 の設計 1 回 + Lv3A の 3 回
    assert "Qwen: 1 回 / 1,500 tokens / ¥1.25" in (rs.run_dir / "consult_report.md").read_text(encoding="utf-8")


def test_lv3_costs_keep_their_shape_without_qwen_keys(rs: RosterScenario) -> None:
    rs.lv3a.plans["design"] = [Lv3Plan()]
    assert rs.consult() == 20
    assert set(rs.state()["costs"]) == {"codex_calls", "codex_tokens", "ds_calls", "ds_yen", "ds_yen_unknown_calls"}
    assert "Qwen" not in (rs.run_dir / "consult_report.md").read_text(encoding="utf-8").split("## 費用")[1]


def test_unknown_qwen_cost_is_marked_in_the_line(rs: RosterScenario) -> None:
    unknown = {**QWEN_COSTS, "qwen_cost_known": False}
    rs.lv3a.plans["design"] = [Lv3Plan(costs=unknown)]
    assert rs.consult("--roster", "lv7") == 20
    assert rs.state()["costs"]["qwen_unknown_runs"] == 1
    assert "費用は不明を 0 として数えた" in (rs.run_dir / "consult_report.md").read_text(encoding="utf-8")


def test_consult_stops_when_lv3a_ran_a_different_roster_than_requested(rs: RosterScenario, capsys: pytest.CaptureFixture[str]) -> None:
    rs.lv3a.recorded["design"] = "lv3"  # lv7 を頼んだのに Lv3A が lv3 で走った (別の組の結果を黙って使わない)
    assert rs.consult("--roster", "lv7") == 12
    state = rs.state()
    assert state["phase"] == "consult_failed" and "組 lv3 で走った（要求: lv7）" in state["failure"]["reason"]
    assert "組 lv3 で走った" in capsys.readouterr().err


def test_consult_stops_when_the_lv3a_run_has_no_roster_record_for_lv7(rs: RosterScenario) -> None:
    rs.lv3a.recorded["design"] = ""
    assert rs.consult("--roster", "lv7") == 12
    assert "(記録なし)" in rs.state()["failure"]["reason"]


def test_default_roster_accepts_an_lv3a_run_without_roster_record(rs: RosterScenario) -> None:
    """この機能より前の Lv3A の run.json には roster が無い。lv3 の要求なら、そのまま使える。"""
    rs.lv3a.recorded["design"] = ""
    assert rs.consult() == 20


def test_consult_rejects_an_unknown_roster_before_anything_runs(rs: RosterScenario) -> None:
    assert rs.consult("--roster", "lv9") == 1
    assert not rs.lv3a.calls and not rs.work_root.exists()


# ------------------------------------------------------------------ implement


def test_implement_uses_the_consult_roster_for_the_diff_review_and_a_light_re_review(rs: RosterScenario) -> None:
    consulted_with(rs, "--roster", "lv7", "--no-qwen")
    rs.lv3a.plans["impl"] = [Lv3Plan(clusters=[cl("直すべき重大", members=("R4#1",))], costs=QWEN_COSTS)]
    rs.lv3a.plans["impl2"] = [Lv3Plan(clusters=[])]
    assert rs.implement(None, "--effort", "high") == 0
    first = rs.lv3a.runs("impl")[0]
    assert flag_values(first, "--roster") == ["lv7"] and "--effort" not in first and "--no-qwen" in first  # 差分レビュー: 組を透過・強度は渡さない
    assert flag_values(rs.lv0.argvs("impl")[0], "--effort") == ["high"]  # Lv0 の実装へは今までどおり
    (second,) = rs.lv3a.runs("impl2")
    assert "--roster" not in second and "--no-qwen" not in second  # 再レビューは軽い構成のまま
    assert flag_values(second, "--effort") == ["high"] and "--no-ds" in second
    result = rs.state()["result"]
    assert result["autofix"]["rounds"] == 1 and result["review2"]["performed"] is True


def test_re_review_stays_light_for_lv8_too(rs: RosterScenario) -> None:
    consulted_with(rs, "--roster", "lv8")
    rs.lv3a.plans["impl"] = [Lv3Plan(clusters=[cl("直すべき重大")])]
    assert rs.implement() == 0
    assert flag_values(rs.lv3a.runs("impl")[0], "--roster") == ["lv8"]
    assert "--roster" not in rs.lv3a.runs("impl2")[0]


def test_implement_has_no_roster_option_and_rejects_it(rs: RosterScenario, capsys: pytest.CaptureFixture[str]) -> None:
    consulted_with(rs, "--roster", "lv7")
    assert rs.implement(None, "--roster", "lv8") == 1  # argparse が拒否 (implement は consult の値を使う)
    assert "unrecognized arguments" in capsys.readouterr().err
    assert not rs.lv3a.runs("impl")


def test_implement_default_roster_calls_lv3a_exactly_as_before(rs: RosterScenario) -> None:
    rs.lv3a.plans["design"] = [Lv3Plan()]
    assert rs.consult() == 20
    assert rs.implement() == 0
    review = rs.lv3a.runs("impl")[0]
    assert "--roster" not in review and "--no-qwen" not in review and flag_values(review, "--effort") == ["medium"]


def test_no_qwen_can_also_be_given_at_implement_time(rs: RosterScenario) -> None:
    consulted_with(rs, "--roster", "lv7")
    assert rs.implement(None, "--no-qwen") == 0
    assert "--no-qwen" in rs.lv3a.runs("impl")[0]


def test_old_state_without_a_roster_still_implements_as_lv3(rs: RosterScenario) -> None:
    """この機能より前に consult 済みで、回答待ちのまま残っている run の state.json (roster も no_qwen も無い)。"""
    rs.lv3a.plans["design"] = [Lv3Plan()]
    assert rs.consult() == 20
    path = rs.run_dir / "state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state.pop("roster")
    state["args"].pop("roster")
    state["args"].pop("no_qwen")
    write(path, json.dumps(state, ensure_ascii=False))
    assert rs.implement() == 0
    review = rs.lv3a.runs("impl")[0]
    assert "--roster" not in review and "--no-qwen" not in review
    assert rs.main(["status", "--run", str(rs.run_dir)]) == 0


def test_diff_review_with_a_wrong_roster_is_reported_as_not_performed(rs: RosterScenario) -> None:
    consulted_with(rs, "--roster", "lv7")
    rs.lv3a.recorded["impl"] = "lv3"
    assert rs.implement() == 21  # 要ユーザー判断
    review = rs.state()["result"]["review1"]
    assert review["performed"] is False and "組 lv3 で走った（要求: lv7）" in review["reason"]
    assert "レビュー未実施" in (rs.run_dir / "final_report.md").read_text(encoding="utf-8")


def test_final_report_shows_the_roster_and_the_light_re_review(rs: RosterScenario) -> None:
    consulted_with(rs, "--roster", "lv7")
    rs.lv3a.plans["impl"] = [Lv3Plan(clusters=[cl("直すべき重大")], costs=QWEN_COSTS)]
    assert rs.implement() == 0
    report = (rs.run_dir / "final_report.md").read_text(encoding="utf-8")
    assert "（差分レビューに適用。🔴 の自動修正後の再レビューは軽い構成のまま）" in report
    assert len(report.splitlines()) <= 60
    assert "Qwen: 2 回 / 3,000 tokens / ¥2.50" in report  # consult の設計レビュー 1 回 + 差分レビュー 1 回


def test_final_report_without_autofix_only_names_the_roster(rs: RosterScenario) -> None:
    consulted_with(rs, "--roster", "lv8")
    assert rs.implement() == 0
    report = (rs.run_dir / "final_report.md").read_text(encoding="utf-8")
    assert f"組: {ltext.ROSTER_LABELS['lv8']}（差分レビューに適用）" in report and "軽い構成" not in report


def test_impl_spec_names_the_qwen_vendor_of_a_finding(rs: RosterScenario) -> None:
    """設計レビューの指摘の出所 (R4#1 = qwen_tech) が「Qwen」と出る (名前の接頭辞の決め打ちで名前がそのまま出ない)。"""
    consulted_with(rs, "--roster", "lv7")
    assert rs.implement() == 0
    assert "R4#1 (Qwen): qwen_techの指摘1" in (rs.run_dir / "spec_impl.txt").read_text(encoding="utf-8")


def test_vendor_falls_back_to_the_reviewer_name_when_run_json_has_no_vendor(tmp_path: Path) -> None:
    run_dir = tmp_path / "wr" / "r"
    write(run_dir / "run.json", json.dumps({"status": "success", "mapping": {"R1": "qwen_tech", "R2": "codex_tech_high", "R3": "ds_tech"},
                                            "reviewers": {"qwen_tech": {"returncode": 0}}, "clusters": []}))
    write(run_dir / "questions.json", "[]")
    write(run_dir / "qwen_tech.md", "x\n```json\n" + json.dumps({"findings": [{"id": "QT1", "severity": "🟠", "headline": "見出し"}]}) + "\n```\n")
    run = lio.parse_lv3a(CliResult(0), tmp_path / "wr")
    assert run.roster == "" and run.vendors == {}
    assert run.finding("R1#1") == ("Qwen", "見出し")
    assert run.finding("R2#1")[0] == "Codex" and run.finding("R3#1")[0] == "DeepSeek"


def test_vendor_recorded_in_run_json_wins_over_a_guess_from_the_name(tmp_path: Path) -> None:
    """名前の接頭辞 (codex / ds / qwen) で決め打ちしない。run.json が出所のベンダーを持っているなら、それを使う。"""
    run_dir = tmp_path / "wr" / "r"
    write(run_dir / "run.json", json.dumps({"status": "success", "mapping": {"R1": "codex_odd", "R2": "brand_new_tech"}, "clusters": [],
                                            "reviewers": {"codex_odd": {"vendor": "Qwen"}, "brand_new_tech": {"vendor": "NewVendor"}}}))
    write(run_dir / "questions.json", "[]")
    run = lio.parse_lv3a(CliResult(0), tmp_path / "wr")
    assert run.vendors == {"codex_odd": "Qwen", "brand_new_tech": "NewVendor"}
    assert run.finding("R1#1")[0] == "Qwen" and run.finding("R2#1")[0] == "NewVendor"


# ------------------------------------------------------------------ 環境変数 (Qwen の鍵は Lv3A にだけ)


def test_qwen_env_reaches_lv3a_only_not_lv0(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dump = write(tmp_path / "envdump.py", "import json, os\nprint(json.dumps(sorted(os.environ)))\n")
    other = write(tmp_path / "other.py", "import json, os\nprint(json.dumps(sorted(os.environ)))\n")
    for key in ("DASHSCOPE_API_KEY", "QWEN_BASE_URL", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(key, "v")
    monkeypatch.setattr(lio, "LV3A_SCRIPT", dump)
    lv3a_names = set(json.loads(lio.run_cli(dump, [], 60).stdout))
    other_names = set(json.loads(lio.run_cli(other, [], 60).stdout))  # Lv0 など、Lv3A 以外
    assert {"DASHSCOPE_API_KEY", "QWEN_BASE_URL", "DEEPSEEK_API_KEY"} <= lv3a_names
    assert not lv3a_names & {"OPENAI_API_KEY", "ANTHROPIC_API_KEY"}
    assert "DEEPSEEK_API_KEY" in other_names and not other_names & {"DASHSCOPE_API_KEY", "QWEN_BASE_URL", "OPENAI_API_KEY"}


def test_default_child_env_still_excludes_qwen_keys() -> None:
    source = {"DASHSCOPE_API_KEY": "no", "QWEN_BASE_URL": "no", "DEEPSEEK_API_KEY": "yes"}
    assert set(lio.child_env(source)) == {"DEEPSEEK_API_KEY", "PYTHONIOENCODING", "PYTHONUTF8"}
    assert {"DASHSCOPE_API_KEY", "QWEN_BASE_URL"} <= set(lio.child_env(source, lio.LV3A_ENV_PREFIXES))


# ------------------------------------------------------------------ 契約 (Lv3A の CLI・定義との一致。Lv5A 本体は import しない)


def test_lv5a_roster_choices_match_lv3a() -> None:
    assert list(lio.ROSTERS) == list(cgd_lv3a.ROSTERS) and lio.DEFAULT_ROSTER == cgd_lv3a.DEFAULT_ROSTER
    assert set(ltext.ROSTER_LABELS) == set(cgd_lv3a.ROSTERS)
    parser = target.build_parser()
    for command in ("plan", "consult"):
        args = parser.parse_args([command, "--brief", "b", "--workdir", "w", "--checks", "c", *(["--label", "x"] if command == "consult" else [])])
        assert args.roster == "lv3" and args.no_qwen is False
    with pytest.raises(SystemExit):
        parser.parse_args(["plan", "--brief", "b", "--workdir", "w", "--checks", "c", "--roster", "lv9"])


def test_the_real_lv3a_cli_accepts_the_flags_lv5a_passes(tmp_path: Path) -> None:
    """Lv5A が渡す --roster / --no-qwen が、実物の Lv3A のパーサに通る (名前を推測しない)。引数エラーなら入力の検査に届かない。"""
    nope = str(tmp_path / "no.md")

    def probe(*args: str) -> tuple[int, str]:
        done = subprocess.run([sys.executable, str(TOOLS / "cgd_lv3a.py"), *args], capture_output=True, timeout=120,
                              env={**lio.child_env(), "PYTHONIOENCODING": "utf-8"}, stdin=subprocess.DEVNULL)
        return done.returncode, done.stdout.decode("utf-8", "replace") + done.stderr.decode("utf-8", "replace")

    for roster in lio.ROSTERS:
        for args in (("plan", "--brief", nope, "--roster", roster, "--no-ds", "--no-qwen"),
                     ("run", "--brief", nope, "--label", "x", "--work-root", str(tmp_path / "wr"), "--roster", roster, "--no-qwen")):
            code, out = probe(*args)
            assert "unrecognized arguments" not in out and "usage:" not in out and "存在しません" in out and code == 1, (args, out[:300])
    assert not (tmp_path / "wr").exists()
    # lv7/lv8 と --effort の併用は、実物の Lv3A が exit 1 で拒否する (Lv5A が lv7/lv8 で --effort を渡さない理由)
    code, out = probe("run", "--brief", nope, "--label", "x", "--work-root", str(tmp_path / "wr"), "--roster", "lv7", "--effort", "medium")
    assert code == 1 and "併用できません" in out and not (tmp_path / "wr").exists()
    code, out = probe("run", "--brief", nope, "--label", "x", "--work-root", str(tmp_path / "wr"), "--effort", "high")  # lv3 は従来どおり
    assert code == 1 and "存在しません" in out and "併用できません" not in out


def test_lv5a_sources_still_do_not_import_the_other_drivers() -> None:
    for name in ("cgd_lv5a.py", "cgd_lv5a_io.py", "cgd_lv5a_text.py"):
        source = (TOOLS / name).read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import|from)\s+cgd_(lv0|lv3a)\w*", source, re.MULTILINE), name
        assert "importlib" not in source and "__import__" not in source, name
        assert not re.search(r"""["'](qwen_advisor|deepseek_coder)""", source), name  # 外部 AI の CLI を直接は呼ばない (呼ぶのは Lv0/Lv3A の CLI だけ)


def test_re_review_reason_is_written_next_to_the_code() -> None:
    """再レビューを軽い構成のままにする理由が、コードのコメントに 1 行以上ある (SKILL にもある)。"""
    source = (TOOLS / "cgd_lv5a.py").read_text(encoding="utf-8")
    assert "再レビューは Codex 単独" in source and "軽い構成" in source


def test_lv0_plan_unaffected_by_roster(rs: RosterScenario) -> None:
    assert rs.plan("--roster", "lv8", "--no-qwen") == 0
    assert rs.lv0.calls[-1] == ["plan", "--workdir", str(rs.workdir), "--checks", str(rs.checks), "--review", "deepseek"]
