"""skill_scope を使う 3 つの既存ツール (verify_sync / skill_version_check / skill_freshness) のテスト.

なぜ要るか (2026-09-19): 「編集したコピー」と「実際に読まれるコピー」の一致を確かめる場所が
どこにも無かった。優先順位の実装を skill_scope.py の 1 か所に集約し、3 つのツールは呼ぶだけに
したので、ここでは **3 者が同じ答えを返す**ことと、それぞれの出力が事故を防げる形であることを固定する。

  - verify_sync        : /g-ul の完了条件。重複があれば exit 1 の原因として報告する
  - skill_version_check: cgd Step 0 の版照合。以前は探索順が逆で、古い複製を見逃した
  - skill_freshness    : セッション開始時の警告 (プロセスを起動して本番と同じ形で確かめる)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
HOOKS = TOOLS.parent / "hooks"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(HOOKS))

import skill_freshness as F  # noqa: E402
import skill_scope as S  # noqa: E402
import skill_version_check as V  # noqa: E402
import verify_sync as VS  # noqa: E402


def _skill(base: Path, name: str, body: str = "body") -> Path:
    d = base / ".claude" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    f = d / "SKILL.md"
    f.write_text(body, encoding="utf-8")
    return f


@pytest.fixture()
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    project, home = tmp_path / "proj", tmp_path / "home"
    project.mkdir()
    home.mkdir()
    return project, home


# ---- verify_sync ----------------------------------------------------------------------------


def test_verify_sync_clean_has_no_scope_problem(dirs):
    project, home = dirs
    _skill(project, "handoff")
    assert VS.check_scope_collisions(project / ".claude", home) == []


def test_verify_sync_reports_the_shadow_with_a_remedy(dirs):
    project, home = dirs
    _skill(project, "handoff", "new")
    _skill(home, "handoff", "old")
    (problem,) = VS.check_scope_collisions(project / ".claude", home)
    assert "handoff" in problem and "retire handoff" in problem and "読まれる" in problem


def test_verify_sync_coverage_note_does_not_overclaim():
    """exit 0 を「新しいセッションで動く」保証と読ませない (2026-09-19 の誤解の再発防止)。"""
    assert "検証範囲" in VS.COVERAGE_NOTE
    assert "未検証" in VS.COVERAGE_NOTE
    assert "worktree" in VS.COVERAGE_NOTE
    assert "動く" in VS.COVERAGE_NOTE and "確かめていません" in VS.COVERAGE_NOTE


# ---- skill_version_check ---------------------------------------------------------------------


def test_version_check_looks_at_the_copy_that_actually_loads(dirs, monkeypatch):
    """回帰: 探索順が [project → personal] で逆だったため、古い personal を見逃して OK と返していた。"""
    project, home = dirs
    _skill(project, "handoff", "<!-- SKILL_VERSION: 2026-09-19_new -->")
    personal = _skill(home, "handoff", "<!-- SKILL_VERSION: 2026-05-15_old -->")
    monkeypatch.setattr(S, "default_project_dir", lambda: project)
    monkeypatch.setattr(S, "default_home", lambda: home)
    assert V.find_skill("handoff") == personal
    assert V.read_stamp(V.find_skill("handoff")) == "2026-05-15_old"


def test_version_check_unknown_skill_is_none(dirs, monkeypatch):
    project, home = dirs
    monkeypatch.setattr(S, "default_project_dir", lambda: project)
    monkeypatch.setattr(S, "default_home", lambda: home)
    assert V.find_skill("nothing") is None


# ---- skill_freshness (関数) --------------------------------------------------------------------


@pytest.fixture()
def fresh_env(dirs, monkeypatch):
    project, home = dirs
    monkeypatch.setattr(F, "_PROJECT", project)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return project, home


def test_findings_report_a_collision_once(fresh_env):
    project, home = fresh_env
    _skill(project, "handoff")
    _skill(home, "handoff")
    findings = F._scope_findings(None, [], is_init=True)
    assert [k for k, _ in findings] == ["scope:handoff"]
    assert F._scope_findings(None, ["scope:handoff"], is_init=False) == [], "通知済みは繰り返さない (狼少年化の防止)"


def test_findings_empty_when_clean(fresh_env):
    project, _home = fresh_env
    _skill(project, "handoff")
    assert F._scope_findings(None, [], is_init=True) == []


def test_worktree_note_only_at_init_and_only_in_worktree(fresh_env):
    wt = r"C:\ClaudeCode\.claude\worktrees\adoring-mclaren-ec105c"
    assert [k for k, _ in F._scope_findings(wt, [], is_init=True)] == ["worktree:init"]
    assert F._scope_findings(wt, [], is_init=False) == []
    assert F._scope_findings(r"C:\ClaudeCode", [], is_init=True) == []
    assert F._scope_findings(wt, ["worktree:init"], is_init=True) == []


def test_scope_text_is_plain_and_actionable(fresh_env):
    project, home = fresh_env
    _skill(project, "handoff", "a")
    _skill(home, "handoff", "b")
    text = F._scope_text(F._scope_findings(None, [], is_init=True))
    assert "retire handoff" in text and "新しいセッションに届きません" in text
    assert "manifest" not in text  # 専門用語を運用の指示文に出さない


# ---- skill_freshness (本番と同じ形: プロセス起動・stdin JSON・stdout JSON) ------------------------


def _hook(session: str, home: Path, project: Path, state: Path, cwd: str = "") -> dict | None:
    env = {
        **os.environ,
        "USERPROFILE": str(home), "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "LOCALAPPDATA": str(state),  # セッション状態を本物の場所から隔離する
    }
    payload = json.dumps({"session_id": session, "cwd": cwd or str(project), "prompt": "x"}).encode("utf-8")
    r = subprocess.run(
        [sys.executable, str(HOOKS / "skill_freshness.py")],
        input=payload, capture_output=True, env=env, timeout=30,
    )
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    return json.loads(r.stdout.decode("utf-8")) if r.stdout.strip() else None


def test_hook_process_warns_at_session_start_then_stays_quiet(dirs, tmp_path):
    project, home = dirs
    state = tmp_path / "state"
    _skill(project, "handoff", "new")
    _skill(home, "handoff", "old")

    first = _hook("sess-A", home, project, state)
    assert first is not None
    ctx = first["hookSpecificOutput"]["additionalContext"]
    assert "skill-scope" in ctx and "retire handoff" in ctx

    assert _hook("sess-A", home, project, state) is None, "同じセッションで同じ警告を繰り返さない"

    # 退避したら、新しいセッションでは何も出ない
    assert S.retire("handoff", project, home) is not None
    assert _hook("sess-B", home, project, state) is None


def test_hook_process_is_silent_when_clean(dirs, tmp_path):
    project, home = dirs
    _skill(project, "handoff")
    assert _hook("sess-C", home, project, tmp_path / "state") is None
