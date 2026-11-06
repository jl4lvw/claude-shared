"""持ち戻し依頼 指示書(A4・1枚 PDF)を作る。

template.html の {{名前}} を引数で埋め、Chromium(Playwright)で A4 の PDF にする。
PDF 化の方式は 091.領収書発行 の html_to_pdf と同じ。
"""
from __future__ import annotations

import argparse
import html
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEEKDAY = "月火水木金土日"

RECORD_BLOCK = """
  <h2>4. 対応の記録(電話のあとに記入)</h2>
  <table class="rec">
    <tr><th>電話した日時</th><td class="box">　　月　　日　　時　　分</td><th>対応した人</th><td class="box"></td></tr>
    <tr><th>結果</th><td colspan="3" class="box">□ 電話で受付済み　　□ 窓口での手続きが必要　　□ その他</td></tr>
    <tr><th>返送の見込み</th><td class="box"></td><th>手数料</th><td class="box">□ なし　□ あり(　　　円)</td></tr>
    <tr><th>メモ</th><td colspan="3" class="box" style="height:48px"></td></tr>
  </table>
"""


def parse_date(text: str) -> date:
    y, m, d = (int(x) for x in text.replace("/", "-").split("-"))
    return date(y, m, d)


def jp_date(d: date, weekday: bool = True) -> str:
    base = f"{d.year}年{d.month}月{d.day}日"
    return base + (f"({WEEKDAY[d.weekday()]})" if weekday else "")


def render(values: dict[str, str], raw: dict[str, str]) -> str:
    """template.html に値を入れる。values は HTML エスケープして入れ、raw は組み済みの HTML をそのまま入れる。"""
    text = (HERE / "template.html").read_text(encoding="utf-8")
    for key, val in values.items():
        text = text.replace("{{" + key + "}}", html.escape(val).replace("\n", "<br>"))
    for key, val in raw.items():
        text = text.replace("{{" + key + "}}", val)
    if "{{" in text:
        left = text[text.index("{{"):][:40]
        raise SystemExit(f"埋められていない項目があります: {left}")
    return text


def html_to_pdf(html_text: str, pdf_path: Path) -> None:
    from playwright.sync_api import sync_playwright

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(html_text, wait_until="load")
            page.pdf(path=str(pdf_path), format="A4", print_background=True, prefer_css_page_size=True,
                     margin={"top": "0", "right": "0", "bottom": "0", "left": "0"})
        finally:
            browser.close()


def page_count(pdf_path: Path) -> int:
    from pypdf import PdfReader

    return len(PdfReader(str(pdf_path)).pages)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="持ち戻し依頼 指示書(A4・1枚 PDF)を作る")
    ap.add_argument("--inquiry-no", required=True, help="お問い合わせ番号(例 2560-9255-8761)")
    ap.add_argument("--order-no", required=True, help="注文番号")
    ap.add_argument("--ship-date", required=True, help="差出日 YYYY-MM-DD")
    ap.add_argument("--to-name", required=True, help="お届け先の氏名(様は付けない)")
    ap.add_argument("--to-addr", required=True, help="お届け先の郵便番号と住所")
    ap.add_argument("--product-type", default="ゆうパック", help="商品種別(例 代金引換ゆうパック(代引まとめ)・サイズ80)")
    ap.add_argument("--status", required=True, help="現在の状況(例 11/4 ご不在のため持ち戻り(名寄郵便局で保管中) / お客様は受取を拒否)")
    ap.add_argument("--request-office", default="呉郵便局", help="電話をかける局(差し出した局)")
    ap.add_argument("--request-tel", default="0570-943-916", help="電話をかける局の番号")
    ap.add_argument("--holding-office", required=True, help="荷物がある局(追跡の配達予定局・取扱局)")
    ap.add_argument("--holding-note", default="", help="荷物がある局の補足(電話番号など)")
    ap.add_argument("--created", default="", help="作成日 YYYY-MM-DD(省略=今日)")
    ap.add_argument("--no-record", action="store_true", help="対応の記録欄を付けない")
    ap.add_argument("--out", required=True, help="出力する PDF のパス")
    ap.add_argument("--html-out", default="", help="確認用に HTML も書き出す")
    a = ap.parse_args()

    ship = parse_date(a.ship_date)
    created = parse_date(a.created) if a.created else date.today()
    note = a.holding_note.strip()
    values = {
        "created": jp_date(created),
        "company": "株式会社制服のフジ",
        "company_tel": "0823-21-7731",
        "return_addr": "〒737-0046 広島県呉市中通1-1-21 株式会社制服のフジ",
        "request_office": a.request_office,
        "request_tel": a.request_tel,
        "inquiry_no": a.inquiry_no,
        "order_no": a.order_no,
        "ship_date": jp_date(ship),
        "ship_date_short": f"{ship.month}月{ship.day}日",
        "to_name": a.to_name,
        "to_addr": a.to_addr,
        "product_type": a.product_type,
        "status": a.status,
        "holding_office": a.holding_office,
    }
    raw = {
        "holding_note": ("<br>" + html.escape(note)) if note else "",
        "record_block": "" if a.no_record else RECORD_BLOCK,
    }
    text = render(values, raw)

    if a.html_out:
        Path(a.html_out).write_text(text, encoding="utf-8", newline="")
    out = Path(a.out)
    html_to_pdf(text, out)
    pages = page_count(out)
    if pages != 1:
        print(f"NG: PDF が {pages} ページになりました(A4・1枚の条件に反します)", file=sys.stderr)
        return 1
    print(f"OK: {out} (1ページ)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
