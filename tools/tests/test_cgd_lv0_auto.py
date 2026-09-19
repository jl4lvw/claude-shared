"""cgd Lv0 自動実行ドライバ cgd_lv0_auto.py のテスト.

本物の Codex は呼ばない（run だけを差し替えた偽の Codex で、本物の prepare / diff・検査を通す）。
確かめること: 検査定義の検証・検査の実行（前段ゲート・アプリ起動・打ち切り）・出し直しの判断
（成功・上限・停滞・利用枠・Codex 失敗）・凍結ファイルの強制・レポートの大きさ。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import cgd_lv0_auto as auto  # noqa: E402
import cgd_lv0_codex as engine  # noqa: E402

PY = "python"
HAS_NODE = shutil.which("node") is not None
HAS_GIT = shutil.which("git") is not None


# ---------------------------------------------------------------- 準備


@pytest.fixture()
def runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "runs"
    base.mkdir()
    monkeypatch.setattr(engine, "RUNS_BASE", base)
    return base


def make_repo(tmp_path: Path) -> Path:
    """git リポジトリの中の app/ を作業フォルダにして返す（prepare がリポジトリ最上位を拒否するため）。"""
    repo = tmp_path / "repo"
    (repo / "app" / "tests").mkdir(parents=True)
    (repo / "app" / "hello.py").write_text("print('hi')\n", encoding="utf-8", newline="")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo / "app"


def cmd_check(name: str, code: str, **extra: object) -> dict:
    return {"name": name, "type": "cmd", "cmd": [PY, "-c", code], "timeout": 60, **extra}


def manifest_of(*checks: dict, frozen: list[str] | None = None) -> auto.Manifest:
    return auto.parse_manifest({"checks": list(checks), "frozen": frozen or []})


REQUIRE_FLAG = cmd_check("need-flag", "import sys,pathlib; sys.exit(0 if pathlib.Path('flag.txt').exists() else 1)")
COUNT_FILES = cmd_check("count", "import os,sys; print('files', len(os.listdir('.'))); sys.exit(1)")
ALWAYS_FAIL = cmd_check("never", "import sys; print('nope'); sys.exit(1)")


class FakeCodex(auto.Engine):
    """run だけを差し替える。steps は (workdir を書き換える関数, Codex の終了コード) の列。"""

    def __init__(self, steps: list[tuple[Callable[[Path], None], int]], weekly: list[float | None] | None = None) -> None:
        super().__init__(inprocess=True)
        self.steps, self.weekly, self.specs = steps, list(weekly or [10.0]), []

    def run(self, run: str, workdir: Path, spec: Path, effort: str, timeout: int) -> tuple[int, str]:
        step, code = self.steps[min(len(self.specs), len(self.steps) - 1)]
        self.specs.append(spec.read_text(encoding="utf-8"))
        step(workdir)
        rundir = self.rundir(run)
        (rundir / "run.json").write_text(json.dumps(
            {"exit": code, "seconds": 7, "tokens": 1234, "weekly_used_percent": 12.0}), encoding="utf-8")
        (rundir / "last.txt").write_text("最終報告", encoding="utf-8")
        return code, ""

    def weekly_used(self) -> float | None:
        return self.weekly.pop(0) if len(self.weekly) > 1 else self.weekly[0]


def writer(rel: str, text: str) -> Callable[[Path], None]:
    def step(workdir: Path) -> None:
        target = workdir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="")
    return step


def both(*steps: Callable[[Path], None]) -> Callable[[Path], None]:
    def step(workdir: Path) -> None:
        for s in steps:
            s(workdir)
    return step


def config(workdir: Path, manifest: auto.Manifest, **kw: object) -> auto.Config:
    return auto.Config(workdir=workdir, spec="元の仕様です", manifest=manifest, **kw)  # type: ignore[arg-type]


# ---------------------------------------------------------------- 検査定義


def test_parse_manifest_accepts_a_full_definition() -> None:
    m = auto.parse_manifest({
        "checks": [
            {"name": "js", "type": "js-syntax"},
            {"name": "e2e-smoke", "type": "e2e", "cmd": ["python", "tests/e2e/smoke.py"], "timeout": 90},
        ],
        "frozen": ["tests/**"],
        "serve": {"cmd": ["python", "main.py", "--port", "{port}"], "ready_path": "/health"},
    })
    assert [c.blocking for c in m.checks] == [True, False]  # 静的な検査は既定で前段ゲート
    assert m.checks[1].needs_serve is True and m.serve is not None and m.serve.ready_path == "/health"


@pytest.mark.parametrize("bad, message", [
    ([], "オブジェクト"),
    ({"checks": []}, "1 件以上"),
    ({"checks": [{"name": "a b", "type": "lf"}]}, "name"),
    ({"checks": [{"name": "a", "type": "lf"}, {"name": "a", "type": "lf"}]}, "重複"),
    ({"checks": [{"name": "a", "type": "nope"}]}, "type"),
    ({"checks": [{"name": "a", "type": "cmd"}]}, "cmd"),
    ({"checks": [{"name": "a", "type": "lf", "timeout": 0}]}, "timeout"),
    ({"checks": [{"name": "a", "type": "lf", "cwd": ".."}]}, "cwd"),
    ({"checks": [{"name": "a", "type": "e2e", "cmd": ["python", "x.py"]}]}, "serve"),
    ({"checks": [{"name": "a", "type": "lf"}], "extra": 1}, "未知"),
    ({"checks": [{"name": "a", "type": "lf"}], "frozen": [1]}, "frozen"),
])
def test_parse_manifest_rejects_bad_definitions(bad: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        auto.parse_manifest(bad)


def test_the_shipped_example_checks_file_is_valid() -> None:
    example = TOOLS.parent / "skills" / "lv0" / "checks.example.json"
    manifest = auto.load_manifest(example)
    assert {c.type for c in manifest.checks} >= {"js-syntax", "cmd", "e2e"} and manifest.serve is not None
    assert (TOOLS.parent / "skills" / "lv0" / "e2e_contract.md").is_file() and auto.CONTRACT_PATH.is_file()


def test_load_manifest_reports_unreadable_files(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="読めない"):
        auto.load_manifest(tmp_path / "missing.json")


# ---------------------------------------------------------------- 小さな道具


def test_subst_only_replaces_known_names() -> None:
    assert auto.subst("{port}-{other}-{python}", {"port": "1", "python": "py"}) == "1-{other}-py"


def test_expand_cmd_resolves_python_and_globs(tmp_path: Path) -> None:
    (tmp_path / "a.test.mjs").write_text("", encoding="utf-8")
    (tmp_path / "b.test.mjs").write_text("", encoding="utf-8")
    out = auto.expand_cmd(["python", "-m", "x", "*.test.mjs", "none-*.zzz"], {}, tmp_path)
    assert out == [sys.executable, "-m", "x", "a.test.mjs", "b.test.mjs", "none-*.zzz"]


def test_clean_env_drops_secret_looking_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    monkeypatch.setenv("MY_TOKEN", "x")
    monkeypatch.setenv("KEEP_ME", "1")
    env = auto.clean_env({"LV0_BASE_URL": "http://x"})
    assert "DEEPSEEK_API_KEY" not in env and "MY_TOKEN" not in env
    assert env["KEEP_ME"] == "1" and env["LV0_BASE_URL"] == "http://x" and env["PYTHONUTF8"] == "1"


def test_matches_frozen_star_crosses_directories() -> None:
    assert auto.matches_frozen("tests/e2e/smoke.py", ["tests/**"])
    assert auto.matches_frozen("tests/a.py", ["tests/*"])
    assert not auto.matches_frozen("app.js", ["tests/**"])


def test_tail_keeps_the_end() -> None:
    assert auto.tail("\n".join(str(i) for i in range(100)), 3) == "97\n98\n99"
    assert len(auto.tail("x" * 5000, 5, 100)) == 100


def test_failure_signature_ignores_durations_but_not_content() -> None:
    def res(text: str) -> list[auto.CheckResult]:
        return [auto.CheckResult("t", "cmd", "fail", excerpt=text)]
    assert auto.failure_signature(res("FAILED in 1.2s")) == auto.failure_signature(res("FAILED in 3.4s"))
    assert auto.failure_signature(res("FAILED a")) != auto.failure_signature(res("FAILED b"))


def test_within_rejects_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="外"):
        auto.within(tmp_path, "../x")


def test_render_fix_spec_carries_failures_frozen_and_original_spec() -> None:
    failed = [auto.CheckResult(f"c{i}", "cmd", "fail", excerpt=f"boom{i}") for i in range(5)]
    text = auto.render_fix_spec("元の仕様", failed, ["tests/**"], 2)
    assert "第 2 周" in text and "tests/**" in text and "【元の仕様】\n元の仕様" in text
    assert "boom0" in text and "boom2" in text and "boom3" not in text and "ほか 2 件" in text


# ---------------------------------------------------------------- 検査の実行


def test_run_command_times_out_and_reports() -> None:
    code, seconds, out = auto.run_command([sys.executable, "-c", "import time; time.sleep(30)"], Path.cwd(), auto.clean_env(), 1)
    assert code is None and "打ち切った" in out and seconds < 20


def test_run_command_reports_launch_failure(tmp_path: Path) -> None:
    code, _, out = auto.run_command([str(tmp_path / "no-such-exe")], tmp_path, auto.clean_env(), 5)
    assert code is None and "起動できない" in out


def test_run_checks_skips_dynamic_checks_after_a_blocking_failure(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("x = (\n", encoding="utf-8", newline="")
    manifest = manifest_of(
        {"name": "fail-first", "type": "cmd", "cmd": [PY, "-c", "import sys; sys.exit(1)"], "blocking": True},
        cmd_check("later", "print('never')"),
    )
    results = auto.run_checks(manifest, tmp_path, None, tmp_path / "log")
    assert [r.status for r in results] == ["fail", "skip"]


@pytest.mark.skipif(not HAS_NODE, reason="node が無い")
def test_js_syntax_check_finds_a_broken_file(tmp_path: Path) -> None:
    (tmp_path / "ok.js").write_text("const a = 1;\n", encoding="utf-8", newline="")
    (tmp_path / "bad.js").write_text("const = ;\n", encoding="utf-8", newline="")
    m = manifest_of({"name": "js", "type": "js-syntax"})
    (only_bad,) = auto.run_checks(m, tmp_path, ["bad.js"], tmp_path / "log1")
    (only_ok,) = auto.run_checks(m, tmp_path, ["ok.js"], tmp_path / "log2")
    (none,) = auto.run_checks(m, tmp_path, ["README.md"], tmp_path / "log3")
    assert (only_bad.status, only_ok.status, none.status) == ("fail", "ok", "skip")


def test_lf_check_flags_carriage_returns(tmp_path: Path) -> None:
    (tmp_path / "crlf.py").write_bytes(b"a = 1\r\nb = 2\r\n")
    (tmp_path / "lf.py").write_bytes(b"a = 1\n")
    m = manifest_of({"name": "lf", "type": "lf"})
    (bad,) = auto.run_checks(m, tmp_path, ["crlf.py"], tmp_path / "log1")
    (good,) = auto.run_checks(m, tmp_path, ["lf.py"], tmp_path / "log2")
    assert (bad.status, good.status) == ("fail", "ok")


def test_serve_starts_passes_env_and_stops(tmp_path: Path) -> None:
    probe = (
        "import os,sys,urllib.request;"
        "r=urllib.request.urlopen(os.environ['LV0_BASE_URL']+'/index.html',timeout=5);"
        "sys.exit(0 if r.status==200 else 1)"
    )
    (tmp_path / "index.html").write_text("<p>ok</p>", encoding="utf-8", newline="")
    m = auto.parse_manifest({
        "checks": [{"name": "e2e", "type": "e2e", "cmd": [PY, "-c", probe], "timeout": 60}],
        "serve": {"cmd": [PY, "-m", "http.server", "{port}", "--bind", "127.0.0.1"], "ready_path": "/index.html"},
    })
    captured: list[str] = []
    original = auto.ServeHandle.__enter__

    def spy(self: auto.ServeHandle) -> auto.ServeHandle:
        handle = original(self)
        captured.append(self.ctx["base_url"])
        return handle

    auto.ServeHandle.__enter__ = spy  # type: ignore[method-assign]
    try:
        (result,) = auto.run_checks(m, tmp_path, None, tmp_path / "log")
    finally:
        auto.ServeHandle.__enter__ = original  # type: ignore[method-assign]
    assert result.status == "ok", result.excerpt
    with pytest.raises((urllib.error.URLError, OSError)):  # 検査のあとアプリが止まっている
        urllib.request.urlopen(captured[0] + "/index.html", timeout=2)


def test_serve_that_exits_immediately_fails_the_e2e_check(tmp_path: Path) -> None:
    m = auto.parse_manifest({
        "checks": [{"name": "e2e", "type": "e2e", "cmd": [PY, "-c", "print(1)"]}],
        "serve": {"cmd": [PY, "-c", "import sys; print('boom'); sys.exit(3)"], "ready_timeout": 5},
    })
    (result,) = auto.run_checks(m, tmp_path, None, tmp_path / "log")
    assert result.status == "fail" and "終了した" in result.excerpt and "boom" in result.excerpt


# ---------------------------------------------------------------- 出し直しの判断


pytestmark_git = pytest.mark.skipif(not HAS_GIT, reason="git が無い")


@pytestmark_git
def test_execute_ok_on_the_first_round(tmp_path: Path, runs: Path) -> None:
    workdir = make_repo(tmp_path)
    api = FakeCodex([(writer("flag.txt", "1"), 0)])
    out = auto.execute(config(workdir, manifest_of(REQUIRE_FLAG)), api)
    assert out.status == "ok" and len(out.rounds) == 1 and len(api.specs) == 1
    assert out.overall["added"] == ["flag.txt"]
    assert auto.STATUS_EXIT[out.status] == 0


@pytestmark_git
def test_execute_fixes_on_the_second_round(tmp_path: Path, runs: Path) -> None:
    workdir = make_repo(tmp_path)
    api = FakeCodex([(writer("first.txt", "1"), 0), (writer("flag.txt", "1"), 0)])
    out = auto.execute(config(workdir, manifest_of(REQUIRE_FLAG)), api)
    assert out.status == "ok" and len(out.rounds) == 2
    assert out.rounds[0].failed == ["need-flag"] and out.rounds[1].failed == []
    assert "自動検査による修正依頼" in api.specs[1] and "need-flag" in api.specs[1] and "元の仕様です" in api.specs[1]
    assert sorted(out.overall["added"]) == ["first.txt", "flag.txt"]  # 全周の合計


@pytestmark_git
def test_execute_gives_up_after_the_round_limit(tmp_path: Path, runs: Path) -> None:
    workdir = make_repo(tmp_path)
    steps = [(writer(f"f{i}.txt", str(i)), 0) for i in range(3)]  # 検査の出力（ファイル数）が毎回変わる
    out = auto.execute(config(workdir, manifest_of(COUNT_FILES), max_fix_rounds=2), FakeCodex(steps))
    assert out.status == "checks_failed" and len(out.rounds) == 3


@pytestmark_git
def test_execute_stops_when_the_same_failure_repeats(tmp_path: Path, runs: Path) -> None:
    workdir = make_repo(tmp_path)
    steps = [(writer("x.txt", "1"), 0), (writer("y.txt", "1"), 0), (writer("z.txt", "1"), 0)]
    out = auto.execute(config(workdir, manifest_of(ALWAYS_FAIL), max_fix_rounds=3), FakeCodex(steps))
    assert out.status == "stalled" and len(out.rounds) == 2


@pytestmark_git
def test_execute_restores_frozen_files_tampered_in_a_fix_round(tmp_path: Path, runs: Path) -> None:
    workdir = make_repo(tmp_path)
    steps = [
        (writer("tests/test_x.py", "ORIGINAL"), 0),
        (both(writer("tests/test_x.py", "TAMPERED"), writer("flag.txt", "1")), 0),
    ]
    out = auto.execute(config(workdir, manifest_of(REQUIRE_FLAG, frozen=["tests/*"])), FakeCodex(steps))
    assert out.status == "ok"
    assert out.rounds[1].frozen_restored == ["tests/test_x.py"]
    assert (workdir / "tests" / "test_x.py").read_text(encoding="utf-8") == "ORIGINAL"
    assert "違反" in auto.render_report(out) and "tests/test_x.py" in auto.render_report(out)


@pytestmark_git
def test_execute_allows_new_frozen_pattern_files_and_first_round_tests(tmp_path: Path, runs: Path) -> None:
    workdir = make_repo(tmp_path)
    steps = [(both(writer("tests/test_a.py", "A"), writer("flag.txt", "1")), 0)]
    out = auto.execute(config(workdir, manifest_of(REQUIRE_FLAG, frozen=["tests/*"])), FakeCodex(steps))
    assert out.status == "ok" and out.rounds[0].frozen_restored == []


@pytestmark_git
def test_execute_protects_preexisting_frozen_files_from_the_first_round(tmp_path: Path, runs: Path) -> None:
    workdir = make_repo(tmp_path)
    (workdir / "tests" / "test_old.py").write_text("HUMAN", encoding="utf-8", newline="")
    steps = [(both(writer("tests/test_old.py", "CODEX"), writer("flag.txt", "1")), 0)]
    out = auto.execute(config(workdir, manifest_of(REQUIRE_FLAG, frozen=["tests/*"])), FakeCodex(steps))
    assert out.rounds[0].frozen_restored == ["tests/test_old.py"]
    assert (workdir / "tests" / "test_old.py").read_text(encoding="utf-8") == "HUMAN"


@pytestmark_git
def test_execute_reports_codex_failure_without_changes(tmp_path: Path, runs: Path) -> None:
    workdir = make_repo(tmp_path)
    out = auto.execute(config(workdir, manifest_of(REQUIRE_FLAG)), FakeCodex([(lambda w: None, 3)]))
    assert out.status == "codex_failed" and out.results == [] and out.questions == "最終報告"


@pytestmark_git
def test_execute_reports_no_change_on_the_first_round(tmp_path: Path, runs: Path) -> None:
    workdir = make_repo(tmp_path)
    out = auto.execute(config(workdir, manifest_of(REQUIRE_FLAG)), FakeCodex([(lambda w: None, 0)]))
    assert out.status == "no_change" and auto.STATUS_EXIT[out.status] == auto.EXIT_CODEX_FAILED


class SandboxNoiseCodex(FakeCodex):
    """エンジンは 3（読んだ文面の鍵語の誤検出）を返すが、Codex 自身は exit 0 で正常終了した場合。"""

    def run(self, run: str, workdir: Path, spec: Path, effort: str, timeout: int) -> tuple[int, str]:
        _code, text = super().run(run, workdir, spec, effort, timeout)
        path = self.rundir(run) / "run.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["sandbox_errors"] = 12
        path.write_text(json.dumps(data), encoding="utf-8")
        return engine.EXIT_CODEX_FAILED, text


@pytestmark_git
def test_execute_treats_marker_noise_as_success_when_codex_exited_zero(tmp_path: Path, runs: Path) -> None:
    workdir = make_repo(tmp_path)
    out = auto.execute(config(workdir, manifest_of(REQUIRE_FLAG)), SandboxNoiseCodex([(writer("flag.txt", "1"), 0)]))
    assert out.status == "ok"
    assert out.rounds[0].sandbox_warning.startswith("sandbox_errors=12")
    assert "注意（Codex の実行）" in auto.render_report(out)


@pytestmark_git
def test_execute_still_fails_on_marker_noise_when_nothing_changed(tmp_path: Path, runs: Path) -> None:
    workdir = make_repo(tmp_path)
    out = auto.execute(config(workdir, manifest_of(REQUIRE_FLAG)), SandboxNoiseCodex([(lambda w: None, 0)]))
    assert out.status == "codex_failed" and out.rounds[0].sandbox_warning == ""


@pytestmark_git
def test_execute_runs_checks_when_codex_fails_midway_but_does_not_loop(tmp_path: Path, runs: Path) -> None:
    workdir = make_repo(tmp_path)
    api = FakeCodex([(writer("first.txt", "1"), 1), (writer("flag.txt", "1"), 0)])
    out = auto.execute(config(workdir, manifest_of(REQUIRE_FLAG)), api)
    assert out.status == "codex_failed" and len(out.rounds) == 1 and out.rounds[0].failed == ["need-flag"]


@pytestmark_git
def test_execute_stops_before_starting_when_the_weekly_quota_is_high(tmp_path: Path, runs: Path) -> None:
    workdir = make_repo(tmp_path)
    api = FakeCodex([(writer("flag.txt", "1"), 0)], weekly=[85.0])
    out = auto.execute(config(workdir, manifest_of(REQUIRE_FLAG)), api)
    assert out.status == "quota_stop" and out.rounds == [] and api.specs == []


@pytestmark_git
def test_execute_stops_between_rounds_when_the_quota_runs_out(tmp_path: Path, runs: Path) -> None:
    workdir = make_repo(tmp_path)
    api = FakeCodex([(writer("first.txt", "1"), 0), (writer("flag.txt", "1"), 0)], weekly=[10.0, 90.0])
    out = auto.execute(config(workdir, manifest_of(REQUIRE_FLAG)), api)
    assert out.status == "quota_stop" and len(out.rounds) == 1 and len(api.specs) == 1


@pytestmark_git
def test_execute_zero_fix_rounds_never_retries(tmp_path: Path, runs: Path) -> None:
    workdir = make_repo(tmp_path)
    out = auto.execute(config(workdir, manifest_of(REQUIRE_FLAG), max_fix_rounds=0), FakeCodex([(writer("a.txt", "1"), 0)]))
    assert out.status == "checks_failed" and len(out.rounds) == 1


@pytestmark_git
def test_execute_appends_the_e2e_contract_only_when_given(tmp_path: Path, runs: Path) -> None:
    workdir = make_repo(tmp_path)
    api = FakeCodex([(writer("flag.txt", "1"), 0)])
    auto.execute(config(workdir, manifest_of(REQUIRE_FLAG), contract="【自動検査の約束】"), api)
    assert api.specs[0].startswith("元の仕様です") and api.specs[0].endswith("【自動検査の約束】")


def test_engine_new_run_ids_are_unique(runs: Path) -> None:
    api = auto.Engine(inprocess=True)
    first = api.new_run_id()
    api.rundir(first).mkdir()
    assert api.new_run_id() != first


# ---------------------------------------------------------------- レポート


@pytestmark_git
def test_report_stays_short_even_with_many_failures_and_files(tmp_path: Path, runs: Path) -> None:
    workdir = make_repo(tmp_path)
    many = both(*[writer(f"src/m{i}.txt", "x") for i in range(60)])
    noisy = cmd_check("noisy", "print('\\n'.join('line %d' % i for i in range(500))); import sys; sys.exit(1)")
    out = auto.execute(config(workdir, manifest_of(noisy), max_fix_rounds=0), FakeCodex([(many, 0)]))
    report = auto.render_report(out)
    assert len(report.splitlines()) <= 60, report
    assert "検査が通らない" in report and "line 499" in report and "ほか 30 件" in report


def test_report_for_quota_stop_without_rounds_is_minimal() -> None:
    out = auto.Outcome(status="quota_stop", note="週の利用枠が 85%", workdir="W")
    report = auto.render_report(out)
    assert "利用枠で停止" in report and "85%" in report and len(report.splitlines()) < 12


def test_cmd_plan_rejects_a_workdir_outside_a_repo(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    checks = tmp_path / "checks.json"
    checks.write_text(json.dumps({"checks": [{"name": "lf", "type": "lf"}]}), encoding="utf-8")
    code = auto.main(["plan", "--workdir", str(tmp_path), "--checks", str(checks)])
    assert code == auto.EXIT_GENERIC and "NG:" in capsys.readouterr().out


def test_cmd_plan_names_deepseek_only_when_asked(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    checks = tmp_path / "checks.json"
    checks.write_text(json.dumps({"checks": [{"name": "lf", "type": "lf"}]}), encoding="utf-8")
    auto.main(["plan", "--workdir", str(tmp_path), "--checks", str(checks)])
    assert "DeepSeek" not in capsys.readouterr().out
    auto.main(["plan", "--workdir", str(tmp_path), "--checks", str(checks), "--review", "deepseek"])
    assert "DeepSeek" in capsys.readouterr().out
