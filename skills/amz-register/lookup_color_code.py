"""Amazon出品SKU用のカラーコード変換ツール。

色名(日本語/英語)からcolor_codes.jsonの正式コードを引く。その場で略語を
推測しない(NVY/BLKのような誤りを防ぐため)。未登録の色はエラーにして
color_codes.jsonへの追記を促す。

使い方:
    python lookup_color_code.py ブラック
    python lookup_color_code.py navy
    python lookup_color_code.py --list
"""

import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

DATA_PATH = Path(__file__).resolve().parent / "color_codes.json"


def load_codes() -> dict[str, dict]:
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))["codes"]


def build_lookup(codes: dict[str, dict]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for code, entry in codes.items():
        lookup[code.upper()] = code
        for alias in entry["aliases"]:
            lookup[alias.upper()] = code
    return lookup


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("query", nargs="?", help="色名(日本語/英語) または既存コード")
    ap.add_argument("--list", action="store_true", help="登録済みの全コードを一覧表示")
    args = ap.parse_args()

    codes = load_codes()

    if args.list:
        for code, entry in codes.items():
            print(f"{code}\t{'/'.join(entry['aliases'])}\t{entry['note']}")
        return

    if not args.query:
        ap.error("色名を指定してください(または --list)")

    lookup = build_lookup(codes)
    key = args.query.strip().upper()
    if key in lookup:
        print(lookup[key])
        return

    print(f"ERROR: '{args.query}' は color_codes.json に未登録です。", file=sys.stderr)
    print("推測で略語を作らず、実物を確認のうえ color_codes.json に追記してから再実行してください。", file=sys.stderr)
    print("登録済みコード一覧:", file=sys.stderr)
    for code, entry in codes.items():
        print(f"  {code}: {'/'.join(entry['aliases'])}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
