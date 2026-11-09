"""西ロジ向けの仮納品書(JAN・自番号・商品名・数量・賞味期限)を作る。

様式は 900.ClaudeCode/納品書発行/template.html(紺の帯・金額なし)を流用し、列だけ差し替える。
明細は JSON で渡す(例: data/20261109_items.json)。賞味期限は既定で全行「対象外」。

    python make_kari_note.py --items data/20261109_items.json --date 2026-11-17 \
        --out-dir "<出力フォルダ>" [--copy]

--copy は PDF を Downloads と NAS(999.一時ファイル)へ写して照合する。
"""
from __future__ import annotations

import argparse
import html
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = Path(r"C:\ClaudeCode\900.ClaudeCode\納品書発行\template.html")
COPY_DIRS = [Path.home() / "Downloads", Path(r"\\192.168.1.50\共有スペース\寺下\999.一時ファイル")]
HEAD_OLD = ('<thead><tr><th class="c">No.</th><th>品番</th><th>品名</th><th class="c">数量</th>'
            '<th>備考</th></tr></thead>')
HEAD_NEW = ('<thead><tr><th class="c">No.</th><th>JANコード</th><th>自番号</th><th>商品名</th>'
            '<th class="c">数量</th><th class="c">賞味期限</th></tr></thead>')
FOOT_OLD = '<tfoot><tr><td colspan="3" class="r">合計</td><td class="c qty">{{total_qty}}</td><td></td></tr></tfoot>'
FOOT_NEW = '<tfoot><tr><td colspan="4" class="r">合計</td><td class="c qty">{{total_qty}}</td><td></td></tr></tfoot>'


def jp_date(iso: str) -> str:
    y, m, d = (int(x) for x in iso.split("-"))
    return f"{y}年{m}月{d}日"


def build_html(cfg: dict, delivery_date: str) -> tuple[str, int, int]:
    esc = html.escape
    items = cfg["items"]
    rows = "\n".join(
        f'<tr><td class="c">{i}</td><td class="code">{esc(it["jan"])}</td><td class="code">{esc(it["code"])}</td>'
        f'<td>{esc(it["name"])}' + (f'<div class="rem">{esc(it["sub"])}</div>' if it.get("sub") else "") + "</td>"
        f'<td class="c qty">{int(it["qty"])}</td><td class="c rem">{esc(it.get("expiry", "対象外"))}</td></tr>'
        for i, it in enumerate(items, 1)
    )
    total = sum(int(it["qty"]) for it in items)
    t = TEMPLATE.read_text(encoding="utf-8")
    for old, new in ((HEAD_OLD, HEAD_NEW), (FOOT_OLD, FOOT_NEW),
                     ('<div class="banner">納品書</div>', '<div class="banner">仮納品書</div>'),
                     ('<div class="meta"><div>納品日<span>', '<div class="meta"><div>納品予定日<span>')):
        if old not in t:
            raise SystemExit(f"納品書のtemplate.htmlが変わっていて差し替えできません: {old[:40]}")
        t = t.replace(old, new)
    t = t.replace("tbody tr:nth-child(even)", "td .rem{margin-top:2px}\ntbody tr:nth-child(even)")
    vals = {
        "title": esc(cfg["title"]),
        "delivery_date": jp_date(delivery_date),
        "recipient": esc(cfg["recipient"]),
        "recipient_sub": cfg["recipient_sub"],  # <br> を含むのでそのまま(自分で書いた固定文)
        "order_ref": esc(cfg["order_ref"]),
        "rows": rows,
        "total_qty": str(total),
        "item_count": str(len(items)),
        "notice": "",
    }
    for k, v in vals.items():
        t = t.replace("{{" + k + "}}", v)
    return t, len(items), total


def main() -> None:
    from playwright.sync_api import sync_playwright
    import fitz

    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="西ロジ向け仮納品書を作る")
    ap.add_argument("--items", required=True, type=Path, help="明細JSON(data/*.json)")
    ap.add_argument("--date", required=True, help="納品予定日 YYYY-MM-DD")
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--stem", default="仮納品書_西ロジ")
    ap.add_argument("--copy", action="store_true", help="Downloads と NAS へ写して照合する")
    args = ap.parse_args()

    cfg = json.loads(args.items.read_text(encoding="utf-8"))
    doc_html, n_items, total = build_html(cfg, args.date)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    html_path = args.out_dir / f"{args.stem}.html"
    pdf_path = args.out_dir / f"{args.stem}.pdf"
    png_path = args.out_dir / f"{args.stem}.png"
    html_path.write_text(doc_html, encoding="utf-8", newline="")
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.goto(html_path.as_uri(), wait_until="networkidle")
        pg.evaluate("document.fonts.ready")
        pg.pdf(path=str(pdf_path), format="A4", print_background=True, prefer_css_page_size=True)
        b.close()
    pdf = fitz.open(pdf_path)
    pages = pdf.page_count
    pdf[0].get_pixmap(dpi=110).save(png_path)
    print(f"{n_items}品目 / {total}点 / PDF {pages}ページ\nPDF: {pdf_path}\nPNG: {png_path}")
    if pages != 1:
        print("注意: 2ページ以上あります。品名の長さや行数を確かめてください。")
    if args.copy:
        for d in COPY_DIRS:
            if not d.is_dir():
                print(f"写せませんでした(フォルダがありません): {d}")
                continue
            dst = d / pdf_path.name
            shutil.copy2(pdf_path, dst)
            same = dst.read_bytes() == pdf_path.read_bytes()
            print(f"写しました: {dst}（照合 {'一致' if same else '不一致!'}）")


if __name__ == "__main__":
    main()
