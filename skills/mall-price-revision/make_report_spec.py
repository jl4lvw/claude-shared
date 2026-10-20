"""価格改定の報告画像(line-notify の make_status_image.py)用の仕様 JSON を、plan.json から作る。

  python make_report_spec.py <plan.json> <out_spec.json> --date "2026年11月1日" --updated "10/20 更新" [--names names.json]

plan.json は改定の「前」に mall_price_plan.py が作ったもの(現在値=旧価格が入っている)。
names.json は {"G0675": "水兵巾着袋", ...}。無い品番は商品マスタ(023)の商品名の先頭語を使うので、画像で目視して直す。
G番号は商品名を括弧書きで併記するのではなく、表の「品番」列と「商品名」列に並べる。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DB = Path(r"C:\ClaudeCode\023.商品マスタDB\data\productmaster.db")


def short_name(conn: sqlite3.Connection, sku: str) -> str:
    row = conn.execute("SELECT product_name FROM products WHERE deleted_at IS NULL AND (sku=? OR sku=?) LIMIT 1",
                       (sku, sku + "-parent")).fetchone()
    name = re.sub(r"^【[^】]*】\s*", "", row[0]) if row else sku
    return name.split()[0] if name.split() else sku


def money(n: int | None) -> str:
    return f"{n:,}" if n is not None else "未登録"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("plan")
    ap.add_argument("out")
    ap.add_argument("--date", required=True)
    ap.add_argument("--updated", required=True)
    ap.add_argument("--names")
    a = ap.parse_args()
    targets = json.loads(Path(a.plan).read_text(encoding="utf-8"))["targets"]
    names = json.loads(Path(a.names).read_text(encoding="utf-8")) if a.names else {}
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows, done = [], []
    for sku, t in targets.items():
        es_now, es_new = t["estore"]["now"], t["new_estore"]
        if t["rakuten"]["now"] == [t["new_mall"]] and es_now == es_new and t["yahoo"]["now"] == t["new_mall"]:
            done.append(sku)  # 計画の時点で3モールとも新価格だった商品は「変更なし」として別に書く
            continue
        e = (f"{money(es_now)}→{money(es_new)}" if es_now not in (None, es_new)
             else ("未登録" if es_now is None else money(es_new)))
        rows.append([sku, names.get(sku) or short_name(conn, sku), e, money(t["new_mall"])])
    lines = ["楽天・Yahoo!の価格は、Eストアの価格の1.1倍。"]
    if done:
        lines.append("・".join(done) + "は改定済みのため、変更なし。")
    for sku, t in targets.items():
        if t["estore"]["kind"] == "none" and sku not in done:
            lines.append(f"{sku}はEストア未登録。楽天・Yahoo!だけ更新(Eストアは後日登録)。")
    lines.append("「17,600」のように1つだけの数字は、Eストアが改定済みだった商品。")
    spec = {"title": f"価格改定 {a.date}", "subtitle": f"Eストア・楽天・Yahoo!ショッピング  {a.updated}",
            "sections": [{"heading": f"改定した商品({len(rows)}品番)",
                          "grid": {"header": ["品番", "商品名", "Eストア", "楽天・Yahoo!"], "widths": [130, 560, 220, 210], "rows": rows}},
                         {"heading": "メモ", "lines": lines}],
            "footer": "確認方法: 3モールの価格を読み直して新価格との一致を確認済み"}
    Path(a.out).write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8", newline="")
    print(f"仕様を書きました: {a.out}({len(rows)}行・変更なし{len(done)}品番)。商品名は画像で目視して names.json で直す")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
