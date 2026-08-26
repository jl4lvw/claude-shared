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
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib import error, request
from urllib.parse import urlencode

if sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
LOCAL_DIR = Path(__file__).resolve().parents[3] / "relay_local"
# 参照する .env は環境変数で差し替えられる(2026-08-05)。
# これが無いと「1つのクライアントで複数の名義を使い分ける」ことができず、
# 名義を分けたいだけでクライアント一式を複製する羽目になる(RC/TK間の論点)。
# .env の中身を切り替えのたびに書き換える運用は、並行実行時に
# **取り違えた名義で送信する**ため採らない。
# expanduser は Mac/Linux で "~/..." を設定ファイル等に書いた場合の保険
# (シェルの外で代入されると ~ が展開されないまま渡ってくる)
ENV_PATH = Path(os.environ.get("RELAY_ENV_FILE") or (LOCAL_DIR / ".env")).expanduser()
# 受信ファイルの置き場も名義ごとに分けられる。既定は .env と同じ場所の inbox なので、
# **別名義の .env を既存の .env と同じディレクトリに置くと inbox は分かれない**。
# 分けたいなら .env ごと別ディレクトリに置くか、RELAY_INBOX_DIR を明示する。
DOWNLOAD_DIR = Path(
    os.environ.get("RELAY_INBOX_DIR") or (ENV_PATH.parent / "inbox")
).expanduser()


def _fmt_local(value: object, with_date: bool = True) -> str:
    """サーバーの UTC ISO 文字列を、この端末の現地時刻にして返す。

    **切り出して表示してはいけない。** `value[:16]` のように書くと
    `+00:00` を剥がしただけの UTC が現地時刻の顔で出る（JST なら 9 時間ずれ）。
    PC の GUI と スマホ PWA は先に直してあり、CLI だけ残っていた（2026-08-23）。

    解釈できない値はそのまま返す（壊れた表示にするより原文の方が手掛かりになる）。
    タイムゾーンが無い値は現地時刻とみなす（古いデータへの保険）。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.strftime("%Y-%m-%d %H:%M" if with_date else "%H:%M")


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


def _setting(name: str) -> str:
    """設定を1つ読む。**環境変数が .env より優先**する。

    環境変数を上に置くのは、セッションごとに名義を切り替えられるようにするため。
    .env は従来どおり既定値として残るので、何も指定しなければ挙動は変わらない。
    """
    return (os.environ.get(name) or _ENV.get(name, "")).strip()


BASE_URL = _setting("RELAY_BASE_URL").rstrip("/")
API_KEY = _setting("RELAY_API_KEY")
SELF_USER_ID = _setting("RELAY_SELF_USER_ID")


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
            f"({SCRIPT_DIR / '.env.example'} をコピー)。"
            f"\n別名義で使う場合は、環境変数で直接指定するか"
            f" RELAY_ENV_FILE で別の .env を指すこともできます"
            f"(環境変数が .env より優先)。",
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
    holder: Optional[str] = None,
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
    # スレッド予約(2026-08-04 /r#28)のクライアント識別子。
    # APIキー(=actor)は常駐GUIもこのCLIも同じなので、これが無いと両者を区別できない。
    # 未指定なら「予約中スレッドは見えない」側に倒れる(fail safe)。
    if holder:
        req.add_header("X-Relay-Holder", holder)
    # 常駐GUIのエージェントとして動いているなら名乗る(2026-08-25)。
    # `human_only` のメッセージはエージェントには見せない。
    # 運用者の `/m` はこの環境変数を持たないので従来どおり見える
    if os.environ.get("RELAY_AGENT", "").strip().lower() in ("1", "true", "yes"):
        req.add_header("X-Relay-Agent", "1")
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


def _fmt_span(seconds: int) -> str:
    """秒を人が読める長さにする。

    「900秒」では長いのか短いのか判らない。予約の要点は**いつ切れるか**
    なので、そこが一目で判る書き方にする(2026-08-24)。
    """
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}秒"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"約{minutes}分" if sec else f"{minutes}分"
    hours, minutes = divmod(minutes, 60)
    if minutes:
        return f"約{hours}時間{minutes}分"
    return f"{hours}時間"


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
    if getattr(args, "human_only", False):
        payload["human_only"] = True

    # 返信の受け取り手を、送信と同じ操作で予約する(2026-08-04 /r#28)。
    # 送信してから別コマンドで予約すると、その隙に返信が届いて常駐GUIに攫われる。
    # サーバー側では同一トランザクションで処理されるため、この窓が生じない。
    holder: Optional[str] = None
    if getattr(args, "reserve", False):
        holder = getattr(args, "holder", None) or _new_holder()
        payload["reserve_holder"] = holder
        payload["reserve_ttl_sec"] = args.reserve_ttl

    act_as = getattr(args, "act_as", None)
    message = _request_json("POST", "/messages", payload=payload, act_as=act_as)
    name = f"{act_as}名義(実行はあなた)" if act_as else "自分名義"
    print(f"送信しました[{name}]: thread_id={message['thread_id']} message_id={message['id']}")
    # **毎回、返信を誰が受け取るかを言う**(2026-08-24)。
    # 予約はオプトインで、忘れても何も起きず黙って常駐GUIが拾う。
    # 実際に「委託した作業の返信をGUIが勝手に処理する」が起きた。
    # 警告にしないのは、警告は慣れると読み飛ばされるから。事実を毎回置く。
    if holder:
        print(
            f"返信の受け取り手: このセッション"
            f"(holder={holder} / {_fmt_span(args.reserve_ttl)}で自動解放)"
        )
        print("このセッションで返信を受け取るには、続けて次を実行してください:")
        print(
            f"  relay_client.py wait --thread {message['thread_id']} "
            f"--holder {holder} --after-id {message['id']}"
        )
        print(
            "  relay_client.py release --thread "
            f"{message['thread_id']} --holder {holder}   # 終わったら必ず"
        )
    elif getattr(args, "human_only", False):
        # **AI が触らない件は「誰が受け取るか」の答えが違う。**
        # 常駐GUIと書くと嘘になる(2026-08-25)
        print(
            "返信の受け取り手: 相手の運用者(AIの自動応答を禁止しました)。"
            "相手は /m で扱います"
        )
    else:
        print(
            "返信の受け取り手: 常駐GUI(このセッションでは受け取りません)。"
            "自分で受け取るなら --reserve を付けて送り直すか、"
            f"reserve --thread {message['thread_id']} で今から予約してください"
        )

    if args.file:
        file_path = Path(args.file)
        if not file_path.exists():
            print(f"ファイルが見つかりません: {file_path}", file=sys.stderr)
            sys.exit(1)
        file_info = _upload_file(message["thread_id"], message["id"], file_path)
        print(f"添付しました: file_id={file_info['file_id']} filename={file_info['filename']}")


# 確認(ask)は受信箱とは別管理で、発端メッセージが done になると
# `GET /messages` からは消える。そのため **check だけを見ていると
# 未回答の確認に永久に気づけない**(2026-08-21 運用者指摘)。
_ASK_STATE_LABEL = {
    "awaiting_operator": "回答待ち",
    "answer_sent": "回答済み(パソコンの反映待ち)",
    "answer_stuck": "回答がパソコンに届いていません",
    "failed": "AIが失敗しました",
    "escalated": "AIが判断を諦めました",
    "working": "AIが処理中",
}


# サーバーの `_CLI_ANSWERABLE_KINDS` と揃える。増やすときは両方直す
_CLI_ANSWERABLE_KINDS = ("question",)


def _print_open_asks(act_as: Optional[str] = None) -> None:
    """自分宛の「いま出ている確認」を表示する。**0件なら何も出さない。**

    代理閲覧(--as)のときは出さない。確認は名義ごとの持ち物で、
    代理の受信箱に混ぜると誰が答えるべきか分からなくなる。
    """
    if act_as:
        return
    try:
        data = _request_json("GET", "/agent/attention")
    except SystemExit:
        # 旧サーバー(この口が無い)。check 本来の仕事は済んでいるので黙って戻る
        return
    except Exception:
        return
    items = [i for i in (data or {}).get("items", []) if i.get("needs_you")]
    if not items:
        return
    print("=" * 60)
    print(f"■ 未回答の確認が {len(items)} 件あります(受信箱とは別枠です)")
    for it in items:
        label = _ASK_STATE_LABEL.get(it.get("state") or "", it.get("state") or "")
        origin = it.get("origin_status")
        note = ""
        if origin == "done":
            note = "  ※ 発端メッセージは完了済み(この確認は取り残されています)"
        elif it.get("origin_deleted"):
            note = "  ※ 発端メッセージは削除済み"
        print("-" * 60)
        # 発端は「あれば付く」もの。特定できない確認もここに出る(2026-08-23)
        oid = it.get("origin_msg_id")
        where = f"message_id={oid}" if oid else "message_id=(不明)"
        who = it.get("from_user")
        print(f"[{label}] {where}"
              + (f" from={who}" if who else "")
              + f" ask_key={it.get('ask_key')}")
        if note:
            print(note)
        body = (it.get("question") or "").strip()
        if body:
            print(body)
        for i, opt in enumerate(it.get("options") or [], 1):
            mark = " ←推奨" if opt == it.get("recommended") else ""
            print(f"  {i}. {opt}{mark}")
        if it.get("reason"):
            print(f"  推奨理由: {it['reason']}")
        # **答えられない確認に「答えてください」と書かない。**
        # サーバーは question 以外を 403 で拒む(承認は机の前でしか通さない)
        # のに、以前は全件に同じ案内を出していた
        # (2026-08-22 cgd Lv8 で codex med/high が収束)
        if (it.get("ask_kind") or "question") not in _CLI_ANSWERABLE_KINDS:
            print("  ※ この確認はここからは回答できません"
                  "(パソコンの確認パネルで対応してください)")
    print("-" * 60)
    answerable = [i for i in items
                  if (i.get("ask_kind") or "question") in _CLI_ANSWERABLE_KINDS]
    if answerable:
        print("答えるとき: relay_client.py answer <ask_key> \"回答の本文\"")
    else:
        print("この一覧はパソコンの確認パネルで対応してください")


def cmd_answer(args: argparse.Namespace) -> None:
    """確認に CLI から答える。常駐GUIの確認パネルを待たずに引き取れる。

    回答の置き場はスマホから答えたときと同じ(サーバーに積み、常駐GUIが
    取りに来て待機中のエージェントへ渡す)。**危険操作の承認は答えられない** —
    パソコンの承認パネルで行う。
    """
    data = _request_json(
        "POST",
        "/agent/attention/answer",
        payload={"ask_key": args.ask_key, "answer": args.answer},
    )
    print(f"回答しました: ask_key={data.get('ask_key')}")
    print("常駐GUIが取りに来て、待っている処理へ渡します(最大60秒)。")


def _is_agent_self() -> bool:
    """自分が常駐GUIのエージェントとして動いているか。"""
    return os.environ.get("RELAY_AGENT", "").strip().lower() in ("1", "true", "yes")


def _handoff_messages(no: int, act_as=None) -> object:
    """引き継がれた会話の未処理メッセージを取る。取れなければ None を返す。

    **受信箱(GET /messages)からは取れない。** 引き継ぎ中のスレッドは lease で
    隠れており、しかも holder は1つしか送れないので、引き継ぎが2件あると
    片方は必ず隠れたままになる(本番で #103/#104 が同時に立っていた)。
    サーバーに「その引き継ぎの持ち主だから読める」専用の口を用意してある。
    """
    # **soft_status で吸収する。** _request_json は HTTPError で sys.exit(1)
    # するので、404 を返す旧サーバーだと check 全体がそこで終了してしまう
    # (SystemExit は BaseException なので except Exception では拾えない)。
    # 2026-08-26 Step C 🟠
    try:
        got = _request_json(
            "GET", f"/handoffs/{no}/messages", act_as=act_as,
            soft_status=(403, 404),
        )
    except BaseException as exc:  # noqa: BLE001 表示の付加情報なので本体は止めない
        print(f"    ⚠ 本文を取得できませんでした({type(exc).__name__}: {exc})")
        return None
    if isinstance(got, ApiConflict):
        print(f"    ⚠ 本文を取得できませんでした(HTTP {got.status})")
        print(f"    サーバーが古い可能性があります。着手して読む: "
              f"relay_client.py handoff takeover {no}")
        return None
    return got


def _print_handoffs(act_as=None) -> int:
    """自分に引き継がれた会話を、**本文まで**出す。件数を返す。

    引き継ぎ中のスレッドは lease で受信箱から隠れるので、`/m` は
    「未処理はありません」と言う。そこで黙ると、引き継がれた会話が
    永久に見つからない(2026-08-24 運用者の問いへの対応)。

    2026-08-26 に本文表示を足した。存在だけ出しても本文が読めなければ
    着手できず、実際に RC からの依頼5件が12時間放置された。

    **取得に失敗したら黙らない。** 「引き継ぎはありません」と
    区別が付かなくなるのが、この事故の本体だった。
    """
    # エージェントには見せない。一覧には会話の抜粋と takeover 手順が載るので、
    # 人へ渡したはずの会話を奪えてしまう(2026-08-26 Lv8 V4)
    if _is_agent_self():
        return 0
    try:
        body = _request_json("GET", "/handoffs", act_as=act_as)
    except Exception as exc:  # noqa: BLE001
        print("=" * 60)
        print(f"⚠ 引き継ぎ状態を確認できません({type(exc).__name__}: {exc})")
        print("  「引き継ぎは無い」ではありません。サーバーに繋がらないだけの可能性があります")
        print("=" * 60)
        return 0
    active = [h for h in (body.get("active") or [])]
    if not active:
        return 0
    print("=" * 60)
    print(f"■ あなたに引き継がれた会話が {len(active)} 件あります(未処理より先に扱ってください)")
    print("-" * 60)
    for h in active:
        status_ja = {
            "pending": "未着手",
            "in_session": "対応中",
            "taken": "対応中",
        }.get(h.get("status"), h.get("status"))
        print(
            f"[{status_ja}] #{h['handoff_no']:03d} thread={h['thread_id']}"
            f" 発行={_fmt_local(h['created_at'])}"
        )
        summary = (h.get("summary") or "").strip()
        if summary:
            for line in summary.splitlines()[:4]:
                print(f"    {line[:110]}")
        msgs = _handoff_messages(h["handoff_no"], act_as)
        if msgs is not None:
            if msgs:
                print(f"    --- 未処理 {len(msgs)} 件 ---")
                for m in msgs:
                    print(
                        f"    [#{m['id']}] {m['from_user']} "
                        f"{_fmt_local(m['created_at'])}"
                    )
                    for line in str(m.get("content") or "").splitlines():
                        print(f"      {line}")
            else:
                print("    (このスレッドに未処理はありません)")
        if h.get("status") == "pending":
            print(
                f"    着手する: relay_client.py handoff takeover {h['handoff_no']}"
            )
        else:
            print(
                f"    終えたら: relay_client.py handoff done {h['handoff_no']}"
            )
    print("=" * 60)
    return len(active)


def cmd_check(args: argparse.Namespace) -> None:
    # GET /messages(自分宛の受信箱取得)は「未処理すべて」(未読 + 取得済みだがdone
    # されていないもの)を返す(2026-07-20サーバー仕様変更)。自動ポーリングが先に取得
    # していても、doneするまで毎回表示されるため処理漏れが起きない。
    # 取得と同時にサーバー側でunread→processing(取得済み)へ自動遷移する。
    act_as = getattr(args, "act_as", None)
    peek = getattr(args, "peek", False)
    target = act_as or SELF_USER_ID
    # holder未指定なら、他クライアントが予約中のスレッドはサーバー側で除外される。
    # 常駐GUIの内蔵エージェントもこの check を通るため、ここで受け取らないことが
    # 「予約したセッションが返信を受け取る」の実効性を担保している
    params = {"to": target, "unread": "true"}
    if peek:
        # peek=true はサーバー側でunread→processingへの遷移を起こさない
        # (自動ポーリング向け。2026-07-20仕様、wait コマンドでも同様に使用)
        params["peek"] = "true"
    # **引き継ぎを本当に一番上に出す。** 以前はコメントだけ「一番上」と言い、
    # 実装は /messages の後だった(2026-08-26 Lv8 Codex両者収束)。
    # 受信箱の取得が失敗した回は引き継ぎも出ないままだった
    handoff_count = _print_handoffs(act_as)
    messages = _request_json(
        "GET",
        "/messages",
        params=params,
        act_as=act_as,
        holder=getattr(args, "holder", None),
    )
    if not messages:
        who = f"{target}宛(代理)" if act_as else "自分宛"
        if handoff_count:
            # **「ありません」と言い切らない。** 引き継ぎは受信箱から隠れる
            # 仕組みなので、ここで言い切ると画面が嘘をつくことになる
            print(
                f"{who}の受信箱に未処理はありません"
                f"(引き継ぎ {handoff_count} 件は上に出ています。そちらを扱ってください)。"
            )
        else:
            print(f"{who}の未処理メッセージはありません。")
        _print_open_asks(act_as)
        return
    if act_as:
        print(f"※ {act_as}宛を代理で閲覧しています(statusは変更されません)")
    if peek:
        print("※ --peek: 副作用なしで確認しています(statusは変更されません)")

    status_labels = {"unread": "新着", "processing": "未処理(以前取得済み)"}
    for msg in messages:
        label = status_labels.get(msg.get("status", ""), msg.get("status", ""))
        print("=" * 60)
        print(
            f"[{msg['type']}][{label}] thread_id={msg['thread_id']} message_id={msg['id']} "
            f"from={msg['from_user']} at={_fmt_local(msg['created_at'])}"
        )
        # **AI の自動応答が禁止された件**(2026-08-25)。常駐GUIのエージェントには
        # 見えないので、ここに出ている時点で「人が扱う」ものだと判る。
        # それでも明示する — 代理で AI に投げ返す誘惑を断つため
        if msg.get("human_only"):
            print("  ※ AIの自動応答は禁止されています。あなた(運用者)が判断してください")
        # 他の実行者が処理権を持っているものは触らない(二重処理防止)
        claimed_by = msg.get("claimed_by")
        if claimed_by:
            # claimed_by は実行者(APIキーの持ち主)なので、代理でも自分なら SELF_USER_ID
            note = (
                "自分が処理中"
                if claimed_by == SELF_USER_ID
                else f"{claimed_by} が処理中 → 手を出さない"
            )
            print(f"  ※ 処理権: {note} (claimed_at={_fmt_local(msg.get('claimed_at'))})")
        print(msg["content"])

        files = _request_json(
            "GET", "/files", params={"message_id": str(msg["id"])}, act_as=act_as
        )
        for file_info in files or []:
            dest = _download_file(file_info["file_id"], file_info["filename"])
            print(f"  添付ダウンロード: {dest}")

    print("=" * 60)
    print(f"{len(messages)}件の未処理メッセージがあります(doneするまで毎回表示されます)。")
    _print_open_asks(act_as)
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
    print(f"編集しました: message_id={message['id']} "
          f"edited_at={_fmt_local(message['edited_at'])}")


def cmd_done(args: argparse.Namespace) -> None:
    act_as = getattr(args, "act_as", None)
    message = _request_json(
        "POST", f"/messages/{args.message_id}/done", act_as=act_as
    )
    print(f"既読処理済にしました: message_id={message['id']} "
          f"completed_at={_fmt_local(message['completed_at'])}")


def cmd_forward(args: argparse.Namespace) -> None:
    """自分宛のメッセージを別名義へ転送し、スレッドごと引き継がせる。

    転送先は**自分の名義のまま**原送信者と同じスレッドで続けられる。
    自分は担当から外れる(未処理・予約が外れる)が、履歴としてスレッドは残る。
    """
    act_as = getattr(args, "act_as", None)
    body: dict[str, Any] = {"to": args.to}
    if args.note:
        body["note"] = args.note
    result = _request_json(
        "POST", f"/messages/{args.message_id}/forward", body, act_as=act_as
    )
    print(
        f"引き継ぎました: #{args.message_id} → {result['to_user']} "
        f"(thread={result['thread_id']} 転送メッセージ=#{result['forward_message_id']})"
    )
    print(f"以後このスレッドは {result['to_user']} が担当します。あなたの受信箱からは外れました。")


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
        f"claimed_by={result['claimed_by']} at={_fmt_local(result['claimed_at'])}"
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
    print(f"代理権限を取り消しました: id={body['id']} "
          f"revoked_at={_fmt_local(body['revoked_at'])}")


# --- スレッド予約と返信待ち受け(2026-08-04 /r#28) ------------------------
# 常駐GUI「AIリレー コンソール」と、この CLI を使う Claude Code セッションは
# 同じ受信箱(to=自分)を見ている。何もしなければ、送った相手からの返信は
# 先にポーリングしたGUIが攫っていき、発信元セッションではラリーを続けられない。
#
# そこで「このスレッドの返信は自分が受け取る」とサーバーに予約(lease)を立てる。
# 予約の持ち主(holder)はAPIキーの持ち主ではなくクライアント識別子で、
# GUIもCLIも同じキーで動く以上、これが無いと両者を区別できない。

_WAIT_TIMEOUT_EXIT = 3   # 返信が来ないまま待ち時間切れ(異常終了1と区別する)
_WAIT_LOST_EXIT = 4      # 予約を失った(期限切れ後に他が取得した等)


def _new_holder() -> str:
    """このセッション用のクライアント識別子を作る。"""
    return f"cli:{uuid.uuid4().hex[:12]}"


def _touch_lease(
    thread_id: str, holder: str, ttl_sec: int, act_as: Optional[str] = None
) -> Optional[dict[str, Any]]:
    """予約を取得・延長する。他holderに取られていれば None を返す。"""
    result = _request_json(
        "POST",
        f"/threads/{thread_id}/lease",
        payload={"holder": holder, "ttl_sec": ttl_sec, "pid": os.getpid()},
        act_as=act_as,
        soft_status=(409,),
    )
    if isinstance(result, ApiConflict):
        return None
    return result


def cmd_reserve(args: argparse.Namespace) -> None:
    """スレッドを予約する(既に自分が持っていれば延長)。"""
    holder = args.holder or _new_holder()
    act_as = getattr(args, "act_as", None)
    lease = _touch_lease(args.thread, holder, args.ttl, act_as)
    if lease is None:
        print(
            f"他のクライアントがこのスレッドを予約中です: thread_id={args.thread}",
            file=sys.stderr,
        )
        sys.exit(_WAIT_LOST_EXIT)
    print(f"予約しました: thread_id={args.thread} holder={holder}")
    print(f"  期限: {_fmt_local(lease['expires_at'])} (待ち受け中は自動で延長されます)")
    print(f"  返信を待つ: relay_client.py wait --thread {args.thread} --holder {holder}")


def cmd_release(args: argparse.Namespace) -> None:
    """予約を解放して常駐GUIに戻す。予約が無くても成功する(冪等)。"""
    body = _request_json(
        "DELETE",
        f"/threads/{args.thread}/lease",
        params={"holder": args.holder},
        act_as=getattr(args, "act_as", None),
    )
    if body and body.get("released"):
        print(f"予約を解放しました: thread_id={args.thread}(常駐GUIが引き取れる状態に戻りました)")
    else:
        print(f"予約はありませんでした: thread_id={args.thread}")


def cmd_handoff(args: argparse.Namespace) -> None:
    """GUIから引き継いだ会話への着手・完了・差し戻し・一覧。

    takeover はサーバー側で lease holder を**原子的に**自分へ付け替える。
    release→再取得の2段にしないのは、その隙間にGUIの巡回が滑り込むため。
    """
    sub = args.handoff_cmd
    if sub == "list":
        body = _request_json("GET", "/handoffs")
        active = body.get("active") or []
        if not active:
            print("有効な引き継ぎはありません")
        for h in active:
            print(
                f"#{h['handoff_no']} [{h['status']}] thread={h['thread_id']}"
                f" 発行={_fmt_local(h['created_at'])} "
                f"{h['summary'][:60] if h.get('summary') else ''}"
            )
        for h in body.get("recent") or []:
            print(f"  (済) #{h['handoff_no']} [{h['status']}] {h.get('finish_note') or ''}")
        return
    no = args.number
    if sub == "takeover":
        holder = getattr(args, "holder", None) or _new_holder()
        body = _request_json(
            "POST", f"/handoffs/{no}/takeover", payload={"holder": holder}
        )
        peers = "・".join(body.get("peers") or []) or "?"
        print(f"引き継ぎ #{no} に着手しました(状態: 対応中)")
        print(f"  thread_id={body['thread_id']} 相手={peers} holder={holder}")
        if body.get("summary"):
            print("---- GUIからの要約 ----")
            print(body["summary"])
            print("----------------------")
        print("続け方:")
        # `check` に `--thread` は無い。しかも `--holder` を付けないと
        # 予約中スレッドはサーバー側で除外されて見えない(2026-08-23)
        print(
            f"  経緯の取得: python {Path(__file__).name} check --peek"
            f" --holder {holder}"
        )
        print(
            f"  返信: python {Path(__file__).name} send --to {peers.split('・')[0]}"
            f" --thread {body['thread_id']} --holder {holder} <本文>"
        )
        print(
            f"  返信待ち(予約維持): python {Path(__file__).name} wait"
            f" --thread {body['thread_id']} --holder {holder}"
        )
        print(f"  完了: python {Path(__file__).name} handoff done {no}")
        print(f"  戻す: python {Path(__file__).name} handoff return {no} --reason \"理由\"")
        print("⚠ ctx台帳に holder と thread_id を必ず記録すること(圧縮で失うと延長も解放もできない)")
        return
    if sub in ("done", "return"):
        note = getattr(args, "reason", "") or getattr(args, "note", "") or ""
        body = _request_json(
            "POST", f"/handoffs/{no}/{'done' if sub == 'done' else 'return'}",
            payload={"note": note},
        )
        label = "完了" if sub == "done" else "差し戻し"
        print(f"引き継ぎ #{no} を{label}にしました(常駐GUIが引き取れる状態に戻りました)")
        return
    print("handoff のサブコマンド: takeover / done / return / list", file=sys.stderr)
    raise SystemExit(2)


def _lease_left_sec(
    thread_id: str, holder: str, act_as: "str | None" = None
) -> "int | None":
    """自分の予約の残り秒。判らなければ None。

    表示のためだけに使う。**取れなくても処理は続ける**
    (残り時間が出せないことは、予約を失ったことを意味しない)。
    """
    try:
        rows = _request_json("GET", "/threads/leases", act_as=act_as)
    except Exception:
        return None
    for row in rows or []:
        if row.get("thread_id") == thread_id and row.get("holder") == holder:
            try:
                expires = datetime.fromisoformat(str(row["expires_at"]))
            except (ValueError, KeyError, TypeError):
                return None
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            return max(0, int((expires - datetime.now(timezone.utc)).total_seconds()))
    return None


def cmd_wait(args: argparse.Namespace) -> None:
    """このスレッドへの返信が来るまで待ち、来たら表示する。

    取得は peek=true(副作用なし)で行う。status=unread で待つと、常駐GUIや
    手動checkが一瞬でも先に触った瞬間 processing に進んで見えなくなるため
    (cgd Lv7 で Codex medium/high が独立に指摘)。
    """
    holder = args.holder
    act_as = getattr(args, "act_as", None)
    target = act_as or SELF_USER_ID
    params: dict[str, str] = {
        "to": target,
        "peek": "true",
        "thread_id": args.thread,
    }
    if args.after_id is not None:
        params["after_id"] = str(args.after_id)

    deadline = time.monotonic() + args.timeout
    print(
        f"返信を待っています: thread_id={args.thread} holder={holder} "
        f"(最大{args.timeout}秒 / {args.interval}秒間隔)"
    )
    while True:
        # 待っている間はTTLを延ばし続ける。延長できない=予約を失った合図
        if _touch_lease(args.thread, holder, args.ttl, act_as) is None:
            print(
                "予約を失いました(他のクライアントが取得済み)。"
                "常駐GUIがこのスレッドを処理している可能性があります。",
                file=sys.stderr,
            )
            sys.exit(_WAIT_LOST_EXIT)

        messages = _request_json(
            "GET", "/messages", params=params, act_as=act_as, holder=holder
        )
        if messages:
            # 受け取った後も返信を書き終えるまで予約を保持する必要がある。
            # wait が返った瞬間にheartbeatが止まると、返信作成中にTTLが切れて
            # 常駐GUIが同じスレッドに二重返信しうる(cgd Lv7 DS critic 指摘)。
            # 延長できたかは必ず確認する — 失敗を黙って成功と表示すると、
            # 二重返信防止という中心要件が静かに破れる(Step C Codex指摘)
            held = _touch_lease(args.thread, holder, args.hold, act_as) is not None
            for msg in messages:
                print("=" * 60)
                print(
                    f"[{msg['type']}] message_id={msg['id']} from={msg['from_user']} "
                    f"at={_fmt_local(msg['created_at'])}"
                )
                print(msg["content"])
            print("=" * 60)
            if held:
                print(f"{len(messages)}件の返信を受け取りました(予約は{args.hold}秒延長済み)。")
            else:
                print(
                    f"{len(messages)}件の返信を受け取りましたが、"
                    "**予約の延長に失敗しました**。返信を書いている間に常駐GUIが"
                    "同じスレッドを処理する可能性があります。すぐに返信するか、"
                    f"reserve --thread {args.thread} --holder {holder} で取り直してください。",
                    file=sys.stderr,
                )
            print("続けてやり取りする場合:")
            print(
                f"  relay_client.py send --to {messages[0]['from_user']} "
                f"--thread {args.thread} --type reply \"本文\""
            )
            print("処理を終えたら、必ず消し込みと予約解放を行ってください:")
            for msg in messages:
                print(f"  relay_client.py done {msg['id']}")
            print(f"  relay_client.py release --thread {args.thread} --holder {holder}")
            return

        if time.monotonic() >= deadline:
            if getattr(args, "keep", False):
                # **手放さずに戻る**(2026-08-24)。委託した作業の返事は
                # 数時間後に来る。そこで解放すると、その瞬間に常駐GUIが
                # 拾ってしまい「勝手に応答された」になる。
                # 予約自体には TTL があるので、放置しても必ずいつかは戻る。
                left = _lease_left_sec(args.thread, holder, act_as)
                print(
                    f"返信がないまま{args.timeout}秒が経過しました。"
                    "予約は保持したままです"
                    + (f"(残り{_fmt_span(left)})" if left is not None else "")
                    + "。放置すると期限切れで常駐GUIが引き取ります。",
                    file=sys.stderr,
                )
                print(
                    f"  relay_client.py wait --thread {args.thread} "
                    f"--holder {holder} --keep   # 続けて待つ",
                    file=sys.stderr,
                )
                print(
                    f"  relay_client.py release --thread {args.thread} "
                    f"--holder {holder}   # 常駐GUIへ返す",
                    file=sys.stderr,
                )
                sys.exit(_WAIT_TIMEOUT_EXIT)
            # 待ち切れなかったら予約を返す。持ったまま放置すると、
            # 誰も見ないメッセージが生まれる(このシステムが一番避けたい状態)。
            # 直前のheartbeatからここまでの間に他へ移っていると409になるが、
            # 「もう自分の手元に無い」だけなので後始末としては成功扱いでよい
            # (Step C Codex指摘: 通常エラー終了だと終了コード3を返せない)
            _request_json(
                "DELETE",
                f"/threads/{args.thread}/lease",
                params={"holder": holder},
                act_as=act_as,
                soft_status=(409,),
            )
            print(
                f"返信がないまま{args.timeout}秒が経過しました。"
                "予約を解放したので、以降は常駐GUIが引き取ります。",
                file=sys.stderr,
            )
            sys.exit(_WAIT_TIMEOUT_EXIT)
        time.sleep(args.interval)


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
        "--human-only",
        action="store_true",
        help="AIの自動応答を禁止し、相手の運用者が /m で扱うことを強制する。"
        "常駐GUIのエージェントからは見えなくなるが、画面と件数には出る",
    )
    send_parser.add_argument(
        "--no-reply-needed",
        action="store_true",
        help="相手からの返信・確認を要さないメッセージ(お礼・完了報告等)に付与する。"
        "定時LineWorks通知やビューアの放置⚠️バッジの対象から除外される",
    )
    send_parser.add_argument(
        "--reserve",
        action="store_true",
        help="返信の受け取り手をこのセッションとして予約する。"
        "付けないと、返信は常駐GUIが先に拾って処理する",
    )
    send_parser.add_argument(
        "--holder",
        default=None,
        help="--reserve時のクライアント識別子。省略すると自動採番して表示する",
    )
    send_parser.add_argument(
        "--reserve-ttl", type=int, default=900, help="--reserve時の予約TTL秒(既定900)"
    )
    _add_as_option(send_parser)
    send_parser.set_defaults(func=cmd_send)

    check_parser = subparsers.add_parser("check", help="自分宛の未処理を確認する")
    check_parser.add_argument(
        "--holder",
        default=None,
        help="自分が予約中のスレッドも見る場合に指定する。"
        "省略すると予約中スレッドは表示されない(常駐GUIとの二重処理を防ぐため)",
    )
    check_parser.add_argument(
        "--peek",
        action="store_true",
        help="副作用なしで確認する(unread→processingへの遷移を起こさない)。"
        "人間が読まない自動ポーリング(スケジュールタスク等)向け。"
        "変化があれば通常のcheck(--peek無し)で本取得すること",
    )
    _add_as_option(check_parser, "指定user宛を代理で閲覧する(statusは変更されない)")
    check_parser.set_defaults(func=cmd_check)

    answer_parser = subparsers.add_parser(
        "answer", help="未回答の確認に答える(ask_key は check で表示される)"
    )
    answer_parser.add_argument("ask_key", help="check で表示された ask_key")
    answer_parser.add_argument("answer", help="回答の本文(選択肢はそのまま書く)")
    answer_parser.set_defaults(func=cmd_answer)

    reserve_parser = subparsers.add_parser(
        "reserve", help="スレッドを予約する(返信を常駐GUIでなく自分が受け取る)"
    )
    reserve_parser.add_argument("--thread", required=True, help="予約するthread_id")
    reserve_parser.add_argument(
        "--holder", default=None, help="クライアント識別子。省略すると自動採番"
    )
    reserve_parser.add_argument("--ttl", type=int, default=900, help="予約TTL秒(既定900)")
    _add_as_option(reserve_parser)
    reserve_parser.set_defaults(func=cmd_reserve)

    release_parser = subparsers.add_parser(
        "release", help="予約を解放して常駐GUIに戻す(処理が終わったら必ず実行する)"
    )
    release_parser.add_argument("--thread", required=True, help="解放するthread_id")
    release_parser.add_argument("--holder", required=True, help="予約時のクライアント識別子")
    _add_as_option(release_parser)
    release_parser.set_defaults(func=cmd_release)

    wait_parser = subparsers.add_parser(
        "wait", help="予約したスレッドへの返信を待ち受ける(そのままラリーを続けるため)"
    )
    wait_parser.add_argument("--thread", required=True, help="待ち受けるthread_id")
    wait_parser.add_argument("--holder", required=True, help="予約時のクライアント識別子")
    wait_parser.add_argument(
        "--after-id",
        type=int,
        default=None,
        dest="after_id",
        help="このmessage_idより新しい返信だけを待つ(通常は自分が送ったmessage_id)",
    )
    wait_parser.add_argument("--timeout", type=int, default=900, help="待つ上限秒(既定900)")
    wait_parser.add_argument("--interval", type=int, default=5, help="確認間隔秒(既定5)")
    wait_parser.add_argument(
        "--keep",
        action="store_true",
        help="待ち切れなくても予約を解放しない。委託のように返事が数時間後に来る場合に使う(既定は解放して常駐GUIへ返す)",
    )
    wait_parser.add_argument(
        "--ttl", type=int, default=300, help="待ち受け中に維持する予約TTL秒(既定300)"
    )
    wait_parser.add_argument(
        "--hold",
        type=int,
        default=900,
        help="返信を受け取った後、返信を書き終えるまで予約を保つ秒数(既定900)",
    )
    _add_as_option(wait_parser)
    wait_parser.set_defaults(func=cmd_wait)

    handoff_parser = subparsers.add_parser(
        "handoff", help="GUIから引き継いだ会話への着手・完了・差し戻し"
    )
    handoff_sub = handoff_parser.add_subparsers(dest="handoff_cmd")
    ho_take = handoff_sub.add_parser("takeover", help="引き継ぎに着手する(lease付け替え)")
    ho_take.add_argument("number", type=int, help="引き継ぎ番号(3桁)")
    ho_take.add_argument("--holder", default=None, help="省略時は自動採番")
    ho_done = handoff_sub.add_parser("done", help="完了として記録しGUIへ返す")
    ho_done.add_argument("number", type=int)
    ho_done.add_argument("--note", default="", help="完了メモ")
    ho_ret = handoff_sub.add_parser("return", help="完了せずGUIへ差し戻す")
    ho_ret.add_argument("number", type=int)
    ho_ret.add_argument("--reason", default="", help="差し戻す理由")
    handoff_sub.add_parser("list", help="引き継ぎ一覧")
    handoff_parser.set_defaults(func=cmd_handoff)

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

    forward_parser = subparsers.add_parser(
        "forward", help="自分宛のメッセージを別名義へ転送し、スレッドごと引き継がせる"
    )
    forward_parser.add_argument("message_id", type=int, help="転送するメッセージのID")
    forward_parser.add_argument("--to", required=True, help="引き継ぐ相手のuser_id (例: RCS)")
    forward_parser.add_argument("--note", help="転送時に添える一言(引き継ぐ理由など)")
    _add_as_option(forward_parser)
    forward_parser.set_defaults(func=cmd_forward)

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
