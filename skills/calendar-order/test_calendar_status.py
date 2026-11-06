import json
import sqlite3
from datetime import date
from pathlib import Path

import calendar_status as cs


def make_db(path: Path, orders: list[dict]) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE ec_orders_archive (raw_json TEXT)")
    conn.executemany("INSERT INTO ec_orders_archive VALUES (?)",
                     [(json.dumps(o, ensure_ascii=False),) for o in orders])
    conn.commit()
    conn.close()


def order(code: str, qty: str, day: str = "2026-11-01", status: str = "処理済") -> dict:
    return {"注文日": day, "受注ステータス": status, "商品コード": code, "商品SKU": code, "個数": qty}


def test_fetch_net_sales_counts_period_and_skips_cancel(tmp_path: Path) -> None:
    db = tmp_path / "ecm.db"
    make_db(db, [
        order("G1648", "2"),
        order("g1648", "3"),
        order("G1648", "5", status="キャンセル"),
        order("G1648", "7", day="2026-08-31"),
        order("G9999", "9"),
    ])
    assert cs.fetch_net_sales("2026-09-01", "2026-11-06", db) == {cs.ITEM_NAMES[0]: 5}


def test_fetch_net_sales_uses_sku_when_code_is_other(tmp_path: Path) -> None:
    db = tmp_path / "ecm.db"
    make_db(db, [{**order("X", "1"), "商品SKU": "G1649"}])
    assert cs.fetch_net_sales("2026-09-01", "2026-11-06", db) == {cs.ITEM_NAMES[3]: 1}


def test_fetch_net_sales_without_db_is_none(tmp_path: Path) -> None:
    assert cs.fetch_net_sales("2026-09-01", "2026-11-06", tmp_path / "none.db") is None


def test_classify_wholesale() -> None:
    assert cs.classify_wholesale("丸カレンダー\\2600   ") == cs.ITEM_NAMES[3]
    assert cs.classify_wholesale("海自ｶﾚﾝﾀﾞｰ(ﾌﾞｯｸﾀｲﾌﾟ)\\1200") == cs.ITEM_NAMES[1]
    assert cs.classify_wholesale("海組立式卓上ｶﾚﾝﾀﾞｰ \\1500") == cs.ITEM_NAMES[2]
    assert cs.classify_wholesale("カレンダー\\2500") is None


def test_same_day_last_year() -> None:
    assert cs.same_day_last_year(date(2026, 11, 6)) == "2025-11-06"
    assert cs.same_day_last_year(date(2028, 2, 29)) == "2027-02-28"
