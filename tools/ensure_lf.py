"""ensure_lf — テキストファイルに CR(0x0D) 等の制御バイトが無いことをバイト列で検査する.

なぜ必要か（2 回事故が起きた）:
    Workflow ツールに渡す JS スクリプトが CRLF になると、承認ダイアログが
    「script contains control characters that would be hidden in the approval
    dialog」で起動を拒否し、Lv6/7/8 が使用不能になる。
    2026-08-06 と 2026-08-11 の 2 回発生し、2 回とも原因特定に時間を溶かした。
    参照: python C:/ClaudeCode/.claude/tools/incident_log.py show INC-20260811-144157bce8d2

なぜ「テキストとして走査」では見つからないか:
    Windows の Path.read_text() は universal newlines で CR を \\n に潰して隠す。
    逆に Path.write_text() は \\n を \\r\\n に黙って変換して CRLF を生む。
    **検出も書き込みもバイト列で行う必要がある**。

    NG: open(p).read().count("\\r")            → 常に 0
    OK: open(p, "rb").read().count(b"\\r")     → 実数

使い方:
    python ensure_lf.py --check <path>...          # 検査のみ
    python ensure_lf.py --fix   <path>...          # LF へ変換 (.bak を残す)
    python ensure_lf.py --check --preset cgd-wf    # cgd の WF スクリプト 3 本
    python ensure_lf.py --fix --exit-on-change ... # CI 用: 変換したら 1 を返す

終了コード:
    0 = 問題なし（--fix で変換に成功した場合も 0）
    1 = 制御バイトを検出（--check）、または --fix --exit-on-change で変換した
    2 = 引数エラー
    3 = 修復失敗 / 読取不可 / ファイルが無い

    --fix の成功を非 0 にしていた版があったが、「変更あり」「検出」「失敗」「欠落」を
    exit 1 に多重定義していて呼び出し側が区別できなかった (2026-08-11 の Lv6 レビュー 🔴)。
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

EXIT_OK = 0
EXIT_FOUND = 1
EXIT_USAGE = 2
EXIT_ERROR = 3

# LF(0x0A) と TAB(0x09) は許容する。CR(0x0D) と DEL(0x7F) を含むその他は不可。
# TAB を許すのは既存コードのインデントを壊さないため。
_ALLOWED = {0x0A, 0x09}

# 配置が 2 通りある:
#   作業側 : <repo>/.claude/tools/ensure_lf.py  → workflows は ../skills/cgd/workflows
#   共有側 : claude-shared/tools/ensure_lf.py   → workflows は ../skills/cgd/workflows
# parents[2] 決め打ちだと共有側ミラーで存在しないパスを指すので、候補を順に試す。
_HERE = Path(__file__).resolve().parent            # .../tools
_WF_CANDIDATES = (
    _HERE.parent / "skills" / "cgd" / "workflows",                    # 共有側 & 作業側の .claude 配下
    _HERE.parents[1] / ".claude" / "skills" / "cgd" / "workflows",    # リポジトリ直下から
    Path("C:/ClaudeCode/.claude/skills/cgd/workflows"),               # 最後の砦
)


def _wf_dir() -> Path:
    for cand in _WF_CANDIDATES:
        if cand.is_dir():
            return cand
    return _WF_CANDIDATES[0]


PRESETS: dict[str, tuple[Path, ...]] = {
    "cgd-wf": tuple(
        _wf_dir() / f"cgd_lv{n}_review.js" for n in (6, 7, 8)
    ),
}


def _warn(msg: str) -> None:
    """警告・エラーは stderr へ。stdout に混ぜるとパイプ利用を壊す。"""
    print(msg, file=sys.stderr)


def scan(path: Path) -> dict[int, int]:
    """制御バイトの出現数を {byte: count} で返す。読めない場合は例外を投げる。"""
    counts: dict[int, int] = {}
    for byte in path.read_bytes():
        if (byte < 0x20 and byte not in _ALLOWED) or byte == 0x7F:
            counts[byte] = counts.get(byte, 0) + 1
    return counts


def _fmt(counts: dict[int, int]) -> str:
    names = {0x0D: "CR", 0x7F: "DEL", 0x00: "NUL", 0x0C: "FF", 0x0B: "VT"}
    return ", ".join(
        f"{names.get(b, hex(b))}({hex(b)}) x{n}" for b, n in sorted(counts.items())
    )


def _backup_path(path: Path) -> Path:
    """同一秒に 2 回走っても衝突しないバックアップ名を作る。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    candidate = path.with_name(f"{path.name}.bak_{stamp}_crlf")
    n = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak_{stamp}_crlf.{n}")
        n += 1
    return candidate


def fix(path: Path) -> tuple[int, Path | None]:
    """CRLF/CR を LF に正規化して書き戻す。(除去した CR 数, バックアップパス) を返す。

    **必ず bytes で読み書きする**。write_text() は \\n を \\r\\n に戻してしまう。
    """
    data = path.read_bytes()
    fixed = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    if fixed == data:
        return 0, None
    # 除去数はバイト長の差ではなく CR の個数で数える。
    # lone CR は CR→LF 置換で長さが変わらず、長さ差だと 0 と表示されてしまう。
    removed = data.count(b"\r") - fixed.count(b"\r")
    backup = _backup_path(path)
    shutil.copy2(path, backup)

    # **書き戻しはアトミックに行う。** 直接 write_bytes すると、途中で失敗したときに
    # 元ファイルが中途半端な状態で残る（並行して読んでいる側にも途中状態が見える）。
    # 一時ファイルへ書いてから os.replace で差し替えれば、成功か未変更かのどちらかになる。
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    try:
        tmp.write_bytes(fixed)
        os.replace(tmp, path)
    except OSError:
        # 差し替え前に失敗したら元ファイルは無傷。ゴミの一時ファイルだけ片付ける。
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return removed, backup


def resolve_targets(paths: list[str], preset: str | None) -> list[Path]:
    targets = [Path(p) for p in paths]
    if preset:
        targets += list(PRESETS[preset])
    return targets


def main() -> int:
    parser = argparse.ArgumentParser(
        description="テキストファイルに CR 等の制御バイトが無いことをバイト列で検査する"
    )
    parser.add_argument("paths", nargs="*", help="検査対象のファイル")
    parser.add_argument(
        "--preset",
        choices=sorted(PRESETS),
        help="既定の対象セットを追加する。paths と併用した場合は **両方** が対象になる（置き換えではない）",
    )
    parser.add_argument(
        "--exit-on-change",
        action="store_true",
        help="--fix で実際に変換したとき 1 を返す (CI で変更を検知したい場合)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="検査のみ (既定)")
    mode.add_argument("--fix", action="store_true", help="LF へ変換する")
    args = parser.parse_args()

    targets = resolve_targets(args.paths, args.preset)
    if not targets:
        parser.error("対象がありません (paths か --preset を指定してください)")

    found = 0    # 制御バイトを検出した件数 (--check)
    changed = 0  # 実際に変換した件数 (--fix)
    errors = 0   # 読めない・直せない・無い

    for path in targets:
        # 「ファイルではない」を一括で『見つかりません』にすると、
        # ディレクトリ指定なのかパスの綴り間違いなのか切り分けられない。
        if not path.is_file():
            try:
                exists = path.exists()
                is_dir = path.is_dir()
            except (OSError, ValueError):
                exists = is_dir = False
            if is_dir:
                _warn(f"[ensure_lf] ⚠️ ディレクトリです（ファイルを指定してください）: {path}")
            elif exists:
                _warn(f"[ensure_lf] ⚠️ 通常ファイルではありません: {path}")
            else:
                _warn(f"[ensure_lf] ⚠️ 見つかりません: {path}")
            errors += 1
            continue
        # OSError 以外（不正なパスの ValueError 等）も拾う。素通りすると
        # トレースバックで異常終了し、後続ファイルが未処理のまま打ち切られる。
        try:
            counts = scan(path)
        except (OSError, ValueError) as exc:
            _warn(f"[ensure_lf] ⚠️ 読めません: {path} ({exc})")
            errors += 1
            continue

        if not counts:
            print(f"[ensure_lf] OK   {path}")
            continue

        if not args.fix:
            found += 1
            print(f"[ensure_lf] NG   {path} — {_fmt(counts)}")
            print(f"             → 修正: python {Path(__file__).as_posix()} --fix {path}")
            continue

        # 1 ファイルの失敗で全体を traceback で落とさない (🟠 Codex+DS)。
        # 失敗しても **元ファイルは無傷**（書き戻しは一時ファイル + os.replace）。
        try:
            removed, backup = fix(path)
        except (OSError, ValueError) as exc:
            _warn(
                f"[ensure_lf] ⚠️ 修復に失敗: {path} ({exc})\n"
                f"             元ファイルは変更していません（書き戻しはアトミックです）。\n"
                f"             作成済みのバックアップがあれば {path.name}.bak_* を確認してください。"
            )
            errors += 1
            continue

        try:
            after = scan(path)
        except (OSError, ValueError) as exc:
            _warn(f"[ensure_lf] ⚠️ 修復後の再検査に失敗: {path} ({exc})")
            errors += 1
            continue

        if after:
            _warn(
                f"[ensure_lf] ⚠️ 変換後も残存: {path} — {_fmt(after)}"
                + (f" (バックアップ: {backup})" if backup else "")
            )
            errors += 1
        else:
            changed += 1
            print(f"[ensure_lf] FIX  {path} — CR x{removed} を LF へ変換 (バックアップ: {backup})")

    if errors:
        return EXIT_ERROR
    if found:
        return EXIT_FOUND
    if changed and args.exit_on_change:
        return EXIT_FOUND
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
