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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

TOOLS = Path(__file__).resolve().parent
DEFAULT_WORK_ROOT = Path("C:/tmp-ai/cgd_lv3a")
MAX_INPUT_BYTES = 200 * 1024
JSON_INSTRUCTION = (
    "【回答形式・必須】回答本文の最後に、次の形式の JSON ブロックを 1 つだけ付けること"
    "（```json で囲む）。本文で述べた指摘を 1 件 1 要素で漏れなく列挙する。"
    '{"findings":[{"id":"<P>1","severity":"<S>",'
    '"headline":"指摘の見出し(40字以内)"}]} id は <P>1, <P>2, … の連番。'
    "severity は <SLIST> のいずれか。"
)
TECH_PROMPT = (
    "これは設計レビューです。バグ・設計上の懸念・セキュリティ・副作用・既存仕様との"
    "整合性を厳密にレビューしてください。対象ファイルは開けないので、以下の内容だけで"
    "判断すること。断言できない点は根拠がないと明記すること。日本語で回答。"
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
SECRET_PATTERNS = (
    ("OpenAI API キー", re.compile(r"sk-[A-Za-z0-9]{10,}", re.IGNORECASE)),
    ("AWS アクセスキー", re.compile(r"AKIA[0-9A-Z]{12,}", re.IGNORECASE)),
    ("API キー", re.compile(r"api[_-]?key\s*[:=]", re.IGNORECASE)),
    ("password", re.compile(r"password\s*[:=]", re.IGNORECASE)),
    ("passwd", re.compile(r"passwd\s*[:=]", re.IGNORECASE)),
    ("Bearer token", re.compile(r"bearer\s+[A-Za-z0-9._-]{10,}", re.IGNORECASE)),
    (
        "メールアドレス",
        re.compile(
            r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.(com|jp|net)", re.IGNORECASE
        ),
    ),
    ("秘密鍵", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE)),
    ("JWT", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}", re.IGNORECASE)),
    ("資格情報付き接続文字列", re.compile(r"[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s@]+@", re.IGNORECASE)),
    ("AWS secret access key", re.compile(r"aws_secret_access_key\s*[:=]", re.IGNORECASE)),
    ("secret", re.compile(r"secret\s*[:=]\s*\S{8,}", re.IGNORECASE)),
    ("token", re.compile(r"token\s*[:=]\s*\S{16,}", re.IGNORECASE)),
)


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


@dataclass(frozen=True)
class ParsedReview:
    body: str
    findings: list[dict[str, str]]


REVIEWERS = (
    ReviewerSpec("codex_tech", "Codex", "technical", "CT", ("🔴", "🟠", "🟡"), "reviewer"),
    ReviewerSpec("codex_crit", "Codex", "critic", "CC", ("高", "中", "低"), "critic"),
    ReviewerSpec("ds_tech", "DeepSeek", "technical", "DT", ("🔴", "🟠", "🟡"), "reviewer"),
    ReviewerSpec("ds_crit", "DeepSeek", "critic", "DC", ("高", "中", "低"), "critic"),
)

ReviewerRunner = Callable[[ReviewerSpec, str, Path, int, str, Path, str], ExecResult]
IntegratorRunner = Callable[[str, Path, int, str], ExecResult]


class FrontError(ValueError):
    pass


def read_utf8(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise FrontError(f"読み込めません: {path}: {exc}") from exc


def secret_hits(label: str, text: str) -> list[str]:
    hits: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        for pattern_name, pattern in SECRET_PATTERNS:
            if pattern.search(line):
                hits.append(f"{label}: {pattern_name} / 行 {number}")
    return hits


def validate_inputs(brief_path: Path, files: Sequence[Path]) -> tuple[str, list[tuple[Path, str]], int]:
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
    if "## 確認済みの事実" not in brief:
        raise FrontError("依頼文に『## 確認済みの事実』の欄が要る")
    loaded: list[tuple[Path, str]] = []
    for path in files:
        text = read_utf8(path)
        loaded.append((path, text))
    hits = secret_hits(str(brief_path), brief)
    for path, text in loaded:
        hits.extend(secret_hits(str(path), text))
    if hits:
        raise FrontError("秘匿情報候補を検出しました（内容は非表示）:\n" + "\n".join(hits))
    return brief, loaded, total


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


def build_review_input(brief: str, loaded: Sequence[tuple[Path, str]]) -> str:
    parts = [brief.rstrip(), ""]
    for path, text in loaded:
        parts.extend((f"### {path}", f"```{language_for(path)}", text.rstrip(), "```", ""))
    return "\n".join(parts)


def json_instruction(spec: ReviewerSpec) -> str:
    severities = " / ".join(spec.severities)
    return JSON_INSTRUCTION.replace("<P>", spec.prefix).replace("<SLIST>", severities)


def reviewer_input(spec: ReviewerSpec, packed: str) -> str:
    suffix = json_instruction(spec)
    if spec.vendor == "DeepSeek":
        return f"{packed}\n\n{suffix}\n"
    role_prompt = TECH_PROMPT if spec.kind == "technical" else CRIT_PROMPT
    return f"{role_prompt}\n\n{packed}\n\n{suffix}\n"


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
# ものだけを通す (DeepSeek=DEEPSEEK_ / Codex=CODEX_)。Codex は認証を USERPROFILE 配下の
# ファイルで行うので OPENAI_API_KEY は不要 (渡さない)。
CHILD_ENV_ALLOWED = frozenset({
    "PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "USERPROFILE", "HOME",
    "TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "COMSPEC", "PATHEXT", "LANG",
    "PYTHONIOENCODING", "PYTHONUTF8", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "USERNAME", "HOMEDRIVE", "HOMEPATH", "OS", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS",
    "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "COMMONPROGRAMFILES",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
})


def child_env(secret_prefix: str) -> dict[str, str]:
    """許可リストの変数と、指定した接頭辞の変数だけを子プロセスへ渡す (大文字小文字は無視)。"""
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper().startswith(secret_prefix) or key.upper() in CHILD_ENV_ALLOWED
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
            f'model_reasoning_effort="{effort}"',
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "-",
        ]
        return _subprocess(command, stdin=prompt, cwd=cwd.parent, timeout=timeout, env=env)
    input_path = cwd / f"{spec.name}_input.txt"
    input_path.write_text(prompt, encoding="utf-8", newline="")
    command = [sys.executable, str(tools / "deepseek_coder.py"), "--role", spec.role, str(input_path)]
    env = child_env("DEEPSEEK_")
    return _subprocess(command, stdin=None, cwd=cwd.parent, timeout=timeout, env=env)


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
    if not isinstance(findings, list) or not findings:
        raise ValueError("findings が空または配列ではありません")
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


def attempt_record(spec: ReviewerSpec, result: ExecResult, gate_error: str | None) -> dict[str, Any]:
    return {
        "returncode": result.returncode,
        "gate_error": gate_error,
        "tokens": codex_tokens(result.stderr) if spec.vendor == "Codex" else 0,
        "yen": ds_yen(result.stderr) if spec.vendor == "DeepSeek" else 0.0,
    }


def calculate_costs(
    history: Sequence[tuple[ReviewerSpec, ExecResult]],
    integrations: Sequence[ExecResult],
) -> dict[str, Any]:
    return {
        "codex_calls": sum(spec.vendor == "Codex" for spec, _ in history) + len(integrations),
        "codex_tokens": sum(
            codex_tokens(result.stderr) for spec, result in history if spec.vendor == "Codex"
        ) + sum(codex_tokens(result.stderr) for result in integrations),
        "ds_calls": sum(spec.vendor == "DeepSeek" for spec, _ in history),
        "ds_yen": sum(ds_yen(result.stderr) for spec, result in history if spec.vendor == "DeepSeek"),
    }


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


INTEGRATOR_RULES = """あなたは複数のレビューを統合する議長。R1〜R4 の出所は伏せてある。どの AI かを推測して扱いを変えないこと。
与えられた指摘 ID（R<n>#<k>）すべてを、ちょうど 1 つのクラスタに割り当てる。似た指摘は 1 クラスタにまとめる。technical と critic は同じクラスタに混ぜない。クラスタ数の上限は設けない。
重大度と出所の数は書かない（機械が指摘 ID から算出する）。
出力は JSON のみ: {"summary":"総評(1〜3文)","clusters":[{"id":"C1","kind":"technical","title":"40字以内","members":["R1#1","R2#3"],"proposal":"対応案(1文)","adopt":"採用|部分採用|見送り","adopt_reason":"1文"}],"questions_for_user":[{"id":"Q1","question":"...","options":[{"label":"...","description":"..."}],"recommended":"labelのどれか","recommended_reason":"根拠。依頼文の『確認済みの事実』の語句か、R<n>#<k> を引くこと"}],"next_actions":["..."]}
questions_for_user はユーザーの方向性の判断が要るものだけ、最大 3 問、選択肢は 2〜4 個。依頼文に無い事実を推測で根拠にしない。"""


def integrator_input(sections: Sequence[dict[str, Any]]) -> str:
    parts = [INTEGRATOR_RULES, "", "レビュー入力:"]
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


def _omitted_label(items: Sequence[dict[str, Any]], limit: int = 160) -> str:
    """表から外したクラスタの題名を並べる。行数を増やさず、外した指摘の存在と中身を見えるようにする。"""
    titles = " / ".join(_safe_cell(item["title"]) for item in items)
    if len(titles) > limit:
        titles = titles[:limit] + "…"
    return f"他 {len(items)} 件（run.json 参照）: {titles}"


def _safe_cell(value: Any) -> str:
    return str(value).replace("|", "｜").replace("\r", " ").replace("\n", " ")


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
) -> str:
    ranks = {"🔴": 3, "🟠": 2, "🟡": 1, "高": 3, "中": 2, "低": 1}
    ordered = sorted(clusters, key=lambda item: (-ranks[item["severity"]], -len(item["vendors"])))
    technical = [item for item in ordered if item["kind"] == "technical"]
    critics = [item for item in ordered if item["kind"] == "critic"]
    shown_tech, shown_crit = list(technical), list(critics)

    def render(detail_limit: int | None = None, truncate_descriptions: bool = False) -> list[str]:
        lines = [f"# {run_name}", "", f"所要: {elapsed:.1f} 秒 / 状態: 成功" + (" / DS なし" if no_ds else ""), "",
                 "※ 採否・対応案は統合 AI の提案です。最終判断はユーザーが行います。", ""]
        lines.extend(("## 技術レビュー", "", "| 指摘 | 重大度 | Codex | DeepSeek | 採否 | 対応案 |", "|---|---|---|---|---|---|"))
        for item in shown_tech:
            title = item["title"] + (" (単独)" if item["single_source"] else "")
            lines.append(f"| {_safe_cell(title)} | {item['severity']} | {'✅' if 'Codex' in item['vendors'] else ''} | {'✅' if 'DeepSeek' in item['vendors'] else ''} | {_safe_cell(item.get('adopt', ''))} | {_safe_cell(item.get('proposal', ''))} |")
        omitted = len(technical) - len(shown_tech)
        if omitted:
            lines.append(f"| {_omitted_label([i for i in technical if i not in shown_tech])} | | | | | |")
        lines.extend(("", "## 批評レビュー", "", "| 観点 | 困り度 | Codex | DeepSeek | 採否 | 改善の方向 |", "|---|---|---|---|---|---|"))
        for item in shown_crit:
            title = item["title"] + (" (単独)" if item["single_source"] else "")
            lines.append(f"| {_safe_cell(title)} | {item['severity']} | {'✅' if 'Codex' in item['vendors'] else ''} | {'✅' if 'DeepSeek' in item['vendors'] else ''} | {_safe_cell(item.get('adopt', ''))} | {_safe_cell(item.get('proposal', ''))} |")
        omitted = len(critics) - len(shown_crit)
        if omitted:
            lines.append(f"| {_omitted_label([i for i in critics if i not in shown_crit])} | | | | | |")
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
        lines.extend(("", "## 費用", "", f"Codex: {costs['codex_calls']} 回 / {costs['codex_tokens']:,} tokens、DeepSeek: {costs['ds_calls']} 回 / ¥{costs['ds_yen']:.3f}", "", "## 生ログ", "", str(run_dir)))
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
        lines.insert(4, "⚠ 行数超過: 必須節と全 🔴 クラスタを保持するため 90 行を超えています。")
    return "\n".join(lines) + "\n"


def create_run_dir(work_root: Path, label: str, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    suffix = f"{random.SystemRandom().randrange(0x1000000):06x}"
    run_dir = work_root / f"{label}_{stamp}_{suffix}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="")


def default_usage_logger() -> None:
    subprocess.run(
        [sys.executable, str(TOOLS / "cgd_usage_log.py"), "record", "--level", "3", "--note", "lv3a"],
        capture_output=True,
        check=False,
    )


def command_plan(args: argparse.Namespace, resolver: Callable[[], str | None], weekly: Callable[[], float | None]) -> int:
    try:
        brief, loaded, total = validate_inputs(Path(args.brief), [Path(item) for item in args.files])
    except FrontError as exc:
        print(str(exc), file=sys.stderr)
        return 1
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
    print(f"Codex: {codex_path}")
    print("週枠: 不明（取得失敗）" if weekly_percent is None else f"週枠: {weekly_percent:.1f}%")
    targets = "Codex（OpenAI）" + ("" if args.no_ds else "、DeepSeek（中国本土サーバ）")
    calls = "Codex 3 回（技術・批評・統合）" + ("" if args.no_ds else "、DS 2 回")
    print(f"送信先: {targets}")
    print(f"呼出予定: {calls} / Codex 想定 tokens: 約 6 万")
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
    try:
        brief, loaded, _ = validate_inputs(Path(args.brief), [Path(item) for item in args.files])
    except FrontError as exc:
        print(str(exc), file=sys.stderr)
        return 1
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
    if args.codex_timeout <= 0 or args.ds_timeout <= 0:
        print("timeout は正の整数で指定してください", file=sys.stderr)
        return 1
    try:
        run_dir = create_run_dir(work_root, args.label)
    except OSError as exc:
        print(f"run ディレクトリを作成できません: {exc}", file=sys.stderr)
        return 1
    packed = build_review_input(brief, loaded)
    (run_dir / "review_input.txt").write_text(packed, encoding="utf-8", newline="")
    specs = [item for item in REVIEWERS if not (args.no_ds and item.vendor == "DeepSeek")]
    prompts = {spec.name: reviewer_input(spec, packed) for spec in specs}
    results: dict[str, ExecResult] = {}
    result_history: list[tuple[ReviewerSpec, ExecResult]] = []
    attempts: dict[str, list[dict[str, Any]]] = {spec.name: [] for spec in specs}
    timeouts = {"Codex": args.codex_timeout, "DeepSeek": args.ds_timeout}
    with ThreadPoolExecutor(max_workers=len(specs)) as executor:
        futures = {
            executor.submit(
                reviewer_runner, spec, prompts[spec.name], run_dir,
                timeouts[spec.vendor], codex_path, TOOLS, args.effort,
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
    failed = [spec.name for spec in specs if results[spec.name].returncode != 0]
    base_state: dict[str, Any] = {
        "run_name": run_dir.name,
        "weekly_percent": weekly_percent,
        "no_ds": args.no_ds,
    }

    def reviewer_state() -> dict[str, Any]:
        return {
            name: {**asdict(result), "attempts": attempts[name]}
            for name, result in results.items()
        }

    def finish_failure(status: str, exit_code: int, **extra: Any) -> int:
        base_state.update(
            {
                "status": status,
                "exit_code": exit_code,
                "elapsed_seconds": time.monotonic() - started,
                "reviewers": reviewer_state(),
                "costs": calculate_costs(result_history, integration_results),
                **extra,
            }
        )
        write_json(run_dir / "run.json", base_state)
        return exit_code

    integration_results: list[ExecResult] = []
    if failed:
        print("レビュアー実行失敗: " + ", ".join(failed), file=sys.stderr)
        return finish_failure(
            "reviewer_execution_failed", 10, gate1={"ok": False, "failed": failed}
        )
    parsed: dict[str, ParsedReview] = {}
    gate_errors: dict[str, str] = {}
    for spec in specs:
        try:
            parsed[spec.name] = parse_review(results[spec.name].stdout, spec)
        except ValueError as exc:
            gate_errors[spec.name] = str(exc)
            attempts[spec.name][0]["gate_error"] = str(exc)
    retry_specs = [spec for spec in specs if spec.name in gate_errors]
    retry_results: dict[str, ExecResult] = {}
    if retry_specs:
        with ThreadPoolExecutor(max_workers=len(retry_specs)) as executor:
            futures = {
                executor.submit(
                    reviewer_runner, spec, prompts[spec.name] + "\n" + RETRY_NOTE + "\n",
                    run_dir, timeouts[spec.vendor], codex_path, TOOLS, args.effort,
                ): spec
                for spec in retry_specs
            }
            for future in as_completed(futures):
                spec = futures[future]
                try:
                    retry_results[spec.name] = future.result()
                except Exception as exc:
                    retry_results[spec.name] = ExecResult(127, "", str(exc))
    retry_failed: list[str] = []
    for spec in retry_specs:
        retry = retry_results[spec.name]
        results[spec.name] = retry
        result_history.append((spec, retry))
        save_exec(run_dir, spec.name, retry, 1)
        gate_error: str | None = None
        if retry.returncode != 0:
            retry_failed.append(spec.name)
        else:
            try:
                parsed[spec.name] = parse_review(retry.stdout, spec)
                gate_errors.pop(spec.name, None)
            except ValueError as exc:
                gate_error = str(exc)
                gate_errors[spec.name] = gate_error
        attempts[spec.name].append(attempt_record(spec, retry, gate_error))
    if retry_failed:
        print("レビュアー再実行失敗: " + ", ".join(retry_failed), file=sys.stderr)
        return finish_failure(
            "reviewer_execution_failed", 10,
            gate1={"ok": False, "failed": retry_failed},
        )
    if gate_errors:
        for name, error in gate_errors.items():
            print(f"{name}: {error}", file=sys.stderr)
        return finish_failure("gate1_failed", 12, gate1={"ok": False, "errors": gate_errors})
    # 再実行があった者は、採用した (=見出しの出所の) 再実行のログを指す
    log_files = {
        spec.name: (f"{spec.name}.retry1" if len(attempts[spec.name]) > 1 else spec.name)
        for spec in specs
    }
    sections, mapping, metadata = anonymize(parsed, specs, run_dir.name, log_files)
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
        )
    clusters = enrich_clusters(payload, metadata)
    questions = prepare_questions(payload, brief, metadata)
    costs = calculate_costs(result_history, integration_results)
    elapsed = time.monotonic() - started
    state = {
        **base_state,
        "status": "success",
        "exit_code": 0,
        "elapsed_seconds": elapsed,
        "mapping": mapping,
        "reviewers": reviewer_state(),
        "integration": asdict(integration),
        "gate1": {"ok": True},
        "gate2": {"ok": True},
        "clusters": clusters,
        "costs": costs,
    }
    write_json(run_dir / "questions.json", questions)
    write_json(run_dir / "run.json", state)
    report = build_report(run_dir.name, elapsed, clusters, metadata, payload, questions, costs, run_dir, args.no_ds)
    (run_dir / "report.md").write_text(report, encoding="utf-8", newline="")
    print(report, end="")
    try:
        usage_logger()
    except Exception:
        pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="cgd Lv3A レビュー・ドライバ")
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan", help="承認用の事前情報を表示")
    plan.add_argument("--brief", required=True)
    plan.add_argument("--files", nargs="*", default=[])
    plan.add_argument("--no-ds", action="store_true")
    run = subparsers.add_parser("run", help="レビューと統合を実行")
    run.add_argument("--brief", required=True)
    run.add_argument("--files", nargs="*", default=[])
    run.add_argument("--label", default="lv3a")
    run.add_argument("--effort", choices=("medium", "high"), default="medium")
    run.add_argument("--no-ds", action="store_true")
    run.add_argument("--work-root", default=str(DEFAULT_WORK_ROOT))
    run.add_argument("--codex-timeout", type=int, default=600)
    run.add_argument("--ds-timeout", type=int, default=300)
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
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if int(exc.code) == 0 else 1
    if args.command == "plan":
        return command_plan(args, resolver, weekly)
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
