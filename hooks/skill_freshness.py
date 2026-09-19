"""UserPromptSubmit hook: セッション開始後に更新されたスキル/コマンドを検知する。

背景 (2026-07-27):
  スキル本文は `Skill` 呼び出し時に会話へ差し込まれる **スナップショット** であり、
  以後ディスク上の SKILL.md を更新しても、既に走っているセッションの認識は
  古いまま固定される。実際に 5 月に読み込んだ cgd (Lv1-5/Gemini 構成) を
  7 月まで参照し続け、現行 (Lv0-8/DeepSeek 構成) と食い違う事故が起きた。

対策:
  セッション初回プロンプトで対象ファイルの (mtime, size) を記録し、以降の
  プロンプトで stat を取り直して差分を検出。変化していれば additionalContext
  で 1 回だけ警告する。スキル本文には一切依存しないので、既に古い本文を
  読み込んでしまったセッションでも警告が出る。

コスト: stat のみ (数十ファイル ~数 ms)。ファイル内容は読まない。

追加 (2026-09-19): 同名のスキル/コマンドが「このPC全体用 (~/.claude)」と project の両方に
あるとき、優先される前者が project 側を警告なしで隠す。handoff で project 側だけ直した修正が
新しいセッションに届かなかった事故を受け、その重複を **検知して知らせる**。名前の突き合わせ
だけで内容は (重複があるときを除き) 読まない。判定は tools/skill_scope.py の 1 か所に集約している。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

# 同名スキルの衝突検出 (優先順位の実装は tools/skill_scope.py に 1 つだけ持つ)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
try:
    import skill_scope
except ImportError:  # pragma: no cover - tools/ は 1 セットでミラーされるので通常は起きない
    skill_scope = None

_PROJECT = Path(os.environ.get("CLAUDE_PROJECT_DIR", r"C:/ClaudeCode"))
_STATE_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ClaudeCodeSkillWatch"
_LOG = _STATE_DIR / "hook.log"

# 監視対象: プロジェクト/ユーザー双方のスキル・コマンド定義
_WATCH_GLOBS: tuple[tuple[Path, str], ...] = (
    (_PROJECT / ".claude" / "skills", "**/SKILL.md"),
    (_PROJECT / ".claude" / "commands", "*.md"),
    (Path.home() / ".claude" / "skills", "**/SKILL.md"),
    (Path.home() / ".claude" / "commands", "*.md"),
)


def _log(msg: str) -> None:
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def _session_key(payload: dict) -> str:
    """session_id > transcript_path ハッシュ > 固定値、の順にキーを決める。"""
    sid = payload.get("session_id")
    if isinstance(sid, str) and sid.strip():
        return "".join(c for c in sid if c.isalnum() or c in "-_")[:64]
    tp = payload.get("transcript_path")
    if isinstance(tp, str) and tp.strip():
        return "tp-" + hashlib.sha1(tp.encode("utf-8")).hexdigest()[:16]
    return "default"


def _scan() -> dict[str, list[float | int]]:
    """監視対象の (mtime, size) を集める。内容は読まない。"""
    out: dict[str, list[float | int]] = {}
    for root, pattern in _WATCH_GLOBS:
        if not root.is_dir():
            continue
        for p in root.glob(pattern):
            name = p.name.lower()
            # バックアップ類は無視 (SKILL.md.bak_YYYYMMDD_HHMMSS など)
            if ".bak_" in name or name.endswith(".bak"):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            out[str(p)] = [round(st.st_mtime, 3), st.st_size]
    return out


def _label(path_str: str) -> str:
    """C:/…/skills/cgd/SKILL.md → 'cgd', commands/r.md → 'r' のように短縮。"""
    p = Path(path_str)
    if p.name.lower() == "skill.md":
        return p.parent.name
    return p.stem


def _in_worktree(cwd: str) -> bool:
    return "/.claude/worktrees/" in (cwd.replace("\\", "/").lower() + "/")


def _scope_findings(cwd: str | None, warned: list[str], *, is_init: bool) -> list[tuple[str, str]]:
    """同名スキルの衝突と worktree 実行の注意 (未通知のものだけ)。(重複通知キー, 本文) を返す。

    hook は作業を止めない。例外は握りつぶして空を返す (ログだけ残す)。
    """
    if skill_scope is None:
        return []
    out: list[tuple[str, str]] = []
    try:
        for c in skill_scope.find_collisions(_PROJECT, Path.home()):
            key = f"scope:{c.name.casefold()}"
            if key not in warned:
                out.append((key, "\n".join(skill_scope.describe_collision(c))))
    except Exception as exc:  # noqa: BLE001
        _log(f"[scope-error] {type(exc).__name__}: {exc}")
        return []
    if is_init and cwd and _in_worktree(cwd) and "worktree:init" not in warned:
        out.append(
            (
                "worktree:init",
                "このセッションは worktree で動いています。スキルと AGENTS.md は、この worktree に"
                "コミットされた時点のコピーです (メイン作業ツリーの未コミット編集は届きません)。",
            )
        )
    return out


def _scope_text(findings: list[tuple[str, str]]) -> str:
    collisions = [text for key, text in findings if key.startswith("scope:")]
    notes = [text for key, text in findings if not key.startswith("scope:")]
    parts: list[str] = []
    if collisions:
        parts.append(
            "[skill-scope] 同名のスキル/コマンドが複数の場所にあり、読み込まれるのは先頭の 1 つだけです。"
            "project 側 (.claude/skills) を直しても新しいセッションに届きません。\n"
            + "\n".join(collisions)
        )
    parts.extend(f"[skill-scope] {text}" for text in notes)
    return "\n".join(parts)


def _emit(text: str) -> None:
    out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0

    key = _session_key(payload)
    state_path = _STATE_DIR / f"{key}.json"
    current = _scan()

    prev: dict = {}
    if state_path.exists():
        try:
            prev = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            prev = {}

    cwd = payload.get("cwd")
    cwd = cwd if isinstance(cwd, str) else None

    if not prev:
        # セッション初回: 基準を記録するだけ。payload キーも 1 回だけ記録して
        # (session_id の有無などを) 後から検証できるようにする。
        _log(f"[init] key={key} keys={sorted(payload.keys())} files={len(current)}")
        # 同名スキルの衝突 (このPC全体用が project を隠す) は、開始時点で必ず知らせる。
        scope = _scope_findings(cwd, [], is_init=True)
        _save(state_path, {"baseline": current, "warned": [k for k, _ in scope]})
        if scope:
            _emit(_scope_text(scope))
        return 0

    baseline: dict = prev.get("baseline", {})
    warned: list[str] = list(prev.get("warned", []))

    changed: list[str] = []
    for path_str, meta in current.items():
        old = baseline.get(path_str)
        if old is None:
            changed.append(path_str)  # 新規追加
        elif old != meta:
            changed.append(path_str)
    # 未通知のものだけ
    fresh = [p for p in changed if p not in warned]
    scope = _scope_findings(cwd, warned, is_init=False)

    if not fresh and not scope:
        return 0

    parts: list[str] = []
    if fresh:
        labels = sorted({_label(p) for p in fresh})
        _log(f"[warn] key={key} changed={labels}")
        warned.extend(fresh)
        listed = ", ".join(f"`{name}`" for name in labels[:10])
        more = f" 他 {len(labels) - 10} 件" if len(labels) > 10 else ""
        parts.append(
            f"[skill-freshness] セッション開始後に更新されたスキル/コマンド: {listed}{more}。"
            "スキル本文は呼び出し時点のスナップショットとして会話に固定されるため、"
            "このセッションで既に読み込み済みのものは古い版のままです。"
            "該当スキルを使う前に再度 Skill で呼び直すか、実ファイルを Read して"
            "現行仕様を確認してください。"
        )
    if scope:
        _log(f"[warn] key={key} scope={[k for k, _ in scope]}")
        warned.extend(k for k, _ in scope)
        parts.append(_scope_text(scope))
    _save(state_path, {"baseline": baseline, "warned": warned})
    _emit("\n\n".join(parts))
    return 0


def _save(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
