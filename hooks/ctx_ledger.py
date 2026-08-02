"""文脈台帳 (.ctx) の共通ロジック — パス決定・排他追記・パース・伏字化・圧縮検知。

背景 (2026-07-30 初版 / 2026-08-02 再設計):
  コンテキスト圧縮は「作業の流れ」を要約として残す一方、**セッション限定の制約 /
  得た承認 / 起動中プロセス / 試して失敗した方法 / 検証状態 / 生の値** を落とす。
  CLAUDE.md と MEMORY.md は毎回再注入されるので恒久ルールは生き残るが、
  セッション内だけで有効な情報を保全する層がどこにも無かった。

2026-08-02 再設計の要点 (6視点並列レビューの結論):
  1. 圧縮検知の主経路は **トランスクリプト jsonl の compact_boundary 行の増分走査**。
     PostCompact フックはこの環境 (デスクトップアプリ) で stdin が空になり、
     本番の圧縮 3/3 回すべてを取り逃した実績がある。トランスクリプトには
     `{"type":"system","subtype":"compact_boundary",...}` が 100% 記録される
     (全 jsonl 走査 78/78 イベントで構造確認済み) ので、こちらを一次ソースにする。
  2. **沈黙して失敗する経路をゼロにする**。stdin の読取は read_payload() に集約し、
     失敗は必ず log() に残す。「return する分岐の直前には必ず log がある」を規約とする。
     (旧実装は json.load 失敗を無ログ exit 0 しており、障害が3日間潜伏した)
  3. 行スタンプに日付を付ける ("MM-DD HH:MM")。セッションは1ヶ月続くことがあり
     (実測: 7/5 開始のセッションが 8/2 も継続)、HH:MM だけでは承認の鮮度が判らない。
     旧形式 (HH:MM のみ) の行も読める後方互換パーサにする。
  4. 台帳本文はサニタイズする (マーカー偽造対策)。台帳の行は additionalContext として
     会話へ注入されるため、本文に "[ctx]" や "✅" を混ぜて偽の承認行を作る
     プロンプトインジェクションが実証された。追記時と注入時の両方で無害化する。

行フォーマット:
  "<MM-DD HH:MM> <TAG> <本文>"  (旧形式 "<HH:MM> <TAG> <本文>" も読める)
  取消は DROP 行で **その時点までの有効な同一行1件** を無効化する (時系列処理)。
  グローバル完全一致にすると、staging の起動→停止→再起動のように同じ本文が
  再登場するケースで再追加分まで永久に消えてしまう。

セッション同一性:
  session_id > transcript_path の stem (uuid 形式ならそのまま採用) > 環境変数
  の順で決める。どれも取れなければ None (fail closed)。共有の "default" に
  落とすと別セッションの制約・承認が混入し、誤った作業許可につながる。
  transcript の basename は常に <session_id>.jsonl (実データで確認) なので、
  stem 採用ならハッシュ化と違い session_id 版の台帳と分裂しない。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

try:
    import msvcrt  # Windows のファイルロック
except ImportError:  # pragma: no cover — 非 Windows
    msvcrt = None  # type: ignore[assignment]

CTX_DIR = Path(os.environ.get("CLAUDE_CTX_DIR", r"C:/ClaudeCode/.ctx"))
LOG_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ClaudeCodeCtx"

# log() の発生源タグ。各フックが import 直後に上書きする
# (例: L._SRC = "inject")。ログ行から発生元を特定できないと事後診断が
# 不可能になる (2026-07-30 のログ3行の正体特定に法医学調査が必要だった)。
_SRC = "ledger"

LOG_ROTATE_BYTES = 512 * 1024  # これを超えたら ctx_hook.log → ctx_hook.log.1

# 台帳が異常に育った場合にログへ注意を残す閾値。
# **読み飛ばしはしない**。部分読みにすると古いが未 DROP の LIMIT/OK/STATE が
# 押し出されて注入されなくなり、中心保証が壊れる。実測: 10MB 全読みでも 300ms 弱。
WARN_SIZE_BYTES = 1024 * 1024

# 1本文の上限。注入枠 (1500字) を1行で食い潰す DoS と、長文に紛れた
# 偽指示の埋め込みを抑える。
MAX_BODY_CHARS = 300

# TAG -> 表示ラベル
TAGS: dict[str, str] = {
    "LIMIT": "🚫制約",
    "OK": "✅承認",
    "STATE": "⚙️状態",
    "VAL": "🔢値",
    "DEAD": "💀失敗",
    "VERIFY": "🔬検証",
    "COMPACT": "圧縮",
    "DROP": "取消",
}

# 圧縮直後に注入する枠 (事故に直結する3枠のみ。残りは台帳を読めば足りる)
INJECT_TAGS: tuple[str, ...] = ("LIMIT", "OK", "STATE")

# 旧形式 "HH:MM" と新形式 "MM-DD HH:MM" の両方を受理する。
# 時刻は実在するものに限る (旧 LINE_RE は "25:99" も受理していた)。
LINE_RE = re.compile(
    r"^(?P<stamp>(?:\d{2}-\d{2}\s+)?(?:[01]?\d|2[0-3]):[0-5]\d)"
    r"\s+(?P<tag>[A-Z]+)\s+(?P<body>.+)$"
)
CREATED_RE = re.compile(r"^#\s*created:\s*(?P<stamp>.+)$")
LEDGER_ID_RE = re.compile(r"^#\s*ledger-id:\s*(?P<id>\S+)$")
_UUID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")

# ---------------------------------------------------------------------------
# 伏字化 (redact)
# ---------------------------------------------------------------------------
# 台帳に値そのものを書かせないのは SKILL.md 側の規律だが、万一書かれた場合に
# 備えて **追記時と注入時の両方** で落とす (多重防御)。
# 2026-08-02 の突破実験で判明した穴を塞いだ:
#   - ラベル無しのトークン形状 (sk-ant- / ghp_ / AKIA / xoxb / JWT) が素通り
#   - 全角区切り (＝ ：) と全角数字が素通り
#   - 日本語ラベルの表記揺れ (API キー / API・キー) が素通り
#   - 値の先頭トークンが3文字未満だとパターン全体が不成立 (pin=42 が素通り)
#   - 空白入りパスフレーズ (password: my secret ...) は先頭語しか落ちない

# 値の形。**非引用の値は ASCII 印字文字に限定する** (空白・引用符を除く)。
# CJK まで飲む `[^\s"']+` は「PIN=***ポート用のもの」を再 redact で「PIN=***」に
# 縮める非冪等性を生み、drop の一致・注入内容の破壊につながった (2026-08-03 レビュー)。
# `*` を含めるのは redact("key=***")==("key=***") の冪等性のため。
_ASCII_TOKEN = r"[!#-&(-~]+"  # 印字 ASCII から空白・"・' を除いた1トークン
_VALUE = rf"(?:\"[^\"]{{3,}}\"|'[^']{{3,}}'|{_ASCII_TOKEN})"

# password/passwd は空白入りパスフレーズがありうるので ASCII トークンを最大4つまで
_PHRASE_VALUE = rf"{_ASCII_TOKEN}(?:[ ]{_ASCII_TOKEN}){{0,3}}"

# 「〜は」区切りの値。ASCII のシークレットらしい並び + 全角数字に限定する。
# 制限しないと `PIN はユーザーに口頭確認済` のような **推奨される書き方** まで
# 潰れる（実測で確認）。全角数字は log() 経由の生 redact (NFKC 前) 用に残す。
_ASCII_VALUE = r"(?:\"[^\"]{3,}\"|'[^']{3,}'|[A-Za-z0-9_\-./+=:]{4,}|[０-９]{4,})"

_KEYS = (
    r"pin|password|passwd|secret|api[-_ ]?key|access[-_ ]?token"
    r"|refresh[-_ ]?token|token|client[-_ ]?secret"
)
# SECRET_KEY / GITHUB_TOKEN / WRITE_PIN のような合成名。\b は `_` に効かないため
# 専用の境界を使う (実測: SECRET_KEY=... が素通りしていた)。
# 直後の小文字は除外して max_tokens 等の複数形 FP を避ける。
_COMPOUND_KEY = (
    r"(?<![A-Za-z0-9])[A-Za-z0-9_\-]*"
    r"(?:secret|token|passwd|password|api[_-]?key|pin)(?![a-z])[A-Za-z0-9_\-]*"
)
_JP_KEYS = r"パスワード|暗証番号|認証キー|ＡＰＩキー|API[\s・]?キー|アクセス\s*トークン|トークン|秘密鍵"

# 区切り: 半角/全角のコロン・イコール
_SEP = r"[:=：＝]"

# 順序が重要。`Authorization: Bearer xxx` は Bearer/Basic を先に処理しないと、
# 汎用 key=value が "Bearer" だけを消費して token 本体が残る（実測でリークを確認）。
# 行末まで喰うパターンは禁止 — 「password: は本人が入れる。本番DBは触らない」の
# 後半 (制約の実体) まで消して意味を破壊した実測がある。値トークンだけを落とす。
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # スキーム付き資格情報
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{6,}"), "Bearer ***"),
    (re.compile(r"(?i)\bbasic\s+[A-Za-z0-9+/=]{8,}"), "Basic ***"),
    # Authorization: <生トークン> (スキーム無し)
    (re.compile(rf"(?i)\bauthorization\b\s*(?:{_SEP}|は)\s*{_ASCII_TOKEN}"), "Authorization=***"),
    # ラベル無しトークン形状 (最も遭遇しやすい直書きを拾う)
    (re.compile(r"(?i)\bsk-ant-[A-Za-z0-9_\-]{8,}"), "sk-ant-***"),
    (re.compile(r"(?i)\bsk-[A-Za-z0-9]{20,}"), "sk-***"),
    (re.compile(r"(?i)\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"), "gh*_***"),
    (re.compile(r"(?i)\bgithub_pat_[A-Za-z0-9_]{20,}"), "github_pat_***"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "AKIA***"),
    (re.compile(r"(?i)\bxox[baprse]-[A-Za-z0-9\-]{10,}"), "xox***"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "AIza***"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{4,}"), "eyJ***"),
    # 接続文字列の user:pass@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^\s:/@]+:[^\s@]+@"), r"\1***:***@"),
    # 空白入りパスフレーズ: password/passwd は ASCII トークン最大4つまで
    (re.compile(rf"(?i)\b(password|passwd)\b\s*{_SEP}\s*{_PHRASE_VALUE}"), r"\1=***"),
    # key: value / key="value"
    (re.compile(rf"(?i)\b({_KEYS})\b\s*{_SEP}\s*{_VALUE}"), r"\1=***"),
    # SECRET_KEY=... / GITHUB_TOKEN=... 等の合成名
    (re.compile(rf"(?i)({_COMPOUND_KEY})\s*{_SEP}\s*{_VALUE}"), r"\1=***"),
    # key は value（読点を許す。値は ASCII/全角数字限定 — 日本語の説明文を潰さないため）
    (re.compile(rf"(?i)\b({_KEYS})\b\s*は\s*[、,]?\s*{_ASCII_VALUE}"), r"\1=***"),
    # 日本語のラベル（同じ理由で値は ASCII/全角数字限定）
    (re.compile(rf"({_JP_KEYS})\s*(?:{_SEP}|は)\s*[、,]?\s*{_ASCII_VALUE}"), r"\1=***"),
)

# ---------------------------------------------------------------------------
# サニタイズ (マーカー偽造対策)
# ---------------------------------------------------------------------------
# 台帳の行は "[ctx] 🚫 ..." の形で会話へ注入される。本文に区切り記号や
# マーカーを混ぜると偽の制約・承認行を捏造できる (実証済み) ため、
# 追記時と注入時の両方で落とす。
_FRAME_MARKS = "🚫✅⚙️⚠️ℹ️"
_LEAD_JUNK_RE = re.compile(rf"^(?:\s|\[\s*ctx\s*\]|[{_FRAME_MARKS}\ufe0f])+", re.IGNORECASE)
_CTX_TOKEN_RE = re.compile(r"\[\s*ctx\s*\]", re.IGNORECASE)

_HEADER = """# 文脈台帳 — セッション {key}
# created: {created}
# ledger-id: {ledger_id}
# 圧縮で失われる揮発性情報の保全先。append-only。既存行は書き換えない。
# 形式: <MM-DD HH:MM> <TAG> <本文>   取消: <MM-DD HH:MM> DROP <TAG> <本文>
# TAG: LIMIT=制約 OK=承認 STATE=状態 VAL=値 DEAD=失敗 VERIFY=検証 COMPACT=圧縮印
# 禁止: PIN / パスワード / API キーの値そのもの (「口頭確認済」等の事実だけ書く)
"""


def log(msg: str) -> None:
    """フックの異常をローカルに残す。全例外を飲む設計の唯一の手がかり。

    発生源タグ (_SRC) と PID を必ず付ける。伏字化も通す (msg にペイロード断片を
    含めることがあるため)。512KB 超で1世代ローテーション。書けない場合は
    stderr へ複線を試みる (claude --debug で拾える最後の糸)。
    """
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [{_SRC}:{os.getpid()}] {redact(msg)}"
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / "ctx_hook.log"
        try:
            if path.stat().st_size > LOG_ROTATE_BYTES:
                os.replace(path, LOG_DIR / "ctx_hook.log.1")
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        try:
            sys.stderr.write(line + "\n")
        except Exception:
            pass


def read_payload(hook: str) -> dict | None:
    """stdin の JSON ペイロードを読む。**失敗は必ずログに残す** (無音経路の根絶)。

    2026-08-02 の実障害: PostCompact フックに空 stdin が渡り、json.load の例外を
    無ログで握りつぶして exit 0 したため、圧縮印が一度も打たれない障害が
    3日間潜伏した。raw を先に文字列で読み、何が来たかを証拠として残す。
    """
    try:
        # sys.stdin.read() は Windows で cp932 decode になり、日本語+引用符を含む
        # 正常な UTF-8 ペイロードを壊す (実測: 偽の degraded 警告)。必ず bytes で読む
        raw_bytes = sys.stdin.buffer.read()
        raw = raw_bytes.decode("utf-8", errors="replace")
    except Exception as exc:
        log(f"{hook}: stdin read failed: {type(exc).__name__}: {exc}")
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        # head はシークレット混入に備えて長い英数列を潰してから redact 込みで記録
        head = re.sub(r"[A-Za-z0-9+/=_\-]{16,}", "***", raw[:120])
        log(f"{hook}: stdin not JSON ({type(exc).__name__}); len={len(raw)} head={head!r}")
        return None
    if not isinstance(payload, dict):
        log(f"{hook}: payload is not a dict: {type(payload).__name__}")
        return None
    return payload


def sanitize(body: str) -> str:
    """注入マーカーの偽造材料を本文から落とす。

    - NFKC 正規化 + ゼロ幅/制御文字 (Cf) の除去。`[ｃｔｘ]`(全角) や
      `[<ZWSP>ctx]` での偽造がパターンを素通りした実測への対策
    - 行頭の "[ctx]" / 枠マーカー絵文字は剥がす (偽の ctx 行の起点になる)
    - "|" は "/" へ (項目分裂・視覚偽装の抑止)
    - 本文中の "[ctx]" は "〔ctx〕" へ
    - 長さ上限 MAX_BODY_CHARS で切り詰め

    置換に使う文字は **NFKC 安定な ASCII** に限る。"／" や "…" を使うと
    2回目の NFKC で "/" "..." に畳まれて冪等性が破れ、drop の一致が崩れる (実測)。
    """
    body = unicodedata.normalize("NFKC", body)
    body = "".join(c for c in body if unicodedata.category(c) != "Cf")
    body = _LEAD_JUNK_RE.sub("", body)
    body = body.replace("|", "/")
    body = _CTX_TOKEN_RE.sub("〔ctx〕", body)
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "..."
    return body


def redact(text: str) -> str:
    """PIN・パスワード・API キーらしき値を伏字化する。"""
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def normalize_body(body: str) -> str:
    """append/drop が共通で通す正規化パイプライン。

    drop の本文一致は「この関数を通した後の文字列」同士で行われるため、
    追記と取消で必ず同じ変換を適用しないと空振りする。
    """
    return redact(sanitize(" ".join(body.split())))


def session_key(payload: dict, allow_env: bool = True) -> str | None:
    """session_id > transcript_path の stem > 環境変数、の順にキーを決める。

    transcript の basename は常に <session_id>.jsonl (実データで確認済み) なので、
    stem が uuid 形式ならそのまま使う。ハッシュ化すると session_id 版と別キーに
    なり、同一セッションに第2の台帳が無言で生まれる (実験で確認)。
    **どれも取れなければ None**（fail closed）。共有の "default" に落とすと
    別セッションの制約・承認が混入し、誤った作業許可につながる。
    """
    sid = payload.get("session_id")
    if isinstance(sid, str) and sid.strip():
        safe = "".join(c for c in sid if c.isalnum() or c in "-_")[:64]
        if safe:
            return safe
    tp = payload.get("transcript_path")
    if isinstance(tp, str) and tp.strip():
        stem = Path(tp).stem
        if _UUID_RE.match(stem):
            log(f"session_key: from transcript stem {stem}")
            return stem
        log(f"session_key: transcript stem not uuid-like; hashing")
        return "tp-" + hashlib.sha1(tp.encode("utf-8")).hexdigest()[:16]
    if allow_env:
        for var in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
            env_sid = os.environ.get(var, "").strip()
            if env_sid:
                safe = "".join(c for c in env_sid if c.isalnum() or c in "-_")[:64]
                if safe:
                    log(f"session_key: from env {var}")
                    return safe
    return None


_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def ledger_path(key: str | None) -> Path | None:
    """キーを検証してから台帳パスを組み立てる。

    CLI 経由の key は argv 素通しなので、`zz/../../CLAUDE` のような
    パストラバーサルで CTX_DIR 外の .md へ追記できてしまった (実証済み)。
    session_key() と同じ許可集合で拒否する (fail closed)。
    """
    if key is None:
        return None
    if not _KEY_RE.match(key):
        log(f"ledger_path rejected invalid key: {key!r}")
        return None
    return CTX_DIR / f"CTX_{key}.md"


@contextmanager
def _ledger_lock(path: Path, timeout_s: float = 3.0) -> Iterator[bool]:
    """台帳ごとのプロセス間ロック。取得できなくても処理は続ける。

    フックは本流を止めてはいけないので、失敗したら False を渡してロックなしで
    続行する（行の混在より、記録されないことのほうが害が大きい）。
    ロック対象はデータ本体でなく `<台帳>.lock` の先頭 1 バイト。
    """
    lock_path = path.with_name(path.name + ".lock")
    handle = None
    acquired = False
    try:
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(lock_path, "a+b")
        except OSError as exc:
            log(f"lock open failed: {lock_path}: {exc}")
            yield False
            return
        deadline = time.monotonic() + timeout_s
        while msvcrt is not None:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    log(f"lock timeout: {lock_path}")
                    break
                time.sleep(0.05)
        yield acquired
    finally:
        if handle is not None:
            if acquired and msvcrt is not None:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            try:
                handle.close()
            except OSError:
                pass


def ensure_ledger(path: Path, key: str) -> bool:
    """台帳が無ければヘッダ付きで作る。作成したら True。"""
    if path.exists():
        return False
    created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        CTX_DIR.mkdir(parents=True, exist_ok=True)
        with open(path, "x", encoding="utf-8") as f:
            f.write(
                _HEADER.format(
                    key=key, created=created, ledger_id=uuid.uuid4().hex[:12]
                )
            )
            # ledger-id が飛ぶと世代管理が mtime フォールバックに落ちて
            # 誤再告知の種になるため、ヘッダも fsync する
            f.flush()
            os.fsync(f.fileno())
        return True
    except FileExistsError:
        return False  # 並行フックが先に作った
    except (OSError, ValueError) as exc:
        log(f"ensure_ledger failed: {path}: {exc}")
        return False


def generation(path: Path) -> str | None:
    """台帳の世代識別子。**読取失敗時は None** (呼び手は世代比較をスキップする)。

    台帳が削除・作り直された場合に、圧縮告知の既報数をリセットするために使う。
    旧実装は読取失敗で "unknown" を返し、一時的な失敗のたびに世代フラッピング→
    誤再告知が起きえた。失敗と「世代が変わった」は区別する。
    """
    created: str | None = None
    try:
        with open(path, encoding="utf-8") as f:
            for _ in range(10):
                line = f.readline()
                if not line:
                    break
                stripped = line.strip()
                m_id = LEDGER_ID_RE.match(stripped)
                if m_id:
                    return "id:" + m_id.group("id")
                m_created = CREATED_RE.match(stripped)
                if m_created and created is None:
                    created = m_created.group("stamp").strip()
    except (OSError, UnicodeDecodeError) as exc:
        log(f"generation read failed: {path}: {exc}")
        return None
    if created:
        return created
    try:
        return f"mtime:{path.stat().st_mtime:.0f}"
    except OSError as exc:
        log(f"generation stat failed: {path}: {exc}")
        return None


def _now_stamp() -> str:
    return datetime.now().strftime("%m-%d %H:%M")


def append(
    path: Path, tag: str, body: str, hhmm: str | None = None, normalize: bool = True
) -> bool:
    """台帳へ1行追記する。既存行には触らない。

    追記時点で正規化・サニタイズ・伏字化する。台帳は `/ctx` で直接 Read される
    運用なので、注入時だけの防御では平文がそのまま読まれてしまう。
    失敗 (不正タグ・空本文・IO エラー) は必ずログに残す。

    normalize=False は drop() 専用。DROP 行の本文は「TAG + 正規化済み本文」で、
    再正規化すると 300字切詰の位置ズレや再 redact で active() の完全一致が
    崩れ、「取消成功と報告したのに行が残る」事故になる (2026-08-03 レビューで実証)。
    """
    if tag not in TAGS:
        log(f"append rejected: unknown tag {tag!r}")
        return False
    if normalize:
        body = normalize_body(body)
    else:
        body = " ".join(body.split())  # 改行だけは行構造を壊すので必ず潰す
    if not body:
        log(f"append rejected: empty body after normalize (tag={tag})")
        return False
    stamp = hhmm or _now_stamp()
    try:
        CTX_DIR.mkdir(parents=True, exist_ok=True)
        with _ledger_lock(path):
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{stamp} {tag} {body}\n")
                f.flush()
                os.fsync(f.fileno())
        return True
    except OSError as exc:
        log(f"append failed: {path}: {exc}")
        return False


def drop(path: Path, tag: str, body: str) -> tuple[bool, str]:
    """有効な行を確認してから DROP を追記する。空振りを無音にしない。

    戻り値: (成功か, 人間向けメッセージ)。一致行が無い場合は追記せず、
    同 TAG の有効行一覧を返す (typo にその場で気づけるようにする)。
    """
    if tag not in TAGS or tag in ("DROP", "COMPACT"):
        return False, f"タグ {tag!r} は DROP できません"
    body = normalize_body(body)
    if not body:
        return False, "本文が空です"
    entries = parse(path)
    act = active(entries)
    rows = [b for _, b in act.get(tag, [])]
    if body not in rows:
        candidates = "\n".join(f"  {tag} {b}" for b in rows) or "  (なし)"
        return False, f"一致する有効行がありません。現在の {tag} 行:\n{candidates}"
    # normalize=False が要: body は正規化済みで、再正規化 (特に 300字切詰と
    # 再 redact) は active() の完全一致を崩す
    ok = append(path, "DROP", f"{tag} {body}", normalize=False)
    if not ok:
        return False, "DROP の追記に失敗しました (ctx_hook.log 参照)"
    # 書いた DROP が本当に効いたかを読み直して確認する (成功の虚偽報告を許さない)
    after = active(parse(path)).get(tag, [])
    if len(after) >= len(rows):
        log(f"drop verification failed: {tag} {body[:60]!r}")
        return False, "DROP を追記しましたが行が消えていません (ctx_hook.log 参照)"
    return True, f"取消: {tag} {body}"


@dataclass
class ParseResult:
    """parse_result() の戻り値。読取失敗と「有効行ゼロ」を区別する。

    旧実装は両方を空リストで表現していたため、台帳が読めない状態でも
    呼び手が保全機能の死を検知できなかった (実験で確認)。
    """

    entries: list[tuple[str, str, str]] = field(default_factory=list)
    read_ok: bool = True
    malformed: int = 0


def parse_result(path: Path) -> ParseResult:
    """台帳を (stamp, TAG, 本文) のリストに読む。

    **全行を読む。** 部分読みにすると古い未 DROP の制約が落ちて、
    圧縮対策としての保証そのものが壊れる。壊れた行は捨てるが数は数える
    (呼び手が「読めない行がある」ことを注入で警告できるようにする)。
    """
    result = ParseResult()
    try:
        with _ledger_lock(path):
            data = path.read_bytes()
        if len(data) > WARN_SIZE_BYTES:
            log(f"ledger is large ({len(data)} bytes): {path}")
        raw = data.decode("utf-8", errors="replace")
    except OSError as exc:
        log(f"parse failed: {path}: {exc}")
        result.read_ok = False
        return result
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = LINE_RE.match(line)
        if not m:
            result.malformed += 1
            continue
        tag = m.group("tag")
        if tag not in TAGS:
            result.malformed += 1
            continue
        result.entries.append((m.group("stamp"), tag, m.group("body").strip()))
    if result.malformed:
        log(f"parse: {result.malformed} malformed line(s) in {path.name}")
    return result


def parse(path: Path) -> list[tuple[str, str, str]]:
    """後方互換ラッパー。新規コードは parse_result() を使う。"""
    return parse_result(path).entries


def active(entries: list[tuple[str, str, str]]) -> dict[str, list[tuple[str, str]]]:
    """DROP されていない有効な行を TAG ごとにまとめる（時系列処理）。

    DROP 行の本文は "<TAG> <本文>" 形式で、**その時点までに有効な同一行のうち
    最後の1件だけ** を取り消す。
    """
    live: list[tuple[str, str, str]] = []
    for stamp, tag, body in entries:
        if tag == "COMPACT":
            continue
        if tag == "DROP":
            for i in range(len(live) - 1, -1, -1):
                if f"{live[i][1]} {live[i][2]}" == body:
                    live.pop(i)
                    break
            continue
        live.append((stamp, tag, body))
    result: dict[str, list[tuple[str, str]]] = {}
    for stamp, tag, body in live:
        result.setdefault(tag, []).append((stamp, body))
    return result


def compact_events(entries: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
    """圧縮印の一覧 [(stamp, 本文), ...]。本文は "auto src=tx" 等。"""
    return [(stamp, body) for stamp, tag, body in entries if tag == "COMPACT"]


# ---------------------------------------------------------------------------
# 状態マーカー (LOG_DIR/<key>.json)
# ---------------------------------------------------------------------------

def state_path(key: str) -> Path:
    return LOG_DIR / f"{key}.json"


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        log(f"load_state failed: {path}: {exc}")
        return {}


def save_state(path: Path, data: dict) -> bool:
    """PID 付き一時ファイル経由で atomic 置換する。

    Windows の os.replace は置換先が並行アクセス中だと拒否される
    (実測: 6並行で 21% 失敗) ため、短いリトライと tmp の後始末を持つ。
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        for attempt in range(3):
            try:
                os.replace(tmp, path)
                return True
            except OSError:
                if attempt == 2:
                    raise
                time.sleep(0.01)
        return False
    except OSError as exc:
        log(f"save_state failed: {path}: {exc}")
        return False
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def heartbeat(state: dict, event: str, result: str, detail: str = "") -> None:
    """state dict にイベント種別ごとの最終実行記録を書く (保存は呼び手が行う)。

    「PostCompact が最後にいつ・どう終わったか」を doctor で確認できるようにする。
    観測されないものは修理できない。
    """
    events = state.setdefault("events", {})
    rec: dict = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "result": result}
    if detail:
        rec["detail"] = detail
    events[event] = rec


def orphan_heartbeat(event: str, result: str, detail: str = "") -> None:
    """セッションを特定できないときのハートビート。_orphan.json に残す。"""
    path = LOG_DIR / "_orphan.json"
    state = load_state(path)
    heartbeat(state, event, result, detail)
    save_state(path, state)


# ---------------------------------------------------------------------------
# トランスクリプトからの圧縮検知 (主経路)
# ---------------------------------------------------------------------------
# 圧縮はトランスクリプト jsonl に
#   {"type":"system","subtype":"compact_boundary","compactMetadata":{"trigger":...}}
# として 100% 記録される (78/78 イベントで確認)。jsonl は append-only なので、
# boundary 行数は単調増加のカウンタとして使える。
# **固定 tail 読みは不可**: auto 圧縮はターン途中に起き、その後もツール結果が
# 追記され続ける (実測で boundary 後に最大 320KB)。オフセット差分走査を使う。

# プリフィルタは緩く (JSON の区切り空白の揺れに耐える)、確定判定は
# json.loads 後の type/subtype 検査で行う。会話本文中の引用は type=user に
# なるため誤検知しない。
_BOUNDARY_NEEDLE = b"compact_boundary"


@dataclass
class TxScan:
    """scan_transcript() の結果。"""

    boundaries: list[dict] = field(default_factory=list)  # 新しく見つけた boundary
    new_offset: int = 0
    reset: bool = False  # ファイルが縮んでいて 0 から再走査した
    ok: bool = True


def scan_transcript(tpath: Path, offset: int) -> TxScan:
    """offset 以降の完全な行だけを走査し、compact_boundary を抽出する。

    末尾の書きかけ行 (改行なし) はカウントせず、オフセットも進めない。
    harness の追記と競合しても取りこぼさないための要。
    """
    result = TxScan(new_offset=offset)
    try:
        size = tpath.stat().st_size
    except OSError as exc:
        log(f"scan_transcript stat failed: {tpath}: {exc}")
        result.ok = False
        return result
    if offset > size:
        log(f"scan_transcript: offset {offset} > size {size}; rescanning from 0")
        offset = 0
        result.reset = True
    if offset == size:
        result.new_offset = offset
        return result
    try:
        with open(tpath, "rb") as f:
            f.seek(offset)
            chunk = f.read(size - offset)
    except OSError as exc:
        log(f"scan_transcript read failed: {tpath}: {exc}")
        result.ok = False
        return result
    # 完全行のみ処理。最後の改行以降は次回に回す
    last_nl = chunk.rfind(b"\n")
    if last_nl < 0:
        result.new_offset = offset
        return result
    complete = chunk[: last_nl + 1]
    result.new_offset = offset + last_nl + 1
    for line in complete.split(b"\n"):
        if _BOUNDARY_NEEDLE not in line:
            continue
        # 会話本文中に同文字列が引用された場合は \" にエスケープされるため
        # 生の部分一致では誤検知しない (実測確認済み) が、JSON 検証も通す
        try:
            rec = json.loads(line.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, ValueError):
            continue
        if rec.get("type") != "system" or rec.get("subtype") != "compact_boundary":
            continue
        meta = rec.get("compactMetadata") or {}
        result.boundaries.append(
            {
                "trigger": meta.get("trigger") or "unknown",
                "uuid": (rec.get("uuid") or "")[:8],
                "timestamp": rec.get("timestamp") or "",
                "sessionId": rec.get("sessionId") or "",
            }
        )
    return result


def derive_transcript(payload: dict, key: str) -> Path | None:
    """transcript_path をペイロード → cwd 変換 → 既知プロジェクト走査の順で求める。

    パス規約 (実在確認済み): C:/Users/<user>/.claude/projects/<cwd の [:\\/]→'-' 変換>/<session_id>.jsonl
    """
    tp = payload.get("transcript_path")
    if isinstance(tp, str) and tp.strip():
        p = Path(tp)
        if p.exists():
            return p
        log(f"derive_transcript: payload path missing: {tp}")
    projects = Path.home() / ".claude" / "projects"
    cwd = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    if isinstance(cwd, str) and cwd.strip():
        name = re.sub(r"[:\\/]", "-", cwd.rstrip("\\/"))
        cand = projects / name / f"{key}.jsonl"
        if cand.exists():
            return cand
    # 最後の手段: プロジェクト横断で session_id 完全一致のファイルを探す
    try:
        if projects.is_dir():
            for proj in projects.iterdir():
                cand = proj / f"{key}.jsonl"
                if cand.exists():
                    return cand
    except OSError as exc:
        log(f"derive_transcript scan failed: {exc}")
    return None


# ---------------------------------------------------------------------------
# 堆積の掃除 (prune)
# ---------------------------------------------------------------------------

PRUNE_KEEP_DAYS = 30
PRUNE_KEEP_DAYS_UNKNOWN = 60  # transcript を特定できないキー (tp-* 等)


def _find_transcript_for(key: str, transcript_dir: Path | None) -> Path | None:
    """prune 用: key のトランスクリプトを現行 dir → projects 横断で探す。

    現行 dir 決め打ちだと、別プロジェクトで生きているセッションの台帳を
    「transcript 無し」と誤判定して削除してしまう (2026-08-03 レビューで実証)。
    """
    if transcript_dir is not None:
        cand = transcript_dir / f"{key}.jsonl"
        if cand.exists():
            return cand
    try:
        projects = Path.home() / ".claude" / "projects"
        if projects.is_dir():
            for proj in projects.iterdir():
                cand = proj / f"{key}.jsonl"
                if cand.exists():
                    return cand
    except OSError:
        pass
    return None


def maybe_prune(current_key: str, transcript_dir: Path | None) -> None:
    """1日1回だけ、終了済みセッションの台帳・ロック・マーカーを掃除する。

    生存判定は **トランスクリプトの mtime** を使う。台帳の mtime は使わない
    (台帳無更新のままセッションが活動し続ける実例があるため)。
    実行中セッション (current_key) は無条件に保護する。
    transcript_dir が不明なターンでは削除判定の精度が落ちるため **prune 自体を
    スキップ** する (スタンプに "skipped" を残し、特定できたターンで改めて実行)。
    """
    stamp_file = CTX_DIR / ".last_prune"
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        stamp = stamp_file.read_text(encoding="utf-8").strip() if stamp_file.exists() else ""
    except OSError as exc:
        log(f"prune stamp unreadable: {stamp_file}: {exc}")
        stamp = ""
    if stamp == today:
        return
    if transcript_dir is None:
        # 生存確認の主シグナルが無い日は削除しない。ただし完全スキップの印は
        # 残し、同日中に transcript を特定できたターンが来たら実行する
        if stamp != f"{today} skipped":
            try:
                stamp_file.parent.mkdir(parents=True, exist_ok=True)
                stamp_file.write_text(f"{today} skipped", encoding="utf-8")
            except OSError as exc:
                log(f"prune stamp write failed: {exc}")
            log("prune skipped: transcript dir unknown")
        return
    removed = 0
    try:
        cutoff = datetime.now() - timedelta(days=PRUNE_KEEP_DAYS)
        cutoff_unknown = datetime.now() - timedelta(days=PRUNE_KEEP_DAYS_UNKNOWN)
        for ledger in CTX_DIR.glob("CTX_*.md"):
            key = ledger.stem[len("CTX_"):]
            if key == current_key:
                continue
            tx = _find_transcript_for(key, transcript_dir)
            try:
                if tx is not None:
                    alive = datetime.fromtimestamp(tx.stat().st_mtime) >= cutoff
                else:
                    # transcript がどこにも無い: 台帳 mtime で保守的に判断
                    limit = cutoff_unknown if key.startswith("tp-") else cutoff
                    alive = datetime.fromtimestamp(ledger.stat().st_mtime) >= limit
            except OSError as exc:
                log(f"prune stat failed for {ledger.name}: {exc}")
                continue
            if alive:
                continue
            for victim in (
                ledger,
                ledger.with_name(ledger.name + ".lock"),
                state_path(key),
            ):
                try:
                    victim.unlink(missing_ok=True)
                except OSError:
                    pass  # 使用中の .lock は WinError 32 で自然に守られる
            removed += 1
        # マーカーの残骸 tmp も掃除
        for tmp in LOG_DIR.glob("*.tmp"):
            try:
                if datetime.fromtimestamp(tmp.stat().st_mtime) < datetime.now() - timedelta(days=1):
                    tmp.unlink(missing_ok=True)
            except OSError:
                pass
        stamp_file.parent.mkdir(parents=True, exist_ok=True)
        stamp_file.write_text(today, encoding="utf-8")
        if removed:
            log(f"prune: removed {removed} expired ledger set(s)")
    except OSError as exc:
        log(f"prune failed: {exc}")
