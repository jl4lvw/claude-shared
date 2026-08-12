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
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parent

# --- 端末ごとにファイルを分ける（2026-08-12・pv と同じ方式に揃えた）-----------
# 単一ファイル `cgd_usage.sqlite3` を共有ミラーに載せていたため、古い DB を持つ
# 端末が push した時点で他端末の記録が上書きで消えていた。pv は先に端末別へ
# 移行済みで、cgd だけ取り残されていた。
# あわせて g-ul / g-dl の //XF に `cgd_usage*.sqlite3` を入れ、ミラーが
# usage DB を触らないようにしてある（消えるのも復活するのも起きなくなる）。
def _host_slug() -> str:
    raw = platform.node() or os.environ.get("COMPUTERNAME") or "unknown"
    slug = re.sub(r"[^A-Za-z0-9_-]", "-", raw).strip("-")
    return slug or "unknown"


HOST = _host_slug()
DB_GLOB = "cgd_usage_*.sqlite3"
DB_PATH: Path = Path(
    os.environ.get("CGD_USAGE_DB", str(_TOOLS_DIR / f"cgd_usage_{HOST}.sqlite3"))
)
# 端末名を入れる前の単一ファイル。存在すれば初回に自端末のファイルへ引き継ぐ。
_LEGACY_DB: Path = _TOOLS_DIR / "cgd_usage.sqlite3"


def _migrate_legacy() -> None:
    """旧 `cgd_usage.sqlite3` を自端末のファイルへ引き継ぐ（pv と同じ手順）。

    コピー → 原子的な os.replace で確定する。移動だけだと途中で落ちたとき記録を失い、
    ロックファイルは stale で恒久ブロックするので使わない。
    """
    if not _LEGACY_DB.is_file() or DB_PATH.is_file() or DB_PATH == _LEGACY_DB:
        return
    tmp = DB_PATH.with_name(DB_PATH.name + f".migrating.{os.getpid()}")
    try:
        shutil.copy2(_LEGACY_DB, tmp)
        if DB_PATH.is_file():
            tmp.unlink(missing_ok=True)   # 競合に負けた側。相手の結果を尊重する
            return
        os.replace(tmp, DB_PATH)
        try:
            _LEGACY_DB.rename(_LEGACY_DB.with_suffix(".sqlite3.migrated"))
        except OSError:
            pass  # 相手が先に退避済み。移行自体は完了している
        print(f"[cgd usage] 旧 DB を {DB_PATH.name} へ引き継ぎました", file=sys.stderr)
    except OSError as exc:
        print(f"[cgd usage] 旧 DB の引き継ぎに失敗: {exc}", file=sys.stderr)


def all_db_paths() -> list[Path]:
    """集計対象。**全端末のファイルを読む**（書くのは自端末のものだけ）。"""
    _migrate_legacy()
    paths = sorted(_TOOLS_DIR.glob(DB_GLOB))
    if DB_PATH not in paths and DB_PATH.is_file():
        paths.append(DB_PATH)
    return paths


def _connect_readonly(path: Path) -> sqlite3.Connection:
    """他端末の DB は**本当に read-only で開く**（schema 作成も移行も走らせない）。

    pv で「集計のつもりが他端末の DB を触って壊す経路」になっていた実績があるため、
    URI モードの mode=ro を使う。`?` `#` `%` を含むパスのため URI エスケープする。
    """
    uri = "file:" + urllib.parse.quote(str(path).replace("\\", "/"), safe="/:") + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5.0)

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
    """書込用。自端末のファイルだけを開く（初回は旧 DB を引き継ぐ）。"""
    if db_path == DB_PATH:
        _migrate_legacy()
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


def load_all_rows(since: str | None = None) -> list[tuple]:
    """全端末の DB から行を読む。壊れた DB 1 つで集計全体を落とさない。"""
    where = "WHERE logged_at >= ?" if since else ""
    params: tuple = (since,) if since else ()
    rows: list[tuple] = []
    for path in all_db_paths():
        try:
            conn = _connect_readonly(path)
        except sqlite3.Error as exc:
            print(f"[cgd usage] WARN: {path.name} を開けません: {exc}", file=sys.stderr)
            continue
        try:
            rows += conn.execute(
                "SELECT logged_at, level, gemini_opted_in, critic_used "
                f"FROM cgd_usage_log {where}",
                params,
            ).fetchall()
        except sqlite3.Error as exc:
            print(f"[cgd usage] WARN: {path.name} を読めません: {exc}", file=sys.stderr)
        finally:
            conn.close()
    return rows


def _sources_line() -> str:
    hosts = []
    for p in all_db_paths():
        m = re.match(r"cgd_usage_(.+)\.sqlite3$", p.name)
        hosts.append(m.group(1) if m else p.name)
    if not hosts:
        return "[cgd usage] 記録なし（ログファイル未作成）"
    return (f"[cgd usage] 書込先={DB_PATH.name}（端末 {HOST}）"
            f"/ 集計対象 {len(hosts)} 端末: {', '.join(hosts)}")


def build_report(since: str | None = None, db_path: Path | None = None) -> str:
    """集計結果を人間可読テキストで返す。レコードが1件もなければその旨を返す。

    db_path は後方互換のため残しているが、既定では**全端末のファイル**を集計する
    （2026-08-12 に端末別ファイルへ移行したため）。
    """
    if db_path is not None:
        # 単一ファイルを明示された場合（テスト等）
        if not db_path.exists():
            return "[cgd usage] 記録なし（ログファイル未作成）"
        conn = _connect_readonly(db_path)
        try:
            where = "WHERE logged_at >= ?" if since else ""
            params: tuple = (since,) if since else ()
            rows = conn.execute(
                "SELECT logged_at, level, gemini_opted_in, critic_used "
                f"FROM cgd_usage_log {where}", params).fetchall()
        finally:
            conn.close()
        header = f"[cgd usage] {db_path.name}"
    else:
        rows = load_all_rows(since)
        header = _sources_line()
        if not all_db_paths():
            return header

    total = len(rows)
    if total == 0:
        return header + "\n" + ("  該当期間の記録なし" if since else "  記録なし")

    stamps = sorted(r[0] for r in rows)
    counts_by_level = {lv: 0 for lv in VALID_LEVELS}
    for _ts, lv, _g, _c in rows:
        if lv in counts_by_level:
            counts_by_level[lv] += 1
    gemini_count = sum(1 for r in rows if r[2])
    critic_count = sum(1 for r in rows if r[3])

    lines = [
        header,
        f"[cgd usage] 集計期間: {stamps[0]} 〜 {stamps[-1]}（全 {total} 件）",
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
