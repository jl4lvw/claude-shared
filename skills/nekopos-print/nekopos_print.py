"""ネコポス送り状PDFを [倉庫3F]ネコポス へ印刷する（マスク除去→左1mm→実寸→結果確認）。

使い方: python nekopos_print.py <PDF> [--dry-run]
- B2の送り状PDFには「$Mask…$」というフォームが付いており、Acrobat以外(Sumatra等)では
  紙面が白く覆われて**白紙で印刷される**。印刷前にそれを取り除く。
- 位置は 047 の neko6 プロファイルと同じ（左へ1mm・縦ずれ0・100% noscale）。
- 印刷前に画面相当の描画でインク量を検査し、白紙なら印刷しない。
- 印刷後は Windows の印刷ログ(ID 307)で「印刷されました・ページ数」を確認して表示する。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import fitz  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "047.GoQ送り状再印刷"))
from server.pdf_shift import shift_pdf  # noqa: E402

PRINTER_SUFFIX = "ネコポス"  # Windows名 [倉庫3F]ネコポス
OFFSET_MM = (-1.0, 0.0)
SUMATRA = Path(os.environ.get("LOCALAPPDATA", "")) / "SumatraPDF" / "SumatraPDF.exe"


def strip_masks(src: Path, dst: Path) -> int:
    doc = fitz.open(src)
    n = 0
    for page in doc:
        for w in list(page.widgets() or []):
            page.delete_widget(w)
            n += 1
        for a in list(page.annots() or []):
            page.delete_annot(a)
    doc.save(dst, garbage=3, deflate=True)
    return n


def ink_per_page(pdf: Path) -> list[int]:
    out = []
    for page in fitz.open(pdf):
        s = page.get_pixmap(dpi=60).samples
        out.append(sum(1 for k in range(0, len(s), 3) if s[k] < 128))
    return out


def printer_name() -> str:
    ps = ("(Get-Printer | Where-Object { $_.Name -like '*%s' } | Select-Object -First 1).Name"
          % PRINTER_SUFFIX)
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + ps],
                       capture_output=True, text=True, encoding="utf-8")
    name = r.stdout.strip()
    if not name:
        raise SystemExit("プリンター(*ネコポス)が見つかりません")
    return name


def print_log_since(t0: float, name: str) -> str:
    ps = ("Get-WinEvent -LogName 'Microsoft-Windows-PrintService/Operational' -MaxEvents 30 | "
          "Where-Object { $_.Id -eq 307 -and $_.TimeCreated -ge [DateTime]::Now.AddSeconds(-%d) } | "
          "Select-Object -First 1 -ExpandProperty Message" % int(time.time() - t0 + 5))
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + ps],
                       capture_output=True, text=True, encoding="utf-8")
    return re.sub(r"\s+", " ", r.stdout).strip()


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    src = Path(args[0])
    work = Path(tempfile.mkdtemp(prefix="nekopos_"))
    clean, shifted = work / "nomask.pdf", work / "print.pdf"
    n = strip_masks(src, clean)
    info = shift_pdf(clean, shifted, OFFSET_MM[0], OFFSET_MM[1])
    ink = ink_per_page(shifted)
    print(f"マスク除去 {n} 件 / ページ {info['pages']} / 描画インク {ink} / 警告 {info['warnings']}")
    if min(ink) < 1000:
        print("白紙の疑い: 印刷しません")
        return 1
    if "--dry-run" in sys.argv:
        print("dry-run: 印刷せず終了", shifted)
        return 0
    name = printer_name()
    t0 = time.time()
    subprocess.run([str(SUMATRA), "-print-to", name, "-print-settings", "noscale", "-silent",
                    str(shifted)], capture_output=True)
    time.sleep(8)
    log = print_log_since(t0, name)
    print(log or "印刷ログが見つかりません(要確認)")
    return 0 if "印刷されました" in log else 1


if __name__ == "__main__":
    raise SystemExit(main())
