"""cgd Lv6/Lv7/Lv8 の Workflow 実行を機械的に強制するゲート (PreToolUse hook + CLI).

背景 (2026-08-05):
  cgd スキルは Lv6/Lv7/Lv8 のレビュー段を Workflow (cgd_lv6_review.js /
  cgd_lv7_review.js / cgd_lv8_review.js) に委譲する仕組みを持ち、主 context への
  生出力流入を 94% 削減できる。しかし SKILL.md の該当節は inline 手順で書かれており、
  Workflow の説明は 300 行下の別節にあるため、素直に読むと必ず inline に着地する。
  実際 2026-08-05 の 1 日で Lv7 が 3 件実行され、Workflow は 0 件だった
  (独立した 2 セッションが同じ間違いをした)。

  → 「本文に書いてあるが読まれない」型の失敗なので、文章の修正だけでは再発する。
     本 hook が inline の `codex exec` を機械的に遮断する。

仕組み:
  1. `/cgd` Step 1 でレベルが確定すると cgd_usage_log.py が本スクリプトを
     `arm --level 6|7|8` で呼び、ゲートマーカー (JSON) を書く。
  2. PreToolUse(Bash) hook として、マーカーが有効な間 `codex exec` を deny する。
  3. Workflow 内の agent が叩く codex には `CGD_WF_RUN=1` が前置されているので
     通過する。通過を検知した時点でゲートは自動解除される (= WF が実際に走った)。
  4. 主 context は WF 完了後に `disarm` を明示的に呼ぶ (Step C の再レビューは
     inline の codex を使うため、解除されていないと誤って弾かれる)。
  5. 保険として TTL (既定 180 分) で自然失効する。

  なお Workflow の subagent が発行する Bash に PreToolUse hook が発火するか否かは
  環境依存だが、本設計はどちらでも壊れない:
    - 発火する  → `CGD_WF_RUN=1` の前置で通過し、ゲートが自動解除される
    - 発火しない → そもそも遮断対象外。ゲートは明示 disarm か TTL で解除される

使い方 (CLI):
    python cgd_wf_gate.py arm --level 7 [--ttl-min 180]
    python cgd_wf_gate.py disarm
    python cgd_wf_gate.py status

hook として: stdin に PreToolUse の JSON を渡す (サブコマンドなしで起動)。
失敗は常に fail-open (exit 0 / 遮断しない)。ゲート機構の不調で cgd 本体を
止めてはならない。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

# ゲートマーカーの置き場所。cgd が既に使っている C:/tmp-ai に置く (テスト時は env で差し替え)。
GATE_PATH: Path = Path(os.environ.get("CGD_WF_GATE_PATH", r"C:/tmp-ai/.cgd_wf_gate.json"))

# WF 経由の codex 呼び出しに前置される目印。これが含まれる Bash は遮断しない。
BYPASS_TOKEN = "CGD_WF_RUN=1"

# 遮断対象。`codex exec ...` のみ (codex login status 等は対象外)。
# 「コマンドとして起動される位置」に限定する: 行頭 / `&&` `||` `;` `|` の直後 /
# 環境変数代入 (`FOO=1 codex exec`) の直後。単に文字列として含むだけ
# (JSON ペイロード内の "codex exec" 等) を遮断しないための限定。
CODEX_EXEC_RE = re.compile(
    r"(?:^|[\n;|&])\s*(?:\w+=[^\s]*\s+)*codex\s+exec\b",
    re.MULTILINE,
)

WF_REQUIRED_LEVELS = (6, 7, 8)
DEFAULT_TTL_MIN = 180

_WORKFLOW_SCRIPT = {
    6: "C:/ClaudeCode/.claude/skills/cgd/workflows/cgd_lv6_review.js",
    7: "C:/ClaudeCode/.claude/skills/cgd/workflows/cgd_lv7_review.js",
    8: "C:/ClaudeCode/.claude/skills/cgd/workflows/cgd_lv8_review.js",
}

_TS_FMT = "%Y-%m-%d %H:%M:%S"


# --------------------------------------------------------------------------
# ゲート状態
# --------------------------------------------------------------------------
def arm(level: int, ttl_min: int = DEFAULT_TTL_MIN, gate_path: Path = GATE_PATH) -> bool:
    """ゲートを張る。対象外レベルなら何もせず False。"""
    if level not in WF_REQUIRED_LEVELS:
        return False
    now = datetime.now()
    payload = {
        "level": level,
        "armed_at": now.strftime(_TS_FMT),
        "expires_at": (now + timedelta(minutes=ttl_min)).strftime(_TS_FMT),
        "workflow_script": _WORKFLOW_SCRIPT[level],
    }
    try:
        gate_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = gate_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, gate_path)
    except OSError as exc:
        print(f"[cgd wf-gate] WARN: ゲート設定に失敗 (fail-open): {exc}", file=sys.stderr)
        return False
    return True


def disarm(gate_path: Path = GATE_PATH) -> bool:
    """ゲートを解除する。元から無ければ False。"""
    try:
        if gate_path.exists():
            gate_path.unlink()
            return True
    except OSError as exc:
        print(f"[cgd wf-gate] WARN: ゲート解除に失敗: {exc}", file=sys.stderr)
    return False


def read_gate(gate_path: Path = GATE_PATH) -> dict | None:
    """有効なゲートを返す。無い/壊れている/期限切れ なら None (期限切れは掃除する)。"""
    try:
        if not gate_path.exists():
            return None
        data = json.loads(gate_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("level") not in WF_REQUIRED_LEVELS:
        return None
    try:
        expires = datetime.strptime(str(data.get("expires_at", "")), _TS_FMT)
    except ValueError:
        return None
    if datetime.now() >= expires:
        disarm(gate_path)
        return None
    return data


# --------------------------------------------------------------------------
# hook 本体
# --------------------------------------------------------------------------
def _deny_reason(gate: dict) -> str:
    level = gate["level"]
    script = gate.get("workflow_script", _WORKFLOW_SCRIPT[level])
    return (
        f"[cgd wf-gate] Lv{level} は Workflow 実行が必須です。inline の `codex exec` は遮断しました。\n"
        f"主 context の生出力汚染を防ぐため、レビュー段は必ず Workflow に委譲してください:\n"
        f'  Workflow({{ scriptPath: "{script}", args: {{ ... }} }})\n'
        f"手順は cgd/SKILL.md の「Workflow 経由実行 (Lv6-WF / Lv7-WF / Lv8-WF)」節を参照。\n"
        f"WF 完了後、Step C の Codex 再レビュー (inline) に進む前に次でゲートを解除:\n"
        f'  python "C:/ClaudeCode/.claude/hooks/cgd_wf_gate.py" disarm\n'
        f"WF が使えない事情がある場合のみ、コマンド先頭に {BYPASS_TOKEN} を付けて意図的に迂回できます"
        f"（迂回した理由をユーザーに必ず伝えること）。"
    )


def handle_hook(payload: dict) -> dict | None:
    """deny する場合は hook 出力 dict、通す場合は None を返す。"""
    if payload.get("tool_name") != "Bash":
        return None
    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or not CODEX_EXEC_RE.search(command):
        return None

    gate = read_gate()
    if gate is None:
        return None

    if BYPASS_TOKEN in command:
        # WF が実際に走り出した (または意図的な迂回)。以後の inline 再レビューを
        # 妨げないようゲートを解除する。
        disarm()
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _deny_reason(gate),
        }
    }


def _run_hook() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        out = handle_hook(payload)
    except Exception as exc:  # noqa: BLE001 — fail-open が最優先
        print(f"[cgd wf-gate] WARN: hook 内で例外 (fail-open): {exc}", file=sys.stderr)
        return 0
    if out is not None:
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main() -> int:
    if len(sys.argv) <= 1:
        return _run_hook()

    parser = argparse.ArgumentParser(description="cgd Lv6/Lv7/Lv8 Workflow 強制ゲート")
    sub = parser.add_subparsers(dest="command", required=True)

    p_arm = sub.add_parser("arm", help="ゲートを張る (Lv6/Lv7/Lv8 のみ有効)")
    p_arm.add_argument("--level", type=int, required=True)
    p_arm.add_argument("--ttl-min", type=int, default=DEFAULT_TTL_MIN)

    sub.add_parser("disarm", help="ゲートを解除する")
    sub.add_parser("status", help="現在のゲート状態を表示する")

    args = parser.parse_args()

    if args.command == "arm":
        if arm(args.level, ttl_min=args.ttl_min):
            print(f"[cgd wf-gate] Lv{args.level}: WF 必須ゲートを張りました ({args.ttl_min} 分で失効)")
        else:
            print(f"[cgd wf-gate] Lv{args.level} はゲート対象外 (対象: Lv{'/Lv'.join(map(str, WF_REQUIRED_LEVELS))})")
        return 0

    if args.command == "disarm":
        print("[cgd wf-gate] 解除しました" if disarm() else "[cgd wf-gate] ゲートは張られていません")
        return 0

    gate = read_gate()
    if gate is None:
        print("[cgd wf-gate] 未設定 (inline codex は遮断されません)")
    else:
        print(f"[cgd wf-gate] Lv{gate['level']} 有効 / 張った時刻 {gate['armed_at']} / 失効 {gate['expires_at']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
