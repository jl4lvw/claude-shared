"""/remote-control 用クライアントCLI。

LineWorksトーク(room)単位のセッションAPI(041.Claude間連携API)を叩く。
relay/lineworks-checkと同じサーバー・同じ設定ファイル(.claude/relay_local/.env)を使う。
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
KNOWN_ROOMS_PATH = LOCAL_DIR / "lineworks_known_rooms.json"


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
    method: str,
    path: str,
    params: Optional[dict[str, str]] = None,
    json_body: Optional[dict[str, Any]] = None,
) -> Any:
    url = f"{BASE_URL}{path}"
    if params:
        url += "?" + urlencode(params)
    data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
    req = request.Request(url, method=method, data=data)
    req.add_header("X-API-Key", API_KEY)
    if data is not None:
        req.add_header("Content-Type", "application/json")
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


def _load_known_rooms() -> dict[str, dict[str, str]]:
    if not KNOWN_ROOMS_PATH.exists():
        return {}
    try:
        data = json.loads(KNOWN_ROOMS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"警告: {KNOWN_ROOMS_PATH} の形式が不正です。ラベル解決をスキップします。", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def resolve_room(room_id_or_label: str, room_type: Optional[str] = None) -> tuple[str, Optional[str]]:
    """`lineworks_known_rooms.json`に登録済みのラベルなら room_id/room_type を解決する。

    登録キーとの完全一致でのみ解決する(UUID風文字列との誤判定を避けるため、
    正規表現等での「らしさ」判定はしない。cgd Lv7レビューでの指摘を踏まえた設計)。
    未登録の文字列はそのままroom_idとして扱う(後方互換)。
    session-start/messagesの両方から呼ぶこと(片方だけだと統合バグになる、
    cgd Lv7レビューでCodex highが指摘)。
    """
    known = _load_known_rooms()
    entry = known.get(room_id_or_label)
    if entry is None:
        return room_id_or_label, room_type
    resolved_room_id = entry.get("room_id", room_id_or_label)
    resolved_room_type = entry.get("room_type")
    if room_type is not None and resolved_room_type is not None and room_type != resolved_room_type:
        print(
            f"エラー: ラベル'{room_id_or_label}'の登録room_typeは'{resolved_room_type}'ですが、"
            f"引数では'{room_type}'が指定されました。",
            file=sys.stderr,
        )
        sys.exit(1)
    return resolved_room_id, (resolved_room_type or room_type)


def cmd_rooms_list(_args: argparse.Namespace) -> None:
    known = _load_known_rooms()
    if not known:
        print("登録済みのルームはありません。")
        return
    print(json.dumps(known, ensure_ascii=False, indent=2))


def cmd_rooms_discover(_args: argparse.Namespace) -> None:
    """Botが過去にメッセージを受信した全roomと、ローカル登録済みラベルを突き合わせて一覧表示する。

    room_idの手入力・記憶を不要にし、クリック選択(AskUserQuestion)の材料を
    Claude Codeへ渡すためのコマンド(2026-07-13 ユーザー指摘への対応)。
    """
    rooms = _request_json("GET", "/lineworks/rooms") or []
    known = _load_known_rooms()
    label_by_room_id = {entry.get("room_id"): label for label, entry in known.items()}

    result = [
        {
            "room_id": room["room_id"],
            "room_type": room["room_type"],
            "label": label_by_room_id.get(room["room_id"]),
            "last_received_at": room["last_received_at"],
            "message_count": room["message_count"],
        }
        for room in rooms
    ]
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_session_start(args: argparse.Namespace) -> None:
    room_id, room_type = resolve_room(args.room_id, args.room_type)
    if room_type is None:
        print(
            "エラー: room_typeを解決できません。未登録のroom_idを使う場合はroom_typeを明示してください。",
            file=sys.stderr,
        )
        sys.exit(1)
    session = _request_json(
        "POST",
        "/lineworks/sessions",
        json_body={"room_id": room_id, "room_type": room_type},
    )
    print(json.dumps(session, ensure_ascii=False, indent=2))


def cmd_claim(args: argparse.Namespace) -> None:
    session = _request_json(
        "POST",
        f"/lineworks/sessions/{args.session_id}/claim",
        json_body={"sender_id": args.sender_id, "lease_token": args.lease_token},
    )
    print(json.dumps(session, ensure_ascii=False, indent=2))


def cmd_ask(args: argparse.Namespace) -> None:
    session = _request_json(
        "POST",
        f"/lineworks/sessions/{args.session_id}/ask",
        json_body={"question": args.question, "lease_token": args.lease_token},
    )
    print(json.dumps(session, ensure_ascii=False, indent=2))


def cmd_notify(args: argparse.Namespace) -> None:
    session = _request_json(
        "POST",
        f"/lineworks/sessions/{args.session_id}/notify",
        json_body={"message": args.message, "lease_token": args.lease_token},
    )
    print(json.dumps(session, ensure_ascii=False, indent=2))


def cmd_remind(args: argparse.Namespace) -> None:
    session = _request_json(
        "POST",
        f"/lineworks/sessions/{args.session_id}/remind",
        json_body={"lease_token": args.lease_token},
    )
    print(json.dumps(session, ensure_ascii=False, indent=2))


def cmd_heartbeat(args: argparse.Namespace) -> None:
    session = _request_json(
        "POST",
        f"/lineworks/sessions/{args.session_id}/heartbeat",
        json_body={"lease_token": args.lease_token},
    )
    print(json.dumps(session, ensure_ascii=False, indent=2))


def cmd_close(args: argparse.Namespace) -> None:
    session = _request_json(
        "POST",
        f"/lineworks/sessions/{args.session_id}/close",
        json_body={"lease_token": args.lease_token, "status": args.status},
    )
    print(json.dumps(session, ensure_ascii=False, indent=2))


def cmd_messages(args: argparse.Namespace) -> None:
    room_id, _room_type = resolve_room(args.room_id)
    params = {"room_id": room_id, "unprocessed": "true"}
    if args.after_id is not None:
        params["after_id"] = str(args.after_id)
    messages = _request_json("GET", "/lineworks/messages", params=params)
    if not messages:
        print("[]")
        return
    print(json.dumps(messages, ensure_ascii=False, indent=2))


def cmd_done(args: argparse.Namespace) -> None:
    message = _request_json("POST", f"/lineworks/messages/{args.message_id}/done")
    print(json.dumps(message, ensure_ascii=False, indent=2))


def main() -> None:
    _require_config()
    parser = argparse.ArgumentParser(description="/remote-control セッションAPI クライアント")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p = subparsers.add_parser("session-start", help="roomのセッションを新規作成or引き継ぎ取得する")
    p.add_argument("room_id", help="room_id、またはlineworks_known_rooms.json登録済みラベル")
    p.add_argument(
        "room_type",
        nargs="?",
        default=None,
        choices=["user", "channel"],
        help="登録済みラベルを使う場合は省略可(登録値を使う)",
    )
    p.set_defaults(func=cmd_session_start)

    p = subparsers.add_parser("rooms-list", help="lineworks_known_rooms.jsonの登録済みラベル一覧を表示する")
    p.set_defaults(func=cmd_rooms_list)

    p = subparsers.add_parser(
        "rooms-discover", help="Botが受信したことのある全roomをラベル付き(あれば)で一覧表示する"
    )
    p.set_defaults(func=cmd_rooms_discover)

    p = subparsers.add_parser("claim", help="指示発行者(sender_id)をセッションに確定する")
    p.add_argument("session_id", type=int)
    p.add_argument("lease_token")
    p.add_argument("sender_id")
    p.set_defaults(func=cmd_claim)

    p = subparsers.add_parser("ask", help="LineWorksへ質問を送信しwaiting_replyにする")
    p.add_argument("session_id", type=int)
    p.add_argument("lease_token")
    p.add_argument("question")
    p.set_defaults(func=cmd_ask)

    p = subparsers.add_parser("notify", help="回答を待たない一方向メッセージを送信する(完了報告等)")
    p.add_argument("session_id", type=int)
    p.add_argument("lease_token")
    p.add_argument("message")
    p.set_defaults(func=cmd_notify)

    p = subparsers.add_parser("remind", help="24時間リマインドを1回だけ送信する")
    p.add_argument("session_id", type=int)
    p.add_argument("lease_token")
    p.set_defaults(func=cmd_remind)

    p = subparsers.add_parser("heartbeat", help="待機ループの生存通知(leaseを延長する)")
    p.add_argument("session_id", type=int)
    p.add_argument("lease_token")
    p.set_defaults(func=cmd_heartbeat)

    p = subparsers.add_parser("close", help="セッションを終了する")
    p.add_argument("session_id", type=int)
    p.add_argument("lease_token")
    p.add_argument("status", choices=["done", "expired"])
    p.set_defaults(func=cmd_close)

    p = subparsers.add_parser("messages", help="指定roomの未処理メッセージを取得する")
    p.add_argument("room_id")
    p.add_argument("--after-id", type=int, default=None)
    p.set_defaults(func=cmd_messages)

    p = subparsers.add_parser("done", help="処理完了したメッセージを完了にする")
    p.add_argument("message_id", type=int)
    p.set_defaults(func=cmd_done)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
