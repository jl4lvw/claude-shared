"""Claude間連携API(041)のクライアントCLI。/relay send, /relay check から呼ばれる。

依存はPython標準ライブラリのみ(urllib)。A/B双方のマシンで別々のPython環境に
置かれる可能性があるため、python-dotenv 等の外部依存を持たない。

このスクリプト自体は .claude/skills/relay/scripts/ 配下にあり、/g-ul・/g-dl で
A/B間を同期する。APIキー等の秘密情報は同期対象外の .claude/relay_local/.env に
置く(g-ulのミラー対象は .claude/{skills,commands,tools,rules,memory} のみで
relay_local は含まれないため、誤ってclaude-sharedへコミットされない)。
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib import error, request
from urllib.parse import urlencode

if sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
LOCAL_DIR = Path(__file__).resolve().parents[3] / "relay_local"
ENV_PATH = LOCAL_DIR / ".env"
DOWNLOAD_DIR = LOCAL_DIR / "inbox"


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
SELF_USER_ID = _ENV.get("RELAY_SELF_USER_ID", "")


def _require_config() -> None:
    missing = [
        name
        for name, value in (
            ("RELAY_BASE_URL", BASE_URL),
            ("RELAY_API_KEY", API_KEY),
            ("RELAY_SELF_USER_ID", SELF_USER_ID),
        )
        if not value
    ]
    if missing:
        print(
            f"設定が不足しています: {', '.join(missing)}。{ENV_PATH} を作成してください"
            f"({SCRIPT_DIR / '.env.example'} をコピー)。",
            file=sys.stderr,
        )
        sys.exit(1)


def _request_json(
    method: str,
    path: str,
    payload: Optional[dict[str, Any]] = None,
    params: Optional[dict[str, str]] = None,
) -> Any:
    url = f"{BASE_URL}{path}"
    if params:
        url += "?" + urlencode(params)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = request.Request(url, data=data, method=method)
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


_MIME_OVERRIDES = {
    # Windowsのmimetypesレジストリは.zipを"application/x-zip-compressed"等の
    # 非標準MIMEで返すことがあり、サーバー側の許可リスト("application/zip"のみ)に
    # 弾かれる(415)。拡張子ベースで明示的に上書きする。
    ".zip": "application/zip",
}


def _upload_file(thread_id: str, message_id: int, file_path: Path) -> dict[str, Any]:
    boundary = uuid.uuid4().hex
    content_type = (
        _MIME_OVERRIDES.get(file_path.suffix.lower())
        or mimetypes.guess_type(file_path.name)[0]
        or "application/octet-stream"
    )

    body = bytearray()
    body += f"--{boundary}\r\n".encode("utf-8")
    body += (
        f'Content-Disposition: form-data; name="file"; filename="{file_path.name}"\r\n'
    ).encode("utf-8")
    body += f"Content-Type: {content_type}\r\n\r\n".encode("utf-8")
    body += file_path.read_bytes()
    body += f"\r\n--{boundary}--\r\n".encode("utf-8")

    url = f"{BASE_URL}/files?" + urlencode({"thread_id": thread_id, "message_id": message_id})
    req = request.Request(url, data=bytes(body), method="POST")
    req.add_header("X-API-Key", API_KEY)
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"ファイルアップロードエラー {exc.code}: {detail}", file=sys.stderr)
        sys.exit(1)


def _download_file(file_id: str, filename: str) -> Path:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    url = f"{BASE_URL}/files/{file_id}"
    req = request.Request(url, method="GET")
    req.add_header("X-API-Key", API_KEY)
    dest = DOWNLOAD_DIR / f"{file_id}_{filename}"
    with request.urlopen(req, timeout=60) as resp:
        dest.write_bytes(resp.read())
    return dest


def cmd_send(args: argparse.Namespace) -> None:
    payload: dict[str, Any] = {"to": args.to, "type": args.type, "content": args.content}
    if args.thread:
        payload["thread_id"] = args.thread
    if args.no_reply_needed:
        payload["no_reply_needed"] = True

    message = _request_json("POST", "/messages", payload=payload)
    print(f"送信しました: thread_id={message['thread_id']} message_id={message['id']}")

    if args.file:
        file_path = Path(args.file)
        if not file_path.exists():
            print(f"ファイルが見つかりません: {file_path}", file=sys.stderr)
            sys.exit(1)
        file_info = _upload_file(message["thread_id"], message["id"], file_path)
        print(f"添付しました: file_id={file_info['file_id']} filename={file_info['filename']}")


def cmd_check(_args: argparse.Namespace) -> None:
    # GET /messages(自分宛の受信箱取得)は「未処理すべて」(未読 + 取得済みだがdone
    # されていないもの)を返す(2026-07-20サーバー仕様変更)。自動ポーリングが先に取得
    # していても、doneするまで毎回表示されるため処理漏れが起きない。
    # 取得と同時にサーバー側でunread→processing(取得済み)へ自動遷移する。
    messages = _request_json("GET", "/messages", params={"to": SELF_USER_ID, "unread": "true"})
    if not messages:
        print("未処理のメッセージはありません。")
        return

    status_labels = {"unread": "新着", "processing": "未処理(以前取得済み)"}
    for msg in messages:
        label = status_labels.get(msg.get("status", ""), msg.get("status", ""))
        print("=" * 60)
        print(
            f"[{msg['type']}][{label}] thread_id={msg['thread_id']} message_id={msg['id']} "
            f"from={msg['from_user']} at={msg['created_at']}"
        )
        print(msg["content"])

        files = _request_json("GET", "/files", params={"message_id": str(msg["id"])})
        for file_info in files or []:
            dest = _download_file(file_info["file_id"], file_info["filename"])
            print(f"  添付ダウンロード: {dest}")

    print("=" * 60)
    print(f"{len(messages)}件の未処理メッセージがあります(doneするまで毎回表示されます)。")
    print(
        "処理が完了したら、必ず次のコマンドで「既読処理済」にしてください: "
        "relay_client.py done <message_id>"
    )


def cmd_edit(args: argparse.Namespace) -> None:
    message = _request_json(
        "PATCH", f"/messages/{args.message_id}", payload={"content": args.content}
    )
    print(f"編集しました: message_id={message['id']} edited_at={message['edited_at']}")


def cmd_done(args: argparse.Namespace) -> None:
    message = _request_json("POST", f"/messages/{args.message_id}/done")
    print(f"既読処理済にしました: message_id={message['id']} completed_at={message['completed_at']}")


def main() -> None:
    _require_config()
    parser = argparse.ArgumentParser(description="Claude間連携API クライアント")
    subparsers = parser.add_subparsers(dest="command", required=True)

    send_parser = subparsers.add_parser("send", help="メッセージを送信する")
    send_parser.add_argument("content", help="メッセージ本文")
    send_parser.add_argument("--to", required=True, help="宛先ユーザーID (例: A, B, RC, TK)")
    send_parser.add_argument(
        "--type", choices=["task", "question", "reply", "result"], default="task"
    )
    send_parser.add_argument("--thread", default=None, help="継続する場合はthread_idを指定")
    send_parser.add_argument("--file", default=None, help="添付するファイルのパス")
    send_parser.add_argument(
        "--no-reply-needed",
        action="store_true",
        help="相手からの返信・確認を要さないメッセージ(お礼・完了報告等)に付与する。"
        "定時LineWorks通知やビューアの放置⚠️バッジの対象から除外される",
    )
    send_parser.set_defaults(func=cmd_send)

    check_parser = subparsers.add_parser("check", help="自分宛の未読を確認する")
    check_parser.set_defaults(func=cmd_check)

    edit_parser = subparsers.add_parser("edit", help="未読の自分の送信メッセージを編集する")
    edit_parser.add_argument("message_id", type=int, help="編集するメッセージのID")
    edit_parser.add_argument("content", help="新しい本文")
    edit_parser.set_defaults(func=cmd_edit)

    done_parser = subparsers.add_parser(
        "done", help="処理完了したメッセージを「既読処理済」にする"
    )
    done_parser.add_argument("message_id", type=int, help="完了にするメッセージのID")
    done_parser.set_defaults(func=cmd_done)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
