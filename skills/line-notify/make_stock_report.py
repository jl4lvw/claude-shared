"""在庫変更の報告用画像(余白なし・Eストア商品トップ画像つき)と本文を作る。

使い方:
  python make_stock_report.py --code G2243 --title "部隊識別帽(第4護衛隊)" \
      --row 佐官用=25 --row 一般用=100 --outdir <作業フォルダ> [--date 10/21] [--body-name "G2243(...)"]

出力(outdir):
  out.png   報告画像(Eストア商品トップ画像+在庫表+メモ。下の余白は切り落とし済み)
  body.txt  本文(URL は line_notify.py の --url が末尾に足す)
  top.jpg   取得した商品トップ画像

決まり(2026-10-21 運用者指示): 何の商品かわかるよう Eストアの商品トップ画像を必ず載せる。
報告用で印刷しないので、A4 の空きは残さない(footer は使わず確認方法はメモへ)。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

from PIL import Image, ImageChops

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
DETAIL_API = "http://127.0.0.1:8294/api/items/{code}/detail"
DETAIL_HEADERS = {"X-Forwarded-For": "192.168.1.200"}  # 026 は素のアクセスだと 403
BOTTOM_PAD = 40


def fetch_top_image(code: str, dest: Path) -> str:
    req = urllib.request.Request(DETAIL_API.format(code=code), headers=DETAIL_HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        url = json.load(r).get("image_url")
    if not url:
        raise SystemExit(f"{code}: Eストアの商品トップ画像がありません(026 の image_url が空)")
    with urllib.request.urlopen(url, timeout=30) as r:
        dest.write_bytes(r.read())
    Image.open(dest).verify()
    return url


def crop_bottom(png: Path, out: Path) -> tuple[int, int]:
    im = Image.open(png).convert("RGB")
    bbox = ImageChops.difference(im, Image.new("RGB", im.size, "white")).getbbox()
    bottom = min(im.height, bbox[3] + BOTTOM_PAD)
    im.crop((0, 0, im.width, bottom)).save(out)
    return im.width, bottom


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True, help="Eストア商品コード(例 G2243)")
    ap.add_argument("--title", required=True, help="商品名の短い形(例 部隊識別帽(第4護衛隊))")
    ap.add_argument("--row", action="append", required=True, help="種類=在庫数(例 佐官用=25)。複数可")
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--date", required=True, help="更新日(例 10/21)")
    ap.add_argument("--malls", default="楽天・Yahoo!・本店", help="件名の反映先")
    ap.add_argument("--body-name", help="本文の商品名(例 G2243(部隊識別帽 第4護衛隊 アゴヒモ付))")
    a = ap.parse_args()

    outdir = Path(a.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    top = outdir / "top.jpg"
    print("商品トップ画像:", fetch_top_image(a.code, top))

    rows = [r.split("=", 1) for r in a.row]
    spec = {
        "title": f"{a.code} {a.title} 在庫({a.date}更新)",
        "subtitle": f"{a.malls}の3モールに反映済み",
        "sections": [
            {"heading": "ネットの在庫数", "image": top.as_posix(),
             "grid": {"header": ["種類", "在庫数"], "widths": [260, 260], "rows": rows}},
            {"heading": "メモ", "lines": ["反映先: 楽天市場 / Yahoo!ショッピング / 本店(Eストア)",
                                        "確認方法: 各モールの在庫を読み戻して確認済み"]},
        ],
    }
    spec_path = outdir / "spec.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    full = outdir / "full.png"
    subprocess.run([sys.executable, str(HERE / "make_status_image.py"), str(spec_path), str(full)], check=True)
    w, h = crop_bottom(full, outdir / "out.png")
    print(f"画像: {outdir / 'out.png'} ({w}x{h})")

    name = a.body_name or f"{a.code}({a.title})"
    body = f"{name}の在庫を反映しました。\n" + " / ".join(f"{k} {v}" for k, v in rows) + f"\n{a.malls}に反映済みです。\n"
    (outdir / "body.txt").write_text(body, encoding="utf-8", newline="\n")
    print("本文:", outdir / "body.txt")


if __name__ == "__main__":
    main()
