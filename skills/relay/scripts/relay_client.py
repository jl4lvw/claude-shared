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


class ApiConflict:
    """想定内のHTTPエラー(競合等)を、異常終了させずに呼び出し側へ返すための箱。"""

    def __init__(self, code: int, detail: str) -> None:
        self.code = code
        self.detail = detail


def _request_json(
    method: str,
    path: str,
    payload: Optional[dict[str, Any]] = None,
    params: Optional[dict[str, str]] = None,
    act_as: Optional[str] = None,
    soft_status: tuple[int, ...] = (),
) -> Any:
    url = f"{BASE_URL}{path}"
    if params:
        url += "?" + urlencode(params)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = request.Request(url, data=data, method=method)
    req.add_header("X-API-Key", API_KEY)
    # 代理(delegation): 自分のキーで認証しつつ、名義だけ相手に切り替える。
    # サーバー側で委任表を引くため、登録がなければ403で弾かれる(なりすまし不可)。
    if act_as:
        req.add_header("X-Act-As", act_as)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else None
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        # soft_status に挙げたコードは「想定内の結果」として呼び出し側に判断させる
        # (claimの409=他端末が処理中 は異常ではなくスキップ判断の材料)
        if exc.code in soft_status:
            return ApiConflict(exc.code, detail)
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

# GET /config のキャッシュ(プロセス内で1回だけ取得する)。
# 2026-08-02 TK運用者提言 項目1・2: 添付の上限・許可拡張子をSKILL.mdへハードコードせず、
# サーバー(単一の正)から機械可読に取得して、送信前チェックに使う。
_CONFIG_CACHE: Optional[dict[str, Any]] = None


def _fetch_config() -> Optional[dict[str, Any]]:
    """GET /config を取得する。失敗しても呼び出し側は事前チェックを諦めるだけにする

    (サーバー側の413/415は引き続き最終防衛線として機能するため、/config自体が
    取れないことを送信失敗の理由にはしない)。
    """
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    try:
        _CONFIG_CACHE = _request_json("GET", "/config")
    except SystemExit:
        _CONFIG_CACHE = None
    return _CONFIG_CACHE


def _check_attachment_before_send(file_path: Path) -> Optional[str]:
    """送信前にサイズ・拡張子を/configと照合する。問題があれば理由文字列を返す。

    2026-08-02 項目2(必須): 従来は本文POSTの後に添付だけ415/413で失敗し、
    「添付なし孤児メッセージ」が本文だけ残る事故があった。本文を送る前に検査し、
    違反があれば送信自体を中止する。
    """
    config = _fetch_config()
    if config is None:
        return None  # /configが取れない時は事前チェックを諦める(サーバー側の413/415に委ねる)
    try:
        size = file_path.stat().st_size
    except OSError as exc:
        return f"添付ファイルを確認できません: {exc}"
    max_bytes = config.get("max_file_size_bytes")
    if isinstance(max_bytes, int) and size > max_bytes:
        return (
            f"添付ファイルのサイズ({size}バイト)が上限"
            f"({config.get('max_file_size_mb', max_bytes // (1024 * 1024))}MB)を超えています。"
            "送信を中止しました(本文だけ送られる事故を防ぐため)。"
        )
    ext = file_path.suffix.lower()
    allowed_exts = config.get("allowed_extensions")
    if isinstance(allowed_exts, list) and ext and ext not in allowed_exts:
        return (
            f"添付ファイルの拡張子({ext})が許可リスト外です"
            f"(許可: {', '.join(sorted(allowed_exts))})。送信を中止しました。"
        )
    return None


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
        # 20MB前提でタイムアウトを180秒に拡張(2026-08-02 項目8。従来60秒は低速回線で
        # 大きめの添付が完了前に切れる余地があった)
        with request.urlopen(req, timeout=180) as resp:
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
    # タイムアウトは180秒(項目8、アップロードと同じ理由)
    with request.urlopen(req, timeout=180) as resp:
        content = resp.read()
        # Content-Length検査(項目8): 途中で切れたダウンロードを「成功」として
        # 扱わない。ヘッダが無い場合は検査をスキップする(サーバー実装依存にしない)。
        declared = resp.headers.get("Content-Length")
        if declared is not None and int(declared) != len(content):
            raise RuntimeError(
                f"ダウンロードが不完全です(宣言サイズ{declared}バイト、"
                f"受信{len(content)}バイト)。再試行してください"
            )
        dest.write_bytes(content)
    return dest


def cmd_send(args: argparse.Namespace) -> None:
    # 送信前チェック(2026-08-02 項目2・必須): 本文POSTより前にサイズ・拡張子を
    # /configと照合する。違反があれば送信自体を中止し、「添付なし孤児メッセージ」
    # (本文だけ送られ添付が415/413で失敗した状態)を残さない。
    if args.file:
        file_path = Path(args.file)
        if not file_path.exists():
            print(f"ファイルが見つかりません: {file_path}", file=sys.stderr)
            sys.exit(1)
        problem = _check_attachment_before_send(file_path)
        if problem:
            print(problem, file=sys.stderr)
            sys.exit(1)

    payload: dict[str, Any] = {"to": args.to, "type": args.type, "content": args.content}
    if args.thread:
        payload["thread_id"] = args.thread
    if args.no_reply_needed:
        payload["no_reply_needed"] = True

    act_as = getattr(args, "act_as", None)
    message = _request_json("POST", "/messages", payload=payload, act_as=act_as)
    name = f"{act_as}名義(実行はあなた)" if act_as else "自分名義"
    print(f"送信しました[{name}]: thread_id={message['thread_id']} message_id={message['id']}")

    if args.file:
        file_path = Path(args.file)
        if not file_path.exists():
            print(f"ファイルが見つかりません: {file_path}", file=sys.stderr)
            sys.exit(1)
        file_info = _upload_file(message["thread_id"], message["id"], file_path)
        print(f"添付しました: file_id={file_info['file_id']} filename={file_info['filename']}")


def cmd_check(args: argparse.Namespace) -> None:
    # GET /messages(自分宛の受信箱取得)は「未処理すべて」(未読 + 取得済みだがdone
    # されていないもの)を返す(2026-07-20サーバー仕様変更)。自動ポーリングが先に取得
    # していても、doneするまで毎回表示されるため処理漏れが起きない。
    # 取得と同時にサーバー側でunread→processing(取得済み)へ自動遷移する。
    act_as = getattr(args, "act_as", None)
    target = act_as or SELF_USER_ID
    messages = _request_json(
        "GET", "/messages", params={"to": target, "unread": "true"}, act_as=act_as
    )
    if not messages:
        who = f"{target}宛(代理)" if act_as else "自分宛"
        print(f"{who}の未処理メッセージはありません。")
        return
    if act_as:
        print(f"※ {act_as}宛を代理で閲覧しています(statusは変更されません)")

    status_labels = {"unread": "新着", "processing": "未処理(以前取得済み)"}
    for msg in messages:
        label = status_labels.get(msg.get("status", ""), msg.get("status", ""))
        print("=" * 60)
        print(
            f"[{msg['type']}][{label}] thread_id={msg['thread_id']} message_id={msg['id']} "
            f"from={msg['from_user']} at={msg['created_at']}"
        )
        # 他の実行者が処理権を持っているものは触らない(二重処理防止)
        claimed_by = msg.get("claimed_by")
        if claimed_by:
            # claimed_by は実行者(APIキーの持ち主)なので、代理でも自分なら SELF_USER_ID
            note = (
                "自分が処理中"
                if claimed_by == SELF_USER_ID
                else f"{claimed_by} が処理中 → 手を出さない"
            )
            print(f"  ※ 処理権: {note} (claimed_at={msg.get('claimed_at')})")
        print(msg["content"])

        files = _request_json(
            "GET", "/files", params={"message_id": str(msg["id"])}, act_as=act_as
        )
        for file_info in files or []:
            dest = _download_file(file_info["file_id"], file_info["filename"])
            print(f"  添付ダウンロード: {dest}")

    print("=" * 60)
    print(f"{len(messages)}件の未処理メッセージがあります(doneするまで毎回表示されます)。")
    if act_as:
        print(
            f"代理で処理する場合は、先に処理権を取ってください: "
            f"relay_client.py claim <message_id> --as {act_as}"
        )
        print(
            f"処理が完了したら: relay_client.py done <message_id> --as {act_as}"
        )
    else:
        print(
            "処理を始める前に、必ず処理権を取ってください(二重処理防止): "
            "relay_client.py claim <message_id>"
        )
        print(
            "  → 終了コード2(他の実行者が処理中)ならそのメッセージはスキップする"
        )
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
    act_as = getattr(args, "act_as", None)
    message = _request_json(
        "POST", f"/messages/{args.message_id}/done", act_as=act_as
    )
    print(f"既読処理済にしました: message_id={message['id']} completed_at={message['completed_at']}")


# claim が競合した(他端末が処理中)ときの終了コード。異常終了(1)と区別するため。
EXIT_CLAIM_CONFLICT = 2


def cmd_claim(args: argparse.Namespace) -> None:
    """処理権(claim)を取る。**処理を始める前に必ず実行する**(自分宛でも代理でも)。

    同じ受信箱を複数の実行者が見ている(A端末のGUI + 手動セッション、
    A端末の代理処理 + TK端末本人)ため、claimを取らずに処理すると
    同じ依頼へ二重に返信してしまう。409なら他が着手済みなのでスキップする。
    """
    act_as = getattr(args, "act_as", None)
    result = _request_json(
        "POST", f"/messages/{args.message_id}/claim", act_as=act_as, soft_status=(409,)
    )
    if isinstance(result, ApiConflict):
        print(f"処理権を取得できませんでした(他の実行者が処理中): {result.detail}")
        print("このメッセージには手を出さず、スキップしてください。")
        sys.exit(EXIT_CLAIM_CONFLICT)
    print(
        f"処理権を取得しました: message_id={result['id']} "
        f"claimed_by={result['claimed_by']} at={result['claimed_at']}"
    )
    print("これで他の端末はこのメッセージをdoneできません。処理後に done を実行してください。")


def cmd_unclaim(args: argparse.Namespace) -> None:
    """自分が取った処理権を解除する(処理を中断してほかに任せるとき)。"""
    act_as = getattr(args, "act_as", None)
    message = _request_json(
        "DELETE", f"/messages/{args.message_id}/claim", act_as=act_as
    )
    print(
        f"処理権を解除しました: message_id={message['id']} "
        f"(claimed_by={message['claimed_by']})"
    )
    print("他の端末が処理できる状態に戻りました(放置通知の対象にも戻ります)。")


def cmd_delegate(args: argparse.Namespace) -> None:
    """代理権限を登録する(自分が誰かの代理をする)。"""
    payload: dict[str, Any] = {
        "principal": args.principal,
        "scopes": [s.strip() for s in args.scopes.split(",") if s.strip()],
    }
    if args.from_user:
        payload["from_filter"] = args.from_user
    if args.note:
        payload["note"] = args.note
    body = _request_json("POST", "/delegations", payload=payload)
    limit = f"{body['from_filter']}発のみ" if body["from_filter"] else "送信者制限なし"
    print(
        f"代理権限を登録しました: id={body['id']} "
        f"{body['actor']} → {body['principal']}名義 ({limit}, 権限={','.join(body['scopes'])})"
    )


def cmd_delegations(args: argparse.Namespace) -> None:
    """自分が関わる代理権限を一覧する。"""
    params = {"include_revoked": "true"} if args.all else None
    rows = _request_json("GET", "/delegations", params=params)
    if not rows:
        print("代理権限は登録されていません。")
        return
    for row in rows:
        limit = f"{row['from_filter']}発のみ" if row["from_filter"] else "制限なし"
        state = f"取消済み({row['revoked_at'][:16]})" if row["revoked_at"] else "有効"
        print(
            f"id={row['id']} {row['actor']} → {row['principal']}名義 "
            f"[{limit}] 権限={','.join(row['scopes'])} {state}"
        )
        if row.get("note"):
            print(f"    メモ: {row['note']}")


def cmd_revoke(args: argparse.Namespace) -> None:
    body = _request_json("DELETE", f"/delegations/{args.delegation_id}")
    print(f"代理権限を取り消しました: id={body['id']} revoked_at={body['revoked_at']}")


def _add_as_option(parser: argparse.ArgumentParser, help_text: str | None = None) -> None:
    """`--as <user>` を追加する。自分のキーで認証しつつ名義だけ切り替える(代理)。"""
    parser.add_argument(
        "--as",
        dest="act_as",
        default=None,
        metavar="USER",
        help=help_text
        or "指定userの名義で実行する(代理)。事前に delegate で権限登録が必要",
    )


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
    _add_as_option(send_parser)
    send_parser.set_defaults(func=cmd_send)

    check_parser = subparsers.add_parser("check", help="自分宛の未処理を確認する")
    _add_as_option(check_parser, "指定user宛を代理で閲覧する(statusは変更されない)")
    check_parser.set_defaults(func=cmd_check)

    edit_parser = subparsers.add_parser("edit", help="未読の自分の送信メッセージを編集する")
    edit_parser.add_argument("message_id", type=int, help="編集するメッセージのID")
    edit_parser.add_argument("content", help="新しい本文")
    edit_parser.set_defaults(func=cmd_edit)

    claim_parser = subparsers.add_parser(
        "claim", help="処理権を取る(代理処理する前に必ず実行し、二重処理を防ぐ)"
    )
    claim_parser.add_argument("message_id", type=int, help="処理権を取るメッセージのID")
    _add_as_option(claim_parser)
    claim_parser.set_defaults(func=cmd_claim)

    unclaim_parser = subparsers.add_parser(
        "unclaim", help="自分が取った処理権を解除する(中断して他に任せるとき)"
    )
    unclaim_parser.add_argument("message_id", type=int, help="処理権を解除するメッセージのID")
    _add_as_option(unclaim_parser)
    unclaim_parser.set_defaults(func=cmd_unclaim)

    done_parser = subparsers.add_parser(
        "done", help="処理完了したメッセージを「既読処理済」にする"
    )
    done_parser.add_argument("message_id", type=int, help="完了にするメッセージのID")
    _add_as_option(done_parser)
    done_parser.set_defaults(func=cmd_done)

    delegate_parser = subparsers.add_parser(
        "delegate", help="代理権限を登録する(自分が誰かの代理をする)"
    )
    delegate_parser.add_argument("principal", help="代理される側=名義 (例: TK)")
    delegate_parser.add_argument(
        "--from-user",
        dest="from_user",
        default=None,
        help="この送信者からのメッセージのみ代理可 (例: RC)。省略すると制限なし",
    )
    delegate_parser.add_argument(
        "--scopes",
        default="read,reply,done",
        help="許可する操作をカンマ区切りで (read/reply/done)。既定は全部",
    )
    delegate_parser.add_argument("--note", default=None, help="用途メモ")
    delegate_parser.set_defaults(func=cmd_delegate)

    delegations_parser = subparsers.add_parser(
        "delegations", help="自分が関わる代理権限を一覧する"
    )
    delegations_parser.add_argument(
        "--all", action="store_true", help="取消済みのものも表示する"
    )
    delegations_parser.set_defaults(func=cmd_delegations)

    revoke_parser = subparsers.add_parser("revoke", help="代理権限を取り消す")
    revoke_parser.add_argument("delegation_id", type=int, help="取り消す代理権限のID")
    revoke_parser.set_defaults(func=cmd_revoke)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
