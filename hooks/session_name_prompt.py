"""session_name_prompt.py — 新しいセッションの開始時に「セッション名を選ぶ」指示を注入する。

背景 (2026-09-21 ユーザー決定):
  セッション名は `072★写真アルバム 4` の書式で、ハンドオフでも通常でも共通の基本ルール。
  通常のセッションでは、トークが始まったら選択肢 (AskUserQuestion) で名前を選んでもらう。
  規則の本文は CLAUDE.md「セッション名の基本ルール」。本フックは **その存在を思い出させるだけ**
  (指示の重複記述を避け、端末記号だけをここで解決して渡す)。

聞くタイミング (2026-09-26 ユーザー決定):
  当初は「最初の作業より前」に聞いていたが、回答待ちの間に最初の依頼の処理が止まるため、
  「最初の依頼の作業が一段落した時点」へ変更した。途中で確認・承認を出すならその
  AskUserQuestion に同梱し、止まる回数を増やさない。

設計規約 (他のフックと同じ):
  - どの経路でも exit 0。フックがセッションを止めてはならない
  - SessionStart の startup だけで動く (resume / clear / compact では注入しない)
  - stdin は bytes で読んで UTF-8 decode
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover  # noqa: BLE001
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

try:
    import session_name
except Exception:  # noqa: BLE001  # pragma: no cover
    session_name = None  # type: ignore[assignment]


def build_context(symbol: str | None) -> str:
    sym = f"この端末の記号は {symbol}" if symbol else "この端末は対応表に無い (記号は聞いて決める)"
    return (
        f"[session-name] 新しいセッションです（{sym}）。"
        "CLAUDE.md「セッション名の基本ルール」に従い、AskUserQuestion でセッション名を選んでもらうこと。"
        "聞くのは最初の依頼の作業が一段落してから（名前のために作業を止めない）: "
        "途中で確認・承認を出すならその AskUserQuestion に名前の質問を同梱し、"
        "出さないなら最初の回答を返す直前に聞く。"
        "ただし次の場合は出さない: 最初の入力が /handoff load、非対話の実行"
        "（claude -p・サブエージェント・Workflow・自動実行）、ユーザーが名前を指定済み。"
    )


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace") or "{}")
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0
    # source (公式) または reason (旧フックが使っていた名前) が startup 以外なら何もしない。
    # どちらも無ければ startup とみなす (matcher 側でも startup に絞ってある)。
    kind = payload.get("source", payload.get("reason"))
    if kind is not None and kind != "startup":
        return 0
    symbol = session_name.detect_symbol() if session_name is not None else None
    out = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": build_context(symbol)}}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        sys.exit(0)
