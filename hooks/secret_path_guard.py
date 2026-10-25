"""secret_path_guard.py — TS のコマンドに署名する秘密鍵の置き場を、Claude から読み書きさせない PreToolUse hook。

2026-10-25: TS(PC-FF11)に承認つきでコマンドを実行させる仕組み(.codex-review/DESIGN.md §16)を作った。
TS は端末 A の見張り役が付けた署名だけを信じる。署名の秘密鍵は見張り役のデータ置き場
(900.ClaudeCode/a_browser_data/ts_command/)にあり、Claude がこれを読めば承認を経ずに署名できてしまう
(自己承認)。決まりで禁じるだけでなく、ツールの呼び出しを機械的に止める。

止めるもの: そのフォルダ・鍵のファイル名を、ファイル操作の引数またはコマンドの文字列に含む呼び出し。
シェルの迂回(変数で組み立てる等)までは防げない(skill_write_guard と同じ限界)。決まりと併せて守る。

fail-open: hook 自体の不具合で作業を止めない(このリポジトリ全 hook 共通の規約)。
"""
from __future__ import annotations

import json
import re
import sys

_FILE_TOOLS = frozenset({"Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Grep", "Glob"})
_SHELL_TOOLS = frozenset({"Bash", "PowerShell"})
_PATH_KEYS = ("file_path", "notebook_path", "path", "pattern", "glob")
# 区切り(\ /)と大小文字の違いを吸収して、鍵の置き場とファイル名を見つける
_SECRET = re.compile(r"a_browser_data[\\/]+ts_command|signing_key\.pem", re.IGNORECASE)


def is_secret(text: str) -> bool:
    return bool(_SECRET.search(text))


def handle_hook(payload: dict) -> dict | None:
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    if tool in _FILE_TOOLS:
        texts = [str(tool_input.get(k, "")) for k in _PATH_KEYS]
    elif tool in _SHELL_TOOLS:
        texts = [str(tool_input.get("command", ""))]
    else:
        return None
    hit = next((t for t in texts if t and is_secret(t)), None)
    if hit is None:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "[secret-path-guard] TS のコマンドに署名する秘密鍵の置き場"
                "(900.ClaudeCode/a_browser_data/ts_command/)は、Claude が読み書きしてはいけません。\n"
                "理由: 署名は承認の画面で承認されたものにだけ、見張り役が付けます。Claude が鍵に触れると"
                "承認を経ずに署名できてしまいます(自己承認の禁止・.codex-review/DESIGN.md §16)。\n"
                "→ TS でコマンドを実行したいときは `python -m ts_command.cli enqueue <script.ps1> --purpose ...` で"
                "申請し、承認の画面での承認を待ってください。"
            ),
        }
    }


def _run_hook() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        out = handle_hook(payload)
    except Exception:
        return 0
    if out is not None:
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(_run_hook())
