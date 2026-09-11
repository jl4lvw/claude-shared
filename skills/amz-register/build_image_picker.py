"""amz-register Phase 1: other画像クリック選択アーティファクトのビルドスクリプト.

template_image_picker.html (プレースホルダ入りの静的HTML) を、候補画像のJSON配列と
パラメータで埋めて完成HTMLを吐き出す。テンプレート側は普通のHTMLファイルなので
Pythonの文字列リテラル(f-string/.format()のブレースエスケープ)が一切絡まず、
表現の揺れ(CSS/JS破損)が起きない。

使い方:
    python build_image_picker.py \
      --candidates candidates.json \
      --img-dir "C:/ProductMaster/images/G2179" \
      --output g2179_image_picker.html \
      --slug g2179 \
      --title-tag "G2179 画像候補(クリック選択)" \
      --eyebrow "G2179 — other画像 クリック選択(最大6枚)" \
      --h1 "PVCワッペン(護衛艦かが F-35B・ブルー)" \
      --max-select 6

candidates.json の各要素 (キー名固定):
    {"name": "sub_02", "label": "正面・純白背景", "url": "https://image.rakuten.co.jp/..."}

"name" は img-dir 配下の "<name>.jpg" を指す。先頭から A, B, C... の文字が自動で振られる。
"url" は任意(省略可)。画像取得元の実URL(楽天CDN等)を渡すと、アーティファクト上で
その画像を長押し(600ms)してクリップボードへコピーできるようになる — 選定後に
別PCでmain画像を加工する際、検索し直さずに元画像へ辿り着けるようにするための機能
(2026-09-11追加)。023商品マスタDBの product_images.rakuten_location から
`https://image.rakuten.co.jp/{RAKUTEN_SHOP_URL}/cabinet{rakuten_location}` で組み立てられる
(server.services.rakuten_image_import.build_image_url() と同じ規則)。
"""

from __future__ import annotations

import argparse
import base64
import json
import string
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = SKILL_DIR / "template_image_picker.html"


def b64_file(path: Path) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def build_cards(candidates: list[dict], img_dir: Path) -> str:
    letters = list(string.ascii_uppercase)
    if len(candidates) > len(letters):
        raise ValueError(f"candidates が26件を超えています({len(candidates)}件)")
    cards = []
    for i, c in enumerate(candidates):
        letter = letters[i]
        name = c["name"]
        label = c.get("label", "")
        url = c.get("url", "")
        img_b64 = b64_file(img_dir / f"{name}.jpg")
        url_attr = f' data-url="{url}"' if url else ""
        url_hint = '<span class="url-hint">長押しでURLコピー</span>' if url else ""
        cards.append(
            f'    <figure data-letter="{letter}"{url_attr}><span class="letter">{letter}</span>'
            f'<span class="order-badge"></span>'
            f'<img src="{img_b64}" alt="{letter}: {name} {label}">'
            f'<figcaption><span class="name">{letter} &mdash; {name}</span>'
            f'<span class="label">{label}</span>{url_hint}</figcaption></figure>'
        )
    return "\n".join(cards)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--candidates", required=True, help="候補画像JSON配列のパス")
    ap.add_argument("--img-dir", required=True, help="候補画像が置かれているディレクトリ")
    ap.add_argument("--output", required=True, help="出力HTMLパス")
    ap.add_argument("--slug", required=True, help="db保存先スラッグ(例: g2179)。カテゴリ/色ごとに一意にする")
    ap.add_argument("--title-tag", required=True, help="<title>タグの中身")
    ap.add_argument("--eyebrow", required=True, help="見出し上のラベル")
    ap.add_argument("--h1", required=True, help="商品名等の見出し")
    ap.add_argument(
        "--subtitle",
        default=(
            "画像をクリックした順に other_1〜N として登録されます。"
            "main画像は別途ファイルパス/URLでご提供ください。"
            "画像を長押し(600ms)すると取得元URLをコピーできます。"
        ),
        help="見出し下の説明文",
    )
    ap.add_argument("--note", default="", help="任意の注意書き(在庫切れ等)。空なら非表示")
    ap.add_argument(
        "--footer",
        default="SP-API へはまだ何も送信していません。クリックで選んだ順序は自動保存されます。",
        help="フッター文言",
    )
    ap.add_argument("--max-select", type=int, default=6, help="最大選択数(既定6)")
    args = ap.parse_args()

    candidates = json.loads(Path(args.candidates).read_text(encoding="utf-8"))
    img_dir = Path(args.img_dir)
    cards_html = build_cards(candidates, img_dir)

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    note_block = f'  <div class="note">{args.note}</div>\n' if args.note else ""

    html = template
    html = html.replace("__TITLE_TAG__", args.title_tag)
    html = html.replace("__EYEBROW__", args.eyebrow)
    html = html.replace("__H1__", args.h1)
    html = html.replace("__SUBTITLE__", args.subtitle)
    html = html.replace("__NOTE_BLOCK__", note_block)
    html = html.replace("__FOOTER__", args.footer)
    html = html.replace("__SLUG__", args.slug)
    html = html.replace("__MAX__", str(args.max_select))
    html = html.replace("__CARDS__", cards_html)

    out_path = Path(args.output)
    out_path.write_text(html, encoding="utf-8")
    print(f"written {out_path} ({len(html)} bytes, {len(candidates)} candidates)")


if __name__ == "__main__":
    main()
