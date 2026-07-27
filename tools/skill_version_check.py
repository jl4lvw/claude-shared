"""スキル定義の版ずれを機械的に判定する。

用途:
  スキル本文は `Skill` 呼び出し時点のスナップショットとして会話へ差し込まれ、
  以後ディスクを更新しても既存セッションの認識は古いまま固定される。
  Claude は「自分のコンテキストにある本文の SKILL_VERSION」を引数で渡し、
  本ツールがディスク上の現物と突き合わせて STALE / OK を返す。

使い方:
  python skill_version_check.py <skill-name> <自分のコンテキストのスタンプ>
  python skill_version_check.py cgd 2026-07-27_194451
  python skill_version_check.py cgd --show      # 現物のスタンプだけ表示

終了コード:
  0 = 一致 (OK) / --show
  3 = 不一致 (STALE — Read で読み直しが必要)
  4 = スタンプ無し・ファイル無し等の判定不能
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

_STAMP_RE = re.compile(r"<!--\s*SKILL_VERSION:\s*([0-9A-Za-z_\-.]+)\s*-->")


def _search_roots() -> tuple[Path, ...]:
    """探索ルート: CLAUDE_PROJECT_DIR > このファイルからの相対 > ユーザー階層。

    PC ごとにプロジェクトパスが異なる（他 PC へ配布する）ため、パスは
    決め打ちにせず環境変数と自身の設置位置から解決する。
    """
    roots: list[Path] = []
    env = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    if env:
        roots.append(Path(env) / ".claude" / "skills")
    # 本ファイルは <project>/.claude/tools/ に置かれる想定
    roots.append(Path(__file__).resolve().parent.parent / "skills")
    roots.append(Path.home() / ".claude" / "skills")
    # 重複除去（順序保持）
    seen: set[str] = set()
    uniq: list[Path] = []
    for r in roots:
        k = str(r).lower()
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    return tuple(uniq)


def find_skill(name: str) -> Path | None:
    for root in _search_roots():
        p = root / name / "SKILL.md"
        if p.is_file():
            return p
    return None


def read_stamp(path: Path) -> str | None:
    try:
        # スタンプは冒頭付近にある前提。全文読まず先頭 8KB だけ見る
        head = path.read_text(encoding="utf-8", errors="replace")[:8192]
    except OSError:
        return None
    m = _STAMP_RE.search(head)
    return m.group(1) if m else None


def main() -> int:
    ap = argparse.ArgumentParser(description="スキル定義の版ずれ判定")
    ap.add_argument("skill", help="スキル名 (例: cgd)")
    ap.add_argument("context_stamp", nargs="?", help="自分のコンテキストにあるスタンプ")
    ap.add_argument("--show", action="store_true", help="現物のスタンプを表示して終了")
    args = ap.parse_args()

    path = find_skill(args.skill)
    if path is None:
        print(f"[UNKNOWN] SKILL.md が見つかりません: {args.skill}")
        return 4

    disk = read_stamp(path)
    if disk is None:
        print(f"[UNKNOWN] スタンプ未設定: {path}")
        return 4

    if args.show or not args.context_stamp:
        print(f"[DISK] {args.skill} = {disk}  ({path})")
        return 0

    if args.context_stamp.strip() == disk:
        print(f"[OK] {args.skill} は最新です (stamp={disk})")
        return 0

    print(
        f"[STALE] {args.skill} の版がずれています\n"
        f"  コンテキスト: {args.context_stamp.strip()}\n"
        f"  ディスク現物: {disk}\n"
        f"  → Read で {path} を読み直してから続行してください"
    )
    return 3


if __name__ == "__main__":
    sys.exit(main())
