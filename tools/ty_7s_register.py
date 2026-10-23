"""TY からの「Amazon 登録済み SKU の 7S 商品登録」依頼を、安全条件つきで実行する.

背景(2026-10-21): 運用者が「今後も TY からの依頼であれば、自動で登録して OK
(問題が発生しないと判断された場合のみ)」と述べた。待ち受けセッションは歯止め
フックで CMR への書込を拒否するため、条件の機械検査と書込を **この 1 本に閉じ込め**、
待ち受けの処理役はこのスクリプトを呼ぶだけにする。

使い方:
    python ty_7s_register.py --message-id 3366 --plan <tshirt4_7s_plan.json>          # 検査だけ(書込なし)
    python ty_7s_register.py --message-id 3366 --plan <...json> --apply               # 条件を満たせば登録

終了コード:
    0  登録完了(--apply なしなら「条件を満たす」)
    10 条件を満たさない(何も書いていない。理由を標準出力へ)
    11 登録を送ったが成否を確認できない/一部失敗(再送せず運用者へ報告)

安全条件(1 つでも外れたら何も書かず exit 10):
    - 依頼の送り元が TY(relay の受信箱で message_id の from を確認。見つからなければ拒否)
    - 件数 1〜100、各行に item_code/product_name/spec1/spec2/amazon_fba_sku
    - 品番が `G番号4桁-色2文字-サイズ` 形式、品番・SKU 重複なし
    - 022 の register-match で全件 FNSKU あり・FNSKU 重複なし・7S 未登録
    書込は商品マスタ登録(register-via-api)のみ。数量は送らない。
返信(relay)と done は呼び出し側が行う(歯止めフックの文言規則を守るため)。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = "http://127.0.0.1:8090/cart/7s/"
# 022 は state-changing リクエストの Origin を検証する(localhost は許可されている)
HEADERS = {"Content-Type": "application/json", "Origin": "http://127.0.0.1:8090"}
ALLOWED_SENDER = "TY"
MAX_ITEMS = 100
CODE_RE = re.compile(r"^G\d{4}-[A-Z]{2}-[0-9A-Z]{1,3}$")
REQUIRED = ("item_code", "product_name", "spec1", "spec2", "amazon_fba_sku")
OK_STATUSES = ("verified_ok", "callback_ok")
PENDING = (None, "pending", "sent", "queued", "accepted")
RELAY = Path(__file__).resolve().parents[1] / "skills" / "relay" / "scripts" / "relay_client.py"
LEDGER = Path(__file__).resolve().parents[1] / "relay_local" / "ty_7s_register_ledger.jsonl"


def validate_plan(plan: object) -> list[str]:
    """計画ファイルだけで分かる条件。問題の一覧を返す(空なら OK)。"""
    if not isinstance(plan, list) or not (1 <= len(plan) <= MAX_ITEMS):
        return [f"件数が 1〜{MAX_ITEMS} ではありません"]
    problems: list[str] = []
    for i, row in enumerate(plan):
        if not isinstance(row, dict) or any(not str(row.get(k) or "").strip() for k in REQUIRED):
            problems.append(f"{i + 1}行目: 必須項目が不足")
            continue
        if not CODE_RE.match(row["item_code"]):
            problems.append(f"{i + 1}行目: 品番の形式が違います({row['item_code']})")
    for key in ("item_code", "amazon_fba_sku"):
        vals = [r.get(key) for r in plan if isinstance(r, dict)]
        if len(set(vals)) != len(vals):
            problems.append(f"{key} に重複があります")
    return problems


def check_sender(message_id: int) -> str | None:
    """relay の受信箱(peek・副作用なし)で message_id の送り元を返す。見つからなければ None。"""
    out = subprocess.run(
        [sys.executable, str(RELAY), "check", "--peek"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    ).stdout
    m = re.search(rf"message_id={message_id}\s+from=(\S+)", out)
    return m.group(1) if m else None


def _post(path: str, body: dict) -> dict:
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), headers=HEADERS)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def _get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.load(r)


def match(plan: list[dict]) -> list[dict]:
    return _post("register-match", {"skus": [r["amazon_fba_sku"] for r in plan]})["results"]


def check_match(results: list[dict]) -> list[str]:
    problems = [f"FNSKU 未解決: {r['sku']}" for r in results if not r.get("fnsku")]
    fnskus = [r["fnsku"] for r in results if r.get("fnsku")]
    if len(set(fnskus)) != len(fnskus):
        problems.append("FNSKU に重複があります")
    # 2026-10-22: 通信失敗を「7S 未登録」と取り違えない。022 は既定では確認不能なら
    # HTTP 503 を返す(→ match() で refuse)が、status 付きで unknown が来た場合も登録しない。
    problems += [
        f"7S 登録の有無を確認できません(通信): {r.get('sku')}"
        for r in results if r.get("status") not in (None, "registered", "absent")
    ]
    problems += [f"7S に登録済み: {r['sku']}({r.get('item_code')})" for r in results if r.get("registered")]
    return problems


def log(entry: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), **entry}
    with LEDGER.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def refuse(message_id: int, reasons: list[str]) -> int:
    print("REFUSED(何も書いていません): " + " / ".join(reasons))
    log({"message_id": message_id, "result": "refused", "reasons": reasons})
    return 10


def run(message_id: int, plan_path: Path, apply: bool) -> int:
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return refuse(message_id, [f"計画ファイルを読めません({type(exc).__name__})"])
    sender = check_sender(message_id)
    if sender != ALLOWED_SENDER:
        return refuse(message_id, [f"送り元が {ALLOWED_SENDER} と確認できません(確認結果: {sender})"])
    problems = validate_plan(plan)
    if problems:
        return refuse(message_id, problems)
    try:
        results = match(plan)
    except (urllib.error.URLError, OSError, KeyError, ValueError) as exc:
        return refuse(message_id, [f"022 の照合に失敗({type(exc).__name__})"])
    problems = check_match(results)
    if problems:
        return refuse(message_id, problems)
    if not apply:
        print(f"OK(検査のみ・書込なし): {len(plan)}件 すべて条件を満たします")
        return 0

    items = [
        {"sku": r["amazon_fba_sku"], "item_code": r["item_code"], "product_name": r["product_name"],
         "spec1": r["spec1"], "spec2": r["spec2"]}
        for r in plan
    ]
    try:
        sent = _post("register-via-api", {"items": items})
        sid = sent["send_log_id"]
    except (urllib.error.URLError, OSError, KeyError, ValueError) as exc:
        # 送信が通ったか分からないので再送しない
        log({"message_id": message_id, "result": "send_unknown", "error": type(exc).__name__})
        print(f"UNCERTAIN: 送信結果が不明です({type(exc).__name__})。再送せず運用者へ報告してください")
        return 11
    status: dict = {}
    for _ in range(40):
        time.sleep(5)
        try:
            status = _get(f"register-via-api/status/{sid}")
        except (urllib.error.URLError, OSError, ValueError):
            continue
        if status.get("status") not in PENDING:
            break
    done = status.get("status") in OK_STATUSES
    # 登録後の再照合: 全件が 7S 登録済みか
    registered = 0
    try:
        registered = sum(1 for r in match(plan) if r.get("registered"))
    except (urllib.error.URLError, OSError, KeyError, ValueError):
        pass
    ok = done and registered == len(plan)
    log({"message_id": message_id, "result": "ok" if ok else "failed", "send_log_id": sid,
         "status": status.get("status"), "registered": registered, "count": len(plan)})
    print(f"{'DONE' if ok else 'FAILED'}: send_log={sid} status={status.get('status')} "
          f"登録確認={registered}/{len(plan)}")
    return 0 if ok else 11


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--message-id", type=int, required=True)
    ap.add_argument("--plan", type=Path, required=True)
    ap.add_argument("--apply", action="store_true", help="条件を満たすとき実際に登録する")
    args = ap.parse_args()
    return run(args.message_id, args.plan, args.apply)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
