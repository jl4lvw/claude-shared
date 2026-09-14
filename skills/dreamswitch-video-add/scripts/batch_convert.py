"""ドリームスイッチ向け YouTube 動画の一括ダウンロード+変換ドライバ。

SYMV.LST への登録は行わない(名称・カテゴリ確定後に register_batch.py で別途行う)。
1本失敗しても残りは続行し、_build/batch_log.txt に記録する(再実行で失敗分だけ再試行される
— 既に .jmv/.jbm が揃っている出力名はスキップするため)。

使い方:
    python batch_convert.py <root> <candidates.json>

<root> はプロジェクトフォルダ(例: C:\\ClaudeCode\\069.ドリームスイッチ)。
  以下が存在している前提: <root>\\_build\\build_video.sh, <root>\\_build\\encode_frames.py
<candidates.json> は [{"id": "<videoId>", "url": "https://youtu.be/<id>", "proposedName": "<CamelCaseName>"}, ...] の配列。
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

if len(sys.argv) != 3:
    print("usage: python batch_convert.py <root> <candidates.json>")
    sys.exit(1)

ROOT = Path(sys.argv[1])
DATA_FILE = Path(sys.argv[2])
BUILD = ROOT / "_build"
PY = sys.executable  # このスクリプトを起動したのと同じ python.exe を使う

# deno (yt-dlp の JS チャレンジ解決に必要) を PATH に足す。winget既定パスから探す。
DENO_CANDIDATES = list(
    Path(os.environ.get("LOCALAPPDATA", "")).glob(
        "Microsoft/WinGet/Packages/DenoLand.Deno_*"
    )
)
env = os.environ.copy()
if DENO_CANDIDATES:
    env["PATH"] = str(DENO_CANDIDATES[0]) + os.pathsep + env.get("PATH", "")
env["PYTHONUTF8"] = "1"  # yt-dlp の標準出力が CP932 で化けるのを防ぐ (Windows既定コンソール対策)

LOG = BUILD / "batch_log.txt"


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run(cmd, **kw):
    return subprocess.run(cmd, env=env, check=True, **kw)


THUMB_PY = r'''
import sys, subprocess, os
from PIL import Image, ImageOps
src, dst, video = sys.argv[1], sys.argv[2], sys.argv[3]
im = None
if src and os.path.isfile(src):
    try:
        im = Image.open(src).convert('RGB')
    except Exception as e:
        print('  thumb read failed:', e)
if im is None:
    tmp = dst + '.frame.png'
    subprocess.run(['ffmpeg','-y','-v','error','-ss','5','-i',video,'-frames:v','1',tmp], check=True)
    im = Image.open(tmp).convert('RGB')
    os.remove(tmp)
im = ImageOps.fit(im, (240,45), Image.LANCZOS)
im.save(dst, 'JPEG', quality=92, subsampling=0)
print('  OK', dst, im.size)
'''


def process_one(item):
    id_ = item["id"]
    name = item["proposedName"]
    url = item["url"]
    jmv = ROOT / "SYMV" / "JMV" / f"{name}.jmv"
    jbm = ROOT / "SYMV" / "JBM" / f"{name}.jbm"

    if jmv.exists() and jbm.exists():
        log(f"SKIP {name} ({id_}): already converted")
        return True

    dl = BUILD / f"dl_{name}.mp4"
    thumb_raw = BUILD / f"dl_{name}.thumb"

    try:
        log(f"START {name} ({id_}) <- {url}")

        log("  [1/4] download video")
        # NOTE: -hwaccel cuda 等の GPU デコードは付けないこと。
        # AV1 ソースで GTX1660 世代の GPU が非対応のまま 0 フレームで
        # 失敗する実例あり(2026-09-14)。ボトルネックは次段の Pillow
        # フレームエンコード(CPU 限定)なのでデコード高速化の恩恵も薄い。
        run([PY, "-m", "yt_dlp", "--remote-components", "ejs:github",
             "--no-playlist",
             "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
             "--merge-output-format", "mp4",
             "-o", str(dl), url])

        log("  [2/4] download thumbnail")
        try:
            run([PY, "-m", "yt_dlp", "--remote-components", "ejs:github",
                 "--no-playlist", "--write-thumbnail", "--skip-download",
                 "--convert-thumbnails", "jpg",
                 "-o", str(thumb_raw), url])
        except subprocess.CalledProcessError:
            log("  thumbnail download failed, will fall back to video frame")

        thumb_src = ""
        for cand in [f"{thumb_raw}.jpg", str(thumb_raw).replace(".thumb", "") + ".thumb.jpg", str(thumb_raw)]:
            if os.path.isfile(cand):
                thumb_src = cand
                break

        log("  [3/4] build_video.sh (convert to .jmv)")
        run(["bash", str(BUILD / "build_video.sh"), str(dl), name, "92"])

        log("  [4/4] generate thumbnail .jbm")
        run([PY, "-", thumb_src, str(jbm), str(dl)], input=THUMB_PY, text=True)

        for p in BUILD.glob(f"dl_{name}.*"):
            try:
                p.unlink()
            except OSError:
                pass

        log(f"DONE {name}")
        return True

    except subprocess.CalledProcessError as e:
        log(f"FAILED {name} ({id_}): {e}")
        return False
    except Exception as e:
        log(f"FAILED {name} ({id_}): unexpected {e!r}")
        return False


def main():
    data = json.load(open(DATA_FILE, encoding="utf-8"))
    total = len(data)
    ok, ng = 0, 0
    for i, item in enumerate(data, 1):
        log(f"=== {i}/{total}: {item['proposedName']} ===")
        if process_one(item):
            ok += 1
        else:
            ng += 1
    log(f"ALL DONE. ok={ok} ng={ng} total={total}")


if __name__ == "__main__":
    main()
