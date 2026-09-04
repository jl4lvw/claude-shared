"""HQ 状態ボード書込フック — 各セッションの「いま何をしているか / 何で止まっているか」を
C:/ClaudeCode/.hq/board/<session_id>.json へ 1 セッション 1 ファイルで書く。

背景 (2026-09-04):
  複数セッションを並行運用すると「どこが確認待ちで止まっているか」が判らなくなる。
  司令塔セッション (/hq) がトランスクリプトを読み回すのはトークンを食うので、
  各セッションのフックが軽量な状態ファイルを書き、司令塔はそれを読むだけにする。

対応イベントと状態遷移:
  SessionStart            -> idle (起動直後は入力待ち)
  UserPromptSubmit        -> running (依頼文の先頭を記録)
  PreToolUse              -> pending_tool を記録 (AskUserQuestion なら waiting_question)
  PermissionRequest       -> waiting_permission
  Notification            -> permission_prompt: waiting_permission / idle_prompt: idle 印
  PostToolUse             -> pending_tool を解除。待ち状態なら running へ戻す
  Stop                    -> 末尾が質問なら waiting_answer、それ以外は done
  SessionEnd              -> ended

設計規約 (ctx_ledger と同じ):
  - stdin は bytes で読み UTF-8 decode (cp932 破損対策)。失敗は必ず L.log() に残す
  - 本文抜粋は L.redact() で伏字化してから書く (PIN / パスワード / API キー対策)
  - どの経路でも exit 0。フックがセッションを止めてはならない
  - PermissionRequest では **何も出力しない** (= 既定の確認ダイアログに任せる)。
    司令塔が代理承認する経路は作らない
  - トランスクリプトは末尾の部分読みだけ (18MB 級のファイルが実在する)

書込先は環境変数 CLAUDE_HQ_DIR で上書き可能 (既定 C:/ClaudeCode/.hq)。
Git 管理外・PC 固有 (フックは g-ul で他 PC に同期されるため、ボードは混ぜない)。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001, S110  # pragma: no cover
    pass

import ctx_ledger as L

L._SRC = "hq_board"

HQ_DIR = Path(os.environ.get("CLAUDE_HQ_DIR", r"C:/ClaudeCode/.hq"))
BOARD_DIR = HQ_DIR / "board"

HEAD_PROMPT = 120
HEAD_ASSISTANT = 300
HEAD_TOOL = 160
TAIL_BYTES = 256 * 1024
EVENT_LOG_KEEP = 8

# Stop 時に「ユーザーへの質問で終わっているか」を判定する語。末尾 300 字に対して見る。
_QUESTION_RE = re.compile(
    r"(\?|？|番号で|教えてください|しますか|ますか|ませんか|でしょうか|どちら|どれに|選んでください|"
    r"よろしいですか|確認してください|確認をお願い|ご指示|指示をください|承認|進めてよい|"
    r"Which|Should I|Do you want|Confirm)"
)
_NUMBERED_OPTIONS_RE = re.compile(r"(^|\n)\s*1[.．、)]\s*\S.*\n\s*2[.．、)]\s*\S", re.DOTALL)


def _now() -> str:
    """ローカル時刻 (tz 付き ISO)。CLI 側も tz 付きで比較する。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _head(text: Any, n: int) -> str:
    if not isinstance(text, str):
        return ""
    t = " ".join(text.split())
    t = L.redact(t)
    return t[:n] + ("..." if len(t) > n else "")


def _state_path(sid: str) -> Path:
    return BOARD_DIR / f"{sid}.json"


def _load(sid: str) -> dict:
    p = _state_path(sid)
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
            L.log(f"state not a dict: {p}")
    except (OSError, ValueError) as exc:
        L.log(f"state load failed {p}: {type(exc).__name__}: {exc}")
    return {"session_id": sid, "created_at": _now(), "status": "idle", "since": _now(), "events": {}}


def _save(sid: str, st: dict) -> bool:
    p = _state_path(sid)
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    try:
        BOARD_DIR.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
        return True
    except OSError as exc:
        L.log(f"state save failed {p}: {type(exc).__name__}: {exc}")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _tail_lines(path: Path, nbytes: int = TAIL_BYTES) -> list[str]:
    """ファイル末尾 nbytes だけを読んで行に分ける (先頭の欠けた行は捨てる)。"""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > nbytes:
                f.seek(size - nbytes)
            raw = f.read()
    except OSError as exc:
        L.log(f"tail read failed {path}: {type(exc).__name__}: {exc}")
        return []
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if size > nbytes and lines:
        lines = lines[1:]
    return lines


def _transcript_info(payload: dict) -> dict:
    """トランスクリプト末尾から custom-title / last-prompt / 最終 assistant 本文を拾う。"""
    out: dict[str, str] = {}
    tp = payload.get("transcript_path")
    if not isinstance(tp, str) or not tp:
        return out
    path = Path(tp)
    if not path.exists():
        return out
    last_text: list[str] | None = None
    for line in reversed(_tail_lines(path)):
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        t = e.get("type")
        if t == "custom-title" and "title" not in out:
            ct = e.get("customTitle")
            if isinstance(ct, str) and ct.strip():
                out["title"] = ct.strip()
        elif t == "last-prompt" and "last_prompt" not in out:
            lp = e.get("lastPrompt")
            if isinstance(lp, str) and lp.strip():
                out["last_prompt"] = lp
        elif t == "assistant" and last_text is None:
            msg = e.get("message") if isinstance(e.get("message"), dict) else {}
            content = msg.get("content")
            parts: list[str] = []
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
                        parts.append(b["text"])
            if parts:
                last_text = parts
        if "title" in out and "last_prompt" in out and last_text is not None:
            break
    if last_text:
        out["last_assistant"] = "\n".join(last_text)
    return out


def _looks_like_question(text: str) -> bool:
    tail = text.strip()[-300:]
    if _QUESTION_RE.search(tail):
        return True
    return bool(_NUMBERED_OPTIONS_RE.search(text.strip()[-600:]))


def _tool_summary(payload: dict) -> dict:
    name = str(payload.get("tool_name") or "?")
    ti = payload.get("tool_input")
    detail = ""
    if isinstance(ti, dict):
        for key in ("command", "file_path", "url", "pattern", "message", "description"):
            v = ti.get(key)
            if isinstance(v, str) and v.strip():
                detail = v
                break
        if not detail:
            detail = json.dumps(ti, ensure_ascii=False)
    return {
        "name": name,
        "detail": _head(detail, HEAD_TOOL),
        "tool_use_id": payload.get("tool_use_id"),
        "at": _now(),
    }


def _question_text(payload: dict) -> str:
    ti = payload.get("tool_input")
    qs: list[str] = []
    if isinstance(ti, dict):
        for q in ti.get("questions") or []:
            if isinstance(q, dict) and isinstance(q.get("question"), str):
                qs.append(q["question"])
    return _head(" / ".join(qs) if qs else json.dumps(ti, ensure_ascii=False), HEAD_ASSISTANT)


def _set(st: dict, status: str, reason: str = "") -> None:
    if st.get("status") != status:
        st["since"] = _now()
    st["status"] = status
    st["reason"] = reason


# 中継API(041)への送信は「状態が変わったとき」と「ハートビート相当 (SessionStart/Stop/SessionEnd)」だけ。
# ツール実行のたびには送らない (フックの体感速度を守る)。
_PUSH_EVENTS = ("SessionStart", "Stop", "SessionEnd")


def _should_push(ev: str, prev_status: str | None, st: dict) -> bool:
    if os.environ.get("CLAUDE_HQ_NO_PUSH", "").strip().lower() in ("1", "true", "yes"):
        return False
    return ev in _PUSH_EVENTS or st.get("status") != prev_status


def _spawn_push(sid: str, end: bool) -> None:
    """hq_push.py を切り離した別プロセスで起動する (フックは待たない)。"""
    cmd = [sys.executable, str(Path(__file__).resolve().parent / "hq_push.py"), sid]
    if end:
        cmd.append("--end")
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
        )
    except OSError as exc:
        L.log(f"spawn push failed: {type(exc).__name__}: {exc}")


def handle(payload: dict) -> None:
    ev = str(payload.get("hook_event_name") or "")
    sid = L.session_key(payload, allow_env=False)
    if sid is None:
        L.log(f"{ev}: no session identity; keys={sorted(payload.keys())}")
        return
    st = _load(sid)
    prev_status = st.get("status")
    st["session_id"] = sid
    st["updated_at"] = _now()
    st["last_event"] = ev
    if isinstance(payload.get("cwd"), str):
        st["cwd"] = payload["cwd"]
    if isinstance(payload.get("permission_mode"), str):
        st["permission_mode"] = payload["permission_mode"]
    events = st.setdefault("events", {})
    events[ev] = int(events.get(ev, 0)) + 1
    log = st.setdefault("event_log", [])
    log.append(f"{_now()} {ev}" + (f":{payload.get('notification_type')}" if ev == "Notification" else ""))
    del log[:-EVENT_LOG_KEEP]

    # UserPromptSubmit の本文キーは実環境では "prompt" (r_consume.py と同じ)。
    # ドキュメント表記の "user_prompt" も併せて受ける
    user_prompt = payload.get("prompt") or payload.get("user_prompt")

    # タイトルはアプリが最初の応答後に付けるため、未取得の間はどのイベントでも拾いに行く
    if ev in ("SessionStart", "Stop", "UserPromptSubmit") or not st.get("title"):
        info = _transcript_info(payload)
        if info.get("title"):
            st["title"] = info["title"]
        if info.get("last_prompt") and not user_prompt:
            st["last_prompt"] = _head(info["last_prompt"], HEAD_PROMPT)
        if ev == "Stop" and info.get("last_assistant") and not payload.get("last_assistant_message"):
            st["_tx_last_assistant"] = info["last_assistant"]

    if ev == "SessionStart":
        st["started_at"] = st.get("started_at") or _now()
        st["start_reason"] = payload.get("reason") or payload.get("source")
        st.pop("ended_at", None)
        _set(st, "idle", "起動/再開")
    elif ev == "UserPromptSubmit":
        if isinstance(user_prompt, str) and user_prompt.strip():
            st["last_prompt"] = _head(user_prompt, HEAD_PROMPT)
        st["pending_tool"] = None
        st["question"] = None
        st["idle_notified"] = False
        _set(st, "running", "依頼受付")
    elif ev == "PreToolUse":
        tool = _tool_summary(payload)
        if tool["name"] == "AskUserQuestion":
            st["question"] = _question_text(payload)
            _set(st, "waiting_question", "AskUserQuestion")
        else:
            st["pending_tool"] = tool
            if st.get("status") in ("idle", "done", "waiting_answer"):
                _set(st, "running", "ツール実行")
    elif ev == "PermissionRequest":
        tool = _tool_summary(payload)
        st["pending_tool"] = tool
        # デスクトップアプリでは AskUserQuestion も PermissionRequest を経由する (2026-09-04 実測)。
        # 選択肢ダイアログは「質問待ち」のまま保つ (許可待ちに格下げしない)
        if tool["name"] == "AskUserQuestion":
            st["question"] = st.get("question") or _question_text(payload)
            _set(st, "waiting_question", "AskUserQuestion")
        else:
            _set(st, "waiting_permission", "PermissionRequest")
    elif ev == "Notification":
        nt = str(payload.get("notification_type") or "")
        st["last_notification"] = {"type": nt, "text": _head(payload.get("notification_text"), HEAD_TOOL), "at": _now()}
        if nt == "permission_prompt" and st.get("status") != "waiting_question":
            _set(st, "waiting_permission", "permission_prompt")
        elif nt == "idle_prompt":
            st["idle_notified"] = True
        elif nt == "agent_needs_input":
            _set(st, "waiting_answer", "agent_needs_input")
    elif ev == "PostToolUse":
        st["pending_tool"] = None
        if st.get("status") in ("waiting_permission", "waiting_question"):
            st["question"] = None
            _set(st, "running", "ツール完了")
    elif ev == "Stop":
        text = payload.get("last_assistant_message")
        if not isinstance(text, str) or not text.strip():
            text = st.pop("_tx_last_assistant", "") or ""
            st["last_assistant_source"] = "transcript"
        else:
            st.pop("_tx_last_assistant", None)
            st["last_assistant_source"] = "payload"
        st["last_assistant"] = _head(text, HEAD_ASSISTANT)
        st["last_assistant_tail"] = _head(text.strip()[-HEAD_ASSISTANT:], HEAD_ASSISTANT)
        st["pending_tool"] = None
        calls = payload.get("tool_calls_in_turn")
        if isinstance(calls, list):
            st["tool_calls_last_turn"] = len(calls)
        if text and _looks_like_question(text):
            _set(st, "waiting_answer", "応答が質問で終了")
        else:
            _set(st, "done", "応答完了")
    elif ev == "SessionEnd":
        st["ended_at"] = _now()
        st["end_reason"] = payload.get("reason")
        st["pending_tool"] = None
        _set(st, "ended", str(payload.get("reason") or ""))
    else:
        L.log(f"unhandled event {ev!r}")

    if _save(sid, st) and _should_push(ev, prev_status, st):
        _spawn_push(sid, end=(ev == "SessionEnd"))


def main() -> int:
    payload = L.read_payload("hq_board")
    if payload is None:
        return 0
    try:
        handle(payload)
    except Exception as exc:  # noqa: BLE001  # フックは決してセッションを止めない
        L.log(f"handle failed: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
