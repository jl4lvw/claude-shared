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
    """pv build を走らせ、prompts ディレクトリを返す。

    **本番の run 置き場 (C:/tmp-ai/pv) を絶対に触らせない。**
    最初にこれを書いたとき、存在しない環境変数 `PV_PLAN_DIR` を設定して
    「隔離したつもり」になり、テストが本番へ 3 件の run を作って
    未検証マーカーを撒いた (`pv_verify_reminder` が毎ターン催促する状態になった)。
    正しい変数は `PV_ROOT`。さらに `ROOT` は **import 時に確定する定数**なので、
    親プロセスの `P.run_dir()` を当てにすると本番のパスを見てしまう。
    子プロセスへ env を渡し、**親は tmp から直接組み立てる**のが唯一正しい。
    下の `_assert_production_untouched` で、それが守られているかを毎回検査する。
    """
    root = tmp_path / "pvroot"
    topic = tmp_path / "topic.txt"
    topic.write_text("【テーマ】テスト\n", encoding="utf-8", newline="")
    common = tmp_path / "common.py"
    common.write_text("# COMMON-MARKER\n", encoding="utf-8", newline="")

    env = {**os.environ, "PV_ROOT": str(root)}
    version = P.read_skill_version(P.SKILL_MD) or ""
    cmd = [sys.executable, str(PLAN_PY), "build", "--level", "3",
           "--topic-file", str(topic), "--run", run,
           "--skill-version", version,
           "--attach", str(common), *extra]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)
    assert proc.returncode == 0, proc.stderr

    prompts = root / run / "prompts"
    assert prompts.is_dir(), (
        f"tmp 側に run が作られていない: {prompts}\n"
        "  PV_ROOT が効いていない = 本番へ書いた可能性がある"
    )
    return prompts


@pytest.fixture(autouse=True)
def _assert_production_untouched():
    """テストの前後で **本番の run 一覧が変わっていない**ことを確かめる。

    「隔離したつもり」を信じない。実際に増えていないかを見る。
    """
    def _snapshot() -> set[str]:
        return {p.name for p in P.ROOT.iterdir()} if P.ROOT.is_dir() else set()

    before = _snapshot()
    yield
    leaked = sorted(_snapshot() - before)
    assert not leaked, f"本番の run 置き場 ({P.ROOT}) に残骸を作った: {leaked}"


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
