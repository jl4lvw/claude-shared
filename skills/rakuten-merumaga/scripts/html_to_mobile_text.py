"""メルマガの HTML(貼り付け用タグ)を、携帯メルマガ用のテキストに変換する。

昔の携帯電話(テキストしか受信できない端末)向け。制約:
  1. 全体で 5,000 文字を超えない
  2. 1 行が 100 文字を超えない(適切なところで改行する)。ただし URL の行は例外で長さ無制限(2026-10-17 ユーザー決定)

変換の方針(旧スクリプト「メルマガテキスト作成」の考え方を、正規表現の置換から、
HTML を正しく読む方式に作り直したもの):
  - 見出し・段落・表の行・<br> などの区切りで改行する。装飾(font/b/img 等)は捨てる。
  - リンクは「リンクの文字」の次の行に URL を出す(画像だけのリンクは URL だけ)。
  - 先頭のバナー画像のリンク(文字の無い URL)は外す(2026-10-17 ユーザー指示。--keep-leading-url で残せる)。
  - 連続して同じ行は 1 つにする。空行は 1 つまで。<hr> は短い区切り線にする。
  - 100 文字を超える行は、。、！？ ▼ などの切れ目か空白で折り返す。URL は切らない。
  - 5,000 文字を超えるときは、勝手に削らず、超過量と削る候補を報告して終了コード 2 にする。

使い方:
  python html_to_mobile_text.py <入力のHTML(テキスト).txt> [--out 出力.txt] [--width 100] [--limit 5000]
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

WIDTH = 100
LIMIT = 5000
HR_LINE = "-----------------"
BLOCK_TAGS = {
    "p", "div", "tr", "table", "tbody", "thead", "tfoot", "ul", "ol", "li", "h1", "h2",
    "h3", "h4", "h5", "h6", "center", "blockquote", "form", "dl", "dt", "dd",
}  # fmt: skip
SKIP_TAGS = {"script", "style", "head", "title"}
BREAK_AFTER = "。、！？!?）)」』】♪★☆▼▲　 "
_URL_RE = re.compile(r"^https?://\S+$")


@dataclass
class _Converter(HTMLParser):
    lines: list[str] = field(default_factory=list)
    _cur: list[str] = field(default_factory=list)
    _skip: int = 0
    _href: str | None = None
    _anchor_text: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__init__(convert_charrefs=True)

    # --- 行の管理 ---
    def _flush(self) -> None:
        text = re.sub(r"[ \t ]+", " ", "".join(self._cur)).strip()
        self._cur = []
        if text:
            self.lines.append(text)

    def _blank(self) -> None:
        self._flush()
        if self.lines and self.lines[-1] != "":
            self.lines.append("")

    # --- HTML のイベント ---
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "br":
            self._flush()
        elif tag == "hr":
            self._blank()
            self.lines.append(HR_LINE)
            self.lines.append("")
        elif tag == "a":
            self._href = dict(attrs).get("href")
            self._anchor_text = []
        elif tag in BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag == "a":
            self._close_anchor()
        elif tag in {"td", "th"}:
            self._flush()
        elif tag == "p" or tag in {"h1", "h2", "h3", "h4", "h5", "h6", "table"}:
            self._blank()
        elif tag in BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        data = data.replace("\r", "").replace("\n", " ")
        self._cur.append(data)
        if self._href is not None:
            self._anchor_text.append(data)

    def _close_anchor(self) -> None:
        href, self._href = self._href, None
        text = "".join(self._anchor_text).strip()
        self._anchor_text = []
        if not href or not href.lower().startswith(("http://", "https://")):
            return
        # リンクの文字はそのまま行に残っている。その次の行に URL を出す。
        self._flush()
        self.lines.append(href.strip())
        del text  # 画像だけのリンクは、文字が無いので URL だけが残る

    def result(self) -> list[str]:
        self._flush()
        return self.lines


def html_to_lines(html: str) -> list[str]:
    conv = _Converter()
    conv.feed(html)
    conv.close()
    return tidy(conv.result())


def tidy(lines: list[str]) -> list[str]:
    """連続した同じ行・余分な空行・先頭末尾の空行を整える。"""
    out: list[str] = []
    for ln in lines:
        ln = ln.rstrip()
        if ln == "":
            if out and out[-1] != "":
                out.append("")
            continue
        if out and out[-1] == ln:
            continue
        # 区切り線が連続したら 1 つにする
        if ln == HR_LINE and out and out[-1] == HR_LINE:
            continue
        # 「■」で始まる見出しの前は空行を 1 つ入れる
        if ln.startswith("■") and out and out[-1] != "":
            out.append("")
        out.append(ln)
    while out and out[0] == "":
        out.pop(0)
    while out and out[-1] == "":
        out.pop()
    return group_items(out)


def _is_url(line: str) -> bool:
    return bool(_URL_RE.match(line))


def group_items(lines: list[str]) -> list[str]:
    """商品名・価格・URL を 1 かたまりに詰め、かたまりの間を空行 1 つにする。"""
    # 同じ URL が近く(3 行以内)に 2 回あるときは、前の方を消す(画像リンクと文字リンクの重複)
    drop: set[int] = set()
    for i, ln in enumerate(lines):
        if not _is_url(ln):
            continue
        ahead = [x for x in lines[i + 1 : i + 8] if x != ""][:3]  # 空行を数えずに 3 行先まで
        if ln in ahead:
            drop.add(i)
    kept = [ln for i, ln in enumerate(lines) if i not in drop]
    out: list[str] = []
    for i, ln in enumerate(kept):
        nxt = kept[i + 1] if i + 1 < len(kept) else None
        if ln == "":
            # 空行の直後が価格(数字で始まる行)または URL なら、空行を捨てて詰める
            if nxt and (_is_url(nxt) or re.match(r"^[0-9,]+", nxt)) and out and out[-1] != "":
                continue
            out.append(ln)
            continue
        out.append(ln)
        # URL の後に、続きの文(URL・「＞」・区切り線でも空行でもない)が来るなら空行を入れる
        if _is_url(ln) and nxt and nxt != "" and not _is_url(nxt) and not nxt.startswith("＞") and nxt != HR_LINE:
            out.append("")
    return out


def wrap_line(line: str, width: int = WIDTH) -> list[str]:
    """width 文字を超える行を、切れ目で折り返す。URL(空白なしの https://...)は切らない。"""
    if len(line) <= width or _URL_RE.match(line):
        return [line]
    out: list[str] = []
    rest = line
    while len(rest) > width:
        window = rest[:width]
        cut = max((window.rfind(ch) for ch in BREAK_AFTER), default=-1)
        # URL の途中で切らない: 切れ目の後ろに http が始まるなら、その手前で切る
        m = re.search(r"https?://", rest)
        if m and 0 < m.start() <= width:
            cut = m.start() - 1
        if cut < width // 3:  # 良い切れ目が前すぎる/無いときは、幅いっぱいで切る
            cut = width - 1
        out.append(rest[: cut + 1].rstrip())
        rest = rest[cut + 1 :].lstrip()
    if rest:
        out.append(rest)
    return out


def drop_leading_urls(lines: list[str]) -> list[str]:
    """先頭の、文字が付かない URL だけの行(バナー画像のリンク)を外す。本文より前にある URL だけが対象。"""
    i = 0
    while i < len(lines) and (lines[i] == "" or _URL_RE.match(lines[i])):
        i += 1
    return lines[i:]


def convert(html: str, width: int = WIDTH, keep_leading_url: bool = False) -> str:
    lines: list[str] = []
    src = html_to_lines(html)
    for ln in src if keep_leading_url else drop_leading_urls(src):
        lines.extend(wrap_line(ln, width))
    text = "\n".join(lines)
    return unicodedata.normalize("NFC", text) + "\n"


# ----------------------------------------------------------------- 検査 ----
@dataclass
class Report:
    chars: int
    chars_crlf: int
    lines: int
    longest: int
    over_width: list[str]
    unencodable: list[str]

    @property
    def over_limit(self) -> int:
        return max(0, self.chars_crlf - LIMIT)


def inspect(text: str, width: int = WIDTH) -> Report:
    ls = text.rstrip("\n").split("\n")
    body = text.rstrip("\n")
    bad = sorted({ch for ch in body if ch != "\n" and not _sjis_ok(ch)})
    return Report(
        chars=len(body),
        chars_crlf=len(body) + body.count("\n"),
        lines=len(ls),
        longest=max((len(x) for x in ls), default=0),
        over_width=[x for x in ls if len(x) > width and not _URL_RE.match(x)],  # URL の行は例外(長さ無制限)
        unencodable=bad,
    )


def _sjis_ok(ch: str) -> bool:
    try:
        ch.encode("cp932")
    except UnicodeEncodeError:
        return False
    return True


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp932")


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    ap = argparse.ArgumentParser(description="メルマガ HTML → 携帯メルマガ用テキスト")
    ap.add_argument("input", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--width", type=int, default=WIDTH)
    ap.add_argument("--limit", type=int, default=LIMIT)
    ap.add_argument("--keep-leading-url", action="store_true", help="先頭のバナー URL を残す")
    a = ap.parse_args(argv)
    text = convert(read_text(a.input), a.width, a.keep_leading_url)
    rep = inspect(text, a.width)
    over = max(0, rep.chars_crlf - a.limit)
    print(f"文字数: {rep.chars}(改行を 2 文字と数えると {rep.chars_crlf}) / 上限 {a.limit}")
    print(f"行数: {rep.lines} / 最長行: {rep.longest} 文字(上限 {a.width})")
    status = 0
    if over:
        print(f"【超過】{over} 文字オーバーです。保存しません。削る候補を決めてください。")
        status = 2
    if rep.over_width:
        print(f"【注意】{a.width} 文字を超える行が {len(rep.over_width)} 本あります(URL の行は除く)。")
        for x in rep.over_width:
            print(f"  ({len(x)}文字) {x[:60]}...")
        status = status or 3
    if rep.unencodable:
        print("【注意】Shift_JIS(携帯で使われる文字コード)に無い文字: " + " ".join(rep.unencodable))
    if a.out and status != 2:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(text, encoding="utf-8", newline="\n")
        print(f"保存: {a.out}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
