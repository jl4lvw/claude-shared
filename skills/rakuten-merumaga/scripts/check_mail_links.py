"""メルマガ(HTML)の中のリンクを検査する。読み取りだけ(何も書き換えない)。

  1. リンク切れ ……… 商品は RMS 商品 API(存在・非表示)、それ以外は HTTP で到達確認
  2. 説明との食い違い … メルマガ上の商品名・価格を、RMS の商品名・価格と照合
  3. 欠品 ……………… RMS 在庫 API(2.1)でバリエーションごとの在庫数を見る

判定:
  - リンク切れ・食い違いの疑い・一部の欠品 は「警告」(一覧で人が見る)
  - メルマガに載っている **商品がすべて販売できない状態**(在庫 0 または非表示)のときだけ「エラー」
終了コード: 0=問題なし / 1=警告あり / 2=エラー(全商品が販売不可 など)

使い方:
  python check_mail_links.py <メルマガHTML(貼り付け用タグやプレビュー).txt/.html> [--json 結果.json] [--http-items]
RMS の認証情報は 040.RakutenOkaimonoMarathon\\.env(環境変数 RMS_API_DIR で場所を変更可)から読む。値は表示しない。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import html_to_mobile_text as m

DEFAULT_RMS_DIR = Path(r"C:\ClaudeCode\900.ClaudeCode\040.RakutenOkaimonoMarathon")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
SHOP = "seifukunofuji"
ITEM_RE = re.compile(rf"^https?://item\.rakuten\.co\.jp/{SHOP}/([A-Za-z0-9_\-]+)/?$")
PRICE_RE = re.compile(r"^([0-9][0-9,]*)円")
NAME_OK = 0.6  # 商品名の一致度: これ以上なら問題なし
NAME_NG = 0.3  # これ未満なら「説明と違う疑い」、間は「要確認」
INV_CHUNK = 50
SLEEP = 1.1  # RMS API の間隔(秒)


# ----------------------------------------------------------------- 抽出 ----
@dataclass
class Link:
    url: str
    name: str  # URL の直前の行(商品名・リンク名)
    context: list[str] = field(default_factory=list)  # 直前の数行
    price: int | None = None  # メルマガ上の価格(円)

    @property
    def manage_number(self) -> str | None:
        mt = ITEM_RE.match(self.url)
        return mt.group(1).lower() if mt else None


def extract_links(html: str) -> list[Link]:
    """変換済みの行から、URL ごとに「直前の行(名前)」と価格を取り出す。同じ URL は 1 つにまとめる。"""
    lines = m.html_to_lines(html)
    links: dict[str, Link] = {}
    for i, ln in enumerate(lines):
        if not re.match(r"^https?://\S+$", ln):
            continue
        block: list[str] = []
        j = i - 1
        while j >= 0 and lines[j] != "" and not re.match(r"^https?://", lines[j]) and len(block) < 4:
            block.insert(0, lines[j])
            j -= 1
        price = None
        names = []
        for b in block:
            pm = PRICE_RE.match(b)
            if pm:
                price = int(pm.group(1).replace(",", ""))
            else:
                names.append(b)
        if ln not in links:
            links[ln] = Link(url=ln, name=names[-1] if names else "", context=block, price=price)
    return list(links.values())


# ----------------------------------------------------------------- 照合 ----
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    return re.sub(r"[\s\W_]+", "", s)


def name_score(mail_name: str, title: str) -> float:
    """メルマガの商品名の文字 2 つ組が、RMS のタイトルにどれだけ含まれるか(0〜1)。"""
    a, b = _norm(mail_name), _norm(title)
    if not a:
        return 1.0
    if a in b:
        return 1.0
    grams = {a[i : i + 2] for i in range(max(1, len(a) - 1))}
    return sum(1 for g in grams if g in b) / len(grams)


@dataclass
class Finding:
    level: str  # ok / warn / error
    kind: str  # link / name / price / stock / info
    url: str
    name: str
    message: str


# --------------------------------------------------------------- HTTP 側 ----
def fetch(url: str, timeout: float = 30.0) -> tuple[int, str, str]:
    """(状態コード, 最終URL, 本文テキスト)。到達不能は状態コード -1。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ja"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, code, final = r.read(), r.status, r.geturl()
    except urllib.error.HTTPError as e:
        raw, code, final = e.read(), e.code, url
    except Exception as e:  # noqa: BLE001
        return -1, url, f"{type(e).__name__}: {e}"
    for enc in ("utf-8", "euc_jp", "cp932"):
        text = raw.decode(enc, errors="replace")
        if text.count("\ufffd") < 5:
            break
    return code, final, text


def page_title(html: str) -> str:
    t = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
    return re.sub(r"\s+", " ", t.group(1)).strip() if t else ""


def _site(host: str) -> str:
    """rakuten.co.jp と rakuten.ne.jp は同じ楽天の中の移動として扱う。"""
    parts = host.split(".")
    return "rakuten" if "rakuten" in parts else host


GENERIC_LABEL = re.compile(r"▼|▲|こちら|クリック|タップ|詳細|はじめる|予約|会場")


def check_web_link(link: Link, get=fetch) -> list[Finding]:
    """商品以外のリンク(特設・カテゴリ・検索・クーポン)の到達確認と、リンク名との照合。"""
    code, final, text = get(link.url)
    f: list[Finding] = []
    title = page_title(text) if code > 0 else ""
    if code < 0 or code >= 400:
        return [Finding("warn", "link", link.url, link.name, f"リンク切れの疑い(HTTP {code})")]
    if re.search(r"エラー|見つかりません|存在しません|ページがありません", title):
        return [Finding("warn", "link", link.url, link.name, f"リンク切れの疑い(ページ名が「{title[:40]}」)")]
    host = urllib.parse.urlparse(link.url).netloc
    if _site(urllib.parse.urlparse(final).netloc) != _site(host):
        f.append(Finding("warn", "link", link.url, link.name, f"別のサイトへ移動します → {final[:70]}"))
    if host.startswith("coupon."):
        f.append(Finding("ok", "info", link.url, link.name, "クーポンページに到達(内容・期限はログインが要るため未検証)"))
    elif host.startswith("search."):
        kw = urllib.parse.unquote(urllib.parse.urlparse(link.url).path.rstrip("/").split("/")[-1])
        if kw and kw != "mall" and name_score(kw, title) < 1.0:
            f.append(Finding("warn", "name", link.url, link.name, f"検索語「{kw}」と、ページ名「{title[:40]}」が違います"))
        cnt = re.search(r"「[^」]*」\s*([0-9,]+)件", text)
        if cnt and cnt.group(1) == "0":
            f.append(Finding("warn", "link", link.url, link.name, f"検索結果が 0 件です(「{kw}」。商品名に「{kw}」を含む商品が今は無い)"))
        elif not f:
            f.append(Finding("ok", "info", link.url, link.name, f"検索ページに到達({title[:40]})"))
    else:
        label = re.sub(r"^[＞>\s]+", "", link.name)
        if label and not GENERIC_LABEL.search(label) and name_score(label, title) < NAME_NG and "http" not in label:
            f.append(Finding("warn", "name", link.url, link.name, f"リンクの説明「{label[:30]}」と、ページ名「{title[:40]}」が合いません"))
        elif not f:
            f.append(Finding("ok", "info", link.url, link.name, f"到達OK({title[:40]})"))
    return f


# --------------------------------------------------------------- RMS 側 ----
def load_rms() -> Any:
    base = Path(os.environ.get("RMS_API_DIR") or DEFAULT_RMS_DIR)
    if not (base / "rms_api.py").exists() or not (base / ".env").exists():
        raise SystemExit(f"RMS の設定が見つかりません: {base}(rms_api.py と .env が要ります。環境変数 RMS_API_DIR で変更可)")
    sys.path.insert(0, str(base))
    import rms_api  # type: ignore[import-not-found]

    rms_api.load_env(base / ".env")
    return rms_api


def rms_items(numbers: list[str], rms: Any, hdrs: dict) -> dict[str, dict]:
    return rms.bulk_get(numbers, hdrs)  # 取得できなかった商品は含まれない


def rms_stock(items: dict[str, dict], rms: Any, hdrs: dict) -> dict[tuple[str, str], int]:
    """(商品管理番号, バリエーションID) → 在庫数。"""
    reqs = [{"manageNumber": mn, "variantId": str(vid)} for mn, it in items.items() for vid in (it.get("variants") or {})]
    out: dict[tuple[str, str], int] = {}
    for i in range(0, len(reqs), INV_CHUNK):
        if i:
            time.sleep(SLEEP)
        d = rms.http_json("POST", "https://api.rms.rakuten.co.jp/es/2.1/inventories/bulk-get", hdrs, {"inventories": reqs[i : i + INV_CHUNK]})
        for inv in (d or {}).get("inventories", []):
            out[(inv["manageNumber"], str(inv["variantId"]))] = int(inv.get("quantity") or 0)
    return out


def check_item(link: Link, item: dict | None, stock: dict[tuple[str, str], int]) -> tuple[list[Finding], bool | None]:
    """商品 1 件の検査。戻り値の 2 つ目は「販売できる状態か」(None=判定不能)。"""
    mn = link.manage_number or ""
    if item is None:
        return [Finding("warn", "link", link.url, link.name, "リンク切れの疑い(RMS に商品が見つかりません)")], None
    f: list[Finding] = []
    if item.get("hideItem"):
        f.append(Finding("warn", "link", link.url, link.name, "商品が「非表示」になっています(リンク先は見られません)"))
    variants: dict[str, dict] = item.get("variants") or {}
    visible = {vid: v for vid, v in variants.items() if not v.get("hidden")}
    title = item.get("title") or ""
    sc = name_score(link.name, title)
    if sc < NAME_NG:
        f.append(Finding("warn", "name", link.url, link.name, f"説明と違う商品の疑い(一致度 {sc:.0%}): RMS 上の商品名「{title[:36]}」"))
    elif sc < NAME_OK:
        f.append(Finding("warn", "name", link.url, link.name, f"商品名が少し違います(一致度 {sc:.0%}): RMS 上「{title[:36]}」"))
    prices = {int(v["standardPrice"]) for v in visible.values() if v.get("standardPrice")}
    if link.price is not None and prices and link.price not in prices:
        f.append(Finding("warn", "price", link.url, link.name, f"価格が違います: メルマガ {link.price:,}円 / RMS {min(prices):,}円〜{max(prices):,}円"))
    qty = {vid: stock.get((mn, str(vid)), 0) for vid in visible}
    total = sum(qty.values())
    sellable = (not item.get("hideItem")) and total > 0
    if not visible:
        f.append(Finding("warn", "stock", link.url, link.name, "表示中のバリエーションがありません"))
    elif total == 0:
        f.append(Finding("warn", "stock", link.url, link.name, f"欠品: 表示中の {len(visible)} バリエーションがすべて在庫 0"))
    else:
        out = [vid for vid, q in qty.items() if q <= 0]
        if out:
            skus = [str(visible[v].get("merchantDefinedSkuId") or v) for v in out]
            f.append(Finding("warn", "stock", link.url, link.name, f"一部欠品: {len(out)}/{len(visible)} バリエーション({', '.join(skus[:4])}{'…' if len(skus) > 4 else ''})"))
    if not f:
        f.append(Finding("ok", "info", link.url, link.name, f"OK(在庫 計{total}・{len(visible)}バリエーション・{prices and f'{min(prices):,}円〜'}）"))
    return f, sellable


# ----------------------------------------------------------------- 本体 ----
def run(html: str, rms: Any | None = None, get=fetch, http_items: bool = False) -> tuple[list[Finding], dict]:
    links = extract_links(html)
    items_l = [lk for lk in links if lk.manage_number]
    web_l = [lk for lk in links if not lk.manage_number]
    findings: list[Finding] = []
    sellable: list[bool | None] = []
    if items_l:
        rms = rms or load_rms()
        hdrs = rms.headers()
        items = rms_items(sorted({lk.manage_number for lk in items_l if lk.manage_number}), rms, hdrs)
        time.sleep(SLEEP)
        stock = rms_stock(items, rms, hdrs)
        for lk in items_l:
            f, ok = check_item(lk, items.get(lk.manage_number or ""), stock)
            findings += f
            sellable.append(ok)
            if http_items:
                findings += [x for x in check_web_link(lk, get) if x.level != "ok"]
    for lk in web_l:
        findings += check_web_link(lk, get)
    n_item = len(items_l)
    n_ok = sum(1 for s in sellable if s)
    summary = {"links": len(links), "items": n_item, "items_sellable": n_ok, "all_items_unsellable": bool(n_item) and n_ok == 0}
    if summary["all_items_unsellable"]:
        findings.insert(0, Finding("error", "stock", "", "", f"エラー: メルマガの商品 {n_item} 点が、すべて販売できない状態です(在庫 0 または非表示)"))
    return findings, summary


def render(findings: list[Finding], summary: dict) -> str:
    out = [f"リンク {summary['links']} 本(商品 {summary['items']} 点・販売できる商品 {summary['items_sellable']} 点)"]
    for lvl, mark in (("error", "【エラー】"), ("warn", "【警告】")):
        sel = [x for x in findings if x.level == lvl]
        if sel:
            out.append(f"\n{mark}{len(sel)} 件")
            for x in sel:
                out.append(f"  ・{x.name[:34] or '(名前なし)'}\n      {x.message}\n      {x.url}" if x.url else f"  ・{x.message}")
    ok = sum(1 for x in findings if x.level == "ok")
    out.append(f"\nOK {ok} 件")
    return "\n".join(out)


def exit_code(findings: list[Finding]) -> int:
    if any(x.level == "error" for x in findings):
        return 2
    return 1 if any(x.level == "warn" for x in findings) else 0


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    ap = argparse.ArgumentParser(description="メルマガのリンク検査(リンク切れ・説明違い・欠品)")
    ap.add_argument("input", type=Path)
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--http-items", action="store_true", help="商品ページも HTTP で取得して確認する(遅い)")
    a = ap.parse_args(argv)
    findings, summary = run(m.read_text(a.input), http_items=a.http_items)
    print(render(findings, summary))
    if a.json:
        a.json.write_text(json.dumps({"summary": summary, "findings": [asdict(x) for x in findings]}, ensure_ascii=False, indent=2), encoding="utf-8")
    return exit_code(findings)


if __name__ == "__main__":
    raise SystemExit(main())
