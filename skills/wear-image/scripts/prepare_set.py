"""Gemini ドラッグ&ドロップ用の3点セットを Downloads へ日時プレフィックス付きでコピーする。

使い方:
    python prepare_set.py <ポーズ参照.jpg> <平置き写真.jpg> <プロンプト.md>

出力例 (C:\\Users\\jl4lv\\Downloads\\):
    20260904_1650_01_G2190_02_back_pose.jpg
    20260904_1650_02_g2193_08.jpg
    20260904_1650_03_ipd26_back_orange_prompt.md
"""

import shutil
import sys
from datetime import datetime
from pathlib import Path

DOWNLOADS = Path.home() / "Downloads"


def prepare_set(files: list[Path], dest_dir: Path = DOWNLOADS, stamp: str | None = None) -> list[Path]:
    """3ファイルを同一プレフィックス + 連番で dest_dir にコピーし、出力パスを返す。"""
    if len(files) != 3:
        raise ValueError(f"3ファイル必要 (受領 {len(files)})")
    missing = [f for f in files if not f.is_file()]
    if missing:
        raise FileNotFoundError(", ".join(str(m) for m in missing))
    stamp = stamp or datetime.now().astimezone().strftime("%Y%m%d_%H%M")
    outputs: list[Path] = []
    for i, src in enumerate(files, start=1):
        dst = dest_dir / f"{stamp}_{i:02d}_{src.name}"
        shutil.copy2(src, dst)
        outputs.append(dst)
    return outputs


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if len(argv) != 4:
        print(__doc__)
        return 2
    try:
        outputs = prepare_set([Path(a) for a in argv[1:]])
    except (ValueError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}")
        return 1
    for p in outputs:
        print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
