"""コンパス(CONPHAS)の仕入(SMS)・売上(UMS)明細を読み取り専用で検索する CLI.

013.CONPHAS-PWA の外部データ API (/api/external/data/extract/rows) を呼ぶだけ。
書き込みはしない。API キーは external_secrets.json から読み、画面には出さない。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

SECRETS = Path(r"C:\ClaudeCode\013.CONPHAS-PWA\server\external_secrets.json")
URL = "https://sfuji.f5.si/conphasapi/api/external/data/extract/rows"
KEY_NAME = "fuji-main"
MAX_DAYS = 90  # サーバー上限は 92 日。余裕を持って分割する
MODE_LABEL = {"purchase": "仕入", "sales": "売上"}


def fold(s: str) -> str:
    """全角/半角・大小・空白の違いを無視するための正規化."""
    return unicodedata.normalize("NFKC", s or "").lower().replace(" ", "").replace("\u3000", "")


def load_key() -> str:
    cfg = json.loads(SECRETS.read_text(encoding="utf-8"))
    for ent in cfg["external_api_keys"]:
        if ent["name"] == KEY_NAME:
            return ent["key"]
    raise SystemExit(f"API キー {KEY_NAME} が見つかりません")


def fetch(key: str, mode: str, start: date, end: date, customer: str) -> list[dict]:
    body = json.dumps(
        {
            "mode": mode,
            "range_type": "date",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "customer_code": customer,
            "max_rows": 10000,
        }
    ).encode()
    req = urllib.request.Request(
        URL, data=body, headers={"Content-Type": "application/json", "X-API-Key": key}
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"API エラー {e.code}: {e.read()[:200].decode('utf-8', 'replace')}")
    if d.get("truncated"):
        print(f"[警告] {start}〜{end} は件数上限で切り詰められました。期間を狭めてください", file=sys.stderr)
    return d["rows"]


def chunks(start: date, end: date):
    cur = start
    while cur <= end:
        nxt = min(cur + timedelta(days=MAX_DAYS - 1), end)
        yield cur, nxt
        cur = nxt + timedelta(days=1)


def main() -> None:
    ap = argparse.ArgumentParser(description="コンパス明細の検索(読み取り専用)")
    ap.add_argument("--mode", choices=["purchase", "sales", "both"], default="purchase")
    ap.add_argument("--from", dest="start", help="開始日 YYYY-MM-DD (既定: 30日前)")
    ap.add_argument("--to", dest="end", help="終了日 YYYY-MM-DD (既定: 今日)")
    ap.add_argument("--kw", nargs="*", default=[], help="品名・得意先名のキーワード(AND)")
    ap.add_argument("--customer", default="", help="得意先/仕入先コード")
    ap.add_argument("--json", action="store_true", help="JSON Lines で出力")
    a = ap.parse_args()

    end = date.fromisoformat(a.end) if a.end else date.today()
    start = date.fromisoformat(a.start) if a.start else end - timedelta(days=30)
    if end < start:
        raise SystemExit("期間が不正です (終了日 < 開始日)")
    kws = [fold(k) for k in a.kw]
    key = load_key()
    modes = ["purchase", "sales"] if a.mode == "both" else [a.mode]

    hits: list[tuple[str, dict]] = []
    total = 0
    for mode in modes:
        for s, e in chunks(start, end):
            rows = fetch(key, mode, s, e, a.customer)
            total += len(rows)
            for r in rows:
                hay = fold(f"{r.get('SNA','')}{r.get('顧客名','')}{r.get('備考','')}{r.get('TKY','')}")
                if all(k in hay for k in kws):
                    hits.append((mode, r))
            time.sleep(0.5)

    hits.sort(key=lambda t: (t[1].get("年月日", ""), t[1].get("DNO", "")))
    if a.json:
        for mode, r in hits:
            print(json.dumps({"mode": mode, **r}, ensure_ascii=False))
        return
    print(f"期間 {start}〜{end} / 取得 {total} 行 / 一致 {len(hits)} 行")
    for mode, r in hits:
        qty = float(r.get("SRR") or 0)
        print(
            f"{r.get('年月日','')} {MODE_LABEL[mode]} 伝票{r.get('DNO','').strip()} "
            f"{r.get('SNA','').strip()}(品番{r.get('MIN','').strip()}) "
            f"{qty:g}個 × {float(r.get('TNK') or 0):,.0f}円 = {float(r.get('KIN') or 0):,.0f}円 "
            f"/ {r.get('顧客名','').strip()}"
        )


if __name__ == "__main__":
    main()
