"""子SKUのキーの付け替え(sku-rekey)のあとの確認。読み取りだけ。

使い方: python C:/ClaudeCode/.claude/skills/sku-rekey/verify.py G0862-parent
確かめること: 023 の子のキー=SKU番号・variant_id が残っている / 092 の台帳・切り替え済み・未統一 /
書き先(楽天・Yahoo!・Eストア) / 092 内部API / 023 の切り替え済み判定(GoQ へ書かない判定)
"""
from __future__ import annotations

import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
PM = Path(r"C:\ClaudeCode\023.商品マスタDB")
SH = Path(r"C:\ClaudeCode\092.受注影武者と在庫台帳")
PM_DB = PM / "data" / "productmaster.db"
SH_DB = SH / "data" / "shadow.db"


def main(parent: str) -> int:
    ng: list[str] = []
    c = sqlite3.connect(f"file:{PM_DB.as_posix()}?mode=ro", uri=True)
    kids = c.execute("SELECT sku, rakuten_variant_id, rakuten_merchant_sku FROM products"
                     " WHERE parent_sku=? AND deleted_at IS NULL ORDER BY sku", (parent,)).fetchall()
    c.close()
    keys = [k[0] for k in kids]
    for sku, vid, mds in kids:
        if (mds or "").upper() != sku.upper():
            ng.append(f"023 {sku}: キーとSKU番号({mds})が違う")
        if not vid:
            ng.append(f"023 {sku}: rakuten_variant_id が空")
    print(f"023 子 {len(kids)}件:", ", ".join(keys))

    s = sqlite3.connect(f"file:{SH_DB.as_posix()}?mode=ro", uri=True)
    q = ",".join("?" * len(keys))
    stock = dict(s.execute(f"SELECT sku, qty FROM sku_stock WHERE sku IN ({q})", keys).fetchall())
    cut = {r[0] for r in s.execute(f"SELECT sku FROM cutover_sku WHERE sku IN ({q})", keys)}
    s.close()
    print("092 台帳:", stock or "(無し)", f"/ 自前連携 {len(cut)}/{len(keys)}")

    sys.path.insert(0, str(SH))
    from shadow.mall_targets import TargetIndex
    from shadow.web.catalog import Catalog, unify_state
    cat = Catalog.from_db(PM_DB)
    p = cat.parents.get(parent)
    bad, total = unify_state(p) if p else (-1, 0)
    print(f"092 未統一: {bad}/{total}")
    if bad:
        ng.append(f"092 で未統一が {bad} 件")
    ti = TargetIndex.from_db(PM_DB)
    for k in keys:
        t = ti.targets_for(k)
        if t.problems or set(t.targets) != {"rakuten", "yahoo", "estore"}:
            ng.append(f"書き先 {k}: {sorted(t.targets)} {t.problems}")
    print("書き先: 確認済み" if not any(x.startswith("書き先") for x in ng) else "書き先: 問題あり")

    from shadow.web.internal_api import load_key
    req = urllib.request.Request(f"http://127.0.0.1:8337/internal/v1/stock?skus={','.join(keys)}",
                                 headers={"X-API-Key": load_key(), "X-Caller": "sku_rekey_verify"})
    items = json.load(urllib.request.urlopen(req, timeout=10))["items"]
    for it in items:
        if it["sku"] in cut and not it["managed"]:
            ng.append(f"内部API {it['sku']}: managed=false")
    print("092 内部API:", {it["sku"]: it["qty"] for it in items})

    sys.path.insert(0, str(PM))
    from server.services import shadow_cutover
    got = shadow_cutover.cutover_skus_for(parent)
    if cut and sorted(got) != sorted(cut):
        ng.append(f"023 の切り替え済み判定({parent}): {got}")
    print("023 切り替え済み判定:", got)

    for x in ng:
        print("[NG]", x)
    print("OK" if not ng else f"NG {len(ng)}件")
    return 0 if not ng else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
