"""クリップボードの「貼り付け用タグ」(HTML)を、UTF-8・LF のテキストファイルとして保存する。

メルマガエディタの画面の「タグをコピーする」を押した直後に実行する。画面の「文字数」は
Shift_JIS(cp932)のバイト数(改行は LF の 1 バイト)で数えられているため、保存した内容の
cp932 バイト数と画面の文字数が一致するかで、取りこぼしが無いことを確認できる。

使い方:
  python save_clipboard_tag.py [--expect 20272] [--out-dir 保存先フォルダ(既定 900.ClaudeCode/000.tmp)]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_OUT_DIR = Path(r"C:\ClaudeCode\900.ClaudeCode\000.tmp")


def read_clipboard() -> str:
    ps = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard -Raw"
    r = subprocess.run(
        ["powershell", "-STA", "-NoProfile", "-Command", ps],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"クリップボードを読めません: {r.stderr[:200]}")
    return r.stdout


def normalize(text: str) -> str:
    """改行を LF に揃え、前後の空白を落とす(末尾は改行 1 つ)。"""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"


def sjis_len(text: str) -> int:
    """画面の「文字数」と同じ数え方(cp932 のバイト数・改行は LF)。"""
    return len(text.strip("\n").encode("cp932", errors="replace"))


def looks_like_tag(text: str) -> bool:
    low = text.lower()
    return "<table" in low and "</table>" in low


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect", type=int, default=None, help="画面に出ている文字数")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    a = ap.parse_args(argv)
    text = normalize(read_clipboard())
    if not looks_like_tag(text):
        print("エラー: クリップボードが貼り付け用タグ(HTML の table)ではありません。保存しません。")
        return 1
    n = sjis_len(text)
    if a.expect is not None and n != a.expect:
        print(f"エラー: 文字数が違います(画面 {a.expect} / 取得 {n})。保存しません。もう一度コピーしてください。")
        return 1
    a.out_dir.mkdir(parents=True, exist_ok=True)
    path = a.out_dir / f"merumaga_tag_{datetime.now():%Y%m%d_%H%M}.txt"  # noqa: DTZ005  (ファイル名はローカル時刻)
    path.write_text(text, encoding="utf-8", newline="\n")
    print(f"保存: {path}")
    print(f"文字数(画面と同じ数え方): {n}" + (" = 画面と一致" if a.expect == n else ""))
    print(f"UTF-8 / LF / {len(text)} 文字")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
