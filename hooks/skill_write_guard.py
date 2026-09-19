"""skill_write_guard.py — ユーザー階層 (~/.claude/skills, ~/.claude/commands) への書込みを止める PreToolUse hook。

2026-09-19: handoff スキルが「このPC全体用」(~/.claude/skills) と「このフォルダ専用」
(project の .claude/skills) の 2 か所にあり、Claude Code は同名だとユーザー階層を優先して
project 側を警告なしで隠すため、project 側だけ直した修正が新しいセッションに届かなかった。
複製は 2026-04-20 から放置され、2026-05-15 と 2026-09-19 には AI が「食い違いを手で揃える」
形で二重管理を延命していた。

AGENTS.md に「置かない」と書くだけでは防げない (worktree のセッションは古い AGENTS.md を読む・
そもそも読まない AI もいる)。そこで Write / Edit の書込みを機械的に止める。
Bash 経由の書込み (cp / python 等) は解析しない: シェル構文の解析は迂回と誤検知を同時に抱える
(cgd_wf_gate.py で 3 周の修正を経て断念した教訓)。そちらは verify_sync.py (/g-ul) と
skill_freshness.py (セッション開始時) の重複検出が受け持つ。

fail-open: hook 自体の不具合で作業を止めない (このリポジトリ全 hook 共通の規約)。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
_PATH_KEYS = ("file_path", "notebook_path", "path")
_PROTECTED_SUBDIRS = ("skills", "commands")

_MSYS_DRIVE_RE = re.compile(r"^/([A-Za-z])/(.*)$")


def _norm(path_str: str) -> str:
    """比較用に正規化する (~ 展開・Git Bash 形式・区切り・大小文字・リンクの解決)。"""
    p = os.path.expanduser(path_str.strip().strip('"'))
    m = _MSYS_DRIVE_RE.match(p)
    if m:  # /c/Users/... → C:/Users/...
        p = f"{m.group(1)}:/{m.group(2)}"
    return os.path.normcase(os.path.realpath(os.path.abspath(p)))


def _protected_roots(home: Path) -> tuple[str, ...]:
    return tuple(_norm(str(home / ".claude" / d)) for d in _PROTECTED_SUBDIRS)


def is_protected(path_str: str, home: Path | None = None) -> bool:
    """path がユーザー階層のスキル/コマンド置き場の配下か。"""
    home = home or Path.home()
    target = _norm(path_str)
    return any(target == root or target.startswith(root + os.sep) for root in _protected_roots(home))


def _project_is_home(home: Path, project_dir: Path | None) -> bool:
    """project がホーム直下そのものなら、ユーザー階層 = project 階層になり止められない。"""
    if project_dir is None:
        env = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
        if not env:
            return False
        project_dir = Path(env)
    return _norm(str(project_dir)) == _norm(str(home))


def handle_hook(
    payload: dict, home: Path | None = None, project_dir: Path | None = None
) -> dict | None:
    if payload.get("tool_name") not in _TOOLS:
        return None
    home = home or Path.home()
    if _project_is_home(home, project_dir):
        return None
    tool_input = payload.get("tool_input") or {}
    for key in _PATH_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip() and is_protected(value, home):
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        "[skill-write-guard] 「このPC全体用」の置き場 (~/.claude/skills, "
                        "~/.claude/commands) への書き込みは禁止です。\n"
                        "スキル/コマンドの置き場は、このプロジェクトの .claude/skills と "
                        ".claude/commands (このフォルダ専用) だけです。\n"
                        "理由: 同名がこのPC全体用にあると、そちらが優先されて project 側の修正が"
                        "新しいセッションに届かなくなります (2026-09-19 に handoff で実際に起きました)。\n"
                        f"対象: {value}\n"
                        "→ 編集先をプロジェクトの .claude/skills/<name>/SKILL.md に変えてください。"
                        "すでに同名がこのPC全体用にある場合は、同期せず "
                        "`python .claude/tools/skill_scope.py retire <name>` で退避してください。"
                    ),
                }
            }
    return None


def _run_hook() -> int:
    try:
        raw = sys.stdin.buffer.read()
    except OSError:
        return 0
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        out = handle_hook(payload)
    except Exception:
        return 0
    if out is not None:
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(_run_hook())
