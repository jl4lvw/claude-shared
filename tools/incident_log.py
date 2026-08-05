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
def load_incidents(path: Path = INCIDENTS_PATH) -> list[dict]:
    """JSONL を読む。壊れた行は黙って捨てる (台帳全体を失わないため)。"""
    if not path.exists():
        return []
    out: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


def append_incident(incident: Incident, path: Path = INCIDENTS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(incident), ensure_ascii=False) + "\n")


def rewrite_incidents(records: list[dict], path: Path = INCIDENTS_PATH) -> None:
    """resolve 等で既存行を書き換える。原子的に差し替える。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def load_telemetry(path: Path = TELEMETRY_PATH) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


# --------------------------------------------------------------------------
# ID
# --------------------------------------------------------------------------
def issue_id(existing: list[dict], now: datetime | None = None) -> str:
    """INC-YYYYMMDD-xxxx。既存と重複しない 4 桁 hex を選ぶ。"""
    now = now or datetime.now()
    used = {str(r.get("id", "")) for r in existing}
    prefix = f"INC-{now.strftime('%Y%m%d')}-"
    for _ in range(200):
        candidate = prefix + f"{random.randrange(0x10000):04x}"
        if candidate not in used:
            return candidate
    raise RuntimeError("ID の発番に失敗しました（同日 200 回衝突）")


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
    telemetry = [r for r in load_telemetry() if not since or str(r.get("ts", "")) >= since]

    lines: list[str] = []
    span = f"（{since} 以降）" if since else "（全期間）"

    lines.append(f"=== 不具合台帳 {span} ===")
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

    lines.append(f"  全 {len(telemetry)} 呼出")
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
    records = load_incidents()
    hit = False
    for r in records:
        if r.get("id") == args.id:
            r["status"] = args.status
            r["resolved_at"] = datetime.now().strftime(_TS_FMT)
            r["resolution"] = args.resolution
            hit = True
            break
    if not hit:
        print(f"[incident] ID '{args.id}' は見つかりません", file=sys.stderr)
        return 1
    rewrite_incidents(records)
    print(f"[incident] {args.id} を {args.status} にしました")
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

    args = parser.parse_args()
    if args.command == "add":
        return _cmd_add(args)
    if args.command == "list":
        return _cmd_list(args)
    if args.command == "show":
        return _cmd_show(args)
    if args.command == "resolve":
        return _cmd_resolve(args)
    print(build_report(since=args.since))
    return 0


if __name__ == "__main__":
    sys.exit(main())
