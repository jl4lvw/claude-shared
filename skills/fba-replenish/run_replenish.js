// FBA補充の判定を 022 PWA と同じ picking-select.js で実行し、結果を JSON で標準出力へ出す。
// 使い方: node run_replenish.js <picking-select.js のパス> <inventory/all の JSON ファイル>
// 判定の中身はここに書かない (PWA と二重にしない)。行から item (元データ) を外して出すだけ。
"use strict";
const fs = require("fs");

const [, , libPath, inputPath] = process.argv;
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
}));
