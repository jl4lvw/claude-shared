"""LINE グループへ「状況説明の画像のURL つきの通知」を送る道具。

LINE(通常の LINE。LINE WORKS ではない)の Messaging API で、090.LINE業務指示エージェントのボット
(FujiWorks)の名義で送る。画像は LINE の仕様で「公開されたURL」が要るため、
pub.sfuji.f5.si(自社の公開置き場・ログイン不要・一覧なし)に画像を1枚置き、そのURLをテキストに添える。

  python line_notify.py groups                                  登録済みのグループ名を一覧
  python line_notify.py register --name 名前 --group-id Cxxxx   グループIDを登録(090/data/line_groups.json・Git対象外)
  python line_notify.py publish 画像.png [--prefix g0964]        画像を公開置き場へ置き、URLを表示(送信はしない)
  python line_notify.py send --group 名前 --text-file 本文.txt --url URL            確認のみ(送らない)
  python line_notify.py send --group 名前 --text-file 本文.txt --url URL --send --approved <sha8>

送信は2段階: まず --send なしで実行すると、送る全文と確認コード(sha8)が出る。ユーザーが全文を承認したら、
同じ引数に --send --approved <sha8> を付けて実行する(文面を変えると確認コードが変わり、送れない)。
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


def load_groups() -> dict[str, str]:
    if not GROUPS.exists():
        return {}
    return json.loads(GROUPS.read_text(encoding="utf-8"))


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
    print("社外の回線(スマホのモバイル通信)で開けることを、送信前にユーザーに確認してもらう。"
          "社内LANから開けないときは、DNS(pub.sfuji.f5.si → 192.168.1.166)が未登録。")


def build_text(a: argparse.Namespace) -> str:
    text = Path(a.text_file).read_text(encoding="utf-8").strip()
    if a.url and a.url not in text:
        text += "\n" + a.url
    if a.url and not a.url.startswith(f"https://{PUBLIC_HOST}/"):
        raise SystemExit(f"URL は https://{PUBLIC_HOST}/ で始まるものだけです")
    if len(text) > MAX_LEN:
        raise SystemExit(f"本文が長すぎます({len(text)}字・上限{MAX_LEN})")
    return text


def cmd_send(a: argparse.Namespace) -> None:
    groups = load_groups()
    if a.group not in groups:
        raise SystemExit(f"グループ「{a.group}」は未登録です。登録済み: {list(groups)}")
    text = build_text(a)
    sha = hashlib.sha256((a.group + "\n" + text).encode("utf-8")).hexdigest()[:8]
    print(f"送信先: {a.group}\n--- 送る全文 ---\n{text}\n--- 確認コード: {sha} ---")
    if not a.send:
        print("(確認のみ。送信していません。承認後に --send --approved " + sha + " を付けて実行)")
        return
    if a.approved != sha:
        raise SystemExit("--approved が確認コードと一致しません。全文を承認してもらってから、表示された確認コードを指定してください")
    sys.path.insert(0, str(AGENT))
    import run_api  # noqa: E402  (090 の環境変数読み込みを再利用。秘密鍵は表示しない)
    run_api.load_environment()
    from api import notifications  # noqa: E402
    sent = notifications.push(groups[a.group], text, str(uuid.uuid4()))
    ids = [x["id"] for x in (sent or [])]
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "group": a.group, "sha": sha, "ids": ids}, ensure_ascii=False) + "\n")
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
    s = sub.add_parser("send")
    s.add_argument("--group", required=True)
    s.add_argument("--text-file", required=True)
    s.add_argument("--url", default="")
    s.add_argument("--send", action="store_true")
    s.add_argument("--approved", default="")
    s.set_defaults(fn=cmd_send)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
