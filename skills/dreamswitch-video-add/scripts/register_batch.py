"""変換済みの .jmv/.jbm を SYMV.LST へ一括登録する。

SYMV.LST は「タブ区切り・CRLF改行」が絶対条件(崩すと本体がフリーズする実例あり)。
本スクリプトはバイト単位で読み書きし、newline="" 相当(write_bytes)で改行を保持する。

使い方:
    python register_batch.py <root> <selections.json>

<root> はプロジェクトフォルダ(例: C:\\ClaudeCode\\069.ドリームスイッチ)。
<selections.json> は {"<videoId>": {"name": "<出力名>", "cat": "stories1|stories2|song1|song2"}, ...} の辞書。
  出力名は SYMV/JMV/<name>.jmv と SYMV/JBM/<name>.jbm が両方実在することを事前チェックする
  (無ければ何も書き込まず中断する)。
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

if len(sys.argv) != 3:
    print("usage: python register_batch.py <root> <selections.json>")
    sys.exit(1)

ROOT = Path(sys.argv[1])
SEL_FILE = Path(sys.argv[2])
LST = ROOT / "SYMV" / "SYMV.LST"

import json  # noqa: E402

SELECTIONS = json.load(open(SEL_FILE, encoding="utf-8"))

# 1) 事前チェック: 登録対象の実ファイルが揃っているか
missing = []
for vid, sel in SELECTIONS.items():
    name = sel["name"]
    jmv = ROOT / "SYMV" / "JMV" / f"{name}.jmv"
    jbm = ROOT / "SYMV" / "JBM" / f"{name}.jbm"
    if not jmv.exists() or not jbm.exists():
        missing.append(name)
if missing:
    print("MISSING FILES, aborting (何も書き込んでいません):", missing)
    sys.exit(1)

# 2) カテゴリごとにグルーピング(辞書の挿入順を維持)
by_cat = {}
for vid, sel in SELECTIONS.items():
    by_cat.setdefault(sel["cat"], []).append(sel["name"])

data = LST.read_bytes()
lines = data.split(b"\r\n")

already = set()
for l in lines:  # noqa: E741
    if l.strip() and not l.startswith(b"*"):
        already.add(l.split(b"\t")[0].strip())

skipped = []
out = []
for l in lines:  # noqa: E741
    out.append(l)
    decoded = l.decode("utf-8", "ignore")
    for cat, names in by_cat.items():
        header = f"* category_{cat}.jbm"
        if decoded.startswith(header):
            for name in names:
                key = (name + ".jbm").encode()
                if key in already:
                    skipped.append(name)
                    continue
                newline = f"{name}.jbm\t\t\t{name}.jmv\t\t!ED_Sanrio.jmv  BackToMenu.jmv".encode("utf-8")
                out.append(newline)
                already.add(key)

new_data = b"\r\n".join(out)
LST.write_bytes(new_data)

registered = sum(len(v) for v in by_cat.values()) - len(skipped)
print(f"registered {registered} entries")
print("skipped (already present):", skipped)
print("new file size:", len(new_data), "bytes; CR:", new_data.count(b"\r"), "LF:", new_data.count(b"\n"))
