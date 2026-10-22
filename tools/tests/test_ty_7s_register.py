import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ty_7s_register as t  # noqa: E402


def row(code="G2218-NV-S", sku="FJ-G2218-TS-NAGARA-NV-S"):
    return {"item_code": code, "product_name": "x", "spec1": "a", "spec2": "S", "amazon_fba_sku": sku}


def test_valid_plan():
    assert t.validate_plan([row(), row("G2218-NV-M", "FJ-M")]) == []


def test_bad_inputs():
    assert t.validate_plan([]) != []
    assert t.validate_plan([row("g2218-nv-s")]) != []
    assert t.validate_plan([row(), row()]) != []  # 重複
    assert t.validate_plan([{"item_code": "G2218-NV-S"}]) != []
    assert t.validate_plan([row(f"G{i:04d}-NV-S", f"S{i}") for i in range(51)]) != []


def test_match_checks():
    ok = [{"sku": "a", "fnsku": "X1", "registered": False}]
    assert t.check_match(ok) == []
    assert t.check_match([{"sku": "a", "fnsku": "", "registered": False}]) != []
    assert t.check_match([{"sku": "a", "fnsku": "X1", "registered": True, "item_code": "G1"}]) != []
    dup = ok + [{"sku": "b", "fnsku": "X1", "registered": False}]
    assert t.check_match(dup) != []


def test_match_unknown_status_blocks_registration():
    """2026-10-22: 通信失敗(unknown)を「7S 未登録」と取り違えて登録に進まない。"""
    unknown = [{"sku": "a", "fnsku": "X1", "registered": False, "status": "unknown"}]
    assert t.check_match(unknown) != []
    absent = [{"sku": "a", "fnsku": "X1", "registered": False, "status": "absent"}]
    assert t.check_match(absent) == []
