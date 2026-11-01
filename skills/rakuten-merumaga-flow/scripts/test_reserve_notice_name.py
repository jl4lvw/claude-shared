"""reserve_notice._plain: 表に出す名前から頭の【…】と絵文字を外す(2026-11-01 受付 #51 で 4 行が同じ見え方になった)。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import reserve_notice as rn  # noqa: E402

HEAD = "【早期特典★1部購入ごとに部隊ピンバッチをプレゼント】 "


def test_plain_strips_bracket_head_so_rows_differ():
    a = rn._plain(HEAD + "月刊『丸』特別編集 海上自衛隊カレンダー2027年 ( A2・横型 壁掛け )")[:16]
    b = rn._plain(HEAD + "海上自衛隊カレンダー2027年 卓上 ( A5 )")[:16]
    assert a.startswith("月刊『丸』") and b.startswith("海上自衛隊")
    assert a != b


def test_plain_two_heads_and_emoji():
    assert rn._plain("【送料無料】【限定】艦艇BOXカレー🍛") == "艦艇BOXカレー"


def test_plain_bracket_only_name_is_kept():
    assert rn._plain("【早期特典】") == "【早期特典】"
