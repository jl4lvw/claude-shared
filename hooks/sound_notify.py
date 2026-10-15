"""対応待ち音声通知フック — Claude Code が確認・選択・権限許可待ちで止まったとき、
効果音 + 英語の読み上げを鳴らし、設定があればスマホ(LINE WORKS)にも知らせる (2026-10-10)。

対象イベント (settings.local.json に登録):
  PermissionRequest            -> 「権限の確認待ちです」
  Notification                 -> permission_prompt は権限 / idle_prompt は入力待ち
  PreToolUse(AskUserQuestion)  -> 「選択肢の確認待ちです」
  Stop                         -> 質問・依頼で終わったときだけ「返信待ちです」

設計 (Lv3 レビュー ×5 の指摘を反映した 2026-10-10 版):
  - 読み上げは固定文言のみ。イベント本文は読み上げず、ログにも残さない
  - 重複の判定は「同じセッションの同じ種類が DEBOUNCE_SEC 秒以内」。別セッション・別種類は落とさない
    (AskUserQuestion は PreToolUse / PermissionRequest / Notification の 3 経路で発火するが、
     どれも同じ「選択肢」に分類されるので同じキーで 1 回に畳まれる)
  - 判定と記録は notify_common.claim() で排他・原子的に行う
  - 音とスマホは独立。片方の失敗(powershell 不在など)でもう片方を止めない
  - 音は別プロセス(PowerShell)で鳴らし、フック自体は即 exit 0。子の stderr は notify_hook.log へ

環境変数:
  CLAUDE_NOTIFY=0         すべて止める
  CLAUDE_SOUND_NOTIFY=0   PC の音だけ止める (スマホは送る)
  CLAUDE_PHONE_NOTIFY=0   スマホだけ止める
  CLAUDE_SOUND_SPEAK=0    読み上げだけ止める (効果音は鳴る)
スマホの有効/無効は relay_local/phone_notify.json (無い or enabled:false で無効)。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from notify_common import LOG, SID_RE, claim, log, phone_enabled, powershell_exe  # noqa: E402

DEBOUNCE_SEC = 15.0
STAMP = Path(tempfile.gettempdir()) / "claude_sound_notify.stamp.json"
EVENT_LOG = Path(tempfile.gettempdir()) / "claude_sound_notify.events"
EVENT_LOG_MAX = 20_000

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200  # DETACHED_PROCESS は音声が出なくなるので使わない

PERMISSION = "権限の確認待ちです"
QUESTION = "選択肢の確認待ちです"
REPLY = "返信待ちです"
IDLE = "入力待ちです"
CONFIRM = "確認待ちです"

CHIME_Q = "Windows Notify System Generic"
CHIME_P = "Windows Exclamation"

# PowerShell に渡すスクリプト。波括弧を含むので str.format は使わず、置換記号を replace する
PS_TEMPLATE = (
    "$w = 'C:/Windows/Media/__SOUND__.wav'; "
    "if (Test-Path -LiteralPath $w) { (New-Object System.Media.SoundPlayer $w).PlaySync() } "
    "else { [System.Media.SystemSounds]::Exclamation.Play(); Start-Sleep -Milliseconds 800 }; "
    "__SPEAK__"
)
PS_SPEAK = (
    "Add-Type -AssemblyName System.Speech; "
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
    "try { $s.SelectVoice('Microsoft Zira Desktop') } "
    "catch { [Console]::Error.WriteLine('voice fallback: Zira not installed') }; "
    "$s.Speak('__TEXT__')"
)

# 読み上げは英語 (2026-10-10 ユーザー指定)。スマホ通知は日本語の文言のまま
SPEECH_EN = {
    PERMISSION: "Permission needed",
    QUESTION: "Question waiting",
    IDLE: "Waiting for input",
    CONFIRM: "Confirmation needed",
    REPLY: "Reply needed",
}

# 041 の POST /notify/phone に渡す種類コード (文言はサーバーが決める)
PHONE_KIND = {
    PERMISSION: "permission",
    QUESTION: "question",
    REPLY: "reply",
    IDLE: "idle",
    CONFIRM: "confirm",
}

PHONE_SCRIPT = Path(__file__).resolve().with_name("lw_phone_notify.py")
BOARD_DIR = Path(os.environ.get("CLAUDE_HQ_DIR", r"C:/ClaudeCode/.hq")) / "board"


def read_event() -> dict:
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        ev = json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}
    return ev if isinstance(ev, dict) else {}


# --- Stop: 質問・依頼で終わったか ---------------------------------------------------
# 末尾の文だけを見る。本文中の「承認」や「?」では反応しない (hq_board の緩い判定は流用しない)
_CLOSERS = r"[」』）)\]\"'*_`\s。．.!！]*"
_JP_REQUEST_RE = re.compile(
    r"(?:[?？]|ください|下さい|お願いします|お願いいたします|お願い致します|ですか|ますか|でしょうか|ませんか"
    r"|いかがでしょうか|よろしいですか|ほしいです|欲しいです)" + _CLOSERS + r"$"
)
_EN_REQUEST_RE = re.compile(
    r"\b(?:please|kindly|let me know|could you|can you|would you|which|what|how should)\b[^.!?\n]*[.!]?\s*$",
    re.IGNORECASE,
)
_OPTIONS_AFTER_QUESTION_RE = re.compile(
    r"[?？][^\n]*\n(?:[ \t]*(?:[-*・]|\d+[.．、)])[ \t]*\S[^\n]*\n?){2,}\s*$"
)


def looks_like_request(text: str) -> bool:
    tail = text.rstrip()[-400:]
    if not tail:
        return False
    return bool(
        _JP_REQUEST_RE.search(tail) or _EN_REQUEST_RE.search(tail) or _OPTIONS_AFTER_QUESTION_RE.search(tail)
    )


def ends_with_question(ev: dict) -> bool:
    """Stop 時の最終応答が質問・依頼で終わっているか。

    本文は Stop のペイロード(last_assistant_message)だけから取る。別のフック(hq_board)にも
    transcript にも依存しない (TK で hq_board が無くても動き、古い質問を拾わない)。
    他のフックが処理を続けさせている最中(stop_hook_active)は、人の対応待ちではないので鳴らさない。
    """
    if ev.get("stop_hook_active"):
        return False
    text = ev.get("last_assistant_message")
    return isinstance(text, str) and looks_like_request(text)


def log_event(ev: dict) -> None:
    """発火した hook の要点だけを残す (誤判定の調査用)。本文(message)は一切残さない。"""
    try:
        line = "\t".join([
            time.strftime("%H:%M:%S"),
            str(ev.get("hook_event_name", "")),
            str(ev.get("tool_name", "")),
            str(ev.get("notification_type", "")),
            "auq" if "AskUserQuestion" in str(ev.get("message", "")) else "",
        ])
        if EVENT_LOG.exists() and EVENT_LOG.stat().st_size > EVENT_LOG_MAX:
            EVENT_LOG.write_bytes(EVENT_LOG.read_bytes()[-EVENT_LOG_MAX // 2 :])
        with EVENT_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def classify(ev: dict) -> tuple[str, str] | None:
    """(効果音名, 文言) を返す。対象外なら None。

    AskUserQuestion は PreToolUse に加えて PermissionRequest / Notification(permission_prompt)
    でも発火するので、どの経路でも「選択肢」として扱う。
    """
    name = ev.get("hook_event_name", "")
    if name == "Stop":
        return (CHIME_Q, REPLY) if ends_with_question(ev) else None
    if name == "PermissionRequest":
        if ev.get("tool_name") == "AskUserQuestion":
            return CHIME_Q, QUESTION
        return CHIME_P, PERMISSION
    if name == "PreToolUse":
        if ev.get("tool_name") == "AskUserQuestion":
            return CHIME_Q, QUESTION
        return None
    if name == "Notification":
        kind = ev.get("notification_type", "")
        if kind == "permission_prompt":
            if "AskUserQuestion" in str(ev.get("message", "")):
                return CHIME_Q, QUESTION
            return CHIME_P, PERMISSION
        if kind == "idle_prompt":
            return CHIME_Q, IDLE
        if kind == "elicitation_dialog":
            return CHIME_Q, CONFIRM
    return None


def debounced(sid: str, text: str) -> bool:
    """同じセッションの同じ種類が DEBOUNCE_SEC 秒以内なら True (= 鳴らさない)。

    別セッション・別種類は落とさない。判定と記録は排他・原子的 (notify_common.claim)。
    """
    return claim(STAMP, f"{sid}|{text}", DEBOUNCE_SEC) is None


def session_title(ev: dict) -> str:
    """HQ ボードに記録済みのセッション名。無ければセッション ID の先頭 8 文字。"""
    sid = str(ev.get("session_id", ""))
    if not SID_RE.match(sid):
        return ""
    try:
        title = json.loads((BOARD_DIR / f"{sid}.json").read_text(encoding="utf-8")).get("title")
        if title:
            return str(title)
    except Exception:
        pass
    return f"(セッション {sid[:8]})"


def _spawn(args: list[str]) -> None:
    """切り離して起動する。子の stderr は notify_hook.log へ (失敗が見えるように)。"""
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        err = LOG.open("ab")
    except OSError:
        err = None
    try:
        subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=err if err is not None else subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
        )
    finally:
        if err is not None:
            err.close()


def phone(text: str, ev: dict) -> None:
    kind = PHONE_KIND.get(text)
    if kind is None:
        return
    sid = str(ev.get("session_id", ""))
    sid = sid if SID_RE.match(sid) else ""
    _spawn([sys.executable, "-X", "utf8", str(PHONE_SCRIPT), kind, session_title(ev), sid])


def play(sound: str, text: str) -> None:
    speak = ""
    if os.environ.get("CLAUDE_SOUND_SPEAK", "1") != "0":
        speak = PS_SPEAK.replace("__TEXT__", SPEECH_EN.get(text, "Attention"))
    cmd = PS_TEMPLATE.replace("__SOUND__", sound).replace("__SPEAK__", speak)
    _spawn([powershell_exe(), "-NoProfile", "-NonInteractive", "-Command", cmd])


def main() -> int:
    if os.environ.get("CLAUDE_NOTIFY") == "0":
        return 0
    try:
        ev = read_event()
        log_event(ev)
        hit = classify(ev)
    except Exception as exc:
        log(f"sound_notify 判定失敗: {type(exc).__name__}: {str(exc)[:200]}")
        return 0
    if not hit:
        return 0
    chime, text = hit
    sid = str(ev.get("session_id", ""))

    # 音とスマホは独立した例外境界で実行する (片方の失敗でもう片方を止めない)
    if os.environ.get("CLAUDE_SOUND_NOTIFY") != "0":
        try:
            if not debounced(sid, text):
                play(chime, text)
        except Exception as exc:
            log(f"音の起動失敗: {type(exc).__name__}: {str(exc)[:200]}")
    if os.environ.get("CLAUDE_PHONE_NOTIFY") != "0":
        try:
            if phone_enabled():
                phone(text, ev)
        except Exception as exc:
            log(f"スマホ通知の起動失敗: {type(exc).__name__}: {str(exc)[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
