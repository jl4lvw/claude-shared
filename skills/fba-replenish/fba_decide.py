"""Claude が決めた FBA補充の数を検査し、最終の表と取り込み JSON を作る (読み取りだけ)。

fba_replenish.py の出力フォルダにある materials.json と、Claude が書いた decisions.json を読む。

decisions.json の形:
  {"genres_today": {"パーカー": "冬に向けて売れる時期", "ワッペン": "売れ筋が在庫切れ"},
   "items": [{"sku": "...", "qty": 5, "source": "hq" | "7s", "reason": "直近7日が伸びている"}, ...]}

  genres_today = 今回ピッキングリストに入れるジャンルと、その理由 (Claude が提案する)。
  書かれていないジャンルは「次回に回す」。ジャンルを混ぜると集めるときに混乱するため、
  取り込み JSON は今回のジャンルの分だけにする (2026-10-28 運用者の指示)。

検査 (1 つでも当たれば何も書かずに終了コード 1):
  - materials.json の「一件ずつ見る」行は全件そろっていること (見落とし防止)
  - 知らない SKU・重複・数が 0 未満の整数でない・補充元が hq / 7s 以外
  - 機械の数と違う数にしたのに理由が無い
  - 7S から送る数が 7S在庫を超える
  - genres_today が無い・空・知らないジャンル・理由が無い

出力 (出力フォルダへ):
  decision_report.md       本文に貼る最終の表 (ジャンルごと)
  cart_import_hq.json      今回のジャンルの本店分 (ピッキングリスト取り込み用・mode replace)
  cart_import_hq_all.json  全ジャンルの本店分 (参考)

使い方:
  python fba_decide.py --dir C:/tmp-ai/fba_replenish/<日時> [--decisions <path>]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from materials import GENRE_NAMES, genre_of  # noqa: E402


@dataclass
class Decision:
    sku: str
    qty: int
    source: str
    reason: str


def load_decisions(path: Path) -> tuple[list[dict], object]:
    """(items, genres_today) を返す。genres_today は検査前の生の値。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("decisions.json の items が配列ではありません")
    return items, data.get("genres_today") if isinstance(data, dict) else None


def genre_of_row(r: dict) -> str:
    return r.get("genre") or genre_of(r.get("product_name", ""))


def validate_genres(raw: object) -> tuple[dict[str, str], list[str]]:
    """今回入れるジャンル {名前: 理由} と、エラーを返す。"""
    if not isinstance(raw, dict) or not raw:
        return {}, ["genres_today (今回ピッキングリストに入れるジャンルと理由) を書いてください"]
    errors: list[str] = []
    out: dict[str, str] = {}
    for g, why in raw.items():
        if g not in GENRE_NAMES:
            errors.append(f"知らないジャンルです ({g})。使えるのは: " + "・".join(GENRE_NAMES))
            continue
        why = str(why or "").strip()
        if not why:
            errors.append(f"ジャンル {g} を今回入れる理由がありません")
            continue
        out[g] = why
    return out, errors


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


def build_report(materials: dict, decisions: list[Decision], warns: list[str],
                 today: dict[str, str] | None = None) -> str:
    today = today or {}
    rows = {r["sku"]: r for r in materials["review"]}
    send = [d for d in decisions if d.qty > 0]
    picked = [d for d in send if genre_of_row(rows[d.sku]) in today]
    up = [d for d in decisions if d.qty > rows[d.sku]["machine"]]
    down = [d for d in decisions if d.qty < rows[d.sku]["machine"]]

    def total(ds: list[Decision]) -> str:
        return f"{len(ds)} 件・{sum(d.qty for d in ds)} 個"

    lines = [
        f"# FBA補充 送る商品と数 ({materials['today']}・納品にかかる日数 {materials['lead_days']} 日)",
        "",
        f"- **今回ピッキングリストに入れる: {'・'.join(g for g in GENRE_NAMES if g in today) or 'なし'}"
        f" → {total(picked)}**"
        f" (本店から {total([d for d in picked if d.source == 'hq'])} / 7S から {total([d for d in picked if d.source == '7s'])})",
        f"- 全ジャンルで送る候補: {total(send)} (残りは次回に回す)",
        f"- 機械の数より増やした {len(up)} 件・減らした(送らないを含む) {len(down)} 件",
        f"- 売上が無いので一括で送らない B: {len(materials['skipped_b'])} 件",
    ]
    lines += [f"- ⚠️ {w}" for w in warns]

    # 今回のジャンルを先に、次回に回すジャンルを後に並べる
    order = [g for g in GENRE_NAMES if g in today] + [g for g in GENRE_NAMES if g not in today]
    gsum = []
    for g in order:
        ds = [d for d in send if genre_of_row(rows[d.sku]) == g]
        if not ds and g not in today:
            continue
        zero = sum(rows[d.sku].get("have") == 0 for d in ds)
        gsum.append([g, "今回" if g in today else "次回", len(ds), sum(d.qty for d in ds), zero, today.get(g, "")])
    lines += ["", "## ジャンルごと", "", *_table(["ジャンル", "提案", "件数", "個数", "FBA在庫0", "今回入れる理由"], gsum)]

    def row_of(d: Decision) -> list[object]:
        r = rows[d.sku]
        return [r["kind"], d.sku, r["product_name"][:30], "7S" if d.source == "7s" else "本店",
                r["machine"], d.qty, d.reason or "機械の数のまま"]

    cols = ["区分", "SKU", "商品名", "元", "機械", "決定", "理由"]
    for g in order:
        ds = sorted((d for d in send if genre_of_row(rows[d.sku]) == g), key=lambda d: (-d.qty, d.sku))
        if ds:
            mark = "今回" if g in today else "次回に回す"
            lines += ["", f"## {g} ({mark}・{total(ds)})", "", *_table(cols, [row_of(d) for d in ds])]

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
        "- 本店分: cart_import_hq.json (今回のジャンルだけ) の中身を、ピッキングリストの「📥 JSON取り込み」に貼る"
        " (指定した SKU の数を上書き)",
        "- 7S 分: 今回のジャンルの表で元が「7S」の数を、7S カートへ入れる",
        "- 集めるときはジャンルごとに分けて集める",
    ]
    return "\n".join(lines) + "\n"


def build_cart(decisions: list[Decision], materials: dict | None = None,
               genres: dict[str, str] | None = None) -> dict:
    """本店分の取り込み JSON。genres を渡すとそのジャンルの分だけにする。"""
    rows = {r["sku"]: r for r in materials["review"]} if materials else {}
    items = [{"sku": d.sku, "qty": d.qty} for d in decisions
             if d.source == "hq" and d.qty > 0 and (genres is None or genre_of_row(rows[d.sku]) in genres)]
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
        raw, raw_genres = load_decisions(dec_path)
    except (OSError, ValueError) as e:
        print(f"decisions.json を読めません: {e}", file=sys.stderr)
        return 1
    decisions, errors, warns = validate(materials, raw)
    today, g_errors = validate_genres(raw_genres)
    errors += g_errors
    if errors:
        print("検査で止めました (何も書いていません):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    rows = {r["sku"]: r for r in materials["review"]}
    for g in today:
        if not any(d.qty > 0 and genre_of_row(rows[d.sku]) == g for d in decisions):
            warns.append(f"今回入れるジャンル {g} に送る商品がありません")
    report = build_report(materials, decisions, warns, today)
    (a.dir / "decision_report.md").write_text(report, encoding="utf-8", newline="")
    for name, genres in (("cart_import_hq.json", today), ("cart_import_hq_all.json", None)):
        (a.dir / name).write_text(
            json.dumps(build_cart(decisions, materials, genres), ensure_ascii=False, indent=1),
            encoding="utf-8", newline="",
        )
    print(report)
    print(f"出力: {(a.dir / 'decision_report.md').as_posix()} / {(a.dir / 'cart_import_hq.json').as_posix()}"
          " (今回のジャンルだけ) / cart_import_hq_all.json (全ジャンル)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
