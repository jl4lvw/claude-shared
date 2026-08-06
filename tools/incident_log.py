"""外部 AI / Claude Code ハーネスの不具合台帳 — 追記・一覧・集計.

背景 (2026-08-05):
  Codex のトークン肥大、DeepSeek の空応答、ハーネスの "Output too large" など、
  外部ツール由来の問題はセッションごとにその場で対処されて消えていた。同じ日に
  独立した 2 セッションが同じ Lv7 の間違いを踏んでも、互いに気づけなかった。

  → 気づいたその場では「記録するだけ」にして、後日 `/incidents` の専用セッションで
     まとめて読み取り・解析する。本ツールはその蓄積側。

記録先:
  <project>/.claude/incidents/incidents.jsonl  — 人が書く事象 (1 件 1 行・共有する)
  <project>/.claude/incidents/telemetry.jsonl  — hook が自動記録する実測値
                                                 (ai_telemetry.py・端末ローカル)

JSONL を選んだ理由: append なので並行書込・複数拠点マージに強く、git diff が
人間可読で、壊れても 1 行単位で復旧できる (SQLite はバイナリのため /g-ul の
git 同期で衝突すると復旧が難しい)。

使い方:
    python incident_log.py add --tool codex --category token \\
        --title "Lv7 inline で 1 回 8 万 tokens" --detail "..." --evidence <path>
    python incident_log.py list [--status open] [--tool codex] [--since YYYY-MM-DD]
    python incident_log.py show INC-20260805-a3f2
    python incident_log.py resolve INC-20260805-a3f2 --resolution "..."
    python incident_log.py report [--since YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

_PROJECT = Path(os.environ.get("CLAUDE_PROJECT_DIR", r"C:/ClaudeCode"))
INCIDENTS_DIR = Path(os.environ.get("INCIDENTS_DIR", str(_PROJECT / ".claude" / "incidents")))
INCIDENTS_PATH = INCIDENTS_DIR / "incidents.jsonl"
TELEMETRY_PATH = INCIDENTS_DIR / "telemetry.jsonl"

# 対象範囲: 外部 AI + Claude Code ハーネス (2026-08-05 ユーザー決定)
KNOWN_TOOLS = ("codex", "deepseek", "qwen", "gemini", "harness", "claude-code", "other")
KNOWN_CATEGORIES = (
    "token",       # トークン/クォータ肥大
    "output",      # 出力肥大・切り詰め・欠落
    "auth",        # 認証・鍵
    "empty",       # 空応答・無言失敗
    "wrong",       # 誤った結果・誤対象
    "workflow",    # 手順・導線の問題 (WF 未使用など)
    "perf",        # 遅延・タイムアウト
    "other",
)
SEVERITIES = ("low", "mid", "high")
STATUSES = ("open", "resolved", "wontfix")

_TS_FMT = "%Y-%m-%d %H:%M:%S"


@dataclass
class Incident:
    """台帳 1 件。JSONL の 1 行に対応する。"""

    id: str
    ts: str
    tool: str
    category: str
    severity: str
    title: str
    detail: str = ""
    evidence: list[str] = field(default_factory=list)
    session: str = ""
    status: str = "open"
    resolved_at: str = ""
    resolution: str = ""


# --------------------------------------------------------------------------
# 入出力
# --------------------------------------------------------------------------
def load_events(path: Path = INCIDENTS_PATH) -> tuple[list[dict], list[str]]:
    """JSONL を生イベント列として読む。(events, corrupt_lines) を返す。

    壊れた行は **捨てずに** corrupt_lines として返す。黙って捨てると、
    resolve のたびに全書き換えする設計と組み合わさって永久消失するため
    (Lv8 レビュー指摘)。呼び出し側は件数を必ず警告表示する。
    """
    if not path.exists():
        return [], []
    events: list[dict] = []
    corrupt: list[str] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                corrupt.append(line)
                continue
            if isinstance(obj, dict):
                events.append(obj)
            else:
                corrupt.append(line)
    return events, corrupt


def fold_events(events: list[dict]) -> list[dict]:
    """イベント列を台帳の現在状態に畳み込む。

    `type` 無し / `type=="incident"` が本体、`type=="resolve"` が状態更新イベント。
    resolve を「全書き換え」ではなく追記で表現することで、load→rewrite の
    区間に別プロセスが append した分が消える競合を構造的に無くしている。
    """
    by_id: dict[str, dict] = {}
    order: list[str] = []
    pending: list[dict] = []
    for ev in events:
        kind = ev.get("type", "incident")
        ident = str(ev.get("id", ""))
        if kind == "resolve":
            pending.append(ev)
            continue
        if ident and ident not in by_id:
            order.append(ident)
        by_id[ident] = dict(ev)
    for ev in pending:
        target = by_id.get(str(ev.get("id", "")))
        if target is None:
            continue
        target["status"] = ev.get("status", "resolved")
        target["resolved_at"] = ev.get("ts", "")
        target["resolution"] = ev.get("resolution", "")
    return [by_id[i] for i in order if i in by_id]


def load_incidents(path: Path = INCIDENTS_PATH, warn: bool = True) -> list[dict]:
    """台帳の現在状態を返す。壊れた行があれば stderr に件数を警告する。"""
    events, corrupt = load_events(path)
    if corrupt and warn:
        print(
            f"[incident] WARN: 壊れた行が {len(corrupt)} 件あります（読み飛ばしましたが削除はしていません）。"
            f"\n           {path} を確認してください。",
            file=sys.stderr,
        )
    return fold_events(events)


def append_incident(incident: Incident, path: Path = INCIDENTS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(incident), ensure_ascii=False) + "\n")


def append_event(event: dict, path: Path = INCIDENTS_PATH) -> None:
    """状態更新イベント (resolve 等) を追記する。全書き換えしない。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def rewrite_incidents(records: list[dict], path: Path = INCIDENTS_PATH) -> None:
    """台帳を全書き換えする **保守専用** 経路。通常運用では呼ばない。

    resolve は追記イベント方式に変えたので、日常操作で全書き換えは発生しない
    (load→rewrite の区間に別プロセスが append した分を消す競合があったため)。
    破損行が残っている状態で全書き換えすると復旧不能になるので禁止する。
    """
    _, corrupt = load_events(path)
    if corrupt:
        raise RuntimeError(
            f"壊れた行が {len(corrupt)} 件あるため全書き換えを中止しました。"
            "先に手作業で修復してください（全書き換えすると復旧できません）。"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    # 固定 tmp 名は並行実行で衝突するのでユニーク化する
    tmp = path.with_name(f"{path.stem}.{os.getpid()}.{random.randrange(0x10000):04x}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def load_telemetry(path: Path = TELEMETRY_PATH) -> tuple[list[dict], int]:
    """(records, 壊れた行数) を返す。破損を黙って捨てると計測が信用できなくなる。

    `type=="backfill"` の行は後埋めイベントとして、同じ `ts` の計測行へ畳み込む
    (PostToolUse hook は巨大出力の末尾を受け取れず tokens を取り逃すため、
     生ログから後から補う。全書き換えせず追記で表現するのは台帳と同じ理由)。
    """
    events, corrupt = load_events(path)
    records = [e for e in events if e.get("type") != "backfill"]
    by_ts: dict[str, dict] = {str(r.get("ts")): r for r in records}
    for ev in events:
        if ev.get("type") != "backfill":
            continue
        target = by_ts.get(str(ev.get("ts")))
        if target is None:
            continue
        for key in ("tokens_used", "exec_calls", "raw_log_path", "raw_log_bytes", "match"):
            if ev.get(key) is not None:
                target[key] = ev[key]
        flags = [f for f in (target.get("flags") or []) if f != "tokens_unmeasured"]
        if ev.get("tokens_used") and int(ev["tokens_used"]) >= 80_000:
            if "tokens_high" not in flags:
                flags.append("tokens_high")
        target["flags"] = flags
        target["backfilled"] = True
    return records, len(corrupt)


# --------------------------------------------------------------------------
# 後埋め (backfill) — hook が取り逃した実測値を生ログから回収する
# --------------------------------------------------------------------------
_RAW_TOKENS_RE = re.compile(r"^tokens used\s*$\r?\n\s*([\d,]+)", re.MULTILINE)
_RAW_TOKENS_INLINE_RE = re.compile(r"^tokens used[:\s]+([\d,]+)\s*$", re.MULTILINE)
_RAW_EXEC_RE = re.compile(r"^exec\b", re.MULTILINE)

PROJECTS_DIR = Path(
    os.environ.get("CLAUDE_PROJECTS_DIR", str(Path.home() / ".claude" / "projects" / "C--ClaudeCode"))
)


def parse_raw_log(path: Path) -> dict | None:
    """Codex の生ログから tokens used と exec 回数を取り出す。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    hits = [(m.start(), m.group(1)) for m in _RAW_TOKENS_RE.finditer(text)]
    hits += [(m.start(), m.group(1)) for m in _RAW_TOKENS_INLINE_RE.finditer(text)]
    if not hits:
        return None
    try:
        tokens = int(max(hits, key=lambda h: h[0])[1].replace(",", ""))
    except ValueError:
        return None
    return {
        "tokens_used": tokens,
        "exec_calls": len(_RAW_EXEC_RE.findall(text)),
        "raw_log_path": str(path),
        "raw_log_bytes": path.stat().st_size,
    }


def backfill_telemetry(
    window_sec: int = 300,
    projects_dir: Path = PROJECTS_DIR,
    telemetry_path: Path = TELEMETRY_PATH,
    dry_run: bool = False,
) -> list[dict]:
    """tokens 欠測の codex 行を、時刻が近い生ログと突き合わせて後埋めする。

    hook は巨大出力を先頭 30KB 程度しか受け取れず、末尾の `tokens used` を
    取り逃す。生ログ (tool-results/*.txt) には全文が残っているので、
    記録時刻の近さでマッチさせて実測値を回収する。
    """
    records, _ = load_telemetry(telemetry_path)
    missing = [
        r for r in records
        if r.get("tool") == "codex" and not r.get("tokens_used") and r.get("ts")
    ]
    if not missing:
        return []

    candidates: list[tuple[float, Path]] = []
    for tr in projects_dir.glob("*/tool-results/*.txt"):
        try:
            candidates.append((tr.stat().st_mtime, tr))
        except OSError:
            continue

    filled: list[dict] = []
    used: set[str] = set()
    for row in missing:
        try:
            ts = datetime.strptime(str(row["ts"]), _TS_FMT).timestamp()
        except ValueError:
            continue
        # 入力ファイル名が生ログ中に出てくるかで照合する。時刻の近さだけで
        # 結び付けると別の呼出の数値を書き込みかねない（測れない数字より
        # 間違った数字の方が有害）。
        marker = Path(str(row.get("input_path") or "")).name or None
        near = sorted(
            (c for c in candidates if abs(c[0] - ts) <= window_sec and str(c[1]) not in used),
            key=lambda c: abs(c[0] - ts),
        )
        strong: dict | None = None
        weak: dict | None = None
        for _, path in near:
            parsed = parse_raw_log(path)
            if parsed is None:
                continue
            if marker:
                try:
                    hit = marker in path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    hit = False
                if hit:
                    strong = {**parsed, "match": "input_path"}
                    break
            if weak is None:
                weak = {**parsed, "match": "mtime_only"}
        chosen = strong or weak
        if chosen is None:
            continue
        used.add(str(chosen["raw_log_path"]))
        filled.append({"type": "backfill", "ts": row["ts"], **chosen})

    if filled and not dry_run:
        for ev in filled:
            append_event(ev, telemetry_path)
    return filled


# --------------------------------------------------------------------------
# ID
# --------------------------------------------------------------------------
def issue_id(existing: list[dict] | None = None, now: datetime | None = None) -> str:
    """INC-YYYYMMDD-<hhmmss><rand6>。時刻 + 乱数で、既存一覧に依存せず一意にする。

    旧実装は「読み込んだ一覧に無い 4 桁 hex」を選ぶスナップショット依存で、
    並行 add で衝突しえた (Lv8 レビュー指摘)。時刻(秒) + 24bit 乱数にして
    全走査なしで実用上一意にする。existing は互換のため受け取るが、
    渡された場合のみ追加の衝突チェックに使う。
    """
    now = now or datetime.now()
    used = {str(r.get("id", "")) for r in (existing or [])}
    for _ in range(50):
        candidate = f"INC-{now.strftime('%Y%m%d')}-{now.strftime('%H%M%S')}{random.randrange(0x1000000):06x}"
        if candidate not in used:
            return candidate
        now = datetime.now()
    raise RuntimeError("ID の発番に失敗しました（50 回衝突）")


# --------------------------------------------------------------------------
# 集計
# --------------------------------------------------------------------------
def _tally(values: list[str]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def build_report(since: str | None = None) -> str:
    incidents = [r for r in load_incidents() if not since or str(r.get("ts", "")) >= since]
    tele_all, tele_corrupt = load_telemetry()
    telemetry = [r for r in tele_all if not since or str(r.get("ts", "")) >= since]
    _, inc_corrupt = load_events()

    lines: list[str] = []
    span = f"（{since} 以降）" if since else "（全期間）"

    lines.append(f"=== 不具合台帳 {span} ===")
    if inc_corrupt:
        lines.append(f"  ⚠️ 壊れた行 {len(inc_corrupt)} 件（読み飛ばし・削除はしていない）")
    if not incidents:
        lines.append("  記録なし")
    else:
        open_items = [r for r in incidents if r.get("status") == "open"]
        lines.append(f"  全 {len(incidents)} 件 / 未対応 open {len(open_items)} 件")
        lines.append("  [tool 別]      " + ", ".join(f"{k}:{n}" for k, n in _tally([str(r.get("tool", "?")) for r in incidents])))
        lines.append("  [category 別]  " + ", ".join(f"{k}:{n}" for k, n in _tally([str(r.get("category", "?")) for r in incidents])))
        lines.append("  [severity 別]  " + ", ".join(f"{k}:{n}" for k, n in _tally([str(r.get("severity", "?")) for r in incidents])))
        if open_items:
            lines.append("")
            lines.append("  未対応 open:")
            for r in open_items:
                lines.append(f"    {r.get('id')} [{r.get('severity')}/{r.get('tool')}/{r.get('category')}] {r.get('title')}")

    lines.append("")
    lines.append(f"=== 自動テレメトリ {span} ===")
    if not telemetry:
        lines.append("  記録なし（hook 未登録か、まだ外部 AI を呼んでいない）")
        return "\n".join(lines)

    lines.append(f"  全 {len(telemetry)} 呼出" + (f"  ⚠️ 壊れた行 {tele_corrupt} 件" if tele_corrupt else ""))
    lines.append("  [tool 別]  " + ", ".join(f"{k}:{n}" for k, n in _tally([str(r.get("tool", "?")) for r in telemetry])))

    all_flags = [f for r in telemetry for f in (r.get("flags") or [])]
    lines.append("  [flags]    " + (", ".join(f"{k}:{n}" for k, n in _tally(all_flags)) if all_flags else "なし"))

    codex = [r for r in telemetry if r.get("tool") == "codex"]
    tokens = [int(r["tokens_used"]) for r in codex if isinstance(r.get("tokens_used"), int)]
    if tokens:
        lines.append(
            f"  [codex tokens] {len(tokens)} 回 / 合計 {sum(tokens):,} / "
            f"平均 {sum(tokens) // len(tokens):,} / 最大 {max(tokens):,}"
        )
    wf = [r for r in telemetry if r.get("tool") == "codex" and r.get("via_workflow")]
    if codex:
        lines.append(f"  [codex 経路]  WF 経由 {len(wf)} / inline {len(codex) - len(wf)}")

    execs = [int(r["exec_calls"]) for r in telemetry if isinstance(r.get("exec_calls"), int)]
    if execs:
        lines.append(f"  [codex 探索]  記録 {len(execs)} 回 / 平均 {sum(execs)/len(execs):.1f} exec/回 / 最大 {max(execs)}")
    unmeasured = [r for r in telemetry if r.get("tool") == "codex" and not r.get("tokens_used")]
    if unmeasured:
        lines.append(f"  ⚠️ tokens 未計測の codex 呼出 {len(unmeasured)} 件 → `incident_log.py backfill` で回収できます")

    worst = sorted(
        (r for r in telemetry if isinstance(r.get("tokens_used"), int)),
        key=lambda r: -int(r["tokens_used"]),
    )[:5]
    if worst:
        lines.append("")
        lines.append("  トークン消費の上位:")
        for r in worst:
            flags = ",".join(r.get("flags") or []) or "-"
            lines.append(
                f"    {r.get('ts')} {r.get('tool')}({r.get('effort') or '-'}) "
                f"{int(r['tokens_used']):,} tok / out {r.get('out_bytes', 0):,}B / {flags}"
            )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _cmd_add(args: argparse.Namespace) -> int:
    if args.tool not in KNOWN_TOOLS:
        print(f"[incident] WARN: 未知の tool '{args.tool}'（そのまま記録します）", file=sys.stderr)
    if args.category not in KNOWN_CATEGORIES:
        print(f"[incident] WARN: 未知の category '{args.category}'（そのまま記録します）", file=sys.stderr)

    existing = load_incidents()
    incident = Incident(
        id=issue_id(existing),
        ts=datetime.now().strftime(_TS_FMT),
        tool=args.tool,
        category=args.category,
        severity=args.severity,
        title=args.title,
        detail=args.detail or "",
        evidence=list(args.evidence or []),
        session=args.session or "",
    )
    append_incident(incident)
    print(f"[incident] 記録: {incident.id} [{incident.severity}/{incident.tool}/{incident.category}] {incident.title}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    records = load_incidents()
    if args.status:
        records = [r for r in records if r.get("status") == args.status]
    if args.tool:
        records = [r for r in records if r.get("tool") == args.tool]
    if args.since:
        records = [r for r in records if str(r.get("ts", "")) >= args.since]
    if not records:
        print("[incident] 該当なし")
        return 0
    for r in records:
        mark = {"open": "🔴", "resolved": "✅", "wontfix": "⚪"}.get(str(r.get("status")), "?")
        print(f"{mark} {r.get('id')}  {r.get('ts')}  [{r.get('severity')}/{r.get('tool')}/{r.get('category')}]  {r.get('title')}")
    print(f"\n計 {len(records)} 件")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    for r in load_incidents():
        if r.get("id") == args.id:
            print(json.dumps(r, ensure_ascii=False, indent=2))
            return 0
    print(f"[incident] ID '{args.id}' は見つかりません", file=sys.stderr)
    return 1


def _cmd_resolve(args: argparse.Namespace) -> int:
    """状態更新を **追記** する。全書き換えしないので並行 add と競合しない。"""
    if not any(r.get("id") == args.id for r in load_incidents()):
        print(f"[incident] ID '{args.id}' は見つかりません", file=sys.stderr)
        return 1
    append_event({
        "type": "resolve",
        "id": args.id,
        "ts": datetime.now().strftime(_TS_FMT),
        "status": args.status,
        "resolution": args.resolution,
    })
    print(f"[incident] {args.id} を {args.status} にしました")
    return 0


def _cmd_backfill(args: argparse.Namespace) -> int:
    filled = backfill_telemetry(window_sec=args.window_sec, dry_run=args.dry_run)
    if not filled:
        print("[incident] 後埋めできる欠測はありませんでした")
        return 0
    print(f"[incident] {len(filled)} 件を{'（dry-run）' if args.dry_run else ''}後埋め:")
    for ev in filled:
        mark = "✅照合" if ev.get("match") == "input_path" else "⚠️時刻のみ"
        print(f"  {ev['ts']}  {ev['tokens_used']:>8,} tok  exec {ev['exec_calls']:>3} 回"
              f"  {mark}  ({ev['raw_log_bytes']:,} bytes)")
    weak = [e for e in filled if e.get("match") != "input_path"]
    if weak:
        print("")
        print(f"  ⚠️ {len(weak)} 件は入力ファイル名で照合できず、時刻の近さだけで結び付けています。")
        print("     数値を根拠にする前に raw_log_path を目視確認してください。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="外部 AI / ハーネス不具合台帳")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="事象を 1 件記録する")
    p_add.add_argument("--tool", required=True, help=f"候補: {', '.join(KNOWN_TOOLS)}")
    p_add.add_argument("--category", required=True, help=f"候補: {', '.join(KNOWN_CATEGORIES)}")
    p_add.add_argument("--title", required=True, help="1 行要約")
    p_add.add_argument("--severity", default="mid", choices=SEVERITIES)
    p_add.add_argument("--detail", default="", help="再現条件・観測値・仮説")
    p_add.add_argument("--evidence", action="append", help="証拠のパス/URL（複数可）")
    p_add.add_argument("--session", default="", help="発生セッション ID（先頭 8 桁で可）")

    p_list = sub.add_parser("list", help="一覧表示")
    p_list.add_argument("--status", choices=STATUSES)
    p_list.add_argument("--tool")
    p_list.add_argument("--since", help="YYYY-MM-DD 以降")

    p_show = sub.add_parser("show", help="1 件の全文表示")
    p_show.add_argument("id")

    p_res = sub.add_parser("resolve", help="対応済みにする")
    p_res.add_argument("id")
    p_res.add_argument("--resolution", required=True, help="何をして解決したか")
    p_res.add_argument("--status", default="resolved", choices=("resolved", "wontfix"))

    p_rep = sub.add_parser("report", help="台帳 + テレメトリの集計")
    p_rep.add_argument("--since", help="YYYY-MM-DD 以降")

    p_bf = sub.add_parser("backfill", help="tokens 欠測のテレメトリを生ログから後埋めする")
    p_bf.add_argument("--window-sec", type=int, default=300, help="時刻マッチの許容幅（既定 300 秒）")
    p_bf.add_argument("--dry-run", action="store_true", help="書き込まずに結果だけ表示")

    args = parser.parse_args()
    if args.command == "add":
        return _cmd_add(args)
    if args.command == "list":
        return _cmd_list(args)
    if args.command == "show":
        return _cmd_show(args)
    if args.command == "resolve":
        return _cmd_resolve(args)
    if args.command == "backfill":
        return _cmd_backfill(args)
    print(build_report(since=args.since))
    return 0


if __name__ == "__main__":
    sys.exit(main())
