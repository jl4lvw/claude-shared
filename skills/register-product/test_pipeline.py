"""pipeline.py の純粋な判定ロジックのテスト(外部システムには触れない)."""
from __future__ import annotations

import copy
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline as pl  # noqa: E402


def _single() -> dict:
    return {
        "g": "G2225", "manage_number": "g2225", "sku_main": "G2225", "is_variation": False,
        "title": "自衛隊 ワッペン 護衛艦ちょうかい派米記念",
        "variants": [{"variant_id": "g2225", "selectors": {}, "price": 1694, "estore_price": 1540, "stock": 20}],
        "estore_price_main": 1540, "lead_time_days": 5,
        "estore": {"register": True, "publish": True},
        "yahoo": {"register": True, "copy_code": "g2223", "publish": True, "reserve_publish": True,
                  "pending_before": {"rows": 0, "item_pages": 0}, "pending_allow": 4},
        "goq": {"resync": True},
    }


def _variation() -> dict:
    p = _single()
    p.update(g="G2300", manage_number="g2300", sku_main="G2300-parent", is_variation=True)
    p["variants"] = [
        {"variant_id": "v1", "selectors": {"色": "ネイビー", "サイズ": "M"}, "price": 3520, "estore_price": 3200, "stock": 5},
        {"variant_id": "v2", "selectors": {"色": "ネイビー", "サイズ": "L"}, "price": 3520, "estore_price": 3200, "stock": 5},
    ]
    return p


def test_valid_plans_pass():
    pl.validate_plan(_single())
    pl.validate_plan(_variation())


@pytest.mark.parametrize("mutate", [
    lambda p: p["variants"][0].update(stock=None),
    lambda p: p["variants"][0].update(stock=-1),
    lambda p: p["variants"][0].update(stock=True),
    lambda p: p["variants"][0].update(estore_price=0),
    lambda p: p.update(manage_number="G2225"),
    lambda p: p.update(sku_main="G2225-parent"),
    lambda p: p["yahoo"].update(copy_code=None),
    lambda p: p["yahoo"].update(copy_code="g2225"),
    lambda p: p["yahoo"].update(register=False),  # 登録しないのに公開
    lambda p: p["estore"].update(register=False),  # 登録しないのに公開
    lambda p: p["yahoo"].update(publish=False),  # 公開しないのに店頭反映
    lambda p: p["variants"].append(dict(p["variants"][0])),  # 単品なのに variant が複数
])
def test_invalid_single_plans_rejected(mutate):
    p = _single()
    mutate(p)
    with pytest.raises(SystemExit):
        pl.validate_plan(p)


def test_variation_rules():
    p = _variation()
    p["variants"][1]["variant_id"] = "v1"
    with pytest.raises(SystemExit):
        pl.validate_plan(p)
    p = _variation()
    p["variants"][0]["selectors"] = {}
    with pytest.raises(SystemExit):
        pl.validate_plan(p)


def test_approval_lifecycle():
    p = _single()
    with pytest.raises(SystemExit):
        pl.check_approval(p)  # 未承認
    p["approval"] = {"hash": pl.plan_hash(p), "approved_at": datetime.now().isoformat(timespec="seconds")}
    pl.check_approval(p)  # 承認済み
    edited = copy.deepcopy(p)
    edited["lead_time_days"] = 6
    with pytest.raises(SystemExit):
        pl.check_approval(edited)  # 承認後の編集
    old = copy.deepcopy(p)
    old["approval"]["approved_at"] = (datetime.now() - timedelta(hours=25)).isoformat(timespec="seconds")
    with pytest.raises(SystemExit):
        pl.check_approval(old)  # 期限切れ


def test_plan_hash_ignores_approval_only():
    p = _single()
    h = pl.plan_hash(p)
    p["approval"] = {"hash": "x", "approved_at": "2026-01-01T00:00:00"}
    assert pl.plan_hash(p) == h


class _C:
    def __init__(self, plan: dict):
        self.plan = plan
        self.g = plan["g"]


def _row(value: str, text: str) -> dict:
    return {"value": value, "text": text}


def test_goq_single_requires_one_row_with_code():
    c = _C(_single())
    assert pl._plan_goq_jobs(c, [_row("1", "G2225 ワッペン")]) == [("1", 20)]
    with pytest.raises(pl.NeedsUser):
        pl._plan_goq_jobs(c, [_row("1", "G2225"), _row("2", "G2225 Amazon")])
    with pytest.raises(pl.NeedsUser):
        pl._plan_goq_jobs(c, [_row("1", "別の商品 G9999")])


def test_goq_variation_requires_exact_one_to_one_mapping():
    c = _C(_variation())
    ok = [_row("10", "ネイビー M"), _row("11", "ネイビー L")]
    assert pl._plan_goq_jobs(c, ok) == [("10", 5), ("11", 5)]
    with pytest.raises(pl.NeedsUser):  # 行数が variant 数と違う(別チャネルの行が混入)
        pl._plan_goq_jobs(c, ok + [_row("12", "Amazon ネイビー M")])
    with pytest.raises(pl.NeedsUser):  # 同じ行に 2 つの variant が対応
        pl._plan_goq_jobs(c, [_row("10", "ネイビー M L"), _row("11", "ネイビー")])
    with pytest.raises(pl.NeedsUser):  # どの行にも対応しない
        pl._plan_goq_jobs(c, [_row("10", "ネイビー M"), _row("11", "ホワイト L")])


def test_excl_tax_examples():
    assert pl.excl_tax(1694) == 1540
    assert pl.excl_tax(1815) == 1650
