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
- ただし Lv6/Lv7/Lv8 のときだけは副作用が1つある: `.claude/hooks/cgd_wf_gate.py arm` を呼んで
  「inline の codex exec を遮断するゲート」を張る（2026-08-05 追加）。Lv6/Lv7/Lv8 は Workflow 実行が
  必須だが、SKILL.md の導線だけでは守られなかった実績があるため機械的に強制する。
  ゲート設定の失敗も本流を止めない（fail-open）。
- report は集計結果を人間可読テキストで stdout に返す（他ツールからの呼出しは想定しない・
  必要になれば --json を後で足せばよい、という程度の割り切り）。
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

DB_PATH: Path = Path(__file__).parent / "cgd_usage.sqlite3"

VALID_LEVELS = tuple(range(0, 9))  # Lv0-8

# Workflow 実行が必須のレベル。record 時に inline 遮断ゲートを張る。
# 2026-08-05: Lv8 も追加 (Codex 3 回・出力量が最大なので本来いちばん WF が要る)。
WF_REQUIRED_LEVELS = (6, 7, 8)
GATE_SCRIPT: Path = Path(__file__).parent.parent / "hooks" / "cgd_wf_gate.py"


def _arm_wf_gate(level: int, session: str | None = None) -> None:
    """Lv6/Lv7/Lv8 のとき inline codex 遮断ゲートを張る。

    失敗しても本流(cgd の実行)は止めないが、**黙って素通りさせない**。
    ゲートが張られていないと「WF 必須」が無効化されるので、失敗は WARN ではなく
    はっきりした ERROR として出す (Lv8 レビューの fail-open 指摘への対応)。
    """
    if level not in WF_REQUIRED_LEVELS:
        return

    def _fail(msg: str) -> None:
        print(f"[cgd usage] ❌ ERROR: Lv{level} の WF 必須ゲートを張れませんでした: {msg}", file=sys.stderr)
        print("[cgd usage]    → inline の codex が遮断されません。**Workflow を使うことを自分で守ること**", file=sys.stderr)

    if not GATE_SCRIPT.is_file():
        _fail(f"{GATE_SCRIPT} が見つかりません（/g-dl 未実施の可能性）")
        return
    cmd = [sys.executable, str(GATE_SCRIPT), "arm", "--level", str(level)]
    if session:
        cmd += ["--session", session]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            # text=True だけだと Windows の既定ロケール (CP932) で子プロセスの
            # 日本語出力をデコードして UnicodeDecodeError になる。必ず明示する。
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _fail(str(exc))
        return
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or "wf_nonce" not in out:
        _fail((proc.stderr or out or f"exit={proc.returncode}").strip())
        return
    print(out, file=sys.stderr)


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
    session: str | None = None,
) -> bool:
    """利用ログを1件記録する。成功したら True、失敗しても例外は投げず False を返す。"""
    if level not in VALID_LEVELS:
        print(f"[cgd usage] WARN: 未知の level '{level}' のため記録をスキップ", file=sys.stderr)
        return False
    # ゲートは DB 書込より先に張る（DB が落ちていても強制は効かせる）
    _arm_wf_gate(level, session=session)
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
    p_record.add_argument("--session", default=None,
                          help="セッションID（scratchpad ディレクトリ名）。Lv6/7/8 のゲートを"
                               "自セッション限定にする。省略すると最初に codex を叩いたセッションが所有者になる")

    p_report = sub.add_parser("report", help="集計を表示する")
    p_report.add_argument("--since", default=None, help="YYYY-MM-DD 以降のみ集計")

    args = parser.parse_args()

    if args.command == "record":
        record_usage(args.level, gemini_opted_in=args.gemini, critic_used=args.critic,
                     note=args.note, session=args.session)
        return

    if args.command == "report":
        print(build_report(since=args.since))
        return


if __name__ == "__main__":
    main()
