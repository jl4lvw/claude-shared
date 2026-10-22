"""LINE グループへ「状況説明の画像のURL つきの通知」を送る道具(送信前に、寺下さんの1対1トークで承認をもらう)。

LINE(通常の LINE。LINE WORKS ではない)の Messaging API で、090.LINE業務指示エージェントのボット
(FujiWorks)の名義で送る。画像は LINE の仕様で「公開されたURL」が要るため、
pub.sfuji.f5.si(自社の公開置き場・ログイン不要・一覧なし)に画像を1枚置き、そのURLをテキストに添える。

  python line_notify.py groups                                  登録済みのグループ名を一覧
  python line_notify.py register --name 名前 --group-id Cxxxx   グループIDを登録(090/data/line_groups.json・Git対象外)
  python line_notify.py publish 画像.png [--prefix g0964]        画像を公開置き場へ置き、URLを表示(送信はしない)
  python line_notify.py request-approval --group 名前 --text-file 本文.txt --url URL [--mention 表示名]
        → 寺下さんの1対1トークへ、下書き(全文・画像・「送信する」「中止」ボタン)を送り、確認コードを表示
        --mention: 先頭に「@表示名 さん」のメンションを付ける(send にも同じ値を渡す。相手も承認の照合に入る)
  python line_notify.py wait-approval コード [--timeout-min 30]
        → LINE での返信を待つ。終了コード 0=承認 / 10=中止 / 11=修正の依頼(本文を表示) / 12=期限切れ / 13=時間切れ(まだ待ち)
  python line_notify.py send --group 名前 --text-file 本文.txt --url URL --approval コード            確認のみ
  python line_notify.py send --group 名前 --text-file 本文.txt --url URL --approval コード --send     グループへ送信

**グループへの送信は、1対1トークで承認された全文だけ**。承認コードが「承認済み」で、送信先・本文(sha8)が承認時と
一致し、未使用で、期限内のときだけ送れる。送ったら承認コードは使用済みになり、二度は使えない。

2026-10-20 運用者決定: 下書きの承認(1対1トーク)は LINE WORKS の承認トークへ送る(グループへの投稿は LINE のまま)。
本文は全文(長ければ分けて送る)・画像は URL の文字で出す。押されたボタン・文の返信(「OK コード」「中止 コード」
「コード 直してほしいこと」)の判定は端末 A の見張り役(a_browser_watch)が行うので、見張り役が止まっていれば承認を求めない。
LINE で承認する送り方も残してあり、900.ClaudeCode/a_browser_common/approval_channel.py で戻せる。

2026-10-20 運用者決定(夜間の抑止): 19:00〜翌 9:00 は LINE グループへ送らない(全グループ。quiet_hours.json)。
  request-approval をこの間に呼ぶと、承認を求めずにキューに積んで終了コード 20 で終わる。
  send は、19:00 より前に承認されたものは送る。19:00 以降に承認されたものは送らずに溜める(終了コード 20)。
  溜めたものは毎朝 9:00 のタスク(LineNotifyMorningFlush → flush)が 1 件ずつ承認を求め、承認されたら送る。
  python line_notify.py queue-list [--all]        溜まっている報告の一覧
  python line_notify.py queue-cancel 番号          溜まっている報告を取り消す(送らない)
  python line_notify.py flush                      朝のまとめ送り(普段はタスクが起動する)
  python line_notify.py install-flush-task         毎朝 9:00 のタスクを登録 / uninstall-flush-task で外す
"""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

if sys.stdout is not None:  # pythonw(朝のまとめ送りのタスク)では標準出力が無い
    sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[3]
AGENT = ROOT / "090.LINE業務指示エージェント"
GROUPS = AGENT / "data" / "line_groups.json"
LOG = AGENT / "data" / "line_notify_log.jsonl"
PUBLIC = ROOT / "900.ClaudeCode" / "public-share"
if str(ROOT / "900.ClaudeCode") not in sys.path:  # 承認の出し先の切り替えと LINE WORKS の送り方(a_browser_common)
    sys.path.insert(0, str(ROOT / "900.ClaudeCode"))
from a_browser_common import approval_channel, approval_purpose, approval_text, lineworks, phone_text  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
import notify_queue  # noqa: E402
QUEUE_DB = AGENT / "data" / "line_notify_queue.sqlite3"
FLUSH_LOG = AGENT / "data" / "line_notify_flush.log"
FLUSH_TASK = "LineNotifyMorningFlush"
PUBLIC_HOST = "pub.sfuji.f5.si"
MAX_LEN = 4500
TTL_HOURS = 24
PREVIEW_MAX = 1024 * 1024  # LINE の previewImageUrl は 1MB まで
CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 紛らわしい文字(0/O・1/I)を除く
EXIT = {"approved": 0, "rejected": 10, "revise": 11, "expired": 12, "pending": 13, "used": 14, "queued": 20}


def now_dt():
    """いまの時刻(テストで差し替える)。"""
    from datetime import datetime
    return datetime.now()


def hold(group: str, text: str, url: str, mention: str, sha: str, code: str | None = None, purpose: str = "") -> None:
    """抑止期間中なので送らずにキューへ積み、終了コード 20 で終わる。"""
    q = notify_queue.load_config()
    item_id, new = notify_queue.Store(QUEUE_DB, now_dt).enqueue(group, text, url, mention, sha, code, purpose=purpose)
    at = notify_queue.next_open(now_dt(), q)
    print(f"抑止期間({q.start:%H:%M}〜{q.end:%H:%M})のため送りません。キューに{'積みました' if new else 'すでにあります'}(#{item_id})")
    if code:
        print(f"承認 {code} は {at:%m/%d %H:%M} のまとめ送りでそのまま使います(承認を求め直しません)")
    else:
        print(f"{at:%m/%d %H:%M} に、寺下さんへ許可を求めてから送ります(タスク {FLUSH_TASK})")
    sys.exit(EXIT["queued"])


def load_groups() -> dict[str, str]:
    if not GROUPS.exists():
        return {}
    return json.loads(GROUPS.read_text(encoding="utf-8"))


def agent_env() -> None:
    """090 の環境変数(LINEのトークン等)を読み込む。値は表示しない。"""
    sys.path.insert(0, str(AGENT))
    import run_api  # noqa: E402
    run_api.load_environment()


def push_messages(to: str, messages: list[dict]) -> list[str]:
    import os
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    if not token:
        raise SystemExit("LINE_CHANNEL_ACCESS_TOKEN が読めません(090 の設定を確認)")
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/push", method="POST",
        data=json.dumps({"to": to, "messages": messages}, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "X-Line-Retry-Key": str(uuid.uuid4())})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"LINE への送信に失敗: HTTP {e.code} {e.read().decode('utf-8', 'replace')[:300]}")
    return [m.get("id", "") for m in body.get("sentMessages", [])]


def cmd_groups(_a: argparse.Namespace) -> None:
    g = load_groups()
    if not g:
        print("登録なし。register で登録してください")
    for name, gid in g.items():
        print(f"{name}\t{gid[:5]}…{gid[-4:]}")


def cmd_register(a: argparse.Namespace) -> None:
    if not (a.group_id.startswith("C") and len(a.group_id) == 33):
        raise SystemExit("グループIDは C で始まる33文字です(LINE のグループID)")
    g = load_groups()
    g[a.name] = a.group_id
    GROUPS.parent.mkdir(parents=True, exist_ok=True)
    GROUPS.write_text(json.dumps(g, ensure_ascii=False, indent=1), encoding="utf-8")
    print("登録:", a.name)


def cmd_publish(a: argparse.Namespace) -> None:
    src = Path(a.image)
    if not src.is_file() or src.suffix.lower() not in (".png", ".jpg", ".jpeg"):
        raise SystemExit("画像は png / jpg のファイルを指定してください")
    if src.stat().st_size > 5 * 1024 * 1024:
        raise SystemExit("画像が5MBを超えています")
    name = f"{a.prefix}-{secrets.token_urlsafe(18).replace('_', 'x').replace('-', 'y')}{src.suffix.lower()}"
    PUBLIC.mkdir(parents=True, exist_ok=True)
    dst = PUBLIC / name
    shutil.copy2(src, dst)
    # この端末から外向きの名前は引けない(社内LANのため)ので、自分自身(127.0.0.1)に向けて確認する
    out = subprocess.run(
        ["curl", "-sk", "-m", "20", "--resolve", f"{PUBLIC_HOST}:443:127.0.0.1", "-o", "NUL",
         "-w", "%{http_code} %{size_download}", f"https://{PUBLIC_HOST}/{name}"],
        capture_output=True, text=True).stdout.strip()
    code, size = (out.split() + ["0", "0"])[:2]
    if code != "200" or int(size) != src.stat().st_size:
        dst.unlink(missing_ok=True)
        raise SystemExit(f"公開の確認に失敗: {out}(Caddy の pub.sfuji.f5.si が動いているか確認)")
    print(f"公開しました: https://{PUBLIC_HOST}/{name}")
    if dst.stat().st_size > PREVIEW_MAX:
        print("注意: 1MBを超えているため、1対1トークの下書きでは画像を直接は表示せず、URLだけになります。")
    print("社外の回線(スマホのモバイル通信)で開けることを、送信前にユーザーに確認してもらう。"
          "社内LANから開けないときは、DNS(pub.sfuji.f5.si → 192.168.1.166)が未登録。")


def build_text(a: argparse.Namespace) -> str:
    text = Path(a.text_file).read_text(encoding="utf-8").strip()
    if a.url and not a.url.startswith(f"https://{PUBLIC_HOST}/"):
        raise SystemExit(f"URL は https://{PUBLIC_HOST}/ で始まるものだけです")
    if a.url and a.url not in text:
        text += "\n" + a.url
    if len(text) > MAX_LEN:
        raise SystemExit(f"本文が長すぎます({len(text)}字・上限{MAX_LEN})")
    return text


def sha_of(group: str, text: str, mention_id: str = "") -> str:
    """承認の照合用。メンションの相手も含める(承認後に相手を変えたら送れない)。メンションなしは従来と同じ値"""
    key = group + "\n" + text + (f"\nmention:{mention_id}" if mention_id else "")
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def resolve_mention(name: str) -> tuple[str, str]:
    """090 の発言者名簿(roster.json)から表示名でユーザーIDを引く。完全一致で1人に決まるときだけ"""
    if not name:
        return "", ""
    roster = json.loads((AGENT / "data" / "roster.json").read_text(encoding="utf-8"))
    hits = [uid for uid, v in roster.items() if (v or {}).get("displayName") == name]
    if len(hits) != 1:
        raise SystemExit(f"メンションの相手「{name}」が名簿で{len(hits)}件です(表示名の完全一致で1件だけ必要)")
    return hits[0], name


def text_message(text: str, mention_id: str = "") -> dict:
    """送るメッセージ。メンションありは textV2 で先頭に「@名前 さん」を付ける({ } は文字として扱う)"""
    if not mention_id:
        return {"type": "text", "text": text}
    body = text.replace("{", "{{").replace("}", "}}")
    return {"type": "textV2", "text": "{m} さん\n" + body,
            "substitution": {"m": {"type": "mention", "mentionee": {"type": "user", "userId": mention_id}}}}


def open_db():
    agent_env()
    from api import approvals, db  # noqa: E402
    db.init_db()  # 承認の表が無ければ作る(090 を再起動していなくても動く)
    return approvals, db


def cmd_request_approval(a: argparse.Namespace) -> None:
    groups = load_groups()
    if a.group not in groups:
        raise SystemExit(f"グループ「{a.group}」は未登録です。登録済み: {list(groups)}")
    try:  # 何のための投稿か(依頼元の説明)。無い依頼は出せない(2026-10-21 運用者決定)
        a.purpose = approval_purpose.check(a.purpose)
    except ValueError as exc:
        raise SystemExit(f"承認を求められません: {exc}")
    text = build_text(a)
    mention_id, mention_name = resolve_mention(a.mention)
    sha = sha_of(a.group, text, mention_id)
    if notify_queue.held(a.group, now_dt(), notify_queue.load_config()):
        hold(a.group, text, a.url, a.mention, sha, purpose=a.purpose)
    if approval_channel.use_lineworks():
        request_approval_lw(a, text, sha, mention_name)
        return
    approvals, db = open_db()
    from api import settings  # noqa: E402
    users = sorted(settings.LINE_AGENT_AUTHORIZED_USER_IDS)
    if len(users) != 1:
        raise SystemExit(f"承認者(許可された本人)が {len(users)} 人います。1人だけの設定にしてください")
    user = users[0]
    code = "".join(secrets.choice(CODE_CHARS) for _ in range(4))
    with db.write_tx() as conn:
        approvals.create(conn, code=code, sha=sha, target_name=a.group, user_id=user, ttl_ms=TTL_HOURS * 3600 * 1000)
    draft = (f"【LINE送信前の確認】 コード {code}\n送信先: {a.group}\n{approval_purpose.line(a.purpose)}\n"
             f"(下の全文を、ボット名義でそのまま送ります)\n"
             f"────────────\n{f'@{mention_name} さん(メンション)' + chr(10) if mention_name else ''}{text}")
    msgs: list[dict] = [{"type": "text", "text": draft[:4900]}]
    if a.url:
        local = PUBLIC / a.url.rsplit("/", 1)[-1]
        if local.is_file() and local.stat().st_size <= PREVIEW_MAX:
            msgs.append({"type": "image", "originalContentUrl": a.url, "previewImageUrl": a.url})
    msgs.append({"type": "template", "altText": f"送信前の確認 {code}: 返信は『OK {code}』か『中止』",
                 "template": {"type": "confirm", "text": f"{a.group} へ、この内容で送信しますか?(コード {code})",
                              "actions": [
                                  {"type": "postback", "label": "送信する", "data": f"ap|approve|{code}|{sha}", "displayText": "送信する"},
                                  {"type": "postback", "label": "中止", "data": f"ap|reject|{code}|{sha}", "displayText": "中止"}]}})
    ids = push_messages(user, msgs)
    print(f"寺下さんの1対1トークへ下書きを送りました(メッセージID {len(ids)}件)")
    print(f"確認コード: {code}  本文sha: {sha}  有効期限: {TTL_HOURS}時間")
    print(f"次: python line_notify.py wait-approval {code}")


def request_approval_lw(a: argparse.Namespace, text: str, sha: str, mention_name: str) -> None:
    code, parts = ask_lineworks(a.group, text, sha, mention_name, a.url, purpose=a.purpose)
    print(f"寺下さんの LINE WORKS の承認トークへ下書きを送りました({parts + 1} 通)")
    print(f"確認コード: {code}  本文sha: {sha}  有効期限: {TTL_HOURS}時間")
    print(f"次: python line_notify.py wait-approval {code}")


def ask_lineworks(group: str, text: str, sha: str, mention_name: str, url: str, note: str = "",
                  purpose: str = "") -> tuple[str, int]:
    """LINE WORKS の承認トークへ、下書きの全文(長ければ分けて)と画像の URL、「送信する」「中止」ボタンを送る。
    承認者は lw:<userId>。送れなかったら作った承認待ちを中止にしてから止める(押せない承認を残さない)。
    戻り値は (確認コード, 本文を分けて送った通数)。朝のまとめ送り(flush)からも使う。"""
    a = argparse.Namespace(group=group, url=url)
    try:
        lineworks.require_watcher()
        room = lineworks.load_room()
    except lineworks.NotifyError as exc:
        raise SystemExit(f"承認を求められません: {exc}")
    approvals, db = open_db()
    code = "".join(secrets.choice(CODE_CHARS) for _ in range(6))
    with db.write_tx() as conn:  # 全文を送り終えるまでは「送信中」(承認できない。2026-10-20 レビュー)
        approvals.create(conn, code=code, sha=sha, target_name=a.group, user_id=room.approver, ttl_ms=TTL_HOURS * 3600 * 1000)
        conn.execute("UPDATE line_approvals SET status=? WHERE code=?", (lineworks.SENDING, code))
    mention = f"@{mention_name} さん(メンション)\n" if mention_name else ""
    draft = "\n".join([
        f"【LINE送信前の確認】 コード {code}" + (f"({note})" if note else ""),
        f"送信先: LINE のグループ「{a.group}」",
        approval_purpose.line(purpose),  # 何のための投稿か(依頼元の説明。2026-10-21)
        "(下の全文を、LINE のボット FujiWorks の名義でそのまま送ります)",
        "────────────",
        mention + text,
        "────────────",
    ])
    if a.url:
        draft += f"\n画像: {a.url}"
    # 承認は承認の画面でだけ(2026-10-22 運用者決定)。本文の末尾に、その承認を直接開くリンクを書く
    draft += f"\n有効期限 {TTL_HOURS} 時間。{lineworks.reply_hint(code)}\n承認の画面: {lineworks.approval_link(code)}"
    try:  # 承認の画面(PWA)で読む全文(2026-10-21 運用者決定: 長い文は LINE WORKS では読まれないため)
        approval_text.write(code, sha, f"LINE「{a.group}」への投稿",
                            f"{approval_purpose.line(purpose)}\n送信先: LINE のグループ「{a.group}」\n\n{mention}{text}"
                            + (f"\n\n画像: {a.url}" if a.url else ""), purpose=approval_purpose.normalize(purpose))
    except Exception:  # noqa: BLE001  残せなくても承認は止めない(画面では承認できないだけ)
        pass
    try:
        parts = lineworks.send_text(draft, room=room)
        lineworks.send_buttons(f"LINE の「{a.group}」への投稿を中止するときは押してください(コード {code})",
                               lineworks.reject_buttons(code, sha), room=room)
    except Exception as exc:  # noqa: BLE001
        with db.write_tx() as conn:
            lineworks.void_unsent(conn, code)
        raise SystemExit(f"LINE WORKS へ下書きを送れません: {exc}")
    with db.write_tx() as conn:
        lineworks.open_for_approval(conn, code)
    try:  # 承認が放置されたときの電話の音声案内で読む文(グループ名は読まない。目的は人名・住所・連絡先らしい形が無ければ読む)
        phone_text.write(code, "LINEグループへの投稿です。" + approval_purpose.phone_sentence(purpose))
    except Exception:  # noqa: BLE001
        pass
    return code, parts


def read_approval(code: str):
    approvals, db = open_db()
    with db.read_conn() as conn:
        row = approvals.get(conn, code)
    if row is None:
        raise SystemExit(f"確認コード {code} は見つかりません")
    return row


def cmd_wait(a: argparse.Namespace) -> None:
    end = time.time() + a.timeout_min * 60
    while True:
        row = read_approval(a.code)
        status = row["status"]
        if status == "pending" and row["expires_at"] <= int(time.time() * 1000):
            status = "expired"
        if status != "pending":
            break
        if time.time() >= end:
            print(f"まだ返信がありません(待機{a.timeout_min}分)。確認コード {a.code} は有効です")
            sys.exit(EXIT["pending"])
        time.sleep(a.poll)
    label = {"approved": "承認されました", "rejected": "中止されました", "revise": "修正の依頼です",
             "expired": "期限切れです", "used": "すでに送信済みです"}[status]
    print(f"{a.code}: {label}")
    if status == "revise":
        print("--- 寺下さんの返信 ---\n" + (row["decision_text"] or ""))
    sys.exit(EXIT[status])


def cmd_send(a: argparse.Namespace) -> None:
    groups = load_groups()
    if a.group not in groups:
        raise SystemExit(f"グループ「{a.group}」は未登録です。登録済み: {list(groups)}")
    text = build_text(a)
    mention_id, mention_name = resolve_mention(a.mention)
    sha = sha_of(a.group, text, mention_id)
    head = f"@{mention_name} さん(メンション)\n" if mention_name else ""
    print(f"送信先: {a.group}\n--- 送る全文 ---\n{head}{text}\n--- 本文sha: {sha} ---")
    row = read_approval(a.approval)
    problems = []
    if row["status"] != "approved":
        problems.append(f"承認されていません(状態: {row['status']})")
    if row["target_name"] != a.group:
        problems.append("承認時の送信先と違います")
    if row["sha"] != sha:
        problems.append("承認された本文と違います(文面が変わりました。もう一度 request-approval から)")
    if row["expires_at"] <= int(time.time() * 1000):
        problems.append("承認の有効期限が切れています")
    if row["used_at"]:
        problems.append("この承認はすでに使いました")
    if problems:
        raise SystemExit("送れません: " + " / ".join(problems))
    if approved_in_quiet(a.group, row):
        if not a.send:
            print("(承認は確認できましたが、19:00 以降の承認なので --send でもキューに溜めます。朝 9:00 に送ります)")
            return
        hold(a.group, text, a.url, a.mention, sha, code=a.approval)
    if not a.send:
        print("(承認は確認できました。まだ送信していません。--send を付けると送ります)")
        return
    ids = deliver(groups[a.group], a.group, text, mention_id, sha, a.approval)
    print("送信しました。LINEのメッセージID:", ids or "(返却なし)")


def approved_in_quiet(group: str, row) -> bool:
    """いま抑止期間中で、承認されたのも抑止期間に入ってから(19:00 以降)なら True。
    19:00 より前に承認されたものは、送るのが 19:00 を過ぎても送る(2026-10-20 運用者決定)。"""
    from datetime import datetime
    q = notify_queue.load_config()
    if not notify_queue.held(group, now_dt(), q):
        return False
    decided = row["decided_at"]
    if not decided:
        return True  # 承認の時刻が分からなければ、送らない側に倒す
    return notify_queue.in_quiet(datetime.fromtimestamp(decided / 1000), q)


def deliver(group_id: str, group: str, text: str, mention_id: str, sha: str, code: str) -> list[str]:
    """承認コードを使用済みにしてから、グループへ送る(二重送信の防止)。送信に失敗したら承認済みに戻す。"""
    approvals, db = open_db()
    with db.write_tx() as conn:
        cur = conn.execute("UPDATE line_approvals SET status='used', used_at=? WHERE code=? AND status='approved' AND used_at IS NULL",
                           (db.now_ms(), code))
        if cur.rowcount != 1:
            raise SystemExit("この承認はすでに使われたか、状態が変わりました")
    try:
        ids = push_messages(group_id, [text_message(text, mention_id)])
    except SystemExit:
        with db.write_tx() as conn:
            conn.execute("UPDATE line_approvals SET status='approved', used_at=NULL WHERE code=?", (code,))
        raise
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "group": group, "sha": sha,
                            "approval": code, "ids": ids}, ensure_ascii=False) + "\n")
    return ids


def queue_store():
    return notify_queue.Store(QUEUE_DB, now_dt)


def cmd_queue_list(a: argparse.Namespace) -> None:
    rows = queue_store().items() if a.all else queue_store().items(*notify_queue.OPEN)
    if not rows:
        print("溜まっている報告はありません")
    for r in rows:
        head = r["text"].splitlines()[0][:40] if r["text"] else ""
        print(f"#{r['id']}\t{r['status']}\t{r['group_name']}\t{r['created_at']}\t{r['code'] or '-'}\t{head}"
              + (f"\t({r['note']})" if r["note"] else ""))


def cmd_queue_cancel(a: argparse.Namespace) -> None:
    store = queue_store()
    row = next((r for r in store.items() if r["id"] == a.id), None)
    if row is None or not store.cancel(a.id):
        raise SystemExit(f"#{a.id} は取り消せません(無いか、すでに終わっています)")
    if row["code"]:
        void_approval(row["code"], "cancelled")
    print(f"#{a.id} を取り消しました(送りません)")


def void_approval(code: str, reason: str) -> None:
    """返事の無い・不要になった承認を取り消す(後から押されても承認にならないように)。使用済みは変えない。"""
    approvals, db = open_db()
    with db.write_tx() as conn:
        conn.execute("UPDATE line_approvals SET status='rejected', decision_text=? "
                     "WHERE code=? AND status IN (?, 'pending', 'approved') AND used_at IS NULL",
                     (reason, code, lineworks.SENDING))


def flush_hooks() -> notify_queue.Hooks:
    groups = load_groups()

    def ask(item: dict) -> str:
        if item["group_name"] not in groups:
            raise SystemExit(f"グループ「{item['group_name']}」は未登録です")
        code, _ = ask_lineworks(item["group_name"], item["text"], item["sha"], item["mention"], item["url"],
                                note=f"夜間に溜めた報告・{item['created_at'][5:16].replace('T', ' ')} 受付",
                                purpose=item.get("purpose") or "")
        return code

    def read(code: str) -> dict | None:
        approvals, db = open_db()
        with db.read_conn() as conn:
            row = approvals.get(conn, code)
        return dict(row) if row is not None else None

    def send(item: dict, code: str) -> list[str]:
        mention_id, _ = resolve_mention(item["mention"])
        if sha_of(item["group_name"], item["text"], mention_id) != item["sha"]:
            raise SystemExit("メンションの相手が積んだときと変わっています")
        return deliver(groups[item["group_name"]], item["group_name"], item["text"], mention_id, item["sha"], code)

    def tell(text: str) -> None:
        lineworks.send_text(text)

    return notify_queue.Hooks(ask=ask, read=read, deliver=send, void=lambda c: void_approval(c, "carried_over"),
                              tell=tell, now=now_dt, sleep=time.sleep, now_ms=lambda: int(time.time() * 1000))


def flush_log(text: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}"
    print(line, flush=True)
    FLUSH_LOG.parent.mkdir(parents=True, exist_ok=True)
    with FLUSH_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def cmd_flush(_a: argparse.Namespace) -> None:
    if not approval_channel.use_lineworks():
        raise SystemExit("朝のまとめ送りは LINE WORKS の承認だけに対応しています(approval_channel を確認)")
    import msvcrt
    FLUSH_LOG.parent.mkdir(parents=True, exist_ok=True)
    lock = (AGENT / "data" / "line_notify_flush.lock").open("a+")
    try:  # 同時に 2 つ動かさない(タスクと手動の起動が重なったとき)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        flush_log("別のまとめ送りが動いているので終わります")
        return
    notify_queue.Flusher(queue_store(), flush_hooks(), notify_queue.load_config(), log=flush_log).run()


def flush_task_command() -> tuple[str, str]:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")  # 窓を出さない
    return str(w if w.exists() else exe), f'"{Path(__file__).resolve()}" flush'


def cmd_install_flush_task(_a: argparse.Namespace) -> None:
    """毎朝 9:00(設定の終わりの時刻)に flush を起動するタスク。PC が落ちていたら起動後すぐ・重なったら新しい方を起動しない。"""
    exe, args = flush_task_command()
    at = notify_queue.load_config().end
    ps = ("$a = New-ScheduledTaskAction -Execute '{exe}' -Argument '{args}' -WorkingDirectory '{wd}';"
          "$t = New-ScheduledTaskTrigger -Daily -At '{at:%H:%M}';"
          "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew "
          "-ExecutionTimeLimit (New-TimeSpan -Hours 11) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries;"
          "Register-ScheduledTask -TaskName '{name}' -Action $a -Trigger $t -Settings $s -Force | Out-Null;"
          "(Get-ScheduledTask -TaskName '{name}').State").format(
        exe=exe.replace("'", "''"), args=args.replace("'", "''"), wd=str(Path(__file__).resolve().parent).replace("'", "''"),
        at=at, name=FLUSH_TASK)
    cp = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, encoding="utf-8",
                        errors="replace")
    if cp.returncode != 0:
        raise SystemExit(f"タスクを登録できません: {cp.stderr.strip()[:500]}")
    print(f"タスク {FLUSH_TASK} を登録しました(毎日 {at:%H:%M}・状態 {cp.stdout.strip()})")
    print(f"起動: {exe} {args}")


def cmd_uninstall_flush_task(_a: argparse.Namespace) -> None:
    cp = subprocess.run(["schtasks", "/Delete", "/F", "/TN", FLUSH_TASK], capture_output=True)
    print("外しました" if cp.returncode == 0 else f"外せません: {(cp.stdout + cp.stderr).decode('cp932', 'replace').strip()}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("groups").set_defaults(fn=cmd_groups)
    r = sub.add_parser("register")
    r.add_argument("--name", required=True)
    r.add_argument("--group-id", required=True)
    r.set_defaults(fn=cmd_register)
    p = sub.add_parser("publish")
    p.add_argument("image")
    p.add_argument("--prefix", default="notice")
    p.set_defaults(fn=cmd_publish)
    q = sub.add_parser("request-approval")
    q.add_argument("--group", required=True)
    q.add_argument("--text-file", required=True)
    q.add_argument("--url", default="")
    q.add_argument("--mention", default="", help="メンションする相手の表示名(090 の roster.json と完全一致)")
    q.add_argument("--purpose", required=True,
                   help="何のための投稿か(1〜2 行・6〜120 字。承認の画面・LINE WORKS・電話で示す。人名・住所・連絡先は書かない)")
    q.set_defaults(fn=cmd_request_approval)
    w = sub.add_parser("wait-approval")
    w.add_argument("code")
    w.add_argument("--timeout-min", type=float, default=30)
    w.add_argument("--poll", type=float, default=5)
    w.set_defaults(fn=cmd_wait)
    s = sub.add_parser("send")
    s.add_argument("--group", required=True)
    s.add_argument("--text-file", required=True)
    s.add_argument("--url", default="")
    s.add_argument("--approval", required=True)
    s.add_argument("--mention", default="", help="request-approval と同じ値を指定する")
    s.add_argument("--send", action="store_true")
    s.set_defaults(fn=cmd_send)
    ql = sub.add_parser("queue-list")
    ql.add_argument("--all", action="store_true", help="送り終えた・取り消したものも出す")
    ql.set_defaults(fn=cmd_queue_list)
    qc = sub.add_parser("queue-cancel")
    qc.add_argument("id", type=int)
    qc.set_defaults(fn=cmd_queue_cancel)
    sub.add_parser("flush").set_defaults(fn=cmd_flush)
    sub.add_parser("install-flush-task").set_defaults(fn=cmd_install_flush_task)
    sub.add_parser("uninstall-flush-task").set_defaults(fn=cmd_uninstall_flush_task)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
