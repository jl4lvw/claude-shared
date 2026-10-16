"""在庫切れ 現物確認表(A4・PDF)を作る。読み取り専用。

2026-10-16 寺下さん決定の形(案A・修正3回)で、現物確認の紙を印刷するためのもの:
  - 商品ごとに「写真・商品名・品番」と、バリエーション(サイズ)ごとの行
  - 列は「サイズ・種類 / Eストア在庫 / 現物の数(手書き)」の3つだけ
  - Eストア在庫が0のセルだけ薄い赤
  - 1商品(バリエーション全体)はページをまたがない。入らなければ商品ごと次のページへ
  - 「受注あり」「通知」の印、楽天在庫、見送りのチェック欄は出さない

使い方(Python 3.12 + playwright が要る):
    python stock_check_sheet.py --out-dir C:/tmp-ai/stockcheck               # 通知から要対応を拾う
    python stock_check_sheet.py --code G2210 --code U0147 --out-dir ...      # 品番を直接指定
    python stock_check_sheet.py ... --copy                                   # Downloads と NAS にもコピー

出力: stock_check_<日時>.pdf / .html(印刷用) / _artifact.html(アーティファクトで見る用)
"""

from __future__ import annotations

import argparse
import html
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import stock_alert_artifact as S  # noqa: E402

PRODUCT_DB = Path(r"C:\ClaudeCode\023.商品マスタDB\data\productmaster.db")
PRODUCT_ROOT = r"C:\ClaudeCode\023.商品マスタDB"
DOWNLOADS = Path.home() / "Downloads"
NAS_DIR = r"\\192.168.1.50\共有スペース\寺下\999.一時ファイル"

PAGE_W, PAGE_H = 794, 1123  # A4 を 96dpi の CSS px にした値
PAD_TOP, PAD_BOTTOM = 30, 52  # 下は脚注の分を空ける
# Eストアにある見せかけの行(在庫9995等でサイズ選択に出さないための行)。数える対象ではない
DUMMY_PATTERNS = ("以前のサイズデータ", "備考欄に記載")
DUMMY_MIN_QTY = 9000

CSS = """
:root{--bg:#f3f4f2;--fg:#1c2420;--mute:#59655f;--line:#cfd6d1;--paper:#fff;--ink:#111;--ink2:#444;--rule:#999;--thead:#e9e9e9;--zero:#f7caca}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#121619;--fg:#e3e9ee;--mute:#9aa7b3;--line:#34404a;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#121619;--fg:#e3e9ee;--mute:#9aa7b3;--line:#34404a;color-scheme:dark}
@page{size:A4;margin:0}
.pg{width:794px;height:1123px;box-sizing:border-box;padding:30px 46px;background:var(--paper);color:var(--ink);font-family:"Yu Gothic UI","Meiryo",sans-serif;font-size:14px;position:relative;overflow:hidden;page-break-after:always;break-after:page}
.pg:last-child{page-break-after:auto;break-after:auto}
.pgh{display:flex;justify-content:space-between;align-items:flex-end;border-bottom:3px solid var(--ink);padding-bottom:6px;margin-bottom:8px}
.pgh .t{font-size:24px;font-weight:700;letter-spacing:.04em}.pgh .m{font-size:12px;color:var(--ink2)}
.ph{object-fit:contain;background:#fff;border:1px solid var(--rule);display:block;width:76px;height:76px;flex:0 0 76px}.ph.noimg{display:flex;align-items:center;justify-content:center;color:var(--ink2);font-size:12px}
.cd{font-family:Consolas,"Courier New",monospace;font-weight:700;font-size:16px}
.blk{display:flex;gap:10px;margin-bottom:6px;break-inside:avoid}
.bl{width:110px;flex:0 0 110px;font-size:13px;line-height:1.4}.bn{margin-top:5px}
.vt{border-collapse:collapse;flex:1;min-width:0;align-self:flex-start}.vt th,.vt td{border:1.5px solid var(--ink);padding:0 7px;vertical-align:middle}
.vt th{background:var(--thead);font-size:11.5px;font-weight:700;height:34px;line-height:1.15}
.vt tr{height:34px}.vt .vn{font-size:15px;font-weight:700}.vt .c{text-align:center;font-variant-numeric:tabular-nums;font-size:15px;width:58px}
.vt .wr{width:230px;position:relative}.unit{position:absolute;right:8px;bottom:4px;font-size:14px}
.vt td.zero{background:var(--zero)}
.ft{position:absolute;left:46px;right:46px;bottom:18px;font-size:11.5px;color:var(--ink2)}.pno{float:right}
"""
ARTIFACT_CSS = """
body{background:var(--bg);color:var(--fg);font-family:"Yu Gothic UI","Hiragino Sans","Meiryo",sans-serif;line-height:1.6;padding-inline:16px;padding-block:20px;max-width:760px;margin-inline:auto}
h1{font-size:1.3rem;margin:0 0 2px}.sub{color:var(--mute);font-size:.88rem;margin-bottom:14px}.note{color:var(--mute);font-size:.88rem;margin:10px 0 0}
.stage{width:100%;max-width:700px;overflow:hidden;border:1px solid var(--line);border-radius:4px}.scaler{width:794px;transform-origin:0 0}
.pg+.pg{border-top:1px dashed #999}
"""
ARTIFACT_JS = """(function(){function fit(){document.querySelectorAll('.stage').forEach(function(st){var sc=st.querySelector('.scaler');var k=st.clientWidth/794;sc.style.transform='scale('+k+')';st.style.height=(sc.scrollHeight*k)+'px';});}window.addEventListener('resize',fit);fit();})();"""


def e(s: object) -> str:
    return html.escape(str(s))


def estore_rows(code: str, conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """Eストアのライブ在庫を (サイズ・種類, 数) の行にする。ダミー行は除く。"""
    from server.services.estore_client import request as estore_request

    res = estore_request("GET", f"/items/{code}/stock", api_kind="manager")
    stock = res.body.get("stock") if isinstance(res.body, dict) else None
    if not stock:
        raise RuntimeError(f"{code}: Eストアの在庫を取得できません")
    patterns = ((stock.get("management_variation") or {}).get("patterns")) or []
    if patterns:
        rows = []
        for p in patterns:
            label = " / ".join(p.get("pattern") or []) or "(名称なし)"
            qty = int(p.get("quantity") or 0)
            if any(d in label for d in DUMMY_PATTERNS) or qty >= DUMMY_MIN_QTY:
                continue
            rows.append((label, qty))
        return rows
    item = stock.get("management_item") or {}
    label = "単品"
    try:  # 色・サイズが1つだけ決まる商品は、その名前を行に出す
        from server.services import live_mall_stock as L

        d = L.fetch_live_mall_stock(conn, code)
        if d and len(d["variants"]) == 1:
            v = d["variants"][0]
            parts = [x for x in (v.get("color"), v.get("size")) if x]
            if parts:
                label = " / ".join(parts)
    except Exception:  # noqa: BLE001 — ラベルが取れなくても在庫は出す
        pass
    return [(label, int(item.get("quantity") or 0))]


def short_name(name: str, limit: int = 44) -> str:
    name = " ".join((name or "").split())
    return name if len(name) <= limit else name[: limit - 1] + "…"


def block_html(code: str, name: str, image_url: str, rows: list[tuple[str, int]]) -> str:
    uri = S.embed_image(image_url)
    pic = f'<img class="ph" src="{e(uri)}" alt="">' if uri else '<div class="ph noimg">画像なし</div>'
    trs = "".join(
        f'<tr><td class="vn">{e(label)}</td><td class="c{" zero" if qty == 0 else ""}">{qty}</td>'
        '<td class="wr"><span class="unit">個</span></td></tr>'
        for label, qty in rows
    )
    return (
        f'<div class="blk"><div class="bl">{pic}<div class="bn"><b>{e(short_name(name))}</b><br>'
        f'<span class="cd">{e(code)}</span></div></div>'
        '<table class="vt"><tr><th>サイズ・種類</th><th>Eストア<br>在庫</th><th>現物の数</th></tr>'
        f"{trs}</table></div>"
    )


def head_html(now: str) -> str:
    return (f'<div class="pgh"><div class="t">在庫切れ 現物確認表</div>'
            f'<div class="m">作成 {now} JST ／ 確認日 ____月____日 ／ 確認者 ________</div></div>')


def measure(blocks: dict[str, str], now: str) -> tuple[float, dict[str, float]]:
    """各商品ブロックの実際の高さを、ブラウザで測る(推測しない)。"""
    from playwright.sync_api import sync_playwright

    probe = ("<!doctype html><meta charset='utf-8'><style>" + CSS +
             "body{margin:0}.pg{height:auto;overflow:visible}</style><div class='pg'>" + head_html(now) +
             "".join(blocks.values()) + "</div>")
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page(viewport={"width": PAGE_W, "height": PAGE_H})
        pg.set_content(probe)
        pg.wait_for_timeout(400)
        m = pg.evaluate("""() => ({
            head: document.querySelector('.pgh').getBoundingClientRect().height,
            blocks: [...document.querySelectorAll('.blk')].map(b =>
                b.getBoundingClientRect().height + parseFloat(getComputedStyle(b).marginBottom))})""")
        b.close()
    return m["head"] + 8, dict(zip(blocks.keys(), m["blocks"]))


def paginate(order: list[str], heights: dict[str, float], head_h: float) -> list[list[str]]:
    """1商品(バリエーション全体)を分割せず、入らなければ商品ごと次のページへ。"""
    usable = PAGE_H - PAD_TOP - PAD_BOTTOM - head_h
    pages: list[list[str]] = []
    cur: list[str] = []
    used = 0.0
    for c in order:
        if cur and used + heights[c] > usable:
            pages.append(cur)
            cur, used = [], 0.0
        cur.append(c)
        used += heights[c]
    if cur:
        pages.append(cur)
    return pages


def to_pdf(page_html: str, pdf_path: Path) -> int:
    """PDF にして、ページ数を返す。"""
    from playwright.sync_api import sync_playwright

    doc = "<!doctype html><meta charset='utf-8'><style>" + CSS + "body{margin:0;background:#fff}</style>" + page_html
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page()
        pg.set_content(doc)
        pg.wait_for_timeout(400)
        pg.pdf(path=str(pdf_path), format="A4", print_background=True, prefer_css_page_size=True,
               margin={"top": "0", "right": "0", "bottom": "0", "left": "0"})
        b.close()
    try:
        import re
        return len(re.findall(rb"/Type\s*/Page[^s]", pdf_path.read_bytes()))
    except OSError:
        return 0


def pick_codes(days: int) -> list[str]:
    data = S.run_stock_notices(days)
    seen: list[str] = []
    for g in data.get("groups", []):
        if g.get("needs_action"):
            c = S.base_code(str(g.get("code", "")))
            if c not in seen:
                seen.append(c)
    return seen


def main() -> int:
    ap = argparse.ArgumentParser(description="在庫切れ 現物確認表(A4 PDF)を作る")
    ap.add_argument("--code", action="append", default=[], help="品番(複数可。無指定=通知の要対応から拾う)")
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--copy", action="store_true", help="Downloads と NAS の 999.一時ファイル にもコピーする")
    args = ap.parse_args()

    codes = [S.base_code(c) for c in args.code] or pick_codes(args.days)
    if not codes:
        print("要対応の在庫切れはありません。")
        return 0
    estore = S.fetch_estore_items()
    sys.path.insert(0, PRODUCT_ROOT)
    conn = sqlite3.connect(str(PRODUCT_DB))
    conn.row_factory = sqlite3.Row

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    blocks: dict[str, str] = {}
    for c in codes:
        it = estore.get(c) or {}
        rows = estore_rows(c, conn)
        blocks[c] = block_html(c, it.get("item_name") or "(商品名不明)", it.get("image_url") or "", rows)
        print(f"  {c}: {len(rows)}行 / {short_name(it.get('item_name') or '')}")

    head_h, heights = measure(blocks, now)
    pages = paginate(codes, heights, head_h)
    n = len(pages)
    page_html = "".join(
        '<div class="pg">' + head_html(now) + "".join(blocks[c] for c in cs) +
        '<div class="ft">数えた数を書く → 「品番 サイズ ○個」の形で返信 ／ 数が分からないときは空欄のまま ／ '
        f'Eストア在庫が0のセルは薄い赤<span class="pno">{i} / {n} ページ</span></div></div>'
        for i, cs in enumerate(pages, 1)
    )

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    pdf = out / f"stock_check_{stamp}.pdf"
    pages_in_pdf = to_pdf(page_html, pdf)
    (out / f"stock_check_{stamp}.html").write_text(
        "<!doctype html><meta charset='utf-8'><style>" + CSS + "body{margin:0;background:#fff}</style>" + page_html,
        encoding="utf-8", newline="")
    (out / f"stock_check_{stamp}_artifact.html").write_text(
        "<title>現物確認シート</title>\n<style>\n" + CSS + ARTIFACT_CSS + "\n</style>\n"
        f"<h1>現物確認シート({n}ページ)</h1><div class=\"sub\">{now} JST</div>\n"
        '<div class="stage"><div class="scaler">' + page_html + "</div></div>\n"
        f"<script>{ARTIFACT_JS}</script>\n", encoding="utf-8", newline="")
    print(f"ページ割り: {pages}")
    print(f"PDF: {pdf}  (PDF内ページ数 {pages_in_pdf} / 想定 {n})")
    if pages_in_pdf and pages_in_pdf != n:
        print("★ PDF のページ数が想定と違います。印刷前に PDF を開いて確認してください。", file=sys.stderr)
        return 1

    if args.copy:
        for dest in (DOWNLOADS, Path(NAS_DIR)):
            try:
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copy2(pdf, dest / pdf.name)
                ok = (dest / pdf.name).stat().st_size == pdf.stat().st_size
                print(f"コピー: {dest / pdf.name}  {'OK(サイズ一致)' if ok else '★サイズ不一致'}")
            except OSError as exc:
                print(f"★ コピー失敗: {dest} ({exc})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
