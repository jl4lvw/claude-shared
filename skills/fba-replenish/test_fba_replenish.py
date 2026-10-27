"""fba_replenish.py のテスト (node で picking-select.js を実際に動かす)。

python -m pytest -q .claude/skills/fba-replenish/test_fba_replenish.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fba_replenish as fr  # noqa: E402

BASE = {
    "is_fba": True, "listing_status": "Active", "sales_30d": 10, "fulfillable_qty": 3,
    "inbound_working_qty": 0, "inbound_shipped_qty": 0, "inbound_receiving_qty": 0,
    "airlogi_qty": 5, "inbound_7s_qty": 0, "required_qty": None, "product_name": "x",
}


def _item(sku: str, **o) -> dict:
    return {**BASE, "sku": sku, "product_name": sku, **o}


def _run(tmp_path: Path, items: list[dict], *extra: str) -> tuple[dict, str]:
    inv = tmp_path / "inv.json"
    inv.write_text(json.dumps({"items": items}), encoding="utf-8")
    out = tmp_path / "out"
    assert fr.main(["--input", str(inv), "--out-dir", str(out), *extra]) == 0
    cart = json.loads((out / "cart_import_hq.json").read_text(encoding="utf-8"))
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
