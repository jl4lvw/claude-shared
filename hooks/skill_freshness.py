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

    if not prev:
        # セッション初回: 基準を記録するだけ。payload キーも 1 回だけ記録して
        # (session_id の有無などを) 後から検証できるようにする。
        _log(f"[init] key={key} keys={sorted(payload.keys())} files={len(current)}")
        _save(state_path, {"baseline": current, "warned": []})
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

    if not fresh:
        return 0

    labels = sorted({_label(p) for p in fresh})
    _log(f"[warn] key={key} changed={labels}")

    warned.extend(fresh)
    _save(state_path, {"baseline": baseline, "warned": warned})

    listed = ", ".join(f"`{name}`" for name in labels[:10])
    more = f" 他 {len(labels) - 10} 件" if len(labels) > 10 else ""
    out = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                f"[skill-freshness] セッション開始後に更新されたスキル/コマンド: {listed}{more}。"
                "スキル本文は呼び出し時点のスナップショットとして会話に固定されるため、"
                "このセッションで既に読み込み済みのものは古い版のままです。"
                "該当スキルを使う前に再度 Skill で呼び直すか、実ファイルを Read して"
                "現行仕様を確認してください。"
            ),
        }
    }
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
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
