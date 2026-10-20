"""返答が英語になっていたら差し戻す Stop フック(2026-10-20 運用者決定: 「この会話に書き込む言葉はすべて日本語」を機械で守る)。

CLAUDE.md の 1 行目に最優先のルールとして書いたあとも、英語の資料・ツールの出力・要約のあとに英語で返答する事故が
同じ日に 4 回起きた。文字のルールだけでは止まらないので、送る直前に確かめる。

- 対象: そのターンの最後の返答(ペイロードの last_assistant_message)と、同じターンの途中で書いた文(トランスクリプト)。
- 判定: コード(``` と `)・URL・ファイルのパスを除いてから、**英語の文にしか出てこない言葉**(the / is / I'll / next など)を数える。
  MIN_STOPWORDS 個以上あり、かつ日本語の文字(ひらがな・カタカナ・漢字)の割合が JA_RATIO 未満なら英語とみなす。
  文字の割合だけで決めると、ファイル名や英語の用語が多い短い日本語の文(「main.py の startup と…」)まで英語と判定した
  (過去の記録 31,021 文で 693 文。2026-10-20)。
- 英語なら 1 回だけ差し戻す(decision: block)。2 回目(stop_hook_active)は通して記録だけ残す(止まらなくなるのを防ぐ)。
- 途中の文は、もう画面に出ているので取り消せない。差し戻しの理由で「最後の返答で日本語で書き直す」よう求める。
- 環境変数 JAPANESE_GUARD_OFF=1 で止められる。記録は .claude/logs/japanese_guard.jsonl。
- どんな失敗でも返答は止めない(フック自体の不具合で会話を止めない)。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

MIN_STOPWORDS = 2  # 「I'm calling now.」(I'm・now)も拾う
JA_RATIO = 0.35  # 日本語の返答は 0.5 以上、英語の返答は日本語の表を含んでも 0.2 前後(2026-10-20 の記録で確かめた)
STOPWORDS = frozenset("""
the a an is are was were be been being am do does did done have has had will would shall should can could may might must
i i'm i'll i've i'd we we'll we're you you're your it it's its this that these those there here they them their
and or but so because if then than when while which who what where why how not no yes
to of in on at for with from by about into after before over under up out off as
let let's me my our us next now first also just only still already again more less all any some each both
here's there's that's what's done ok okay please thanks
""".split())
TAIL_BYTES = 400_000

_CODE_BLOCK = re.compile(r"```.*?```", re.S)
_INLINE = re.compile(r"`[^`\n]*`")
_URL = re.compile(r"https?://\S+|\]\([^)]*\)")
_PATH = re.compile(r"\S*[/\\]\S*")
_TABLE_OR_QUOTE = re.compile(r"^\s*[|>].*$", re.M)  # 表の行・引用の行(英語の名前や原文が並ぶので数えない)
_JA = re.compile(r"[぀-ヿ㐀-鿿ｦ-ﾟ]")
_LATIN = re.compile(r"[A-Za-z]")
_WORD = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")

REASON_FINAL = ("【日本語ルール】直前の返答が英語でした。CLAUDE.md の最優先ルール(この会話に書き込む言葉はすべて日本語)に従い、"
                "同じ内容を日本語で書き直して返答し直してください。コード・識別子・固有名詞は英語のままでかまいません。")
REASON_MIDWAY = ("【日本語ルール】このターンの途中で英語の文を書きました。CLAUDE.md の最優先ルールに従い、最後の返答で、"
                 "その英語の部分の内容も含めて日本語で書き直してください。")


def strip_noise(text: str) -> str:
    """数えない部分(コード・表の行・引用の行・URL・パス)を除く。"""
    for pat in (_CODE_BLOCK, _TABLE_OR_QUOTE, _INLINE, _URL, _PATH):
        text = pat.sub(" ", text)
    return text


def is_english(text: str) -> bool:
    t = strip_noise(text or "")
    stop = sum(1 for w in _WORD.findall(t) if w.lower() in STOPWORDS)
    if stop < MIN_STOPWORDS:
        return False
    latin = len(_LATIN.findall(t))
    ja = len(_JA.findall(t))
    return ja / (ja + latin) < JA_RATIO


def _blocks(entry: dict) -> list:
    msg = entry.get("message") if isinstance(entry.get("message"), dict) else {}
    content = msg.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content if isinstance(content, list) else []


def turn_texts(transcript: Path, tail_bytes: int = TAIL_BYTES) -> list[str]:
    """最後の運用者の発言より後に書いた返答の文(古い順)。ツールの結果は運用者の発言に数えない。"""
    try:
        with open(transcript, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    texts: list[str] = []
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        if e.get("type") == "user":
            blocks = _blocks(e)
            if any(isinstance(b, dict) and b.get("type") == "text" and not e.get("isMeta") for b in blocks):
                break  # 運用者の発言(ツールの結果だけの行は飛ばす)
            continue
        if e.get("type") == "assistant":
            for b in reversed(_blocks(e)):
                if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
                    texts.append(b["text"])
    return list(reversed(texts))


def judge(payload: dict) -> dict | None:
    """差し戻すなら {"decision": "block", "reason": ...}。通すなら None。"""
    final = payload.get("last_assistant_message")
    final = final if isinstance(final, str) else ""
    english_final = is_english(final)
    english_mid = False
    tp = payload.get("transcript_path")
    if isinstance(tp, str) and tp:
        mids = [t for t in turn_texts(Path(tp)) if t.strip() and t.strip() != final.strip()]
        english_mid = any(is_english(t) for t in mids)
    if not (english_final or english_mid):
        return None
    _log(payload, "final" if english_final else "midway", final)
    if payload.get("stop_hook_active"):
        return None  # 差し戻しは 1 回だけ(止まらなくなるのを防ぐ)
    return {"decision": "block", "reason": REASON_FINAL if english_final else REASON_MIDWAY}


def _log(payload: dict, kind: str, final: str) -> None:
    try:
        root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
        d = root / ".claude" / "logs"
        d.mkdir(parents=True, exist_ok=True)
        rec = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "session": payload.get("session_id"), "kind": kind,
               "retry": bool(payload.get("stop_hook_active")), "head": final[:120]}
        with open(d / "japanese_guard.jsonl", "a", encoding="utf-8", newline="") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    if os.environ.get("JAPANESE_GUARD_OFF") == "1":
        return 0
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
        out = judge(payload) if isinstance(payload, dict) else None
    except Exception:  # noqa: BLE001  フックの不具合で会話を止めない
        return 0
    if out is not None:
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
