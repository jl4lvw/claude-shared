"""ステッチハウスの無地Tシャツ手配テキスト → TOMS 取り込みCSV(品番00300)。

入力例:  ブラック: Mサイズ50枚 / Lサイズ70枚
色名はユーザー決定(2026-10-17)で読み替える: グレー→ダークグレー, オリーブ→アーミーグリーン。
コードは 017.Tシャツなどの注文/toms_color_codes.json と toms_size_codes.json を正とする。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PRODUCT = "00300"
ROOT = Path(__file__).resolve().parents[4]
DATA = ROOT / "017.Tシャツなどの注文"
COLOR_ALIAS = {"グレー": "ダークグレー", "グレイ": "ダークグレー", "オリーブ": "アーミーグリーン"}
SIZE_RE = re.compile(r"(5L|4L|3L|LL|L|M|S|\d{2,3})\s*(?:サイズ)?\s*[:：]?\s*(\d+)\s*枚")
LABEL_RE = re.compile(r"^\s*([^\s:：/]+)\s*[:：]\s*(.+)$")
NOTE = "ステッチハウス"
ORDER_NO = "fuji"


class ParseError(ValueError):
    pass


def normalize_color(name: str) -> str:
    return COLOR_ALIAS.get(name.strip(), name.strip())


def parse(text: str) -> list[tuple[str, str, int]]:
    """(色名(読み替え後), サイズ, 枚数) を行順に返す。"""
    rows: list[tuple[str, str, int]] = []
    for line in text.splitlines():
        m = LABEL_RE.match(line)
        if not m:
            continue
        sizes = SIZE_RE.findall(m.group(2))
        if not sizes:
            continue
        color = normalize_color(m.group(1))
        rows += [(color, s, int(n)) for s, n in sizes]
    if not rows:
        raise ParseError("色:サイズ枚数 の行が見つかりません")
    return rows


def to_csv_rows(rows: list[tuple[str, str, int]]) -> list[list[str]]:
    colors = json.loads((DATA / "toms_color_codes.json").read_text(encoding="utf-8"))[PRODUCT]
    sizes = json.loads((DATA / "toms_size_codes.json").read_text(encoding="utf-8"))[PRODUCT]
    out = []
    for color, size, qty in rows:
        if color not in colors:
            raise ParseError(f"色コード未登録: {color}(TOMS画面で確認して toms_color_codes.json に追記)")
        if size not in sizes:
            raise ParseError(f"サイズコード未登録: {size}")
        out.append([PRODUCT, colors[color], sizes[size], str(qty), NOTE, ORDER_NO])
    return out


def write_csv(path: Path, rows: list[list[str]]) -> None:
    head = "品番(5桁),カラーコード,サイズコード,数量,備考（納品書・出荷案内書の行備考）,お客様注文NO"
    body = "\n".join(",".join(r) for r in rows)
    path.write_bytes((head + "\n" + body + "\n").encode("cp932"))


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="依頼テキストのファイル(UTF-8)")
    ap.add_argument("--out", help="CSV出力先(省略時は表示のみ)")
    a = ap.parse_args(argv)
    parsed = parse(Path(a.input).read_text(encoding="utf-8-sig"))
    csv_rows = to_csv_rows(parsed)
    total = sum(q for _, _, q in parsed)
    for (c, s, q), r in zip(parsed, csv_rows):
        print(f"{c:<8} {s:<3} {q:>4}枚  -> {r[1]},{r[2]}")
    print(f"明細 {len(parsed)} 行 / 合計 {total} 枚")
    if a.out:
        write_csv(Path(a.out), csv_rows)
        print("CSV:", a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
