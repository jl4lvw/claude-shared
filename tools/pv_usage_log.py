"""pv の利用状況・不具合を 1 か所に貯める台帳。

背景 (2026-08-12):
  pv 自身の自己レビュー（反証担当）に「cgd には `cgd_usage_log` があるのに pv には
  利用計測が無い。統合判断に最も必要な『pv が実際に何回使われるか』が測れない」と
  指摘されていた。ユーザーからも「レベル別の使用回数を記録し、あまり使われていない
  レベルが何か・なぜ使われていないかを後で検証したい」という要求があった。

設計方針:
  - **記録は自動**。cgd の usage log は「Claude が忘れずに叩く」前提なので、
    急いでいるときに飛ぶ。pv は `pv_plan.py` が必ず通る経路（build / exec /
    collect / halt）から呼ぶので、使えば必ず残る。
  - **失敗も halt も残す**。「Lv4 を指定したが未実装で止まった」が残れば、
    使われていないレベルが「不要だから」なのか「実装が無いから」なのかを
    後で区別できる。これが利用回数だけでは絶対に分からない部分。
  - **記録の失敗で本処理を落とさない**。計測は本業ではない。
  - 追記のみ。行の書き換えをしないので、並行実行しても壊れない。

置き場所 (2026-08-12 端末別に変更):
  `.claude/tools/pv_usage_<端末名>.sqlite3`。
  `/g-ul` `/g-dl` はどちらも robocopy //MIR で `.claude` を丸ごとミラーするため、
  単一ファイルだと古い DB を持つ端末の push で他端末の記録が消える。
  端末名を入れて **各端末は自分のファイルしか書かない**ようにした。
  集計 (`summary` / `levels` / `failures`) は `pv_usage_*.sqlite3` を**全部読む**ので、
  端末を分けても全体の統計は取れる。

  2026-08-12 追記: この「//MIR が共有側から他端末のファイルを消す」問題は、
  `/g-ul` `/g-dl` 両方の robocopy //XF に `pv_usage_*.sqlite3` `cgd_usage*.sqlite3`
  を入れて解消した。//XF に載せたファイルはコピーも削除もされないので、usage DB は
  各端末の手元だけで育つ（共有もされないが、消えることも無くなった）。
  同日 cgd の usage DB も端末別（`cgd_usage_<端末名>.sqlite3`）へ移行済み。

  分類の注意: halt の `reason` は**記録した時点の分類ルール**が残る。ルールを足す前に
  書かれた行は古い分類のままなので、改修をまたぐ集計は `reclassify` で付け直す
  （元の値は detail.reclassified_from に残す）。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import sqlite3
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

_TOOLS_DIR = Path(__file__).resolve().parent
_TS_FMT = "%Y-%m-%d %H:%M:%S"

# --- 端末ごとにファイルを分ける（2026-08-12 ユーザー指示）-------------------
# `/g-ul` `/g-dl` はどちらも robocopy //MIR で .claude を丸ごとミラーする。
# 単一ファイルだと、古い DB を持つ端末が push した時点で他端末の記録が
# **上書きで消える**。端末名をファイル名に入れれば、各端末は自分のファイルしか
# 書かないので、他端末のデータで自分の記録が消えることは無くなる。
#
# 残る限界（正直に書く）: //MIR は「送り側に無いファイルを消す」ので、
# /g-dl せずに /g-ul した端末は、共有側から**他端末のファイルを消す**。
# ただし消えるのは共有側のコピーだけで、各端末の手元には原本が残るため、
# その端末が次に push すれば復活する。単一ファイル方式の「記録が本当に失われる」
# とは失敗の重さが違う。
def _host_slug() -> str:
    raw = platform.node() or os.environ.get("COMPUTERNAME") or "unknown"
    slug = re.sub(r"[^A-Za-z0-9_-]", "-", raw).strip("-")
    return slug or "unknown"


HOST = _host_slug()
DB_GLOB = "pv_usage_*.sqlite3"
DB_PATH = Path(os.environ.get("PV_USAGE_DB", str(_TOOLS_DIR / f"pv_usage_{HOST}.sqlite3")))
# 端末名を入れる前の単一ファイル。存在すれば初回に自端末のファイルへ引き継ぐ。
_LEGACY_DB = _TOOLS_DIR / "pv_usage.sqlite3"


def all_db_paths() -> list[Path]:
    """集計対象。**全端末のファイルを読む**（書くのは自端末のものだけ）。"""
    # 読み取りだけの経路（summary 等）は _connect(write) を通らないので、
    # ここでも移行を試す。これが無いと、端末別へ切り替えた直後の集計から
    # 旧 DB の記録が丸ごと消えて見える（実際に踏んだ）。
    _migrate_legacy()
    paths = sorted(_TOOLS_DIR.glob(DB_GLOB))
    if DB_PATH not in paths and DB_PATH.is_file():
        paths.append(DB_PATH)
    # 引き継げなかった旧 DB も読む。記録が在るのに集計から消えるのが一番たちが悪い。
    if _LEGACY_DB.is_file() and _LEGACY_DB not in paths:
        paths.append(_LEGACY_DB)
    return paths

# event の種類。増やすときはここに書いてから使う（タイポで別種になるのを防ぐ）
EVENTS = ("build", "halt", "exec", "collect", "result")

# --- 費用の数値化 -----------------------------------------------------------
# cgd は「💰 費用集計」を全 Lv 必須の出力にしているが、Claude が stderr の
# usage 行を目で読んで表に起こす運用なので、書き忘れると消える。
# pv は Python が同じ行を**数値として**保存し、後から集計できるようにする。
#
# 実フォーマット (2026-08-12 実データで確認):
#   [DS Usage] 今回: 入力 405 (miss) + 384 (hit) / 出力 4,477 tok (¥0.20 / $0.0013) [model=deepseek-v4-flash]
_USAGE_RE = re.compile(
    r"入力\s*([\d,]+)\s*\(miss\)\s*\+\s*([\d,]+)\s*\(hit\)\s*/\s*出力\s*([\d,]+)\s*tok"
    r"\s*\(¥([\d.]+)\s*/\s*\$([\d.]+)\)"
)
_MODEL_RE = re.compile(r"\[model=([^\]]+)\]")


def parse_usage_line(line: str) -> dict | None:
    """`[DS Usage] 今回: ...` を数値に落とす。読めなければ None（推測しない）。"""
    m = _USAGE_RE.search(line or "")
    if not m:
        return None
    miss, hit, out, jpy, usd = m.groups()
    model = _MODEL_RE.search(line)
    return {
        "in_miss": int(miss.replace(",", "")),
        "in_hit": int(hit.replace(",", "")),
        "out": int(out.replace(",", "")),
        "jpy": float(jpy),
        "usd": float(usd),
        "model": model.group(1) if model else None,
    }

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pv_event (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,
    event         TEXT    NOT NULL,
    run           TEXT,
    level         INTEGER,
    depth         TEXT,
    engine        TEXT,
    task          TEXT,
    ok            INTEGER,
    reason        TEXT,
    exit_code     INTEGER,
    bytes         INTEGER,
    duration_ms   INTEGER,
    skill_version TEXT,
    detail        TEXT
);
CREATE INDEX IF NOT EXISTS idx_pv_event_level ON pv_event(level);
CREATE INDEX IF NOT EXISTS idx_pv_event_event ON pv_event(event);
CREATE INDEX IF NOT EXISTS idx_pv_event_ts    ON pv_event(ts);
"""


def _migrate_legacy() -> None:
    """端末名を入れる前の `pv_usage.sqlite3` を自端末のファイルへ引き継ぐ。

    コピーしてから元を退避する（移動だけだと、途中で落ちたとき記録を失う）。
    """
    if not _LEGACY_DB.is_file() or DB_PATH.is_file() or DB_PATH == _LEGACY_DB:
        return
    # 2 プロセスが同時に来ると、旧実装は copy2 → rename が競合して
    # 片方が「rename 済みのファイルを copy2」して失敗した
    # （2026-08-12 cgd Lv8・Codex 指摘）。
    # ロックファイルは stale で恒久ブロックする（DS 指摘）ので使わない。
    #
    # 2026-08-12 追加修正: `is_file()` で確認してから `os.replace` する形も不可分では
    # なく、確認と置換の間に別プロセスの記録が DB を作って書き込むと、os.replace が
    # それを**無条件で上書きして消す**（cgd Lv7 で 4 者が収束指摘）。
    # 確定は **os.link**（既に在れば FileExistsError）で行い、単一勝者を決める。
    tmp = DB_PATH.with_name(DB_PATH.name + f".migrating.{os.getpid()}")
    try:
        shutil.copy2(_LEGACY_DB, tmp)
        try:
            os.link(tmp, DB_PATH)     # ← 不可分。既に在れば FileExistsError
        except FileExistsError:
            return                    # 競合に負けた側。相手の結果を尊重する
        except OSError as exc:
            print(f"[pv_usage] 旧 DB の引き継ぎを見送りました（集計には引き続き含まれます）: {exc}",
                  file=sys.stderr)
            return
        try:
            _LEGACY_DB.rename(_LEGACY_DB.with_suffix(".sqlite3.migrated"))
        except OSError as exc:
            print(f"[pv_usage] 旧 DB の退避に失敗（引き継ぎ自体は完了）: {exc}", file=sys.stderr)
        print(f"[pv_usage] 旧 DB を {DB_PATH.name} へ引き継ぎました", file=sys.stderr)
    except OSError as exc:
        print(f"[pv_usage] 旧 DB の引き継ぎに失敗: {exc}", file=sys.stderr)
    finally:
        # 成功時は link 済みなので余分なリンク、失敗時は残骸。どちらも消す
        # （残ると robocopy の //XF に掛からずミラーに乗る）。
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _connect(path: Path | None = None, *, readonly: bool = False) -> sqlite3.Connection:
    """readonly=True は **本当に read-only で開く**。

    以前は名前だけ readonly で、mkdir も移行も走り schema も作られた。
    集計のつもりで他端末の DB を触って壊す経路になっていた
    （2026-08-12 cgd Lv8・Codex 2 者一致）。
    """
    target = path or DB_PATH
    if readonly:
        # URI モードで mode=ro。存在しないファイルはここで例外になるので、
        # 呼び出し側 (load_all_events) が握って次のファイルへ進む。
        # パスは URI エスケープする。`?` `#` `%` が入ると誤解釈される
        # （Codex 再レビュー 🟡）。`/` はパス区切りとして残す。
        uri_path = urllib.parse.quote(target.as_posix(), safe="/:")
        return sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True, timeout=5.0)
    if path is None:
        _migrate_legacy()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=5.0)
    conn.executescript(_SCHEMA)
    return conn


_COLUMNS = ("ts", "event", "run", "level", "depth", "engine", "task", "ok",
            "reason", "exit_code", "bytes", "duration_ms", "skill_version", "detail")


def _expand_mode(rec: dict) -> None:
    """`detail` の中の mode をイベント辞書のトップレベルへ出す。

    mode は専用カラムではなく detail JSON に入れている（既存 DB を ALTER せずに
    済ませるため・tasks や engines と同じ扱い）。集計側で毎回 JSON を開くのは
    読みにくいので、読み込み時に一度だけ展開する。
    記録が無い古い行は None のままにして、`levels` が「未記録」として表示する。
    """
    rec.setdefault("mode", None)
    detail = rec.get("detail")
    if not detail:
        return
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except (TypeError, ValueError):
            return
    if isinstance(detail, dict):
        mode = detail.get("mode")
        if isinstance(mode, str) and mode:
            rec["mode"] = mode


def load_all_events() -> list[dict]:
    """全端末のファイルから event を読み集める。

    集計を SQL の GROUP BY でやると DB をまたげないので、行を読んで
    Python 側で数える。件数は多くても数千行なので実用上問題にならない。
    """
    out: list[dict] = []
    for p in all_db_paths():
        host = p.stem.replace("pv_usage_", "") or "?"
        try:
            with _connect(p, readonly=True) as conn:
                rows = conn.execute(f"SELECT {', '.join(_COLUMNS)} FROM pv_event").fetchall()
        except sqlite3.Error as exc:
            print(f"[pv_usage] {p.name} を読めません: {exc}", file=sys.stderr)
            continue
        for r in rows:
            rec = dict(zip(_COLUMNS, r))
            rec["host"] = host
            _expand_mode(rec)
            out.append(rec)
    # DB へ書けずに退避された分も読む。残っているのに集計へ出ないと、
    # 「落ちた事実」を残した意味が無い（2026-08-12 cgd Lv8・DS の条件付き賛成）。
    for fb in sorted(_TOOLS_DIR.glob("pv_usage_*.fallback.jsonl")):
        host = fb.name.replace("pv_usage_", "").replace(".fallback.jsonl", "")
        try:
            for line in fb.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rec["host"] = host + "(退避)"
                _expand_mode(rec)
                out.append(rec)
        except OSError:
            continue
    out.sort(key=lambda r: r.get("ts") or "")
    return out


def log_event(
    event: str,
    *,
    run: str | None = None,
    level: int | None = None,
    depth: str | None = None,
    engine: str | None = None,
    task: str | None = None,
    ok: bool | None = None,
    reason: str | None = None,
    exit_code: int | None = None,
    size_bytes: int | None = None,
    duration_ms: int | None = None,
    skill_version: str | None = None,
    detail: dict | None = None,
) -> None:
    """1 件記録する。**例外を外へ出さない**（計測の失敗で pv を止めない）。

    2026-08-12 cgd Lv8 の 🟠 対応。旧実装は 1 回試して失敗したら stderr に出すだけで、
    **並行実行時に記録が黙って落ちた**（計測そのものが信用できなくなる）。
    いまは (1) SQLite のロック競合を数回リトライし、(2) それでも駄目なら
    **JSONL へ追記して「落ちた事実」を残す**。この JSONL は次回の
    `flush-fallback` で DB へ取り込める。
    """
    for attempt in range(4):
        try:
            _insert(event, run=run, level=level, depth=depth, engine=engine, task=task,
                    ok=ok, reason=reason, exit_code=exit_code, size_bytes=size_bytes,
                    duration_ms=duration_ms, skill_version=skill_version, detail=detail)
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                break
            time.sleep(0.15 * (attempt + 1))
        except Exception:  # noqa: BLE001
            break
    _append_fallback({
        "ts": datetime.now().strftime(_TS_FMT), "event": event, "run": run, "level": level,
        "depth": depth, "engine": engine, "task": task,
        "ok": None if ok is None else int(ok), "reason": reason, "exit_code": exit_code,
        "bytes": size_bytes, "duration_ms": duration_ms, "skill_version": skill_version,
        "detail": json.dumps(detail, ensure_ascii=False) if detail else None,
    })


FALLBACK_PATH = _TOOLS_DIR / f"pv_usage_{HOST}.fallback.jsonl"


def _append_fallback(row: dict) -> None:
    """DB へ書けなかった記録を JSONL に残す。**ここが最後の砦**なので極力単純に。"""
    try:
        with open(FALLBACK_PATH, "a", encoding="utf-8", newline="") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"[pv_usage] DB へ書けなかったため {FALLBACK_PATH.name} に退避しました",
              file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"[pv_usage] 記録を完全に失いました: {exc}", file=sys.stderr)


def _insert(
    event: str,
    *,
    run: str | None = None,
    level: int | None = None,
    depth: str | None = None,
    engine: str | None = None,
    task: str | None = None,
    ok: bool | None = None,
    reason: str | None = None,
    exit_code: int | None = None,
    size_bytes: int | None = None,
    duration_ms: int | None = None,
    skill_version: str | None = None,
    detail: dict | None = None,
) -> None:
    """DB へ 1 行入れるだけ。**例外はそのまま投げる**（呼び側がリトライ判断する）。"""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO pv_event (ts, event, run, level, depth, engine, task, ok,"
            " reason, exit_code, bytes, duration_ms, skill_version, detail)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                datetime.now().strftime(_TS_FMT), event, run, level, depth, engine,
                task, None if ok is None else int(ok), reason, exit_code,
                size_bytes, duration_ms, skill_version,
                json.dumps(detail, ensure_ascii=False) if detail else None,
            ),
        )


def forward_incident(title: str, *, category: str, detail: str, evidence: list[str] | None = None,
                     severity: str = "mid") -> str | None:
    """明確な不具合だけを既存の不具合台帳へ転記する。

    台帳を新しく作らず `incident_log.py` に合流させる（memory の運用方針）。
    こちらも失敗しても本処理を止めない。
    """
    try:
        sys.path.insert(0, str(_TOOLS_DIR))
        import incident_log  # type: ignore

        existing = incident_log.load_incidents(warn=False)
        inc = incident_log.Incident(
            id=incident_log.issue_id(existing),
            ts=datetime.now().strftime(_TS_FMT),
            tool="harness",
            category=category,
            title=title,
            severity=severity,
            detail=detail,
            evidence=evidence or [],
            session="",
            status="open",
        )
        incident_log.append_incident(inc)
        return inc.id
    except Exception as exc:  # noqa: BLE001
        print(f"[pv_usage] 不具合台帳への転記に失敗（本処理は継続）: {exc}", file=sys.stderr)
        return None


# --------------------------------------------------------------------------- CLI


def _sources_line() -> str:
    paths = all_db_paths()
    return (f"[pv usage] 書込先={DB_PATH.name}（端末 {HOST}）/ 集計対象 {len(paths)} 端末: "
            + ", ".join(p.name.replace("pv_usage_", "").replace(".sqlite3", "") for p in paths))


def _events(args: argparse.Namespace) -> list[dict]:
    evs = load_all_events()
    since = getattr(args, "since", None)
    if since:
        evs = [e for e in evs if (e.get("ts") or "") >= since]
    return evs


def _tally(evs: list[dict], key: str, pred=None) -> dict:
    out: dict = {}
    for e in evs:
        if pred and not pred(e):
            continue
        out[e.get(key)] = out.get(e.get(key), 0) + 1
    return out


def _cmd_levels(args: argparse.Namespace) -> int:
    """レベル別の使用回数。『使われていないレベル』を見るための主表。"""
    print(_sources_line())
    evs = _events(args)
    built = _tally(evs, "level", lambda e: e["event"] == "build")
    halted = _tally(evs, "level",
                    lambda e: e["event"] == "halt" and e.get("reason") == "level_unimplemented")
    other_halt = _tally(evs, "level",
                        lambda e: e["event"] == "halt" and e.get("reason") != "level_unimplemented")
    # 「完走した run」= result イベントが ok で残っているもの。build 回数と
    # 混同すると「使われている」を過大評価する（Lv8 で 3 者が凡例のずれを指摘）。
    done = _tally(evs, "level", lambda e: e["event"] == "result" and e.get("ok") == 1)
    levels = sorted({k for k in list(built) + list(halted) + list(other_halt) if k is not None})
    if not levels:
        print("  まだ記録がありません")
        return 0
    # 日本語は端末上 2 桁幅なので、str.format の桁揃えでは崩れる。
    # 見出しを ASCII にして揃え、意味は下の注記で説明する。
    print(f"  {'Lv':<5}{'build':>7}{'done':>6}{'no-impl':>10}{'halted':>9}")
    for lv in levels:
        print(f"  {('Lv' + str(lv)):<5}{built.get(lv, 0):>7}{done.get(lv, 0):>6}"
              f"{halted.get(lv, 0):>10}{other_halt.get(lv, 0):>9}")
    print("    build=build できた回数（**完走したとは限らない**） /"
          " no-impl=未実装で拒否 / halted=その他の理由で中止")
    print("\n  ※「未実装で拒否」が多いレベルは、需要はあるのに実装が無いということ。")
    print("    実行 0 かつ拒否 0 のレベルは、そもそも選ばれていない。両者は原因が違う。")
    # depth の分布も出す（レベルと直交する軸なので混ぜない）
    depths = _tally(evs, "depth", lambda e: e["event"] == "build")
    if depths:
        print("\n  depth 別:", "  ".join(f"{d or '?'}={n}" for d, n in sorted(depths.items(), key=lambda x: str(x[0]))))
    # mode も直交軸。レベル番号は mode 間で共有なので、混ぜたままだと
    # 「review モードが使われているか」が分からない（2026-08-12 に記録追加）。
    modes = _tally(evs, "mode", lambda e: e["event"] == "build")
    if modes:
        known = {k: v for k, v in modes.items() if k}
        unknown = modes.get(None, 0)
        parts = [f"{k}={v}" for k, v in sorted(known.items())]
        if unknown:
            # mode を記録する前の run。0 件にはならないので黙って落とさない。
            parts.append(f"未記録={unknown}")
        print("  mode 別:", "  ".join(parts))
    hosts = _tally(evs, "host", lambda e: e["event"] == "build")
    if len(hosts) > 1:
        print("  端末別  :", "  ".join(f"{h}={n}" for h, n in sorted(hosts.items())))
    return 0


def _cmd_summary(args: argparse.Namespace) -> int:
    print(_sources_line())
    evs = _events(args)
    if not evs:
        print("  まだ記録がありません")
        return 0
    for ev, n in sorted(_tally(evs, "event").items(), key=lambda x: str(x[0])):
        print(f"  {ev:<10}{n:>5}")
    ts = [e["ts"] for e in evs if e.get("ts")]
    print(f"  期間      : {min(ts)} 〜 {max(ts)}")
    print(f"  run 数    : {len({e['run'] for e in evs if e.get('run')})}")
    print(f"  失敗      : {sum(1 for e in evs if e.get('ok') == 0)} 件（`failures` で内訳）")
    hosts = _tally(evs, "host")
    if len(hosts) > 1:
        print("  端末別    :", "  ".join(f"{h}={n}" for h, n in sorted(hosts.items())))
    return 0


def _cmd_failures(args: argparse.Namespace) -> int:
    """不具合の蓄積を読む口。改善のネタはここから拾う。"""
    print(_sources_line())
    evs = [e for e in _events(args) if e.get("ok") == 0 or e["event"] == "halt"]
    if not evs:
        print("  失敗・中止の記録はありません")
        return 0
    for e in list(reversed(evs))[:args.limit]:
        head = f"  {e['ts']}  {e['event']:<8} Lv{e['level'] if e['level'] is not None else '?'}"
        if e.get("run"):
            head += f"  run={e['run']}"
        if len(_tally(evs, "host")) > 1:
            head += f"  [{e['host']}]"
        print(head)
        print(f"      理由={e.get('reason') or '-'}  engine={e.get('engine') or '-'}"
              f"  task={e.get('task') or '-'}  exit={e['exit_code'] if e.get('exit_code') is not None else '-'}")
        if e.get("detail"):
            print(f"      {str(e['detail'])[:200]}")
    print("\n  ※ 分類ごとの件数:")
    for reason, n in sorted(_tally(evs, "reason").items(), key=lambda x: -x[1]):
        print(f"      {(reason or '(未分類)'):<28}{n:>4}")
    return 0


def _cmd_record(args: argparse.Namespace) -> int:
    """Workflow 完了後の結果を主 context が記録する（自動で取れない値の受け口）。"""
    detail = {}
    if args.detail:
        try:
            detail = json.loads(args.detail)
        except json.JSONDecodeError:
            detail = {"note": args.detail}
    log_event(
        "result", run=args.run, level=args.level,
        ok=(args.outcome == "ok"), reason=args.outcome,
        duration_ms=args.duration_ms, engine=args.merge_model,
        detail={**detail, "tokens": args.tokens} if args.tokens else detail or None,
    )
    print(f"[pv usage] result を記録しました: run={args.run} outcome={args.outcome}")
    return 0


def _cmd_cost(args: argparse.Namespace) -> int:
    """費用集計。cgd の「💰 費用集計」に相当するが、**数値は Python が保存済み**。"""
    print(_sources_line())
    evs = [e for e in _events(args) if e["event"] == "exec"]
    if not evs:
        print("  外部エンジンの呼出記録がありません")
        return 0

    per_engine: dict[str, dict] = {}
    unparsed = 0
    for e in evs:
        eng = e.get("engine") or "?"
        acc = per_engine.setdefault(eng, {"calls": 0, "in": 0, "out": 0, "jpy": 0.0, "priced": 0})
        acc["calls"] += 1
        detail = e.get("detail")
        info = None
        if detail:
            try:
                info = parse_usage_line(json.loads(detail).get("usage_line") or "")
            except (json.JSONDecodeError, AttributeError):
                info = None
        if info:
            acc["in"] += info["in_miss"] + info["in_hit"]
            acc["out"] += info["out"]
            acc["jpy"] += info["jpy"]
            acc["priced"] += 1
        else:
            unparsed += 1

    # 「観測済み / 未観測」を第一級の列として出す。合計は**観測済みのみ**。
    # 合計 ¥0.00 を「実費ゼロ」と誤読してそのまま報告する事故を防ぐ
    # （2026-08-12 cgd Lv8・批評 2 者が困り度「高」で一致）。
    print(f"  {'engine':<12}{'calls':>6}{'measured':>9}{'unmeasured':>11}"
          f"{'in tok':>10}{'out tok':>10}{'JPY':>9}")
    total_jpy = 0.0
    total_unmeasured = 0
    for eng, a in sorted(per_engine.items()):
        miss = a["calls"] - a["priced"]
        total_unmeasured += miss
        print(f"  {eng:<12}{a['calls']:>6}{a['priced']:>9}{miss:>11}"
              f"{a['in']:>10,}{a['out']:>10,}{a['jpy']:>9.2f}")
        total_jpy += a["jpy"]
    print(f"  {'合計':<10}{'':>6}{'':>9}{total_unmeasured:>11}{'':>10}{'':>10}{total_jpy:>9.2f}")
    print(f"\n  ⚠️ この合計は **観測できた {sum(a['priced'] for a in per_engine.values())} 件だけ**の"
          f"金額です（未観測 {total_unmeasured} 件）。**実費の総額ではありません。**")
    print("     - codex はサブスク認証のため料金もトークン数も出力されない（クォータは消費している）")
    print("     - **Claude 側（各視点・統合・検証）の消費はそもそも記録対象外**なので、"
          "Lv1 は常に ¥0 に見える")
    per_run: dict = {}
    broken = 0
    for e in evs:
        detail = e.get("detail")
        info = None
        if detail:
            # 前半のループと違いここだけ裸の json.loads で、壊れた detail 1 件で
            # 費用集計全体が例外落ちしていた（2026-08-12 cgd Lv8・3 者一致）。
            try:
                info = parse_usage_line(json.loads(detail).get("usage_line") or "")
            except (json.JSONDecodeError, AttributeError, TypeError):
                broken += 1
        if info:
            per_run[e.get("run")] = per_run.get(e.get("run"), 0.0) + info["jpy"]
    if broken:
        print(f"\n  ※ detail を読めなかった行が {broken} 件あります（集計からは除外）")
    if per_run:
        print("\n  run 別:")
        for run, jpy in sorted(per_run.items(), key=lambda x: -x[1])[:10]:
            print(f"    {run:<24}¥{jpy:.2f}")
    return 0


def _cmd_backfill(args: argparse.Namespace) -> int:
    """既存 run の plan.json から過去の実行を復元する。

    計測を後から足したので、それ以前の run が抜けている。plan.json には
    実際の作成時刻・レベル・depth が入っているので、**実データから**復元できる。
    でっち上げではないことが分かるよう `backfilled=true` を detail に残す。
    """
    root = Path(os.environ.get("PV_ROOT", r"C:/tmp-ai/pv"))
    if not root.is_dir():
        print(f"[pv usage] run ディレクトリがありません: {root}")
        return 1
    # 既知判定は**全端末**を見る（他端末が既に記録した run を二重に入れない）
    known = {e["run"] for e in load_all_events() if e["event"] == "build" and e.get("run")}
    added = 0
    for plan_file in sorted(root.glob("*/plan.json")):
        try:
            plan = json.loads(plan_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  [SKIP] {plan_file}: {exc}")
            continue
        run = plan.get("run") or plan_file.parent.name
        if run in known:
            continue
        tasks = plan.get("tasks", [])
        try:
            with _connect() as conn:
                conn.execute(
                    "INSERT INTO pv_event (ts, event, run, level, depth, ok, skill_version, detail)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (plan.get("created_at") or "?", "build", run, plan.get("level"),
                     plan.get("depth"), 1, plan.get("skill_version"),
                     json.dumps({"backfilled": True,
                                 "tasks": [t.get("id") for t in tasks],
                                 "engines": sorted({t.get("engine") for t in tasks if t.get("engine")}),
                                 "attachments": len(plan.get("attachments") or [])},
                                ensure_ascii=False)))
            added += 1
            print(f"  [ADD] {run}  Lv{plan.get('level')}  {plan.get('created_at')}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [ERR] {run}: {exc}")

    # 外部エンジンの呼出は audit.jsonl に残っている（費用集計の元データ）。
    # こちらも実データなので復元してよい。
    known_exec = {(e.get("run"), e.get("task"), e.get("ts"))
                  for e in load_all_events() if e["event"] == "exec"}
    added_exec = 0
    for audit in sorted(root.glob("*/audit.jsonl")):
        run = audit.parent.name
        for line in audit.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = (run, rec.get("task"), rec.get("ts"))
            if key in known_exec or not rec.get("engine"):
                continue
            size = rec.get("bytes")
            exit_code = rec.get("exit")
            try:
                with _connect() as conn:
                    conn.execute(
                        "INSERT INTO pv_event (ts, event, run, engine, task, ok, exit_code,"
                        " bytes, detail) VALUES (?,?,?,?,?,?,?,?,?)",
                        (rec.get("ts") or "?", "exec", run, rec.get("engine"), rec.get("task"),
                         1 if exit_code == 0 else 0, exit_code, size,
                         json.dumps({"backfilled": True, "usage_line": rec.get("usage_line")},
                                    ensure_ascii=False)))
                added_exec += 1
                known_exec.add(key)
            except Exception as exc:  # noqa: BLE001
                print(f"  [ERR] {run}/{rec.get('task')}: {exc}")
    print(f"[pv usage] build {added} 件 / exec {added_exec} 件を復元しました"
          "（既知のものは再挿入しません）")
    return 0


def _cmd_purge(args: argparse.Namespace) -> int:
    """テストで入った行を消す。run 名を指定しないと何もしない（誤爆防止）。"""
    if not args.run:
        print("[pv usage] --run の指定が必要です（全消しはできません）")
        return 1
    with _connect() as conn:
        n = conn.execute("DELETE FROM pv_event WHERE run = ?", (args.run,)).rowcount
    print(f"[pv usage] run={args.run} の {n} 行を削除しました")
    return 0


def _cmd_reclassify(args: argparse.Namespace) -> int:
    """古い halt 行の reason を、現行の分類ルールで付け直す。

    分類は `pv_plan._HALT_PATTERNS` の部分文字列一致で、**記録した時点のルール**が
    そのまま残る。ルールを足す前に書かれた行は古い分類（多くは other）のままなので、
    改修をまたいだ期間の集計が割れる。実際 2026-08-12 に 33 秒差の同じ中止が
    Lv4=other / Lv8=level_unimplemented と別分類で入っていた。

    detail に元の message が入っているので**実データから**引き直せる。
    backfill と同じく、書き換えた事実を `reclassified_from` として detail に残す
    （後から「でっち上げでない」ことを確認できるようにするため）。

    書き換えるのは**自端末の DB だけ**。他端末のファイルは read-only で開く方針
    （集計のつもりで他端末の DB を壊した実績があるため）。
    """
    try:
        import pv_plan  # 遅延 import（pv_plan が本モジュールを import するため循環回避）
    except Exception as exc:  # noqa: BLE001
        print(f"[pv usage] pv_plan を読み込めないので再分類できません: {exc}")
        return 1

    if not DB_PATH.is_file():
        print(f"[pv usage] 自端末の DB がありません: {DB_PATH.name}")
        return 1

    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, level, run, reason, detail FROM pv_event WHERE event = 'halt'"
        ).fetchall()
        changes: list[tuple[int, str, str, str]] = []
        skipped_broken = 0    # detail が JSON として壊れている
        skipped_nomsg = 0     # message が入っていない（分類の根拠が無い）
        for rid, level, run, reason, detail in rows:
            try:
                payload = json.loads(detail) if detail else {}
            except (TypeError, ValueError):
                skipped_broken += 1
                continue
            msg = payload.get("message")
            if not isinstance(msg, str) or not msg:
                skipped_nomsg += 1
                continue
            fresh = pv_plan._halt_reason(msg)
            if fresh != reason:
                payload["reclassified_from"] = reason
                changes.append((rid, reason or "-", fresh, json.dumps(payload, ensure_ascii=False)))

        # 黙って飛ばすと「付け直し不要」と「判定できなかった」が区別できない
        # （2026-08-12 cgd Lv7・DS 指摘）。
        skipped = skipped_broken + skipped_nomsg
        if skipped:
            print(f"[pv usage] 判定できず飛ばした halt 行: {skipped} 件"
                  f"（detail 破損 {skipped_broken} / message 無し {skipped_nomsg}）")

        if not changes:
            print(f"[pv usage] {DB_PATH.name}: halt {len(rows)} 行中、付け直しが要るものはありません")
            return 0

        print(f"[pv usage] {DB_PATH.name}: {len(changes)} 行の reason が現行ルールと違います")
        for rid, old, new, _ in changes:
            print(f"    id={rid}  {old} → {new}")

        if not args.apply:
            print("\n  ※ 確認のみ（何も書き換えていません）。適用するには --apply を付けてください")
            return 0

        conn.executemany(
            "UPDATE pv_event SET reason = ?, detail = ? WHERE id = ?",
            [(new, det, rid) for rid, _old, new, det in changes],
        )
        conn.commit()
        print(f"\n  {len(changes)} 行を更新しました（元の値は detail.reclassified_from に保存）")
        return 0
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="pv の利用状況・不具合台帳")
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, helptext in (("summary", "全体の件数と期間"),
                           ("levels", "レベル別の使用回数（未実装での拒否も含む）"),
                           ("failures", "失敗・中止の一覧と分類"),
                           ("cost", "エンジン別・run 別の費用集計")):
        s2 = sub.add_parser(name, help=helptext)
        s2.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                        help="この日以降に絞る（cgd の report --since と同じ）")
        if name == "failures":
            s2.add_argument("--limit", type=int, default=20)

    r = sub.add_parser("record", help="Workflow 完了後の結果を記録する")
    r.add_argument("--run", required=True)
    r.add_argument("--level", type=int)
    r.add_argument("--outcome", required=True,
                   choices=("ok", "halt_missing_args", "halt_task_incomplete",
                            "halt_collect_failed", "halt_merge_failed", "other"))
    r.add_argument("--merge-model", default=None, help="実際に使われた統合モデル")
    r.add_argument("--duration-ms", type=int, default=None)
    r.add_argument("--tokens", type=int, default=None)
    r.add_argument("--detail", default=None, help="JSON か自由記述")

    sub.add_parser("backfill", help="既存 run の plan.json から過去の実行を復元する")

    rc = sub.add_parser("reclassify",
                        help="古い halt 行の reason を現行の分類ルールで付け直す")
    rc.add_argument("--apply", action="store_true",
                    help="実際に書き換える（既定は確認のみ）")

    p = sub.add_parser("purge", help="指定 run の記録を削除する（テスト行の掃除用）")
    p.add_argument("--run", required=True)

    args = ap.parse_args()
    return {
        "summary": _cmd_summary, "levels": _cmd_levels,
        "failures": _cmd_failures, "record": _cmd_record,
        "backfill": _cmd_backfill, "purge": _cmd_purge, "cost": _cmd_cost,
        "reclassify": _cmd_reclassify,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
