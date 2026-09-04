"""HQ 状態ボードの 1 セッション分を中継API(041)へ送る pusher。

hq_board.py フックから **切り離した別プロセス** として起動される (フックは即座に
戻り、ツール実行や応答停止を待たせない)。/hq の `push` (取りこぼしの一括送信) からも
関数として呼ばれる。

使い方:
  python hq_push.py <session_id> [--end]

結果は board/<sid>.push.json に残す (最終送信時刻・HTTP コード・hq 受理の可否)。
失敗は ctx_hook.log ([hq_push:...]) に記録し、exit 0 で終わる。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001, S110  # pragma: no cover
    pass

import ctx_ledger as L
import hq_relay

L._SRC = "hq_push"

BOARD_DIR = Path(os.environ.get("CLAUDE_HQ_DIR", r"C:/ClaudeCode/.hq")) / "board"


def push_session(sid: str, end: bool = False, board_dir: Path | None = None) -> dict:
    bdir = board_dir or BOARD_DIR
    path = bdir / f"{sid}.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        L.log(f"state unreadable {path}: {type(exc).__name__}: {exc}")
        return {"ok": False, "code": 0, "detail": "state unreadable"}
    if not isinstance(state, dict):
        return {"ok": False, "code": 0, "detail": "state not dict"}
    result = hq_relay.push_state(state, end=end)
    if not result.get("ok"):
        L.log(f"push failed sid={sid[:8]} end={end} code={result.get('code')} detail={str(result.get('detail'))[:200]}")
    record = {
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "ok": bool(result.get("ok")),
        "code": result.get("code"),
        "hq_accepted": result.get("hq_accepted"),
        "end": end,
    }
    try:
        (bdir / f"{sid}.push.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        L.log(f"push record write failed: {exc}")
    return result


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    end = "--end" in args
    sids = [a for a in args if not a.startswith("--")]
    if not sids:
        L.log("no session_id given")
        return 0
    try:
        push_session(sids[0], end=end)
    except Exception as exc:  # noqa: BLE001  # pusher は決して例外で落ちない
        L.log(f"push crashed: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
