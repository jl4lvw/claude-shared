"""Memory MCP の memory.json に、サブプロジェクト間のHTTP依存関係を自動検出して反映する。

背景:
  Memory MCPは会話駆動でしか更新されない(手動記録 or Claudeの自発判断)。
  コードが変わっても自動では追随しないため、モジュール依存関係のような
  「構造的事実」だけは静的解析で定期的に再生成し、陳腐化を防ぐ。

  ただし本リポジトリはモノレポで、サブプロジェクト間の依存は
  Pythonのimportではなく HTTP 呼び出し(127.0.0.1:<port>) で行われる
  (022/042 は 023 を別プロセスのAPIとして呼ぶ)。そのため
  .claude/launch.json のポート定義とソースコード中のポート参照を
  突き合わせて依存辺を検出する。

安全設計:
  - 手動で作成したエンティティ/リレーションは一切変更しない
  - 自動検出したリレーションだけに relationType="http_calls_detected" という
    専用の目印を付け、再実行のたびにその型のリレーションだけを入れ替える
    (手動リレーションは別の relationType なので巻き込まれない)
  - 検出したがまだ存在しないサブプロジェクトのエンティティは
    entityType="Module", 既存の観察に "[auto:module_graph]" マーカーを持つ
    形で追加する。**既に手動で存在するエンティティ名と衝突する場合は
    新規作成しない**(手動エンティティの上書き禁止)

使い方:
  python .claude/tools/refresh_module_graph.py            # 反映する
  python .claude/tools/refresh_module_graph.py --dry-run  # 差分表示のみ
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

REPO_ROOT = Path(r"C:/ClaudeCode")
LAUNCH_JSON = REPO_ROOT / ".claude" / "launch.json"
MEMORY_JSON = REPO_ROOT / "900.ClaudeCode" / "mcp-memory-3d-viewer" / "memory.json"

AUTO_RELATION_TYPE = "http_calls_detected"
AUTO_MARKER = "[auto:module_graph]"

_SUBPROJECT_PATH_RE = re.compile(r"C:/ClaudeCode/(\d{3}\.[^/\"]+)", re.IGNORECASE)
_HOST_PORT_RE = re.compile(r"(?:127\.0\.0\.1|localhost):(\d{4,5})")

# 単一の凝集したモジュールではないフォルダ(グラフに載せると実態を誤って表現する)。
# 000.tmp: 一時ファイル/バックアップ置き場。001.Python: 無関係な個別スクリプトの寄せ集め。
_EXCLUDE_FOLDERS = {"000.tmp", "001.Python"}


def _load_port_map() -> dict[int, str]:
    """launch.json から {port: サブプロジェクトフォルダ名} を作る。"""
    data = json.loads(LAUNCH_JSON.read_text(encoding="utf-8"))
    port_map: dict[int, str] = {}
    for cfg in data.get("configurations", []):
        port = cfg.get("port")
        if not port:
            continue
        args_joined = " ".join(str(a) for a in cfg.get("runtimeArgs", []))
        m = _SUBPROJECT_PATH_RE.search(args_joined)
        if m:
            port_map[int(port)] = m.group(1)
    return port_map


def _list_subproject_folders() -> list[Path]:
    return sorted(
        p for p in REPO_ROOT.iterdir()
        if p.is_dir() and re.match(r"^\d{3}\.", p.name) and p.name not in _EXCLUDE_FOLDERS
    )


def _scan_dependencies(port_map: dict[int, str]) -> set[tuple[str, str]]:
    """(呼び出し元フォルダ, 呼び出し先フォルダ) の集合を返す。自己参照は除外。"""
    edges: set[tuple[str, str]] = set()
    for folder in _list_subproject_folders():
        caller = folder.name
        for ext in ("*.py", "*.js"):
            for src in folder.rglob(ext):
                if any(part in ("node_modules", ".venv", "__pycache__") for part in src.parts):
                    continue
                try:
                    text = src.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                for port_str in _HOST_PORT_RE.findall(text):
                    port = int(port_str)
                    callee = port_map.get(port)
                    if callee and callee != caller and callee not in _EXCLUDE_FOLDERS:
                        edges.add((caller, callee))
    return edges


def _load_memory_lines() -> list[dict]:
    if not MEMORY_JSON.exists():
        return []
    lines = []
    for line in MEMORY_JSON.read_text(encoding="utf-8").split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            lines.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return lines


def build_updated_records(
    existing: list[dict], edges: set[tuple[str, str]]
) -> tuple[list[dict], dict]:
    """既存レコードから自動生成分だけ取り除き、新しい検出結果を足す。手動分は不変。"""
    def _is_auto(r: dict) -> bool:
        if r.get("type") == "relation" and r.get("relationType") == AUTO_RELATION_TYPE:
            return True
        if r.get("type") == "entity" and any(AUTO_MARKER in o for o in r.get("observations", [])):
            return True
        return False

    manual = [r for r in existing if not _is_auto(r)]
    existing_entity_names = {r["name"] for r in manual if r.get("type") == "entity"}

    new_entities: list[dict] = []
    seen_new_names: set[str] = set()
    for caller, callee in edges:
        for name in (caller, callee):
            if name in existing_entity_names or name in seen_new_names:
                continue
            new_entities.append(
                {
                    "type": "entity",
                    "name": name,
                    "entityType": "Module",
                    "observations": [f"{AUTO_MARKER} launch.jsonのポート定義とソース中のURL参照から自動検出"],
                }
            )
            seen_new_names.add(name)

    new_relations = [
        {"type": "relation", "from": caller, "to": callee, "relationType": AUTO_RELATION_TYPE}
        for caller, callee in sorted(edges)
    ]

    updated = manual + new_entities + new_relations
    summary = {
        "manual_kept": len(manual),
        "auto_entities_added": len(new_entities),
        "auto_relations": len(new_relations),
    }
    return updated, summary


def main() -> int:
    ap = argparse.ArgumentParser(description="サブプロジェクト間HTTP依存をmemory.jsonへ自動反映")
    ap.add_argument("--dry-run", action="store_true", help="書き込まず結果だけ表示")
    args = ap.parse_args()

    if not LAUNCH_JSON.exists():
        print(f"[ERROR] launch.json が見つかりません: {LAUNCH_JSON}")
        return 1

    port_map = _load_port_map()
    print(f"launch.json から {len(port_map)} 件のポート定義を読み込みました")

    edges = _scan_dependencies(port_map)
    print(f"検出した依存関係: {len(edges)} 件")
    for caller, callee in sorted(edges):
        print(f"  {caller}  ->  {callee}")

    existing = _load_memory_lines()
    updated, summary = build_updated_records(existing, edges)

    print(
        f"\n手動エンティティ/リレーション: {summary['manual_kept']} 件(維持)"
        f" / 自動追加エンティティ: {summary['auto_entities_added']} 件"
        f" / 自動リレーション: {summary['auto_relations']} 件"
    )

    if args.dry_run:
        print("\n--dry-run のため書き込みません。")
        return 0

    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in updated)
    MEMORY_JSON.write_text(body + "\n" if body else "", encoding="utf-8", newline="")
    print(f"\n{MEMORY_JSON} を更新しました。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
