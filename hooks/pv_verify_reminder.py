"""UserPromptSubmit hook: pv の未検証 run を毎ターン可視化する.

背景 (2026-08-11):
  pv では Workflow が Bash を持てないため、成果物が揃っているかの
  **唯一の非 LLM ゲート**は「主 context が自分で
  `pv_plan.py collect --run <RUN> --include-merge` を叩くこと」になっている。
  ところが担保が SKILL.md の「省略禁止」という文言だけで、
  memory には「事後の任意チェックは急いでいるときに必ず飛ばされる」という
  実事故の記録がある。pv 自身の自己レビューでも「cgd の hook に相当する
  強制が無く、規律に依存している」と指摘された。

  そこで build が `<run>/.pending_verify` を置き、collect --include-merge が
  exit 0 したときだけ消す。本 hook はその印が残っている run を毎ターン提示する。

  **遮断はしない。** pv の run は数分かかるので、走っている最中に
  作業を止めるのは害の方が大きい。忘れたまま結果を採用することを防ぐのが目的。

コスト: ディレクトリ走査のみ (数ミリ秒)。ファイル内容は読まない。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

PV_ROOT = Path(os.environ.get("PV_ROOT", r"C:/tmp-ai/pv"))
PENDING_NAME = ".pending_verify"
PY = 'python "C:/ClaudeCode/.claude/tools/pv_plan.py"'
MAX_SHOWN = 5


def find_pending() -> list[dict]:
    """未検証 run の一覧。

    実体は `pv_plan.list_pending()` に 1 本化してある。以前はここに同じ処理を
    書き写していて、片方だけ直すと挙動がずれる状態だった
    （2026-08-12 cgd Lv8 で 3 者が指摘）。import は **PYTHONPATH に依存しない**
    ようファイルパス指定で行う（cwd 差で壊れる、という DS の指摘への対応）。
    """
    try:
        import importlib.util

        tools = Path(__file__).resolve().parent.parent / "tools" / "pv_plan.py"
        spec = importlib.util.spec_from_file_location("pv_plan_for_hook", tools)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod.list_pending()
    except (OSError, ImportError, AttributeError, SyntaxError):
        # 取り込めない具体的な理由だけを握る。何でも握ると、pv_plan 側の
        # 実バグを無言で隠す（2026-08-12 cgd Lv8・Qwen 指摘と同趣旨）。
        pass

    # 取り込めなかった場合だけ、最小限の走査で代替する（通知が消えるより良い）。
    out: list[dict] = []
    try:
        if not PV_ROOT.is_dir():
            return out
        for p in sorted(PV_ROOT.glob(f"*/{PENDING_NAME}")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                out.append({"run": p.parent.name, "created_at": "?"})
    except OSError:
        pass
    return out


def build_message(pending: list[dict]) -> str:
    lines = [
        f"[pv] Step 4 の検証が済んでいない run が {len(pending)} 件あります。",
        "pv は Workflow が Bash を持てないため、**主 context が自分で collect を叩くことが"
        "唯一の非 LLM ゲート**です。結果を採用する前に必ず実行してください:",
    ]
    for item in pending[:MAX_SHOWN]:
        run = item.get("run", "?")
        lines.append(f"  {PY} collect --run {run} --include-merge   (作成 {item.get('created_at', '?')})")
    if len(pending) > MAX_SHOWN:
        lines.append(f"  ... 他 {len(pending) - MAX_SHOWN} 件")
    lines.append(
        "exit 0 を確認すると印が消えてこの通知も止まります。"
        "使わなくなった run は run ディレクトリごと消しても構いません。"
    )
    return "\n".join(lines)


def main() -> int:
    try:
        json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        pass  # payload が読めなくても走査自体は可能

    try:
        pending = find_pending()
    except Exception:  # noqa: BLE001 — 通知の失敗で作業を止めない
        return 0
    if not pending:
        return 0

    out = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": build_message(pending),
        }
    }
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
