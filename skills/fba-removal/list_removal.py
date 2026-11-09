"""親 SKU(または子 SKU)から、FBA から返送できる子 SKU と数を 022 の DB から読む(読み取りだけ).

使い方:
    python list_removal.py FJ-G2041-PK-SMKT-parent FJ-G2048-PK-GOSOU-parent
    python list_removal.py --json FJ-G2124-PK-ETJM2-parent

親 SKU(-parent で終わる)はその子すべて、それ以外は SKU そのものを対象にする。
返送数 = 販売可能数(fulfillable_qty)。引当・入出荷作業中・販売不可があれば注意に出す。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

DB_PATH = Path(r"C:\ClaudeCode\022.Amazon在庫PWA\server\amzinv.db")


@dataclass
class Row:
    parent_sku: str
    sku: str
    name: str
    channel: str
    fulfillable: int
    unfulfillable: int
    reserved: int
    inbound: int
    notes: list[str]


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def load_rows(con: sqlite3.Connection, keys: list[str]) -> tuple[list[Row], list[str]]:
    """対象の子 SKU を読む。見つからなかった指定は 2 つ目の戻り値に返す."""
    rows: list[Row] = []
    missing: list[str] = []
    q = (
        "select parent_sku, sku, coalesce(nullif(product_name_user_override,''), product_name) as name,"
        " fulfillment_channel, is_fba, coalesce(fulfillable_qty,0) f, coalesce(unfulfillable_qty,0) u,"
        " coalesce(reserved_total_qty,0) r,"
        " coalesce(inbound_working_qty,0)+coalesce(inbound_shipped_qty,0)+coalesce(inbound_receiving_qty,0) i"
        " from inventory_items where {cond} order by sku"
    )
    for key in keys:
        cond, arg = ("parent_sku = ?", key) if key.endswith("-parent") else ("sku = ?", key)
        found = con.execute(q.format(cond=cond), (arg,)).fetchall()
        if not found:
            missing.append(key)
            continue
        for r in found:
            notes: list[str] = []
            if not r["is_fba"]:
                notes.append("FBAではない")
            if r["r"]:
                notes.append(f"引当{r['r']}")
            if r["i"]:
                notes.append(f"入出荷作業中{r['i']}")
            if r["u"]:
                notes.append(f"販売不可{r['u']}")
            rows.append(Row(r["parent_sku"] or key, r["sku"], r["name"] or "", r["fulfillment_channel"] or "",
                            int(r["f"]), int(r["u"]), int(r["r"]), int(r["i"]), notes))
    return rows, missing


def last_full_sync(con: sqlite3.Connection) -> str:
    r = con.execute(
        "select finished_at from sync_runs where status='success' and kind='full' order by id desc limit 1"
    ).fetchone()
    return f"{r[0]} (UTC)" if r else "不明"


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("skus", nargs="+", help="親 SKU(-parent)または子 SKU")
    ap.add_argument("--json", action="store_true", help="JSON で出す")
    a = ap.parse_args(argv)

    if not DB_PATH.exists():
        print(f"DB が見つかりません: {DB_PATH}", file=sys.stderr)
        return 2
    con = _connect()
    try:
        rows, missing = load_rows(con, a.skus)
        synced = last_full_sync(con)
    finally:
        con.close()

    targets = [r for r in rows if r.fulfillable > 0]
    total = sum(r.fulfillable for r in targets)
    if a.json:
        print(json.dumps({"last_full_sync": synced, "missing": missing, "total": total,
                          "rows": [asdict(r) for r in rows]}, ensure_ascii=False, indent=2))
    else:
        print(f"022 の最終全件同期: {synced}")
        for r in rows:
            mark = "" if r.fulfillable > 0 else "  (返送なし)"
            note = f"  注意: {'・'.join(r.notes)}" if r.notes else ""
            print(f"{r.parent_sku}\t{r.sku}\t返送{r.fulfillable}{mark}{note}")
        print(f"合計: {len(targets)} SKU・{total} 点")
        if missing:
            print(f"見つからない指定: {', '.join(missing)}")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
