"""reserve_notice._name / _plain: 表に出す名前から頭の【…】と絵文字を外す(2026-11-01 受付 #51 で 4 行が同じ見え方になった)。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import reserve_notice as rn  # noqa: E402

HEAD = "【早期特典★1部購入ごとに部隊ピンバッチをプレゼント】 "


def test_name_strips_bracket_head_so_rows_differ():
    a = rn._name(HEAD + "月刊『丸』特別編集 海上自衛隊カレンダー2027年 ( A2・横型 壁掛け )")[:16]
    b = rn._name(HEAD + "海上自衛隊カレンダー2027年 卓上 ( A5 )")[:16]
    assert a.startswith("月刊『丸』") and b.startswith("海上自衛隊")
    assert a != b


def test_name_two_heads_and_emoji():
    assert rn._name("【送料無料】【限定】艦艇BOXカレー🍛") == "艦艇BOXカレー"


def test_name_bracket_only_is_kept():
    assert rn._name("【早期特典】") == "【早期特典】"
    assert rn._name("【早期特典】🎁") == "【早期特典】"  # 空判定は絵文字除去の後(Lv7 レビューの退行指摘)


def test_plain_does_not_strip_bracket():
    assert rn._plain("【警告】在庫 0🎁") == "【警告】在庫 0"
