"""cgd_logfilter — レビュアー生ログを「agent に見せる用」に圧縮して標準出力へ流す.

なぜ必要か (2026-08-12 実測):
    codex の生ログが 1 本 7.2MB に達し、ハーネスがツール結果をファイル退避する
    ところまで来ていた (INC-20260805-f37b)。中身を解剖したところ、
    **7.2MB のうち 94.7% がたった 39 行**だった。正体は codex 側の不具合:

        ERROR codex_models_manager::manager: failed to refresh available models:
          unknown variant `max`, expected one of `none`,`minimal`,...
          ... body: {…モデル一覧 JSON 約190KB…}

    API が返す新しい reasoning レベルを手元の codex が知らずデコードに失敗し、
    **190KB の JSON 本文をまるごと stderr へ吐く**。これが 1 実行あたり十数回。
    さらに codex が /c/tmp-ai を grep すると過去ログの巨大行を再び拾って増幅する。

    codex 本体の更新でこの発生源は消える見込みだが、
    「1 行が異常に長い出力」は他のレビュアーでも起こりうるので、
    **発生源に依存しない防波堤**をここに置く。

設計:
    - **生ログ本体は一切加工しない。** 加工するのは agent が読む表示側だけ。
      監査証跡 (raw file) はフル保存のままで、collect が読むのもそちら。
    - 呼び出しは wrap() の末尾で `python cgd_logfilter.py "$out" || cat "$out"`。
      **失敗したら素の cat に落ちる**ので、このスクリプトが壊れても
      cgd の成否判定には影響しない (rc は cat より前に確定済み)。
    - 打ち切りは**末尾優先**。レビュアーの指摘は必ず出力の末尾に来るため、
      中間を落として head/tail を残す。

安全性の根拠:
    直近 8 本 (計 41.4MB) の 1500〜6000 字の行を全数目視したところ、
    該当 11 行はすべて JSON の grep ヒットと codex 内部エラーで、
    **レビュー本文は 1 行も該当しなかった**。行長 2000 字での打ち切りは
    指摘を落とさない。この 8 本は 41.4MB → 2.03MB (4.9%) になる。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _warn(msg: str) -> None:
    """警告を stderr へ **バイトで** 書く。

    素の `print(..., file=sys.stderr)` だと、子プロセスの stderr が cp932 のまま
    日本語を書き出し、呼び出し側が UTF-8 で読んでデコード例外になる
    (pytest の reader thread が PytestUnhandledThreadExceptionWarning を出して発覚)。
    モジュール読込時に `sys.stderr.reconfigure` すると取り込んだ側の stderr まで
    書き換えてしまうので、ここだけバイトで書く。
    """
    try:
        sys.stderr.buffer.write((msg + "\n").encode("utf-8", errors="replace"))
        sys.stderr.buffer.flush()
    except (AttributeError, ValueError):   # pytest 等で差し替えられている場合
        print(msg, file=sys.stderr)


def _env_int(name: str, default: int) -> int:
    """環境変数を int で読む。**不正値でも落とさない。**

    以前は `int(os.environ.get(...))` を素で書いており、`CGD_LOG_MAX_LINE=abc`
    のような値があると **import の時点で ValueError** になった。
    呼び出し側の `|| cat` で表示自体は救われるが、原因が分かりにくい失敗が増える。
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        _warn(f"cgd_logfilter: {name}={raw!r} は整数でないため既定値 {default} を使います")
        return default
    if value < 1:
        _warn(f"cgd_logfilter: {name}={value} は 1 未満のため既定値 {default} を使います")
        return default
    return value


# 1 行の上限 (文字数)。これを超えた分は捨てて件数だけ残す。
MAX_LINE = _env_int("CGD_LOG_MAX_LINE", 2000)
# 行を刈った後の総量上限 (文字数)。超えたら中間を落とす。
MAX_TOTAL = _env_int("CGD_LOG_MAX_TOTAL", 300000)
# 中間を落とすとき、先頭に残す割合。残りは末尾に回す。
HEAD_RATIO = 0.2

EXIT_OK = 0
EXIT_NO_FILE = 1
EXIT_USAGE = 2


def cap_lines(text: str, max_line: int = MAX_LINE) -> tuple[list[str], int, int]:
    """長すぎる行を切り詰める。戻り値は (行リスト, 切った行数, 捨てた文字数)。"""
    out: list[str] = []
    cut_lines = 0
    cut_chars = 0
    for line in text.split("\n"):
        if len(line) > max_line:
            dropped = len(line) - max_line
            cut_lines += 1
            cut_chars += dropped
            out.append(f"{line[:max_line]} …[{dropped}字を省略]")
        else:
            out.append(line)
    return out, cut_lines, cut_chars


def cap_total(lines: list[str], max_total: int = MAX_TOTAL) -> tuple[list[str], int]:
    """総量が上限を超えたら中間を落とす。**末尾を厚く残す** (指摘は末尾にある)。"""
    total = sum(len(x) + 1 for x in lines)
    if total <= max_total:
        return lines, 0

    head_budget = int(max_total * HEAD_RATIO)
    tail_budget = max_total - head_budget

    head: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > head_budget:
            break
        head.append(line)
        used += len(line) + 1

    rest = lines[len(head):]
    tail: list[str] = []
    used = 0
    for line in reversed(rest):
        if used + len(line) + 1 > tail_budget:
            break
        tail.append(line)
        used += len(line) + 1
    tail.reverse()

    # **末尾は必ず 1 行残す。** 上のループは「予算に入らない行」で即 break するので、
    # 最終行が tail_budget より長いと tail が空になり、
    # 指摘が出る場所そのものが消える (Lv6 で 3 者が収束して指摘した 🔴)。
    # 既定値では 1 行 2000 字 / tail_budget 24 万字なので到達しないが、
    # CGD_LOG_MAX_TOTAL を絞ると成立する。前提に頼らず構造で塞ぐ。
    if not tail and rest:
        last = rest[-1]
        tail = [last[:tail_budget] + f" …[{len(last) - tail_budget}字を省略]"
                if len(last) > tail_budget else last]

    dropped = len(lines) - len(head) - len(tail)
    marker = (
        f"\n===== [cgd_logfilter] 中間 {dropped} 行を省略 "
        f"(総量が上限 {max_total} 字を超過。指摘は末尾にあるため末尾を優先して残した) =====\n"
    )
    return head + [marker] + tail, dropped


def render(path: Path) -> str:
    """生ログを表示用に整形して返す。"""
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")

    lines, cut_lines, cut_chars = cap_lines(text)
    lines, dropped_lines = cap_total(lines)
    body = "\n".join(lines)

    if not cut_lines and not dropped_lines:
        # 何も削っていないなら注記も付けない (素の cat と同じ見え方にする)
        return body

    notice = (
        "\n===== [cgd_logfilter] この表示は要約版です =====\n"
        f"  生ログ全文  : {path.as_posix()} ({len(raw)} bytes)\n"
        f"  長い行の切詰: {cut_lines} 行 / {cut_chars} 字を省略 (1行 {MAX_LINE} 字で打切り)\n"
        f"  中間の省略  : {dropped_lines} 行\n"
        "  ※ 判定に使う終了コードはこの直後の __CGD_EXIT__ 行が正。\n"
        "     全文が要るときは上のパスを Read すること。\n"
        "================================================"
    )
    return body + notice


def main(argv: list[str]) -> int:
    # **バイナリで書く。** テキストモードの stdout は Windows で `\n` を `\r\n` に
    # 変換するので、「何も削っていないときは素の cat と 1 バイト差もない」という
    # このスクリプトの約束が守れない。pytest は `\r\n` を潰して比較していたため
    # この乖離を隠していた (Lv6 で Codex と DS が収束して指摘)。
    if len(argv) != 2:
        _warn("usage: cgd_logfilter.py <raw_log_path>")
        return EXIT_USAGE

    path = Path(argv[1])
    if not path.is_file():
        _warn(f"cgd_logfilter: not a file: {path}")
        return EXIT_NO_FILE

    try:
        out = render(path)
        # 何も削らなかったときは **素の cat と 1 バイト差もない**ようにする。
        # 末尾改行を無条件に足すと、改行で終わらない生ログで差分が出る。
        # 行頭保証は wrap() 側 (printf の先頭 \n) が持つので、ここでは足さない。
        sys.stdout.buffer.write(out.encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()
    except OSError as exc:
        _warn(f"cgd_logfilter: read failed: {exc}")
        return EXIT_NO_FILE
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
