from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Callable, Mapping, NamedTuple, Sequence
from urllib.parse import urlparse

TOOLS = Path(__file__).resolve().parent
DEFAULT_WORK_ROOT = Path("C:/tmp-ai/cgd_lv3a")
MAX_INPUT_BYTES = 200 * 1024
FACTS_HEADING = "## 確認済みの事実"
# 使えるレビュアー (実行成功かつ JSON ゲート合格) がこの数以上なら、欠けた者を残して暫定版で統合する
MIN_USABLE_REVIEWERS = 2
PARTIAL_EXIT = 20
REDACT_HINT = "伏字して続行するには --redact を付け、ユーザーの承認を取ってください（plan --redact で伏字プレビューを確認できます）"
REDACTION_NOTE = (
    "※ 送信前に、秘匿情報の候補を [伏字:<種別>] へ置換しています（当たった位置から行末までを置換するため、"
    "行が途中で切れて見えることがあります）。伏字そのものや、それによる欠けは指摘しないでください。"
)
PROPOSAL_NOTE = "※ 採否・対応案は統合 AI の提案です。最終判断はユーザーが行います。"
JSON_INSTRUCTION = (
    "【回答形式・必須】回答本文の最後に、次の形式の JSON ブロックを 1 つだけ付けること"
    "（```json で囲む）。本文で述べた指摘を 1 件 1 要素で漏れなく列挙する。"
    '{"findings":[{"id":"<P>1","severity":"<S>",'
    '"headline":"指摘の見出し(40字以内)"}]} id は <P>1, <P>2, … の連番。'
    "severity は <SLIST> のいずれか。"
    "指摘が 1 件も無いときは findings を空配列 [] にする（本文に、確認した観点と、指摘なしと判断した根拠を書くこと）。"
)
TECH_PROMPT = (
    "これは設計レビューです。バグ・設計上の懸念・セキュリティ・副作用・既存仕様との"
    "整合性を厳密にレビューしてください。対象ファイルは開けないので、以下の内容だけで"
    "判断すること。断言できない点は根拠がないと明記すること。日本語で回答。"
)
# lv7 / lv8 の技術レビュアーだけに、レビュー入力の先頭側へ足す重点観点 (cgd の Lv7 の SKILL と同じ狙い)。
# lv3 の入力にはこのブロックを入れない (1 バイトも変えない)
INTEGRATION_FOCUS = (
    "【重点観点】通常のバグ・設計上の懸念に加えて、特に次の integration バグを重点的に評価すること。\n"
    "- 関数間の暗黙の前提違反（呼ぶ側と呼ばれる側で、引数・戻り値・状態の前提が食い違っていないか）\n"
    "- スコープを跨いだ状態管理の破綻（モジュール・クロージャ・キャッシュ等の更新漏れや取り違え）\n"
    "- 呼出経路ごとの副作用の差異（同じ処理でも入口によって書込・通知・記録が変わらないか）\n"
    "- catch / except での例外の握り潰し（throw・raise が黙って失われ、別経路へ落ちていないか）"
)
CRIT_PROMPT = (
    "あなたは辛口の評価者です。技術的な正しさ（バグの有無）ではなく『使う人が困らないか』"
    "『本来この仕様はどうあるべきか』の観点で、遠慮なく否定的に評価してください。次の2つの"
    "立場を併せ持ってください: (1) ITに疎い現場担当者 — 実際に使うときの使いにくさ・"
    "わかりにくさ・手数の多さ・エラー時の困りごとを利用者の生の言葉で指摘する。"
    "(2) 熟練ITアーキテクト — 『本来この仕様はどうあるべきか』を理想形から逆算し、現状の"
    "妥協・場当たり対応・本質を外した設計・優先度の誤りを批判する。出力は次の構造で: "
    "1.現場の不満（各項目に困り度: 高/中/低を付ける） 2.あるべき論とのギャップ "
    "3.そもそも論（この機能は本当に要るか） 4.辛口総評（1〜2行で断言）。擁護・肯定・"
    "『概ね良い』は禁止。技術的なバグ指摘には深入りしない。対象ファイルは開けないので、"
    "以下の内容だけで判断すること。日本語で回答。"
)
RETRY_NOTE = (
    "前回の回答は JSON ブロックが無い/不正だった。指示どおり、回答本文の末尾に JSON "
    "ブロックを付けて、回答全体を出し直せ"
)
PRIVATE_KEY = "private-key"
# 秘密鍵ブロックの終端。BEGIN 側の正規表現は SECRET_PATTERNS の PRIVATE_KEY にある
PEM_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----", re.IGNORECASE)


class SecretPattern(NamedTuple):
    name: str  # 伏字マーカー [伏字:<name>] と redaction.json に使う ASCII 名
    label: str  # 検出メッセージ用の日本語ラベル
    regex: re.Pattern[str]


SECRET_PATTERNS = (
    SecretPattern("sk-key", "OpenAI API キー", re.compile(r"sk-[A-Za-z0-9]{10,}", re.IGNORECASE)),
    SecretPattern("aws-access-key", "AWS アクセスキー", re.compile(r"AKIA[0-9A-Z]{12,}", re.IGNORECASE)),
    SecretPattern("api-key", "API キー", re.compile(r"api[_-]?key\s*[:=]", re.IGNORECASE)),
    SecretPattern("password", "password", re.compile(r"password\s*[:=]", re.IGNORECASE)),
    SecretPattern("passwd", "passwd", re.compile(r"passwd\s*[:=]", re.IGNORECASE)),
    SecretPattern("bearer", "Bearer token", re.compile(r"bearer\s+[A-Za-z0-9._-]{10,}", re.IGNORECASE)),
    SecretPattern(
        "email",
        "メールアドレス",
        re.compile(
            r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.(com|jp|net)", re.IGNORECASE
        ),
    ),
    SecretPattern(PRIVATE_KEY, "秘密鍵", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE)),
    SecretPattern("jwt", "JWT", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}", re.IGNORECASE)),
    SecretPattern(
        "conn-string", "資格情報付き接続文字列",
        re.compile(r"[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s@]+@", re.IGNORECASE),
    ),
    SecretPattern("aws-secret", "AWS secret access key", re.compile(r"aws_secret_access_key\s*[:=]", re.IGNORECASE)),
    SecretPattern("secret", "secret", re.compile(r"secret\s*[:=]\s*\S{8,}", re.IGNORECASE)),
    SecretPattern("token", "token", re.compile(r"token\s*[:=]\s*\S{16,}", re.IGNORECASE)),
)
PRIVATE_KEY_PATTERN = next(item for item in SECRET_PATTERNS if item.name == PRIVATE_KEY)


@dataclass(frozen=True)
class ExecResult:
    returncode: int
    stdout: str
    stderr: str
    elapsed: float = 0.0


@dataclass(frozen=True)
class ReviewerSpec:
    name: str
    vendor: str
    kind: str
    prefix: str
    severities: tuple[str, ...]
    role: str
    # Codex の推論強度をレビュアーごとに上書きする (None なら --effort)。Codex 以外では使わない
    effort: str | None = None


@dataclass(frozen=True)
class ParsedReview:
    body: str
    findings: list[dict[str, str]]


@dataclass(frozen=True)
class Redaction:
    """伏字にした 1 か所。中身は持たない (redaction.json にそのまま書く)。"""

    file: str
    line: int
    pattern: str


@dataclass(frozen=True)
class LoadedInputs:
    brief: str
    files: list[tuple[Path, str]]
    total: int
    redactions: list[Redaction]


REVIEWERS = (
    ReviewerSpec("codex_tech", "Codex", "technical", "CT", ("🔴", "🟠", "🟡"), "reviewer"),
    ReviewerSpec("codex_crit", "Codex", "critic", "CC", ("高", "中", "低"), "critic"),
    ReviewerSpec("ds_tech", "DeepSeek", "technical", "DT", ("🔴", "🟠", "🟡"), "reviewer"),
    ReviewerSpec("ds_crit", "DeepSeek", "critic", "DC", ("高", "中", "低"), "critic"),
)

# 組 (roster) = レビュアーの構成。lv3 は上の REVIEWERS そのまま。prefix は指摘 ID (<prefix><連番>) の頭になるので、
# 組の中で重複させない (check_roster が守る)。ID の検査は spec.prefix から導出しており、prefix を決め打ちした箇所は無い
DEFAULT_EFFORT = "medium"
TECH_SEVERITIES = ("🔴", "🟠", "🟡")
CRIT_SEVERITIES = ("高", "中", "低")
ROSTER_LV7 = (
    ReviewerSpec("codex_tech", "Codex", "technical", "CT", TECH_SEVERITIES, "reviewer", "medium"),
    ReviewerSpec("codex_tech_high", "Codex", "technical", "CH", TECH_SEVERITIES, "reviewer", "high"),
    ReviewerSpec("ds_tech", "DeepSeek", "technical", "DT", TECH_SEVERITIES, "reviewer"),
    ReviewerSpec("qwen_tech", "Qwen", "technical", "QT", TECH_SEVERITIES, "reviewer"),
)
ROSTER_LV8 = (
    *ROSTER_LV7,
    ReviewerSpec("codex_crit", "Codex", "critic", "CC", CRIT_SEVERITIES, "critic", "high"),
    ReviewerSpec("ds_crit", "DeepSeek", "critic", "DC", CRIT_SEVERITIES, "critic"),
)
DEFAULT_ROSTER = "lv3"
ROSTERS: dict[str, tuple[ReviewerSpec, ...]] = {DEFAULT_ROSTER: REVIEWERS, "lv7": ROSTER_LV7, "lv8": ROSTER_LV8}
# lv7 / lv8 は Codex の強度を組で固定する (--effort と併用不可)。技術レビュアーには重点観点ブロックを足す
FIXED_EFFORT_ROSTERS = ("lv7", "lv8")
FOCUS_ROSTERS = ("lv7", "lv8")

ReviewerRunner = Callable[[ReviewerSpec, str, Path, int, str, Path, str], ExecResult]
IntegratorRunner = Callable[[str, Path, int, str], ExecResult]


class FrontError(ValueError):
    pass


def check_roster(specs: Sequence[ReviewerSpec]) -> list[str]:
    """組の不備 (name / prefix の重複) の一覧。prefix が重なると指摘 ID が衝突し、統合が誤る。"""
    errors: list[str] = []
    for label, values in (("name", [item.name for item in specs]), ("prefix", [item.prefix for item in specs])):
        duplicated = sorted({value for value in values if values.count(value) > 1})
        if duplicated:
            errors.append(f"{label} が重複: {', '.join(duplicated)}")
    return errors


def roster_specs(name: str) -> tuple[ReviewerSpec, ...]:
    """組の名前からレビュアーの並びを返す。未知の名前・不備のある組は止める (黙って別の組にしない)。"""
    if name not in ROSTERS:
        raise FrontError(f"未知の組: {name}（使えるのは {' / '.join(ROSTERS)}）")
    errors = check_roster(ROSTERS[name])
    if errors:
        raise FrontError(f"組 {name} が不正: " + "; ".join(errors))
    return ROSTERS[name]


def roster_vendors(specs: Sequence[ReviewerSpec]) -> list[str]:
    """組にいるベンダー (最初に現れた順・重複なし)。レポートの表の列・費用の行になる。"""
    return list(dict.fromkeys(item.vendor for item in specs))


def read_utf8(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise FrontError(f"読み込めません: {path}: {exc}") from exc


def secret_hits(label: str, text: str) -> list[str]:
    hits: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        for pattern in SECRET_PATTERNS:
            if pattern.regex.search(line):
                hits.append(f"{label}: {pattern.label} / 行 {number}")
    return hits


def redaction_marker(name: str) -> str:
    return f"[伏字:{name}]"


def _earliest_hit(line: str) -> tuple[re.Match[str], SecretPattern] | None:
    """行内でいちばん左に当たったパターン。同じ位置なら SECRET_PATTERNS の並びが先のもの。"""
    best: tuple[re.Match[str], SecretPattern] | None = None
    for pattern in SECRET_PATTERNS:
        match = pattern.regex.search(line)
        if match and (best is None or match.start() < best[0].start()):
            best = (match, pattern)
    return best


def redact_text(text: str) -> tuple[str, list[tuple[int, str]]]:
    """秘匿候補を伏字にした文章と、[(行番号, パターン名)] を返す。当たった中身は返さない。

    当たった位置から行末までを `[伏字:<パターン名>]` に置換する (1 行に複数当たれば、いちばん左の当たりから
    行末までを 1 つにまとめる)。秘密鍵は BEGIN から END まで (END が無ければ文末まで) を 1 つに置換する。
    行番号は secret_hits と同じ splitlines() の数え方 (元の文章の行)。
    """
    pending = deque(enumerate(text.splitlines(keepends=True), 1))
    out: list[str] = []
    found: list[tuple[int, str]] = []
    while pending:
        number, raw = pending.popleft()
        body = raw.splitlines()[0]
        eol = raw[len(body):]
        hit = _earliest_hit(body)
        if hit is None:
            out.append(raw)
            continue
        match, pattern = hit
        found.append((number, pattern.name))
        out.append(body[: match.start()] + redaction_marker(pattern.name))
        swallowed = body[match.start():]
        # 行末までに秘密鍵の BEGIN が含まれるなら (別のパターンが先に当たった行でも)、END まで読み飛ばす。
        # 鍵の本体行は何のパターンにも当たらないので、ここで落とさないと本体が残る
        begin = PRIVATE_KEY_PATTERN.regex.search(swallowed)
        if begin is None:
            out.append(eol)
            continue
        if pattern.name != PRIVATE_KEY:
            found.append((number, PRIVATE_KEY))
        tail = swallowed[begin.end():] + eol
        while True:
            end = PEM_END.search(tail)
            if end:
                rest = tail[end.end():]
                if rest:
                    pending.appendleft((number, rest))  # END の後ろの続きも再走査する
                break
            if not pending:
                break  # END が無い: 文末まで置換
            number, tail = pending.popleft()
    return "".join(out), found


def scan_inputs(brief_path: Path, brief: str, loaded: Sequence[tuple[Path, str]]) -> list[str]:
    hits = secret_hits(str(brief_path), brief)
    for path, text in loaded:
        hits.extend(secret_hits(str(path), text))
    return hits


def redact_inputs(
    brief_path: Path, brief: str, loaded: Sequence[tuple[Path, str]]
) -> tuple[str, list[tuple[Path, str]], list[Redaction]]:
    redactions: list[Redaction] = []

    def apply(label: str, text: str) -> str:
        redacted, found = redact_text(text)
        redactions.extend(Redaction(label, line, name) for line, name in found)
        return redacted

    new_brief = apply(str(brief_path), brief)
    return new_brief, [(path, apply(str(path), text)) for path, text in loaded], redactions


def load_inputs(brief_path: Path, files: Sequence[Path], *, redact: bool = False) -> LoadedInputs:
    """依頼文と対象を読み、検査する。秘匿候補があれば、既定は停止 (fail-closed)。

    redact=True のときだけ、当たった箇所を伏字にして続行する。伏字後にもう一度走査し、当たりが残れば停止する。
    """
    paths = [brief_path, *files]
    sizes: list[int] = []
    for index, path in enumerate(paths):
        try:
            stat = path.stat()
        except OSError as exc:
            label = "依頼文" if index == 0 else "詰め込み対象"
            raise FrontError(f"{label}が存在しません: {path}") from exc
        if not path.is_file():
            raise FrontError(f"ファイルではありません: {path}")
        sizes.append(stat.st_size)
    total = sum(sizes)
    if total > MAX_INPUT_BYTES:
        raise FrontError(f"入力合計が 200KB を超えています: {total} bytes")
    brief = read_utf8(brief_path)
    if FACTS_HEADING not in brief:
        raise FrontError("依頼文に『## 確認済みの事実』の欄が要る")
    loaded: list[tuple[Path, str]] = []
    for path in files:
        text = read_utf8(path)
        loaded.append((path, text))
    hits = scan_inputs(brief_path, brief, loaded)
    redactions: list[Redaction] = []
    if hits:
        if not redact:
            raise FrontError("秘匿情報候補を検出しました（内容は非表示）:\n" + "\n".join(hits) + "\n" + REDACT_HINT)
        brief, loaded, redactions = redact_inputs(brief_path, brief, loaded)
        remaining = scan_inputs(brief_path, brief, loaded)
        if remaining:
            raise FrontError("伏字後にも秘匿情報候補が残っています（伏字が不完全・内容は非表示）:\n" + "\n".join(remaining))
        if FACTS_HEADING not in brief:
            raise FrontError("伏字により依頼文の『## 確認済みの事実』の欄が消えました（秘密鍵の BEGIN 以降が文末まで置換された可能性）")
    return LoadedInputs(brief, loaded, total, redactions)


def validate_inputs(brief_path: Path, files: Sequence[Path]) -> tuple[str, list[tuple[Path, str]], int]:
    """従来の入口 (伏字なし。秘匿候補があれば停止)。"""
    inputs = load_inputs(brief_path, files)
    return inputs.brief, inputs.files, inputs.total


# cgd_lv0_codex の公開 API。名前を推測で呼ぶと、例外を握りつぶして PATH の npm shim
# (パスが 260 文字を超え、サンドボックスが起動できない版) へ黙って落ちる
# (2026-09-20 の実走で発覚)。契約テスト (test_engine_api_contract) がこの 4 つの実在を守る。
ENGINE_API = ("choose_bin", "candidate_bins", "weekly_percent", "latest_rate_limits")


def resolve_codex() -> str | None:
    """使える Codex CLI (パス 260 文字未満) を Lv0 のエンジンの判定で選ぶ。無ければ PATH の codex。"""
    try:
        module = importlib.import_module("cgd_lv0_codex")
        choice = module.choose_bin(module.candidate_bins())
        if choice.exe is not None:
            return str(choice.exe)
    except (ImportError, AttributeError, OSError, RuntimeError, TypeError, ValueError):
        pass
    return shutil.which("codex")


def get_weekly_percent() -> float | None:
    """直近の Codex 週枠 (%)。取得できなければ None (不明)。"""
    try:
        module = importlib.import_module("cgd_lv0_codex")
        value = module.weekly_percent(module.latest_rate_limits())
        return None if value is None else float(value)
    except (ImportError, AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return None


def language_for(path: Path) -> str:
    return {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".json": "json",
        ".md": "markdown",
        ".html": "html",
        ".css": "css",
        ".ps1": "powershell",
        ".yml": "yaml",
        ".yaml": "yaml",
    }.get(path.suffix.lower(), "text")


def build_review_input(brief: str, loaded: Sequence[tuple[Path, str]], redaction_count: int = 0) -> str:
    parts = [brief.rstrip(), ""]
    if redaction_count:
        parts.extend((REDACTION_NOTE, ""))
    for path, text in loaded:
        parts.extend((f"### {path}", f"```{language_for(path)}", text.rstrip(), "```", ""))
    return "\n".join(parts)


def json_instruction(spec: ReviewerSpec) -> str:
    severities = " / ".join(spec.severities)
    return JSON_INSTRUCTION.replace("<P>", spec.prefix).replace("<SLIST>", severities)


# Qwen は JSON の最上位を、指摘だけの配列 [ ... ] にして返しがちだった (2026-09-20 の Lv5A consult の実走: 再実行でも
# 2 回続けて「findings が配列ではありません」で不合格になり、使える者から外れて暫定版になった)。ゲート 1 は変えず、
# 形の注意を Qwen にだけ 1 文足す (lv3 に Qwen はいないので、lv3 の入力は変わらない)
QWEN_FORMAT_NOTE = (
    '【形式の注意】JSON の最上位は必ず { } のオブジェクトにし、指摘は "findings" キーの配列に入れる'
    '（例: {"findings":[{"id":"<P>1","severity":"<S1>","headline":"…"}]}）。'
    "最上位を [ ] の配列だけにすると不合格になり、再実行になる。"
)


def qwen_format_note(spec: ReviewerSpec) -> str:
    return QWEN_FORMAT_NOTE.replace("<P>", spec.prefix).replace("<S1>", spec.severities[0])


def reviewer_input(spec: ReviewerSpec, packed: str, *, focus: bool = False) -> str:
    """レビュアーへ渡す入力。focus=True (lv7/lv8 の技術レビュアー) のときだけ、重点観点ブロックを対象の前に足す。

    DeepSeek / Qwen は呼び出し側 (deepseek_coder.py / qwen_advisor.py) が役割の system prompt を持つので、
    ここでは役割の文を足さない。Codex は役割の文の直後に重点観点を置く。
    """
    suffix = json_instruction(spec) + (f"\n{qwen_format_note(spec)}" if spec.vendor == "Qwen" else "")
    lead = f"{INTEGRATION_FOCUS}\n\n" if focus and spec.kind == "technical" else ""
    if spec.vendor in ("DeepSeek", "Qwen"):
        return f"{lead}{packed}\n\n{suffix}\n"
    role_prompt = TECH_PROMPT if spec.kind == "technical" else CRIT_PROMPT
    return f"{role_prompt}\n\n{lead}{packed}\n\n{suffix}\n"


def retry_input(spec: ReviewerSpec, prompt: str, gate_error: str) -> str:
    """JSON ゲートに落ちた者への再実行の入力。Qwen にだけ、不合格の理由も渡す (他のベンダーは従来のまま = lv3 は変わらない)。"""
    text = prompt + "\n" + RETRY_NOTE + "\n"
    return text + (f"前回の不合格の理由: {gate_error}\n" if spec.vendor == "Qwen" else "")


def _subprocess(command: list[str], *, stdin: str | None, cwd: Path, timeout: int, env: dict[str, str]) -> ExecResult:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            env=env,
            timeout=timeout,
            check=False,
        )
        return ExecResult(completed.returncode, completed.stdout, completed.stderr, time.monotonic() - started)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return ExecResult(124, stdout, stderr + "\nタイムアウト", time.monotonic() - started)
    except OSError as exc:
        return ExecResult(127, "", str(exc), time.monotonic() - started)


# 子プロセスへ渡す環境変数の許可リスト (最小権限)。鍵・トークン類は、呼び出し先に必要な接頭辞の
# ものだけを通す (DeepSeek=DEEPSEEK_ / Codex=CODEX_ / Qwen=DASHSCOPE_ と QWEN_)。Codex は認証を USERPROFILE 配下の
# ファイルで行うので OPENAI_API_KEY は不要 (渡さない)。
CHILD_ENV_ALLOWED = frozenset({
    "PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "USERPROFILE", "HOME",
    "TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "COMSPEC", "PATHEXT", "LANG",
    "PYTHONIOENCODING", "PYTHONUTF8", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "USERNAME", "HOMEDRIVE", "HOMEPATH", "OS", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS",
    "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "COMMONPROGRAMFILES",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
})


QWEN_ENV_PREFIXES = ("DASHSCOPE_", "QWEN_")


def child_env(secret_prefix: str | Sequence[str]) -> dict[str, str]:
    """許可リストの変数と、指定した接頭辞 (1 個の文字列、または複数) の変数だけを子プロセスへ渡す (大文字小文字は無視)。

    空の接頭辞は無視する (空を通すと全変数が通り、鍵まで渡ってしまうため)。
    """
    raw = (secret_prefix,) if isinstance(secret_prefix, str) else tuple(secret_prefix)
    prefixes = tuple(item.upper() for item in raw if item)
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper().startswith(prefixes) or key.upper() in CHILD_ENV_ALLOWED
    }


def default_reviewer_runner(
    spec: ReviewerSpec,
    prompt: str,
    cwd: Path,
    timeout: int,
    codex_path: str,
    tools: Path,
    effort: str,
) -> ExecResult:
    if spec.vendor == "Codex":
        env = child_env("CODEX_")
        command = [
            codex_path,
            "exec",
            "-c",
            f'model_reasoning_effort="{spec.effort or effort}"',  # 組が固定した強度 (lv7/lv8) を優先
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "-",
        ]
        return _subprocess(command, stdin=prompt, cwd=cwd.parent, timeout=timeout, env=env)
    # DeepSeek と Qwen は同じ作り: 入力をファイルに書き、そのパスを渡す (どちらも API なのでファイルは読ませられない)
    scripts = {"DeepSeek": ("deepseek_coder.py", ("DEEPSEEK_",)), "Qwen": ("qwen_advisor.py", QWEN_ENV_PREFIXES)}
    if spec.vendor not in scripts:  # 未知のベンダーを黙って別のツールで代用しない
        return ExecResult(127, "", f"未対応のベンダー: {spec.vendor}")
    script, prefixes = scripts[spec.vendor]
    input_path = cwd / f"{spec.name}_input.txt"
    input_path.write_text(prompt, encoding="utf-8", newline="")
    command = [sys.executable, str(tools / script), "--role", spec.role, str(input_path)]
    return _subprocess(command, stdin=None, cwd=cwd.parent, timeout=timeout, env=child_env(prefixes))


def default_integrator_runner(prompt: str, cwd: Path, timeout: int, codex_path: str) -> ExecResult:
    env = child_env("CODEX_")
    command = [
        codex_path,
        "exec",
        "-c",
        'model_reasoning_effort="medium"',
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "-",
    ]
    return _subprocess(command, stdin=prompt, cwd=cwd.parent, timeout=timeout, env=env)


def parse_review(text: str, spec: ReviewerSpec) -> ParsedReview:
    matches = list(re.finditer(r"```json\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE))
    if len(matches) != 1:
        raise ValueError(f"JSON ブロック数が {len(matches)} 件")
    match = matches[0]
    # ブロックの後ろに文章が続いても許す (2026-09-20 の実走で DeepSeek が末尾に一言添え、
    # 再実行になって ¥1.6 を無駄にした)。JSON の中身の検査は変えない
    body = (text[: match.start()].rstrip() + "\n" + text[match.end() :].strip()).strip()
    if len(body.encode("utf-8")) < 200:
        raise ValueError("本文が 200 バイト未満")
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON が不正: {exc.msg}") from exc
    findings = payload.get("findings") if isinstance(payload, dict) else None
    # 指摘なし (空配列) は正当な結果。2026-09-20 の検証で、Codex が {"findings":[]} を返しただけで再実行も
    # 不合格になり (Codex 約 6.8 万 tok の無駄)、使える者が足りず全体が止まった
    if not isinstance(findings, list):
        raise ValueError("findings が配列ではありません")
    seen: set[str] = set()
    normalized: list[dict[str, str]] = []
    for index, item in enumerate(findings, 1):
        if not isinstance(item, dict) or not all(key in item for key in ("id", "severity", "headline")):
            raise ValueError(f"findings[{index}] の必須項目が不足")
        finding_id = item["id"]
        severity = item["severity"]
        headline = item["headline"]
        if not all(isinstance(value, str) for value in (finding_id, severity, headline)):
            raise ValueError(f"findings[{index}] の値が文字列ではありません")
        if finding_id != f"{spec.prefix}{index}" or finding_id in seen:
            raise ValueError(f"id が連番でないか重複: {finding_id}")
        if severity not in spec.severities:
            raise ValueError(f"許可されない severity: {severity}")
        if not headline or len(headline) > 40:
            raise ValueError(f"headline が空または 40 字超: {finding_id}")
        seen.add(finding_id)
        normalized.append({"id": finding_id, "severity": severity, "headline": headline})
    return ParsedReview(body, normalized)


def save_exec(run_dir: Path, name: str, result: ExecResult, attempt: int = 0) -> None:
    suffix = "" if attempt == 0 else f".retry{attempt}"
    (run_dir / f"{name}{suffix}.md").write_text(result.stdout, encoding="utf-8", newline="")
    (run_dir / f"{name}{suffix}.err").write_text(result.stderr, encoding="utf-8", newline="")
    (run_dir / f"{name}{suffix}.exit").write_text(str(result.returncode) + "\n", encoding="utf-8", newline="")


def codex_tokens(stderr: str) -> int:
    match = re.search(r"tokens used\s*[\r\n]+\s*([0-9,]+)", stderr, re.IGNORECASE)
    return int(match.group(1).replace(",", "")) if match else 0


def ds_yen(stderr: str) -> float:
    match = re.search(r"\[DS Usage\]\s*今回:.*?¥\s*([0-9,]+(?:\.[0-9]+)?)", stderr)
    return float(match.group(1).replace(",", "")) if match else 0.0


# qwen_advisor.py の stderr の書式 (実物: `[Qwen Usage] 今回: 入力 N (miss) + M (hit) / 出力 K tok (¥Y / $Z) [model=...]`)。
# ¥ は小数 2 桁で出る。書式が変わって取れなくなったら None (= 費用不明) にし、0 円とは扱わない (契約テストが書式を守る)
QWEN_USAGE_RE = re.compile(
    r"\[Qwen Usage\]\s*今回:\s*入力\s*([0-9,]+)\s*\(miss\)\s*\+\s*([0-9,]+)\s*\(hit\)\s*/\s*出力\s*([0-9,]+)\s*tok"
    r"\s*\(¥\s*([0-9,]+(?:\.[0-9]+)?)"
)


def qwen_usage(stderr: str) -> tuple[int, float] | None:
    """Qwen 1 回分の (tokens = 入力 miss + hit + 出力, 円)。usage の行が取れなければ None。"""
    match = QWEN_USAGE_RE.search(stderr)
    if not match:
        return None
    miss, hit, out = (int(match.group(index).replace(",", "")) for index in (1, 2, 3))
    return miss + hit + out, float(match.group(4).replace(",", ""))


def failure_reason(result: ExecResult) -> str:
    """実行失敗の理由。_subprocess はタイムアウトを終了コード 124 で返す。"""
    return "タイムアウト" if result.returncode == 124 else f"実行失敗(終了コード{result.returncode})"


def attempt_record(spec: ReviewerSpec, result: ExecResult, gate_error: str | None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "returncode": result.returncode,
        "gate_error": gate_error,
        "tokens": codex_tokens(result.stderr) if spec.vendor == "Codex" else 0,
        "yen": ds_yen(result.stderr) if spec.vendor == "DeepSeek" else 0.0,
    }
    if spec.vendor == "Qwen":
        usage = qwen_usage(result.stderr)
        record.update(tokens=usage[0] if usage else 0, yen=usage[1] if usage else 0.0, cost_known=usage is not None)
    return record


def calculate_costs(
    history: Sequence[tuple[ReviewerSpec, ExecResult]],
    integrations: Sequence[ExecResult],
    *,
    with_qwen: bool = False,
) -> dict[str, Any]:
    """費用の集計。Qwen の項目は、組に Qwen がいるとき (with_qwen) だけ足す (lv3 の run.json を変えないため)。

    Qwen の usage の行が取れない呼出は 0 として数え、qwen_cost_known を false にして「不明」を明示する。
    """
    costs: dict[str, Any] = {
        "codex_calls": sum(spec.vendor == "Codex" for spec, _ in history) + len(integrations),
        "codex_tokens": sum(
            codex_tokens(result.stderr) for spec, result in history if spec.vendor == "Codex"
        ) + sum(codex_tokens(result.stderr) for result in integrations),
        "ds_calls": sum(spec.vendor == "DeepSeek" for spec, _ in history),
        "ds_yen": sum(ds_yen(result.stderr) for spec, result in history if spec.vendor == "DeepSeek"),
    }
    if with_qwen:
        usages = [qwen_usage(result.stderr) for spec, result in history if spec.vendor == "Qwen"]
        costs.update(
            qwen_calls=len(usages),
            qwen_tokens=sum(item[0] for item in usages if item),
            qwen_yen=float(sum(item[1] for item in usages if item)),
            qwen_cost_known=all(item is not None for item in usages),
        )
    return costs


def anonymize(
    parsed: dict[str, ParsedReview],
    specs: Sequence[ReviewerSpec],
    seed_text: str,
    log_files: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, dict[str, str]]]:
    ordered = list(specs)
    seed = int.from_bytes(hashlib.sha256(seed_text.encode("utf-8")).digest()[:8], "big")
    random.Random(seed).shuffle(ordered)
    sections: list[dict[str, Any]] = []
    mapping: dict[str, str] = {}
    metadata: dict[str, dict[str, str]] = {}
    for reviewer_number, spec in enumerate(ordered, 1):
        alias = f"R{reviewer_number}"
        mapping[alias] = spec.name
        findings: list[dict[str, str]] = []
        for finding_number, finding in enumerate(parsed[spec.name].findings, 1):
            anonymous_id = f"{alias}#{finding_number}"
            item = {**finding, "id": anonymous_id}
            findings.append(item)
            metadata[anonymous_id] = {
                "kind": spec.kind,
                "vendor": spec.vendor,
                "reviewer": alias,
                "reviewer_name": spec.name,
                "log_file": (log_files or {}).get(spec.name, spec.name),
                "severity": finding["severity"],
                "headline": finding["headline"],
            }
        sections.append({"alias": alias, "kind": spec.kind, "body": parsed[spec.name].body, "findings": findings})
    return sections, mapping, metadata


INTEGRATOR_RULES = """あなたは複数のレビューを統合する議長。R1〜R<K> の出所は伏せてある。どの AI かを推測して扱いを変えないこと。
与えられた指摘 ID（R<n>#<k>）すべてを、ちょうど 1 つのクラスタに割り当てる。似た指摘は 1 クラスタにまとめる。technical と critic は同じクラスタに混ぜない。クラスタ数の上限は設けない。
重大度と出所の数は書かない（機械が指摘 ID から算出する）。
出力は JSON のみ: {"summary":"総評(1〜3文)","clusters":[{"id":"C1","kind":"technical","title":"40字以内","members":["R1#1","R2#3"],"proposal":"対応案(1文)","adopt":"採用|部分採用|見送り","adopt_reason":"1文"}],"questions_for_user":[{"id":"Q1","question":"...","options":[{"label":"...","description":"..."}],"recommended":"labelのどれか","recommended_reason":"根拠。依頼文の『確認済みの事実』の語句か、R<n>#<k> を引くこと"}],"next_actions":["..."]}
questions_for_user はユーザーの方向性の判断が要るものだけ、最大 3 問、選択肢は 2〜4 個。依頼文に無い事実を推測で根拠にしない。
依頼文の『確認済みの事実』や依頼の内容で既に決まっていることは質問にしない（推奨がそれと食い違う質問を作らない）。"""


def integrator_input(sections: Sequence[dict[str, Any]]) -> str:
    # 暫定版では使える者だけが R1..Rk になる。k は実際の件数 (欠けた者が誰かは伏せる)
    parts = [INTEGRATOR_RULES.replace("<K>", str(len(sections))), "", "レビュー入力:"]
    for section in sections:
        parts.extend((f"## {section['alias']} ({section['kind']})", section["body"], "指摘一覧:"))
        for item in section["findings"]:
            parts.append(f"{item['id']}: [{item['severity']}] {item['headline']}")
        parts.append("")
    return "\n".join(parts)


def extract_json_object(text: str) -> dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("JSON オブジェクトがありません")
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"統合 JSON が不正: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError("統合結果がオブジェクトではありません")
    return value


def validate_integration(payload: dict[str, Any], metadata: dict[str, dict[str, str]]) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload.get("summary"), str):
        errors.append("summary が文字列ではありません")
    actions = payload.get("next_actions")
    if not isinstance(actions, list) or not all(isinstance(item, str) for item in actions):
        errors.append("next_actions が文字列の配列ではありません")
    clusters = payload.get("clusters")
    if not isinstance(clusters, list):
        return ["clusters が配列ではありません"]
    occurrences: list[str] = []
    cluster_ids: list[str] = []
    for index, cluster in enumerate(clusters, 1):
        if not isinstance(cluster, dict):
            errors.append(f"cluster {index} がオブジェクトではありません")
            continue
        required = ("id", "kind", "title", "members", "proposal", "adopt", "adopt_reason")
        if not all(key in cluster for key in required):
            errors.append(f"cluster {index} の必須項目が不足しています")
        cluster_id = cluster.get("id")
        if not isinstance(cluster_id, str):
            errors.append(f"cluster {index} の id が文字列ではありません")
        else:
            cluster_ids.append(cluster_id)
        title = cluster.get("title")
        if not isinstance(title, str) or not 1 <= len(title) <= 60:
            errors.append(f"cluster {index} の title が 1〜60 字ではありません")
        if cluster.get("adopt") not in ("採用", "部分採用", "見送り"):
            errors.append(f"cluster {index} の adopt が許可値ではありません")
        members = cluster.get("members")
        if not isinstance(members, list) or not members:
            errors.append(f"cluster {index} の members が空です")
            continue
        string_members = [member for member in members if isinstance(member, str)]
        if len(string_members) != len(members):
            errors.append(f"cluster {index} に文字列でない ID があります")
        occurrences.extend(string_members)
        if len(string_members) != len(set(string_members)):
            errors.append(f"cluster {index} 内で ID が重複しています")
        known = [member for member in string_members if member in metadata]
        kinds = {metadata[member]["kind"] for member in known}
        if len(kinds) > 1:
            errors.append(f"cluster {index} で kind が混在しています")
        if kinds and cluster.get("kind") not in kinds:
            errors.append(f"cluster {index} の kind が members と一致しません")
    expected = set(metadata)
    actual = set(occurrences)
    missing, unknown = expected - actual, actual - expected
    duplicates = {item for item in occurrences if occurrences.count(item) > 1}
    if missing:
        errors.append("未割当 ID: " + ", ".join(sorted(missing)))
    if unknown:
        errors.append("未知 ID: " + ", ".join(sorted(unknown)))
    if duplicates:
        errors.append("重複割当 ID: " + ", ".join(sorted(duplicates)))
    duplicate_cluster_ids = {item for item in cluster_ids if cluster_ids.count(item) > 1}
    if duplicate_cluster_ids:
        errors.append("cluster.id が重複: " + ", ".join(sorted(duplicate_cluster_ids)))
    questions = payload.get("questions_for_user", [])
    if not isinstance(questions, list) or len(questions) > 3:
        errors.append("questions_for_user が配列でないか 3 問超です")
    else:
        question_ids: list[str] = []
        for index, question in enumerate(questions, 1):
            question_id = question.get("id") if isinstance(question, dict) else None
            if not isinstance(question_id, str):
                errors.append(f"質問 {index} の id が文字列ではありません")
            else:
                question_ids.append(question_id)
            options = question.get("options") if isinstance(question, dict) else None
            if not isinstance(options, list) or not 2 <= len(options) <= 4:
                errors.append(f"質問 {index} の選択肢数が 2〜4 ではありません")
                continue
            labels = [option.get("label") for option in options if isinstance(option, dict)]
            if len(labels) != len(options) or question.get("recommended") not in labels:
                errors.append(f"質問 {index} の recommended が選択肢と一致しません")
        duplicate_question_ids = {item for item in question_ids if question_ids.count(item) > 1}
        if duplicate_question_ids:
            errors.append("question.id が重複: " + ", ".join(sorted(duplicate_question_ids)))
    return errors


def enrich_clusters(payload: dict[str, Any], metadata: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    ranks = {"technical": {"🟡": 1, "🟠": 2, "🔴": 3}, "critic": {"低": 1, "中": 2, "高": 3}}
    clusters: list[dict[str, Any]] = []
    for cluster in payload["clusters"]:
        members = cluster["members"]
        kind = cluster["kind"]
        severity = max((metadata[item]["severity"] for item in members), key=ranks[kind].get)
        vendors = sorted({metadata[item]["vendor"] for item in members})
        reviewers = {metadata[item]["reviewer"] for item in members}
        clusters.append({**cluster, "severity": severity, "vendors": vendors, "single_source": len(reviewers) == 1})
    return clusters


def confirmed_fact_lines(brief: str) -> list[str]:
    after = brief.split("## 確認済みの事実", 1)[1]
    section = after.split("\n## ", 1)[0]
    return [line.strip(" -\t") for line in section.splitlines() if len(line.strip(" -\t")) >= 4]


def prepare_questions(payload: dict[str, Any], brief: str, metadata: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    facts = confirmed_fact_lines(brief)
    questions: list[dict[str, Any]] = []
    for question in payload.get("questions_for_user", []):
        reason = question.get("recommended_reason", "")
        id_hit = any(item in reason for item in metadata)
        fact_hit = any(fact in reason for fact in facts)
        questions.append({**question, "reason_ok": bool(reason and (id_hit or fact_hit))})
    return questions


def cost_summary(costs: Mapping[str, Any], with_qwen: bool) -> str:
    """費用の 1 行。Codex・DeepSeek は従来の書式のまま、組に Qwen がいるときだけ Qwen を足す (不明なら明記)。"""
    line = (
        f"Codex: {costs['codex_calls']} 回 / {costs['codex_tokens']:,} tokens、"
        f"DeepSeek: {costs['ds_calls']} 回 / ¥{costs['ds_yen']:.3f}"
    )
    if not with_qwen:
        return line
    known = costs.get("qwen_cost_known", True)
    line += f"、Qwen: {costs.get('qwen_calls', 0)} 回 / {costs.get('qwen_tokens', 0):,} tokens / ¥{costs.get('qwen_yen', 0.0):.2f}"
    return line + ("" if known else "（usage を取得できない呼出があり、費用は不明を 0 として数えた）")


def _omitted_label(items: Sequence[dict[str, Any]], limit: int = 160) -> str:
    """表から外したクラスタの題名を並べる。行数を増やさず、外した指摘の存在と中身を見えるようにする。"""
    titles = " / ".join(_safe_cell(item["title"]) for item in items)
    if len(titles) > limit:
        titles = titles[:limit] + "…"
    return f"他 {len(items)} 件（run.json 参照）: {titles}"


def _safe_cell(value: Any) -> str:
    return str(value).replace("|", "｜").replace("\r", " ").replace("\n", " ")


def _short_reason(reason: str) -> str:
    """欠落理由を見出し用に短くする (「JSON不正: …」→「JSON不正」、「実行失敗(終了コード1)」→「実行失敗」)。"""
    return re.split(r"[(:]", reason, maxsplit=1)[0].strip()


def _missing_view(name: str, roster: Sequence[ReviewerSpec] = REVIEWERS) -> str:
    """欠けた者の視点とベンダー (例: DeepSeek(技術))。同じベンダー・視点が組に複数いるときは強度も添える (Codex(技術・high))。"""
    spec = next((item for item in roster if item.name == name), None)
    if spec is None:
        return name
    twins = [item for item in roster if item.vendor == spec.vendor and item.kind == spec.kind]
    effort = f"・{spec.effort}" if spec.effort and len(twins) > 1 else ""
    return f"{spec.vendor}({'技術' if spec.kind == 'technical' else '批評'}{effort})"


def partial_warning(missing: Sequence[dict[str, str]], roster: Sequence[ReviewerSpec] = REVIEWERS) -> str:
    names = "、".join(item["name"] for item in missing)
    views = "、".join(_missing_view(item["name"], roster) for item in missing)
    return f"⚠ 暫定: {names} が欠けています。収束の判定が弱く、{views} の指摘が出ていません。"


def build_report(
    run_name: str,
    elapsed: float,
    clusters: Sequence[dict[str, Any]],
    metadata: dict[str, dict[str, str]],
    payload: dict[str, Any],
    questions: Sequence[dict[str, Any]],
    costs: dict[str, Any],
    run_dir: Path,
    no_ds: bool,
    *,
    partial_missing: Sequence[dict[str, str]] = (),
    redaction_count: int = 0,
    no_finding_reviewers: Sequence[str] = (),
    roster: Sequence[ReviewerSpec] = REVIEWERS,
    no_qwen: bool = False,
    roster_name: str = DEFAULT_ROSTER,
) -> str:
    ranks = {"🔴": 3, "🟠": 2, "🟡": 1, "高": 3, "中": 2, "低": 1}
    # 表のベンダー列は組にいるベンダーだけ (lv3 は Codex・DeepSeek の 2 列で従来どおり)。--no-ds / --no-qwen で
    # 外した者の列も残す (空欄のまま。lv3 の --no-ds の既存の見え方を保つため)
    vendors = roster_vendors(roster)
    columns = 4 + len(vendors)

    def marks(item: dict[str, Any]) -> str:
        return " | ".join("✅" if vendor in item["vendors"] else "" for vendor in vendors)

    def table_head(first: str, second: str, last: str) -> list[str]:
        return [
            f"| {first} | {second} | " + " | ".join(vendors) + f" | 採否案 | {last} |",
            "|---|---|" + "---|" * len(vendors) + "---|---|",
        ]

    def row(item: dict[str, Any]) -> str:
        title = item["title"] + (" (単独)" if item["single_source"] else "")
        return f"| {_safe_cell(title)} | {item['severity']} | {marks(item)} | {_safe_cell(item.get('adopt', ''))} | {_safe_cell(item.get('proposal', ''))} |"

    def omitted_row(items: Sequence[dict[str, Any]]) -> str:
        return f"| {_omitted_label(items)} |" + " |" * (columns - 1)

    # 批評のレビュアーがいない組 (lv7) では、空の批評の表を出さない (lv3・lv8 は従来どおり出す)
    has_critic = any(item.kind == "critic" for item in roster)
    ordered = sorted(clusters, key=lambda item: (-ranks[item["severity"]], -len(item["vendors"])))
    technical = [item for item in ordered if item["kind"] == "technical"]
    critics = [item for item in ordered if item["kind"] == "critic"]
    shown_tech, shown_crit = list(technical), list(critics)
    if partial_missing:
        detail = ", ".join(f"{item['name']}={_short_reason(item['reason'])}" for item in partial_missing)
        status = f"暫定（欠落: {detail}）"
    else:
        status = "成功"
    header = [
        f"# {run_name}", "",
        f"所要: {elapsed:.1f} 秒 / 状態: {status}" + (" / DS なし" if no_ds else "") + (" / Qwen なし" if no_qwen else ""),
    ]
    if roster_name != DEFAULT_ROSTER:  # lv3 の見出しは従来どおり (組の名前を出さない)
        header.append(f"組: {roster_name}（{' / '.join(item.name for item in roster)}）")
    if partial_missing:
        header.append(partial_warning(partial_missing, roster))
    if redaction_count:
        header.append(f"伏字: {redaction_count} 件（詳細は redaction.json）")
    if no_finding_reviewers:
        header.append(f"指摘なし（JSON 0 件）: {', '.join(no_finding_reviewers)}（本文は生ログで確認できる）")
    header.extend(("", PROPOSAL_NOTE, ""))

    def render(detail_limit: int | None = None, truncate_descriptions: bool = False) -> list[str]:
        lines = list(header)
        lines.extend(("## 技術レビュー", "", *table_head("指摘", "重大度", "対応案")))
        lines.extend(row(item) for item in shown_tech)
        omitted = len(technical) - len(shown_tech)
        if omitted:
            lines.append(omitted_row([i for i in technical if i not in shown_tech]))
        if has_critic:
            lines.extend(("", "## 批評レビュー", "", *table_head("観点", "困り度", "改善の方向")))
            lines.extend(row(item) for item in shown_crit)
            omitted = len(critics) - len(shown_crit)
            if omitted:
                lines.append(omitted_row([i for i in critics if i not in shown_crit]))
        lines.extend(("", "## 🔴 の詳細", ""))
        red = [item for item in technical if item["severity"] == "🔴"]
        if not red:
            lines.append("なし")
        for item in red:
            lines.append(f"- {_safe_cell(item['title'])}")
            members = item["members"] if detail_limit is None else item["members"][:detail_limit]
            for member in members:
                data = metadata[member]
                lines.append(
                    f"  - {member} ({data['vendor']}): {_safe_cell(data['headline'])}"
                    f" — {data.get('log_file', data['reviewer_name'])}.md"
                )
            if detail_limit is not None and len(item["members"]) > detail_limit:
                lines.append(f"  - 他 {len(item['members']) - detail_limit} 件")
        lines.extend(("", "## 総評", "", _safe_cell(payload.get("summary", "")), "", "## ユーザーへの質問", ""))
        if not questions:
            lines.append("なし")
        for question in questions:
            options = " / ".join(f"{item.get('label', '')}: {item.get('description', '')}" for item in question["options"])
            reason = question.get("recommended_reason", "") if question["reason_ok"] else "(根拠なし)"
            detail = f"{question.get('id', '')} {_safe_cell(question.get('question', ''))} — 選択肢: {_safe_cell(options)} — 推奨: {_safe_cell(question.get('recommended', ''))} — 根拠: {_safe_cell(reason)}"
            lines.append("- " + (detail[:120] if truncate_descriptions else detail))
        lines.extend(("", "## 次アクション", ""))
        actions = payload.get("next_actions", [])
        lines.extend(
            f"- {(_safe_cell(item)[:120] if truncate_descriptions else _safe_cell(item))}"
            for item in actions
        )
        if not actions:
            lines.append("なし")
        lines.extend(("", "## 費用", "", cost_summary(costs, "Qwen" in vendors), "", "## 生ログ", "", str(run_dir)))
        return lines

    detail_limit: int | None = None
    truncate_descriptions = False
    lines = render(detail_limit, truncate_descriptions)
    while len(lines) > 90:
        # 🔴 の行は表から外さない (採否・対応案が消えるため)。外せるのは 🔴 以外だけ
        removable = [item for item in (*shown_tech, *shown_crit) if item["severity"] != "🔴"]
        target = min(
            removable,
            key=lambda item: (ranks[item["severity"]], len(item["vendors"])),
            default=None,
        )
        if target is None:
            break
        (shown_crit if target in shown_crit else shown_tech).remove(target)
        lines = render(detail_limit, truncate_descriptions)
    if len(lines) > 90:
        detail_limit = 3
        lines = render(detail_limit, truncate_descriptions)
    if len(lines) > 90:
        truncate_descriptions = True
        lines = render(detail_limit, truncate_descriptions)
    if len(lines) > 90:
        lines.insert(lines.index(PROPOSAL_NOTE), "⚠ 行数超過: 必須節と全 🔴 クラスタを保持するため 90 行を超えています。")
    return "\n".join(lines) + "\n"


def create_run_dir(work_root: Path, label: str, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    suffix = f"{random.SystemRandom().randrange(0x1000000):06x}"
    run_dir = work_root / f"{label}_{stamp}_{suffix}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="")


def default_usage_logger(note: str = "lv3a") -> None:
    # 記録するレベルは組によらず 3。lv7/lv8 と記録すると Workflow 必須のゲートが張られ、無関係な Codex 直叩きを止めてしまう
    subprocess.run(
        [sys.executable, str(TOOLS / "cgd_usage_log.py"), "record", "--level", "3", "--note", note],
        capture_output=True,
        check=False,
    )


def active_specs(roster: Sequence[ReviewerSpec], no_ds: bool, no_qwen: bool) -> list[ReviewerSpec]:
    """組から、--no-ds / --no-qwen で明示的に外した者を除いたレビュアー (外した者は「欠落」に数えない)。"""
    return [
        item for item in roster
        if not (no_ds and item.vendor == "DeepSeek") and not (no_qwen and item.vendor == "Qwen")
    ]


# qwen_advisor.py の DEFAULT_BASE_URL と同じ既定 (Singapore)。契約テストが一致を守る。QWEN_BASE_URL があればそれを優先する
DEFAULT_QWEN_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
QWEN_REGIONS = {
    "dashscope-intl.aliyuncs.com": "国際（シンガポール）",
    "dashscope-us.aliyuncs.com": "米国（バージニア）",
    "dashscope.aliyuncs.com": "中国本土（北京）",
}


def qwen_destination(environ: Mapping[str, str] | None = None) -> str:
    """Qwen の送信先の表示。QWEN_BASE_URL が指す DashScope のリージョンを添える (表示するのはホスト名だけ。鍵は出さない)。"""
    base = (os.environ if environ is None else environ).get("QWEN_BASE_URL") or DEFAULT_QWEN_BASE_URL
    host = urlparse(base).hostname or "(ホスト不明)"
    return f"Qwen（Alibaba DashScope・{QWEN_REGIONS.get(host, 'リージョン不明')}: {host}）"


def plan_targets(specs: Sequence[ReviewerSpec]) -> str:
    """送信先の一覧。Codex は統合に必ず使う。DeepSeek・Qwen は組に (除外されずに) いるときだけ。"""
    vendors = roster_vendors(specs)
    parts = ["Codex（OpenAI）"]
    if "DeepSeek" in vendors:
        parts.append("DeepSeek（中国本土サーバ）")
    if "Qwen" in vendors:
        parts.append(qwen_destination())
    return "、".join(parts)


def plan_calls(specs: Sequence[ReviewerSpec]) -> tuple[str, int]:
    """呼出予定の文と、Codex の呼出回数の見込み (Codex のレビュアー数 + 統合 1。再実行は数えない)。"""
    codex = [item for item in specs if item.vendor == "Codex"]
    labels = [("技術" if item.kind == "technical" else "批評") + (f"[{item.effort}]" if item.effort else "") for item in codex]
    text = f"Codex {len(codex) + 1} 回（{'・'.join([*labels, '統合'])}）"
    for vendor, short in (("DeepSeek", "DS"), ("Qwen", "Qwen")):
        count = sum(item.vendor == vendor for item in specs)
        if count:
            text += f"、{short} {count} 回"
    return text, len(codex) + 1


def print_roster(name: str, roster: Sequence[ReviewerSpec], specs: Sequence[ReviewerSpec]) -> None:
    """組の内訳 (名前・ベンダー・視点・強度)。--no-ds / --no-qwen で外した者は「除外」と明記する。"""
    print(f"組: {name}（{len(specs)} 者）")
    for item in roster:
        view = "技術" if item.kind == "technical" else "批評"
        effort = ""
        if item.vendor == "Codex":
            effort = f" / 強度 {item.effort}" if item.effort else f" / 強度 --effort（既定 {DEFAULT_EFFORT}）"
        excluded = "" if item in specs else "（除外: --no-ds / --no-qwen）"
        print(f"- {item.name}: {item.vendor} / {view}{effort}{excluded}")


def print_redaction_preview(brief_path: Path, redactions: Sequence[Redaction]) -> None:
    """伏字プレビュー。件数・パターン別の件数・行番号だけを出す (伏字にした中身は出さない)。"""
    if not redactions:
        print("伏字: 0 件（秘匿情報候補なし）")
        return
    print(f"伏字プレビュー（内容は表示しません）: 伏字 {len(redactions)} 件")
    counts = Counter(item.pattern for item in redactions)
    print("パターン別: " + " / ".join(f"{name} {count} 件" for name, count in sorted(counts.items())))
    lines_by_file: dict[str, list[int]] = {}
    for item in redactions:
        lines_by_file.setdefault(item.file, []).append(item.line)
    for file, numbers in lines_by_file.items():
        label = "依頼文" if file == str(brief_path) else file
        print(f"- {label}: 行 " + ", ".join(str(number) for number in sorted(set(numbers))))
    print("※ 伏字にして送るには、ユーザーの承認が必要です（承認後に run --redact を実行）")


def command_plan(args: argparse.Namespace, resolver: Callable[[], str | None], weekly: Callable[[], float | None]) -> int:
    try:
        roster = roster_specs(args.roster)
        inputs = load_inputs(Path(args.brief), [Path(item) for item in args.files], redact=args.redact)
    except FrontError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    brief, loaded, total = inputs.brief, inputs.files, inputs.total
    codex_path = resolver()
    if not codex_path:
        print("Codex 実行ファイルが見つかりません", file=sys.stderr)
        return 2
    weekly_percent = weekly()
    if weekly_percent is not None and weekly_percent >= 80:
        print(f"Codex 週枠が {weekly_percent:.1f}% のため停止します", file=sys.stderr)
        return 11
    print("依頼文先頭 200 字:")
    print(brief[:200])
    print("\n詰め込むファイル:")
    for path, text in loaded:
        print(f"- {path}: {len(text.encode('utf-8'))} bytes")
    print(f"合計: {total} bytes")
    if args.redact:
        print_redaction_preview(Path(args.brief), inputs.redactions)
    print(f"Codex: {codex_path}")
    print("週枠: 不明（取得失敗）" if weekly_percent is None else f"週枠: {weekly_percent:.1f}%")
    specs = active_specs(roster, args.no_ds, args.no_qwen)
    print_roster(args.roster, roster, specs)
    calls, codex_calls = plan_calls(specs)
    print(f"送信先: {plan_targets(specs)}")
    print(f"呼出予定: {calls} / Codex 想定 tokens: 約 {2 * codex_calls} 万")
    if any(item.vendor == "Qwen" for item in specs) and "DASHSCOPE_API_KEY" not in os.environ:  # 有無だけ見る (値は見ない・出さない)
        print("⚠ DASHSCOPE_API_KEY が未設定です: Qwen は失敗し、暫定版（exit 20）になります（--no-qwen で外せます）")
    return 0


def command_run(
    args: argparse.Namespace,
    *,
    reviewer_runner: ReviewerRunner,
    integrator_runner: IntegratorRunner,
    usage_logger: Callable[[], None],
    resolver: Callable[[], str | None],
    weekly: Callable[[], float | None],
) -> int:
    if args.roster in FIXED_EFFORT_ROSTERS and args.effort is not None:
        # 黙って無視しない: 強度を選んだつもりのまま、組が決めた強度で走ってしまうため
        print(
            f"--roster {args.roster} では Codex の強度を組で固定しているため、--effort は併用できません"
            "（指定を外してください）",
            file=sys.stderr,
        )
        return 1
    try:
        roster = roster_specs(args.roster)
        inputs = load_inputs(Path(args.brief), [Path(item) for item in args.files], redact=args.redact)
    except FrontError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    brief, loaded, redactions = inputs.brief, inputs.files, inputs.redactions
    codex_path = resolver()
    if not codex_path:
        print("Codex 実行ファイルが見つかりません", file=sys.stderr)
        return 2
    weekly_percent = weekly()
    if weekly_percent is not None and weekly_percent >= 80:
        print(f"Codex 週枠が {weekly_percent:.1f}% のため停止します", file=sys.stderr)
        return 11
    started = time.monotonic()
    if weekly_percent is None:
        print("週枠: 不明（取得失敗）")
    work_root = Path(args.work_root)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.label):
        print("label は英数字・ピリオド・アンダースコア・ハイフンだけ使用できます", file=sys.stderr)
        return 1
    if args.codex_timeout <= 0 or args.ds_timeout <= 0 or args.qwen_timeout <= 0:
        print("timeout は正の整数で指定してください", file=sys.stderr)
        return 1
    try:
        run_dir = create_run_dir(work_root, args.label)
    except OSError as exc:
        print(f"run ディレクトリを作成できません: {exc}", file=sys.stderr)
        return 1
    packed = build_review_input(brief, loaded, len(redactions))
    (run_dir / "review_input.txt").write_text(packed, encoding="utf-8", newline="")
    if redactions:
        # 伏字にした場所 (ファイル・行・パターン名) だけ。中身は入れない
        write_json(run_dir / "redaction.json", [asdict(item) for item in redactions])
    specs = active_specs(roster, args.no_ds, args.no_qwen)
    has_qwen = any(item.vendor == "Qwen" for item in roster)
    base_effort = args.effort or DEFAULT_EFFORT
    # 実際に渡す強度: lv7/lv8 は組が固定した値、lv3 は --effort (Codex 以外は使わないので値は無関係)
    efforts = {spec.name: spec.effort or base_effort for spec in specs}
    focus = args.roster in FOCUS_ROSTERS
    prompts = {spec.name: reviewer_input(spec, packed, focus=focus) for spec in specs}
    results: dict[str, ExecResult] = {}
    result_history: list[tuple[ReviewerSpec, ExecResult]] = []
    attempts: dict[str, list[dict[str, Any]]] = {spec.name: [] for spec in specs}
    timeouts = {"Codex": args.codex_timeout, "DeepSeek": args.ds_timeout, "Qwen": args.qwen_timeout}
    with ThreadPoolExecutor(max_workers=len(specs)) as executor:
        futures = {
            executor.submit(
                reviewer_runner, spec, prompts[spec.name], run_dir,
                timeouts[spec.vendor], codex_path, TOOLS, efforts[spec.name],
            ): spec
            for spec in specs
        }
        for future in as_completed(futures):
            spec = futures[future]
            try:
                results[spec.name] = future.result()
            except Exception as exc:  # injected/external executors must become recorded failures
                results[spec.name] = ExecResult(127, "", str(exc))
            result_history.append((spec, results[spec.name]))
            save_exec(run_dir, spec.name, results[spec.name])
            attempts[spec.name].append(attempt_record(spec, results[spec.name], None))
    # 実行に失敗した者 (終了コード非 0・タイムアウト) と理由。JSON ゲートの不合格はこの後で別に扱う
    exec_missing = {
        spec.name: failure_reason(results[spec.name]) for spec in specs if results[spec.name].returncode != 0
    }
    base_state: dict[str, Any] = {
        "run_name": run_dir.name,
        "roster": args.roster,
        "weekly_percent": weekly_percent,
        "no_ds": args.no_ds,
        "no_partial": args.no_partial,
        "redactions": len(redactions),
    }
    if has_qwen:
        base_state["no_qwen"] = args.no_qwen
    by_name = {spec.name: spec for spec in specs}

    def reviewer_state() -> dict[str, Any]:
        return {
            name: {
                **asdict(result),
                "attempts": attempts[name],
                "vendor": by_name[name].vendor,
                "effort": efforts[name] if by_name[name].vendor == "Codex" else None,
            }
            for name, result in results.items()
        }

    def finish_failure(status: str, exit_code: int, **extra: Any) -> int:
        base_state.update(
            {
                "status": status,
                "exit_code": exit_code,
                "elapsed_seconds": time.monotonic() - started,
                "reviewers": reviewer_state(),
                "costs": calculate_costs(result_history, integration_results, with_qwen=has_qwen),
                **extra,
            }
        )
        write_json(run_dir / "run.json", base_state)
        return exit_code

    integration_results: list[ExecResult] = []
    if exec_missing and args.no_partial:
        # 従来の厳格な挙動: 1 者でも実行に失敗したら、再実行もせず止める
        print("レビュアー実行失敗: " + ", ".join(exec_missing), file=sys.stderr)
        return finish_failure(
            "reviewer_execution_failed", 10, gate1={"ok": False, "failed": list(exec_missing)}
        )
    parsed: dict[str, ParsedReview] = {}
    gate_errors: dict[str, str] = {}
    for spec in specs:
        if spec.name in exec_missing:
            continue
        try:
            parsed[spec.name] = parse_review(results[spec.name].stdout, spec)
        except ValueError as exc:
            gate_errors[spec.name] = str(exc)
            attempts[spec.name][0]["gate_error"] = str(exc)
    retry_specs = [spec for spec in specs if spec.name in gate_errors]
    if not args.no_partial and len(parsed) + len(retry_specs) < MIN_USABLE_REVIEWERS:
        retry_specs = []  # 救済できても暫定版に届かない。費用を使わず、従来どおりの失敗で止める
    retry_results: dict[str, ExecResult] = {}
    if retry_specs:
        with ThreadPoolExecutor(max_workers=len(retry_specs)) as executor:
            futures = {
                executor.submit(
                    reviewer_runner, spec, retry_input(spec, prompts[spec.name], gate_errors[spec.name]),
                    run_dir, timeouts[spec.vendor], codex_path, TOOLS, efforts[spec.name],
                ): spec
                for spec in retry_specs
            }
            for future in as_completed(futures):
                spec = futures[future]
                try:
                    retry_results[spec.name] = future.result()
                except Exception as exc:
                    retry_results[spec.name] = ExecResult(127, "", str(exc))
    for spec in retry_specs:
        retry = retry_results[spec.name]
        results[spec.name] = retry
        result_history.append((spec, retry))
        save_exec(run_dir, spec.name, retry, 1)
        gate_error: str | None = None
        if retry.returncode != 0:
            exec_missing[spec.name] = failure_reason(retry)
            gate_errors.pop(spec.name, None)
        else:
            try:
                parsed[spec.name] = parse_review(retry.stdout, spec)
                gate_errors.pop(spec.name, None)
            except ValueError as exc:
                gate_error = str(exc)
                gate_errors[spec.name] = gate_error
        attempts[spec.name].append(attempt_record(spec, retry, gate_error))
    usable_specs = [spec for spec in specs if spec.name in parsed]
    if args.no_partial or len(usable_specs) < MIN_USABLE_REVIEWERS:
        # 暫定版にできない (--no-partial、または使える者が 2 者未満)。原因が実行失敗なら 10、JSON 不正なら 12。
        # 両方あるときは両方を出す (実行失敗だけを出すと JSON 不正という真因が隠れる。2026-09-20 の検証)
        if exec_missing:
            print(
                "レビュアー実行失敗: " + ", ".join(f"{name}（{reason}）" for name, reason in exec_missing.items()),
                file=sys.stderr,
            )
        for name, error in gate_errors.items():
            print(f"{name}: {error}", file=sys.stderr)
        if exec_missing:
            failed_gate1: dict[str, Any] = {"ok": False, "failed": list(exec_missing)}
            if gate_errors:
                failed_gate1["errors"] = gate_errors
            return finish_failure("reviewer_execution_failed", 10, gate1=failed_gate1)
        if gate_errors:
            return finish_failure("gate1_failed", 12, gate1={"ok": False, "errors": gate_errors})
    # 欠けた者 (実行失敗 / JSON 不正) と理由。使える者が 2 者以上ならここから暫定版になる
    missing_records = [
        {
            "name": spec.name,
            "reason": exec_missing[spec.name] if spec.name in exec_missing else f"JSON不正: {gate_errors[spec.name]}",
        }
        for spec in specs
        if spec.name not in parsed
    ]
    partial = bool(missing_records)
    if partial:
        print("暫定版: 欠けた者=" + ", ".join(f"{item['name']}（{item['reason']}）" for item in missing_records), file=sys.stderr)
    partial_extra: dict[str, Any] = {"partial": {"missing": missing_records}} if partial else {}
    no_finding_names = [spec.name for spec in usable_specs if not parsed[spec.name].findings]  # 指摘なし (JSON 0 件) の者
    # 再実行があった者は、採用した (=見出しの出所の) 再実行のログを指す
    log_files = {
        spec.name: (f"{spec.name}.retry1" if len(attempts[spec.name]) > 1 else spec.name)
        for spec in specs
    }
    # 統合・ゲート2・匿名化は使える者だけが対象 (R1..Rk)
    sections, mapping, metadata = anonymize(parsed, usable_specs, run_dir.name, log_files)
    integration_prompt = integrator_input(sections)
    try:
        integration = integrator_runner(integration_prompt, run_dir, args.codex_timeout, codex_path)
    except Exception as exc:
        integration = ExecResult(127, "", str(exc))
    save_exec(run_dir, "integration", integration)
    integration_results.append(integration)
    integration_error = ""
    payload: dict[str, Any] | None = None
    if integration.returncode == 0:
        try:
            payload = extract_json_object(integration.stdout)
            errors = validate_integration(payload, metadata)
            integration_error = "; ".join(errors)
        except ValueError as exc:
            integration_error = str(exc)
    else:
        integration_error = f"統合実行の終了コード {integration.returncode}"
    if integration_error:
        retry_prompt = integration_prompt + "\n\n前回のエラー:\n" + integration_error + "\n全体を JSON で出し直してください。"
        try:
            integration = integrator_runner(retry_prompt, run_dir, args.codex_timeout, codex_path)
        except Exception as exc:
            integration = ExecResult(127, "", str(exc))
        integration_results.append(integration)
        save_exec(run_dir, "integration", integration, 1)
        try:
            if integration.returncode != 0:
                raise ValueError(f"統合実行の終了コード {integration.returncode}")
            payload = extract_json_object(integration.stdout)
            errors = validate_integration(payload, metadata)
            if errors:
                raise ValueError("; ".join(errors))
            integration_error = ""
        except ValueError as exc:
            integration_error = str(exc)
    if integration_error or payload is None:
        print("統合ゲート失敗: " + integration_error, file=sys.stderr)
        return finish_failure(
            "gate2_failed", 13, mapping=mapping, integration=asdict(integration),
            gate1={"ok": True}, gate2={"ok": False, "error": integration_error},
            **partial_extra,
        )
    clusters = enrich_clusters(payload, metadata)
    questions = prepare_questions(payload, brief, metadata)
    costs = calculate_costs(result_history, integration_results, with_qwen=has_qwen)
    elapsed = time.monotonic() - started
    exit_code = PARTIAL_EXIT if partial else 0
    state = {
        **base_state,
        "status": "partial" if partial else "success",
        "exit_code": exit_code,
        "elapsed_seconds": elapsed,
        "mapping": mapping,
        "reviewers": reviewer_state(),
        "integration": asdict(integration),
        "gate1": {"ok": True},
        "gate2": {"ok": True},
        "no_findings": no_finding_names,
        "clusters": clusters,
        "costs": costs,
        **partial_extra,
    }
    write_json(run_dir / "questions.json", questions)
    write_json(run_dir / "run.json", state)
    report = build_report(
        run_dir.name, elapsed, clusters, metadata, payload, questions, costs, run_dir, args.no_ds,
        partial_missing=missing_records, redaction_count=len(redactions), no_finding_reviewers=no_finding_names,
        roster=roster, no_qwen=args.no_qwen and has_qwen, roster_name=args.roster,
    )
    (run_dir / "report.md").write_text(report, encoding="utf-8", newline="")
    print(report, end="")
    try:
        usage_logger()
    except Exception:
        pass
    return exit_code


def add_roster_options(parser: argparse.ArgumentParser) -> None:
    """plan と run に共通の、レビュアーの組 (roster) の指定。"""
    parser.add_argument(
        "--roster", choices=tuple(ROSTERS), default=DEFAULT_ROSTER,
        help="レビュアーの組。lv3=Codex/DeepSeek の技術×批評 4 者(既定) / lv7=Codex 多重+DeepSeek+Qwen の技術 4 者"
             " / lv8=lv7+批評 2 者(Codex high・DeepSeek)",
    )
    parser.add_argument(
        "--no-qwen", action="store_true",
        help="Qwen を使わない (--no-ds と同じ。明示的に外した者は「欠落」に数えない。Qwen のいない組では何もしない)",
    )
    parser.add_argument(
        "--qwen-timeout", type=int, default=300,
        help="Qwen 1 回あたりの待ち時間（秒。既定は DeepSeek と同じ。plan は何も呼ばないので、run と同じ引数列を渡せるよう受け付けるだけ）",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="cgd Lv3A レビュー・ドライバ")
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan", help="承認用の事前情報を表示")
    plan.add_argument("--brief", required=True)
    plan.add_argument("--files", nargs="*", default=[])
    plan.add_argument("--no-ds", action="store_true")
    add_roster_options(plan)
    plan.add_argument(
        "--redact", action="store_true",
        help="秘匿候補を伏字にした場合のプレビューを出す (要ユーザー承認。中身は表示しない)",
    )
    run = subparsers.add_parser("run", help="レビューと統合を実行")
    run.add_argument("--brief", required=True)
    run.add_argument("--files", nargs="*", default=[])
    run.add_argument("--label", default="lv3a")
    run.add_argument(
        "--effort", choices=("medium", "high"), default=None,
        help=f"Codex の推論強度 (既定 {DEFAULT_EFFORT})。--roster lv7/lv8 は組が固定するので併用できない (exit 1)",
    )
    run.add_argument("--no-ds", action="store_true")
    add_roster_options(run)
    run.add_argument(
        "--no-partial", action="store_true",
        help="一部のレビュアーが失敗したとき暫定版にせず、従来どおり停止する (exit 10/12)",
    )
    run.add_argument(
        "--redact", action="store_true",
        help="秘匿候補を伏字にして続行する (ユーザーの承認後にだけ付ける)",
    )
    run.add_argument("--work-root", default=str(DEFAULT_WORK_ROOT))
    run.add_argument("--codex-timeout", type=int, default=600)
    run.add_argument("--ds-timeout", type=int, default=300, help="DeepSeek 1 回あたりの待ち時間（秒）")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    reviewer_runner: ReviewerRunner = default_reviewer_runner,
    integrator_runner: IntegratorRunner = default_integrator_runner,
    usage_logger: Callable[[], None] = default_usage_logger,
    resolver: Callable[[], str | None] = resolve_codex,
    weekly: Callable[[], float | None] = get_weekly_percent,
) -> int:
    # stdout だけでなく stderr も UTF-8 にする。停止理由 (秘匿候補の行番号・--redact の案内など) は
    # stderr に出るので、コンソールのコードページ (cp932) のままだと Claude が文字化けして読めない
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if int(exc.code) == 0 else 1
    if args.command == "plan":
        return command_plan(args, resolver, weekly)
    if usage_logger is default_usage_logger and args.roster != DEFAULT_ROSTER:
        # 既定の記録器だけ、組の名前をメモに残す (差し替えられた記録器は、引数なしの契約のままそのまま使う)
        usage_logger = partial(default_usage_logger, f"lv3a roster={args.roster}")
    return command_run(
        args,
        reviewer_runner=reviewer_runner,
        integrator_runner=integrator_runner,
        usage_logger=usage_logger,
        resolver=resolver,
        weekly=weekly,
    )


if __name__ == "__main__":
    raise SystemExit(main())
