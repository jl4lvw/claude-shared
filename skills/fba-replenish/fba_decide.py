"""Claude が決めた FBA補充の数を検査し、最終の表と取り込み JSON を作る (読み取りだけ)。

fba_replenish.py の出力フォルダにある materials.json と、Claude が書いた decisions.json を読む。

decisions.json の形:
  {"items": [{"sku": "...", "qty": 5, "source": "hq" | "7s", "reason": "直近7日が伸びている"}, ...]}

検査 (1 つでも当たれば何も書かずに終了コード 1):
  - materials.json の「一件ずつ見る」行は全件そろっていること (見落とし防止)
  - 知らない SKU・重複・数が 0 未満の整数でない・補充元が hq / 7s 以外
  - 機械の数と違う数にしたのに理由が無い
  - 7S から送る数が 7S在庫を超える

出力 (出力フォルダへ):
  decision_report.md   本文に貼る最終の表
  cart_import_hq.json  本店分のピッキングリスト取り込み用 (mode replace)

使い方:
  python fba_decide.py --dir C:/tmp-ai/fba_replenish/<日時> [--decisions <path>]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Decision:
    sku: str
    qty: int
    source: str
    reason: str


def load_decisions(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("decisions.json の items が配列ではありません")
    return items


def validate(materials: dict, raw: list[dict]) -> tuple[list[Decision], list[str], list[str]]:
    """(決定, エラー, 注意) を返す。"""
    rows = {r["sku"]: r for r in materials["review"]}
    errors: list[str] = []
    warns: list[str] = []
    out: list[Decision] = []
    seen: set[str] = set()
    for i, d in enumerate(raw):
        sku = str(d.get("sku", ""))
        if sku not in rows:
            errors.append(f"{i + 1} 行目: 材料に無い SKU です ({sku or '空'})")
            continue
        if sku in seen:
            errors.append(f"{sku}: 2 回書かれています")
            continue
        seen.add(sku)
        qty = d.get("qty")
        if not isinstance(qty, int) or isinstance(qty, bool) or qty < 0:
            errors.append(f"{sku}: 数は 0 以上の整数にしてください ({qty!r})")
            continue
        r = rows[sku]
        source = d.get("source") or r["source"]
        if source not in ("hq", "7s"):
            errors.append(f"{sku}: 補充元は hq か 7s です ({source!r})")
            continue
        reason = str(d.get("reason") or "").strip()
        if qty != r["machine"] and not reason:
            errors.append(f"{sku}: 機械の数 {r['machine']} と違う {qty} にした理由がありません")
            continue
        if source == "7s" and qty > 0:
            stock = r.get("airlogi")
            if stock is None:
                errors.append(f"{sku}: 7S在庫が分からないので 7S からは送れません")
                continue
            if qty > stock:
                errors.append(f"{sku}: 7S在庫 {stock} 個より多い {qty} 個は送れません")
                continue
            if qty == stock:
                warns.append(f"{sku}: 7S在庫を全部送るので FBM 用が 0 個になります")
        out.append(Decision(sku, qty, source, reason))
    missing = [s for s in rows if s not in seen]
    if missing:
        errors.append(f"決めていない商品が {len(missing)} 件あります: " + ", ".join(missing[:20])
                      + (" …" if len(missing) > 20 else ""))
    return out, errors, warns


def _cell(v: object) -> str:
    return "—" if v is None else str(v).replace("|", "／").replace("\n", " ")


def _table(cols: list[str], lines: list[list[object]]) -> list[str]:
    out = ["| " + " | ".join(cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    out += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in lines]
    return out


def build_report(materials: dict, decisions: list[Decision], warns: list[str]) -> str:
    rows = {r["sku"]: r for r in materials["review"]}
    send = [d for d in decisions if d.qty > 0]
    hq = [d for d in send if d.source == "hq"]
    s7 = [d for d in send if d.source == "7s"]
    up = [d for d in decisions if d.qty > rows[d.sku]["machine"]]
    down = [d for d in decisions if d.qty < rows[d.sku]["machine"]]
    lines = [
        f"# FBA補充 送る商品と数 ({materials['today']}・納品にかかる日数 {materials['lead_days']} 日)",
        "",
        f"- **送る: {len(send)} 件・{sum(d.qty for d in send)} 個**"
        f" (本店から {len(hq)} 件・{sum(d.qty for d in hq)} 個 / 7S から {len(s7)} 件・{sum(d.qty for d in s7)} 個)",
        f"- 機械の数より増やした {len(up)} 件・減らした(送らないを含む) {len(down)} 件",
        f"- 売上が無いので一括で送らない B: {len(materials['skipped_b'])} 件",
    ]
    lines += [f"- ⚠️ {w}" for w in warns]

    def row_of(d: Decision) -> list[object]:
        r = rows[d.sku]
        return [r["kind"], d.sku, r["product_name"][:30], r["machine"], d.qty, d.reason or "機械の数のまま"]

    cols = ["区分", "SKU", "商品名", "機械", "決定", "理由"]
    for title, ds in (("本店から送る", hq), ("7S から送る (7S カートへ手で入れる)", s7)):
        if ds:
            ds = sorted(ds, key=lambda d: (-d.qty, d.sku))
            lines += ["", f"## {title} ({len(ds)} 件)", "", *_table(cols, [row_of(d) for d in ds])]
    dropped = sorted((d for d in decisions if d.qty == 0 and rows[d.sku]["machine"] > 0), key=lambda d: d.sku)
    if dropped:
        lines += ["", f"## 機械は送る判定だが送らない ({len(dropped)} 件)", "",
                  *_table(cols, [row_of(d) for d in dropped])]
    moved = [d for d in send if d.source != rows[d.sku]["source"]]
    if moved:
        lines += ["", f"## 補充元が「7S専用」の印と違う ({len(moved)} 件)", "",
                  "PWA の FBA補充タブの補充元とずれます。印を付け外すかは運用者が決めてください。", ""]
        lines += [f"- {d.sku}({rows[d.sku]['product_name'][:30]}): 印は"
                  f"{'7S' if rows[d.sku]['source'] == '7s' else '本店'} → {'7S' if d.source == '7s' else '本店'} から送る"
                  for d in moved]
    lines += [
        "",
        "## カートへの入れ方",
        "",
        "- 本店分: cart_import_hq.json の中身を、ピッキングリストの「📥 JSON取り込み」に貼る (指定した SKU の数を上書き)",
        "- 7S 分: 上の「7S から送る」の表の数を、7S カートへ入れる",
    ]
    return "\n".join(lines) + "\n"


def build_cart(decisions: list[Decision]) -> dict:
    items = [{"sku": d.sku, "qty": d.qty} for d in decisions if d.source == "hq" and d.qty > 0]
    return {"version": 1, "mode": "replace", "items": items}


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Claude が決めた FBA補充の数から最終の表と取り込み JSON を作る")
    p.add_argument("--dir", type=Path, required=True, help="fba_replenish.py の出力フォルダ")
    p.add_argument("--decisions", type=Path, help="既定: <dir>/decisions.json")
    a = p.parse_args(argv)
    materials = json.loads((a.dir / "materials.json").read_text(encoding="utf-8"))
    dec_path = a.decisions or a.dir / "decisions.json"
    try:
        raw = load_decisions(dec_path)
    except (OSError, ValueError) as e:
        print(f"decisions.json を読めません: {e}", file=sys.stderr)
        return 1
    decisions, errors, warns = validate(materials, raw)
    if errors:
        print("検査で止めました (何も書いていません):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    report = build_report(materials, decisions, warns)
    (a.dir / "decision_report.md").write_text(report, encoding="utf-8", newline="")
    (a.dir / "cart_import_hq.json").write_text(
        json.dumps(build_cart(decisions), ensure_ascii=False, indent=1), encoding="utf-8", newline=""
    )
    print(report)
    print(f"出力: {(a.dir / 'decision_report.md').as_posix()} / {(a.dir / 'cart_import_hq.json').as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
