"""cgd_verify_reminder — cgd の run で collect が未実施のものを毎ターン提示する.

なぜ必要か:
    cgd の Review 段は agent の自己申告（executed / findings / raw_log_path）で
    成否が決まっており、生ログが 1 バイトも無くても「指摘なし」として通る。
    それを塞ぐのが `cgd_plan.py collect`（生ログの実在・サイズ・構造を Python が判定）
    だが、**叩くかどうかを人の判断に委ねると急いでいるときに飛ぶ**。
    pv で同じ問題に当たり、印 + リマインダーで解いた実績があるので同じ形にする。

    印を消せるのは collect が成功したときだけ。Workflow 側からは消さない
    （pv では WF 内の Verify が印を消してしまい、正常系でリマインダーが
    一度も鳴らないという本末転倒が起きた）。

    **遮断はしない。** run は数分かかるので、走行中に作業を止める害の方が大きい。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

ROOT = Path("C:/tmp-ai/cgd")
PENDING_NAME = ".pending_verify"
MAX_SHOW = 3


def main() -> int:
    try:
        if not ROOT.is_dir():
            return 0
        pendings = sorted(ROOT.glob(f"*/{PENDING_NAME}"))
    except OSError:
        return 0
    if not pendings:
        return 0

    lines = [
        f"[cgd] Step 検証が済んでいない run が {len(pendings)} 件あります。",
        "レビュー結果を採用する前に、**生ログが実際に書かれたか**を確認してください"
        "（レビュアーの成否は agent の自己申告なので、これが唯一の非 LLM ゲートです）:",
    ]
    for pend in pendings[:MAX_SHOW]:
        run = pend.parent.name
        lv = "?"
        plan = pend.parent / "plan.json"
        if plan.is_file():
            try:
                lv = json.loads(plan.read_text(encoding="utf-8")).get("level", "?")
            except (OSError, ValueError):
                pass
        lines.append(f'  Lv{lv}: python "C:/ClaudeCode/.claude/tools/cgd_plan.py" collect --run {run}')
    if len(pendings) > MAX_SHOW:
        lines.append(f"  … 他 {len(pendings) - MAX_SHOW} 件 (python .claude/tools/cgd_plan.py list)")
    lines.append("exit 0 を確認すると印が消えてこの通知も止まります。"
                 "使わなくなった run はディレクトリごと消して構いません。")

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
