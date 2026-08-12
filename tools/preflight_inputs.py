"""preflight_inputs — WF に渡す入力ファイルの事実を JSON で出力する.

なぜ必要か:
    cgd の Workflow スクリプトはファイルシステムを触れないため、入力の実在確認を
    subagent に頼らざるを得ない。しかし「agent に調べて JSON で返させる」設計だと、
    存在・サイズ・ゲート状態といった**事実の判定を LLM の自己申告に置く**ことになり、
    ガードとしての信頼境界が成立しない（2026-08-11 の Lv8 セルフレビューで
    Codex/DS/Qwen の 4 者が揃って 🔴 指摘）。

    そこで判定材料の生成をこのスクリプトに固定し、agent の役割を
    「このコマンドを実行して標準出力をそのまま返す」だけに縮小する。
    WF 側は生 JSON を厳格にパースして判定する。

失敗系は必ず安全側に倒す (2026-08-11 の Lv6 レビューで 🔴):
    stat も読取も try/except (OSError, ValueError) で囲み、**どこかで失敗したら
    error を必ず載せる**。各フラグの意味は次のとおりで、**独立している**:

      exists   : stat できたか
      is_file  : 通常ファイルか (stat の結果。読めたかどうかとは無関係)
      readable : **中身を最後まで読めたか**。読取に失敗したらここが false

    読取に失敗したときも is_file は true のまま返る（通常ファイルであることは
    stat で確定しているため）。「読めなかったのに通った」を防ぐのは readable の
    役割で、呼び出し側 (WF) は exists / is_file / readable / error を**すべて**見て
    halt する。どれか一つだけを見ると素通りする。

使い方:
    python preflight_inputs.py <path>...
    → {"files": [{"path","exists","is_file","readable","bytes","mtime","mtime_ns",
                  "sha256","head"[,"sha256_skipped"][,"error"]}, ...]}

    sha256_skipped=true は「上限(MAX_HASH_BYTES)を超えたのでハッシュを取らず、
    先頭だけ読んで打ち切った」の意味。このとき readable は false になる
    （最後まで読めていないため）。理由は error に入る。

終了コード: 常に 0（判定は呼び出し側が行う。ここで落とすと生 JSON が届かない）

関連インシデント: INC-20260811-1143127833d4 ほか。
    参照: python C:/ClaudeCode/.claude/tools/incident_log.py show <ID>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat as stat_mod
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

HEAD_CHARS = 200
# sha256 は巨大ファイルでも一括読みしない (read_bytes() だとメモリ枯渇 / 🔴 DS+Qwen)。
CHUNK = 1024 * 1024
# ハッシュを計算する上限。これを超えたら sha256 を諦めて理由を error に載せる
# (計算に時間がかかりすぎると agent 側の Bash がタイムアウトし、原因が見えなくなる)。
MAX_HASH_BYTES = 64 * 1024 * 1024


def _iso(ts: float) -> str:
    """ローカルタイムゾーン付きの ISO8601。分精度・TZ 無しは機械比較に耐えない (🟠 3者一致)。"""
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat()


def _blank(raw: str) -> dict:
    return {
        "path": raw,
        "exists": False,
        "is_file": False,
        "readable": False,
        "bytes": 0,
        "mtime": "",
        "mtime_ns": 0,
        "sha256": "",
        "head": "",
    }


def inspect(raw: str) -> dict:
    """1 ファイルの事実を返す。**解釈はしない**。失敗したら安全側 (false) に倒す。"""
    info = _blank(raw)

    # os.stat を直接呼ぶ。Path.exists() は ValueError を握り潰して False を返すため、
    # 「不正なパスなのか本当に無いのか」を区別できない。
    try:
        st = os.stat(raw)
    except FileNotFoundError:
        return info
    except (OSError, ValueError) as exc:
        info["error"] = f"stat 失敗: {exc}"
        return info

    info["exists"] = True
    info["is_file"] = stat_mod.S_ISREG(st.st_mode)
    # **ここで必ず入れる。** 以前は「読取中にサイズが変化した」異常時にしか
    # 代入しておらず、正常なファイルほど _blank() の 0 のまま返っていた。
    # 呼び出し側(cgd_lv8_review.js)は bytes <= 0 で入力不正と判断するため、
    # **健全な入力が必ず弾かれて Lv8 が一切実行できない**状態だった。
    info["bytes"] = st.st_size
    info["mtime"] = _iso(st.st_mtime)
    info["mtime_ns"] = st.st_mtime_ns
    if not info["is_file"]:
        return info

    # 巨大ファイルは sha256 を諦める。ハッシュ計算に何分もかかると agent 側の Bash が
    # タイムアウトし、WF には「preflight_unparsable」としか見えなくなる。
    # レビュー入力は通常 1MB 未満なので、上限を超えたら理由を明示してスキップする。
    if st.st_size > MAX_HASH_BYTES:
        info["sha256_skipped"] = True
        info["error"] = (
            f"サイズが上限 {MAX_HASH_BYTES} バイトを超えるため sha256 を計算していません "
            f"({st.st_size} バイト)"
        )

    try:
        digest = hashlib.sha256()
        head_raw = b""
        read_bytes = 0
        with open(raw, "rb") as fh:
            while True:
                chunk = fh.read(CHUNK)
                if not chunk:
                    break
                read_bytes += len(chunk)
                if not info.get("sha256_skipped"):
                    digest.update(chunk)
                if len(head_raw) < HEAD_CHARS * 4:  # UTF-8 は最大 4 バイト/文字
                    head_raw += chunk[: HEAD_CHARS * 4 - len(head_raw)]
                if info.get("sha256_skipped") and len(head_raw) >= HEAD_CHARS * 4:
                    break  # ハッシュを取らないなら全部読む意味がない
    except (OSError, ValueError) as exc:
        # 読めなかったのに「通常ファイルとして存在する」で通すとガードが素通りする。
        info["readable"] = False
        info["sha256"] = ""
        info["head"] = ""
        info["error"] = f"読取失敗: {exc}"
        return info

    # readable は「最後まで読めたか」。上限超過で head だけ読んで break した場合は
    # 最後まで読めていないので **true にしない**（docstring の定義と矛盾させない）。
    # 呼び出し側は readable!==true で halt するので、巨大ファイルは明示的に弾かれる。
    info["readable"] = not info.get("sha256_skipped", False)
    if not info.get("sha256_skipped"):
        info["sha256"] = digest.hexdigest()
        # stat のサイズと実際に読めた量が食い違うなら、読んでいる最中に差し替わっている。
        # bytes(st_size 由来) と sha256(読み取り由来) を無検査で並べると、
        # **実在しない版の「事実」を合成して**返してしまう。
        if read_bytes != st.st_size:
            info["error"] = (
                f"読取中にサイズが変化しました (stat={st.st_size} / 実読={read_bytes})。"
                " 事実が一貫しないため信用しないでください"
            )
            info["bytes"] = read_bytes
        else:
            # サイズ差だけでは「同じ長さの別内容に差し替えられた」場合を検出できない。
            # 読み終えた後にもう一度 stat して mtime_ns まで一致するか確かめる。
            try:
                st2 = os.stat(raw)
            except (OSError, ValueError) as exc:
                info["error"] = f"読取後の再確認に失敗: {exc}"
            else:
                if (st2.st_size, st2.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
                    info["error"] = (
                        "読取中にファイルが差し替わりました"
                        f" (mtime {st.st_mtime_ns} → {st2.st_mtime_ns})。"
                        " sha256 はどの版のものか保証できません"
                    )
    # CP932 やバイナリの中身を U+FFFD で潰さない。復元可能な形で残す (🟡 Codex+DS)。
    info["head"] = head_raw.decode("utf-8", errors="backslashreplace")[:HEAD_CHARS]
    return info


def main() -> int:
    parser = argparse.ArgumentParser(description="入力ファイルの事実を JSON で出力する")
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args()

    files = []
    for raw in args.paths:
        try:
            files.append(inspect(raw))
        except Exception as exc:  # noqa: BLE001
            # 想定外でも JSON を必ず出す。ここで落ちると WF 側は生出力を得られず、
            # 「Preflight が壊れた」以上の情報が残らない。
            info = _blank(raw)
            info["error"] = f"想定外の例外: {exc.__class__.__name__}: {exc}"
            files.append(info)
            traceback.print_exc(file=sys.stderr)

    print(json.dumps({"files": files}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
