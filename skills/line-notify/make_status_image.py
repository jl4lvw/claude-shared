"""状況説明用の A4 縦 1 枚の画像(PNG)を、JSON の仕様から作る。

使い方:
  python make_status_image.py spec.json out.png

spec.json の形:
  {
    "title": "G0964 部隊識別コイン(護衛艦かが)",
    "subtitle": "「ケース入り」販売終了に伴う 掲載内容の変更  2026-10-16 完了",
    "sections": [
      {"heading": "変更したこと", "table": [["楽天市場", "商品名から「ケース入」を削除 / ケース写真1枚を削除"], ...]},
      {"heading": "商品名の変わり方", "before": "変更前の文字列", "after": "変更後の文字列", "strike": "取り消し線で強調する語"},
      {"heading": "削除した写真", "images": [{"path": "C:/.../a.jpg", "caption": "楽天", "mark": "del"}, ...]},
      {"heading": "残した写真の例", "images": [{"path": "...", "mark": "keep"}], "note": ["注意", "・補足1", "・補足2"]},
      {"heading": "メモ", "lines": ["1行目", "2行目"]}
    ],
    "footer": "確認方法: ○○を読み戻して確認済み"
  }
mark は "del"(赤枠)/ "keep"(緑枠)/ 省略(枠なし)。内容が1ページに収まらないときは例外で止まる(小さくせず、項目を減らす)。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.stdout.reconfigure(encoding="utf-8")

W, H = 1240, 1754  # A4 縦 150dpi
M = 60
INK, MUT, LINE = (29, 36, 48), (91, 101, 115), (210, 215, 222)
RED, REDBG, GRN, GRNBG = (179, 38, 30), (253, 236, 235), (27, 110, 60), (232, 245, 236)
BLUE = (31, 78, 140)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(rf"C:\Windows\Fonts\BIZ-UDGothic{'B' if bold else 'R'}.ttc", size)


def render(spec: dict, out: Path) -> None:
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)

    def wrap(text: str, f: ImageFont.FreeTypeFont, width: int) -> list[str]:
        lines, cur = [], ""
        for ch in text:
            if d.textlength(cur + ch, font=f) > width:
                lines.append(cur)
                cur = ch
            else:
                cur += ch
        lines.append(cur)
        return lines

    d.rectangle([0, 0, W, 150], fill=BLUE)
    d.text((M, 28), spec["title"], font=font(40, True), fill="white")
    if spec.get("subtitle"):
        d.text((M, 88), spec["subtitle"], font=font(26), fill="white")
    y = 185
    limit = H - 90

    def need(h: int) -> None:
        if y + h > limit:
            raise SystemExit(f"内容が1ページに収まりません(y={y}, 必要={h})。項目を減らしてください")

    for sec in spec.get("sections", []):
        need(60)
        d.text((M, y), sec["heading"], font=font(30, True), fill=INK)
        y += 52
        if "table" in sec:
            for label, txt in sec["table"]:
                lines = wrap(txt, font(23), W - 2 * M - 220)
                h = max(74, 20 + 28 * len(lines))
                need(h + 12)
                d.rectangle([M, y, W - M, y + h], outline=LINE, width=2)
                d.rectangle([M, y, M + 190, y + h], fill=(240, 243, 248))
                d.text((M + 14, y + 20), label, font=font(26, True), fill=INK)
                for i, ln in enumerate(lines):
                    d.text((M + 205, y + 10 + i * 28), ln, font=font(23), fill=INK)
                y += h + 12
            y += 18
        if "grid" in sec:
            g = sec["grid"]
            hdr, rows, widths = g["header"], g["rows"], g["widths"]
            stop = set(g.get("stop_rows", []))
            rh, img_w = 46, 230
            block = max(img_w if sec.get("image") else 0, rh * (len(rows) + 1))
            need(block + 20)
            gx = M
            if sec.get("image"):
                t = Image.open(sec["image"]).convert("RGB")
                t.thumbnail((img_w, img_w))
                im.paste(t, (M, y))
                d.rectangle([M - 1, y - 1, M + t.width, y + t.height], outline=LINE, width=2)
                gx = M + img_w + 24
            xs = [gx]
            for w in widths:
                xs.append(xs[-1] + w)
            d.rectangle([gx, y, xs[-1], y + rh], fill=(226, 232, 241), outline=LINE, width=2)
            for j, h_ in enumerate(hdr):
                d.text((xs[j] + 12, y + 9), h_, font=font(24, True), fill=INK)
            for i, row in enumerate(rows):
                ry = y + rh * (i + 1)
                d.rectangle([gx, ry, xs[-1], ry + rh], fill=REDBG if i in stop else "white", outline=LINE, width=2)
                for j, cell in enumerate(row):
                    d.text((xs[j] + 12, ry + 9), str(cell), font=font(24, j == 0), fill=RED if i in stop and j == len(row) - 1 else INK)
            y += block + 28
        if "before" in sec:
            need(160)
            f26 = font(26)
            d.rectangle([M, y, W - M, y + 62], fill=REDBG)
            d.text((M + 14, y + 6), "変更前", font=font(18), fill=MUT)
            before, strike = sec["before"], sec.get("strike") or ""
            i = before.find(strike) if strike else -1
            if i < 0:
                d.text((M + 14, y + 28), before, font=f26, fill=INK)
            else:
                head, tail = before[:i], before[i + len(strike):]
                x = M + 14
                d.text((x, y + 28), head, font=f26, fill=INK)
                x += d.textlength(head, font=f26)
                d.text((x, y + 28), strike, font=font(26, True), fill=RED)
                d.line([x, y + 44, x + d.textlength(strike, font=f26), y + 44], fill=RED, width=3)
                x += d.textlength(strike, font=f26)
                d.text((x, y + 28), tail, font=f26, fill=INK)
            y += 72
            d.rectangle([M, y, W - M, y + 62], fill=GRNBG)
            d.text((M + 14, y + 6), "変更後", font=font(18), fill=MUT)
            d.text((M + 14, y + 28), sec["after"], font=f26, fill=INK)
            y += 90
        if "images" in sec:
            imgs = sec["images"]
            cols = min(4, max(1, len(imgs))) if not sec.get("note") else 3
            cw = (W - 2 * M - (cols - 1) * 20) // 4 if not sec.get("note") else 220
            need(cw + 70)
            for i, spec_img in enumerate(imgs):
                t = Image.open(spec_img["path"]).convert("RGB")
                t.thumbnail((cw, cw))
                x = M + i * (cw + 20)
                col = {"del": RED, "keep": GRN}.get(spec_img.get("mark", ""), None)
                if col:
                    d.rectangle([x - 2, y - 2, x + cw + 2, y + cw + 2], outline=col, width=3)
                im.paste(t, (x + (cw - t.width) // 2, y + (cw - t.height) // 2))
                if spec_img.get("caption"):
                    d.text((x, y + cw + 10), spec_img["caption"], font=font(19), fill=INK)
            if sec.get("note"):
                nx = M + len(imgs) * (cw + 20) + 10
                for i, ln in enumerate(sec["note"]):
                    d.text((nx, y + i * 34), ln, font=font(22, i == 0), fill=RED if i == 0 else INK)
                y += cw + 60
            else:
                y += cw + 60
        if "lines" in sec:
            for ln in sec["lines"]:
                for sub in wrap(ln, font(24), W - 2 * M):
                    need(36)
                    d.text((M, y), sub, font=font(24), fill=INK)
                    y += 36
            y += 14
    # 下の余白は削る(2026-10-23 運用者決定: A4 である必要は無く、余白があれば詰めてよい)。幅は変えず、縦だけ内容に合わせる。
    # 全面 A4 にしたいときは spec に "trim": false
    bottom = H if spec.get("trim") is False else min(H, y + (100 if spec.get("footer") else 40))
    if spec.get("footer"):
        d.text((M, bottom - 60), spec["footer"], font=font(20), fill=MUT)
    if bottom < H:
        im = im.crop((0, 0, W, bottom))
    im.save(out, dpi=(150, 150))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    render(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")), Path(sys.argv[2]))
    print("作成:", sys.argv[2])
