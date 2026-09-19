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
import re
import sys
from pathlib import Path

try:
    import skill_scope  # 優先順位の実装はここに 1 つだけ持つ
except ImportError:  # pragma: no cover - tools/ は 1 セットでミラーされるので通常は起きない
    skill_scope = None

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

_STAMP_RE = re.compile(r"<!--\s*SKILL_VERSION:\s*([0-9A-Za-z_\-.]+)\s*-->")


def find_skill(name: str) -> Path | None:
    """実際にローダーが読むコピー (このPC全体用 ~/.claude > project) を返す。

    2026-09-19 まで探索順が [project → ユーザー階層] で、Claude Code の実際の優先順位と
    **逆**だった。ユーザー階層に古い複製があると、実際に読まれる古い版ではなく project の
    新しい版のスタンプを見て「OK」と誤判定した (handoff の事故)。
    優先順位の実装は skill_scope.py に 1 つだけ持ち、ここでは再実装しない。
    """
    if skill_scope is None:
        return None
    entry = skill_scope.resolve(name)
    return entry.path if entry else None


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

    if skill_scope is None:
        print("[UNKNOWN] skill_scope.py を読み込めません (/g-dl でツールを揃えてください)")
        return 4

    path = find_skill(args.skill)
    if path is None:
        print(f"[UNKNOWN] SKILL.md が見つかりません: {args.skill}")
        return 4

    collision = skill_scope.find_collision(args.skill)
    if collision is not None:
        print(f"[WARN] 同名のコピーが複数あります。読まれるのは {collision.effective.path} です")
        for line in skill_scope.describe_collision(collision)[1:]:
            print(line)

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
