"""端末 A の「RMS 下書き登録」へ送る申請 JSON を作る(TK 側)。

入力: メルマガエディタの「貼り付け用タグ」(HTML)・HTML メールの件名・テンプレート名。
作るもの(A の仕様 #3246 のとおり):
  {"source":"TK","template_name":...,"purpose":<目的>,"kinds":["html","mobile_text"],
   "html":{"subject":...,"body":<HTML 全文>,"text_body":<テキスト版>},
   "mobile_text":{"body":<携帯用テキスト>}}
  - テキスト版(html.text_body)と携帯用テキストは、同じ変換(html_to_mobile_text)で作る
  - モバイルの件名は A が固定値を持つ(申請では指定しない)
検査(違反は保存せずに終了コード 2。勝手に削らない):
  HTML 100 KB 以下(UTF-8) / 件名 255 バイト以下(半角換算・Shift_JIS) / 携帯テキスト 5,000 文字以内
  purpose(目的・必須): 空白をまとめて 6〜120 字(2026-10-21 運用者決定。A の a_browser_common.approval_purpose と同じ長さ)
本文の中身は表示しない(サイズだけ表示)。

使い方:
  python build_draft_request.py --tag <タグ.txt> --subject "<件名>" --template-name "<テンプレート名>"
      --purpose "<目的(6〜120 字)>" --out <申請.json>   (実際は 1 行で打つ)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "rakuten-merumaga" / "scripts"))
import html_to_mobile_text as m

HTML_MAX_BYTES = 100 * 1024
SUBJECT_MAX_BYTES = 255
SOURCE = "TK"
PURPOSE_MIN, PURPOSE_MAX = 6, 120  # A の a_browser_common/approval_purpose.py の MIN_LEN / MAX_LEN と同じ


class RequestError(ValueError):
    """申請を作れない(入力の不備・上限超過)。"""


def sjis_bytes(text: str) -> int:
    return len(text.encode("cp932", errors="replace"))


def check_purpose(purpose: object) -> str:
    """目的(何のための下書きか)を確かめて正規化する(改行・連続した空白は 1 つの空白に)。A でも同じ検査をする。"""
    if not isinstance(purpose, str):
        raise RequestError("目的(purpose)がありません。何のための下書きかを 1〜2 行で書いてください")
    p = re.sub(r"\s+", " ", "".join(ch if ch.isprintable() else " " for ch in purpose)).strip()
    if len(p) < PURPOSE_MIN:
        raise RequestError(f"目的(purpose)が短すぎます({len(p)} 字)。{PURPOSE_MIN} 字以上で書いてください")
    if len(p) > PURPOSE_MAX:
        raise RequestError(f"目的(purpose)が長すぎます({len(p)} 字)。{PURPOSE_MAX} 字までにしてください")
    return p


def build(tag_html: str, subject: str, template_name: str, kinds: tuple[str, ...] = ("html", "mobile_text"),
          *, purpose: str) -> dict:
    purpose = check_purpose(purpose)
    subject = subject.strip()
    if not subject:
        raise RequestError("HTML メールの件名が空です(担当者が LINE で指示した件名を渡してください)")
    if sjis_bytes(subject) > SUBJECT_MAX_BYTES:
        raise RequestError(f"件名が長すぎます({sjis_bytes(subject)} バイト。半角換算で 255 以内)")
    if not template_name.strip():
        raise RequestError("テンプレート名が空です")
    if not set(kinds) <= {"html", "mobile_text"} or not kinds:
        raise RequestError(f"kinds が不正です: {kinds}")
    if "<table" not in tag_html.lower():
        raise RequestError("貼り付け用タグ(HTML の table)ではありません")
    size = len(tag_html.encode("utf-8"))
    if size > HTML_MAX_BYTES:
        raise RequestError(f"HTML が大きすぎます({size:,} バイト。上限 {HTML_MAX_BYTES:,})")
    text = m.convert(tag_html)
    rep = m.inspect(text)
    if rep.chars_crlf > m.LIMIT:
        raise RequestError(f"携帯用テキストが {m.LIMIT:,} 文字を超えます({rep.chars_crlf:,} 文字・改行を 2 文字と数えた値。超過 {rep.chars_crlf - m.LIMIT:,})。削る候補を決めてください")
    req: dict = {"source": SOURCE, "template_name": template_name.strip(), "purpose": purpose, "kinds": list(kinds)}
    if "html" in kinds:
        req["html"] = {"subject": subject, "body": tag_html.strip(), "text_body": text.strip()}
    if "mobile_text" in kinds:
        req["mobile_text"] = {"body": text.strip()}
    return req


def summary(req: dict) -> str:
    lines = [f"申請: {req['template_name']}  種別: {', '.join(req['kinds'])}", f"  目的: {req['purpose']}"]
    if "html" in req:
        h = req["html"]
        lines.append(f"  HTML メール: 件名「{h['subject']}」({sjis_bytes(h['subject'])} バイト) / HTML {len(h['body'].encode('utf-8')):,} バイト / テキスト版 {len(h['text_body'])} 文字")
    if "mobile_text" in req:
        lines.append(f"  モバイルテキスト: {len(req['mobile_text']['body'])} 文字(件名は A の固定値)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    ap = argparse.ArgumentParser(description="A の RMS 下書き登録への申請 JSON を作る")
    ap.add_argument("--tag", type=Path, required=True)
    ap.add_argument("--subject", required=True)
    ap.add_argument("--template-name", required=True)
    ap.add_argument("--kinds", nargs="+", default=["html", "mobile_text"])
    ap.add_argument("--purpose", required=True, help="目的(何のための下書きか・6〜120 字。承認の画面・LINE WORKS・電話で示す)")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    try:
        req = build(m.read_text(a.tag), a.subject, a.template_name, tuple(a.kinds), purpose=a.purpose)
    except RequestError as e:
        print(f"エラー: {e}")
        return 2
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(req, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    print(summary(req))
    print(f"保存: {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
