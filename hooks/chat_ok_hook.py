"""080 メール自動送信の chat 承認(チャットの返事で送る)を判定するフック(2026-10-24)。

    python chat_ok_hook.py stop     Stop: 最後の応答に確認カードの全文があれば「出した」と記録する
    python chat_ok_hook.py prompt   UserPromptSubmit: カードの後の最初の発言が承認の言葉かを判定する

判定は 080 の autosend/chat.py が行う。防ぐのは Claude の取り違えだけ(設計: 080/docs/chat_ok_approval_design.md)。
chat 承認が無効・このセッションに返事待ちが無いときは何もしない。失敗しても入力は止めず、承認もしない
(返事待ちは15分で承認の画面へ回る)。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT_080 = Path(__file__).resolve().parents[2] / "080.メール自動送信"


def _load():
    sys.path.insert(0, str(ROOT_080))
    from autosend import chat, config, ledger

    return chat, config, ledger


def handle(mode: str, payload: dict) -> dict | None:
    chat, config, ledger = _load()
    if not config.CONFIG_PATH.exists() or not config.load_config().chat_approval:
        return None
    if not config.LEDGER_PATH.exists():
        return None
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return None
    conn = ledger.connect(config.LEDGER_PATH)
    try:
        if not chat.has_open(conn, session_id):
            return None
        if mode == "stop":
            if payload.get("hook_event_name") != "Stop":
                return None                      # サブエージェントの終わり(SubagentStop)では記録しない
            msg = chat.mark_shown(conn, session_id, str(payload.get("last_assistant_message") or ""),
                                  len(payload.get("session_crons") or []), payload.get("prompt_id"))
            return {"systemMessage": msg} if msg else None
        msg = chat.decide_reply(conn, session_id, str(payload.get("prompt") or ""), payload.get("prompt_id"))
        if not msg:
            return None
        return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": msg}}
    finally:
        conn.close()


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in ("stop", "prompt"):
        return 0
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        out = handle(mode, payload)
    except Exception as exc:  # noqa: BLE001  入力を止めない(承認もしない)
        print(f"chat_ok_hook: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 0
    if out:
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
