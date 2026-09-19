"""スキル/コマンドの「実際に読み込まれるコピー」を 1 か所で解決する。

背景 (2026-09-19 の実事故):
  handoff スキルの project 版 (.claude/skills/handoff) を直して /g-ul し、verify_sync も
  exit 0 だったのに、新しいセッションでは修正が効かなかった。ユーザー階層
  (~/.claude/skills/handoff) に **修正前と同一の複製** があり、Claude Code は同名なら
  ユーザー階層を優先して project 側を **警告なしで隠す**ため。複製は 2026-04-20 から
  あり、以後 60 件超のセッション (すべて cwd=C:\\ClaudeCode) が複製の方を読んでいた。

  既存の検証は「.claude ↔ claude-shared ↔ origin の一致」しか見ず、
  skill_version_check.py は探索順が実際の優先順位と **逆** だった。
  「編集したコピー」と「実際に読まれるコピー」が一致するかを確かめる場所が
  どこにも無かったことが本当の原因である。

本モジュールの役割:
  - 優先順位 (このPC全体用 personal > このフォルダ専用 project) の実装を **ここ 1 つに集約**する
  - 同名が複数の場所にある「衝突」を検出する (内容が同一でも衝突。片方だけ直すと食い違うため)
  - 衝突したユーザー階層側を **削除せず退避**する (retire)

verify_sync.py / skill_version_check.py / skill_freshness.py は本モジュールの関数を呼ぶだけにする。
判定を 3 か所に書くと、ずれた瞬間に「ツールによって答えが違う」状態になる
(2026-09-19 の cgd Lv3 で 4 者が収束した指摘)。

使い方:
    python skill_scope.py check            # 衝突の検査 (衝突があれば exit 1)
    python skill_scope.py where <name>     # 実際に読まれるコピーを表示
    python skill_scope.py retire <name>    # ユーザー階層側を _retired_skills へ退避 (project 側が在るときだけ)

検査範囲は「このPC全体用」と「このフォルダ専用」の 2 つだけ。enterprise・plugin・worktree・
他 PC のユーザー階層・.agents/skills は見ない (UNCHECKED_NOTE)。この PC に enterprise
スコープが無いことは 2026-09-19 に確認済み。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

EXIT_OK = 0
EXIT_COLLISION = 1  # 衝突あり
EXIT_ENV = 2  # 前提が満たされない / 操作を中止した
EXIT_NOT_FOUND = 4  # where: 見つからない

PERSONAL = "personal"  # ~/.claude/skills … 優先される
PROJECT = "project"  # <project>/.claude/skills

# 優先順 (先頭が勝つ)。Claude Code の仕様: enterprise > personal > project
_PRIORITY = (PERSONAL, PROJECT)
_SCOPE_LABEL = {PERSONAL: "このPC全体用", PROJECT: "このフォルダ専用"}

RETIRED_DIRNAME = "_retired_skills"

# 検査していない範囲。「検証済み」を過大に読ませないため、出力へ必ず添える。
UNCHECKED_NOTE = "未検査: enterprise・plugin・worktree・他PCのユーザー階層・.agents/skills"


@dataclass(frozen=True)
class Entry:
    """1 つのスキル (SKILL.md) またはコマンド (*.md)。"""

    scope: str
    name: str
    kind: str  # "skill" | "command"
    path: Path  # SKILL.md または <name>.md

    @property
    def key(self) -> str:
        # Windows のファイルシステムは大小文字を区別しない。名前の照合は casefold で行う
        return self.name.casefold()

    @property
    def unit(self) -> Path:
        """退避の単位。skill はディレクトリごと、command はファイル 1 本。"""
        return self.path.parent if self.kind == "skill" else self.path


@dataclass(frozen=True)
class Collision:
    """同名のコピーが複数ある状態。entries[0] だけが実際に読み込まれる。"""

    name: str
    entries: tuple[Entry, ...]
    identical: bool  # 全コピーのバイト列が同一か

    @property
    def effective(self) -> Entry:
        return self.entries[0]

    @property
    def hidden(self) -> tuple[Entry, ...]:
        return self.entries[1:]


def default_project_dir() -> Path:
    """project のルート。CLAUDE_PROJECT_DIR > このファイルの位置 (<project>/.claude/tools/)。"""
    env = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent.parent


def default_home() -> Path:
    return Path.home()


def _claude_dir(scope: str, project_dir: Path, home: Path) -> Path:
    return (home if scope == PERSONAL else project_dir) / ".claude"


def list_entries(scope: str, project_dir: Path, home: Path) -> list[Entry]:
    """1 つのスコープにあるスキルとコマンドを列挙する (バックアップ類は対象外)。"""
    base = _claude_dir(scope, project_dir, home)
    out: list[Entry] = []
    skills = base / "skills"
    if skills.is_dir():
        for d in sorted(skills.iterdir(), key=lambda p: p.name.casefold()):
            f = d / "SKILL.md"
            if d.is_dir() and f.is_file():
                out.append(Entry(scope, d.name, "skill", f))
    commands = base / "commands"
    if commands.is_dir():
        for f in sorted(commands.glob("*.md"), key=lambda p: p.name.casefold()):
            if f.is_file():
                out.append(Entry(scope, f.stem, "command", f))
    return out


def collect(project_dir: Path | None = None, home: Path | None = None) -> dict[str, list[Entry]]:
    """名前 (casefold) → 優先順のコピー一覧。"""
    project_dir = project_dir or default_project_dir()
    home = home or default_home()
    by_key: dict[str, list[Entry]] = {}
    for scope in _PRIORITY:
        for entry in list_entries(scope, project_dir, home):
            by_key.setdefault(entry.key, []).append(entry)
    return by_key


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def find_collisions(
    project_dir: Path | None = None, home: Path | None = None
) -> list[Collision]:
    """同名が複数の (物理的に別の) ファイルにあるものを返す。

    同じファイルが 2 つのスコープから見えているだけ (project がホーム直下にある等) の
    場合は衝突ではない。バイト列が同一でも衝突として扱う: 片方だけ直した瞬間に
    食い違い、しかも直した側が隠れる、というのが今回の事故そのものだったため。
    """
    out: list[Collision] = []
    for _key, entries in sorted(collect(project_dir, home).items()):
        distinct: list[Entry] = []
        for e in entries:
            if not any(_same_file(e.path, d.path) for d in distinct):
                distinct.append(e)
        if len(distinct) < 2:
            continue
        digests = {_digest(e.path) for e in distinct}
        identical = len(digests) == 1 and None not in digests
        out.append(Collision(distinct[0].name, tuple(distinct), identical))
    return out


def resolve(
    name: str, project_dir: Path | None = None, home: Path | None = None
) -> Entry | None:
    """実際に読み込まれるコピー (優先順の先頭)。無ければ None。"""
    entries = collect(project_dir, home).get(name.casefold())
    return entries[0] if entries else None


def find_collision(
    name: str, project_dir: Path | None = None, home: Path | None = None
) -> Collision | None:
    key = name.casefold()
    return next((c for c in find_collisions(project_dir, home) if c.name.casefold() == key), None)


def retire(
    name: str,
    project_dir: Path | None = None,
    home: Path | None = None,
    now: datetime | None = None,
) -> Path:
    """ユーザー階層 (personal) 側の同名コピーを削除せず ~/.claude/_retired_skills/ へ退避する。

    project 側に同名が在るときだけ実行する。personal しか無いものを退避すると、
    唯一のコピーが読み込まれなくなるため。戻り値は退避先。
    """
    project_dir = project_dir or default_project_dir()
    home = home or default_home()
    entries = collect(project_dir, home).get(name.casefold(), [])
    personal = [e for e in entries if e.scope == PERSONAL]
    project = [e for e in entries if e.scope == PROJECT]
    if not personal:
        raise ValueError(f"「{name}」は{_SCOPE_LABEL[PERSONAL]}にありません (退避するものがありません)")
    if not project:
        raise ValueError(
            f"「{name}」は{_SCOPE_LABEL[PROJECT]}に無く、{_SCOPE_LABEL[PERSONAL]}にしかありません。"
            "退避すると唯一のコピーが読み込まれなくなるため中止します"
        )
    if any(_same_file(p.path, j.path) for p in personal for j in project):
        raise ValueError(f"「{name}」は同じファイルが両方のスコープから見えているだけです (退避不要)")

    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    root = home / ".claude" / RETIRED_DIRNAME
    dest = root / f"{personal[0].name}_{stamp}"
    n = 1
    while dest.exists():
        dest = root / f"{personal[0].name}_{stamp}_{n}"
        n += 1
    dest.mkdir(parents=True)
    for e in personal:
        target = dest / e.unit.name
        shutil.move(str(e.unit), str(target))
        if e.unit.exists() or not target.exists():
            raise RuntimeError(f"退避に失敗しました: {e.unit} -> {target}")
    return dest


def describe_collision(c: Collision) -> list[str]:
    """人が読む説明 (是正コマンド付き)。専門用語は避け、直し方を 1 行で添える。"""
    state = (
        "内容は同一 (いまは動くが、片方だけ直すと食い違い、直した側が隠れる)"
        if c.identical
        else "内容が違う (隠れている側の修正は反映されない)"
    )
    lines = [
        f"「{c.name}」が複数の場所にあります。読み込まれるのは先頭の 1 つだけです。{state}",
        f"    読まれる: {c.effective.path}  ({_SCOPE_LABEL[c.effective.scope]})",
    ]
    lines.extend(f"    隠れる  : {h.path}  ({_SCOPE_LABEL[h.scope]})" for h in c.hidden)
    if c.effective.scope == PERSONAL:
        script = Path(__file__).resolve()
        lines.append(
            f'    是正    : python "{script}" retire {c.name}'
            f"   ({_SCOPE_LABEL[PERSONAL]}の側を {RETIRED_DIRNAME} へ退避。削除はしない)"
        )
    else:
        lines.append("    是正    : 同じスコープ内の重複です。どちらかを手で整理してください")
    return lines


def _paths(args: argparse.Namespace) -> tuple[Path, Path]:
    project = Path(args.project_dir) if args.project_dir else default_project_dir()
    home = Path(args.home) if args.home else default_home()
    return project, home


def _cmd_check(args: argparse.Namespace) -> int:
    project, home = _paths(args)
    collisions = find_collisions(project, home)
    if not collisions:
        if not args.quiet:
            entries = [e for es in collect(project, home).values() for e in es]
            n_p = sum(1 for e in entries if e.scope == PERSONAL)
            n_j = sum(1 for e in entries if e.scope == PROJECT)
            print(
                f"[skill-scope] OK: 同名の重複はありません "
                f"({_SCOPE_LABEL[PERSONAL]} {n_p} 件 / {_SCOPE_LABEL[PROJECT]} {n_j} 件)"
            )
            print(f"  検査範囲: {_SCOPE_LABEL[PERSONAL]}・{_SCOPE_LABEL[PROJECT]}。{UNCHECKED_NOTE}")
        return EXIT_OK
    print(
        "[skill-scope] NG: 同名のスキル/コマンドが複数の場所にあります。"
        "優先されるのは「このPC全体用」で、project 側を直しても新しいセッションに届きません。",
        file=sys.stderr,
    )
    for c in collisions:
        for line in describe_collision(c):
            print(f"  {line}", file=sys.stderr)
    print(f"  検査範囲: {_SCOPE_LABEL[PERSONAL]}・{_SCOPE_LABEL[PROJECT]}。{UNCHECKED_NOTE}", file=sys.stderr)
    return EXIT_COLLISION


def _cmd_where(args: argparse.Namespace) -> int:
    project, home = _paths(args)
    entry = resolve(args.name, project, home)
    if entry is None:
        print(
            f"[skill-scope] 「{args.name}」は見つかりません "
            f"({_SCOPE_LABEL[PERSONAL]}・{_SCOPE_LABEL[PROJECT]}のどちらにも無い)",
            file=sys.stderr,
        )
        return EXIT_NOT_FOUND
    st = entry.path.stat()
    digest = (_digest(entry.path) or "?")[:10]
    print(f"[skill-scope] {entry.name}: 実際に読まれるのは「{_SCOPE_LABEL[entry.scope]}」のコピー")
    print(f"  {entry.path}")
    print(f"  sha256={digest} / {st.st_size} B / 更新 {datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M:%S}")
    collision = find_collision(args.name, project, home)
    if collision is not None:
        print("  注意: 同名の別コピーがあります:")
        for line in describe_collision(collision):
            print(f"  {line}")
    print(f"  {UNCHECKED_NOTE}")
    return EXIT_OK


def _cmd_retire(args: argparse.Namespace) -> int:
    project, home = _paths(args)
    try:
        dest = retire(args.name, project, home)
    except ValueError as exc:
        print(f"[skill-scope] 中止: {exc}", file=sys.stderr)
        return EXIT_ENV
    print(f"[skill-scope] 退避しました: {dest}")
    print(f"  元に戻すときは、その中の {args.name} を ~/.claude/skills/ へ戻してください (通常は不要です)")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="スキル/コマンドの実効コピーの解決と衝突検査")
    parser.add_argument("--project-dir", default=None, help="project のルート (既定: 自動判定)")
    parser.add_argument("--home", default=None, help="ユーザーのホーム (既定: 現在のユーザー)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="同名の重複を検査 (あれば exit 1)")
    p_check.add_argument("--quiet", action="store_true", help="重複が無いときは何も出さない")
    p_check.set_defaults(func=_cmd_check)

    p_where = sub.add_parser("where", help="実際に読まれるコピーを表示")
    p_where.add_argument("name")
    p_where.set_defaults(func=_cmd_where)

    p_retire = sub.add_parser("retire", help="ユーザー階層側の同名コピーを退避 (削除しない)")
    p_retire.add_argument("name")
    p_retire.set_defaults(func=_cmd_retire)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
