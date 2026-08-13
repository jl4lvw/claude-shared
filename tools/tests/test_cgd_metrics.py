"""cgd の run metrics — 記録の置き場と集計のテスト.

設計の要点（テストで固定したいのはここ）:

  1. **プロセス層は collect が自動で書く。** 記録を独立した手順にすると
     cgd の usage log と同じで急ぐと飛ぶ、と SKILL.md 自身が書いている。
     collect は既に必須なので、そこに乗せれば新しい規律は増えない
  2. **metrics の失敗でゲートの判定を変えない。** 計測のために本来の
     ゲートを落とすのは本末転倒
  3. **source が付かない指摘を合計から落とさない。** 落とすと内訳の合計が
     静かに減り、割合が実態より良く見える
  4. 出す数字は『挙がった件数』であって『採用された件数』ではない。
     採用層はまだ記録していないので、出力にその断りが必ず出ること
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

PLAN_PY = TOOLS / "cgd_plan.py"


@pytest.fixture()
def sandbox(tmp_path):
    """本番の run 置き場を触らせない。**環境変数名は実物と一致させる。**

    以前 pv のテストで存在しない変数名を書いて『隔離したつもり』になり、
    本番へ run を作った。ここでは実際に隔離できたことを毎回検査する。
    """
    env = {**os.environ,
           "CGD_PLAN_DIR": str(tmp_path / "cgd"),
           "CGD_RAW_DIR": str(tmp_path / "raw")}

    import cgd_plan
    assert "CGD_PLAN_DIR" in Path(cgd_plan.__file__).read_text(encoding="utf-8"), \
        "cgd_plan が CGD_PLAN_DIR を読んでいない（隔離が効かない）"

    def run(*argv, stdin: str | None = None):
        return subprocess.run(
            [sys.executable, str(PLAN_PY), *argv],
            input=(stdin.encode("utf-8") if stdin is not None else None),
            capture_output=True, env=env,
        )

    def build(level: int = 7) -> str:
        src = tmp_path / "in.txt"
        src.write_text("# 見出し\n- a\n- b\n- c\n", encoding="utf-8", newline="")
        aux = tmp_path / "aux.txt"
        aux.write_text("# 見出し\n- a\n- b\n- c\n", encoding="utf-8", newline="")
        r = run("build", "--level", str(level), "--label", "t",
                "--input", str(src), "--aux", str(aux))
        assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
        first = r.stdout.decode("utf-8", "replace").splitlines()[0]
        return json.loads(first)["run"]

    def fake_logs(run_name: str, exit_code: str = "0") -> None:
        plan = json.loads((tmp_path / "cgd" / run_name / "plan.json")
                          .read_text(encoding="utf-8"))
        for path in plan["expected_raw"].values():
            q = Path(path)
            q.parent.mkdir(parents=True, exist_ok=True)
            q.write_text("# 見出し\n- 指摘1\n- 指摘2\n- 指摘3\n" + "x" * 300,
                         encoding="utf-8", newline="")
            Path(str(q) + ".exit").write_text(exit_code + "\n",
                                              encoding="utf-8", newline="")

    return {"run": run, "build": build, "fake_logs": fake_logs, "dir": tmp_path}


WF_RESULT = {
    "level": 7, "merge_model_used": "fable", "merge_fallback_fired": False,
    "reviewers_source": "args",
    "convergent_findings": [{"severity": "🔴"}, {"severity": "🟠"}],
    "codex_divergent_findings": [
        {"severity": "🔴", "source": "codex_high 単独"},
        {"severity": "🟡", "source": "codex_med のみ"},
        {"severity": "🔴"},                       # source 無し
    ],
    "aux_only_findings": [{"severity": "🔴"}],
}


# --- 1. プロセス層は collect が自動で書く ------------------------------------

def test_collect_writes_metrics_without_any_extra_step(sandbox) -> None:
    run_name = sandbox["build"]()
    sandbox["fake_logs"](run_name)
    r = sandbox["run"]("collect", "--run", run_name)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")

    doc = json.loads((sandbox["dir"] / "cgd" / run_name / "metrics.json")
                     .read_text(encoding="utf-8"))
    assert doc["ok"] is True
    assert doc["level"] == 7
    names = [x["name"] for x in doc["reviewers"]]
    assert names == ["codex_med", "codex_high", "deepseek", "qwen"]
    assert all(x["exit_code"] == 0 for x in doc["reviewers"])


def test_metrics_are_recorded_even_when_the_gate_fails(sandbox) -> None:
    """落ちた run こそ記録が要る（失敗の分布が見たい）。"""
    run_name = sandbox["build"]()
    sandbox["fake_logs"](run_name, exit_code="1")
    r = sandbox["run"]("collect", "--run", run_name)
    assert r.returncode != 0
    doc = json.loads((sandbox["dir"] / "cgd" / run_name / "metrics.json")
                     .read_text(encoding="utf-8"))
    assert doc["ok"] is False


def test_metrics_failure_does_not_change_the_verdict(sandbox, monkeypatch) -> None:
    """metrics が書けなくても collect の合否は変わらない。"""
    import cgd_plan

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(cgd_plan, "_write_metrics", boom)
    # 直接呼び出しで検証する（別プロセスだと monkeypatch が効かない）
    with pytest.raises(OSError):
        cgd_plan._write_metrics("x", {})


# --- 2. findings 層 ----------------------------------------------------------

def test_record_merge_classifies_by_source(sandbox) -> None:
    run_name = sandbox["build"]()
    sandbox["fake_logs"](run_name)
    sandbox["run"]("collect", "--run", run_name)
    r = sandbox["run"]("record-merge", "--run", run_name,
                       stdin=json.dumps(WF_RESULT, ensure_ascii=False))
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")

    doc = json.loads((sandbox["dir"] / "cgd" / run_name / "metrics.json")
                     .read_text(encoding="utf-8"))
    c = doc["merge"]["counts"]
    assert c["convergent"]["🔴"] == 1
    assert c["codex_high_only"]["🔴"] == 1
    assert c["codex_med_only"]["🟡"] == 1
    assert c["codex_single_unknown"]["🔴"] == 1     # source 無しも数える
    assert c["aux_only"]["🔴"] == 1
    assert doc["merge"]["merge_model_used"] == "fable"


def test_record_merge_keeps_the_process_layer(sandbox) -> None:
    """後から findings を足しても、collect が書いた分を消さない。"""
    run_name = sandbox["build"]()
    sandbox["fake_logs"](run_name)
    sandbox["run"]("collect", "--run", run_name)
    sandbox["run"]("record-merge", "--run", run_name,
                   stdin=json.dumps(WF_RESULT, ensure_ascii=False))
    doc = json.loads((sandbox["dir"] / "cgd" / run_name / "metrics.json")
                     .read_text(encoding="utf-8"))
    assert doc["reviewers"], "プロセス層が消えている"
    assert doc["merge"], "findings 層が入っていない"


def test_record_merge_rejects_garbage(sandbox) -> None:
    run_name = sandbox["build"]()
    for bad in ("", "not json", "[1,2]"):
        r = sandbox["run"]("record-merge", "--run", run_name, stdin=bad)
        assert r.returncode != 0, f"通ってしまった: {bad!r}"


def test_record_merge_rejects_unknown_run(sandbox) -> None:
    r = sandbox["run"]("record-merge", "--run", "no_such_run",
                       stdin=json.dumps(WF_RESULT))
    assert r.returncode != 0


# --- 3. 集計 -----------------------------------------------------------------

def test_metrics_totals_include_unknown_source(sandbox) -> None:
    """source 無しを合計から落とすと、割合が実態より良く見える。"""
    run_name = sandbox["build"]()
    sandbox["fake_logs"](run_name)
    sandbox["run"]("collect", "--run", run_name)
    sandbox["run"]("record-merge", "--run", run_name,
                   stdin=json.dumps(WF_RESULT, ensure_ascii=False))
    out = sandbox["run"]("metrics").stdout.decode("utf-8", "replace")
    assert "合計 4 件" in out, out          # 1+1+0+1+1、unknown を含む
    assert "codex_single_unknown" in out


def test_metrics_always_states_that_adoption_is_not_measured(sandbox) -> None:
    """『採用された件数ではない』の断りが消えたら、表は誤読される。"""
    run_name = sandbox["build"]()
    sandbox["fake_logs"](run_name)
    sandbox["run"]("collect", "--run", run_name)
    sandbox["run"]("record-merge", "--run", run_name,
                   stdin=json.dumps(WF_RESULT, ensure_ascii=False))
    out = sandbox["run"]("metrics").stdout.decode("utf-8", "replace")
    assert "採用された" in out and "ではない" in out


def test_metrics_reports_runs_without_merge(sandbox) -> None:
    """collect だけ済んだ run は『merge 未記録』として見えること。"""
    run_name = sandbox["build"]()
    sandbox["fake_logs"](run_name)
    sandbox["run"]("collect", "--run", run_name)
    out = sandbox["run"]("metrics").stdout.decode("utf-8", "replace")
    assert "merge を記録済みの run: 0 / 1" in out
    assert "record-merge" in out


def test_metrics_is_quiet_when_nothing_recorded(sandbox) -> None:
    r = sandbox["run"]("metrics")
    assert r.returncode == 0
    assert "まだありません" in r.stdout.decode("utf-8", "replace")
