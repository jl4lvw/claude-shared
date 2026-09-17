"""find_root_guard.py — Bash/PowerShell 経由の `find` によるルート走査を遮断する PreToolUse hook。

2026-09-17: `find / -iname "*.md" -path "*claude/projects*"` が Git Bash の `/proc`
(Windowsレジストリ全体+全プロセスを疑似ファイルとして公開するMSYS仮想FS)を踏み抜き、
Bashツールの120秒タイムアウトでバックグラウンド化された後もオーファンプロセスとして
3時間半動き続け、ハンドルを922万件消費してPC全体を無応答にした
(詳細: .claude/memory/feedback_find_root_scan_banned.md)。

AGENTS.md は以前から「find /禁止」と明記していたが文書のみで技術的強制がなく、
過去8ヶ月で少なくとも16回、複数セッションが同じ危険パターンを実行していた
(たまたま軽症で済んでいただけ)。本hookはその技術的強制を担う。

対象: `find` の最初の非オプション引数(パス)が `/`・`/c`・`/proc`・ドライブ文字ルート
(`C:` 始まりで深さ0)であるもの。narrow なパス(例: `/c/Users/user/.claude/memory`)は許可する。
ファイル名検索は本来 Glob ツールを使うべきだが、`-newer` 等 Glob にない機能で
意図的に find を使うケースは narrow なパスなら通す。

fail-open: hook自体の不具合で作業を止めない(このリポジトリ全hook共通規約)。
"""
from __future__ import annotations

import json
import re
import sys

_DANGEROUS_EXACT = {"/", "/c", "/proc"}


def _is_dangerous_token(tok: str) -> bool:
    tok = tok.strip("'\"")
    if not tok:
        return False
    normalized = tok.rstrip("/") or "/"
    low = normalized.lower()
    if low in _DANGEROUS_EXACT:
        return True
    if low.startswith("/proc"):
        return True
    # ドライブ文字ルート: C:  C:\  C:/ (深さ0)
    if re.fullmatch(r"[A-Za-z]:[\\/]?", tok):
        return True
    return False


_FIND_WORD_RE = re.compile(r"\bfind\b")

# `cat > file << 'EOF' ... EOF` のようにファイルへ書き出すヒアドキュメントは、
# その本文がシェルコマンドとして実行されるわけではない(=データ)ので走査対象から除外する。
# 本hook自身のソースをヒアドキュメントで書き込んだ際に、コメント中の説明文
# 「`find / -iname ...`」を実行コマンドと誤認して自己遮断した実例があるため必須。
# ただし `bash << 'EOF' ... EOF` のように**インタプリタへ流し込む**ヒアドキュメントは
# 本文が実際に実行されるコマンドなので除外しない — 判定は「`<<` より前に `>` による
# リダイレクトがあるか」で行う(cat/tee 等でファイルに書く形にのみ限定する簡易ヒューリスティック)。
_HEREDOC_START_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")


def _mask_file_write_heredocs(command: str) -> str:
    lines = command.split("\n")
    out: list[str] = []
    delim: str | None = None
    strip_tabs = False
    for line in lines:
        if delim is not None:
            check = line.lstrip("\t") if strip_tabs else line
            if check.strip() == delim:
                delim = None
                out.append(line)
            else:
                out.append("")  # ヒアドキュメント本文はデータなのでマスク
            continue
        m = _HEREDOC_START_RE.search(line)
        if m and ">" in line[: m.start()]:
            strip_tabs = "<<-" in line[max(0, m.start() - 1) : m.start() + 3]
            delim = m.group(2)
        out.append(line)
    return "\n".join(out)


def _scan_segment(segment: str) -> str | None:
    """1コマンドセグメント内の find 呼び出しを調べ、危険なら理由文字列を返す。"""
    stripped = segment.strip()
    if stripped.startswith("#"):
        return None  # シェルコメント行
    for m in _FIND_WORD_RE.finditer(segment):
        rest = segment[m.end():].lstrip()
        tokens = rest.split()
        for tok in tokens:
            if tok.startswith("-"):
                # 最初の "-" が出た時点で path フェーズは終了(以降は式・オプション引数)。
                # ここで完全に break しないと、-iname の引数値等を path と誤認する。
                break
            if _is_dangerous_token(tok):
                return f'"{tok}" というルート同然のパスから走査しようとしています'
            # 安全な path でも次のトークンを見続ける(find は複数パスを取れるため:
            # `find /safe /proc -iname x` の /proc を見逃さない)
    return None


def handle_hook(payload: dict) -> dict | None:
    tool = payload.get("tool_name")
    if tool not in ("Bash", "PowerShell"):
        return None
    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or "find" not in command:
        return None
    command = _mask_file_write_heredocs(command)
    # 厳密なシェル解析はしない(誤検知より見逃しの方が安全側 = fail-open の精神に合わせる)
    segments = re.split(r"&&|\|\||[;|\n]", command)
    for seg in segments:
        if "find" not in seg:
            continue
        reason = _scan_segment(seg)
        if reason:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"[find-root-guard] `find` が {reason}\n"
                        "この形は2026-09-17に実際にPC全体をハングさせた事故と同一パターンです"
                        "(Git Bashの /proc がレジストリ全体+全プロセスを疑似ファイルとして持つため、"
                        "`find /` は事実上終わらずハンドルをリークし続けます)。\n"
                        "代わりに:\n"
                        "  - ファイル名/パターン検索 → Glob ツール (pattern: \"**/対象.md\")\n"
                        "  - 内容検索 → Grep ツール\n"
                        "  - どうしても find が必要なら、探索範囲を具体的なディレクトリに"
                        "絞ってください (例: find \"/c/Users/user/.claude/memory\" -iname ...)\n"
                        "詳細: .claude/memory/feedback_find_root_scan_banned.md"
                    ),
                }
            }
    return None


def _run_hook() -> int:
    try:
        raw = sys.stdin.buffer.read()
    except OSError:
        return 0
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        out = handle_hook(payload)
    except Exception:
        return 0
    if out is not None:
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(_run_hook())
