"""cgd Lv6/Lv7/Lv8 の Workflow 実行を機械的に強制するゲート (PreToolUse hook + CLI).

背景 (2026-08-05):
  cgd の Lv6/Lv7/Lv8 はレビュー段を Workflow に委譲すると主 context への生出力流入を
  94% 削減できる。しかし SKILL.md の導線が inline に誘導していたため、同日に独立した
  2 セッションが揃って inline で実行した。「本文に書いてあるが読まれない」型の失敗
  なので、文章の修正だけでは再発する。本 hook が inline の codex 起動を遮断する。

初版 (同日) の欠陥と、それを受けた再設計:
  初版は「`codex exec` を正規表現で拾い、コマンドに `CGD_WF_RUN=1` が含まれていたら通す」
  という素朴な作りで、Lv8 セルフレビューで以下が実証された:
    1. `bash -c "codex exec ..."` / `env X=1 codex exec` / `$(codex exec)` / `time` / `eval`
       の 5 形式が素通りした (6 形式中 5 つ)。「強制」と呼べる代物ではなかった
    2. バイパス判定が部分文字列一致だったため、手順書を書くだけのコマンドに
       `CGD_WF_RUN=1` の文字が入っているだけでゲートが解除された
    3. 通過を検知した時点で自動 disarm していたため、WF の 1 本目が走った瞬間に
       以後の inline が無防備になった (WF が halt しても解除されたまま)
    4. ゲートが単一グローバルファイルだったため、並行セッションが互いの arm/disarm を
       破壊した (A の Lv6 を B の Lv8 が上書きし、B の disarm で A の分も消える)

  再設計の第 1 版はシェル構文を正規表現で解析する方向だったが、Codex 再レビューを
  3 周しても新しい抜け道 (`sudo` → 単一引用符 → `!` / `{ }`) が出続けたため断念した。
  「正規表現ゲートは迂回と誤検知を構造的に同時に抱える」という Lv8 批評の指摘どおり。

  現在の設計:
    - **シェルを解析しない**: ゲート中は `codex` という語を含む Bash を **一律遮断**する。
      引用符・heredoc・ラッパー・シェル構文を一切見ないので、この種の抜け道が原理的に無い。
      代償としてゲート中は `grep "codex exec"` や `codex login status` も止まるが、
      止まるのはゲートが張られている間だけで、deny メッセージが逃げ道を案内する
    - **nonce 照合**: arm 時にランダム nonce を発行し、`CGD_WF_RUN=<nonce> ... codex ...` の
      形（その codex 起動への環境変数代入）でのみバイパスを認める。
      文字列の混入や `echo` での先出しでは通らない
    - **自動 disarm の廃止**: 通過しても解除しない。解除は明示 `disarm` のみ (+ TTL 90 分)
    - **セッション単位**: `--session <SID>` を付けて arm すれば自セッションだけを止める。
      省略時は全体ゲートになり全セッションを止める。**claim(所有権の移動)はしない** —
      レースと「claim 後に disarm できないデッドロック」の原因だったため
    - **fail-closed の限定導入**: ゲートファイルが「存在するが壊れている」場合は遮断側に倒す
      (握り潰すと強制が形骸化するため)。ファイルが無い場合と hook 自体の例外は通す

使い方 (CLI):
    python cgd_wf_gate.py arm --level 7 [--session <SID>] [--ttl-min 90]
    python cgd_wf_gate.py disarm [--session <SID>]
    python cgd_wf_gate.py status [--session <SID>]
    python cgd_wf_gate.py nonce  [--session <SID>]   # WF に渡す nonce を取り出す

hook として: stdin に PreToolUse の JSON を渡す (サブコマンドなしで起動)。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

GATE_DIR: Path = Path(os.environ.get("CGD_WF_GATE_DIR", r"C:/tmp-ai/.cgd_wf_gates"))
GLOBAL_KEY = "_unclaimed"

BYPASS_PREFIX = "CGD_WF_RUN="

WF_REQUIRED_LEVELS = (6, 7, 8)
DEFAULT_TTL_MIN = 90

_WORKFLOW_SCRIPT = {
    6: "C:/ClaudeCode/.claude/skills/cgd/workflows/cgd_lv6_review.js",
    7: "C:/ClaudeCode/.claude/skills/cgd/workflows/cgd_lv7_review.js",
    8: "C:/ClaudeCode/.claude/skills/cgd/workflows/cgd_lv8_review.js",
}

_TS_FMT = "%Y-%m-%d %H:%M:%S"

# --------------------------------------------------------------------------
# コマンド判定 — **シェルを解析しない**
# --------------------------------------------------------------------------
# 2026-08-05: 正規表現でシェル構文を判定する方式を廃止した。
#   `bash -c "..."` → `sudo`/`if`/`while` → 単一引用符 → `!`/`{ }` と、塞ぐたびに
#   Codex 再レビューが新しい構文を見つけ、3 周しても終わらなかった。
#   「迂回と誤検知を構造的に同時に抱える」という Lv8 批評の指摘どおりだったので、
#   **ゲート中は `codex` という語を含む Bash を一律遮断し、束縛された nonce だけ通す**
#   方式に切り替えた。引用符もラッパーもシェル構文も解析しないので、この種の
#   抜け道が原理的に生じない。
#
#   代償: ゲート中は `grep "codex exec"` や `codex login status` も止まる。
#   ただし止まるのはゲートが張られている間だけで、deny メッセージが
#   nonce と disarm の両方を案内する。**見逃すより過剰に止める方を選ぶ。**
_CODEX_TOKEN_RE = re.compile(r"(?<![\w./-])codex(?![\w-])", re.IGNORECASE)

# nonce は「その codex 起動の環境変数代入」でなければ意味がない。
# `echo CGD_WF_RUN=<nonce>; codex exec ...` で通るのを防ぐ。
_BOUND_NONCE_RE = re.compile(
    r"(?:^|[\s;|&(])(?:env\s+)?" + BYPASS_PREFIX + r"([A-Za-z0-9_-]+)"
    r"(?:\s+\w+=[^\s]*)*\s+codex(?![\w-])"
)


def mentions_codex(command: str) -> bool:
    """コマンドが codex に言及しているか。**起動かどうかは判定しない**（安全側）。"""
    return bool(_CODEX_TOKEN_RE.search(command))


def extract_bypass_nonce(command: str) -> str | None:
    """`CGD_WF_RUN=<nonce> [他の代入...] codex ...` の形でのみ nonce を認める。"""
    m = _BOUND_NONCE_RE.search(command)
    return m.group(1) if m else None


# --------------------------------------------------------------------------
# ゲート状態 (セッション単位)
# --------------------------------------------------------------------------
def _gate_path(session: str | None) -> Path:
    key = "".join(c for c in (session or GLOBAL_KEY) if c.isalnum() or c in "-_")[:64] or GLOBAL_KEY
    return GATE_DIR / f"{key}.json"


def _write_gate(path: Path, payload: dict) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.stem}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError as exc:
        print(f"[cgd wf-gate] WARN: ゲート書込に失敗: {exc}", file=sys.stderr)
        return False


def arm(level: int, session: str | None = None, ttl_min: int = DEFAULT_TTL_MIN) -> dict | None:
    """ゲートを張り、発行した内容(nonce 含む)を返す。対象外レベルなら None。"""
    if level not in WF_REQUIRED_LEVELS:
        return None
    now = datetime.now()
    payload = {
        "level": level,
        "session": session or None,
        "nonce": secrets.token_hex(8),
        "armed_at": now.strftime(_TS_FMT),
        "expires_at": (now + timedelta(minutes=ttl_min)).strftime(_TS_FMT),
        "armed_pid": os.getpid(),
        "wf_used_at": None,
        "workflow_script": _WORKFLOW_SCRIPT[level],
    }
    return payload if _write_gate(_gate_path(session), payload) else None


def disarm(session: str | None = None) -> bool:
    """**呼び出し元セッションを遮断しうるゲートをすべて**解除する。

    自セッション用ゲートと、全体ゲート(_unclaimed)の両方が対象。
    全体ゲートは特定セッションの持ち物ではなく全員を止めるものなので、
    止められている側が解除できなければ筋が通らない（解除できずに TTL まで
    Step C が通らないデッドロックを実際に踏んだ）。
    """
    removed = False
    for path in {_gate_path(session), _gate_path(None)}:
        try:
            if path.exists():
                path.unlink()
                removed = True
        except OSError as exc:
            print(f"[cgd wf-gate] WARN: ゲート解除に失敗 ({path.name}): {exc}", file=sys.stderr)
    return removed


def _load(path: Path) -> tuple[dict | None, bool]:
    """(gate, corrupt) を返す。corrupt=True は「在るのに読めない」= 遮断側に倒す合図。"""
    try:
        if not path.exists():
            return None, False
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None, True
    if not isinstance(data, dict) or data.get("level") not in WF_REQUIRED_LEVELS:
        return None, True
    try:
        expires = datetime.strptime(str(data.get("expires_at", "")), _TS_FMT)
    except ValueError:
        return None, True
    if datetime.now() >= expires:
        try:
            path.unlink()
        except OSError:
            pass
        return None, False
    return data, False


def read_gate(session: str | None = None) -> tuple[dict | None, bool]:
    """このセッションを遮断するゲートを返す。

    「自セッション用ゲート」または「セッション未指定で張られた全体ゲート」のどちらかが
    有効ならブロック対象。**claim（所有権の移動）はしない** — read→write→unlink の
    レースと、claim 後に disarm できなくなるデッドロックの原因だったため（Step C 指摘）。
    """
    own, corrupt = _load(_gate_path(session))
    if own is not None or corrupt:
        return own, corrupt
    if session is None:
        return None, False
    return _load(_gate_path(None))


# --------------------------------------------------------------------------
# hook 本体
# --------------------------------------------------------------------------
def _deny_reason(gate: dict) -> str:
    level = gate["level"]
    script = gate.get("workflow_script", _WORKFLOW_SCRIPT.get(level, ""))
    used = gate.get("wf_used_at")
    head = (
        f"[cgd wf-gate] Lv{level} は Workflow 実行が必須です。codex に触れるコマンドを遮断しました。\n"
        f"（このゲートは {gate.get('armed_at')} に Lv{level} を選んだ時点で張られ、"
        f"{gate.get('expires_at')} に失効します）\n"
        "※ ゲート中は `grep \"codex ...\"` や `codex login status` など"
        "**起動でないコマンドもまとめて止まります**（シェルを解析せず安全側に倒すため）。\n"
    )
    if used:
        return (
            head
            + f"\n**Workflow は既に {used} に実行済みです。**レビュー段は終わっているので、\n"
            "次のコマンドでゲートを解除してから Step C の再レビューに進んでください:\n"
            '  python "C:/ClaudeCode/.claude/hooks/cgd_wf_gate.py" disarm\n'
        )
    return (
        head
        + "\n次に打つコマンド:\n"
        f'  Workflow({{ scriptPath: "{script}", args: {{ input_path, aux_input_path, label, wf_nonce }} }})\n'
        f"  wf_nonce は次で取得: python \"C:/ClaudeCode/.claude/hooks/cgd_wf_gate.py\" nonce\n"
        "\n手順は cgd/SKILL.md「Workflow 経由実行 (Lv6-WF / Lv7-WF / Lv8-WF)」節。\n"
        "Workflow 完了後は disarm を実行してください（自動解除はしません）:\n"
        '  python "C:/ClaudeCode/.claude/hooks/cgd_wf_gate.py" disarm\n'
        "\nWorkflow が使えない事情がある場合のみ、正しい nonce を付けて意図的に迂回できます。\n"
        "迂回した理由は必ずユーザーに伝えてください。"
    )


def _corrupt_reason() -> str:
    return (
        "[cgd wf-gate] ゲートファイルが壊れており、Lv6/7/8 の実行中かどうか判定できません。\n"
        "強制が形骸化しないよう安全側に倒して codex を遮断しました。\n"
        "状態を確認して解除してください:\n"
        '  python "C:/ClaudeCode/.claude/hooks/cgd_wf_gate.py" status\n'
        '  python "C:/ClaudeCode/.claude/hooks/cgd_wf_gate.py" disarm'
    )


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def handle_hook(payload: dict) -> dict | None:
    """deny する場合は hook 出力 dict、通す場合は None を返す。"""
    if payload.get("tool_name") != "Bash":
        return None
    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or not mentions_codex(command):
        return None

    session = payload.get("session_id") or None
    gate, corrupt = read_gate(session)
    if corrupt:
        return _deny(_corrupt_reason())
    if gate is None:
        return None

    nonce = extract_bypass_nonce(command)
    if nonce is not None and nonce == gate.get("nonce"):
        # WF が実際に走り出した。**解除はしない**(1 本目通過で無防備になる事故があったため)。
        # 走った事実だけ記録し、以後の deny メッセージで「disarm してよい」と案内する。
        if not gate.get("wf_used_at"):
            gate = dict(gate, wf_used_at=datetime.now().strftime(_TS_FMT))
            _write_gate(_gate_path(gate.get("session")), gate)
        return None

    return _deny(_deny_reason(gate))


def _run_hook() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        out = handle_hook(payload)
    except Exception:  # noqa: BLE001 — hook 自体の不具合で作業を止めない
        # 握り潰すと不発の原因を追えないので、必ずトレースを残す。
        print("[cgd wf-gate] WARN: hook 内で例外 (fail-open)", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
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
    p_arm.add_argument("--session", default=None, help="セッションID(省略時は最初に触れたセッションが所有)")
    p_arm.add_argument("--ttl-min", type=int, default=DEFAULT_TTL_MIN)

    for name, helptext in (("disarm", "ゲートを解除する"), ("status", "状態を表示する"), ("nonce", "WF に渡す nonce を表示する")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--session", default=None)

    args = parser.parse_args()

    if args.command == "arm":
        gate = arm(args.level, session=args.session, ttl_min=args.ttl_min)
        if gate is None:
            print(f"[cgd wf-gate] Lv{args.level} はゲート対象外 (対象: Lv{'/Lv'.join(map(str, WF_REQUIRED_LEVELS))})")
            return 0
        print(f"[cgd wf-gate] Lv{args.level}: WF 必須ゲートを張りました ({args.ttl_min} 分で失効)")
        print(f"[cgd wf-gate] wf_nonce = {gate['nonce']}  ← Workflow の args に渡すこと")
        return 0

    if args.command == "disarm":
        print("[cgd wf-gate] 解除しました" if disarm(args.session) else "[cgd wf-gate] ゲートは張られていません")
        return 0

    gate, corrupt = read_gate(args.session)
    if corrupt:
        print("[cgd wf-gate] ⚠️ ゲートファイルが壊れています (codex は遮断されます)。disarm してください")
        return 1
    if gate is None:
        print("[cgd wf-gate] 未設定 (codex は遮断されません)")
        return 0
    if args.command == "nonce":
        print(gate["nonce"])
        return 0
    used = gate.get("wf_used_at") or "未実行"
    owner = gate.get("session") or "(未所有)"
    print(
        f"[cgd wf-gate] Lv{gate['level']} 有効 / 所有 {owner} / 張った {gate['armed_at']}"
        f" / 失効 {gate['expires_at']} / WF実行 {used}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
