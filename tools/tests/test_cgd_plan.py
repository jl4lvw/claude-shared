"""test_cgd_plan — cgd の run 登録と成果物判定のテスト.

なぜ必要か:
    cgd の Review 段は agent の自己申告（executed / findings / raw_log_path）で
    成否が決まる。生ログが 1 バイトも無くても「指摘なし」として通るため、
    pv の設計理念（成否の判定を Python に固定する）を持ち込んだのが cgd_plan.py。

    その判定そのものが壊れたら元の木阿弥なので、ここで固定する。

実行方法:
    python -m pytest .claude/tools/tests/test_cgd_plan.py -v
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import cgd_plan  # noqa: E402

PLAN_PY = TOOLS / "cgd_plan.py"

OK_BODY = ("# レビュー結果\n\n- 指摘1\n- 指摘2\n- 指摘3\n" + "x" * 300).encode("utf-8")


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """run と生ログの置き場を tmp に逃がす（本番の C:/tmp-ai を汚さない）。"""
    monkeypatch.setattr(cgd_plan, "ROOT", tmp_path / "cgd")
    monkeypatch.setattr(cgd_plan, "RAW_DIR", tmp_path / "raw")
    (tmp_path / "raw").mkdir()
    inp = tmp_path / "in.txt"
    inp.write_bytes(b"input body" * 40)
    aux = tmp_path / "aux.txt"
    aux.write_bytes(b"aux body" * 40)
    return {"tmp": tmp_path, "input": inp, "aux": aux}


def _build(sandbox, level: int = 8, label: str = "t", include_gemini: bool = False):
    import argparse
    args = argparse.Namespace(
        level=level, label=label, input=str(sandbox["input"]),
        aux=str(sandbox["aux"]) if level in (7, 8) else None,
        include_gemini=include_gemini,
    )
    # 「最後のディレクトリ」を取ると、後から作った run が辞書順で前に来たとき
    # 古い run を掴む。build が出力した run 名をそのまま使う。
    import contextlib, io  # noqa: PLC0415
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert cgd_plan.cmd_build(args) == cgd_plan.EXIT_OK
    run = json.loads([l for l in buf.getvalue().splitlines() if l.startswith("{")][0])["run"]
    plan = json.loads((sandbox["tmp"] / "cgd" / run / "plan.json").read_text(encoding="utf-8"))
    return run, plan


def _collect(run: str) -> int:
    import argparse
    return cgd_plan.cmd_collect(argparse.Namespace(run=run))


# ------------------------------------------------------------------- label


def test_label_sanitize_matches_workflow_rule() -> None:
    """WF 側 (cgd_lv*_review.js) と同じ規則であること。

    片方だけ変えると、期待する生ログのパスと実際のパスがずれて
    「レビュアーが走っていない」と誤判定する。
    """
    assert cgd_plan.sanitize_label("dryrun/check..1") == "dryrun_check..1"
    assert cgd_plan.sanitize_label("../../etc/passwd") == ".._.._etc_passwd"
    assert cgd_plan.sanitize_label("") == "target"
    assert len(cgd_plan.sanitize_label("a" * 200)) == 60


# ------------------------------------------------------------------- build


def test_build_run_tag_is_input_sha_prefix(sandbox) -> None:
    """run_tag は WF 側の _runTag（入力 sha256 の先頭8文字）と一致すること。"""
    run, plan = _build(sandbox)
    assert plan["run_tag"] == plan["input_sha256"][:8]


@pytest.mark.parametrize("level,expected", [
    (6, ["codex", "deepseek", "qwen"]),
    (7, ["codex_med", "codex_high", "deepseek", "qwen"]),
    (8, ["codex_med", "codex_high", "deepseek", "qwen", "codex_critic", "deepseek_critic"]),
])
def test_build_expects_all_reviewers(sandbox, level: int, expected: list[str]) -> None:
    _run, plan = _build(sandbox, level=level)
    assert list(plan["expected_raw"]) == expected


def test_build_inserts_gemini_when_opted_in(sandbox) -> None:
    _run, plan = _build(sandbox, level=8, include_gemini=True)
    names = list(plan["expected_raw"])
    assert "gemini" in names
    assert names.index("gemini") == 2, "SKILL.md の列順と揃っていない"


def test_build_writes_pending_marker(sandbox) -> None:
    run, _plan = _build(sandbox)
    assert (sandbox["tmp"] / "cgd" / run / cgd_plan.PENDING_NAME).exists()


def test_build_rejects_missing_input(sandbox) -> None:
    import argparse
    args = argparse.Namespace(level=8, label="t", input=str(sandbox["tmp"] / "nope.txt"),
                              aux=str(sandbox["aux"]), include_gemini=False)
    assert cgd_plan.cmd_build(args) == cgd_plan.EXIT_USAGE


def test_build_requires_aux_for_lv7_and_lv8(sandbox) -> None:
    import argparse
    args = argparse.Namespace(level=8, label="t", input=str(sandbox["input"]),
                              aux=None, include_gemini=False)
    assert cgd_plan.cmd_build(args) == cgd_plan.EXIT_USAGE


# ----------------------------------------------------------------- collect


def _write_raws(plan, body=OK_BODY, only=None, exit_code="0"):
    """生ログと .exit を書く。

    実運用では **シェルがリダイレクトで書く**（agent の Write ではない）。
    テストでもその形を再現する。exit_code=None なら .exit を書かない
    ＝「コマンドが最後まで到達しなかった」状況。
    """
    for name, p in plan["expected_raw"].items():
        if only is not None and name not in only:
            continue
        # 生ログは run ごとのディレクトリに置かれる。実運用ではラッパの
        # `mkdir -p "$(dirname ...)"` が作るので、テストでも同じように作る。
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        Path(p).write_bytes(body)
        if exit_code is not None:
            Path(p + ".exit").write_text(exit_code, encoding="utf-8")


def test_collect_fails_when_no_raw_logs(sandbox) -> None:
    """**これが本題**: レビュアーが実際には走っていない場合を捕まえる。"""
    run, _plan = _build(sandbox)
    assert _collect(run) == cgd_plan.EXIT_NG


def test_collect_fails_on_empty_log(sandbox) -> None:
    run, plan = _build(sandbox)
    _write_raws(plan)
    Path(list(plan["expected_raw"].values())[0]).write_bytes(b"")
    assert _collect(run) == cgd_plan.EXIT_NG


def test_collect_fails_on_short_log(sandbox) -> None:
    run, plan = _build(sandbox)
    _write_raws(plan)
    Path(list(plan["expected_raw"].values())[0]).write_bytes("短い".encode("utf-8"))
    assert _collect(run) == cgd_plan.EXIT_NG


def test_collect_fails_on_structureless_log(sandbox) -> None:
    """長いだけの散文は「答えた」とみなさない（降参文の垂れ流し対策）。"""
    run, plan = _build(sandbox)
    _write_raws(plan)
    Path(list(plan["expected_raw"].values())[0]).write_bytes(
        ("散文だけで構造がない。" * 40).encode("utf-8"))
    assert _collect(run) == cgd_plan.EXIT_NG


def test_collect_fails_when_one_reviewer_is_missing(sandbox) -> None:
    """1 人でも欠けたら通さない（Lv8 は全員必須）。"""
    run, plan = _build(sandbox)
    names = list(plan["expected_raw"])
    _write_raws(plan, only=names[:-1])
    assert _collect(run) == cgd_plan.EXIT_NG


def test_collect_succeeds_and_clears_marker(sandbox) -> None:
    run, plan = _build(sandbox)
    _write_raws(plan)
    pend = sandbox["tmp"] / "cgd" / run / cgd_plan.PENDING_NAME
    assert pend.exists()
    assert _collect(run) == cgd_plan.EXIT_OK
    assert not pend.exists(), "成功時に印が消えていない"


def test_collect_keeps_marker_on_failure(sandbox) -> None:
    """失敗したら印は残す（消えるのは成功したときだけ）。"""
    run, _plan = _build(sandbox)
    pend = sandbox["tmp"] / "cgd" / run / cgd_plan.PENDING_NAME
    assert _collect(run) == cgd_plan.EXIT_NG
    assert pend.exists()


def test_collect_fails_when_exit_file_missing(sandbox) -> None:
    """.exit が無い = コマンドが最後まで到達していない。

    生ログだけあって .exit が無い状態は「途中で死んだ」。
    ログの中身が立派でも通してはいけない。
    """
    run, plan = _build(sandbox)
    _write_raws(plan, exit_code=None)
    assert _collect(run) == cgd_plan.EXIT_NG


def test_collect_fails_on_nonzero_exit(sandbox) -> None:
    """**これが本題**: 成否は agent の申告ではなくシェルが書いた終了コードで決める。"""
    run, plan = _build(sandbox)
    _write_raws(plan)
    first = list(plan["expected_raw"].values())[0]
    Path(first + ".exit").write_text("1", encoding="utf-8")
    assert _collect(run) == cgd_plan.EXIT_NG


def test_collect_reports_exit_code(sandbox) -> None:
    import argparse, io, contextlib  # noqa: PLC0415
    run, plan = _build(sandbox)
    _write_raws(plan)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cgd_plan.cmd_collect(argparse.Namespace(run=run))
    payload = json.loads(buf.getvalue())
    assert all(r["exit_code"] == 0 for r in payload["reviewers"])


def test_wrap_writes_log_and_exit_via_shell(tmp_path) -> None:
    """ラッパが実際にシェルでログと終了コードを書くこと（机上で終わらせない）。

    `{ ...; }` だと中の `exit` でシェルごと死んで .exit が書かれない。
    サブシェルであることをここで固定する。
    """
    import cgd_reviewers  # noqa: PLC0415
    raw = (tmp_path / "r.md").as_posix()
    script = tmp_path / "s.sh"
    nl = chr(10)
    script.write_text(
        "#!/usr/bin/env bash" + nl
        + cgd_reviewers.wrap("echo hello ; exit 7", raw) + nl,
        encoding="utf-8", newline="",
    )
    subprocess.run(["bash", str(script)], capture_output=True)
    assert Path(raw).read_text(encoding="utf-8").strip() == "hello"
    assert Path(raw + ".exit").read_text(encoding="utf-8").strip() == "7"


@pytest.mark.parametrize("code,phrase", [
    ("124", "タイムアウト"),
    ("90", "作業ディレクトリ"),
    ("127", "コマンドが見つからない"),
])
def test_collect_explains_special_exit_codes(sandbox, code: str, phrase: str) -> None:
    """なぜ落ちたかが分からないと、環境の失敗とレビューの失敗を取り違える。"""
    import argparse, io, contextlib  # noqa: PLC0415
    run, plan = _build(sandbox)
    _write_raws(plan)
    first = list(plan["expected_raw"].values())[0]
    Path(first + ".exit").write_text(code, encoding="utf-8")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert cgd_plan.cmd_collect(argparse.Namespace(run=run)) == cgd_plan.EXIT_NG
    payload = json.loads(buf.getvalue())
    bad = [r for r in payload["reviewers"] if not r["ok"]][0]
    assert phrase in bad["reason"], bad["reason"]


def test_collect_declares_itself_authoritative(sandbox) -> None:
    """WF の executed は agent の転記。食い違ったら collect を採ると出力に明示する。"""
    import argparse, io, contextlib  # noqa: PLC0415
    run, plan = _build(sandbox)
    _write_raws(plan)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cgd_plan.cmd_collect(argparse.Namespace(run=run))
    payload = json.loads(buf.getvalue())
    assert "権威" in payload["authority"]
    assert payload["failed"] == []


# ------------------------------------------------- wrap（シェル側の実挙動）


@pytest.mark.parametrize("inner,want", [
    ("echo hello", "0"),
    ("echo out ; exit 7", "7"),
    ("no_such_cmd_zzz_2026", "127"),
    ("false | cat", "1"),          # pipefail が効いていること
])
def test_wrap_records_exit_code(tmp_path, inner: str, want: str) -> None:
    """成否を **シェルが** 書くこと。agent の申告に依存しない。"""
    import cgd_reviewers  # noqa: PLC0415
    raw = (tmp_path / "r.md").as_posix()
    script = tmp_path / "s.sh"
    nl = chr(10)
    script.write_text("#!/usr/bin/env bash" + nl + cgd_reviewers.wrap(inner, raw) + nl,
                      encoding="utf-8", newline="")
    subprocess.run(["bash", str(script)], capture_output=True)
    assert Path(raw + ".exit").read_text(encoding="utf-8").strip() == want


def test_wrap_records_124_when_killed(tmp_path) -> None:
    """打ち切られたら trap が初期値 124 を残す。

    これが無いと .exit が不在になり「走ったのか環境が壊れたのか」を区別できない。
    """
    import cgd_reviewers  # noqa: PLC0415
    raw = (tmp_path / "k.md").as_posix()
    script = tmp_path / "k.sh"
    nl = chr(10)
    script.write_text(
        "#!/usr/bin/env bash" + nl
        + cgd_reviewers.wrap("echo part ; kill -TERM $$", raw) + nl,
        encoding="utf-8", newline="")
    subprocess.run(["bash", str(script)], capture_output=True)
    assert Path(raw + ".exit").read_text(encoding="utf-8").strip() == "124"


def test_wrap_creates_parent_directory(tmp_path) -> None:
    """/c/tmp-ai 決め打ちをやめ、raw_path の親から作ること。"""
    import cgd_reviewers  # noqa: PLC0415
    raw = (tmp_path / "deep" / "nest" / "r.md").as_posix()
    script = tmp_path / "d.sh"
    nl = chr(10)
    script.write_text("#!/usr/bin/env bash" + nl + cgd_reviewers.wrap("echo x", raw) + nl,
                      encoding="utf-8", newline="")
    subprocess.run(["bash", str(script)], capture_output=True)
    assert Path(raw).read_text(encoding="utf-8").strip() == "x"
    assert Path(raw + ".exit").read_text(encoding="utf-8").strip() == "0"


def test_wrap_quotes_path_against_injection(tmp_path) -> None:
    """パスに $( ) が混ざってもコマンドとして実行されないこと。"""
    import cgd_reviewers  # noqa: PLC0415
    marker = tmp_path / "PWNED"
    raw = (tmp_path / f"a $(touch {marker.as_posix()}) b.md").as_posix()
    script = tmp_path / "i.sh"
    nl = chr(10)
    script.write_text("#!/usr/bin/env bash" + nl + cgd_reviewers.wrap("echo x", raw) + nl,
                      encoding="utf-8", newline="")
    subprocess.run(["bash", str(script)], capture_output=True)
    assert not marker.exists(), "パスの $( ) が実行されている"


def test_parallel_runs_do_not_share_raw_paths(sandbox) -> None:
    """同じ入力・同じ label で 2 回 build しても生ログを取り合わないこと。

    旧命名は `<label>_<入力の sha 先頭8>` だけで一意性を作っていたため、
    並行実行すると同じファイルに両方が書き込んでいた。
    """
    run_a, plan_a = _build(sandbox, label="same")
    run_b, plan_b = _build(sandbox, label="same")
    assert run_a != run_b
    a = set(plan_a["expected_raw"].values())
    b = set(plan_b["expected_raw"].values())
    assert not (a & b), f"生ログのパスが衝突している: {sorted(a & b)}"


def test_collect_without_plan_fails(sandbox) -> None:
    assert _collect("no_such_run") == cgd_plan.EXIT_NG


# --------------------------------------------------------------------- CLI


def test_cli_build_prints_workflow_args(sandbox) -> None:
    """args を手で組ませない（キー名取り違えが 4 回続いた事故への対処）。

    subprocess なので monkeypatch は効かない。環境変数で置き場を逃がさないと
    **テストが本番の run ディレクトリを汚す**（実際に 4 件作ってしまった）。
    """
    import os
    env = {**os.environ,
           "CGD_PLAN_DIR": str(sandbox["tmp"] / "cgd"),
           "CGD_RAW_DIR": str(sandbox["tmp"] / "raw")}
    r = subprocess.run(
        [sys.executable, str(PLAN_PY), "build", "--level", "8", "--label", "t",
         "--input", str(sandbox["input"]), "--aux", str(sandbox["aux"])],
        capture_output=True, text=True, encoding="utf-8", env=env,
    )
    assert r.returncode == 0
    line = [l for l in r.stdout.splitlines() if l.startswith("WORKFLOW_ARGS ")]
    assert line, "WORKFLOW_ARGS が出力されていない"
    payload = json.loads(line[0][len("WORKFLOW_ARGS "):])
    assert set(payload) == {"input_path", "aux_input_path", "label", "reviewers"}
    # reviewers は Python 側を単一の出所にするために載せている（2026-08-12）。
    assert [r["name"] for r in payload["reviewers"]] == cgd_plan.REVIEWERS[8]
    assert all(isinstance(r["timeout"], int) and r["timeout"] > 0 for r in payload["reviewers"])


# ------------------------------------------------------- WF との契約テスト
# cgd_plan は「WF はこのパスに生ログを書くはず」を **予測** して collect の判定に使う。
# 予測式が WF 側とずれると、**成功した run でも「生ログが存在しない」と誤報**する。
# 誤報する検査は「無視してよい警告」に成り下がり、いずれ本当の失敗も見逃す。
# ここで両者を突き合わせて、片方だけ変えたら落ちるようにする。

DUMP_MJS = TOOLS / "dump_wf_raw_paths.mjs"


def _wf_raw_paths(level: int, label: str, sha: str, include_gemini: bool = False) -> dict:
    # 内蔵定義を吸い出すのがこのヘルパの目的。2026-08-13 から reviewers 無しの起動は
    # 既定で halt するので、**明示的にオプトインする**必要がある。
    args = {"input_path": "C:/tmp-ai/a.txt", "label": label, "_sha256": sha,
            "allow_builtin": True}
    if level in (7, 8):
        args["aux_input_path"] = "C:/tmp-ai/b.txt"
    if include_gemini:
        args["include_gemini"] = True
    r = subprocess.run(
        ["node", str(DUMP_MJS), f"cgd_lv{level}_review.js", json.dumps(args)],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert r.returncode == 0, f"dump に失敗: {r.stderr}"
    return json.loads(r.stdout)["paths"]


@pytest.mark.parametrize("level", [6, 7, 8])
def test_expected_paths_match_what_the_workflow_writes(level: int) -> None:
    """Python の予測 == WF が agent に指示する保存先、であること。"""
    sha = "b" * 64
    label = "contract_test"
    predicted = cgd_plan.expected_raw_paths(level, label, sha[:8], include_gemini=False)
    actual = _wf_raw_paths(level, label, sha)
    assert predicted == actual, (
        "cgd_plan の予測と WF の指示がずれている。\n"
        f"  予測: {predicted}\n  実際: {actual}"
    )


@pytest.mark.parametrize("level", [6, 7, 8])
def test_expected_paths_match_with_gemini(level: int) -> None:
    """gemini をオプトインしても参加者リストと順序が一致すること。"""
    sha = "c" * 64
    predicted = cgd_plan.expected_raw_paths(level, "g_test", sha[:8], include_gemini=True)
    actual = _wf_raw_paths(level, "g_test", sha, include_gemini=True)
    assert set(predicted) == set(actual), (
        f"参加者が食い違う\n  予測: {sorted(predicted)}\n  実際: {sorted(actual)}"
    )
    assert predicted == actual


def _wf_cmds(level: int, include_gemini: bool = False) -> dict:
    # 同上: 内蔵定義の取り出しには allow_builtin が要る
    args = {"input_path": "C:/tmp-ai/a.txt", "label": "L", "wf_nonce": "NONCE",
            "allow_builtin": True}
    if level in (7, 8):
        args["aux_input_path"] = "C:/tmp-ai/b.txt"
    if include_gemini:
        args["include_gemini"] = True
    r = subprocess.run(
        ["node", str(DUMP_MJS), f"cgd_lv{level}_review.js", json.dumps(args)],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert r.returncode == 0, f"dump に失敗: {r.stderr}"
    return json.loads(r.stdout)["cmds"]


def _wf_timeouts(level: int, include_gemini: bool = False) -> dict:
    # 同上: 内蔵定義の取り出しには allow_builtin が要る
    args = {"input_path": "C:/tmp-ai/a.txt", "label": "L", "wf_nonce": "NONCE",
            "allow_builtin": True}
    if level in (7, 8):
        args["aux_input_path"] = "C:/tmp-ai/b.txt"
    if include_gemini:
        args["include_gemini"] = True
    r = subprocess.run(
        ["node", str(DUMP_MJS), f"cgd_lv{level}_review.js", json.dumps(args)],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert r.returncode == 0, f"dump に失敗: {r.stderr}"
    return json.loads(r.stdout)["timeouts"]


@pytest.mark.parametrize("level", [6, 7, 8])
@pytest.mark.parametrize("gemini", [False, True])
def test_python_reviewers_match_workflow_builtin(level: int, gemini: bool) -> None:
    """cgd_reviewers.py の定義 == WF 内蔵の reviewers、であること。

    同じ定義が 3 本の WF に複製されており、片方だけ直す事故を何度も踏んでいる。
    Python 側を単一の出所にする前提として、**両者が一致していること**を固定する。
    ここが落ちたら、どちらかを直したときにもう片方が取り残されている。
    """
    import cgd_reviewers  # noqa: PLC0415
    aux = "C:/tmp-ai/b.txt" if level in (7, 8) else None
    py = {r["name"]: r["cmd"].replace("__WF_NONCE__", "NONCE")
          for r in cgd_reviewers.build_reviewers(level, "C:/tmp-ai/a.txt", aux, gemini)}
    wf = _wf_cmds(level, gemini)
    assert set(py) == set(wf), f"参加者が不一致: {sorted(set(py) ^ set(wf))}"
    for name in py:
        assert py[name] == wf[name], (
            f"{name} のコマンドが食い違う\n  py: {py[name]}\n  wf: {wf[name]}"
        )

    # **cmd だけ比べていては足りない。** 実際に、別セッションが WF 側の timeout を
    # 180000 -> 600000 に上げたのに、cmd が同じだったのでこのテストが素通りした
    # (2026-08-12)。値を持つ項目は全部突き合わせる。
    py_to = {r["name"]: r["timeout"]
             for r in cgd_reviewers.build_reviewers(level, "C:/tmp-ai/a.txt", aux, gemini)}
    wf_to = _wf_timeouts(level, gemini)
    assert py_to == wf_to, f"timeout が食い違う\n  py: {py_to}\n  wf: {wf_to}"


@pytest.mark.parametrize("level", [6, 7, 8])
def test_python_reviewer_names_match_cgd_plan(level: int) -> None:
    """cgd_reviewers と cgd_plan.REVIEWERS が同じ参加者を指していること。"""
    import cgd_reviewers  # noqa: PLC0415
    assert cgd_reviewers.reviewer_names(level) == cgd_plan.REVIEWERS[level]


def test_label_sanitization_is_consistent_with_workflow() -> None:
    """label の正規化規則が WF 側と一致すること（片方だけ変えるとパスがずれる）。"""
    raw = "dry/run..1"
    sha = "d" * 64
    predicted = cgd_plan.expected_raw_paths(8, cgd_plan.sanitize_label(raw), sha[:8], False)
    actual = _wf_raw_paths(8, raw, sha)
    assert predicted == actual


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
