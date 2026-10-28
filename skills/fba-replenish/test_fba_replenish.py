"""fba_replenish.py / materials.py / fba_decide.py のテスト (node で picking-select.js を実際に動かす)。

python -m pytest -q .claude/skills/fba-replenish/test_fba_replenish.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fba_decide as fd  # noqa: E402
import fba_replenish as fr  # noqa: E402
import materials as mt  # noqa: E402

BASE = {
    "is_fba": True, "listing_status": "Active", "sales_30d": 10, "fulfillable_qty": 3,
    "inbound_working_qty": 0, "inbound_shipped_qty": 0, "inbound_receiving_qty": 0,
    "airlogi_qty": 5, "inbound_7s_qty": 0, "required_qty": None, "product_name": "x",
}


def _item(sku: str, **o) -> dict:
    return {**BASE, "sku": sku, "product_name": sku, **o}


def _make_db(path: Path, rows: list[tuple[str, date, int]]) -> Path:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE order_items_raw (amazon_order_id TEXT, sku TEXT, purchase_date DATE,"
                " order_status TEXT, item_status TEXT, quantity INTEGER)")
    con.executemany("INSERT INTO order_items_raw VALUES (?,?,?,?,?,?)",
                    [(f"o{i}", s, d.isoformat(), "Shipped", "Shipped", q) for i, (s, d, q) in enumerate(rows)])
    con.commit()
    con.close()
    return path


def _run(tmp_path: Path, items: list[dict], *extra: str, db_rows=None) -> tuple[dict, str]:
    inv = tmp_path / "inv.json"
    inv.write_text(json.dumps({"items": items}), encoding="utf-8")
    out = tmp_path / "out"
    db = _make_db(tmp_path / f"db{len(list(tmp_path.glob('db*')))}.db", db_rows or [])
    assert fr.main(["--input", str(inv), "--out-dir", str(out), "--db", str(db), *extra]) == 0
    cart = json.loads((out / "cart_import_hq_machine.json").read_text(encoding="utf-8"))
    return cart, (out / "report.md").read_text(encoding="utf-8")


def test_end_to_end_a_b_and_7s(tmp_path):
    items = [
        _item("A-HQ"),                                                    # A 本店 7個
        _item("B-HQ", sales_30d=3, required_qty=8),                       # B 本店 5個
        _item("A-7S", source_7s_only=True, airlogi_qty=4),               # A 7S 3個 (1個残す)
        _item("X-7S", source_7s_only=True, airlogi_qty=1),               # 送れない
        _item("HQ0", hq_warehouse_qty=0, airlogi_qty=9),                  # 7S専用の候補
    ]
    cart, report = _run(tmp_path, items)
    assert cart == {"version": 1, "mode": "replace", "items": [{"sku": "A-HQ", "qty": 7}, {"sku": "HQ0", "qty": 7}]}
    assert "A 必ず補充: 3 件" in report
    assert "B 要確認: 1 件" in report
    assert "送れない (7S専用で 7S在庫が足りない): 1 件" in report
    assert "7S専用の印を付ける候補 (1 件)" in report
    assert "v61 未反映" not in report

    cart_b, _ = _run(tmp_path, items, "--include-b")
    assert {"sku": "B-HQ", "qty": 5} in cart_b["items"]


def test_warns_when_server_has_no_7s_only_field(tmp_path):
    _, report = _run(tmp_path, [_item("A")])
    assert "v61 未反映" in report


def test_table_escapes_pipe():
    row = {"sku": "S|1", "product_name": "a|b", "source": "hq", "fba": 0, "inbound": 0, "sales": 1,
           "reqFba": None, "send": 1, "extra": 0, "overset": False, "short7s": 0}
    line = fr.table([row], 5)[2]
    assert line.count("|") == 9
    # 補充元の列を出さない表は列が 1 つ少ない (見出しと行がそろう)
    no_src = fr.table([row], 5, with_source=False)
    assert [x.count("|") for x in no_src] == [8, 8, 8]


def test_extra_shows_sendable_for_7s():
    row = {"sku": "S", "product_name": "x", "source": "7s", "fba": 0, "inbound": 0, "sales": 10,
           "reqFba": 20, "send": 7, "extra": 10, "extraSend": 2, "overset": False, "short7s": 0}
    assert "設定数まで+10(7Sから送れるのは+2)" in fr.table([row], 5)[2]


# ---- 判断の材料 (materials) ----

def test_windows_and_summary():
    w = mt.Windows(date(2026, 10, 28))
    assert w.d7 == (date(2026, 10, 22), date(2026, 10, 28))
    assert w.d30[0] == date(2026, 9, 29)
    assert w.ly_next30 == (date(2025, 10, 29), date(2025, 11, 27))
    daily = {date(2026, 10, 28): 2, date(2026, 10, 1): 3, date(2025, 11, 1): 5, date(2025, 10, 20): 1}
    assert mt.sales_summary(daily, w) == {"s7": 2, "s30": 5, "s90": 5, "ly_same30": 1, "ly_next30": 5}
    # 履歴が始まる前に掛かる期間は 0 ではなく None
    assert mt.sales_summary(daily, w, date(2026, 4, 1)) == {
        "s7": 2, "s30": 5, "s90": 5, "ly_same30": None, "ly_next30": None}
    assert mt.sales_summary(daily, w, date(2026, 9, 1))["s90"] is None


def test_history_start_skips_sparse_months(tmp_path):
    rows = [("x", date(2024, 11, 5), 1), ("x", date(2025, 10, 5), 1)]
    rows += [("x", date(2026, m, 1) + timedelta(days=i % 28), 1) for m in (4, 5, 6) for i in range(50)]
    assert mt.history_start(_make_db(tmp_path / "h.db", rows)) == date(2026, 4, 1)
    assert mt.history_start(_make_db(tmp_path / "e.db", [])) is None


def test_guide_and_cover():
    assert mt.guide_qty(30, 10, 7) == 27        # 1日1個 × 37日 − 10
    assert mt.guide_qty(30, 50, 7) == 0
    assert mt.cover_days(30, 10) == 10.0
    assert mt.cover_days(0, 10) is None


def test_materials_near_and_skipped_b(tmp_path):
    today = mt.today_jst()
    items = [
        _item("A-HQ"),                                                    # A
        _item("NEAR", sales_30d=30, fulfillable_qty=32),                  # 30日分はあるが 37日分に届かない → 準A
        _item("OK", sales_30d=30, fulfillable_qty=40),                    # 足りている → 出ない
        _item("B-ZERO", sales_30d=0, fulfillable_qty=0, required_qty=5),  # 売上0の B → 一括で送らない
        _item("B-SEASON", sales_30d=0, fulfillable_qty=0, required_qty=5),  # 昨年この先に売れた → 一件ずつ見る
    ]
    # 履歴の始まりを 400 日前にする (昨年の期間が「データなし」にならないように)
    rows = [("OLD", today - timedelta(days=400), 1), ("B-SEASON", today - timedelta(days=360), 4), ("A-HQ", today, 3)]
    _run(tmp_path, items, db_rows=rows)
    m = json.loads((tmp_path / "out" / "materials.json").read_text(encoding="utf-8"))
    assert {r["sku"]: r["kind"] for r in m["review"]} == {"A-HQ": "A", "NEAR": "準A", "B-SEASON": "B"}
    assert [r["sku"] for r in m["skipped_b"]] == ["B-ZERO"]
    a = next(r for r in m["review"] if r["sku"] == "A-HQ")
    assert a["s7"] == 3 and a["machine"] == 7 and a["guide"] == 10   # ceil(10/30*37)=13 − 3
    near = next(r for r in m["review"] if r["sku"] == "NEAR")
    assert near["machine"] == 0 and near["guide"] == 5
    assert m["lead_days"] == 7
    md = (tmp_path / "out" / "materials.md").read_text(encoding="utf-8")
    assert "準A" in md
    assert all(r["genre"] in mt.GENRE_NAMES for r in m["review"] + m["skipped_b"])
    assert m["genres"] == list(mt.GENRE_NAMES)
    assert "## ジャンルごとの候補" in md


def test_materials_guide_capped_by_7s_stock(tmp_path):
    _run(tmp_path, [_item("A-7S", source_7s_only=True, airlogi_qty=4)])
    m = json.loads((tmp_path / "out" / "materials.json").read_text(encoding="utf-8"))
    assert m["review"][0]["guide"] == 3


def test_lead_days_changes_near(tmp_path):
    items = [_item("NEAR", sales_30d=30, fulfillable_qty=32)]
    _run(tmp_path, items, "--lead-days", "0")
    m = json.loads((tmp_path / "out" / "materials.json").read_text(encoding="utf-8"))
    assert m["review"] == [] and m["lead_days"] == 0


def test_db_opened_read_only(tmp_path):
    db = _make_db(tmp_path / "ro.db", [])
    assert mt.load_daily_sales(db, date(2026, 1, 1)) == {}
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        con.execute("INSERT INTO order_items_raw VALUES ('x','y','2026-01-01','Shipped','',1)")
        raise AssertionError("書けてしまった")
    except sqlite3.OperationalError:
        pass
    finally:
        con.close()


# ---- Claude の決定の検査 (fba_decide) ----

def _mat() -> dict:
    return {"today": "2026-10-28", "lead_days": 7, "keep_7s": 1, "skipped_b": [{"sku": "Z"}],
            "review": [
                {"kind": "A", "genre": "ワッペン", "sku": "H", "product_name": "ワッペン 本店の品",
                 "source": "hq", "machine": 5, "airlogi": 3, "have": 0},
                {"kind": "A", "genre": "パーカー", "sku": "S", "product_name": "パーカー 7Sの品",
                 "source": "7s", "machine": 2, "airlogi": 3, "have": 1},
                {"kind": "B", "genre": "Tシャツ・ポロ", "sku": "B", "product_name": "Tシャツ Bの品",
                 "source": "hq", "machine": 4, "airlogi": None, "have": 2},
                {"kind": "A", "genre": "Tシャツ・ポロ", "sku": "T", "product_name": "Tシャツ 次回の品",
                 "source": "hq", "machine": 3, "airlogi": None, "have": 0},
            ]}


def _write_ok(tmp_path, genres_today=None):
    (tmp_path / "materials.json").write_text(json.dumps(_mat()), encoding="utf-8")
    (tmp_path / "decisions.json").write_text(json.dumps({
        "genres_today": genres_today if genres_today is not None
        else {"ワッペン": "売れ筋が在庫切れ", "パーカー": "冬に向けて売れる"},
        "items": [
            {"sku": "H", "qty": 7, "source": "hq", "reason": "直近7日が伸びている"},
            {"sku": "S", "qty": 2, "source": "7s"},
            {"sku": "B", "qty": 0, "reason": "設定数が多すぎる疑い"},
            {"sku": "T", "qty": 3},
        ]}), encoding="utf-8")


def test_decide_ok_and_outputs(tmp_path):
    _write_ok(tmp_path)
    assert fd.main(["--dir", str(tmp_path)]) == 0
    cart = json.loads((tmp_path / "cart_import_hq.json").read_text(encoding="utf-8"))
    assert cart == {"version": 1, "mode": "replace", "items": [{"sku": "H", "qty": 7}]}   # T は次回のジャンル
    cart_all = json.loads((tmp_path / "cart_import_hq_all.json").read_text(encoding="utf-8"))
    assert [i["sku"] for i in cart_all["items"]] == ["H", "T"]
    rep = (tmp_path / "decision_report.md").read_text(encoding="utf-8")
    assert "今回ピッキングリストに入れる: パーカー・ワッペン → 2 件・9 個" in rep
    assert "7S から 1 件・2 個" in rep
    assert "全ジャンルで送る候補: 3 件・12 個" in rep
    assert "## ワッペン (今回・1 件・7 個)" in rep
    assert "## Tシャツ・ポロ (次回に回す・1 件・3 個)" in rep
    assert rep.index("## パーカー (") < rep.index("## ワッペン (") < rep.index("## Tシャツ・ポロ (次回")
    assert "機械は送る判定だが送らない (1 件)" in rep
    assert "一括で送らない B: 1 件" in rep


def test_decide_requires_genres_today(tmp_path):
    for bad in ({}, {"ぬいぐるみ": "x"}, {"ワッペン": ""}):
        _write_ok(tmp_path, bad)
        assert fd.main(["--dir", str(tmp_path)]) == 1
    _, errors = fd.validate_genres({"ぬいぐるみ": "x", "ワッペン": " "})
    assert any("知らないジャンル" in e for e in errors)
    assert any("理由がありません" in e for e in errors)
    assert fd.validate_genres(None)[1]


def test_genre_of():
    assert mt.genre_of("パーカー さざなみ Type2 炎") == "パーカー"
    assert mt.genre_of("Tシャツ みやじま(DRY,.,L)") == "Tシャツ・ポロ"
    assert mt.genre_of("ポロシャツ (デジタル迷彩)") == "Tシャツ・ポロ"
    assert mt.genre_of("刺繍ワッペン 【自衛艦旗 青】") == "ワッペン"
    assert mt.genre_of("金蒔絵 (菊花紋) 3枚セット") == "ステッカー・金蒔絵"
    assert mt.genre_of("スポーツタオル (護衛艦 とね)") == "手ぬぐい・タオル"
    assert mt.genre_of("自衛隊グッズ 部隊識別帽 帽子 キャップ") == "帽子"
    assert mt.genre_of("幹部用 短靴 本皮 ( 黒 26.5 cm)") == "靴"
    assert mt.genre_of("スプーン かが 先割れ") == "小物"
    # 古い materials.json (genre 列なし) は商品名から振り分ける
    assert fd.genre_of_row({"product_name": "手ぬぐい (海軍)"}) == "手ぬぐい・タオル"


def test_decide_errors():
    _, errors, _ = fd.validate(_mat(), [
        {"sku": "H", "qty": 6},                                  # 理由なし
        {"sku": "S", "qty": 4, "source": "7s", "reason": "x"},   # 7S在庫超え
        {"sku": "NOPE", "qty": 1},                               # 知らない SKU
        {"sku": "H", "qty": 5},                                  # 重複
    ])
    text = " / ".join(errors)
    assert "理由がありません" in text
    assert "7S在庫 3 個より多い" in text
    assert "材料に無い SKU" in text
    assert "2 回書かれています" in text
    assert "決めていない商品が 2 件" in text                       # B・T が無い


def test_decide_rejects_bad_qty_and_source():
    _, errors, _ = fd.validate(_mat(), [
        {"sku": "H", "qty": -1}, {"sku": "S", "qty": True},
        {"sku": "B", "qty": 1, "source": "fba", "reason": "x"},
    ])
    assert sum("0 以上の整数" in e for e in errors) == 2
    assert any("補充元は hq か 7s" in e for e in errors)


def test_decide_warns_when_7s_emptied_and_source_moved():
    decisions, errors, warns = fd.validate(_mat(), [
        {"sku": "H", "qty": 3, "source": "7s", "reason": "本店に無い"},
        {"sku": "S", "qty": 2}, {"sku": "B", "qty": 4}, {"sku": "T", "qty": 3},
    ])
    assert not errors
    assert any("FBM 用が 0 個" in w for w in warns)
    assert "補充元が「7S専用」の印と違う (1 件)" in fd.build_report(_mat(), decisions, warns, {"ワッペン": "x"})


def test_decide_stops_without_writing(tmp_path):
    (tmp_path / "materials.json").write_text(json.dumps(_mat()), encoding="utf-8")
    (tmp_path / "decisions.json").write_text(json.dumps({"items": []}), encoding="utf-8")
    assert fd.main(["--dir", str(tmp_path)]) == 1
    assert not (tmp_path / "cart_import_hq.json").exists()
