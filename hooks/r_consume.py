"""UserPromptSubmit hook: ユーザープロンプト末尾に `<!-- r-consume:NN -->`
タグがあれば RemoteInstructions API の consume を呼び、Claude に「自動取込
完了」のコンテキストを追加する。

仕様:
- stdin: Claude Code が JSON で {prompt, session_id, ...} を渡す
- 末尾 (改行・空白を許容) に `<!-- r-consume:(\\d{2,4}) -->` が見つかった時のみ動作
- POST http://127.0.0.1:8088/r/{code}/consume を叩く
- 200 (新規 consume) / 409 (他端末で先に consume 済) は同等に「成功」扱い
- 接続失敗・5xx は stderr に警告ログのみ (プロンプトは通過)
- タグ自体はプロンプトから除去できない (Claude Code hook 仕様) ため、Claude
  には additionalContext で「無視してよい」旨を伝える
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request

# Windows console は既定で CP932。JSON を ensure_ascii=False で出すと
# stdout pipe にも CP932 化けが乗ることがあるため UTF-8 を強制する。
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

_API = "http://127.0.0.1:8088/r"
_TAG_RE = re.compile(r"<!--\s*r-consume:(\d{2,4})\s*-->\s*\Z")
_TIMEOUT_SEC = 3.0


def _consume(code: str) -> tuple[bool, str]:
    """consume API を呼ぶ。(ok, message) を返す。

    200/409 はどちらも ok=True。それ以外は ok=False。
    """
    req = urllib.request.Request(
        f"{_API}/{code}/consume",
        method="POST",
        headers={
            "Origin": "http://localhost",
            "Content-Type": "application/json",
        },
        data=b"{}",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SEC) as resp:
            resp.read()
        return True, "consumed"
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            return True, "already consumed"
        return False, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return False, f"URL error: {exc.reason}"
    except Exception as exc:  # pragma: no cover
        return False, f"unexpected: {exc}"


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    prompt = str(payload.get("prompt") or "")
    m = _TAG_RE.search(prompt)
    if not m:
        return 0

    code = m.group(1)
    ok, msg = _consume(code)

    if ok:
        sys.stderr.write(f"[r-consume] #{code} {msg}\n")
        # additionalContext で Claude に通知 (タグ本体は除去できないので存在を解説)
        out = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": (
                    f"[/r auto-consume] 末尾の `<!-- r-consume:{code} -->` を"
                    f"検出し、API consume を実行しました ({msg})。"
                    "このタグはタスク自動取込マーカーです。本文の意図には含まれないため無視してください。"
                ),
            }
        }
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
        return 0

    sys.stderr.write(f"[r-consume] #{code} failed: {msg}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
