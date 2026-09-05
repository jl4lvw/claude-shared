"""共有 hook を settings.local.json に冪等登録する。

背景:
  hook スクリプト本体 (`.claude/hooks/*.py`) は `/g-ul` `/g-dl` のミラー対象に
  含めたので他 PC にも配布される。しかし **登録先の settings.local.json は
  PC ごとのローカル設定**（その PC の permissions 等が入る）なので共有できない。
  そこで「配布されたファイルを、その PC の settings.local.json へ登録する」
  作業だけを本ツールが担う。

特性:
  - 冪等: 同じ command が既にあれば追加しない（何度実行しても増殖しない）
  - 非破壊: 既存の permissions / 他イベントの hook はそのまま保持
  - 実行前に `.bak_YYYYMMDD_HHMMSS` を必ず退避（AGENTS.md 準拠）
  - hook スクリプトが存在しない場合はその項目をスキップして警告

使い方:
  python .claude/tools/install_hooks.py            # 登録実行
  python .claude/tools/install_hooks.py --check    # 差分表示のみ（書き込まない）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass


def _project_dir() -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    if env:
        return Path(env)
    # 本ファイルは <project>/.claude/tools/ 配置想定
    return Path(__file__).resolve().parent.parent.parent


# 配布・登録したい hook の定義。追加したらここに 1 行足す。
# (イベント名, matcher(Noneなら未指定), hook スクリプトの相対パス, timeout 秒, 用途メモ)
# matcher が異なる同一イベントは別グループとして登録する(例: PostCompact の auto/manual)。
_HOOKS: tuple[tuple[str, str | None, str, int, str], ...] = (
    (
        "UserPromptSubmit",
        None,
        ".claude/hooks/skill_freshness.py",
        5,
        "セッション開始後に更新されたスキル/コマンドを検知して警告",
    ),
    (
        "UserPromptSubmit",
        None,
        ".claude/hooks/r_consume.py",
        5,
        "貼付テキスト末尾の r-consume タグを検出して RemoteInstructions を consume",
    ),
    (
        "UserPromptSubmit",
        None,
        ".claude/hooks/ctx_inject.py",
        5,
        "ctx: 圧縮直後に制約・承認・状態を注入(ctx/SKILL.md 184-186)",
    ),
    (
        "UserPromptSubmit",
        None,
        ".claude/hooks/pv_verify_reminder.py",
        5,
        "pv: Step4 の collect 検証が済んでいない run を毎ターン提示する",
    ),
    (
        # 実際の登録は matcher="Bash|PowerShell" / command は `python -X utf8 ...`。
        # ここが実態とズレていると、登録済みなのに [ADD] と判定して**二重登録**する
        # (2026-08-12 に --check で検出)。PowerShell 経由の codex 起動も塞ぐ必要が
        # あるので matcher は 2 つとも要る。
        "PreToolUse",
        "Bash|PowerShell",
        ".claude/hooks/cgd_wf_gate.py",
        5,
        "cgd Lv6/Lv7/Lv8 で inline の codex exec を遮断し Workflow 実行を強制",
    ),
    (
        "PostToolUse",
        "Bash",
        ".claude/hooks/ai_telemetry.py",
        5,
        "外部AI(codex/DS/Qwen/Gemini)呼出のトークン・出力サイズを telemetry.jsonl に自動記録",
    ),
    (
        "PostCompact",
        "auto",
        ".claude/hooks/ctx_compact_mark.py",
        5,
        "ctx: 自動圧縮時に台帳へ COMPACT auto を追記(ctx/SKILL.md 184-186)",
    ),
    (
        "PostCompact",
        "manual",
        ".claude/hooks/ctx_compact_mark.py",
        5,
        "ctx: 手動圧縮時に台帳へ COMPACT manual を追記(ctx/SKILL.md 184-186)",
    ),
    (
        "PostToolUse",
        "Edit|Write",
        ".claude/hooks/ruff_check.py",
        15,
        "Claudeが.pyファイルを編集した直後にruff checkを自動実行し指摘を報告(自動修正なし)",
    ),
    # hq_board.py は 8 イベント全てに要る。Stop/SessionStart/SessionEnd の 3 つ
    # だけだと running / waiting_permission / waiting_question が記録されず、
    # 状況板の目的(何で止まっているか)を果たせない(2026-09-05 A端末報告 #2048)。
    # PreToolUse/PostToolUse は matcher 未指定(=全ツール)。既存の
    # cgd_wf_gate.py(Bash|PowerShell) / ruff_check.py(Edit|Write) とは別グループになる。
    (
        "Stop",
        None,
        ".claude/hooks/hq_board.py",
        5,
        "hq: 司令塔の状況板 .hq/board/<sid>.json を更新(hq/SKILL.md 仕組み節)",
    ),
    (
        "SessionStart",
        None,
        ".claude/hooks/hq_board.py",
        5,
        "hq: 司令塔の状況板 .hq/board/<sid>.json を更新(hq/SKILL.md 仕組み節)",
    ),
    (
        "SessionEnd",
        None,
        ".claude/hooks/hq_board.py",
        5,
        "hq: 司令塔の状況板 .hq/board/<sid>.json を更新(hq/SKILL.md 仕組み節)",
    ),
    (
        "UserPromptSubmit",
        None,
        ".claude/hooks/hq_board.py",
        5,
        "hq: 司令塔の状況板 .hq/board/<sid>.json を更新(hq/SKILL.md 仕組み節)",
    ),
    (
        "PreToolUse",
        None,
        ".claude/hooks/hq_board.py",
        5,
        "hq: 司令塔の状況板 .hq/board/<sid>.json を更新(hq/SKILL.md 仕組み節)",
    ),
    (
        "PostToolUse",
        None,
        ".claude/hooks/hq_board.py",
        5,
        "hq: 司令塔の状況板 .hq/board/<sid>.json を更新(hq/SKILL.md 仕組み節)",
    ),
    (
        "Notification",
        None,
        ".claude/hooks/hq_board.py",
        5,
        "hq: 司令塔の状況板 .hq/board/<sid>.json を更新(hq/SKILL.md 仕組み節)",
    ),
    (
        "PermissionRequest",
        None,
        ".claude/hooks/hq_board.py",
        5,
        "hq: 司令塔の状況板 .hq/board/<sid>.json を更新(hq/SKILL.md 仕組み節)",
    ),
)


def _command_for(rel_path: str) -> str:
    return f'python "$CLAUDE_PROJECT_DIR/{rel_path}"'


def _load(settings_path: Path) -> dict:
    if not settings_path.exists():
        return {}
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[ERROR] settings.local.json が壊れています: {exc}")
        raise SystemExit(2)
    return data if isinstance(data, dict) else {}


def _norm_matcher(matcher: str | None) -> str | None:
    """matcher 未指定・空文字・"*" は Claude Code 上どれも「全ツール対象」で同義。
    別PCが手作業で "*" を書いていると、None と厳密比較しては未登録と誤判定して
    二重登録になる(2026-09-05 A端末が PreToolUse を "*" で登録していた)。
    PostCompact の auto/manual のような実マッチャは従来どおり区別される。"""
    if matcher in (None, "", "*"):
        return None
    return matcher


def _existing_commands(settings: dict, event: str, matcher: str | None) -> set[str]:
    """matcher が None の場合は matcher 未指定グループのみ、指定時はそのmatcherの
    グループのみを見る(PostCompact の auto/manual 等、matcher違いは別グループのため
    混同してはいけない)。"""
    out: set[str] = set()
    want = _norm_matcher(matcher)
    for group in settings.get("hooks", {}).get(event, []) or []:
        group_matcher = _norm_matcher((group or {}).get("matcher"))
        if group_matcher != want:
            continue
        for h in (group or {}).get("hooks", []) or []:
            cmd = h.get("command")
            if isinstance(cmd, str):
                out.add(cmd.strip())
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="共有 hook を settings.local.json に冪等登録")
    ap.add_argument("--check", action="store_true", help="書き込まず差分だけ表示")
    args = ap.parse_args()

    project = _project_dir()
    settings_path = project / ".claude" / "settings.local.json"
    settings = _load(settings_path)

    print(f"project : {project}")
    print(f"settings: {settings_path} ({'既存' if settings_path.exists() else '新規作成'})")

    to_add: list[tuple[str, str | None, str, int, str]] = []
    for event, matcher, rel, timeout, note in _HOOKS:
        script = project / rel
        if not script.is_file():
            print(f"  [SKIP] {rel} が見つかりません（/g-dl で配布されていない可能性）")
            continue
        cmd = _command_for(rel)
        label = f"{event}" + (f"[{matcher}]" if matcher else "")
        # 既存判定はコマンド文字列の完全一致だと弱い。`python -X utf8 ...` のように
        # 手で足したオプションがあるだけで「未登録」と誤判定し、二重登録になる
        # (2026-08-12 に cgd_wf_gate で実際に発生)。**スクリプトのパスで見る**。
        existing = _existing_commands(settings, event, matcher)
        if cmd in existing or any(rel in c for c in existing):
            print(f"  [OK]   登録済: {label} <- {rel}")
            continue
        print(f"  [ADD]  未登録: {label} <- {rel}  ({note})")
        to_add.append((event, matcher, rel, timeout, note))

    if not to_add:
        print("\n変更なし（すべて登録済み）")
        return 0

    if args.check:
        print(f"\n--check のため書き込みません。{len(to_add)} 件が未登録です。")
        return 1

    # バックアップ
    if settings_path.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = settings_path.with_name(f"{settings_path.name}.bak_{stamp}_hooks")
        shutil.copy2(settings_path, bak)
        print(f"\nバックアップ: {bak.name}")

    hooks_root = settings.setdefault("hooks", {})
    for event, matcher, rel, timeout, _note in to_add:
        groups = hooks_root.setdefault(event, [])
        entry = {"type": "command", "command": _command_for(rel), "timeout": timeout}
        # matcher が一致する既存グループを探し、あれば相乗り。なければ新規グループ追加。
        target_group = None
        for group in groups:
            if isinstance(group, dict) and _norm_matcher(group.get("matcher")) == _norm_matcher(matcher):
                target_group = group
                break
        if target_group is not None and isinstance(target_group.get("hooks"), list):
            target_group["hooks"].append(entry)
        else:
            new_group: dict = {"hooks": [entry]}
            if matcher is not None:
                new_group["matcher"] = matcher
            groups.append(new_group)

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = settings_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, settings_path)
    print(f"{len(to_add)} 件を登録しました。Claude Code を再起動すると有効になります。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
