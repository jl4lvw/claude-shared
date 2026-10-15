"""在庫切れ・連携在庫マイナスの要対応を、商品画像つきのアーティファクト用HTMLにする。

使い方(mc で在庫切れ通知が出たとき):
    python stock_alert_artifact.py --days 3 --out C:/ClaudeCode/.mail_drafts/stock_alert.html
    python stock_alert_artifact.py --code G0884 --out ...      # 通知から品番を直接指定

流れ:
    1. stock_notices.py --json(Eストアー在庫切れ・GoQ連携在庫マイナスの通知を集約し、
       023 の現在庫で「まだ足りないもの」だけ残す)を呼ぶ
    2. 026 の Eストア商品一覧(localhost:8294)で、商品画像・Eストアの在庫を足す
    3. 要対応を1商品1枚のカードにして HTML を書く(読み取り専用。在庫は変更しない)

在庫の変更は、寺下さんが数を決めたあと、goq-stock-set スキルで承認後に行う。
"""

from __future__ import annotations

import argparse
import base64
import html
import io
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

STOCK_NOTICES = Path(r"C:\ClaudeCode\900.ClaudeCode\mail-search\scripts\stock_notices.py")
ESTORE_EXPORT = "http://localhost:8294/api/items/export"
JST = timezone(timedelta(hours=9))
SOURCE_LABEL = {"goq": "連携在庫マイナス", "estore": "在庫切れ(Eストア)"}


def run_stock_notices(days: int) -> dict:
    """stock_notices.py --json を実行して結果を返す。"""
    proc = subprocess.run(
        [sys.executable, str(STOCK_NOTICES), "--days", str(days), "--json"],
        capture_output=True, text=True, encoding="utf-8", timeout=180,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    if proc.returncode != 0:
        raise RuntimeError(f"stock_notices.py が失敗しました: {proc.stderr[:300]}")
    return json.loads(proc.stdout)


def fetch_estore_items() -> dict[str, dict]:
    """026 のEストア商品一覧を {item_code: 商品} で返す。取れなければ空。"""
    try:
        with urllib.request.urlopen(ESTORE_EXPORT, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — 画像が出せなくても一覧は出す
        print(f"[警告] Eストア商品一覧を取得できませんでした: {exc}", file=sys.stderr)
        return {}
    return {i["item_code"].upper(): i for i in data.get("items", [])}


def embed_image(url: str) -> str:
    """商品画像を取得して縮小し、data URI にする。

    アーティファクトの画面は、外部サイトの画像を読み込めない(CSPで遮断される)ので、
    ページへ埋め込む必要がある。取れなければ空文字(「画像なし」と出す)。
    """
    if not url:
        return ""
    try:
        from PIL import Image  # 遅延import: 無くても一覧は出せる

        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img.thumbnail((240, 240))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=80)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as exc:  # noqa: BLE001 — 画像が出せなくても一覧は出す
        print(f"[警告] 画像を取得できませんでした: {url} ({exc})", file=sys.stderr)
        return ""


def base_code(code: str) -> str:
    """G2123-NV-3L / U0143<>5L / g21233l のような表記から、親の品番(G2123)を取る。"""
    m = re.match(r"^([A-Za-z]\d{4})", code.strip())
    return m.group(1).upper() if m else code.strip().upper()


def jst_text(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(JST)
        return dt.strftime("%m/%d %H:%M")
    except ValueError:
        return iso


def stock_text(value: int | None) -> str:
    return "不明" if value is None else f"{value:,}"


def card(group: dict, estore: dict[str, dict]) -> str:
    code = str(group.get("code", ""))
    item = estore.get(base_code(code))
    name = group.get("name") or (item or {}).get("item_name") or "(商品名不明)"
    image = embed_image((item or {}).get("image_url") or "")
    estore_stock = (item or {}).get("stock_quantity")
    sources = " / ".join(SOURCE_LABEL.get(s, s) for s in group.get("sources", []))
    orders = "、".join(group.get("order_nos") or []) or "なし"
    variants = group.get("variants") or []
    variant_text = "、".join(variants) if variants else ""
    img_html = (f'<img src="{html.escape(image)}" alt="" loading="lazy">' if image
                else '<div class="noimg">画像なし</div>')
    url = group.get("url") or ""
    link = f'<a href="{html.escape(url)}">商品ページ</a>' if url else ""
    return f"""<div class="card">
<div class="pic">{img_html}</div>
<div class="body">
<div class="name">{html.escape(name)}</div>
<div class="code"><b>{html.escape(code)}</b>{('　対象: ' + html.escape(variant_text)) if variant_text else ''}</div>
<div class="tags"><span class="tag">{html.escape(sources)}</span><span class="tag mute">通知{group.get('count', 0)}回・最新 {html.escape(jst_text(group.get('latest', '')))}</span></div>
<table><tr><th>在庫(023)</th><td>{html.escape(stock_text(group.get('current_stock')))}</td><th>Eストア在庫</th><td>{html.escape(stock_text(estore_stock))}</td></tr>
<tr><th>該当する受注</th><td colspan="3">{html.escape(orders)} {link}</td></tr></table>
<div class="reply">返信の例: <code>{html.escape(code)} ○個</code> または <code>{html.escape(code)} 見送り</code></div>
</div></div>"""


def render(groups: list[dict], estore: dict[str, dict], now: datetime) -> str:
    cards = "\n".join(card(g, estore) for g in groups)
    empty = "" if groups else '<div class="card">要対応の在庫切れ・連携在庫マイナスはありません。</div>'
    return f"""<title>在庫切れ 要対応</title>
<style>
:root{{--bg:#f6f7f8;--fg:#1f2933;--mute:#5b6773;--card:#ffffff;--line:#d5dbe0;--accent:#1d5c8a;--warn:#8a5a00;--warnbg:#fff4d6;--tag:#e8eef3}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#14181c;--fg:#e4e9ee;--mute:#9aa7b3;--card:#1c2229;--line:#34404a;--accent:#7fb5e0;--warn:#f0c36a;--warnbg:#3a2f12;--tag:#2a343d;color-scheme:dark}}}}
:root[data-theme="dark"]{{--bg:#14181c;--fg:#e4e9ee;--mute:#9aa7b3;--card:#1c2229;--line:#34404a;--accent:#7fb5e0;--warn:#f0c36a;--warnbg:#3a2f12;--tag:#2a343d;color-scheme:dark}}
body{{background:var(--bg);color:var(--fg);font-family:"Yu Gothic UI","Hiragino Sans","Meiryo",sans-serif;line-height:1.6;padding-inline:16px;padding-block:20px;max-width:900px;margin-inline:auto}}
h1{{font-size:1.35rem;margin:0 0 4px}}.sub{{color:var(--mute);font-size:.9rem;margin-bottom:12px}}
.warn{{background:var(--warnbg);color:var(--warn);border-radius:6px;padding:10px 14px;margin-bottom:14px}}
.card{{display:flex;gap:14px;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:12px 14px;margin-bottom:12px;min-width:0}}
.pic{{flex:0 0 120px}}.pic img{{width:120px;height:120px;object-fit:contain;background:#fff;border-radius:4px;border:1px solid var(--line)}}
.noimg{{width:120px;height:120px;display:flex;align-items:center;justify-content:center;color:var(--mute);border:1px dashed var(--line);border-radius:4px;font-size:.85rem}}
.body{{flex:1;min-width:0}}.name{{font-weight:600;overflow-wrap:anywhere}}.code{{color:var(--accent);margin:2px 0 6px;overflow-wrap:anywhere}}
.tag{{display:inline-block;background:var(--tag);border-radius:10px;padding:1px 9px;font-size:.8rem;margin-right:6px}}.tag.mute{{color:var(--mute)}}
table{{border-collapse:collapse;width:100%;margin:8px 0;font-size:.9rem}}th,td{{border-bottom:1px solid var(--line);padding:3px 6px;text-align:left;vertical-align:top;overflow-wrap:anywhere}}th{{color:var(--mute);font-weight:400;white-space:nowrap}}
.reply{{color:var(--mute);font-size:.85rem}}code{{background:var(--tag);padding:1px 5px;border-radius:3px}}
@media (max-width:560px){{.card{{flex-direction:column}}}}
</style>
<h1>在庫切れ 要対応(Reply needed)</h1>
<div class="sub">{now.strftime('%Y-%m-%d %H:%M')} JST時点 / 在庫(023)は現在の値です。書き込みはしていません。</div>
<div class="warn">在庫を復活できるものは、現物を確認して、「品番 ○個」の形で返信してください。数が決まったら、変更の一覧を出して承認をもらってから、GoQ の在庫連携画面で変更します。</div>
{cards}{empty}
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="在庫切れ・連携在庫マイナスの要対応を商品画像つきのHTMLにする")
    ap.add_argument("--days", type=int, default=3, help="直近何日分の通知か(既定3)")
    ap.add_argument("--code", action="append", default=[], help="この品番だけ出す(複数可。通知の有無に関係なく)")
    ap.add_argument("--all", action="store_true", help="補充済みのものも出す")
    ap.add_argument("--out", required=True, help="出力するHTMLのパス")
    args = ap.parse_args()

    data = run_stock_notices(args.days)
    groups = data.get("groups", [])
    if not args.all:
        groups = [g for g in groups if g.get("needs_action")]
    if args.code:
        wanted = {base_code(c) for c in args.code}
        groups = [g for g in data.get("groups", []) if base_code(str(g.get("code", ""))) in wanted]
    estore = fetch_estore_items()
    page = render(groups, estore, datetime.now(JST))
    Path(args.out).write_text(page, encoding="utf-8", newline="")
    print(f"出力: {args.out} / 要対応 {len(groups)} 商品 / 画像あり {sum(1 for g in groups if (estore.get(base_code(str(g.get('code', '')))) or {}).get('image_url'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
