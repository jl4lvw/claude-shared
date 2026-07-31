"""文脈台帳 (.ctx) の共通ロジック — パス決定・排他追記・パース・伏字化。

背景 (2026-07-30):
  コンテキスト圧縮は「作業の流れ」を要約として残す一方、**セッション限定の制約 /
  得た承認 / 起動中プロセス / 試して失敗した方法 / 検証状態 / 生の値** を落とす。
  CLAUDE.md と MEMORY.md は毎回再注入されるので恒久ルールは生き残るが、
  セッション内だけで有効な情報を保全する層がどこにも無かった。

方式:
  台帳を append-only の行指向ファイルとしてディスクに置き、圧縮の挙動に一切
  依存させない。UserPromptSubmit フック (ctx_inject.py) が毎ターン読み直して
  制約・承認・状態を会話へ差し戻すため、圧縮が何回起きても必ず戻ってくる。
  PostCompact フック (ctx_compact_mark.py) が圧縮の印を打つので、圧縮後の
  Claude は「自分が記憶を失った」ことに気づける。

行フォーマット:
  "<HH:MM> <TAG> <本文>"
  取消は "<HH:MM> DROP <TAG> <本文>" で **その時点までの有効な同一行1件** を
  無効化する (時系列処理)。グローバル完全一致にすると、staging の
  起動→停止→再起動のように同じ本文が再登場するケースで再追加分まで
  永久に消えてしまう (Codex/DS/Qwen 4者が重大指摘、2026-07-30)。

セッション同一性:
  セッションを一意に指す識別子 (session_id、無ければ transcript_path のハッシュ)
  が取れない場合は **台帳を作らない** (fail closed)。transcript_path は
  セッションごとに固有なので識別子として使える。
  skill_freshness.py は共有の "default" に落ちても警告が混ざるだけで無害だが、
  台帳は制約・承認が別セッションへ混入すると誤った作業許可につながるため、
  同じ fallback を流用してはいけない。
  C:/tmp-ai の汎用ファイル名を複数セッションが共有して Codex が誤った内容を
  レビューした事故 (2026-07-30) と同じ形になる。
"""
from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

try:
    import msvcrt  # Windows のファイルロック
except ImportError:  # pragma: no cover — 非 Windows
    msvcrt = None  # type: ignore[assignment]

CTX_DIR = Path(os.environ.get("CLAUDE_CTX_DIR", r"C:/ClaudeCode/.ctx"))
LOG_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ClaudeCodeCtx"

# 台帳が異常に育った場合にログへ注意を残す閾値。
# **読み飛ばしはしない**。当初は末尾だけを読む上限を置いていたが、それでは
# 古いが未 DROP の LIMIT/OK/STATE が押し出されて注入されなくなり、
# 「圧縮後も制約を必ず戻す」という本機能の中心保証が壊れる（Codex 指摘）。
# 台帳は1イベント1行なので、実運用の規模では全読みでも数十 ms に収まる。
WARN_SIZE_BYTES = 1024 * 1024

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

# 毎ターン注入する枠 (事故に直結する3枠のみ。残りは圧縮後に台帳を読めば足りる)
INJECT_TAGS: tuple[str, ...] = ("LIMIT", "OK", "STATE")

LINE_RE = re.compile(r"^(?P<hhmm>\d{1,2}:\d{2})\s+(?P<tag>[A-Z]+)\s+(?P<body>.+)$")
CREATED_RE = re.compile(r"^#\s*created:\s*(?P<stamp>.+)$")
LEDGER_ID_RE = re.compile(r"^#\s*ledger-id:\s*(?P<id>\S+)$")

# PIN / パスワード / API キーらしき値の伏字化。
# 台帳に値そのものを書かせないのは SKILL.md 側の規律だが、万一書かれた場合に
# 備えて **追記時と注入時の両方** で落とす (多重防御)。
# 誤伏字を抑えるため、区切り記号か引用符を伴い値が 3 文字以上のものだけ対象。
# 値の形（素の1トークン / "引用符付き" / '引用符付き'）。
# 引用符を見ないと `password: "my secret pw"` で最初の1語しか落ちず漏れる。
_VALUE = r"(?:\"[^\"]{3,}\"|'[^']{3,}'|[^\s\"']{3,})"

# 「〜は」を区切りとみなす場合の値。ASCII のシークレットらしい並びに限定する。
# 制限しないと `PIN はユーザーに口頭確認済` のような **推奨される書き方** まで
# 潰れる（実測で確認）。シークレットの実値は事実上 ASCII なのでこれで足りる。
_ASCII_VALUE = r"(?:\"[^\"]{3,}\"|'[^']{3,}'|[A-Za-z0-9_\-./+=:]{4,})"

_KEYS = (
    r"pin|password|passwd|secret|api[-_ ]?key|access[-_ ]?token"
    r"|refresh[-_ ]?token|token|client[-_ ]?secret"
)
_JP_KEYS = r"パスワード|暗証番号|認証キー|APIキー|アクセストークン|トークン|秘密鍵"

# 順序が重要。`Authorization: Bearer xxx` は汎用 key=value より先に処理しないと、
# 値として "Bearer" だけを消費して token 本体が残る（実測でリークを確認）。
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Authorization ヘッダは行末まで丸ごと落とす
    (re.compile(r"(?i)\bauthorization\b\s*(?:[:=]|は)\s*.+$"), "Authorization=***"),
    # 単独の Bearer xxx
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{6,}"), "Bearer ***"),
    # 接続文字列の user:pass@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^\s:/@]+:[^\s@]+@"), r"\1***:***@"),
    # key: value / key="value"
    (re.compile(rf"(?i)\b({_KEYS})\b\s*[:=]\s*{_VALUE}"), r"\1=***"),
    # key は value（値は ASCII 限定。日本語の説明文を潰さないため）
    (re.compile(rf"(?i)\b({_KEYS})\b\s*は\s*{_ASCII_VALUE}"), r"\1=***"),
    # 日本語のラベル（同じ理由で値は ASCII 限定）
    (re.compile(rf"({_JP_KEYS})\s*(?:[:=]|は)\s*{_ASCII_VALUE}"), r"\1=***"),
)

_HEADER = """# 文脈台帳 — セッション {key}
# created: {created}
# ledger-id: {ledger_id}
# 圧縮で失われる揮発性情報の保全先。append-only。既存行は書き換えない。
# 形式: <HH:MM> <TAG> <本文>   取消: <HH:MM> DROP <TAG> <本文>
# TAG: LIMIT=制約 OK=承認 STATE=状態 VAL=値 DEAD=失敗 VERIFY=検証 COMPACT=圧縮印
# 禁止: PIN / パスワード / API キーの値そのもの (「口頭確認済」等の事実だけ書く)
"""


def log(msg: str) -> None:
    """フックの異常をローカルに残す。全例外を飲む設計の唯一の手がかり。"""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_DIR / "ctx_hook.log", "a", encoding="utf-8") as f:
            f.write(f"{stamp} {msg}\n")
    except OSError:
        pass


def redact(text: str) -> str:
    """PIN・パスワード・API キーらしき値を伏字化する。"""
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def session_key(payload: dict) -> str | None:
    """session_id > transcript_path ハッシュ、の順にキーを決める。

    transcript_path はセッションごとに固有なので、session_id が無い場合の
    セッション識別子として使える。**どちらも取れなければ None**（fail closed）。
    共有の "default" に落とすと別セッションの制約・承認が混入し、
    誤った作業許可につながる。
    """
    sid = payload.get("session_id")
    if isinstance(sid, str) and sid.strip():
        safe = "".join(c for c in sid if c.isalnum() or c in "-_")[:64]
        if safe:
            return safe
    tp = payload.get("transcript_path")
    if isinstance(tp, str) and tp.strip():
        return "tp-" + hashlib.sha1(tp.encode("utf-8")).hexdigest()[:16]
    return None


def ledger_path(key: str | None) -> Path | None:
    return None if key is None else CTX_DIR / f"CTX_{key}.md"


@contextmanager
def _ledger_lock(path: Path, timeout_s: float = 3.0) -> Iterator[bool]:
    """台帳ごとのプロセス間ロック。取得できなくても処理は続ける。

    フックは本流を止めてはいけないので、タイムアウトしたら False を渡して
    ロックなしで続行する（行の混在より、記録されないことのほうが害が大きい）。
    ロック対象はデータ本体でなく `<台帳>.lock` の先頭 1 バイト。
    """
    lock_path = path.with_name(path.name + ".lock")
    handle = None
    acquired = False
    try:
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(lock_path, "a+b")
        except OSError:
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
        return True
    except FileExistsError:
        return False  # 並行フックが先に作った
    except (OSError, ValueError) as exc:
        log(f"ensure_ledger failed: {path}: {exc}")
        return False


def generation(path: Path) -> str:
    """台帳の世代識別子。

    台帳が削除・作り直された場合に、圧縮告知の既報数をリセットするために使う。
    `ledger-id`（作成時のランダム値）を最優先にする。`created` は秒単位なので、
    同一秒内に作り直されると世代を区別できず、圧縮告知が静かに永久抑止される
    （実測で再現）。created / mtime は古い台帳との後方互換のための fallback。
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
    except (OSError, UnicodeDecodeError):
        pass
    if created:
        return created
    try:
        return f"mtime:{path.stat().st_mtime:.0f}"
    except OSError:
        return "unknown"


def append(path: Path, tag: str, body: str, hhmm: str | None = None) -> bool:
    """台帳へ1行追記する。既存行には触らない。

    追記時点で伏字化する。台帳は `/ctx` で直接 Read される運用なので、
    注入時だけの伏字化では平文がそのまま読まれてしまう。
    """
    if tag not in TAGS:
        return False
    body = redact(" ".join(body.split()))
    if not body:
        return False
    stamp = hhmm or datetime.now().strftime("%H:%M")
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


def parse(path: Path) -> list[tuple[str, str, str]]:
    """台帳を (HH:MM, TAG, 本文) のリストに読む。壊れた行は黙って捨てる。

    **全行を読む。** 部分読みにすると古い未 DROP の制約が落ちて、
    圧縮対策としての保証そのものが壊れる。異常に大きい場合はログに残すが、
    行を捨てることはしない。
    """
    out: list[tuple[str, str, str]] = []
    try:
        with _ledger_lock(path):
            data = path.read_bytes()
        if len(data) > WARN_SIZE_BYTES:
            log(f"ledger is large ({len(data)} bytes): {path}")
        raw = data.decode("utf-8", errors="replace")
    except OSError as exc:
        log(f"parse failed: {path}: {exc}")
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = LINE_RE.match(line)
        if not m:
            continue
        tag = m.group("tag")
        if tag not in TAGS:
            continue
        out.append((m.group("hhmm"), tag, m.group("body").strip()))
    return out


def active(entries: list[tuple[str, str, str]]) -> dict[str, list[tuple[str, str]]]:
    """DROP されていない有効な行を TAG ごとにまとめる（時系列処理）。

    DROP 行の本文は "<TAG> <本文>" 形式で、**その時点までに有効な同一行のうち
    最後の1件だけ** を取り消す。グローバル完全一致にすると
    「STATE staging 起動中 → DROP → また起動して同じ本文で STATE」が
    永久に消えたままになる。
    """
    live: list[tuple[str, str, str]] = []
    for hhmm, tag, body in entries:
        if tag == "COMPACT":
            continue
        if tag == "DROP":
            for i in range(len(live) - 1, -1, -1):
                if f"{live[i][1]} {live[i][2]}" == body:
                    live.pop(i)
                    break
            continue
        live.append((hhmm, tag, body))
    result: dict[str, list[tuple[str, str]]] = {}
    for hhmm, tag, body in live:
        result.setdefault(tag, []).append((hhmm, body))
    return result


def compact_events(entries: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
    """圧縮印の一覧 [(HH:MM, trigger), ...]。"""
    return [(hhmm, body) for hhmm, tag, body in entries if tag == "COMPACT"]
