"""bo_api.py のテスト（実APIには接続しない）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import bo_api  # noqa: E402

PLANS = [
    {"id": 4, "product_code": "G1440", "sku": "G1440-BK-L", "size": "L", "qty": 13, "status": "pending", "version": 1},
    {"id": 5, "product_code": "G1440", "sku": "G1440-BK-LL", "size": "LL", "qty": 10, "status": "pending", "version": 1},
    {"id": 6, "product_code": "G1440", "sku": "G1440-BK-5L", "size": "5L", "qty": 2, "status": "pending", "version": 1},
    {"id": 1, "product_code": "G1691", "sku": "G1691-NV-M", "size": "M", "qty": 5, "status": "pending", "version": 1},
]
PRODUCT = {
    "product_name": "Tシャツ はたかぜ",
    "cells": [
        {"sku": "G1440-BK-L", "size": "L", "s7_qty": 1, "fba_fulfillable": 0, "fba_inbound": 2},
        {"sku": "G1440-BK-LL", "size": "LL", "s7_qty": 2, "fba_fulfillable": 5, "fba_inbound": 0},
        {"sku": "G1440-BK-S", "size": "S", "s7_qty": 27, "fba_fulfillable": 2, "fba_inbound": None},
    ],
}


def test_group_by_product() -> None:
    g = bo_api.group_by_product(PLANS)
    assert sorted(g) == ["G1440", "G1691"]
    assert len(g["G1440"]) == 3


def test_build_stock_rows_sums_and_missing_cell() -> None:
    rows = bo_api.build_stock_rows(PRODUCT, [p for p in PLANS if p["product_code"] == "G1440"])
    by_size = {r["size"]: r for r in rows}
    assert by_size["L"]["stock_total"] == 3 and by_size["L"]["plan"] == 13
    assert by_size["S"]["stock_total"] == 29 and by_size["S"]["plan"] is None  # inbound=None は 0 扱い
    assert any("在庫セルなし" in r["size"] and r["plan"] == 2 for r in rows)    # 5L は在庫セルが無くても落とさない


def test_format_table_flags_non_multiple_of_25() -> None:
    rows = bo_api.build_stock_rows(PRODUCT, PLANS[:3])
    text = bo_api.format_table("G1440", "Tシャツ はたかぜ", ["BK"], rows, None)
    assert "予定数の合計: 25" in text and "25の倍数ではありません" not in text
    rows2 = bo_api.build_stock_rows(PRODUCT, PLANS[:2])
    assert "25の倍数ではありません" in bo_api.format_table("G1440", "x", [], rows2, None)


def test_claim_payload_uses_current_qty_and_validates() -> None:
    p = bo_api.build_claim_payload(PLANS, [4, 5], 3600)
    assert p == {"items": [{"id": 4, "expected_qty": 13}, {"id": 5, "expected_qty": 10}], "ttl_sec": 3600}
    with pytest.raises(bo_api.BoxQRError):
        bo_api.build_claim_payload(PLANS, [999], None)
    with pytest.raises(bo_api.BoxQRError):
        bo_api.build_claim_payload(PLANS, [4], 10)


def test_ordered_and_ids_payloads() -> None:
    assert bo_api.build_ordered_payload(4, "No.1", None, "") == {"items": [{"id": 4, "order_ref": "No.1"}]}
    assert bo_api.build_ordered_payload(4, "No.1", 12, "メモ")["items"][0]["ordered_qty"] == 12
    with pytest.raises(bo_api.BoxQRError):
        bo_api.build_ids_payload([4], "", require_note=True)
    assert bo_api.build_ids_payload([4], "理由", True) == {"ids": [4], "note": "理由"}


def test_check_result_raises_on_skipped(capsys: pytest.CaptureFixture[str]) -> None:
    ok = {"changed": [{"id": 4, "sku": "S", "qty": 1, "status": "ordering"}], "skipped": []}
    bo_api.check_result(ok)
    with pytest.raises(bo_api.BoxQRError):
        bo_api.check_result({"changed": [], "skipped": [{"id": 9, "reason": "qty_changed", "current_qty": 3}]})
    assert "skipped" in capsys.readouterr().out


def test_write_is_dry_run_without_yes(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def boom(*a, **k):  # 送信されたら失敗
        raise AssertionError("dry-run なのに送信された")

    monkeypatch.setattr(bo_api, "_request", boom)
    bo_api.send_write("claim", {"items": [{"id": 4}]}, yes=False)
    assert "dry-run" in capsys.readouterr().out


def test_write_with_yes_sends_key_not_printed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    keyfile = tmp_path / "k.txt"
    keyfile.write_text("SECRET-VALUE\n", encoding="utf-8")
    monkeypatch.setenv("BOXQR_KEY_FILE", str(keyfile))
    seen: dict = {}

    def fake(method, path, body=None, key=None):
        seen.update(method=method, path=path, key=key)
        return {"changed": [{"id": 4, "sku": "S", "qty": 1, "status": "ordering"}], "skipped": []}

    monkeypatch.setattr(bo_api, "_request", fake)
    bo_api.send_write("claim", {"items": [{"id": 4}]}, yes=True)
    assert seen["method"] == "POST" and seen["path"].endswith("/claim") and seen["key"] == "SECRET-VALUE"
    assert "SECRET-VALUE" not in capsys.readouterr().out


def test_load_key_missing_and_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BOXQR_KEY_FILE", str(tmp_path / "none.txt"))
    with pytest.raises(bo_api.BoxQRError):
        bo_api.load_key()
    empty = tmp_path / "e.txt"
    empty.write_text("\n", encoding="utf-8")
    monkeypatch.setenv("BOXQR_KEY_FILE", str(empty))
    with pytest.raises(bo_api.BoxQRError):
        bo_api.load_key()


def test_verify_key(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []

    def fake_ok(method, path, body=None, key=None):
        calls.append((method, path, body))
        return {"changed": [], "skipped": [{"id": 999999, "reason": "not_found"}]}

    monkeypatch.setattr(bo_api, "_request", fake_ok)
    assert bo_api.verify_key("k") is True
    assert calls == [("POST", "/api/v1/order-plans/release", {"ids": [999999]})]   # 存在しないIDだけ → 何も変更しない

    def fake_401(method, path, body=None, key=None):
        raise bo_api.BoxQRError("HTTP 401 キーが違う")

    monkeypatch.setattr(bo_api, "_request", fake_401)
    assert bo_api.verify_key("bad") is False
