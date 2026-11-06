"""カレンダー発注の「実績表」: 昨シーズンの仕入(=販売。売り切ったため)と、今シーズンの発注・入荷・売れた数を並べる。

コンパスの仕入・売上(conphas-search)と 042 のネット注文の記録を読み取り専用で集計し、発注台帳(orders.json)と突き合わせる。
使い方:
    python calendar_status.py                  # 実績表を表示(カレンダーの発注前に毎回見る)
    python calendar_status.py add-order --supplier 三将 --item "将カレンダー A2 海上自衛隊 縦型" --qty 30 [--date 2026-10-16] [--note ...]
    python calendar_status.py items            # 品目名の一覧
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import unicodedata
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
ORDERS_PATH = HERE / "orders.json"
CONPHAS_SEARCH = HERE.parent / "conphas-search" / "conphas_search.py"
ECM_DB = HERE.parents[2] / "042.EC統合管理" / "data" / "ecm.db"

# (表示名, 仕入先, 判定キーワード(NFKC正規化後の品名にすべて含まれる), 1ロットの部数(不明は0), ネットの商品コード)
# 番号は並び順(1始まり)。寺下さんは「番号+個数/ロット数」で指示する(2026-10-22)
ITEMS: list[tuple[str, str, tuple[str, ...], int, str]] = [
    ("将カレンダー A2 海上自衛隊 縦型", "三将", ("将カレンダー", "A2", "海上自衛隊"), 30, "G1648"),
    ("BOOKカレンダー将 A4 海上自衛隊", "三将", ("BOOKカレンダー", "A4", "海上自衛隊"), 50, "G1651"),
    ("卓上カレンダー B6 海上自衛隊", "三将", ("卓上カレンダー", "海上自衛隊"), 50, "G1650"),
    ("海上自衛隊 丸 カレンダー A2横型", "三将", ("丸カレンダー", "海上自衛隊"), 25, "G1649"),
    ("海自カレンダー カード型(卓上)", "海人社", ("海自カレンダー", "カード型"), 10, "G1682"),
    ("海自カレンダー 大判横型(A2)", "海人社", ("海自カレンダー", "大判横"), 10, "G1683"),
    ("三笠カレンダー", "三笠保存会", ("三笠カレンダー",), 0, "G1674"),
]
ITEM_NAMES = [i[0] for i in ITEMS]
# コンパスの売上の品名は仕入と書き方が違うので、はっきり品目が分かる語だけで振り分ける
WHOLESALE_KEYS: list[tuple[str, str]] = [
    ("丸カレンダー", ITEM_NAMES[3]),
    ("ブックタイプ", ITEM_NAMES[1]),
    ("組立式卓上", ITEM_NAMES[2]),
    ("世界の艦船卓上", ITEM_NAMES[4]),
]


def classify(sna: str) -> str | None:
    """コンパスの品名(半角カナ混じり)を表示名へ。該当なしは None。"""
    text = unicodedata.normalize("NFKC", sna).replace(" ", "").replace("　", "")
    for name, _supplier, keys, _lot, _code in ITEMS:
        if all(k.replace(" ", "") in text for k in keys):
            return name
    return None


def classify_wholesale(sna: str) -> str | None:
    """コンパスの売上の品名を表示名へ。品目が言い切れないものは None。"""
    text = unicodedata.normalize("NFKC", sna).replace(" ", "").replace("　", "")
    for key, name in WHOLESALE_KEYS:
        if key in text:
            return name
    return None


def fetch_conphas(mode: str, start: str, end: str) -> list[dict]:
    """期間内のカレンダーの明細をコンパスから取る(読み取りのみ)。"""
    cmd = [sys.executable, str(CONPHAS_SEARCH), "--mode", mode, "--from", start, "--to", end,
           "--kw", "カレンダー", "--json"]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=900,
                          env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
    if proc.returncode != 0:
        raise SystemExit(f"コンパス検索に失敗しました: {proc.stderr[-300:]}")
    lines = (line.strip() for line in proc.stdout.splitlines())
    return [json.loads(line) for line in lines if line.startswith("{")]


def fetch_purchases(start: str, end: str) -> dict[str, int]:
    """期間内のカレンダー仕入数を表示名ごとに集計する。"""
    totals: dict[str, int] = defaultdict(int)
    for row in fetch_conphas("purchase", start, end):
        name = classify(row.get("SNA", ""))
        if name:
            totals[name] += int(float(row.get("SRR", 0)))
    return dict(totals)


def fetch_wholesale(start: str, end: str) -> tuple[dict[str, int], dict[str, int]]:
    """期間内の卸の販売数を集計する。戻り値は(品目ごとの数, 振り分けできなかった品名ごとの数)。"""
    totals: dict[str, int] = defaultdict(int)
    unknown: dict[str, int] = defaultdict(int)
    for row in fetch_conphas("sales", start, end):
        sna = row.get("SNA", "").strip()
        name = classify_wholesale(sna)
        qty = int(float(row.get("SRR", 0)))
        if name:
            totals[name] += qty
        else:
            unknown[sna] += qty
    return dict(totals), dict(unknown)


def fetch_net_sales(start: str, end: str, db_path: Path = ECM_DB) -> dict[str, int] | None:
    """期間内のネット注文の個数を集計する(キャンセルを除く)。記録が読めないときは None。"""
    if not db_path.exists():
        return None
    code_to_name = {item[4]: item[0] for item in ITEMS}
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        rows = conn.execute("SELECT raw_json FROM ec_orders_archive "
                            "WHERE substr(json_extract(raw_json, '$.注文日'), 1, 10) BETWEEN ? AND ?",
                            (start, end)).fetchall()
    finally:
        conn.close()
    totals: dict[str, int] = defaultdict(int)
    for (raw,) in rows:
        order = json.loads(raw)
        if order.get("受注ステータス") == "キャンセル":
            continue
        name = code_to_name.get(str(order.get("商品コード") or "").upper())
        name = name or code_to_name.get(str(order.get("商品SKU") or "").upper())
        if name:
            totals[name] += int(order.get("個数") or 0)
    return dict(totals)


def load_orders() -> list[dict]:
    if not ORDERS_PATH.exists():
        return []
    return json.loads(ORDERS_PATH.read_text(encoding="utf-8"))


def season_range(today: date) -> tuple[str, str, str, str]:
    """シーズンは9/1始まり。今年=今日を含むシーズン、昨年=その1つ前。"""
    y = today.year if today.month >= 9 else today.year - 1
    return (f"{y - 1}-09-01", f"{y}-08-31", f"{y}-09-01", today.isoformat())


def same_day_last_year(today: date) -> str:
    """去年の同じ日。2/29 は 2/28 にする。"""
    day = 28 if (today.month, today.day) == (2, 29) else today.day
    return date(today.year - 1, today.month, day).isoformat()


def show_status() -> None:
    today = date.today()
    last_from, last_to, cur_from, cur_to = season_range(today)
    last = fetch_purchases(last_from, last_to)
    cur = fetch_purchases(cur_from, cur_to)
    net = fetch_net_sales(cur_from, cur_to)
    net_last = fetch_net_sales(last_from, same_day_last_year(today))
    wholesale, wholesale_unknown = fetch_wholesale(cur_from, cur_to)
    orders = [o for o in load_orders() if o["date"] >= cur_from]
    ordered: dict[str, int] = defaultdict(int)
    for o in orders:
        ordered[o["item"]] += int(o["qty"])
    print(f"【カレンダー実績表】 昨シーズン {last_from}〜{last_to} / 今シーズン {cur_from}〜{cur_to}")
    print("昨年の販売数 = 昨年の仕入数(売り切ったため。2026-10-22 寺下さん確認)")
    print(f"{'番号':<4}{'品目':<30}{'仕入先':<8}{'1ロット':>8}{'昨年(販売)':>10}{'今年の発注累計':>14}{'今年の入荷':>10}{'今年売れた数':>12}{'昨年同日(ネット)':>16}{'昨年との差':>10}")
    sums = [0, 0, 0, 0, 0]
    for no, (name, supplier, _keys, lot, _code) in enumerate(ITEMS, 1):
        a, b, c = last.get(name, 0), ordered.get(name, 0), cur.get(name, 0)
        d = (net or {}).get(name, 0) + wholesale.get(name, 0)
        e = (net_last or {}).get(name, 0)
        if not (a or b or c or d or e):
            continue
        for i, value in enumerate((a, b, c, d, e)):
            sums[i] += value
        print(f"{no:<4}{name:<30}{supplier:<8}{(str(lot) + '部') if lot else '不明':>8}{a:>10}{b:>14}{c:>10}{d:>12}{e:>16}{b - a:>10}")
    print(f"{'合計':<50}{sums[0]:>10}{sums[1]:>14}{sums[2]:>10}{sums[3]:>12}{sums[4]:>16}{sums[1] - sums[0]:>10}")
    print("※「昨年との差」=今年の発注累計−昨年の販売数(マイナスは昨年より少ない)。入荷はコンパスの仕入計上で、実際の到着日とずれることがある")
    print("※「今年売れた数」=ネット注文(キャンセル除く)+コンパスの卸。「昨年同日(ネット)」はネット注文だけ。店頭の販売はどちらにも入らない")
    if net is None or net_last is None:
        print(f"※ネット注文の記録が読めないため、ネットの販売数は0で出ています: {ECM_DB}")
    if wholesale_unknown:
        print("※品目に振り分けできなかった卸(今年売れた数に入っていない): "
              + " / ".join(f"{k} {v}部" for k, v in wholesale_unknown.items()))
    print(f"※発注台帳: {ORDERS_PATH}(発注したら add-order で追記する)")
    if orders:
        print("今年の発注の履歴:")
        for o in sorted(orders, key=lambda x: x["date"]):
            print(f"  {o['date']} {o['supplier']} {o['item']} {o['qty']}部 {o.get('note', '')}")


def add_order(args: argparse.Namespace) -> None:
    if args.item.isdigit() and 1 <= int(args.item) <= len(ITEMS):
        args.item = ITEMS[int(args.item) - 1][0]
    if args.item not in ITEM_NAMES:
        raise SystemExit("品目名が一覧にありません。`items` で確認してください: " + " / ".join(ITEM_NAMES))
    if args.lots is not None:
        lot = ITEMS[ITEM_NAMES.index(args.item)][3]
        if not lot:
            raise SystemExit("この品目は1ロットの部数が未設定です。--qty で部数を指定してください")
        args.qty = args.lots * lot
    orders = load_orders()
    orders.append({"date": args.date, "supplier": args.supplier, "item": args.item, "qty": args.qty,
                   "note": args.note})
    ORDERS_PATH.write_text(json.dumps(orders, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
                           newline="")
    print(f"追記しました: {args.date} {args.supplier} {args.item} {args.qty}部")


def main() -> None:
    ap = argparse.ArgumentParser(description="カレンダー発注の実績表")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("add-order", help="発注台帳へ追記")
    p.add_argument("--supplier", required=True)
    p.add_argument("--item", required=True, help="表示名、または番号(実績表の左の数字)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--qty", type=int, help="部数")
    g.add_argument("--lots", type=int, help="ロット数(1ロット=実績表の入り数)")
    p.add_argument("--date", default=date.today().isoformat())
    p.add_argument("--note", default="")
    sub.add_parser("items", help="品目名の一覧")
    args = ap.parse_args()
    if args.cmd == "add-order":
        add_order(args)
    elif args.cmd == "items":
        print("\n".join(f"{n}. {name}" for n, name in enumerate(ITEM_NAMES, 1)))
    else:
        show_status()


if __name__ == "__main__":
    main()
