"""cgd スキルの利用ログ — レベル別使用回数を SQLite に記録・集計する.

「今のレベル構成がユーザーにとって有効に機能しているか」を後から検証できるように、
`/cgd` `/codex` 実行のたびに「いつ・どのレベルで・Gemini/critic オプションを使ったか」を
記録する。Step 1（レベル選択）の直後に record サブコマンドを呼ぶ想定（cgd/SKILL.md 参照）。

使い方:
    python cgd_usage_log.py record --level 2 [--gemini] [--critic] [--note "..."]
    python cgd_usage_log.py report [--since YYYY-MM-DD]

設計方針:
- record は「本流を絶対に止めない」。DB 書込に失敗しても例外を投げず WARN を stderr に出して
  exit 0 で終わる（利用ログはあくまで副次的な計測であり、cgd 本体の実行を左右してはならない）。
- report は集計結果を人間可読テキストで stdout に返す（他ツールからの呼出しは想定しない・
  必要になれば --json を後で足せばよい、という程度の割り切り）。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH: Path = Path(__file__).parent / "cgd_usage.sqlite3"

VALID_LEVELS = tuple(range(0, 8))  # Lv0-7


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cgd_usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_at TEXT NOT NULL,
            level INTEGER NOT NULL,
            gemini_opted_in INTEGER NOT NULL DEFAULT 0,
            critic_used INTEGER NOT NULL DEFAULT 0,
            note TEXT
        )
        """
    )
    conn.commit()
    return conn


def record_usage(
    level: int,
    gemini_opted_in: bool = False,
    critic_used: bool = False,
    note: str | None = None,
    db_path: Path = DB_PATH,
) -> bool:
    """利用ログを1件記録する。成功したら True、失敗しても例外は投げず False を返す。"""
    if level not in VALID_LEVELS:
        print(f"[cgd usage] WARN: 未知の level '{level}' のため記録をスキップ", file=sys.stderr)
        return False
    logged_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = _connect(db_path)
        try:
            conn.execute(
                "INSERT INTO cgd_usage_log (logged_at, level, gemini_opted_in, critic_used, note) "
                "VALUES (?, ?, ?, ?, ?)",
                (logged_at, level, int(gemini_opted_in), int(critic_used), note),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"[cgd usage] WARN: 記録失敗（本流には影響しません）: {exc}", file=sys.stderr)
        return False
    print(f"[cgd usage] 記録: {logged_at} Lv{level}" + (" +Gemini" if gemini_opted_in else "") + (" +critic" if critic_used else ""), file=sys.stderr)
    return True


def build_report(since: str | None = None, db_path: Path = DB_PATH) -> str:
    """集計結果を人間可読テキストで返す。レコードが1件もなければその旨を返す。"""
    if not db_path.exists():
        return "[cgd usage] 記録なし（ログファイル未作成）"

    conn = _connect(db_path)
    try:
        where = ""
        params: tuple = ()
        if since:
            where = "WHERE logged_at >= ?"
            params = (since,)

        total = conn.execute(f"SELECT COUNT(*) FROM cgd_usage_log {where}", params).fetchone()[0]
        if total == 0:
            return "[cgd usage] 該当期間の記録なし" if since else "[cgd usage] 記録なし"

        span = conn.execute(
            f"SELECT MIN(logged_at), MAX(logged_at) FROM cgd_usage_log {where}", params
        ).fetchone()

        by_level = conn.execute(
            f"SELECT level, COUNT(*) FROM cgd_usage_log {where} GROUP BY level ORDER BY level",
            params,
        ).fetchall()

        gemini_count = conn.execute(
            f"SELECT COUNT(*) FROM cgd_usage_log {where}{' AND' if where else 'WHERE'} gemini_opted_in = 1",
            params,
        ).fetchone()[0]
        critic_count = conn.execute(
            f"SELECT COUNT(*) FROM cgd_usage_log {where}{' AND' if where else 'WHERE'} critic_used = 1",
            params,
        ).fetchone()[0]
    finally:
        conn.close()

    counts_by_level = {lv: 0 for lv in VALID_LEVELS}
    counts_by_level.update(dict(by_level))

    lines = [
        f"[cgd usage] 集計期間: {span[0]} 〜 {span[1]}（全 {total} 件）",
        "",
    ]
    for lv in VALID_LEVELS:
        n = counts_by_level[lv]
        bar = "#" * n if n <= 40 else "#" * 40 + f"(+{n - 40})"
        lines.append(f"  Lv{lv}: {n:>4} 件  {bar}")
    lines.append("")
    lines.append(f"  Gemini併用: {gemini_count} 件 / critic併用: {critic_count} 件")
    return "\n".join(lines)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="cgd usage log — record & report")
    sub = parser.add_subparsers(dest="command", required=True)

    p_record = sub.add_parser("record", help="1回分の利用を記録する")
    p_record.add_argument("--level", type=int, required=True, choices=VALID_LEVELS)
    p_record.add_argument("--gemini", action="store_true", help="Gemini をオプトイン参加させた場合")
    p_record.add_argument("--critic", action="store_true", help="critic 観点を併用した場合")
    p_record.add_argument("--note", default=None, help="任意メモ（起動キーワード等）")

    p_report = sub.add_parser("report", help="集計を表示する")
    p_report.add_argument("--since", default=None, help="YYYY-MM-DD 以降のみ集計")

    args = parser.parse_args()

    if args.command == "record":
        record_usage(args.level, gemini_opted_in=args.gemini, critic_used=args.critic, note=args.note)
        return

    if args.command == "report":
        print(build_report(since=args.since))
        return


if __name__ == "__main__":
    main()
