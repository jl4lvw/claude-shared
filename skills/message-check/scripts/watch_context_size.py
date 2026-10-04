"""待ち受けセッションの文脈の大きさを測る(relay 待ち受けの開き直しの目安・2026-10-04).

guard_session.json に登録された待ち受けのセッションIDから、そのトランスクリプト
(~/.claude/projects/*/<id>.jsonl)を探し、最後の有効な usage から
「直近の応答の入力トークン数」(input + cache_read + cache_creation)を出す。
見張りに起こされるたびに、おおよそこの量を読み直す。

勧めと「測れない」の知らせは、1つのセッションにつき1回だけ出す
(通知済みの記録は <状態フォルダ>/context_notice.json)。

終了コード(待ち受け手順書 W5 の 3 で使う):
  0 = 目安未満(何も伝えない)
  4 = 目安以上・このセッションで初めて(出力を運用者に伝える)
  3 = 測れない・このセッションで初めて(出力を運用者に伝える)
  5 = 通知済み(何も伝えない)
  その他(引数の誤り等) = 何も伝えない。計測の失敗で待ち受けを止めない
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

DEFAULT_THRESHOLD = 150_000
# 新しく開いた待ち受けの文脈(実測 2026-10-04: 新規セッションの床 4.5〜9.5万、A の待ち受けは開いた直後 7.6万)
FRESH_CONTEXT = 80_000
# 新着が無い日に起こされる回数(50分ごと)と、1回あたりの API 呼び出し数
IDLE_WAKES_PER_DAY = 24 * 60 / 50
CALLS_PER_IDLE_WAKE = 2
SESSION_ID_RE = re.compile(r"^[0-9A-Za-z-]{8,64}$")
TOKEN_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def state_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", "")) / "RelayWatch"
    return Path.home() / ".relay_watch"


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def registered_session_id(name: str | None) -> str | None:
    data = _read_json(state_dir() / "guard_session.json")
    if not isinstance(data, dict):
        return None
    sessions = data.get("sessions")
    if isinstance(sessions, dict):
        if name:
            entry = sessions.get(name)
        elif len(sessions) == 1:
            # 名義ごとの登録しか無い端末で、登録が1件だけならそれを使う
            entry = next(iter(sessions.values()))
        else:
            entry = None
        if isinstance(entry, dict) and isinstance(entry.get("session_id"), str):
            return entry["session_id"]
        if name:
            return None
    sid = data.get("session_id")
    return sid if isinstance(sid, str) else None


def find_transcript(session_id: str) -> Path | None:
    hits = []
    for p in (Path.home() / ".claude" / "projects").glob(f"*/{session_id}.jsonl"):
        try:
            hits.append((p.stat().st_mtime, p))
        except OSError:
            continue
    return max(hits)[1] if hits else None


def last_context_tokens(path: Path) -> int | None:
    last = None
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"usage"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict) or d.get("type") != "assistant" or d.get("isSidechain"):
                continue
            msg = d.get("message")
            usage = msg.get("usage") if isinstance(msg, dict) else None
            if not isinstance(usage, dict):
                continue
            values = [usage.get(k, 0) for k in TOKEN_KEYS]
            # 数でない値が混じった行・合計0の行は採らない(直前の有効な値を残す)
            if not all(isinstance(v, int) and v >= 0 for v in values) or sum(values) == 0:
                continue
            last = sum(values)
    return last


def already_noticed(sid: str, kind: str) -> bool:
    data = _read_json(state_dir() / "context_notice.json")
    return isinstance(data, dict) and isinstance(data.get(sid), dict) and bool(data[sid].get(kind))


def mark_noticed(sid: str, kind: str) -> None:
    path = state_dir() / "context_notice.json"
    data = _read_json(path)
    if not isinstance(data, dict):
        data = {}
    entry = data.get(sid) if isinstance(data.get(sid), dict) else {}
    entry[kind] = True
    data[sid] = entry
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def measure(args) -> int:
    sid = args.session_id or registered_session_id(args.name)
    if not sid or not SESSION_ID_RE.match(sid):
        # 登録が無い・読めない端末では、通知済みを記録する場所(セッション)が無いので毎回黙る
        print("文脈: 測れません(待ち受けの登録が見つかりません)")
        return 5
    path = find_transcript(sid)
    tokens = last_context_tokens(path) if path else None
    if tokens is None:
        if already_noticed(sid, "unmeasurable"):
            return 5
        mark_noticed(sid, "unmeasurable")
        print("待ち受けの文脈の大きさを測れませんでした(トランスクリプトが見つからないか、形式が変わりました)。"
              "開き直しの目安は出せませんが、待ち受けはこのまま続けます")
        return 3
    man = round(tokens / 10_000, 1)
    if tokens < args.threshold:
        print(f"文脈: 約{man}万トークン(目安 {args.threshold // 10_000}万未満)")
        return 0
    if already_noticed(sid, "over"):
        return 5
    mark_noticed(sid, "over")
    extra = max(tokens - FRESH_CONTEXT, 0) * CALLS_PER_IDLE_WAKE * IDLE_WAKES_PER_DAY
    print(f"待ち受けの文脈が約{man}万トークンまで増えました(目安 {args.threshold // 10_000}万)。"
          f"このままだと、新着が無い日でも新しく開いた場合より1日あたり約{round(extra / 10_000)}万トークン多く読み直します。"
          "都合のよいときに、C:\\ClaudeCode で新しいセッションを開き(Sonnet 5.5・エフォート中・確認なし)、"
          "/mw と打ってください。新しいセッションが待ち受けを引き継ぎ、このセッションは自動で待ち受けを終えます"
          "(この案内はこのセッションでは1回だけ出します)")
    return 4


def main() -> int:
    # 既定(cp932)だと Git Bash 経由で文字化けし、待ち受けが読めない
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="relay 待ち受けセッションの文脈の大きさを測る")
    ap.add_argument("--session-id", help="省略時は guard_session.json の登録を使う")
    ap.add_argument("--name", help="名義ごとに登録している端末で、guard_session.json の sessions の名義")
    ap.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    args = ap.parse_args()
    try:
        return measure(args)
    except Exception as exc:  # 計測の失敗で待ち受けを止めない
        print(f"文脈: 測れません({type(exc).__name__})")
        return 5


if __name__ == "__main__":
    sys.exit(main())
