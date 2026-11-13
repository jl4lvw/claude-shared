"""状況の帯(Claude Code の Mod)に出す「他のセッションの状態」を読む。

HQ のフックが書く記録(.hq/board。AI管制室と同じもの)を hq_board_cli.load_board で読み、
ユーザーの入力を待っているセッション・作業中・完了の件数を JSON 1 行で標準出力へ出す。
読むだけで、何も書き換えない。

使い方: python status_band.py [自分のセッションID]   (自分は一覧から外す)
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hq_board_cli  # noqa: E402

TERMINAL_SYMBOL = "★"
# デスクトップアプリがセッションごとに持つ記録(local_<アプリのID>.json。cliSessionId が HQ のID)。
APP_SESSIONS_DIR = Path(os.environ.get("APPDATA", "")) / "Claude" / "claude-code-sessions"
APP_SCAN_DAYS = 14  # これより古いアプリの記録は読まない(見つからないIDで全件を読み続けないため)
MAX_WAITING = 4  # 帯に札で出す件数(残りは「ほか N 件」)
RUNNING_FRESH_MIN = 60  # これより長く動きの無い「作業中」は数えない(止まった記録の残り)
SHORT_LABEL = {
    "waiting_permission": "許可待ち",
    "waiting_question": "質問待ち",
    "waiting_answer": "回答待ち",
}


@dataclass
class WaitingSession:
    title: str
    kind: str  # 許可待ち / 質問待ち / 回答待ち
    elapsed: str  # 「12分」「1時間28分」
    session_id: str  # アプリのセッションID(local_...)。分からないときは空


@dataclass
class BandSnapshot:
    ok: bool
    waiting: list[WaitingSession] = field(default_factory=list)
    waiting_total: int = 0
    running: int = 0
    done: int = 0
    symbol: str = TERMINAL_SYMBOL
    checked_at: str = ""
    error: str | None = None


def app_session_ids(cli_ids: set[str], sessions_dir: Path | None = None) -> dict[str, str]:
    """HQ のセッションID(CLI のID) → アプリのセッションID(local_...)。

    HQ のIDとアプリのIDは別物(2026-11-10 実測。HQ のIDに local_ を付けても開けない)。
    アプリの記録を新しい順に読み、必要な分が全部見つかったら止める。
    """
    base = APP_SESSIONS_DIR if sessions_dir is None else sessions_dir
    found: dict[str, str] = {}
    if not cli_ids or not base.is_dir():
        return found
    cutoff = time.time() - APP_SCAN_DAYS * 86400
    stamped = [(f.stat().st_mtime, f) for f in base.glob("*/*/local_*.json")]
    for _, f in sorted((x for x in stamped if x[0] >= cutoff), key=lambda x: x[0], reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        cli = data.get("cliSessionId")
        if cli in cli_ids and cli not in found and not data.get("isArchived"):
            found[cli] = str(data.get("sessionId") or "")
            if len(found) == len(cli_ids):
                break
    return found


def build_snapshot(rows: list[dict], now: datetime, sessions_dir: Path | None = None) -> BandSnapshot:
    """load_board の行から帯の中身を作る。待ちは、待ち始めが新しい順。"""
    waiting = sorted((r for r in rows if r["waiting"]), key=lambda r: r["elapsed_min"])
    app_ids = app_session_ids({str(r["session_id"]) for r in waiting[:MAX_WAITING]}, sessions_dir)
    fresh = now - timedelta(minutes=RUNNING_FRESH_MIN)
    running = 0
    for r in rows:
        if r["status"] != "running":
            continue
        updated = hq_board_cli._parse_ts(r.get("updated_at"))
        if updated is not None and updated >= fresh:
            running += 1
    return BandSnapshot(
        ok=True,
        waiting=[
            WaitingSession(
                title=str(r["title"]).strip()[:40],
                kind=SHORT_LABEL.get(r["status"], "待ち"),
                elapsed=r["elapsed"],
                session_id=app_ids.get(str(r["session_id"]), ""),
            )
            for r in waiting[:MAX_WAITING]
        ],
        waiting_total=len(waiting),
        running=running,
        done=sum(1 for r in rows if r["status"] == "done"),
        checked_at=now.strftime("%H:%M"),
    )


def collect(self_sid: str | None) -> BandSnapshot:
    now = datetime.now().astimezone()
    try:
        if not hq_board_cli.BOARD_DIR.exists():
            raise FileNotFoundError(f"記録のフォルダがありません: {hq_board_cli.BOARD_DIR}")
        rows = hq_board_cli.load_board(self_sid, False)
    except OSError as exc:
        return BandSnapshot(ok=False, checked_at=now.strftime("%H:%M"), error=str(exc))
    return build_snapshot(rows, now)


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    args = sys.argv[1:] if argv is None else argv
    print(json.dumps(asdict(collect(args[0] if args else None)), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
