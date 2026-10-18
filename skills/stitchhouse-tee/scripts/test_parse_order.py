import pytest

import parse_order as p

TEXT = """おつかれさまです。Tシャツの注文お願いします。
ブラック: Mサイズ50枚 / Lサイズ70枚
ネイビー: LLサイズ20枚
オリーブ: 3Lサイズ10枚
グレー: LLサイズ10枚
ホワイト: Mサイズ10枚 / LLサイズ20枚
よろしくお願いします
"""


def test_parse_total_and_alias():
    rows = p.parse(TEXT)
    assert sum(q for _, _, q in rows) == 190
    assert ("アーミーグリーン", "3L", 10) in rows
    assert ("ダークグレー", "LL", 10) in rows


def test_codes():
    rows = p.to_csv_rows(p.parse(TEXT))
    assert ["00300", "005", "02", "50", "ステッチハウス", "fuji"] in rows
    assert ["00300", "037", "06", "10", "ステッチハウス", "fuji"] in rows
    assert ["00300", "187", "04", "10", "ステッチハウス", "fuji"] in rows
    assert ["00300", "001", "04", "20", "ステッチハウス", "fuji"] in rows


def test_unknown_color_stops():
    with pytest.raises(p.ParseError):
        p.to_csv_rows(p.parse("ピンク: Mサイズ5枚"))


def test_no_rows():
    with pytest.raises(p.ParseError):
        p.parse("こんにちは")
