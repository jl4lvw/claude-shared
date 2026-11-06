"""文字だけの 10面ラベルを A4 に割り付け、PDF 作成と印刷を行う。

    python text_label.py make  labels.json [--place 倉庫3階] [--out-dir DIR]
    python text_label.py print labels.json --place 倉庫3階 [--apply]

labels.json の形:
    {"name": "サイズ数量8名",
     "labels": [["あか　山田一郎", "大（L）x5", "中（M）x1"], null, ...],
     "line_pt": [22, 30]}
labels は 1 面ごとの行の配列(null は空きの面)。line_pt は省略可で、行ごとの文字の大きさ(pt)。
足りない行は最後の値を使う。省略すると全面で同じ大きさにし、一番長い面が収まる最大にする。

位置は印刷サーバーのプリンターごとの調整値を使い、並びは PWA の割付と同じ列優先。
print は --apply が無ければ送り先と枚数を出すだけで、何も送らない。
"""
import argparse
import base64
import hashlib
import io
import json
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, r"C:\ClaudeCode\011.ラベル印刷PWA\server")
import labelprint as lp  # noqa: E402

DPI = 600
FONT = "C:/Windows/Fonts/YuGothB.ttc"
LAYOUT = lp.MENSU_LAYOUTS["10"]
COLS, ROWS = int(LAYOUT["cols"]), int(LAYOUT["rows"])
PER_SHEET = COLS * ROWS
LABEL_W, LABEL_H = float(LAYOUT["label_w"]), float(LAYOUT["label_h"])
MARGIN_MM = 4.0
LINE_GAP_MM = 3.5
MAX_PT, MIN_PT, PT_STEP = 30.0, 9.0, 0.5
PRINTERS = {
    "倉庫3階": "px-s887-label10",
    "倉庫2階": "souko2f-px-s887",
    "本店": "honten1f-px-s887-tray3-label10",
}
COPY_DIRS = (Path(r"C:\Users\user\Downloads"), Path(r"\\192.168.1.50\共有スペース\寺下\999.一時ファイル"))
JOB_WAIT_SEC, JOB_POLL_SEC = 120, 3
ACTIVE_JOB = ("queued", "pending", "processing", "printing", "running")


@dataclass(frozen=True)
class Spec:
    name: str
    labels: list[list[str] | None]
    line_pt: list[float] | None = None


def load_spec(path: Path) -> Spec:
    data = json.loads(path.read_text(encoding="utf-8"))
    labels = data.get("labels")
    if not isinstance(labels, list) or not any(labels):
        raise ValueError("labels に 1 面以上の文字を入れてください")
    for i, lines in enumerate(labels, 1):
        if lines is None:
            continue
        if not isinstance(lines, list) or not lines or not all(isinstance(t, str) and t.strip() for t in lines):
            raise ValueError(f"{i} 面目の行が正しくありません: {lines!r}")
    return Spec(str(data.get("name") or "文字ラベル"), labels, data.get("line_pt"))


def px(mm: float) -> int:
    return round(mm / 25.4 * DPI)


def font(pt: float) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT, round(pt / 72 * DPI))


def text_width(text: str, pt: float) -> float:
    return font(pt).getlength(text)


def pts_for(lines: list[str], line_pt: list[float] | None, pt: float) -> list[float]:
    if not line_pt:
        return [pt] * len(lines)
    return [float(line_pt[min(i, len(line_pt) - 1)]) for i in range(len(lines))]


def fits(lines: list[str], pts: list[float]) -> bool:
    if any(text_width(t, p) > px(LABEL_W - MARGIN_MM * 2) for t, p in zip(lines, pts)):
        return False
    height = sum(px(p / 72 * 25.4) for p in pts) + px(LINE_GAP_MM) * (len(lines) - 1)
    return height <= px(LABEL_H - MARGIN_MM * 2)


def choose_pt(spec: Spec) -> float:
    """全面で同じ大きさにするときの、一番長い面が収まる最大の大きさ。"""
    faces = [lines for lines in spec.labels if lines]
    if spec.line_pt:
        for lines in faces:
            if not fits(lines, pts_for(lines, spec.line_pt, 0)):
                raise ValueError(f"面に収まりません: {lines}")
        return 0.0
    pt = MAX_PT
    while pt >= MIN_PT:
        if all(fits(lines, [pt] * len(lines)) for lines in faces):
            return pt
        pt -= PT_STEP
    raise ValueError(f"{MIN_PT}pt でも面に収まりません。行を減らすか短くしてください")


def make_label(lines: list[str], pts: list[float]) -> Image.Image:
    w, h = px(LABEL_W), px(LABEL_H)
    img = Image.new("L", (w, h), 255)
    draw = ImageDraw.Draw(img)
    heights = [px(p / 72 * 25.4) for p in pts]
    y = (h - sum(heights) - px(LINE_GAP_MM) * (len(lines) - 1)) / 2
    for text, pt, lh in zip(lines, pts, heights):
        draw.text((w / 2, y + lh / 2), text, font=font(pt), fill=0, anchor="mm")
        y += lh + px(LINE_GAP_MM)
    return img


def render(spec: Spec) -> list[Image.Image | None]:
    pt = choose_pt(spec)
    return [make_label(lines, pts_for(lines, spec.line_pt, pt)) if lines else None for lines in spec.labels]


def split_sheets(cells: list[Any]) -> list[list[Any]]:
    return [cells[i:i + PER_SHEET] for i in range(0, len(cells), PER_SHEET)]


def geometry(cal: dict[str, Any]) -> dict[str, float]:
    def pick(key: str, default: float) -> float:
        value = cal.get(key)
        return default if value is None else float(value)

    scale = pick("scale", 1.0)
    return {
        "ox": pick("offset_x_mm", LAYOUT["default_offset_x"]),
        "oy": pick("offset_y_mm", LAYOUT["default_offset_y"]),
        "gx": pick("gutter_x_mm", LAYOUT["default_gutter_x"]),
        "gy": pick("gutter_y_mm", LAYOUT["default_gutter_y"]),
        "w": LABEL_W * scale,
        "h": LABEL_H * scale,
    }


def cell_xy(idx: int, g: dict[str, float]) -> tuple[float, float]:
    """列優先(左列の上から下 → 右列)。app.js の buildMultiLabelHTML と同じ順。"""
    c, r = divmod(idx, ROWS)
    return g["ox"] + c * (g["w"] + g["gx"]), g["oy"] + r * (g["h"] + g["gy"])


def sheet_image(cells: list[Image.Image | None], g: dict[str, float]) -> Image.Image:
    sheet = Image.new("L", (px(210), px(297)), 255)
    for i, label in enumerate(cells):
        if label is not None:
            x, y = cell_xy(i, g)
            sheet.paste(label.resize((px(g["w"]), px(g["h"]))), (px(x), px(y)))
    return sheet


def preview_image(sheet: Image.Image, g: dict[str, float]) -> Image.Image:
    out = sheet.convert("RGB")
    draw = ImageDraw.Draw(out)
    for i in range(PER_SHEET):
        x, y = cell_xy(i, g)
        draw.rectangle((px(x), px(y), px(x + g["w"]), px(y + g["h"])), outline=(255, 0, 0), width=6)
    return out.resize((out.width // 4, out.height // 4), Image.LANCZOS)


def sheet_html(cells: list[Image.Image | None], g: dict[str, float]) -> str:
    css, labels = "", ""
    for i, label in enumerate(cells):
        if label is None:
            continue
        buf = io.BytesIO()
        label.save(buf, "PNG", dpi=(DPI, DPI))
        src = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        x, y = cell_xy(i, g)
        css += f'.bg{i}{{background-image:url("{src}");}}\n'
        labels += f'<div class="label bg{i}" style="top:{lp.js_num(y)}mm;left:{lp.js_num(x)}mm;"></div>\n'
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
@page {{ size: A4 portrait; margin: 0; }}
* {{ margin: 0; padding: 0; }}
body {{ width: 210mm; }}
.page {{ width: 210mm; height: 297mm; position: relative; overflow: hidden; }}
.label {{ position: absolute; width: {lp.js_num(g["w"])}mm; height: {lp.js_num(g["h"])}mm; overflow: hidden; background-size: contain; background-repeat: no-repeat; background-position: center; }}
{css}</style></head><body><div class="page">{labels}</div></body></html>"""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_out(pdf: Path) -> None:
    """Downloads と NAS へ置く。フォルダが無いときは作らず知らせる(同名のファイルができる事故を防ぐ)。"""
    for folder in COPY_DIRS:
        if not folder.is_dir():
            print(f"置けませんでした(フォルダがありません): {folder}")
            continue
        dest = folder / pdf.name
        shutil.copyfile(pdf, dest)
        print(f"配置: {dest}" if sha256(dest) == sha256(pdf) else f"配置に失敗(中身が不一致): {dest}")


def cmd_make(args: argparse.Namespace) -> int:
    spec = load_spec(args.json)
    g = geometry(lp.get_calibration(PRINTERS[args.place], "10"))
    sheets = [sheet_image(cells, g) for cells in split_sheets(render(spec))]
    out_dir = args.out_dir or args.json.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf = out_dir / f"10面ラベル_{spec.name}_{date.today():%Y%m%d}.pdf"
    sheets[0].save(pdf, "PDF", resolution=DPI, save_all=True, append_images=sheets[1:])
    for i, sheet in enumerate(sheets, 1):
        preview = out_dir / f"preview_{i}.png"
        preview_image(sheet, g).save(preview)
        print(f"確認画像: {preview}")
    print(f"PDF: {pdf}({len(sheets)} 枚)")
    copy_out(pdf)
    return 0


def wait_job(job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + JOB_WAIT_SEC
    while True:
        time.sleep(JOB_POLL_SEC)
        status = lp.job_status(job_id)
        if status["job_status"] not in ACTIVE_JOB or time.monotonic() > deadline:
            return status


def cmd_print(args: argparse.Namespace) -> int:
    spec = load_spec(args.json)
    printer = PRINTERS[args.place]
    pages = split_sheets(render(spec))
    faces = sum(1 for lines in spec.labels if lines)
    print(f"送り先: {args.place}({printer})・{len(pages)} 枚・{faces} 面")
    if not args.apply:
        print("--apply が無いので送っていません")
        return 0
    g = geometry(lp.get_calibration(printer, "10"))
    for i, cells in enumerate(pages, 1):
        job = lp.enqueue(printer, sheet_html(cells, g), f"{spec.name} {i}/{len(pages)}")
        status = wait_job(job["job_id"])
        print(f"{i}/{len(pages)} 枚目: {status['job_status']} {status['message']}")
        if status["job_status"] != "done":
            print(f"止めました。送信済みは {i} 枚です。やり直す前に、紙と印刷の状態を確かめてください")
            return 1
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="文字だけの 10面ラベルを作って印刷する")
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("make", help="PDF と確認画像を作る")
    make.add_argument("json", type=Path)
    make.add_argument("--place", choices=PRINTERS, default="倉庫3階")
    make.add_argument("--out-dir", type=Path)
    make.set_defaults(func=cmd_make)
    prt = sub.add_parser("print", help="印刷する(--apply で実際に送る)")
    prt.add_argument("json", type=Path)
    prt.add_argument("--place", choices=PRINTERS, required=True)
    prt.add_argument("--apply", action="store_true")
    prt.set_defaults(func=cmd_print)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
