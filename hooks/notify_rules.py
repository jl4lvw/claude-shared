"""通知ルール(判定エンジン + スイッチ + 判定の記録) (2026-10-17)。

背景: 「対応が必要なのに通知されない」取りこぼしを、言い回しをコードに足し続ける方式では
直せなかった (HQ ボード 318 件の試算で、返信待ち 57 件のうち現行の判定が拾うのは 19 件)。
そこで判定を「語句のデータ」にし、画面(relay-viewer の「通知」タブ)で足し引きできるようにする。

判定の流れ (Stop のときだけ。本文はペイロードの last_assistant_message):
  ① 末尾の目印 (既定 【対応待ち】) があれば通知 — AI が自分で「待っている」と宣言した場合
  ② 「無視する文」(例: セッション名は…にしました) を取り除き、「」の中身は判定から外す
  ③ 末尾 500 字を 1 文ずつ見て、依頼・質問の文が 1 つでもあれば通知
     (文末が「？」「ください」「お願いします」「ですか」…、または「依頼する語句」を含む文)
  ④ ただし「除外する語句」(例: 承認済み / 気になったら / 必要であれば) が入っている文の依頼は打ち消す。
     別の文の依頼は生きる (条件つきの依頼と本物の依頼が同じ返答に混ざっていても拾える)
語句は「そのまま一致」だけ。正規表現は使わない (重い式でフックが固まらない・画面のテストと同じ結果になる)。

設定の出所 (先に見つかった有効なものを使う): relay_local/notify_rules.json (端末の上書き・非常用)
  → relay_local/notify_rules_cache.json (041 から取得したキャッシュ) → 下の DEFAULTS。
壊れていても通知は止めない (sanitize で不正な項目だけ捨てる)。

判定の記録: relay_local/notify_decisions.jsonl に Stop ごとの判定と理由を残す。末尾 300 字は
伏字化 (redact) してある。041 へ送るのは別のプロセス (notify_sync.py) で、CLAUDE_NOTIFY_UPLOAD=0 なら送らない。
"""
from __future__ import annotations

import copy
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from notify_common import LOCAL, file_lock, log

RULES_LOCAL = LOCAL / "notify_rules.json"
RULES_CACHE = LOCAL / "notify_rules_cache.json"
DECISIONS = LOCAL / "notify_decisions.jsonl"

TAIL_CHARS = 500          # 依頼語句を探す範囲 (末尾)
SNIPPET_CHARS = 300       # 判定の記録に残す本文の長さ
DECISIONS_MAX_LINES = 400
DECISIONS_KEEP_LINES = 200
MAX_RULES = 500
MAX_PHRASE = 80
MAX_MUTED = 100

KINDS = ("permission", "question", "reply", "idle", "confirm")
CHANNELS = ("sound", "speech", "phone")
RULE_TYPES = ("req", "exc", "ign")
BUILTINS = ("marker", "question_end", "request_end", "options_list", "english")

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _rules(kind: str, phrases: list[str]) -> list[dict]:
    return [{"type": kind, "phrase": p, "on": True} for p in phrases]


DEFAULTS: dict = {
    "version": 0,
    "marker": "【対応待ち】",
    "builtin": {name: True for name in BUILTINS},
    "rules": (
        _rules("req", [
            "教えてください", "教えて下さい", "お願いします", "お願いいたします", "お願い致します",
            "ご確認ください", "確認をお願い", "どちらで進め", "どちらでも進め", "伝えてください",
            "ご指示", "お知らせください", "決めてください", "選んでください", "ご判断",
            "承認してください", "よろしいですか", "いかがですか", "進めてよいですか", "ご連絡ください",
            "指定してください", "入力してください", "返信をお願い", "回答をお願い",
        ])
        + _rules("exc", [
            "承認済み", "確認済み", "対応済み", "承認不要", "気になったら", "必要であれば", "必要なら",
            "場合は教えて", "直らない場合", "もしよければ", "よければ", "問題があれば", "何かあれば", "あれば教えて",
            "来たら", "出たときは", "出たら", "遅れたら", "また不具合", "必要になったら", "ほしくなったら", "ときは貼って",
        ])
        + _rules("ign", ["セッション名は", "セッション名を"])
    ),
    "switches": {k: {"sound": True, "speech": True, "phone": True} for k in KINDS},
    "quiet": {"phone": {"on": False, "from": "22:00", "to": "07:00"}},
    "muted": [],
}


# --- 設定の読み込みと検証 ------------------------------------------------------------------
def _bool(v: object, default: bool) -> bool:
    return v if isinstance(v, bool) else default


def sanitize(raw: object) -> dict:
    """任意の JSON を、安全に使える設定へ直す。不正な項目は捨て、足りない項目は既定で埋める。"""
    cfg = copy.deepcopy(DEFAULTS)
    if not isinstance(raw, dict):
        return cfg
    ver = raw.get("version")
    cfg["version"] = ver if isinstance(ver, int) and not isinstance(ver, bool) else 0
    marker = raw.get("marker")
    if isinstance(marker, str) and 1 <= len(marker.strip()) <= 30:
        cfg["marker"] = marker.strip()
    b = raw.get("builtin")
    if isinstance(b, dict):
        cfg["builtin"] = {name: _bool(b.get(name), True) for name in BUILTINS}
    if isinstance(raw.get("rules"), list):
        seen: set[tuple[str, str]] = set()
        rules: list[dict] = []
        for r in raw["rules"]:
            if not isinstance(r, dict) or r.get("type") not in RULE_TYPES:
                continue
            phrase = r.get("phrase")
            if not isinstance(phrase, str) or not (1 <= len(phrase.strip()) <= MAX_PHRASE):
                continue
            key = (r["type"], phrase.strip())
            if key in seen:
                continue
            seen.add(key)
            rules.append({"type": r["type"], "phrase": phrase.strip(), "on": _bool(r.get("on"), True)})
            if len(rules) >= MAX_RULES:
                break
        cfg["rules"] = rules
    sw = raw.get("switches")
    if isinstance(sw, dict):
        for k in KINDS:
            row = sw.get(k)
            if isinstance(row, dict):
                cfg["switches"][k] = {c: _bool(row.get(c), True) for c in CHANNELS}
    q = raw.get("quiet")
    if isinstance(q, dict) and isinstance(q.get("phone"), dict):
        p = q["phone"]
        cfg["quiet"]["phone"] = {
            "on": _bool(p.get("on"), False),
            "from": p["from"] if isinstance(p.get("from"), str) and _HHMM.match(p["from"]) else "22:00",
            "to": p["to"] if isinstance(p.get("to"), str) and _HHMM.match(p["to"]) else "07:00",
        }
    m = raw.get("muted")
    if isinstance(m, list):
        cfg["muted"] = [s.strip() for s in m if isinstance(s, str) and 1 <= len(s.strip()) <= 80][:MAX_MUTED]
    return cfg


def _read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def load_config(local: Path | None = None, cache: Path | None = None) -> dict:
    """端末の上書き → 041 のキャッシュ → 既定、の順で最初に読めたものを使う。"""
    for path in (RULES_LOCAL if local is None else local, RULES_CACHE if cache is None else cache):
        raw = _read_json(path)
        if isinstance(raw, dict):
            return sanitize(raw)
    return copy.deepcopy(DEFAULTS)


# --- 判定 ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Decision:
    notify: bool
    reason: str
    rule: str = ""


_CLOSERS = r"[」』）)\]\"'*_`\s。．.!！]*"
_QUESTION_END = re.compile(r"[?？]" + _CLOSERS + r"$")
_REQUEST_END = re.compile(
    r"(?:ください|下さい|お願いします|お願いいたします|お願い致します|ですか|ますか|でしょうか|ませんか"
    r"|いかがでしょうか|よろしいですか|ほしいです|欲しいです)" + _CLOSERS + r"$"
)
_OPTIONS_AFTER_QUESTION = re.compile(
    r"[?？][^\n]*\n(?:[ \t]*(?:[-*・]|\d+[.．、)])[ \t]*\S[^\n]*\n?){2,}\s*$"
)
_ENGLISH_REQUEST = re.compile(
    r"\b(?:please|kindly|let me know|could you|can you|would you|which|what|how should)\b[^.!?\n]*[.!]?\s*$",
    re.IGNORECASE,
)
_PAREN_TAIL = re.compile(r"[（(][^）)]{0,80}[）)]([。.!！\s]*)$")
# 文の区切り。URL のクエリ(?x=1)の「?」では割らない (直後が英数字や = & % _ . / # - のとき)
_SPLIT_AFTER = r"(?<=[。！!])|(?<=[？?])(?![A-Za-z0-9=&%_./#-])"
_LINE_SPLIT = re.compile(_SPLIT_AFTER)
_SENTENCE_SPLIT = re.compile(_SPLIT_AFTER + r"|\n")


def _strip_noise(text: str, ign: list[str]) -> str:
    """マークダウンの装飾と「無視する文」を取り除く (改行は保つ)。"""
    plain = re.sub(r"[*_`]", "", text)
    lines = []
    for line in plain.split("\n"):
        sents = [s for s in _LINE_SPLIT.split(line) if s.strip()]
        lines.append("".join(s for s in sents if not any(p in s for p in ign)))
    return "\n".join(lines).strip()


def _mask_quotes(text: str) -> str:
    """「…」『…』の中身を同じ長さの記号に置き換える (引用の中の「？」や依頼語に反応しない)。"""
    return re.sub(r"[「『][^」』\n]{0,200}[」』]", lambda m: "▒" * len(m.group()), text)


def split_sentences(text: str, ign: list[str] | None = None) -> list[str]:
    """本文を判定と同じ手順で文に分ける (画面のルール候補づくりが、判定と同じ見方をするため)。"""
    body = _mask_quotes(_strip_noise(text.strip(), ign or []))
    return [s.strip() for s in _SENTENCE_SPLIT.split(body) if s.strip()]


def _sentence_requests(sentence: str, builtin: dict, req_phrases: list[str]) -> tuple[str, str] | None:
    """1 文が「ユーザーへの依頼・質問」か。(理由, 語句) を返す。"""
    s = _PAREN_TAIL.sub(r"\1", sentence).strip()
    if builtin["question_end"] and _QUESTION_END.search(s):
        return "疑問符で終わる文", ""
    if builtin["request_end"] and _REQUEST_END.search(s):
        return "依頼の形で終わる文", ""
    if builtin["english"] and _ENGLISH_REQUEST.search(s):
        return "英語の依頼", ""
    for p in req_phrases:
        if p in s:
            return "依頼語句", p
    return None


def evaluate(text: object, cfg: dict | None = None) -> Decision:
    """Stop の最終応答が「ユーザーの対応待ち」か判定する。

    末尾 TAIL_CHARS 字を 1 文ずつ見て、依頼・質問の文が 1 つでもあれば通知する。
    除外する語句は、それが入っている文の依頼だけを打ち消す (別の文の依頼は生きる)。
    """
    cfg = load_config() if cfg is None else cfg
    if not isinstance(text, str) or not text.strip():
        return Decision(False, "本文なし")
    raw = text.strip()
    builtin = cfg["builtin"]
    if builtin["marker"] and cfg["marker"] in raw[-120:]:
        return Decision(True, "目印", cfg["marker"])

    active = [r for r in cfg["rules"] if r["on"]]
    body = _strip_noise(raw, [r["phrase"] for r in active if r["type"] == "ign"])
    if not body:
        return Decision(False, "無視する文だけだった")
    tail = _mask_quotes(body)[-TAIL_CHARS:]
    exc = [r["phrase"] for r in active if r["type"] == "exc"]
    req = [r["phrase"] for r in active if r["type"] == "req"]

    excluded = ""
    for sentence in (s.strip() for s in _SENTENCE_SPLIT.split(tail)):
        if not sentence:
            continue
        hit = _sentence_requests(sentence, builtin, req)
        if hit is None:
            continue
        veto = next((p for p in exc if p in sentence), "")
        if veto:
            excluded = veto
            continue
        return Decision(True, hit[0], hit[1])
    if builtin["options_list"] and _OPTIONS_AFTER_QUESTION.search(tail[-400:].rstrip()):
        return Decision(True, "質問と選択肢の一覧")
    if excluded:
        return Decision(False, "除外語句", excluded)
    return Decision(False, "一致なし")


# --- スイッチ・静音・ミュート --------------------------------------------------------------------
def in_quiet_hours(cfg: dict, now: datetime | None = None) -> bool:
    q = cfg["quiet"]["phone"]
    if not q["on"]:
        return False
    cur = (now or datetime.now()).strftime("%H:%M")
    start, end = q["from"], q["to"]
    if start == end:
        return False
    return start <= cur < end if start < end else (cur >= start or cur < end)


def is_muted(cfg: dict, sid: str, title: str) -> bool:
    return any(m and (m == sid or m in title) for m in cfg["muted"])


def channel_allowed(cfg: dict, kind: str, channel: str, sid: str = "", title: str = "",
                    now: datetime | None = None) -> bool:
    """種類×チャンネルのスイッチ、ミュート中のセッション、スマホの静音時間をまとめて見る。"""
    if not cfg["switches"].get(kind, {}).get(channel, True):
        return False
    if is_muted(cfg, sid, title):
        return False
    return not (channel == "phone" and in_quiet_hours(cfg, now))


# --- 判定の記録 -----------------------------------------------------------------------------------
_SECRET_PATTERNS = [
    (re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd|pin|authorization|bearer)\b\s*[:=]?\s*[^\s,;」』）)]{4,}"),
     r"\1=***"),
    (re.compile(r"[A-Za-z0-9_\-]{24,}"), "***"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "***@***"),
    (re.compile(r"\b\d{7,}\b"), "***"),
]


def redact(text: str) -> str:
    """判定の記録に残す本文から、鍵・長い英数字・メールアドレス・長い数字列を伏せる。"""
    for pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


def host_name() -> str:
    """端末名。relay の自分の user_id (A/TK…) を優先し、無ければコンピューター名。"""
    try:
        for line in (LOCAL / ".env").read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("RELAY_SELF_USER_ID="):
                v = line.partition("=")[2].strip().strip('"').strip("'")
                if v:
                    return v
    except OSError:
        pass
    return os.environ.get("COMPUTERNAME", "")


def record_decision(sid: str, title: str, text: str, d: Decision, path: Path | None = None) -> None:
    """Stop ごとの判定と理由を残す (画面①の元データ)。書けなくても通知は止めない。"""
    path = DECISIONS if path is None else path
    try:
        entry = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "host": host_name(),
            "sid": sid,
            "title": title[:80],
            "notify": d.notify,
            "reason": d.reason,
            "rule": d.rule,
            "tail": redact(text.strip()[-SNIPPET_CHARS:]),
        }
        line = json.dumps(entry, ensure_ascii=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(path):
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) > DECISIONS_MAX_LINES:
                path.write_text("\n".join(lines[-DECISIONS_KEEP_LINES:]) + "\n", encoding="utf-8")
    except OSError as exc:
        log(f"判定の記録に失敗: {type(exc).__name__}")
