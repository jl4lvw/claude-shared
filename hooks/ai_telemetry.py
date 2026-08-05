"""PostToolUse(Bash) hook: 外部 AI CLI 呼び出しの実測値を自動記録する.

背景 (2026-08-05):
  「Codex 1 回で 8 万トークン」「単一出力 1.2MB」という数字は、たまたま人が
  数えたから判明した。人力前提だと次は記録が残らない。呼び出しのたびに
  hook が 1 行追記しておけば、後日「専用セッションでまとめて解析する」際に
  異常回を機械的に拾える。

記録対象: codex exec / deepseek_coder.py / qwen_advisor.py / gemini_advisor.py
記録先  : <project>/.claude/incidents/telemetry.jsonl (JSONL・append-only)
          端末ローカル (量が多いので .gitignore 対象)。人が書く事象台帳は
          incidents.jsonl (incident_log.py) 側で、そちらは共有する。

設計方針:
- 常に fail-open (exit 0)。計測の失敗で本流を止めない。
- 出力本文は保存しない (サイズと抽出値のみ)。生ログは既にハーネス側に残る。
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

_PROJECT = Path(os.environ.get("CLAUDE_PROJECT_DIR", r"C:/ClaudeCode"))
LOG_PATH = Path(
    os.environ.get("AI_TELEMETRY_PATH", str(_PROJECT / ".claude" / "incidents" / "telemetry.jsonl"))
)

# コマンド文字列 → ツール名。上から順に最初に当たったものを採用する。
# 「コマンドとして起動される位置」に限定する (行頭 / `&&` `||` `;` `|` の直後 /
# 環境変数代入の直後)。単に文字列として含むだけのコマンドを実行として数えないため。
def _cmd_pos(tool_re: str) -> re.Pattern[str]:
    return re.compile(r"(?:^|[\n;|&])\s*(?:\w+=[^\s]*\s+)*" + tool_re, re.MULTILINE)


_TOOL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("codex", _cmd_pos(r"codex\s+exec\b")),
    ("deepseek", re.compile(r"deepseek_coder\.py")),
    ("qwen", re.compile(r"qwen_advisor\.py")),
    ("gemini", re.compile(r"gemini_advisor\.py")),
)

_EFFORT_RE = re.compile(r"model_reasoning_effort=[\"']?(low|medium|high)[\"']?")
_ROLE_RE = re.compile(r"--role\s+(\w+)")
# codex の出力末尾は "tokens used" の次行に "51,264" 形式 (実測)。": N" 形式も許容。
_TOKENS_SAMELINE_RE = re.compile(r"^tokens used[:\s]+([\d,]+)\s*$", re.MULTILINE)
_TOKENS_NEXTLINE_RE = re.compile(r"^tokens used\s*$\r?\n\s*([\d,]+)", re.MULTILINE)
_USAGE_RE = re.compile(r"^\[(?:DS|Qwen|Gemini) Usage\].*$", re.MULTILINE)
_INPUT_PATH_RE = re.compile(r"[\"']?((?:[A-Za-z]:)?[/\\][^\"'\s]*\.txt)[\"']?")
_TRUNCATED_RE = re.compile(r"Output too large|Full output saved to", re.IGNORECASE)

# 異常判定のしきい値 (後から解析するときの当たりをつける用。厳密さは不要)
TOKENS_HIGH = 80_000
OUTPUT_HUGE_BYTES = 200_000


def _detect_tool(command: str) -> str | None:
    for name, pattern in _TOOL_PATTERNS:
        if pattern.search(command):
            return name
    return None


def _as_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, dict)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return ""
    return ""


def _split_response(response: object) -> tuple[str, str, bool]:
    """tool_response から (stdout, stderr, interrupted) を取り出す。形式が揺れても壊れない。"""
    if isinstance(response, dict):
        return (
            _as_text(response.get("stdout")),
            _as_text(response.get("stderr")),
            bool(response.get("interrupted")),
        )
    return (_as_text(response), "", False)


def _extract_tokens(text: str) -> int | None:
    """codex の 'tokens used' を拾う。複数回出る (ストリーミング) ので最後を採る。"""
    matches = _TOKENS_SAMELINE_RE.findall(text) or _TOKENS_NEXTLINE_RE.findall(text)
    if not matches:
        return None
    try:
        return int(str(matches[-1]).replace(",", ""))
    except ValueError:
        return None


def _input_file_bytes(command: str) -> tuple[str | None, int | None]:
    """コマンド中の .txt 入力ファイルのうち、実在する最初のもののサイズを返す。"""
    for raw in _INPUT_PATH_RE.findall(command):
        try:
            p = Path(raw)
            if p.is_file():
                return str(p), p.stat().st_size
        except OSError:
            continue
    return None, None


def build_record(payload: dict) -> dict | None:
    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str):
        return None
    tool = _detect_tool(command)
    if tool is None:
        return None

    stdout, stderr, interrupted = _split_response(payload.get("tool_response"))
    combined = f"{stdout}\n{stderr}"

    out_bytes = len(stdout.encode("utf-8", errors="replace"))
    tokens = _extract_tokens(combined) if tool == "codex" else None
    input_path, input_bytes = _input_file_bytes(command)

    effort = _EFFORT_RE.search(command)
    role = _ROLE_RE.search(command)
    usage = _USAGE_RE.search(stderr) or _USAGE_RE.search(stdout)

    flags: list[str] = []
    if tokens is not None and tokens >= TOKENS_HIGH:
        flags.append("tokens_high")
    if out_bytes >= OUTPUT_HUGE_BYTES:
        flags.append("output_huge")
    if out_bytes == 0:
        flags.append("empty_output")
    if interrupted:
        flags.append("interrupted")
    if _TRUNCATED_RE.search(combined):
        flags.append("harness_truncated")

    session = str(payload.get("session_id") or "")
    return {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tool": tool,
        "effort": effort.group(1) if effort else None,
        "role": role.group(1) if role else None,
        "via_workflow": "CGD_WF_RUN=1" in command,
        "input_path": input_path,
        "input_bytes": input_bytes,
        "out_bytes": out_bytes,
        "tokens_used": tokens,
        "usage_line": usage.group(0).strip() if usage else None,
        "flags": flags,
        "session": session[:8],
        "cwd": payload.get("cwd"),
    }


def append_record(record: dict, log_path: Path = LOG_PATH) -> bool:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return 0
    try:
        record = build_record(payload)
        if record is not None:
            append_record(record)
    except Exception:  # noqa: BLE001 — 計測失敗で本流を止めない
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
