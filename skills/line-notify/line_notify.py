"""LINE グループへ「状況説明の画像のURL つきの通知」を送る道具(送信前に、寺下さんの1対1トークで承認をもらう)。

LINE(通常の LINE。LINE WORKS ではない)の Messaging API で、090.LINE業務指示エージェントのボット
(FujiWorks)の名義で送る。画像は LINE の仕様で「公開されたURL」が要るため、
pub.sfuji.f5.si(自社の公開置き場・ログイン不要・一覧なし)に画像を1枚置き、そのURLをテキストに添える。

  python line_notify.py groups                                  登録済みのグループ名を一覧
  python line_notify.py register --name 名前 --group-id Cxxxx   グループIDを登録(090/data/line_groups.json・Git対象外)
  python line_notify.py publish 画像.png [--prefix g0964]        画像を公開置き場へ置き、URLを表示(送信はしない)
  python line_notify.py request-approval --group 名前 --text-file 本文.txt --url URL
        → 寺下さんの1対1トークへ、下書き(全文・画像・「送信する」「中止」ボタン)を送り、確認コードを表示
  python line_notify.py wait-approval コード [--timeout-min 30]
        → LINE での返信を待つ。終了コード 0=承認 / 10=中止 / 11=修正の依頼(本文を表示) / 12=期限切れ / 13=時間切れ(まだ待ち)
  python line_notify.py send --group 名前 --text-file 本文.txt --url URL --approval コード            確認のみ
  python line_notify.py send --group 名前 --text-file 本文.txt --url URL --approval コード --send     グループへ送信

**グループへの送信は、1対1トークで承認された全文だけ**。承認コードが「承認済み」で、送信先・本文(sha8)が承認時と
一致し、未使用で、期限内のときだけ送れる。送ったら承認コードは使用済みになり、二度は使えない。
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

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[3]
AGENT = ROOT / "090.LINE業務指示エージェント"
GROUPS = AGENT / "data" / "line_groups.json"
LOG = AGENT / "data" / "line_notify_log.jsonl"
PUBLIC = ROOT / "900.ClaudeCode" / "public-share"
PUBLIC_HOST = "pub.sfuji.f5.si"
MAX_LEN = 4500
TTL_HOURS = 24
PREVIEW_MAX = 1024 * 1024  # LINE の previewImageUrl は 1MB まで
CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 紛らわしい文字(0/O・1/I)を除く
EXIT = {"approved": 0, "rejected": 10, "revise": 11, "expired": 12, "pending": 13, "used": 14}


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


def sha_of(group: str, text: str) -> str:
    return hashlib.sha256((group + "\n" + text).encode("utf-8")).hexdigest()[:8]


def open_db():
    agent_env()
    from api import approvals, db  # noqa: E402
    db.init_db()  # 承認の表が無ければ作る(090 を再起動していなくても動く)
    return approvals, db


def cmd_request_approval(a: argparse.Namespace) -> None:
    groups = load_groups()
    if a.group not in groups:
        raise SystemExit(f"グループ「{a.group}」は未登録です。登録済み: {list(groups)}")
    text = build_text(a)
    sha = sha_of(a.group, text)
    approvals, db = open_db()
    from api import settings  # noqa: E402
    users = sorted(settings.LINE_AGENT_AUTHORIZED_USER_IDS)
    if len(users) != 1:
        raise SystemExit(f"承認者(許可された本人)が {len(users)} 人います。1人だけの設定にしてください")
    user = users[0]
    code = "".join(secrets.choice(CODE_CHARS) for _ in range(4))
    with db.write_tx() as conn:
        approvals.create(conn, code=code, sha=sha, target_name=a.group, user_id=user, ttl_ms=TTL_HOURS * 3600 * 1000)
    draft = (f"【LINE送信前の確認】 コード {code}\n送信先: {a.group}\n(下の全文を、ボット名義でそのまま送ります)\n"
             f"────────────\n{text}")
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
    sha = sha_of(a.group, text)
    print(f"送信先: {a.group}\n--- 送る全文 ---\n{text}\n--- 本文sha: {sha} ---")
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
    if not a.send:
        print("(承認は確認できました。まだ送信していません。--send を付けると送ります)")
        return
    approvals, db = open_db()
    with db.write_tx() as conn:  # 先に使用済みにする(二重送信の防止)。送信に失敗したら戻す
        cur = conn.execute("UPDATE line_approvals SET status='used', used_at=? WHERE code=? AND status='approved' AND used_at IS NULL",
                           (db.now_ms(), a.approval))
        if cur.rowcount != 1:
            raise SystemExit("この承認はすでに使われたか、状態が変わりました")
    try:
        ids = push_messages(groups[a.group], [{"type": "text", "text": text}])
    except SystemExit:
        with db.write_tx() as conn:
            conn.execute("UPDATE line_approvals SET status='approved', used_at=NULL WHERE code=?", (a.approval,))
        raise
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "group": a.group, "sha": sha,
                            "approval": a.approval, "ids": ids}, ensure_ascii=False) + "\n")
    print("送信しました。LINEのメッセージID:", ids or "(返却なし)")


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
    s.add_argument("--send", action="store_true")
    s.set_defaults(fn=cmd_send)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
