"""対応待ちをスマホ(LINE WORKS 専用トーク)へ一方向で通知する (2026-10-10)。

sound_notify.py から切り離した別プロセスとして起動される。単独でも使える:
  python lw_phone_notify.py <種別文言> [セッション名]
  python lw_phone_notify.py --test

設定: .claude/relay_local/phone_notify.json (Git 管理外・端末固有。無ければ何もしない)
  {"room_id": "<Webhook で実受信した room_id>", "room_type": "user|channel"}

設計:
  - 041 中継サーバーの /lineworks/sessions → notify を使う (080 の lw.py と同じ方式)。
    セッションの lease は 30 分。専用トークでのみ使うこと (他と同じ room だと 409 で衝突する)
  - 送るのは固定文言 + セッション名だけ。依頼文・ファイル名・コマンド本文は送らない
  - API キーはログに出さない。失敗は relay_local/phone_notify.log に残し、必ず exit 0
  - 同じ文言の連続送信は 30 秒に 1 回に抑える
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib import error, request

LOCAL = Path(__file__).resolve().parents[1] / "relay_local"
CONFIG = LOCAL / "phone_notify.json"
SESSION = LOCAL / "phone_notify_session.json"
STAMP = LOCAL / "phone_notify_last.json"
LOG = LOCAL / "phone_notify.log"
MIN_INTERVAL_SEC = 30.0
MSG_MAX = 200


def log(msg: str) -> None:
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass


def load_env() -> tuple[str, str]:
    values: dict[str, str] = {}
    for line in (LOCAL / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            values[k.strip()] = v.strip()
    return values.get("RELAY_BASE_URL", "").rstrip("/"), values.get("RELAY_API_KEY", "")


class Client:
    def __init__(self, base: str, key: str, room_id: str, room_type: str) -> None:
        self.base, self.key = base, key
        self.room_id, self.room_type = room_id, room_type
        self.session_id: int | None = None
        self.lease: str | None = None
        try:
            saved = json.loads(SESSION.read_text(encoding="utf-8"))
            if saved.get("room_id") == room_id:
                self.session_id, self.lease = saved.get("session_id"), saved.get("lease")
        except (OSError, ValueError):
            pass

    def call(self, path: str, body: dict) -> dict:
        req = request.Request(self.base + path, method="POST", data=json.dumps(body).encode("utf-8"))
        req.add_header("X-API-Key", self.key)
        req.add_header("Content-Type", "application/json")
        with request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}

    def ensure_session(self) -> None:
        if self.session_id is not None and self.lease:
            try:
                self.call(f"/lineworks/sessions/{self.session_id}/heartbeat", {"lease_token": self.lease})
                return
            except error.HTTPError:
                self.session_id = self.lease = None
        out = self.call("/lineworks/sessions", {"room_id": self.room_id, "room_type": self.room_type})
        self.session_id, self.lease = int(out["id"]), out["lease_token"]
        SESSION.write_text(
            json.dumps({"room_id": self.room_id, "session_id": self.session_id, "lease": self.lease}),
            encoding="utf-8",
        )

    def notify(self, text: str) -> None:
        self.ensure_session()
        self.call(f"/lineworks/sessions/{self.session_id}/notify",
                  {"message": text, "lease_token": self.lease})


def too_soon(kind: str) -> bool:
    now = time.time()
    try:
        last = json.loads(STAMP.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        last = {}
    if now - float(last.get(kind, 0)) < MIN_INTERVAL_SEC:
        return True
    last[kind] = now
    try:
        STAMP.write_text(json.dumps(last), encoding="utf-8")
    except OSError:
        pass
    return False


def send(kind: str, title: str) -> None:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    room_id = str(cfg.get("room_id", "")).strip()
    if not room_id:
        return
    if too_soon(kind):
        return
    base, key = load_env()
    if not base or not key:
        log("relay 設定(RELAY_BASE_URL/RELAY_API_KEY)が無い")
        return
    text = f"【{kind}】{title}".strip()[:MSG_MAX]
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            client = Client(base, key, room_id, str(cfg.get("room_type", "user")))
            client.notify(text)
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            try:
                SESSION.unlink()  # lease 切れ等は作り直す
            except OSError:
                pass
            time.sleep(1.5)
    log(f"送信失敗 kind={kind}: {type(last_exc).__name__}: {str(last_exc)[:150]}")


def main(argv: list[str]) -> int:
    try:
        if argv and argv[0] == "--test":
            send("通知テスト", "Claude Code スマホ通知のテストです")
        elif argv:
            send(argv[0], argv[1] if len(argv) > 1 else "")
    except Exception as exc:  # noqa: BLE001
        log(f"例外: {type(exc).__name__}: {str(exc)[:150]}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001, S110
        pass
    sys.exit(main(sys.argv[1:]))
