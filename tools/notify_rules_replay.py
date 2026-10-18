"""HQ ボードに残るターン終了メッセージに、現在の通知ルールを当てて、取りこぼし/鳴りすぎを数える (2026-10-17)。

使い方: python .claude/tools/notify_rules_replay.py [--show 10]
  - 「旧判定」は HQ ボードが付けた status (waiting_answer = 返信待ち)
  - 「いま」は hooks/notify_rules.py の現在の設定(端末の上書き/キャッシュ/既定)での判定
  ルールを変えたときの前後比較に使う。本文は画面に出さない(--show で末尾 100 字だけ)。
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1] / "hooks"
BOARD = Path(__file__).resolve().parents[2] / ".hq" / "board"
sys.path.insert(0, str(HOOKS))

import notify_rules as nr  # noqa: E402


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001, S110
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", type=int, default=0, help="取りこぼし候補の例を N 件表示する")
    args = ap.parse_args()

    cfg = nr.load_config()
    rows = []
    for f in glob.glob(str(BOARD / "*.json")):
        if f.endswith(".push.json"):
            continue
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        tail = d.get("last_assistant_tail") or ""
        if tail and "Stop" in (d.get("events") or {}):
            rows.append((d.get("status"), tail, d.get("title") or ""))

    waiting = [(t, ti) for s, t, ti in rows if s == "waiting_answer"]
    caught = [(t, ti) for t, ti in waiting if nr.evaluate(t, cfg).notify]
    others = [(t, ti) for s, t, ti in rows if s != "waiting_answer"]
    extra = [(t, ti) for t, ti in others if nr.evaluate(t, cfg).notify]
    print(f"Stop まで進んだセッション: {len(rows)} 件 (設定の版: {cfg['version']})")
    print(f"旧判定が返信待ち: {len(waiting)} 件 → いま通知する: {len(caught)} 件 ({len(caught) * 100 // max(len(waiting), 1)}%)")
    print(f"旧判定が返信待ち以外: {len(others)} 件 → いま通知する: {len(extra)} 件 (うち本当の待ちも含む。目視で確認)")
    reasons = collections.Counter(nr.evaluate(t, cfg).reason for t, _ in extra)
    print("通知する理由の内訳(返信待ち以外):", dict(reasons))
    if args.show:
        print("\n--- 旧判定は返信待ちだが、いま通知しない例")
        for t, ti in [x for x in waiting if x not in caught][: args.show]:
            print(" ・", ti[:18], "|", t[-100:].replace("\n", " "))
    return 0


if __name__ == "__main__":
    sys.exit(main())
