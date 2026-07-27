"""LineWorks Callback受信箱(041.Claude間連携API)のクライアントCLI。/lineworks-check から呼ばれる。

relay(A)と同じサーバー・同じ設定ファイル(.claude/relay_local/.env)を使う。
LineWorks受信箱はA専用(サーバー側で他ユーザーからのアクセスは403)。
依存はPython標準ライブラリのみ(urllib)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional
from urllib import error, request
from urllib.parse import urlencode

if sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

LOCAL_DIR = Path(__file__).resolve().parents[3] / "relay_local"
ENV_PATH = LOCAL_DIR / ".env"


def _load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


_ENV = _load_env(ENV_PATH)
BASE_URL = _ENV.get("RELAY_BASE_URL", "").rstrip("/")
API_KEY = _ENV.get("RELAY_API_KEY", "")


def _require_config() -> None:
    missing = [
        name
        for name, value in (("RELAY_BASE_URL", BASE_URL), ("RELAY_API_KEY", API_KEY))
        if not value
    ]
    if missing:
        print(
            f"設定が不足しています: {', '.join(missing)}。{ENV_PATH} を確認してください。",
            file=sys.stderr,
        )
        sys.exit(1)


def _request_json(
    method: str, path: str, params: Optional[dict[str, str]] = None
) -> Any:
    url = f"{BASE_URL}{path}"
    if params:
        url += "?" + urlencode(params)
    req = request.Request(url, method=method)
    req.add_header("X-API-Key", API_KEY)
    try:
        with request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else None
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"APIエラー {exc.code}: {detail}", file=sys.stderr)
        sys.exit(1)
    except error.URLError as exc:
        print(f"通信エラー: {exc}", file=sys.stderr)
        sys.exit(1)


def cmd_check(_args: argparse.Namespace) -> None:
    messages = _request_json("GET", "/lineworks/messages", params={"unprocessed": "true"})
    if not messages:
        print("未処理のLineWorksメッセージはありません。")
        return

    for msg in messages:
        print("=" * 60)
        print(
            f"message_id={msg['id']} room_id={msg['room_id']} "
            f"sender_id={msg['sender_id']} at={msg['received_at']}"
        )
        print(msg["content"])

    print("=" * 60)
    print(f"{len(messages)}件を取得しました。")
    print(
        "処理が完了したら、必ず次のコマンドで完了にしてください: "
        "lw_client.py done <message_id>"
    )


def cmd_done(args: argparse.Namespace) -> None:
    message = _request_json("POST", f"/lineworks/messages/{args.message_id}/done")
    print(f"完了にしました: message_id={message['id']} processed_at={message['processed_at']}")


def main() -> None:
    _require_config()
    parser = argparse.ArgumentParser(description="LineWorks Callback受信箱 クライアント")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_parser = subparsers.add_parser("check", help="未処理のLineWorksメッセージを確認する")
    check_parser.set_defaults(func=cmd_check)

    done_parser = subparsers.add_parser("done", help="処理完了したメッセージを完了にする")
    done_parser.add_argument("message_id", type=int, help="完了にするメッセージのID")
    done_parser.set_defaults(func=cmd_done)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
