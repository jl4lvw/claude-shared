"""Amazon出品画像の強制アップスケール(単純リサイズ・画質補完なし)。

Amazon要件(長辺1000px以上)を下回る画像を、LANCZOSリサンプリングで単純に
引き伸ばす。AI/生成系の超解像は使わない(このプロジェクトの既定方針 —
過去の判断で精度が不安定だったため)。アスペクト比は保持する
(正方形ソースなら結果も正方形になる)。

使い方:
    python upscale_images.py --img-dir "C:/ProductMaster/images/G1737" \
        --files main sub_01 sub_02 sub_03 sub_04 sub_05 \
        --output-dir <scratchpad>/g1737_upscaled --target 1200

--files を省略すると --img-dir 直下の *.jpg(*_thumb.jpg を除く)全件を対象にする。
"""

import argparse
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from PIL import Image


def upscale_one(src: Path, out_dir: Path, target: int) -> Path:
    im = Image.open(src).convert("RGB")
    w, h = im.size
    scale = target / max(w, h)
    new_size = (round(w * scale), round(h * scale))
    im2 = im.resize(new_size, Image.LANCZOS)
    out = out_dir / f"{src.stem}_{target}.jpg"
    im2.save(out, "JPEG", quality=92)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--img-dir", required=True, help="元画像のディレクトリ")
    ap.add_argument("--files", nargs="*", default=None, help="対象ファイル名(拡張子なし)。省略時は*.jpg全件")
    ap.add_argument("--output-dir", required=True, help="出力先ディレクトリ")
    ap.add_argument("--target", type=int, default=1200, help="長辺の目標px(既定1200)")
    args = ap.parse_args()

    img_dir = Path(args.img_dir)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.files:
        srcs = [img_dir / f"{name}.jpg" for name in args.files]
    else:
        srcs = sorted(p for p in img_dir.glob("*.jpg") if not p.stem.endswith("_thumb"))

    for src in srcs:
        if not src.exists():
            print(f"SKIP (not found): {src}")
            continue
        out = upscale_one(src, out_dir, args.target)
        before = Image.open(src).size
        after = Image.open(out).size
        print(f"{src.name}: {before} -> {after}  ({out})")


if __name__ == "__main__":
    main()
