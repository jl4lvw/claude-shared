// FBA補充の判定を 022 PWA と同じ picking-select.js で実行し、結果を JSON で標準出力へ出す。
// 使い方: node run_replenish.js <picking-select.js のパス> <inventory/all の JSON ファイル> [納品にかかる日数]
// 判定の中身はここに書かない (PWA と二重にしない)。行から item (元データ) を外して出すだけ。
// near (準A) は Claude が判断するための追加の候補: PWA の除外条件 (isExcluded) を通り、機械では対象外だが
// 30日売上の (30 + 納品にかかる日数) 日分に FBA在庫+入荷中 が届かないもの。
"use strict";
const fs = require("fs");

const [, , libPath, inputPath, leadArg] = process.argv;
const leadDays = Number(leadArg || 7);
if (!libPath || !inputPath) {
  console.error("usage: node run_replenish.js <picking-select.js> <inventory_all.json>");
  process.exit(2);
}
const PS = require(libPath);
const data = JSON.parse(fs.readFileSync(inputPath, "utf8"));
const items = Array.isArray(data) ? data : data.items;
if (!Array.isArray(items)) {
  console.error("items が配列ではありません");
  process.exit(2);
}

const res = PS.pickReplenish(items, {});
const strip = (r) => {
  const { item, ...rest } = r;
  return Object.assign(rest, {
    product_name: item.product_name || "",
    hq_warehouse_qty: item.hq_warehouse_qty ?? null,
    hq_warehouse_infinite: !!item.hq_warehouse_infinite,
    source_7s_only: !!item.source_7s_only,
  });
};
const picked = new Set(res.rows.concat(res.blocked).map((r) => r.sku));
const near = [];
for (const it of items) {
  if (picked.has(it.sku) || PS.isExcluded(it, "fba", PS.effectiveIsFba)) continue;
  const sales = Number(it.sales_30d);
  if (it.sales_30d === null || it.sales_30d === undefined || !(sales > 0)) continue;
  const fba = Number(it.fulfillable_qty) || 0;
  const inbound = PS.inboundFba(it);
  if (fba + inbound >= Math.ceil((sales / 30) * (30 + leadDays))) continue;
  const airlogi = it.airlogi_qty === null || it.airlogi_qty === undefined ? null : Number(it.airlogi_qty);
  near.push({
    sku: it.sku, item: it, tier: "near", source: it.source_7s_only ? "7s" : "hq",
    send: 0, need: 0, extra: 0, short7s: 0, overset: false,
    fba, inbound, sales, airlogi, inbound7s: Number(it.inbound_7s_qty) || 0,
    reqFba: it.required_qty === null || it.required_qty === undefined ? null : Number(it.required_qty),
  });
}

process.stdout.write(JSON.stringify({
  constants: {
    sales_factor: PS.REPLEN_SALES_FACTOR,
    overset_ratio: PS.REPLEN_OVERSET_RATIO,
    keep_7s: PS.REPLEN_KEEP_7S,
  },
  has_source_7s_only_field: items.some((it) => "source_7s_only" in it),
  total_items: items.length,
  unknown: res.unknown,
  rows: res.rows.map(strip),
  blocked: res.blocked.map(strip),
  near: near.map(strip),
  lead_days: leadDays,
}));
