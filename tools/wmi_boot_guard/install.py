"""WMI 起動直後よけ(sitecustomize.py)を、システムの Python に入れる・外す・確かめる。

    python install.py status
    python install.py install     # Python312\\Lib\\sitecustomize.py へ写す(既存が別物なら止まる)
    python install.py uninstall   # 自分が入れたものだけ消す
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

MARKER = "CLAUDE-WMI-BOOT-GUARD"
SOURCE = Path(__file__).resolve().parent / "sitecustomize.py"
TARGET = Path(r"C:\Users\user\AppData\Local\Programs\Python\Python312\Lib\sitecustomize.py")


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:12]


def status(target: Path) -> str:
    if not target.exists():
        return "未導入"
    text = target.read_text(encoding="utf-8", errors="replace")
    if MARKER not in text:
        return "別の sitecustomize.py がある(この道具のものではない)"
    return "導入済み(最新)" if sha(target) == sha(SOURCE) else "導入済み(古い版)"


def install(target: Path) -> int:
    if target.exists() and MARKER not in target.read_text(encoding="utf-8", errors="replace"):
        print(f"中止: {target} は別の sitecustomize.py です。上書きしません。")
        return 2
    target.write_bytes(SOURCE.read_bytes())
    print(f"導入しました: {target} ({sha(target)})")
    return 0


def uninstall(target: Path) -> int:
    if not target.exists():
        print("未導入です。")
        return 0
    if MARKER not in target.read_text(encoding="utf-8", errors="replace"):
        print(f"中止: {target} はこの道具のものではありません。消しません。")
        return 2
    target.unlink()
    print(f"外しました: {target}")
    return 0


def main() -> int:
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=["status", "install", "uninstall"])
    ap.add_argument("--target", type=Path, default=TARGET)
    args = ap.parse_args()
    if args.action == "status":
        print(status(args.target))
        return 0
    return install(args.target) if args.action == "install" else uninstall(args.target)


if __name__ == "__main__":
    sys.exit(main())
