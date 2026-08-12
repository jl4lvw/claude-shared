"""collect の「旧形式パス」診断と、record_usage のゲート副作用ガードのテスト。

なぜ必要か（2026-08-12 に実際に踏んだ 2 件）:

1. WF に `args.reviewers` を渡さずに起動すると、WF は**内蔵のレビュアー定義**へ
   フォールバックし、生ログを旧形式 `cgd_raw_<name>_<label>_<tag>.md` に書く。
   これは `expected_raw_paths_v2` の docstring が明記している想定内の経路なのに、
   collect は新形式しか見ておらず「レビュアーが実際には走っていない可能性」と
   誤報していた。4 者が完走していても、である。
   **ゲートが狼少年になるのが一番まずい**ので、診断だけ正確にする（合格はさせない
   — 旧形式には .exit が無く、完走を機械的に確認できないため）。

2. `record_usage(level)` は Lv6/7/8 で inline codex 遮断ゲートを張る副作用がある。
   レース検証で level 7 を渡してしまい、**全セッションを止める全体ゲート**を
   張りかけた。テストから呼ぶ経路に逃げ道を用意する。

実行方法:
    python -m pytest .claude/tools/tests/test_cgd_builtin_fallback.py -v
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import cgd_plan  # noqa: E402
import cgd_usage_log  # noqa: E402

OK_BODY = "# レビュー結果\n\n- 指摘1\n- 指摘2\n- 指摘3\n" + "x" * 300


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(cgd_plan, "ROOT", tmp_path / "cgd")
    monkeypatch.setattr(cgd_plan, "RAW_DIR", tmp_path / "raw")
    (tmp_path / "raw").mkdir()
    inp = tmp_path / "in.txt"
    inp.write_bytes(b"input body" * 40)
    aux = tmp_path / "aux.txt"
    aux.write_bytes(b"aux body" * 40)
    return {"tmp": tmp_path, "input": inp, "aux": aux}


def _build(sandbox, level: int = 7, label: str = "t"):
    args = argparse.Namespace(
        level=level, label=label, input=str(sandbox["input"]),
        aux=str(sandbox["aux"]), include_gemini=False,
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert cgd_plan.cmd_build(args) == cgd_plan.EXIT_OK
    line = [l for l in buf.getvalue().splitlines() if l.startswith("{")][0]
    run = json.loads(line)["run"]
    plan = json.loads((sandbox["tmp"] / "cgd" / run / "plan.json").read_text(encoding="utf-8"))
    return run, plan


def _collect(run: str) -> tuple[int, dict]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cgd_plan.cmd_collect(argparse.Namespace(run=run))
    out = json.loads([l for l in buf.getvalue().splitlines() if l.startswith("{")][-1])
    return code, out


def _write_builtin_logs(sandbox, plan: dict) -> None:
    """WF 内蔵定義で走ったことにする（旧形式のパスにだけ生ログを置く・.exit なし）。"""
    for name in plan["expected_raw"]:
        p = sandbox["tmp"] / "raw" / f"cgd_raw_{name}_{plan['label']}_{plan['run_tag']}.md"
        p.write_text(OK_BODY, encoding="utf-8", newline="")


# ------------------------------------------- collect: 旧形式パスの診断


def test_collect_detects_builtin_path_logs(sandbox) -> None:
    """旧形式に生ログがあれば、それを見つけて理由に書く。"""
    run, plan = _build(sandbox)
    _write_builtin_logs(sandbox, plan)
    _code, out = _collect(run)
    assert out["fell_back_to_builtin"] == list(plan["expected_raw"])
    for r in out["reviewers"]:
        assert r["builtin_log"]["bytes"] > 200


def test_collect_stops_saying_reviewer_did_not_run(sandbox) -> None:
    """最重要の回帰項目: 完走しているのに「走っていない可能性」と言わない。"""
    run, plan = _build(sandbox)
    _write_builtin_logs(sandbox, plan)
    _code, out = _collect(run)
    for r in out["reviewers"]:
        assert "走っていない可能性" not in r["reason"]
        assert "旧形式のパスに生ログがある" in r["reason"]
        assert "完走は確認できない" in r["reason"]


def test_collect_still_fails_the_gate_for_builtin_logs(sandbox) -> None:
    """旧形式は .exit が無く完走を機械的に確認できないので**合格させない**。

    弱い証拠で通すと、このゲートを「非 LLM」たらしめている根拠が消える。
    """
    run, plan = _build(sandbox)
    _write_builtin_logs(sandbox, plan)
    code, out = _collect(run)
    assert code == cgd_plan.EXIT_NG
    assert out["ok"] is False
    assert cgd_plan.pending_path(run).exists()   # 印は残る


def test_collect_keeps_plain_not_run_message_when_nothing_exists(sandbox) -> None:
    """本当に何も無いときは従来どおり「走っていない可能性」と言う（見逃し防止）。"""
    run, _plan = _build(sandbox)
    code, out = _collect(run)
    assert code == cgd_plan.EXIT_NG
    assert out["fell_back_to_builtin"] == []
    assert all("走っていない可能性" in r["reason"] for r in out["reviewers"])


def test_collect_prefers_registered_path_over_builtin(sandbox) -> None:
    """正規の経路（.exit つき）が揃っていれば、旧形式を見に行かず合格する。"""
    run, plan = _build(sandbox)
    _write_builtin_logs(sandbox, plan)          # 旧形式にも置いておく
    for p in plan["expected_raw"].values():
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        Path(p).write_text(OK_BODY, encoding="utf-8", newline="")
        Path(p + ".exit").write_text("0", encoding="utf-8", newline="")
    code, out = _collect(run)
    assert code == cgd_plan.EXIT_OK
    assert out["ok"] is True
    assert out["fell_back_to_builtin"] == []


# ------------------------------------- record_usage: ゲート副作用のガード


def test_record_usage_does_not_arm_gate_when_disabled(tmp_path, monkeypatch) -> None:
    """arm_gate=False なら Lv7 でもゲートを張らない（テストからの誤爆防止）。"""
    called: list[int] = []
    monkeypatch.setattr(cgd_usage_log, "_arm_wf_gate",
                        lambda level, session=None: called.append(level))
    cgd_usage_log.record_usage(7, db_path=tmp_path / "u.sqlite3", arm_gate=False)
    assert called == []


def test_record_usage_arms_gate_by_default(tmp_path, monkeypatch) -> None:
    """既定では張る（cgd 本体の強制が効かなくなっては困る）。"""
    called: list[int] = []
    monkeypatch.setattr(cgd_usage_log, "_arm_wf_gate",
                        lambda level, session=None: called.append(level))
    cgd_usage_log.record_usage(7, db_path=tmp_path / "u.sqlite3")
    assert called == [7]


def test_env_kill_switch_skips_arming(monkeypatch, capsys) -> None:
    """CGD_NO_WF_GATE=1 は実際の _arm_wf_gate の中で効く（呼ばれても張らない）。"""
    monkeypatch.setenv("CGD_NO_WF_GATE", "1")
    # ゲートスクリプトを呼ぶ前に return するので、subprocess は起動しない
    monkeypatch.setattr(cgd_usage_log, "GATE_SCRIPT", Path("存在しないはずのパス"))
    cgd_usage_log._arm_wf_gate(7)
    assert "CGD_NO_WF_GATE=1" in capsys.readouterr().err


def test_arm_wf_gate_warns_when_session_is_omitted(monkeypatch, capsys) -> None:
    """セッション指定なしは全セッションを止めるので、黙って張らない。"""
    monkeypatch.delenv("CGD_NO_WF_GATE", raising=False)
    monkeypatch.setattr(cgd_usage_log, "GATE_SCRIPT", Path("存在しないはずのパス"))
    cgd_usage_log._arm_wf_gate(7)
    assert "セッション指定なし" in capsys.readouterr().err


def test_arm_wf_gate_is_silent_for_non_wf_levels(monkeypatch, capsys) -> None:
    """Lv2 等はそもそもゲート対象外なので警告も出さない。"""
    monkeypatch.setattr(cgd_usage_log, "GATE_SCRIPT", Path("存在しないはずのパス"))
    cgd_usage_log._arm_wf_gate(2)
    assert capsys.readouterr().err == ""
