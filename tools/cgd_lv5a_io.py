"""cgd Lv5A の下請け入出力: CLI の起動と、その出力ファイルの読み取り。

呼ぶのは CLI (cgd_lv0_auto.py / cgd_lv3a.py) だけ。他ドライバの内部関数は import しない
(test_no_driver_imports が守る)。出力ファイルの形は実物 (Lv0: report.md / outcome.json / overall.patch、
Lv3A: run.json / questions.json / レビュアーの生ログ) から起こした。読めないときは理由を残す (黙って捨てない)。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

TOOLS = Path(__file__).resolve().parent
LV0_SCRIPT, LV3A_SCRIPT = TOOLS / "cgd_lv0_auto.py", TOOLS / "cgd_lv3a.py"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 終了コード: 0 成功 / 1 前段・引数・状態違反 / 2 Codex なし / 3 Codex 失敗・変更なし / 10 検査が通らない・停滞
# 11 週枠 / 12 レビュー失敗で consult 不成立 / 20 回答待ち / 21 完了したが要ユーザー判断 / 30 タイムアウト
EXIT_OK, EXIT_GENERIC, EXIT_CODEX_FAILED, EXIT_REVIEW_FAILED = 0, 1, 3, 12
EXIT_AWAITING, EXIT_NEEDS_JUDGMENT, EXIT_TIMEOUT = 20, 21, 30
LV0_PASS, LV3A_PASS = (0, 1, 2, 3, 10, 11, 30), (1, 2, 11, 30)
LV3A_OK = (0, 20)  # 20 = 暫定成功 (警告つきで成功扱い)

# 子プロセスへ渡す環境変数の許可リスト (鍵・トークン類は通さない。接頭辞は下請けが必要とするものだけ)
CHILD_ENV_ALLOWED = frozenset({
    "PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "USERPROFILE", "HOME", "TEMP", "TMP", "APPDATA",
    "LOCALAPPDATA", "COMSPEC", "PATHEXT", "LANG", "PYTHONIOENCODING", "PYTHONUTF8", "HTTP_PROXY",
    "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "USERNAME", "HOMEDRIVE", "HOMEPATH", "OS", "PROGRAMDATA",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "COMMONPROGRAMFILES", "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
})
CHILD_ENV_PREFIXES = ("DEEPSEEK_", "CODEX_", "CGD_")

RUN_LINE_RE = re.compile(r"基準の RUN:\s*(\S+)（記録:\s*(.+?)）")
CHECKS_RE = re.compile(r"^最終の検査:\s*(.+)$", re.MULTILINE)
CHANGE_RE = re.compile(r"変更: 更新 (\d+) / 新規 (\d+) / 削除 (\d+)\s+\+(\d+) -(\d+) 行")
NAMES_RE = re.compile(r"^[ \t]+((?:modified|added|deleted):.+)$", re.MULTILINE)
REVIEW_RE = re.compile(r"^DeepSeek レビュー:\s*(.+)$", re.MULTILINE)
FROZEN_RE = re.compile(r"^凍結ファイルの違反（写しから戻した）:\s*(.+)$", re.MULTILINE)
NOTE_RE = re.compile(r"^Codex の最終報告（抜粋）:\s*\n```\n(.*?)\n```", re.MULTILINE | re.DOTALL)
JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class CliResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    elapsed: float = 0.0


Runner = Callable[[Sequence[str], int], CliResult]


@dataclass(frozen=True)
class Drivers:
    """下請け 2 本の呼び出し口 (引数は CLI の引数列と秒数)。テストは偽の実行器を注入する。"""

    lv0: Runner
    lv3a: Runner


def read_child_json(path: Path, notes: list[str], what: str) -> Any | None:
    """下請けの出力ファイルを読む。読めなければ理由を notes に残して None (黙って捨てない)。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        notes.append(f"{what} を読めない: {path.name}（{exc}）")
        return None


def child_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """許可リストの変数と許可した接頭辞の変数だけを子プロセスへ渡す (大文字小文字は無視)。"""
    env = {k: v for k, v in (os.environ if source is None else source).items()
           if k.upper() in CHILD_ENV_ALLOWED or k.upper().startswith(CHILD_ENV_PREFIXES)}
    return {**env, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}


def run_cli(script: Path, args: Sequence[str], timeout: int) -> CliResult:
    """下請けの CLI を別プロセスで回す。打ち切りはプロセスツリーごと止めて exit 30。"""
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            [sys.executable, str(script), *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=str(TOOLS), env=child_env(), creationflags=NO_WINDOW)
    except OSError as exc:
        return CliResult(EXIT_GENERIC, "", f"起動できない: {exc}")
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
        proc.kill()
        try:
            out, err = proc.communicate(timeout=10)
        except (subprocess.SubprocessError, OSError, ValueError):
            out, err = b"", b""
    text_out, text_err = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    if timed_out:
        text_err += f"\n[{timeout} 秒で打ち切った]"
    return CliResult(EXIT_TIMEOUT if timed_out else proc.returncode, text_out, text_err, time.monotonic() - started)


def default_drivers() -> Drivers:
    return Drivers(lambda a, t: run_cli(LV0_SCRIPT, a, t), lambda a, t: run_cli(LV3A_SCRIPT, a, t))


def map_lv0_exit(code: int) -> int:
    return code if code in LV0_PASS else EXIT_GENERIC


def map_lv3a_exit(code: int) -> int:
    if code in LV3A_OK:
        return EXIT_OK
    return EXIT_REVIEW_FAILED if code in (10, 12, 13) else code if code in LV3A_PASS else EXIT_GENERIC


# ---------------------------------------------------------------- Lv0 (cgd_lv0_auto.py) の出力


@dataclass
class Lv0Run:
    exit_code: int
    autodir: Path | None = None
    base_run: str = ""
    status: str = ""
    note: str = ""
    rounds: list[dict[str, Any]] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    counts: tuple[int, int, int] = (0, 0, 0)
    plus: int = 0
    minus: int = 0
    checks: dict[str, str] = field(default_factory=dict)
    review_line: str = ""
    frozen_violation: str = ""
    codex_note: str = ""
    patch: Path | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return sum(int(r.get("tokens") or 0) for r in self.rounds)

    @property
    def seconds(self) -> int:
        return sum(int(r.get("seconds") or 0) for r in self.rounds)


def parse_lv0(result: CliResult) -> Lv0Run:
    """Lv0 の stdout (= report.md) と、記録先の outcome.json / overall.patch を読む。"""
    run, report = Lv0Run(result.returncode), result.stdout
    if found := RUN_LINE_RE.search(report):
        run.base_run, run.autodir = found.group(1), Path(found.group(2).strip())
        outcome = read_child_json(run.autodir / "outcome.json", run.warnings, "Lv0 outcome.json")
        if isinstance(outcome, dict):
            run.status, run.note = str(outcome.get("status", "")), str(outcome.get("note", ""))
            run.rounds = [r for r in outcome.get("rounds", []) if isinstance(r, dict)]
        try:
            report = (run.autodir / "report.md").read_text(encoding="utf-8")
        except OSError as exc:
            run.warnings.append(f"Lv0 report.md を読めない（stdout で代用）: {exc}")
        patch = run.autodir / "overall.patch"
        run.patch = patch if patch.is_file() and patch.stat().st_size > 0 else None
    elif result.returncode == 0:
        run.warnings.append("Lv0 の記録先（基準の RUN 行）を特定できない")
    if m := CHANGE_RE.search(report):
        run.counts, run.plus, run.minus = (int(m[1]), int(m[2]), int(m[3])), int(m[4]), int(m[5])
    if m := NAMES_RE.search(report):
        names = re.sub(r"\s*…ほか \d+ 件$", "", m[1])
        run.changed = [p.split(":", 1)[1].strip() for p in names.split(", ") if ":" in p]
    if m := CHECKS_RE.search(report):
        run.checks = dict(p.strip().split("=", 1) for p in m[1].split(" / ") if "=" in p)
    run.review_line = m[1].strip() if (m := REVIEW_RE.search(report)) else ""
    run.frozen_violation = "" if not (m := FROZEN_RE.search(report)) or m[1].strip() == "なし" else m[1].strip()
    run.codex_note = m[1].strip() if (m := NOTE_RE.search(report)) else ""
    return run


def lv0_cost(run: Lv0Run) -> dict[str, Any]:
    # Lv0 の DeepSeek レビューは費用を記録しない (stderr を捨てる)。呼んだ事実だけ数える
    return {"codex_calls": len(run.rounds), "codex_tokens": run.tokens, "ds_calls": 0, "ds_yen": 0.0,
            "ds_yen_unknown_calls": 1 if run.review_line else 0}


# ---------------------------------------------------------------- Lv3A (cgd_lv3a.py) の出力


@dataclass
class Lv3aRun:
    exit_code: int
    stderr: str = ""
    run_dir: Path | None = None
    clusters: list[dict[str, Any]] = field(default_factory=list)
    questions: list[dict[str, Any]] = field(default_factory=list)
    costs: dict[str, Any] = field(default_factory=dict)
    mapping: dict[str, str] = field(default_factory=dict)
    retried: set[str] = field(default_factory=set)
    partial_missing: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    cache: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code in LV3A_OK and self.run_dir is not None

    @property
    def partial_note(self) -> str:
        """exit 20 (暫定成功) の説明。欠けたレビュアーは run.json の partial.missing (名前と理由) から。"""
        if self.exit_code != 20:
            return ""
        missing = ", ".join(f"{m.get('name')}（{m.get('reason')}）" for m in self.partial_missing)
        return "Lv3A は暫定成功（exit 20）: 欠落 " + (missing or "不明（run.json に partial が無い）") + "。収束の判定が弱い"

    def finding(self, member: str) -> tuple[str, str]:
        """指摘 ID (R2#7) の (出所ベンダー, 見出し)。レビュアーの生ログの JSON から引く。引けなければ見出しは空。"""
        alias, _, number = member.partition("#")
        name = self.mapping.get(alias, "")
        vendor = "Codex" if name.startswith("codex") else "DeepSeek" if name.startswith("ds") else name
        if self.run_dir is None or not name or not number.isdigit():
            return vendor, ""
        if name not in self.cache:
            log = self.run_dir / f"{name}{'.retry1' if name in self.retried else ''}.md"
            try:
                data = json.loads(JSON_BLOCK_RE.findall(log.read_text(encoding="utf-8"))[-1])
                items = data.get("findings") if isinstance(data, dict) else None
            except (OSError, ValueError, IndexError):
                items = None
            self.cache[name] = [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
        items, index = self.cache[name], int(number) - 1
        return vendor, str(items[index].get("headline", "")) if 0 <= index < len(items) else ""


def parse_lv3a(result: CliResult, work_root: Path) -> Lv3aRun:
    """work-root 配下の唯一のサブフォルダから run.json / questions.json を読む (失敗した実行の費用も拾う)。"""
    run = Lv3aRun(result.returncode, result.stderr)
    subdirs = [p for p in work_root.iterdir() if p.is_dir()] if work_root.is_dir() else []
    if len(subdirs) == 1:
        run.run_dir = subdirs[0]
    elif subdirs:
        run.warnings.append(f"Lv3A の run ディレクトリが {len(subdirs)} 個ある（唯一でない）")
    if run.run_dir is None:
        return run
    data = read_child_json(run.run_dir / "run.json", run.warnings, "Lv3A run.json")
    if isinstance(data, dict):
        run.clusters = [c for c in data.get("clusters", []) if isinstance(c, dict)]
        run.costs = data["costs"] if isinstance(data.get("costs"), dict) else {}
        run.mapping = {str(k): str(v) for k, v in (data.get("mapping") or {}).items()}
        reviewers = data["reviewers"] if isinstance(data.get("reviewers"), dict) else {}
        run.retried = {n for n, v in reviewers.items() if isinstance(v, dict) and len(v.get("attempts", [])) > 1}
        partial = data.get("partial")
        missing = partial.get("missing") if isinstance(partial, dict) else None
        run.partial_missing = [m for m in missing if isinstance(m, dict)] if isinstance(missing, list) else []
    if result.returncode in LV3A_OK:
        questions = read_child_json(run.run_dir / "questions.json", run.warnings, "Lv3A questions.json")
        run.questions = [q for q in questions if isinstance(q, dict)] if isinstance(questions, list) else []
    return run


def lv3a_cost(run: Lv3aRun) -> dict[str, Any]:
    cost: dict[str, Any] = {k: int(run.costs.get(k) or 0) for k in ("codex_calls", "codex_tokens", "ds_calls")}
    return {**cost, "ds_yen": float(run.costs.get("ds_yen") or 0.0)}


def total_costs(stages: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """失敗した工程も含めて合算する (どの工程も state に残るため)。"""
    total: dict[str, Any] = {"codex_calls": 0, "codex_tokens": 0, "ds_calls": 0, "ds_yen": 0.0, "ds_yen_unknown_calls": 0}
    for stage in stages:
        for key in total:
            total[key] += (stage.get("cost") or {}).get(key, 0)
    return total


def cost_line(costs: Mapping[str, Any]) -> str:
    unknown = costs.get("ds_yen_unknown_calls", 0)
    return (f"Codex: {costs['codex_calls']} 回 / {costs['codex_tokens']:,} tokens、DeepSeek: {costs['ds_calls']} 回 / "
            f"¥{costs['ds_yen']:.3f}" + (f"（ほか Lv0 の DeepSeek レビュー {unknown} 回は費用不明）" if unknown else ""))
