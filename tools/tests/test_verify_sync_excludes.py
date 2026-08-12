"""verify_sync.py の除外リストと /g-ul の robocopy //XD //XF を機械照合する.

なぜ要るか (2026-08-12 実発生):
    `/g-ul` は端末ごとの使用量 DB (`pv_usage_*.sqlite3` / `cgd_usage*.sqlite3`) を
    `//XF` でミラーから外している。ところが検証側の `verify_sync.py` は
    それを比較対象に残したままだった。結果、**push が正常に終わっても
    verify_sync が必ず exit 1 を返す**状態になり、
    「反映されていません」と言い続ける検証になった。

    verify_sync.py の該当箇所には以前から
    「合わせないと『毎回不一致』になって警告が形骸化する」と書いてあったのに
    追随を忘れた。**コメントによる注意喚起では防げなかった**ので、
    人間の記憶ではなく突合で守る。

照合の方向:
    SKILL.md に書かれた除外 (実際にミラーを動かす側) を正とし、
    verify_sync.py がそれを漏れなく含むことを要求する。
    逆向き (verify_sync だけが持つ除外) は「検証が甘い」方向なので
    警告に留めず失敗させる — 甘い検証は無い検証と同じ。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import verify_sync as V  # noqa: E402

SKILL_MD = TOOLS.parent / "skills" / "g-ul" / "SKILL.md"


def _robocopy_command() -> str:
    """SKILL.md から **実際に実行される robocopy 行**だけを取り出す (継続行を連結)。

    SKILL.md は本文でも `robocopy //MIR` に言及しているので、
    単に最初の "robocopy" から探すと**解説文を巻き込む**
    (最初にこの実装を書いたとき実際に踏み、コメント本文が除外パターンとして
     抽出されてテストが真っ赤になった)。呼び出し行の形で固定する。
    """
    lines = SKILL_MD.read_text(encoding="utf-8").splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if ln.strip().startswith('robocopy "$SRC_W"')), None)
    assert start is not None, "SKILL.md の robocopy 呼び出し行が見つからない (構成が変わった?)"

    parts = []
    for ln in lines[start:]:
        stripped = ln.strip()
        if stripped.endswith("\\"):
            parts.append(stripped[:-1])
        else:
            parts.append(stripped)
            break
    return " ".join(parts)


def _robocopy_excludes() -> tuple[set[str], set[str]]:
    """//XD と //XF に並ぶパターンを読む。"""
    tokens = re.findall(r'"([^"]+)"|(\S+)', _robocopy_command())
    values = [a or b for a, b in tokens]

    dirs: set[str] = set()
    files: set[str] = set()
    bucket: set[str] | None = None
    for value in values:
        if value == "//XD":
            bucket = dirs
        elif value == "//XF":
            bucket = files
        elif value.startswith("//") or value.startswith(">") or value.startswith("2>"):
            bucket = None                      # 別のオプションに入ったら収集をやめる
        elif bucket is not None:
            bucket.add(value)
    return dirs, files


def test_skill_md_still_declares_excludes() -> None:
    """パースが空を返したら照合が無意味になる。**先に自分自身を疑う。**"""
    dirs, files = _robocopy_excludes()
    assert len(files) >= 5, f"//XF の抽出に失敗している: {files}"
    assert "__pycache__" in dirs, f"//XD の抽出に失敗している: {dirs}"


def test_verify_sync_excludes_everything_robocopy_excludes() -> None:
    _, files = _robocopy_excludes()
    missing = sorted(files - set(V.EXCLUDE_FILES))
    assert not missing, (
        "robocopy が除外しているのに verify_sync が比較対象に残している: "
        f"{missing}\n  → これがあると push が成功しても verify_sync が必ず exit 1 になる"
    )


def test_verify_sync_dir_excludes_match() -> None:
    dirs, _ = _robocopy_excludes()
    missing = sorted(dirs - set(V.EXCLUDE_DIRS))
    assert not missing, f"//XD にあって verify_sync に無い: {missing}"


def test_verify_sync_has_no_extra_excludes() -> None:
    """逆向きの乖離。**検証だけが甘い**状態を作らない。"""
    _, files = _robocopy_excludes()
    extra = sorted(set(V.EXCLUDE_FILES) - files)
    assert not extra, (
        f"verify_sync だけが除外しているパターン: {extra}\n"
        "  → ミラーには載るのに検証がスキップするので、壊れても気づけない"
    )
