"""R-Mail の配信予約受付メールを確かめ、LINE で知らせる文と画像の仕様を作る(読み取りだけ)。

運用者が RMS で配信予約をすると、楽天から「【RMS】R-Mail 配信予約を受け付けました」が届く
(fw-shopmaster@ の受信箱。本文に rmail_id・配信予約日時・メールタイプ・件名・送信人数)。
下書きの rmail_id がすべて揃ったら予約完了とみなし、LINE の本文(短く。詳細は画像)と
make_status_image.py 用の仕様(①確認した観点 ②リンクごとの確認。2026-10-24 運用者が案 1+2 を採用)を書き出す。

使い方:
  python reserve_notice.py check --ids 26792904,26792914 --out <フォルダ>                 # 今ある分だけ見る
  python reserve_notice.py wait  --ids 26792904,26792914 --out <フォルダ> --timeout-min 360  # 揃うまで待つ
  --check <check_*.json> を付けると、画像にリンクごとの確認の表が入る(②の検査結果)

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


ITEM_RE = re.compile(r"item\.rakuten\.co\.jp/seifukunofuji/(g\d{4})/", re.I)
STOCK_RE = re.compile(r"在庫 計(\d+)")
MAX_LINK_ROWS = 14  # 1 枚に収める(A4 縦)ための上限。超えた分は「ほか N 本」にまとめる


def _kinds(rs: list[Reservation]) -> str:
    return "・".join(KIND_LABEL.get(r.mail_type, r.mail_type) for r in rs)


def build_body(rs: list[Reservation], checked: bool = False) -> str:
    """LINE の本文(短く。詳細は画像)。2026-10-24 運用者決定: 案 1(観点の表)+ 案 2(リンクごとの表)を画像に書く。
    URL は line_notify.py の --url が末尾に足すので書かない。"""
    times = sorted({r.send_at for r in rs})
    when_line = (f"{when(times[0])} 配信({_kinds(rs)})" if len(times) == 1
                 else " / ".join(f"{KIND_LABEL.get(r.mail_type, r.mail_type)} {when(r.send_at)}" for r in rs) + " 配信")
    lines = ["◆楽天メルマガ 配信予約完了", when_line]
    if checked:
        lines.append("リンク・在庫を確認しました。")
    lines.append("詳しくは画像をご確認ください。")
    return "\n".join(lines) + "\n"


def _plain(text: str) -> str:
    """画像の字体に無い文字(絵文字など BMP の外)を外す。"""
    return "".join(ch for ch in text if ord(ch) <= 0xFFFF).strip()


def _link_rows(findings: list[dict], accept: frozenset[str] = frozenset()) -> tuple[list[list[str]], list[int], dict]:
    """リンクごとの行(商品と、問題のあったリンク)。OK の商品以外のリンクは最後の 1 行にまとめる。
    accept: 警告を人が確かめて問題なしとした URL(案内文と名前が合わないだけ など)。結果を「OK(確認済)」にする。"""
    rows: list[list[str]] = []
    stop: list[int] = []
    items = {"n": 0, "ok": 0, "stock_ok": 0, "other": 0, "other_ok": 0}
    others_ok = 0
    for f in findings:
        m = ITEM_RE.search(f.get("url", ""))
        reviewed = f.get("level") != "ok" and f.get("url") in accept
        ok = f.get("level") == "ok" or reviewed
        if reviewed:
            rows.append([str(len(rows) + 1), _plain(f.get("name") or "")[:16],
                         m.group(1).upper() if m else "リンク", "―", "OK(確認済)"])
            items["n" if m else "other"] += 1
            items["ok" if m else "other_ok"] += 1
            items["stock_ok"] += bool(m)
            continue
        if m:
            items["n"] += 1
            items["ok"] += ok
            s = STOCK_RE.search(f.get("message", ""))
            stock = s.group(1) if s else ("0" if "欠品" in f.get("message", "") else "あり")
            items["stock_ok"] += stock not in ("0",) and "欠品" not in f.get("message", "")
            rows.append([str(len(rows) + 1), _plain(f.get("name") or "")[:16], m.group(1).upper(), stock, "OK" if ok else "要確認"])
        else:
            items["other"] += 1
            items["other_ok"] += ok
            if ok:
                others_ok += 1
                continue
            rows.append([str(len(rows) + 1), _plain(f.get("name") or "")[:16], "リンク", "―", "要確認"])
        if not ok:
            stop.append(len(rows) - 1)
    if len(rows) > MAX_LINK_ROWS:
        rest = len(rows) - MAX_LINK_ROWS + 1
        rows = rows[:MAX_LINK_ROWS - 1] + [["…", f"ほか {rest} 件(詳細は担当へ)", "", "", ""]]
        stop = [i for i in stop if i < MAX_LINK_ROWS - 1]
    if others_ok:
        rows.append([str(len(rows) + 1), "そのほかのリンク", f"{others_ok} 本", "―", "OK"])
    return rows, stop, items


def build_spec(rs: list[Reservation], findings: list[dict] | None = None,
               accept: frozenset[str] = frozenset()) -> dict:
    """画像の仕様: ①確認した観点(観点|結果)②リンクごとの確認(No|リンク|リンク先|在庫|結果)。"""
    def res(ok: bool) -> str:
        return "OK" if ok else "要確認"

    main = next((r for r in rs if r.mail_type == "HTMLメール"), rs[0])
    view = [[f"{KIND_LABEL.get(r.mail_type, r.mail_type)}の配信予約({when(r.send_at)}・{r.recipients:,}人)", "OK"] for r in rs]
    view.append([f"件名「{main.subject[:24]}…」" if len(main.subject) > 24 else f"件名「{main.subject}」", "OK"])
    sections: list[dict] = []
    if findings is not None:
        rows, stop, it = _link_rows(findings, accept)
        view += [[f"商品リンクが正しい商品につながる({it['n']}点)", res(it["ok"] == it["n"])],
                 [f"商品が品切れでない({it['n']}点)", res(it["stock_ok"] == it["n"])],
                 [f"そのほかのリンクが開ける({it['other']}本)", res(it["other_ok"] == it["other"])]]
        sections.append({"heading": "リンクごとの確認", "grid": {
            "header": ["No", "リンク", "リンク先", "在庫", "結果"], "widths": [80, 460, 260, 150, 170],
            "ok_col": 4, "rows": rows, "stop_rows": stop}})
    view_stop = [i for i, v in enumerate(view) if v[1] != "OK"]
    sections.insert(0, {"heading": "確認した観点", "grid": {
        "header": ["観点", "結果"], "widths": [900, 220], "ok_col": 1, "rows": view, "stop_rows": view_stop}})
    return {"title": "楽天メルマガ 配信予約完了",
            "subtitle": f"{when(min(r.send_at for r in rs))} 配信({_kinds(rs)})", "sections": sections}


def save(rs: list[Reservation], out: Path, findings: list[dict] | None = None,
         accept: frozenset[str] = frozenset()) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "reserve.json").write_text(json.dumps([asdict(r) for r in rs], ensure_ascii=False, indent=1),
                                      encoding="utf-8", newline="")
    (out / "body.txt").write_text(build_body(rs, findings is not None), encoding="utf-8", newline="")
    (out / "spec.json").write_text(json.dumps(build_spec(rs, findings, accept), ensure_ascii=False, indent=1),
                                   encoding="utf-8", newline="")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["check", "wait"])
    ap.add_argument("--ids", required=True, help="下書きの rmail_id(カンマ区切り)")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--timeout-min", type=int, default=360)
    ap.add_argument("--interval-sec", type=int, default=60)
    ap.add_argument("--accept", action="append", default=[],
                    help="人が確かめて問題なしとした警告の URL(何度でも)。画像で「OK(確認済)」にする")
    ap.add_argument("--check", type=Path, help="②のリンク検査の結果(check_mail_links.py --json)。画像にリンクごとの表を入れる")
    a = ap.parse_args()
    ids = [x.strip() for x in a.ids.split(",") if x.strip()]
    findings = json.loads(a.check.read_text(encoding="utf-8"))["findings"] if a.check else None
    end = time.time() + a.timeout_min * 60
    while True:
        found = find(ids)
        for rid in ids:
            r = found.get(rid)
            print(f"{rid}: " + (f"予約あり {r.send_at} {r.mail_type} {r.recipients}人" if r else "まだ"))
        if len(found) == len(ids):
            rs = [found[i] for i in ids]
            save(rs, a.out, findings, frozenset(a.accept))
            print("--- LINE の本文 ---")
            print(build_body(rs, findings is not None), end="")
            print(f"保存: {a.out}")
            return 0
        if a.mode == "check" or time.time() >= end:
            return 13
        time.sleep(a.interval_sec)


if __name__ == "__main__":
    sys.exit(main())
