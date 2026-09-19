"""skill_write_guard.py (PreToolUse: ユーザー階層への書込みを止める) のテスト.

なぜ要るか (2026-09-19): AGENTS.md に「置かない」と書くだけでは防げない
(worktree のセッションは古い AGENTS.md を読む・そもそも読まない AI もいる)。
5 月と 9 月に AI が「食い違いを手で揃える」形で二重管理を延命していた。
ここでは Write / Edit が止まること、止めすぎないこと (project 側・他のツール・他のディレクトリ)、
hook 自体の不具合で作業を止めないこと (fail-open) を固定する。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[2] / "hooks"
sys.path.insert(0, str(HOOKS))

import skill_write_guard as G  # noqa: E402


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    (h / ".claude" / "skills").mkdir(parents=True)
    (h / ".claude" / "commands").mkdir(parents=True)
    return h


def _payload(tool: str, key: str, path: str) -> dict:
    return {"tool_name": tool, "tool_input": {key: path}}


def _denied(out: dict | None) -> bool:
    return bool(out) and out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_write_under_personal_skills_is_denied(home):
    target = str(home / ".claude" / "skills" / "x" / "SKILL.md")
    out = G.handle_hook(_payload("Write", "file_path", target), home=home, project_dir=Path("Z:/none"))
    assert _denied(out)
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "skill-write-guard" in reason and "retire" in reason and target in reason


def test_forward_slash_path_is_also_denied(home):
    target = str(home / ".claude" / "skills" / "x" / "SKILL.md").replace("\\", "/")
    assert _denied(G.handle_hook(_payload("Edit", "file_path", target), home=home, project_dir=Path("Z:/none")))


def test_personal_commands_are_denied_too(home):
    target = str(home / ".claude" / "commands" / "x.md")
    assert _denied(G.handle_hook(_payload("MultiEdit", "file_path", target), home=home, project_dir=Path("Z:/none")))


def test_notebook_path_key_is_checked(home):
    target = str(home / ".claude" / "skills" / "x" / "a.ipynb")
    assert _denied(
        G.handle_hook(_payload("NotebookEdit", "notebook_path", target), home=home, project_dir=Path("Z:/none"))
    )


def test_project_side_is_allowed(home, tmp_path):
    project = tmp_path / "proj"
    target = str(project / ".claude" / "skills" / "x" / "SKILL.md")
    assert G.handle_hook(_payload("Write", "file_path", target), home=home, project_dir=project) is None


def test_other_directories_under_dot_claude_are_allowed(home):
    """memory など ~/.claude の他の場所は止めない。前方一致の罠 (skills_x) も。"""
    for sub in ("projects/C--x/memory/a.md", "skills_backup/a.md", "skillsX/a.md"):
        target = str(home / ".claude" / sub)
        assert G.handle_hook(_payload("Write", "file_path", target), home=home, project_dir=Path("Z:/none")) is None, sub


def test_bash_is_not_analysed(home):
    """シェル構文の解析は迂回と誤検知を同時に抱える。Bash は verify_sync / skill_freshness の検出に任せる。"""
    p = {"tool_name": "Bash", "tool_input": {"command": f'cp a "{home}/.claude/skills/x/SKILL.md"'}}
    assert G.handle_hook(p, home=home, project_dir=Path("Z:/none")) is None


def test_project_equal_to_home_is_not_blocked(home):
    """project がホーム直下そのものなら、ユーザー階層 = project 階層で、止めると正当な編集ができない。"""
    target = str(home / ".claude" / "skills" / "x" / "SKILL.md")
    assert G.handle_hook(_payload("Write", "file_path", target), home=home, project_dir=home) is None


def test_missing_or_garbage_input_is_ignored(home):
    assert G.handle_hook({"tool_name": "Write"}, home=home, project_dir=Path("Z:/none")) is None
    assert G.handle_hook({"tool_name": "Write", "tool_input": {"file_path": 123}}, home=home, project_dir=Path("Z:/none")) is None
    assert G.handle_hook({}, home=home, project_dir=Path("Z:/none")) is None


def _run(stdin: bytes, home: Path, project: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "USERPROFILE": str(home), "HOME": str(home), "CLAUDE_PROJECT_DIR": str(project)}
    return subprocess.run(
        [sys.executable, str(HOOKS / "skill_write_guard.py")],
        input=stdin, capture_output=True, env=env, timeout=30,
    )


def test_process_denies_with_json_on_stdout(home, tmp_path):
    """本番と同じ形 (プロセス起動・stdin の JSON・stdout の JSON) で確かめる。"""
    target = str(home / ".claude" / "skills" / "x" / "SKILL.md")
    payload = json.dumps(_payload("Write", "file_path", target)).encode("utf-8")
    r = _run(payload, home, tmp_path / "proj")
    assert r.returncode == 0
    out = json.loads(r.stdout.decode("utf-8"))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_process_fails_open_on_garbage(home, tmp_path):
    for stdin in (b"", b"not json", b"[]", b"\xff\xfe"):
        r = _run(stdin, home, tmp_path / "proj")
        assert r.returncode == 0 and r.stdout == b"", stdin
