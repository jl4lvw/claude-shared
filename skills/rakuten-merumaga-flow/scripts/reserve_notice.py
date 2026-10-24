"""R-Mail の配信予約受付メールを確かめ、LINE で知らせる文と画像の仕様を作る(読み取りだけ)。

運用者が RMS で配信予約をすると、楽天から「【RMS】R-Mail 配信予約を受け付けました」が届く
(fw-shopmaster@ の受信箱。本文に rmail_id・配信予約日時・メールタイプ・件名・送信人数)。
下書きの rmail_id がすべて揃ったら予約完了とみなし、LINE の本文(2026-10-24 運用者が案 3 を採用)と
make_status_image.py 用の仕様を書き出す。

使い方:
  python reserve_notice.py check --ids 26792904,26792914 --out <フォルダ>                 # 今ある分だけ見る
  python reserve_notice.py wait  --ids 26792904,26792914 --out <フォルダ> --timeout-min 360  # 揃うまで待つ

終了コード: 0=全部そろった(body.txt / spec.json / reserve.json を保存) / 13=まだ揃っていない(時間切れ)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from email.message import Message
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\ClaudeCode\900.ClaudeCode\mail-search\scripts")
from search import SearchQuery, search  # noqa: E402

SUBJECT = "配信予約を受け付けました"
SENDER = "rakuten.co.jp"
KIND_LABEL = {"HTMLメール": "PC用", "モバイルテキスト": "携帯用"}
WEEKDAY = "月火水木金土日"


@dataclass(frozen=True)
class Reservation:
    rmail_id: str
    send_at: str  # 配信予約日時 "2026-10-24 15:00:00"
    mail_type: str  # HTMLメール / モバイルテキスト
    subject: str
    recipients: int


def plain_text(msg: Message) -> str:
    """本文(text/plain)を取り出す。古い形式の Message には get_body が無いので walk で探す。"""
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_type() == "text/plain":
            raw = part.get_payload(decode=True) or b""
            return raw.decode(part.get_content_charset() or "iso-2022-jp", "replace")
    return ""


def parse(text: str) -> Reservation | None:
    def field(name: str) -> str:
        m = re.search(rf"・{name}：(.*)", text)
        return m.group(1).strip() if m else ""

    rid, count = field("rmail_id"), field("送信人数")
    if not rid.isdigit():
        return None
    return Reservation(rid, field("配信予約日時"), field("メールタイプ"), field("メール件名"),
                       int(count) if count.isdigit() else 0)


def find(ids: list[str]) -> dict[str, Reservation]:
    found: dict[str, Reservation] = {}
    for h in search(SearchQuery(subject_contains=SUBJECT, from_contains=SENDER, limit=500)):
        r = parse(plain_text(h.message))
        if r and r.rmail_id in ids:
            found[r.rmail_id] = r
    return found


def when(send_at: str) -> str:
    d = datetime.strptime(send_at, "%Y-%m-%d %H:%M:%S")
    return f"{d.month}/{d.day}({WEEKDAY[d.weekday()]}) {d:%H:%M}"


def build_body(rs: list[Reservation]) -> str:
    """LINE の本文(案 3)。URL は line_notify.py の --url が末尾に足すので書かない。"""
    main = next((r for r in rs if r.mail_type == "HTMLメール"), rs[0])
    times = sorted({r.send_at for r in rs})
    lines = ["◆楽天メルマガ 配信予約完了", ""]
    if len(times) == 1:
        lines.append(f"{when(times[0])} に配信されます。")
    else:  # 種類ごとに時刻が違うときは並べる
        lines += [f"{KIND_LABEL.get(r.mail_type, r.mail_type)}：{when(r.send_at)} に配信" for r in rs]
    lines.append(f"件名：{main.subject}")
    lines.append(" / ".join(f"{KIND_LABEL.get(r.mail_type, r.mail_type)} {r.recipients:,}人" for r in rs))
    return "\n".join(lines) + "\n"


def build_spec(rs: list[Reservation]) -> dict:
    rows = [[KIND_LABEL.get(r.mail_type, r.mail_type),
             f"{when(r.send_at)} 配信 / {r.recipients:,}人 / 件名「{r.subject}」"] for r in rs]
    return {
        "title": "楽天メルマガ 配信予約完了",
        "subtitle": f"{when(min(r.send_at for r in rs))} 配信",
        "sections": [{"heading": "予約した内容", "table": rows}],
    }


def save(rs: list[Reservation], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "reserve.json").write_text(json.dumps([asdict(r) for r in rs], ensure_ascii=False, indent=1),
                                      encoding="utf-8", newline="")
    (out / "body.txt").write_text(build_body(rs), encoding="utf-8", newline="")
    (out / "spec.json").write_text(json.dumps(build_spec(rs), ensure_ascii=False, indent=1),
                                   encoding="utf-8", newline="")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["check", "wait"])
    ap.add_argument("--ids", required=True, help="下書きの rmail_id(カンマ区切り)")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--timeout-min", type=int, default=360)
    ap.add_argument("--interval-sec", type=int, default=60)
    a = ap.parse_args()
    ids = [x.strip() for x in a.ids.split(",") if x.strip()]
    end = time.time() + a.timeout_min * 60
    while True:
        found = find(ids)
        for rid in ids:
            r = found.get(rid)
            print(f"{rid}: " + (f"予約あり {r.send_at} {r.mail_type} {r.recipients}人" if r else "まだ"))
        if len(found) == len(ids):
            rs = [found[i] for i in ids]
            save(rs, a.out)
            print("--- LINE の本文 ---")
            print(build_body(rs), end="")
            print(f"保存: {a.out}")
            return 0
        if a.mode == "check" or time.time() >= end:
            return 13
        time.sleep(a.interval_sec)


if __name__ == "__main__":
    sys.exit(main())
