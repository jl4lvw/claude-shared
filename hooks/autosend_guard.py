"""077 メール自動送信: Claude Code から承認の仕組みを触れないようにする PreToolUse フック。

止めるもの(事故防止。同じ PC 上の別プロセスまでは防げない):
- 080.メール自動送信(旧 077)/data(台帳・依頼・秘密鍵・設定)への Write/Edit と、ログ以外の Read
- data フォルダ・台帳・秘密鍵の名前を含む Bash/PowerShell コマンド
- LINE WORKS の callback 受け口(/lineworks/callback)を叩くコマンド
運用は `python cli.py ...` / `python run_watcher.py` 経由で行う(コマンドに data のパスが出ない)。
"""

from __future__ import annotations

import json
import re
import sys

# 2026-10-07 に 077 → 080 へ引っ越し(077.商品画像検索と番号が重なったため)。
# 旧フォルダには旧い台帳・秘密鍵が残るので、削除されるまで両方を守る
PROJECTS = ("080.メール自動送信", "077.メール自動送信")
READ_OK = re.compile(r"/data/(logs|rejected)/")
BASH_DENY = (
    re.compile("(?:" + "|".join(re.escape(p) for p in PROJECTS) + r")[\\/]+data", re.IGNORECASE),
    re.compile(r"ledger\.sqlite3", re.IGNORECASE),
    re.compile(r"secret\.key", re.IGNORECASE),
    re.compile(r"lw_session\.json", re.IGNORECASE),
    re.compile(r"/lineworks/callback", re.IGNORECASE),
    re.compile(r"X-WORKS-Signature", re.IGNORECASE),
)


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def decide(tool: str, tool_input: dict) -> dict | None:
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit", "Read"):
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        norm = path.replace("\\", "/")
        if any(p in norm for p in PROJECTS) and "/data/" in norm + "/":
            if tool == "Read" and READ_OK.search(norm):
                return None
            return _deny("077 の data(台帳・依頼・秘密鍵)は Claude から直接触れません。"
                         "状態は `python cli.py status`、ログは data/logs を Read してください。")
        return None
    if tool in ("Bash", "PowerShell"):
        cmd = str(tool_input.get("command") or "")
        for pat in BASH_DENY:
            if pat.search(cmd):
                return _deny("077 の承認の仕組み(台帳・秘密鍵・LINE WORKS 受け口)に触れる"
                             "コマンドは止めています。`python cli.py ...` を使ってください。")
    return None


def main() -> int:
    try:
        # Windows の既定(CP932)で読むと日本語パスが化けて判定をすり抜けるので UTF-8 で読む
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except ValueError:
        return 0
    result = decide(str(payload.get("tool_name", "")), payload.get("tool_input") or {})
    if result:
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
