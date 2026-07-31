"""PostCompact hook: 台帳へ「圧縮が起きた」印を打つ。

PreCompact ではなく PostCompact を使う理由: PreCompact は「圧縮しようとした」
時点で、ブロックされたり失敗したりし得る。PostCompact は実際に圧縮が完了した
時点なので印が正確になる。どちらも additionalContext は返せない (公式ドキュメント
2026-07-30 確認) ので、ここでの仕事はファイルへの副作用だけ。

この印が無いと、圧縮後の Claude は「自分が記憶を失った」ことに気づけない
(圧縮されたという自覚は本人には残らない)。次ターンの ctx_inject.py が
この行を読んで警告を出す。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

import ctx_ledger as L  # noqa: E402

_ALLOWED_TRIGGERS = ("auto", "manual")


def _run(payload: dict) -> None:
    key = L.session_key(payload)
    path = L.ledger_path(key)
    if key is None or path is None:
        # 台帳を特定できない。共有ファイルへ書くと別セッションに偽の圧縮印が付く。
        L.log(f"compact_mark: no session identity; keys={sorted(payload.keys())}")
        return

    trigger = payload.get("trigger")
    if not isinstance(trigger, str) or trigger not in _ALLOWED_TRIGGERS:
        L.log(f"compact_mark: unexpected trigger={trigger!r}")
        trigger = "unknown"

    L.ensure_ledger(path, key)
    if not L.append(path, "COMPACT", trigger):
        L.log(f"compact_mark: append failed for {path}")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        _run(payload)
    except Exception as exc:
        L.log(f"compact_mark failed: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # pragma: no cover — フックは絶対に本流を止めない
        sys.exit(0)
