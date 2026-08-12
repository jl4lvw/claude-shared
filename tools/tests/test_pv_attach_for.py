"""`--attach-for` (担当限定の添付) のテスト.

INC-20260812-004007ace10d: 添付は全担当のプロンプトへ丸ごと複製される。
Lv3 なら同じ実体が 4 担当ぶん作られ、DeepSeek と Codex は外部へ実送信される
(162KB の添付で Codex 担当だけ約 14 万トークンに達した実測がある)。

「誰に何を見せるか」は仕様の判断なので**既定は変えない**。
ここで検証するのは 2 点だけ:

    1. `--attach-for` を使わなければ **依頼文が 1 バイトも変わらない**
    2. 使えば、指定した担当にだけ追加され、他の担当には入らない

加えて、担当 id の綴り間違いは**必ず落とす**。黙って無視すると
「絞ったつもりで全員に配っている」という最悪の勘違いが起きる。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import pv_plan as P  # noqa: E402

PLAN_PY = TOOLS / "pv_plan.py"


# --- parse_attach_for 単体 ---------------------------------------------------

def test_parses_task_and_path() -> None:
    got = P.parse_attach_for(["feasible:C:/tmp/impl.py"], {"feasible", "survey"})
    assert got == {"feasible": ["C:/tmp/impl.py"]}


def test_multiple_paths_for_one_task_accumulate() -> None:
    got = P.parse_attach_for(
        ["feasible:a.py", "feasible:b.py"], {"feasible"})
    assert got == {"feasible": ["a.py", "b.py"]}


def test_none_gives_empty_mapping() -> None:
    assert P.parse_attach_for(None, {"survey"}) == {}


def test_unknown_task_id_is_rejected() -> None:
    """綴り間違いを黙って捨てない。"""
    with pytest.raises(SystemExit) as exc:
        P.parse_attach_for(["feasable:x.py"], {"feasible"})
    assert "feasable" in str(exc.value)


def test_missing_separator_is_rejected() -> None:
    with pytest.raises(SystemExit):
        P.parse_attach_for(["justapath.py"], {"feasible"})


def test_bare_windows_path_is_rejected() -> None:
    """`--attach-for C:/x.py` を「担当 C」と誤読しない。"""
    with pytest.raises(SystemExit) as exc:
        P.parse_attach_for(["C:/tmp/x.py"], {"feasible"})
    assert "書式" in str(exc.value)


# --- build を通した振る舞い --------------------------------------------------

def _build(tmp_path: Path, run: str, extra: list[str]) -> Path:
    """pv build を走らせ、prompts ディレクトリを返す。"""
    topic = tmp_path / "topic.txt"
    topic.write_text("【テーマ】テスト\n", encoding="utf-8", newline="")
    common = tmp_path / "common.py"
    common.write_text("# COMMON-MARKER\n", encoding="utf-8", newline="")

    env = {**os.environ, "PV_PLAN_DIR": str(tmp_path / "runs")}
    version = P.read_skill_version(P.SKILL_MD) or ""
    cmd = [sys.executable, str(PLAN_PY), "build", "--level", "3",
           "--topic-file", str(topic), "--run", run,
           "--skill-version", version,
           "--attach", str(common), *extra]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)
    assert proc.returncode == 0, proc.stderr
    return Path(P.run_dir(run)) / "prompts"


@pytest.fixture(autouse=True)
def _isolated_runs(tmp_path, monkeypatch):
    """本番の run ディレクトリを汚さない。"""
    monkeypatch.setenv("PV_PLAN_DIR", str(tmp_path / "runs"))


def test_scoped_attachment_reaches_only_that_task(tmp_path: Path) -> None:
    only = tmp_path / "only.py"
    only.write_text("# ONLY-FOR-FEASIBLE\n", encoding="utf-8", newline="")
    prompts = _build(tmp_path, "scoped", ["--attach-for", f"feasible:{only}"])

    feasible = (prompts / "feasible.txt").read_text(encoding="utf-8")
    assert "ONLY-FOR-FEASIBLE" in feasible
    assert "COMMON-MARKER" in feasible, "共通の添付まで消えてはいけない"

    for other in ("survey", "counter", "outside"):
        body = (prompts / f"{other}.txt").read_text(encoding="utf-8")
        assert "ONLY-FOR-FEASIBLE" not in body, f"{other} にまで届いている"
        assert "COMMON-MARKER" in body


def test_default_path_is_unchanged(tmp_path: Path) -> None:
    """**--attach-for を使わなければ依頼文は 1 バイトも変わらない。**

    pv-token は run ごとに変わるので、その 1 行だけ落として比較する。
    """
    a = _build(tmp_path, "plain_a", [])
    b = _build(tmp_path, "plain_b", [])

    def _body(path: Path, run: str) -> str:
        # run ごとに変わるのは pv-token と raw のパスだけ。そこを均してから比べる。
        text = path.read_text(encoding="utf-8").replace(run, "<RUN>")
        return "\n".join(ln for ln in text.splitlines() if "[pv-token:" not in ln)

    for task in ("survey", "counter", "outside", "feasible", "merge"):
        assert _body(a / f"{task}.txt", "plain_a") == _body(b / f"{task}.txt", "plain_b"), task
