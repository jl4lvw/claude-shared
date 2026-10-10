"""対応待ち音声通知フック — Claude Code が確認・選択・権限許可待ちで止まったとき、
効果音 + 英語の読み上げを鳴らす (2026-10-10)。

対象イベント (settings.local.json に登録):
  PermissionRequest            -> 「権限の確認待ちです」
  Notification                 -> permission_prompt は権限 / idle_prompt は入力待ち
  PreToolUse(AskUserQuestion)  -> 「選択肢の確認待ちです」

設計:
  - 読み上げは固定文言のみ (依頼文・ファイル名など本文は読み上げない = 秘匿情報を声に出さない)
  - 同じ待ちで PermissionRequest と Notification が連続発火するため、3 秒以内の再通知は捨てる
  - 音は別プロセス (PowerShell) を切り離して起動し、フック自体は即 exit 0 (セッションを止めない)
  - 環境変数 CLAUDE_SOUND_NOTIFY=0 で無効化。CLAUDE_SOUND_SPEAK=0 で効果音のみ
  - .claude/relay_local/phone_notify.json があれば、スマホ(LINE WORKS)へも固定文言 + セッション名を送る
    (送信は lw_phone_notify.py を切り離して起動。設定が無ければ何もしない)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEBOUNCE_SEC = 15.0
STAMP = Path(tempfile.gettempdir()) / "claude_sound_notify.stamp.json"
EVENT_LOG = Path(tempfile.gettempdir()) / "claude_sound_notify.events"
ERR_LOG = Path(tempfile.gettempdir()) / "claude_sound_notify.err"

PS_TEMPLATE = (
    "$p = New-Object System.Media.SoundPlayer 'C:/Windows/Media/{sound}.wav'; "
    "$p.PlaySync(); {speak}"
)
PS_SPEAK = (
    "Add-Type -AssemblyName System.Speech; "
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
    "try { $s.SelectVoice('Microsoft Zira Desktop') } catch {}; "
    "$s.Speak('__TEXT__')"
)


def read_event() -> dict:
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


PERMISSION = "権限の確認待ちです"
QUESTION = "選択肢の確認待ちです"


REPLY = "返信待ちです"


def ends_with_question(ev: dict) -> bool:
    """Stop 時の最終応答が質問・依頼で終わっているか。判定は HQ ボード (hq_board.py) と同じ。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import hq_board

    text = ev.get("last_assistant_message")
    if not isinstance(text, str) or not text.strip():
        text = hq_board._transcript_info(ev).get("last_assistant", "")
    return bool(text) and hq_board._looks_like_question(text)


def log_event(ev: dict) -> None:
    """発火した hook の要点だけを残す (誤判定の調査用。本文は 60 文字で切る)。"""
    try:
        line = "\t".join([
            time.strftime("%H:%M:%S"),
            str(ev.get("hook_event_name", "")),
            str(ev.get("tool_name", "")),
            str(ev.get("notification_type", "")),
            str(ev.get("message", ""))[:60].replace("\n", " "),
        ])
        lines = []
        if EVENT_LOG.exists():
            lines = EVENT_LOG.read_text(encoding="utf-8").splitlines()[-49:]
        EVENT_LOG.write_text("\n".join([*lines, line]) + "\n", encoding="utf-8")
    except Exception:
        pass


def classify(ev: dict) -> tuple[str, str] | None:
    """(効果音名, 読み上げ文) を返す。対象外なら None。

    AskUserQuestion は PreToolUse に加えて PermissionRequest / Notification(permission_prompt)
    でも発火するので、どの経路でも「選択肢」として扱う (権限の別件と二重に鳴らさない)。
    """
    name = ev.get("hook_event_name", "")
    chime_q, chime_p = "Windows Notify System Generic", "Windows Exclamation"
    if name == "Stop":
        # テキストの質問・依頼で止まったときだけ (通常の完了報告では鳴らさない)
        return (chime_q, REPLY) if ends_with_question(ev) else None
    if name == "PermissionRequest":
        if ev.get("tool_name") == "AskUserQuestion":
            return chime_q, QUESTION
        return chime_p, PERMISSION
    if name == "PreToolUse":
        if ev.get("tool_name") == "AskUserQuestion":
            return chime_q, QUESTION
        return None
    if name == "Notification":
        kind = ev.get("notification_type", "")
        if kind == "permission_prompt":
            if "AskUserQuestion" in str(ev.get("message", "")):
                return chime_q, QUESTION
            return chime_p, PERMISSION
        if kind == "idle_prompt":
            return chime_q, "入力待ちです"
        if kind == "elicitation_dialog":
            return chime_q, "確認待ちです"
    return None


def debounced(text: str) -> bool:
    """同じ待ちの重複通知を捨てる。

    - 同じ種類は DEBOUNCE_SEC 秒に 1 回
    - 選択肢の直後 (DEBOUNCE_SEC 秒以内) に来た権限通知は、同じ待ちの別経路とみなして捨てる
      (message に AskUserQuestion が含まれない経路への保険)
    """
    now = time.time()
    try:
        last = json.loads(STAMP.read_text(encoding="utf-8"))
    except Exception:
        last = {}
    if now - float(last.get(text, 0)) < DEBOUNCE_SEC:
        return True
    if text == PERMISSION and now - float(last.get(QUESTION, 0)) < DEBOUNCE_SEC:
        return True
    last[text] = now
    try:
        STAMP.write_text(json.dumps(last), encoding="utf-8")
    except Exception:
        pass
    return False


PHONE_CONFIG = Path(__file__).resolve().parents[1] / "relay_local" / "phone_notify.json"
PHONE_SCRIPT = Path(__file__).resolve().with_name("lw_phone_notify.py")
BOARD_DIR = Path(os.environ.get("CLAUDE_HQ_DIR", r"C:/ClaudeCode/.hq")) / "board"


def session_title(ev: dict) -> str:
    """HQ ボードに記録済みのセッション名。無ければセッション ID の先頭 8 文字。"""
    sid = str(ev.get("session_id", ""))
    try:
        title = json.loads((BOARD_DIR / f"{sid}.json").read_text(encoding="utf-8")).get("title")
        if title:
            return str(title)
    except Exception:
        pass
    return f"(セッション {sid[:8]})" if sid else ""


def phone(text: str, ev: dict) -> None:
    if not PHONE_CONFIG.exists() or os.environ.get("CLAUDE_PHONE_NOTIFY") == "0":
        return
    kind = text.removesuffix("です")
    subprocess.Popen(
        [sys.executable, "-X", "utf8", str(PHONE_SCRIPT), kind, session_title(ev)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=0x08000000 | 0x00000200,
    )


# 読み上げは英語 (2026-10-10 ユーザー指定)。スマホ通知は日本語の文言のまま
SPEECH_EN = {
    "権限の確認待ちです": "Permission needed",
    "選択肢の確認待ちです": "Question waiting",
    "入力待ちです": "Waiting for input",
    "確認待ちです": "Confirmation needed",
    "返信待ちです": "Reply needed",
}


def play(sound: str, text: str) -> None:
    speak = ""
    if os.environ.get("CLAUDE_SOUND_SPEAK", "1") != "0":
        speak = PS_SPEAK.replace("__TEXT__", SPEECH_EN.get(text, "Attention"))
    cmd = PS_TEMPLATE.format(sound=sound, speak=speak)
    flags = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | NEW_PROCESS_GROUP (DETACHED_PROCESS は音声が出なくなる)
    subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )


def main() -> int:
    if os.environ.get("CLAUDE_SOUND_NOTIFY") == "0":
        return 0
    try:
        ev = read_event()
        log_event(ev)
        hit = classify(ev)
        if hit and not debounced(hit[1]):
            play(*hit)
            phone(hit[1], ev)
    except Exception as exc:
        try:
            with ERR_LOG.open("a", encoding="utf-8") as f:
                f.write(f"{type(exc).__name__}: {str(exc)[:200]}{chr(10)}")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
