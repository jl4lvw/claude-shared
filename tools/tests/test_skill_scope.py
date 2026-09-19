"""skill_scope.py (実効コピーの解決・衝突検出・退避) のテスト.

なぜ要るか (2026-09-19 の実事故):
    handoff スキルの project 版だけ直した修正が、ユーザー階層 (~/.claude/skills) の
    古い複製に隠されて新しいセッションに届かなかった。Claude Code は同名ならユーザー階層を
    優先し、警告なしで project 側を隠す。

    ここでは「実際に読まれるコピーはどれか」「同名が 2 か所にあれば検出できるか」を、
    本物のホームを触らず tmp_path 上の偽ホーム + 偽 project で確かめる。
    退避 (retire) は **削除しない** ことと、project 側に無いものを退避しないことを固定する。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import skill_scope as S  # noqa: E402


def _skill(base: Path, name: str, body: str = "body") -> Path:
    d = base / ".claude" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    f = d / "SKILL.md"
    f.write_text(body, encoding="utf-8")
    return f


def _command(base: Path, name: str, body: str = "cmd") -> Path:
    d = base / ".claude" / "commands"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{name}.md"
    f.write_text(body, encoding="utf-8")
    return f


@pytest.fixture()
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    """(project, home)。別々のディレクトリ。"""
    project, home = tmp_path / "proj", tmp_path / "home"
    project.mkdir()
    home.mkdir()
    return project, home


def test_no_skills_no_collision(dirs):
    project, home = dirs
    assert S.find_collisions(project, home) == []


def test_project_only_is_not_a_collision(dirs):
    project, home = dirs
    f = _skill(project, "handoff")
    assert S.find_collisions(project, home) == []
    entry = S.resolve("handoff", project, home)
    assert entry is not None and entry.scope == S.PROJECT and entry.path == f


def test_identical_duplicate_is_a_collision(dirs):
    """内容が同一でも衝突。片方だけ直した瞬間に食い違い、直した側が隠れるため。"""
    project, home = dirs
    _skill(project, "handoff", "same")
    _skill(home, "handoff", "same")
    (c,) = S.find_collisions(project, home)
    assert c.name == "handoff"
    assert c.identical is True
    assert c.effective.scope == S.PERSONAL
    assert [e.scope for e in c.hidden] == [S.PROJECT]


def test_different_duplicate_is_flagged_not_identical(dirs):
    project, home = dirs
    _skill(project, "handoff", "new version")
    _skill(home, "handoff", "old version")
    (c,) = S.find_collisions(project, home)
    assert c.identical is False


def test_personal_wins_over_project(dirs):
    """Claude Code の実際の優先順位 (personal > project)。逆にしていたのが skill_version_check の欠陥だった。"""
    project, home = dirs
    p = _skill(project, "handoff", "project")
    h = _skill(home, "handoff", "personal")
    entry = S.resolve("handoff", project, home)
    assert entry is not None and entry.path == h and entry.path != p


def test_name_match_ignores_case(dirs):
    project, home = dirs
    _skill(project, "handoff")
    _skill(home, "Handoff")
    assert len(S.find_collisions(project, home)) == 1


def test_personal_command_hides_project_skill(dirs):
    """command と skill は同じ名前空間。ユーザー階層の command も project の skill を隠しうる。"""
    project, home = dirs
    _skill(project, "r")
    _command(home, "r")
    (c,) = S.find_collisions(project, home)
    assert c.effective.kind == "command" and c.effective.scope == S.PERSONAL


def test_same_physical_file_is_not_a_collision(tmp_path):
    """project がホーム直下そのものだと、両スコープが同じ .claude を指す。これは重複ではない。"""
    home = tmp_path / "home"
    home.mkdir()
    _skill(home, "handoff")
    assert S.find_collisions(home, home) == []


def test_backup_files_are_ignored(dirs):
    project, home = dirs
    _skill(project, "handoff")
    d = home / ".claude" / "skills" / "handoff"
    d.mkdir(parents=True)
    (d / "SKILL.md.bak_20260919_134817").write_text("bak", encoding="utf-8")  # SKILL.md 自体は無い
    assert S.find_collisions(project, home) == []


def test_retire_moves_and_never_deletes(dirs):
    project, home = dirs
    proj_file = _skill(project, "handoff", "project")
    _skill(home, "handoff", "personal")
    (home / ".claude" / "skills" / "handoff" / "SKILL.md.bak_1").write_text("old", encoding="utf-8")

    dest = S.retire("handoff", project, home, now=datetime(2026, 9, 19, 14, 0, 0))

    assert dest == home / ".claude" / S.RETIRED_DIRNAME / "handoff_20260919_140000"
    assert (dest / "handoff" / "SKILL.md").read_text(encoding="utf-8") == "personal"
    assert (dest / "handoff" / "SKILL.md.bak_1").exists(), "バックアップごと退避する"
    assert not (home / ".claude" / "skills" / "handoff").exists()
    assert proj_file.read_text(encoding="utf-8") == "project", "project 側は触らない"
    assert S.find_collisions(project, home) == []
    entry = S.resolve("handoff", project, home)
    assert entry is not None and entry.scope == S.PROJECT


def test_retire_refuses_when_project_has_no_copy(dirs):
    """personal にしか無いものを退避すると、唯一のコピーが読まれなくなる。"""
    project, home = dirs
    _skill(home, "only-personal")
    with pytest.raises(ValueError, match="唯一のコピー"):
        S.retire("only-personal", project, home)
    assert (home / ".claude" / "skills" / "only-personal" / "SKILL.md").exists()


def test_retire_refuses_when_nothing_to_retire(dirs):
    project, home = dirs
    _skill(project, "handoff")
    with pytest.raises(ValueError, match="退避するものがありません"):
        S.retire("handoff", project, home)


def test_retire_destination_is_unique_within_same_second(dirs):
    project, home = dirs
    _skill(project, "handoff")
    now = datetime(2026, 9, 19, 14, 0, 0)
    _skill(home, "handoff", "first")
    d1 = S.retire("handoff", project, home, now=now)
    _skill(home, "handoff", "second")
    d2 = S.retire("handoff", project, home, now=now)
    assert d1 != d2
    assert (d1 / "handoff" / "SKILL.md").read_text(encoding="utf-8") == "first"
    assert (d2 / "handoff" / "SKILL.md").read_text(encoding="utf-8") == "second"


def test_retire_command_moves_the_file(dirs):
    project, home = dirs
    _skill(project, "r")
    c = _command(home, "r", "personal command")
    dest = S.retire("r", project, home, now=datetime(2026, 9, 19, 14, 0, 0))
    assert not c.exists()
    assert (dest / "r.md").read_text(encoding="utf-8") == "personal command"


def test_describe_collision_gives_a_copyable_remedy(dirs):
    project, home = dirs
    _skill(project, "handoff", "a")
    _skill(home, "handoff", "b")
    (c,) = S.find_collisions(project, home)
    text = "\n".join(S.describe_collision(c))
    assert "retire handoff" in text
    assert "skill_scope.py" in text
    assert "削除はしない" in text
    assert "読まれる" in text and "隠れる" in text


# ---- CLI ----------------------------------------------------------------------------------


def _cli(dirs, *argv: str) -> int:
    project, home = dirs
    return S.main(["--project-dir", str(project), "--home", str(home), *argv])


def test_cli_check_ok_states_the_coverage(dirs, capsys):
    """exit 0 を「どこでも動く保証」と読ませない。検査範囲と未検査範囲を必ず出す。"""
    project, _home = dirs
    _skill(project, "handoff")
    assert _cli(dirs, "check") == S.EXIT_OK
    out = capsys.readouterr().out
    assert "OK" in out and "検査範囲" in out and "未検査" in out and "worktree" in out


def test_cli_check_quiet_prints_nothing_when_clean(dirs, capsys):
    project, _home = dirs
    _skill(project, "handoff")
    assert _cli(dirs, "check", "--quiet") == S.EXIT_OK
    assert capsys.readouterr().out == ""


def test_cli_check_fails_on_collision(dirs, capsys):
    project, home = dirs
    _skill(project, "handoff")
    _skill(home, "handoff")
    assert _cli(dirs, "check") == S.EXIT_COLLISION
    err = capsys.readouterr().err
    assert "NG" in err and "retire handoff" in err


def test_cli_where_reports_the_effective_copy(dirs, capsys):
    project, home = dirs
    _skill(project, "handoff", "project")
    h = _skill(home, "handoff", "personal")
    assert _cli(dirs, "where", "handoff") == S.EXIT_OK
    out = capsys.readouterr().out
    assert str(h) in out and "このPC全体用" in out and "注意" in out


def test_cli_where_not_found(dirs):
    assert _cli(dirs, "where", "nothing") == S.EXIT_NOT_FOUND


def test_cli_retire_then_check_is_clean(dirs):
    project, home = dirs
    _skill(project, "handoff")
    _skill(home, "handoff")
    assert _cli(dirs, "retire", "handoff") == S.EXIT_OK
    assert _cli(dirs, "check", "--quiet") == S.EXIT_OK


def test_cli_retire_refusal_is_nonzero(dirs):
    project, home = dirs
    _skill(home, "only-personal")
    assert _cli(dirs, "retire", "only-personal") == S.EXIT_ENV
