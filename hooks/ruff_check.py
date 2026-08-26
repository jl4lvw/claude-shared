"""PostToolUse(Edit|Write) hook: 編集した.pyファイルへ即座にruff checkをかける。

デバッグ用途のトークン節約策の一つ。import漏れ・未定義名・構文エラー等の
機械的に検出できる不具合を、Claudeが後で気づく前にこのフックが先に拾う。
AGENTS.mdの既定ツールチェーン(ruff+black、mypyは含まれない)に合わせ、
ruffのみを対象とする。自動修正はせず、指摘の報告のみ(非破壊)。
fail-open: フック自体の失敗で本流を止めない。
"""

from __future__ import annotations

import json
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

MAX_OUTPUT_CHARS = 2000


def _emit(text: str) -> None:
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": text,
        }
    }
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    sys.stdout.flush()


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict) or payload.get("tool_name") not in ("Edit", "Write"):
        return 0

    file_path = (payload.get("tool_input") or {}).get("file_path")
    if not isinstance(file_path, str) or not file_path.endswith(".py"):
        return 0

    try:
        result = subprocess.run(
            ["ruff", "check", "--output-format=concise", file_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except Exception:  # noqa: BLE001 — ruff未検出等でも本流を止めない
        return 0

    issues = (result.stdout or "").strip()
    if not issues:
        return 0

    if len(issues) > MAX_OUTPUT_CHARS:
        issues = issues[:MAX_OUTPUT_CHARS] + "\n...(以下省略)"

    _emit(f"[ruff] {file_path} に指摘があります(自動修正はしていません):\n{issues}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
