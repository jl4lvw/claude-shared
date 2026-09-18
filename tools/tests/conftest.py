"""tests 共通の fixture.

## cgd_session_env — 「今どのセッションの中で走っているか」をテストから切り離す

cgd のセッション判定 (`cgd_session.resolve_session`) は環境変数
`CLAUDE_SESSION_ID` / `CLAUDE_CODE_SESSION_ID` を読む。2026-08-28 に
所有者判定が fail closed になってから、テストの成否が**実行したシェル**で変わっていた:

  - Claude Code の中 (変数あり) … disarm のテストが「実セッション ≠ ゲートの所有者」で落ちる
  - 素のシェル・CI (変数なし) … build → collect が「所有者不明」で拒まれ 16 件余計に落ちる

どちらもコードは正しく、テストが実環境の変数を暗黙に使っていたのが原因
(2026-09-18 に切り分け)。この fixture は変数を固定値へ差し替える。
`monkeypatch.setenv` は `os.environ` 自体を書き換えるので、
`{**os.environ, ...}` で起動する subprocess にも同じ値が渡る。

autouse にしない。同じディレクトリの pv / hq 等のテストは cgd のセッションと無関係で、
黙って環境を変えると別の原因の失敗を隠しかねない。使うモジュールで明示する:

    pytestmark = pytest.mark.usefixtures("cgd_session_env")
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]

# ゲートの所有者として使う固定値。test_cgd_wf_gate の armed_gate もこれで張る
PYTEST_SESSION = "pytest-session"


def _session_env_vars() -> tuple[str, ...]:
    """cgd_session が読む変数名。名前を複製すると片方だけ増えたときに隔離が漏れる。"""
    if str(TOOLS) not in sys.path:
        sys.path.insert(0, str(TOOLS))
    import cgd_session  # noqa: PLC0415 — conftest の import 失敗で無関係なテストまで巻き込まない

    return cgd_session.ENV_VARS


@pytest.fixture()
def cgd_session_env(monkeypatch) -> str:
    """このテストを「セッション `pytest-session` の中」で走らせる。戻り値はその ID。"""
    for var in _session_env_vars():
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", PYTEST_SESSION)
    return PYTEST_SESSION


@pytest.fixture()
def no_session_env(monkeypatch, cgd_session_env) -> None:
    """セッションを特定できない環境 (素のシェル・CI) を再現する。

    cgd_session_env に依存させているのは実行順を固定するため。モジュール全体で
    cgd_session_env を使っていても、必ずその**後**に消す側が走る。
    """
    for var in _session_env_vars():
        monkeypatch.delenv(var, raising=False)
