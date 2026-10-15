"""対応待ちをスマホ(LINE WORKS 専用トーク)へ一方向で通知する (2026-10-10)。

sound_notify.py から切り離した別プロセスとして起動される。単独でも使える:
  python lw_phone_notify.py <kind> [セッション名] [session_id]
        kind = permission|question|reply|idle|confirm|test
  python lw_phone_notify.py --test        # 結果を表示し、失敗なら終了コード 1

経路: 自分のリレー鍵で 041 の POST /notify/phone へ依頼する (A でも TK でも同じ)。
  宛先と本文の組み立てはサーバー側 (041/secrets/phone_notify.json の users に載った名義だけ)。
  クライアントが送るのは「種類」と「セッション名」だけ。依頼文・コマンド本文は送らない。

設定: .claude/relay_local/phone_notify.json (Git 管理外・端末固有)
  ファイルが在り、{"enabled": false} でなければ有効。中身は {"enabled": true} でよい。
  旧版の room_id 直指定(lease 付きセッション)の経路は撤去した (競合・二重送信・寿命不定のため)。
  サーバーに /notify/phone が無い(404/405)場合は、ログに残して何もしない。

安全側の作り:
  - API キーは https の接続先にだけ送る (localhost は除く)。リダイレクトは辿らない
  - 同じセッション×種類は 15 秒に 1 回 (複数経路で発火した同じ待ちを畳む)。
    判定は排他・原子的で、送信に失敗したら 5 秒後には再通知を通す
  - サーバーが 200 {"sent": false} を返した(抑制)場合もログに残す
  - 失敗は relay_local/notify_hook.log に残し、フック経由では必ず exit 0。API キーはログに出さない
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from urllib import error, request
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from notify_common import LOCAL, claim, fail_backoff, log, phone_enabled  # noqa: E402

STAMP = LOCAL / "phone_notify_last.json"
MIN_INTERVAL_SEC = 15.0
FAIL_RETRY_SEC = 5.0
KINDS = ("permission", "question", "reply", "idle", "confirm", "test")
TITLE_MAX = 60


class _NoRedirect(request.HTTPRedirectHandler):
    """認証ヘッダ付きの POST を別ホストへ転送しない。"""

    def redirect_request(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None


_OPENER = request.build_opener(_NoRedirect)


def load_env() -> tuple[str, str]:
    values: dict[str, str] = {}
    for line in (LOCAL / ".env").read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            values[k.strip()] = v.strip().strip('"').strip("'")
    return values.get("RELAY_BASE_URL", "").rstrip("/"), values.get("RELAY_API_KEY", "")


def base_url_ok(base: str) -> bool:
    u = urlparse(base)
    return u.scheme == "https" or (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1"))


def post_api(base: str, key: str, kind: str, title: str) -> dict:
    """041 の POST /notify/phone へ依頼する (宛先・本文の組み立てはサーバー側)。"""
    req = request.Request(
        base + "/notify/phone",
        method="POST",
        data=json.dumps({"kind": kind, "title": title[:TITLE_MAX]}).encode("utf-8"),
    )
    req.add_header("X-API-Key", key)
    req.add_header("Content-Type", "application/json")
    with _OPENER.open(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def send(kind: str, title: str, sid: str = "", *, dedupe: bool = True) -> dict | None:
    """通知を依頼する。サーバーの応答(dict)を返す。送らなかった/失敗したときは None。"""
    if kind not in KINDS or not phone_enabled():
        return None
    try:
        base, key = load_env()
    except OSError as exc:
        log(f"スマホ通知: .env が読めない: {type(exc).__name__}")
        return None
    if not base or not key:
        log("スマホ通知: relay 設定(RELAY_BASE_URL/RELAY_API_KEY)が無い")
        return None
    if not base_url_ok(base):
        log("スマホ通知: RELAY_BASE_URL が https ではないので送らない")
        return None

    claim_key = f"{sid}|{kind}"
    got = claim(STAMP, claim_key, MIN_INTERVAL_SEC) if dedupe else (0.0, time.time())
    if got is None:
        return None  # 同じ待ちの別経路で既に依頼済み
    _, at = got
    try:
        out = post_api(base, key, kind, title)
    except error.HTTPError as exc:
        log(f"スマホ通知: API失敗 kind={kind} HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001
        log(f"スマホ通知: 通信失敗 kind={kind}: {type(exc).__name__}: {str(exc)[:120]}")
    else:
        if out.get("sent") is False:
            log(f"スマホ通知: サーバーが抑制 kind={kind} reason={out.get('reason')}")
        return out
    if dedupe:
        fail_backoff(STAMP, claim_key, at, MIN_INTERVAL_SEC, FAIL_RETRY_SEC)
    return None


def run_test() -> int:
    """手動テスト。結果を画面に出し、送れなければ終了コード 1。"""
    out = send("test", "Claude Code スマホ通知のテストです", "", dedupe=False)
    if out is None:
        print("失敗: 送れませんでした。原因は relay_local/notify_hook.log を見てください。")
        return 1
    if out.get("sent") is False:
        print(f"サーバーが抑制しました ({out.get('reason')})。30 秒空けて再実行してください。")
        return 1
    print("送信しました。スマホの専用トークを確認してください。")
    return 0


def main(argv: list[str]) -> int:
    try:
        if argv and argv[0] == "--test":
            return run_test()
        if argv:
            send(argv[0], argv[1] if len(argv) > 1 else "", argv[2] if len(argv) > 2 else "")
    except Exception as exc:  # noqa: BLE001
        log(f"スマホ通知: 例外 {type(exc).__name__}: {str(exc)[:150]}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001, S110
        pass
    sys.exit(main(sys.argv[1:]))
