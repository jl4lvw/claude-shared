"""西ロジの発注書(受信FAXのPDF)に、発送日のスタンプ(Eの案)を重ねる。

発注書は用紙が縦で、文字が90度横向きに入っている(読む向きで右下 = 画面の右上隅の摘要欄の右端)。
スタンプは読む向きで水平。全ページ同じ位置に押す。色は朱(FAXで送ると黒く出る)。

    python stamp_order_pdf.py --src "<受信FAXのPDF>" --out "<出力PDF>" --date 2026-11-12

位置・大きさを変えるときは --box X0 Y0 W H(ページ座標pt、画面の向き)。既定は 2026-11-09 の発注書で確かめた値。
"""
from __future__ import annotations

import argparse
import io
import sys
from datetime import date
from pathlib import Path

INK = "#c8321f"
# 摘要欄の右端の空き(画面の右上隅): x 516〜565pt, y 42.5〜288pt。スタンプは画面で 34 x 72pt(読む向きで横72 x 縦34pt、約25 x 12mm)
DEFAULT_BOX = (523.5, 52.5, 34.0, 72.0)


def stamp_svg(ymd: date) -> str:
    y, m, d = f"{ymd.year}年", f"{ymd.month}月", f"{ymd.day}日"
    return f"""<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Serif+JP:wght@600;900&display=swap">
<style>html,body{{margin:0;background:transparent}}svg{{display:block}}
.ink{{fill:none;stroke:{INK}}}.tx{{fill:{INK};font-family:'Noto Serif JP',serif}}</style></head><body>
<svg id="s" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 280 132" width="2240" height="1056">
<rect class="ink" x="3" y="3" width="274" height="126" rx="3" stroke-width="3.5"/>
<line class="ink" x1="3" y1="38" x2="277" y2="38" stroke-width="2"/>
<line class="ink" x1="3" y1="98" x2="277" y2="98" stroke-width="2"/>
<line class="ink" x1="108" y1="38" x2="108" y2="98" stroke-width="1.5"/>
<line class="ink" x1="192" y1="38" x2="192" y2="98" stroke-width="1.5"/>
<text class="tx" x="140" y="28" font-size="20" font-weight="600" text-anchor="middle" letter-spacing="14">発送日</text>
<text class="tx" x="55.5" y="77" font-size="24" font-weight="900" text-anchor="middle">{y}</text>
<text class="tx" x="150" y="78" font-size="27" font-weight="900" text-anchor="middle">{m}</text>
<text class="tx" x="234.5" y="78" font-size="27" font-weight="900" text-anchor="middle">{d}</text>
<text class="tx" x="140" y="120" font-size="16" font-weight="600" text-anchor="middle" letter-spacing="3">株式会社 制服のフジ</text>
</svg></body></html>"""


def render_stamp_png(ymd: date) -> bytes:
    """透明背景のスタンプPNG(反時計回りに90度回して、発注書の文字と同じ向きにしたもの)。"""
    from PIL import Image
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(viewport={"width": 2240, "height": 1056})
        pg.set_content(stamp_svg(ymd))
        pg.evaluate("document.fonts.ready")
        pg.wait_for_timeout(3000)  # Google Fonts の読み込み待ち
        png = pg.locator("#s").screenshot(omit_background=True)
        b.close()
    img = Image.open(io.BytesIO(png)).convert("RGBA").rotate(90, expand=True)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def main() -> None:
    import fitz

    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="発注書PDFに発送日スタンプを重ねる")
    ap.add_argument("--src", required=True, type=Path, help="受信FAXの発注書PDF")
    ap.add_argument("--out", required=True, type=Path, help="出力PDF")
    ap.add_argument("--date", required=True, help="発送日 YYYY-MM-DD")
    ap.add_argument("--box", nargs=4, type=float, metavar=("X0", "Y0", "W", "H"), default=DEFAULT_BOX)
    args = ap.parse_args()

    ymd = date.fromisoformat(args.date)
    stamp = render_stamp_png(ymd)
    x0, y0, w, h = args.box
    rect = fitz.Rect(x0, y0, x0 + w, y0 + h)
    doc = fitz.open(args.src)
    for page in doc:
        page.insert_image(rect, stream=stamp, keep_proportion=True, overlay=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(args.out, garbage=3, deflate=True)
    print(f"保存: {args.out}（{doc.page_count}ページ・位置 {rect}）")


if __name__ == "__main__":
    main()
