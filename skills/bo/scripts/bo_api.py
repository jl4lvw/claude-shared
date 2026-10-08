"""BoxQR 発注予約 API クライアント（/bo スキル用）。

読み取り（plans / table / events）は認証なし。書き込み（claim / release / ordered /
failed / cancelled / reopen）は X-API-Key が必要で、**既定は dry-run**（送る内容を表示するだけ）。
実際に送るには --yes を付ける。--yes は運用者の承認を得たあとにだけ付けること。

API キーは環境変数ではなく、プロジェクト外のファイルから読む（値を画面・ログ・記録に出さない）。
    既定: C:\\Users\\<ユーザー>\\.boxqr\\order_api_key.txt （1行・キーだけ）
    上書き: 環境変数 BOXQR_KEY_FILE にファイルのパス

使い方:
    python bo_api.py plans [--status pending|active|ordering|ordered|failed|cancelled|all]
    python bo_api.py table [--status pending]            # 予定 ＋ 7S ＋ FBA の表（品番ごと）
    python bo_api.py events [--plan-id N] [--limit 20]
    python bo_api.py claim --ids 4,5,6 [--ttl 3600] [--yes]
    python bo_api.py ordered --id 4 --order-ref 注文番号 [--ordered-qty 13] [--yes]
    python bo_api.py failed --ids 4 --note 理由 [--yes]
    python bo_api.py release --ids 4 [--yes]
    python bo_api.py cancelled --ids 4 [--note メモ] [--yes]
    python bo_api.py reopen --ids 4 [--yes]
    python bo_api.py verify-key                           # キーの有効確認（何も変更しない）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

BASE_URL = "https://boxqr.sfuji.f5.si"
AGENT = "TK-order-AI"   # HTTPヘッダーは ASCII が安全。履歴では「接続元IP/TK-order-AI」と表示される
DEFAULT_KEY_FILE = Path.home() / ".boxqr" / "order_api_key.txt"
TIMEOUT = 30


class BoxQRError(Exception):
    """API・キー・入力に関するエラー（メッセージはそのまま利用者に見せてよい）。"""


# ---------- 低レベル ----------
def _key_file() -> Path:
    return Path(os.environ.get("BOXQR_KEY_FILE") or DEFAULT_KEY_FILE)


def load_key() -> str:
    path = _key_file()
    if not path.exists():
        raise BoxQRError(
            f"APIキーのファイルがありません: {path}\n"
            "キーだけを1行で書いたファイルを、運用者がプロジェクトの外に作成してください。"
        )
    key = path.read_text(encoding="utf-8").strip()
    if not key:
        raise BoxQRError(f"APIキーのファイルが空です: {path}")
    return key


def _request(method: str, path: str, body: dict | None = None, key: str | None = None) -> Any:
    url = BASE_URL + path
    data = None
    headers = {"X-Agent": AGENT}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if key:
        headers["X-API-Key"] = key
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8")).get("detail", "")
        except Exception:  # noqa: BLE001 - 本文が JSON でなくても HTTP コードは伝える
            pass
        hint = {
            401: "キーが違う、またはヘッダーなし",
            503: "サーバー側でキーが未設定",
            409: "数量・version の不一致、または状態が合わない",
            404: "指定した id または品番がない",
        }.get(e.code, "")
        raise BoxQRError(f"HTTP {e.code} {hint} {detail}".strip()) from e
    except urllib.error.URLError as e:
        raise BoxQRError(f"接続できません: {e.reason}") from e


# ---------- 読み取り ----------
def get_plans(status: str = "pending", product_code: str | None = None) -> list[dict]:
    q = {"status": status}
    if product_code:
        q["product_code"] = product_code
    return _request("GET", "/api/v1/order-plans?" + urllib.parse.urlencode(q))


def get_product(code: str) -> dict:
    return _request("GET", "/api/v1/products/" + urllib.parse.quote(code))


def get_events(plan_id: int | None = None, limit: int = 20) -> list[dict]:
    q: dict[str, Any] = {"limit": limit}
    if plan_id is not None:
        q["plan_id"] = plan_id
    return _request("GET", "/api/v1/order-plans/events?" + urllib.parse.urlencode(q))


def verify_key(key: str | None = None) -> bool:
    """キーが有効か確認する。存在しない予定ID(999999)を release するだけなので、何も変更されない。

    有効なら {"changed": [], "skipped": [{"reason": "not_found"}]}、無効なら 401。
    """
    try:
        result = _request("POST", "/api/v1/order-plans/release", {"ids": [999999]}, key=key or load_key())
    except BoxQRError as e:
        if "HTTP 401" in str(e):
            return False
        raise
    return not result.get("changed")


# ---------- 表の組み立て（純粋関数） ----------
def group_by_product(plans: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for p in plans:
        out.setdefault(p["product_code"], []).append(p)
    return out


def build_stock_rows(product: dict, plans: list[dict]) -> list[dict]:
    """サイズごとの 7S・FBA（販売可/入荷中）・予定数。本店と GoQ 枠は含めない（運用者決定 2026-10-06）。"""
    plan_by_sku = {p["sku"]: p for p in plans}
    rows = []
    for c in product.get("cells", []):
        s7 = c.get("s7_qty") or 0
        fba = c.get("fba_fulfillable") or 0
        inb = c.get("fba_inbound") or 0
        plan = plan_by_sku.get(c["sku"])
        rows.append({
            "size": c["size"], "s7": s7, "fba": fba, "fba_inbound": inb,
            "stock_total": s7 + fba + inb, "plan": plan["qty"] if plan else None,
            "plan_id": plan["id"] if plan else None,
        })
    # 予定があるのに在庫セルが無い SKU も落とさない
    known = {c["sku"] for c in product.get("cells", [])}
    for sku, p in plan_by_sku.items():
        if sku not in known:
            rows.append({"size": p["size"] + "（在庫セルなし）", "s7": 0, "fba": 0, "fba_inbound": 0,
                         "stock_total": 0, "plan": p["qty"], "plan_id": p["id"]})
    return rows


def format_table(code: str, name: str, color_codes: list[str], rows: list[dict], last_order: dict | None) -> str:
    lines = [f"■ {code} {name}　色: {', '.join(color_codes) or '-'}"]
    lines.append("サイズ |  7S | FBA販売可 | FBA入荷中 | 在庫計 | 予定数 | 予定ID")
    for r in rows:
        plan = "" if r["plan"] is None else str(r["plan"])
        pid = "" if r["plan_id"] is None else str(r["plan_id"])
        lines.append(f"{r['size']:>6} | {r['s7']:>3} | {r['fba']:>8} | {r['fba_inbound']:>8} | {r['stock_total']:>5} | {plan:>5} | {pid:>5}")
    total_plan = sum(r["plan"] or 0 for r in rows)
    lines.append(f"予定数の合計: {total_plan}" + ("" if total_plan % 25 == 0 else "（25の倍数ではありません）"))
    if last_order:
        sizes = " ".join(f"{k}{v}" for k, v in (last_order.get("sizes") or {}).items())
        lines.append(f"台帳の直近の発注: 出荷 {last_order.get('ship_date')} {last_order.get('name')} "
                     f"{last_order.get('material')} {last_order.get('color')} {sizes} 計{last_order.get('total')}")
    else:
        lines.append("台帳の直近の発注: 見つからない（未発注とは限らない）")
    return "\n".join(lines)


# ---------- 書き込み（payload は純粋関数） ----------
def build_claim_payload(plans: list[dict], ids: list[int], ttl: int | None) -> dict:
    by_id = {p["id"]: p for p in plans}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise BoxQRError(f"pending に無い予定ID: {missing}")
    payload: dict[str, Any] = {"items": [{"id": i, "expected_qty": by_id[i]["qty"]} for i in ids]}
    if ttl is not None:
        if not 60 <= ttl <= 21600:
            raise BoxQRError("ttl は 60〜21600 秒です")
        payload["ttl_sec"] = ttl
    return payload


def build_ordered_payload(plan_id: int, order_ref: str, ordered_qty: int | None, note: str) -> dict:
    item: dict[str, Any] = {"id": plan_id, "order_ref": order_ref}
    if ordered_qty is not None:
        item["ordered_qty"] = ordered_qty
    payload: dict[str, Any] = {"items": [item]}
    if note:
        payload["note"] = note
    return payload


def build_ids_payload(ids: list[int], note: str, require_note: bool) -> dict:
    if require_note and not note:
        raise BoxQRError("failed には理由（--note）が必須です")
    payload: dict[str, Any] = {"ids": ids}
    if note:
        payload["note"] = note
    return payload


def check_result(result: dict) -> None:
    """POST の応答。skipped があれば内容を見せて失敗扱いにする（一部だけ通ることがあるため）。"""
    skipped = result.get("skipped") or []
    print(f"changed: {len(result.get('changed') or [])} 件")
    for r in result.get("changed") or []:
        print(f"  id={r['id']} {r['sku']} qty={r['qty']} status={r['status']} order_ref={r.get('order_ref')}")
    if skipped:
        print(f"skipped: {len(skipped)} 件  ← 必ず確認すること")
        for s in skipped:
            print("  ", json.dumps(s, ensure_ascii=False))
        raise BoxQRError("skipped があります。changed に入った行だけが反映されています。")


def send_write(action: str, payload: dict, yes: bool) -> None:
    path = f"/api/v1/order-plans/{action}"
    print(f"POST {path}  X-Agent={AGENT}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not yes:
        print("\n[dry-run] 送信していません。運用者の承認を得たら --yes を付けて実行してください。")
        return
    result = _request("POST", path, payload, key=load_key())
    check_result(result)


# ---------- CLI ----------
def _ids(s: str) -> list[int]:
    try:
        return [int(x) for x in s.split(",") if x.strip()]
    except ValueError as e:
        raise BoxQRError(f"IDは整数のカンマ区切りで指定してください: {s}") from e


def cmd_plans(a: argparse.Namespace) -> None:
    plans = get_plans(a.status, a.product_code)
    if a.json:
        print(json.dumps(plans, ensure_ascii=False, indent=1))
        return
    print(f"{len(plans)} 件（status={a.status}）")
    for p in plans:
        print(f"id={p['id']:>3} {p['product_code']} {p['sku']:<14} {p['size']:>3} qty={p['qty']:>3} "
              f"{p['status']:<9} v{p['version']} 入力元={p['actor']} {p['created_at'][:16]} claimed_by={p['claimed_by']}")


def cmd_table(a: argparse.Namespace) -> None:
    plans = get_plans(a.status, a.product_code)
    if not plans:
        print(f"予定はありません（status={a.status}）")
        return
    for code, ps in group_by_product(plans).items():
        prod = get_product(code)
        rows = build_stock_rows(prod, ps)
        colors = [c.get("code", "") for c in prod.get("colors", [])]
        last = (prod.get("orders") or {}).get("latest")
        print(format_table(code, prod.get("product_name", "(商品名なし)"), colors, rows, last))
        print()


def cmd_events(a: argparse.Namespace) -> None:
    for e in get_events(a.plan_id, a.limit):
        print(json.dumps(e, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="BoxQR 発注予約 API クライアント")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plans", "table"):
        p = sub.add_parser(name)
        p.add_argument("--status", default="pending")
        p.add_argument("--product-code")
        if name == "plans":
            p.add_argument("--json", action="store_true")
    p = sub.add_parser("events")
    p.add_argument("--plan-id", type=int)
    p.add_argument("--limit", type=int, default=20)
    sub.add_parser("verify-key", help="キーが有効か確認（何も変更しない）")
    p = sub.add_parser("claim")
    p.add_argument("--ids", required=True)
    p.add_argument("--ttl", type=int)
    p = sub.add_parser("ordered")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--order-ref", default="")
    p.add_argument("--ordered-qty", type=int)
    p.add_argument("--note", default="")
    for name in ("failed", "release", "cancelled", "reopen"):
        p = sub.add_parser(name)
        p.add_argument("--ids", required=True)
        p.add_argument("--note", default="")
    for name in ("claim", "ordered", "failed", "release", "cancelled", "reopen"):
        sub.choices[name].add_argument("--yes", action="store_true", help="実際に送る（運用者の承認後のみ）")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "plans":
            cmd_plans(a)
        elif a.cmd == "table":
            cmd_table(a)
        elif a.cmd == "events":
            cmd_events(a)
        elif a.cmd == "verify-key":
            ok = verify_key()
            print("キーは有効です（何も変更していません）" if ok else "キーが無効です（401）")
            return 0 if ok else 1
        elif a.cmd == "claim":
            ids = _ids(a.ids)
            send_write("claim", build_claim_payload(get_plans("pending"), ids, a.ttl), a.yes)
        elif a.cmd == "ordered":
            send_write("ordered", build_ordered_payload(a.id, a.order_ref, a.ordered_qty, a.note), a.yes)
        else:
            send_write(a.cmd, build_ids_payload(_ids(a.ids), a.note, a.cmd == "failed"), a.yes)
    except BoxQRError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
