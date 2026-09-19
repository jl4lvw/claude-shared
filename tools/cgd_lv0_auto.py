"""cgd Lv0 の自動実行ドライバ。

Lv0 の機械的な部分（準備 → Codex 実行 → 機械検査 → 失敗なら Codex へ自動で出し直し → 短い報告）を
1 コマンドで任せる。Claude Code は仕様を書き、承認を取り、最後にレポートを読むだけになる。

サブコマンド:
  plan   承認表に載せる情報（作業フォルダの点検・秘密情報らしいファイル・検査コマンド・利用枠）を出す。何も書かない
  run    本体。Codex → 検査 → 失敗なら Codex へ出し直し（最大 --max-fix-rounds 周）→ レポート
  check  今の作業フォルダに検査だけを回す（Claude が手で直したあとの再検査用）

方針:
  - 検査はこのスクリプト（機械）が回す。Codex のサンドボックスは Python / Node を起動できないため
  - 凍結ファイル（テスト等）は出し直しの周では変更・削除させない（テストを書き換えて通す不正を防ぐ）
  - 検査には API キー等らしい名前の環境変数を渡さない
  - 出し直しは最大 N 周。利用枠（週）が --quota-stop % 以上なら止める。失敗の中身が変わらなければ止める
  - 下請けの cgd_lv0_codex.py（prepare / run / diff）は別プロセスで呼び、そのガードと終了コードをそのまま使う
"""

from __future__ import annotations

import argparse
import contextlib
import fnmatch
import glob
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import cgd_lv0_codex as engine  # noqa: E402

EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_NO_CODEX = 2
EXIT_CODEX_FAILED = 3
EXIT_CHECKS_FAILED = 10  # 検査が通らないまま上限に達した・失敗が変わらず停滞した
EXIT_QUOTA = 11  # 利用枠が上限に達したため止めた
EXIT_TIMEOUT = 30

STATUS_EXIT = {
    "ok": EXIT_OK,
    "checks_failed": EXIT_CHECKS_FAILED,
    "stalled": EXIT_CHECKS_FAILED,
    "quota_stop": EXIT_QUOTA,
    "codex_failed": EXIT_CODEX_FAILED,
    "no_change": EXIT_CODEX_FAILED,
    "engine_error": EXIT_GENERIC,
}

STATIC_TYPES = frozenset({"js-syntax", "lf", "ruff"})
DYNAMIC_TYPES = frozenset({"cmd", "e2e"})
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
PLACEHOLDER_RE = re.compile(r"\{(port|base_url|tmp|shots|workdir|python)\}")
SENSITIVE_ENV_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.IGNORECASE)
PYTHON_NAMES = frozenset({"python", "python3", "py"})
LF_EXTENSIONS = frozenset({".js", ".mjs", ".py", ".css", ".html", ".json", ".md", ".txt", ".svg"})
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CONTRACT_PATH = TOOLS.parent / "skills" / "lv0" / "e2e_contract.md"
REVIEW_MAX_BYTES = 300_000
DURATION_RE = re.compile(r"\b\d+(\.\d+)?\s?(ms|s|sec|seconds|秒)\b")


# ---------------------------------------------------------------- 検査の定義（manifest）


@dataclass
class Check:
    name: str
    type: str
    cmd: list[str] = field(default_factory=list)
    cwd: str = "."
    timeout: int = 180
    blocking: bool = False
    needs_serve: bool = False


@dataclass
class Serve:
    cmd: list[str]
    cwd: str = "."
    ready_path: str = "/"
    ready_timeout: int = 30


@dataclass
class Manifest:
    checks: list[Check]
    frozen: list[str] = field(default_factory=list)
    serve: Serve | None = None


def _str_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
        raise ValueError(f"{label} は空でない文字列の配列にしてください")
    return list(value)


def parse_manifest(data: object) -> Manifest:
    if not isinstance(data, dict):
        raise ValueError("検査定義はオブジェクトにしてください")
    unknown = set(data) - {"checks", "frozen", "serve", "note"}
    if unknown:
        raise ValueError(f"検査定義に未知のキー: {sorted(unknown)}")
    raw_checks = data.get("checks")
    if not isinstance(raw_checks, list) or not raw_checks:
        raise ValueError("checks は 1 件以上の配列にしてください")
    checks: list[Check] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_checks, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"checks[{i}] はオブジェクトにしてください")
        name, ctype = raw.get("name"), raw.get("type")
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise ValueError(f"checks[{i}].name は英数字・_ . - の 40 文字以内にしてください")
        if name in seen:
            raise ValueError(f"checks の name が重複: {name}")
        seen.add(name)
        if ctype not in STATIC_TYPES | DYNAMIC_TYPES:
            raise ValueError(f"{name}: type は {sorted(STATIC_TYPES | DYNAMIC_TYPES)} のどれか")
        cmd = _str_list(raw.get("cmd"), f"{name}.cmd") if ctype in DYNAMIC_TYPES else []
        timeout = raw.get("timeout", 180)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 1800:
            raise ValueError(f"{name}.timeout は 1〜1800 の整数（秒）")
        cwd = raw.get("cwd", ".")
        if not isinstance(cwd, str) or Path(cwd).is_absolute() or ".." in Path(cwd).parts:
            raise ValueError(f"{name}.cwd は作業フォルダ内の相対パスにしてください")
        blocking = raw.get("blocking", ctype in {"js-syntax", "ruff"})
        needs_serve = raw.get("needs_serve", ctype == "e2e")
        if not isinstance(blocking, bool) or not isinstance(needs_serve, bool):
            raise ValueError(f"{name}: blocking / needs_serve は true か false")
        checks.append(Check(name, ctype, cmd, cwd, timeout, blocking, needs_serve))
    frozen = data.get("frozen", [])
    if not isinstance(frozen, list) or not all(isinstance(p, str) and p for p in frozen):
        raise ValueError("frozen は文字列の配列にしてください（例: tests/**）")
    serve = None
    if "serve" in data:
        raw_serve = data["serve"]
        if not isinstance(raw_serve, dict):
            raise ValueError("serve はオブジェクトにしてください")
        cwd = raw_serve.get("cwd", ".")
        if not isinstance(cwd, str) or Path(cwd).is_absolute() or ".." in Path(cwd).parts:
            raise ValueError("serve.cwd は作業フォルダ内の相対パスにしてください")
        ready_timeout = raw_serve.get("ready_timeout", 30)
        if not isinstance(ready_timeout, int) or not 1 <= ready_timeout <= 300:
            raise ValueError("serve.ready_timeout は 1〜300 の整数（秒）")
        serve = Serve(
            _str_list(raw_serve.get("cmd"), "serve.cmd"), cwd,
            str(raw_serve.get("ready_path", "/")), ready_timeout,
        )
    if serve is None and any(c.needs_serve for c in checks):
        raise ValueError("e2e / needs_serve の検査には serve（アプリの起動コマンド）が要る")
    return Manifest(checks, list(frozen), serve)


def load_manifest(path: Path) -> Manifest:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"検査定義を読めない: {path}（{exc}）") from exc
    return parse_manifest(data)


# ---------------------------------------------------------------- 小さな道具


def subst(text: str, ctx: dict[str, str]) -> str:
    """{port} など既知の名前だけを置き換える（他の波括弧には触らない）。"""
    return PLACEHOLDER_RE.sub(lambda m: ctx.get(m.group(1), m.group(0)), text)


def expand_cmd(cmd: list[str], ctx: dict[str, str], cwd: Path) -> list[str]:
    """置き換え・python/node の解決・ワイルドカードの展開（シェルを使わないため）。"""
    out = [subst(a, ctx) for a in cmd]
    head = out[0].lower()
    if head in PYTHON_NAMES:
        out[0] = sys.executable
    elif head == "node":
        out[0] = shutil.which("node") or "node"
    expanded = [out[0]]
    for arg in out[1:]:
        if any(ch in arg for ch in "*?["):
            hits = sorted(glob.glob(arg, root_dir=str(cwd)))
            expanded.extend(hits if hits else [arg])
        else:
            expanded.append(arg)
    return expanded


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """検査に渡す環境変数。鍵・トークン・パスワードらしい名前は渡さない。"""
    env = {k: v for k, v in os.environ.items() if not SENSITIVE_ENV_RE.search(k)}
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    env.update(extra or {})
    return env


def tail(text: str, lines: int = 15, chars: int = 1500) -> str:
    kept = text.strip().splitlines()[-lines:]
    joined = "\n".join(kept)
    return joined[-chars:] if len(joined) > chars else joined


def matches_frozen(rel: str, patterns: list[str]) -> bool:
    """rel（/ 区切り）が凍結パターンに当たるか。* は / も含めて一致する（tests/** は tests 配下すべて）。"""
    return any(fnmatch.fnmatch(rel, p) for p in patterns)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def normalize_failure(text: str) -> str:
    return DURATION_RE.sub("T", text)


def within(root: Path, rel: str) -> Path:
    target = (root / rel).resolve()
    if target != root.resolve() and root.resolve() not in target.parents:
        raise ValueError(f"作業フォルダの外を指している: {rel}")
    return target


# ---------------------------------------------------------------- 検査の実行


@dataclass
class CheckResult:
    name: str
    type: str
    status: str  # ok / fail / skip
    seconds: float = 0.0
    excerpt: str = ""
    detail: str = ""


def run_command(cmd: list[str], cwd: Path, env: dict[str, str], timeout: int) -> tuple[int | None, float, str]:
    """コマンドを 1 つ回す。打ち切り時はプロセスツリーごと止める。戻りは (終了コード | None, 秒, 出力)。"""
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, creationflags=NO_WINDOW,
        )
    except OSError as exc:
        return None, 0.0, f"起動できない: {exc}"
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, time.monotonic() - started, out.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        engine._kill_tree(proc)
        try:
            out, _ = proc.communicate(timeout=5)
        except (subprocess.SubprocessError, OSError, ValueError):
            out = b""
        text = out.decode("utf-8", errors="replace") + f"\n[{timeout} 秒で打ち切った]"
        return None, time.monotonic() - started, text


class ServeError(RuntimeError):
    pass


class ServeHandle:
    """検査用にアプリを起動しておく（with で使う）。ポートは空きを選び、終了時にプロセスツリーごと止める。"""

    def __init__(self, serve: Serve, workdir: Path, ctx: dict[str, str], log: Path) -> None:
        self.serve, self.workdir, self.ctx, self.log = serve, workdir, ctx, log
        self.proc: subprocess.Popen | None = None

    def __enter__(self) -> "ServeHandle":
        self.ctx["port"] = str(free_port())
        self.ctx["base_url"] = f"http://127.0.0.1:{self.ctx['port']}"
        cwd = within(self.workdir, self.serve.cwd)
        cmd = expand_cmd(self.serve.cmd, self.ctx, cwd)
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with self.log.open("wb") as f:
            try:
                self.proc = subprocess.Popen(
                    cmd, cwd=str(cwd), env=clean_env(), stdout=f, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, creationflags=NO_WINDOW,
                )
            except OSError as exc:
                raise ServeError(f"アプリを起動できない: {exc}") from exc
        url = self.ctx["base_url"] + self.serve.ready_path
        deadline = time.monotonic() + self.serve.ready_timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise ServeError(f"アプリが終了した（exit {self.proc.returncode}）:\n{self._log_tail()}")
            try:
                urllib.request.urlopen(url, timeout=2).close()
                return self
            except urllib.error.HTTPError as exc:
                if exc.code < 500:
                    return self
            except OSError:
                pass
            time.sleep(0.3)
        self.__exit__(None, None, None)
        raise ServeError(f"{self.serve.ready_timeout} 秒待ってもアプリが応答しない（{url}）:\n{self._log_tail()}")

    def _log_tail(self) -> str:
        try:
            return tail(self.log.read_text(encoding="utf-8", errors="replace"), 12)
        except OSError:
            return ""

    def __exit__(self, *exc: object) -> None:
        if self.proc is not None and self.proc.poll() is None:
            engine._kill_tree(self.proc)


def _files_with(changed: list[str] | None, workdir: Path, suffixes: frozenset[str]) -> list[str]:
    if changed is None:
        changed = [p.as_posix() for p in engine.iter_files(workdir)]
    return [c for c in changed if Path(c).suffix.lower() in suffixes and (workdir / c).is_file()]


def _builtin_check(check: Check, workdir: Path, changed: list[str] | None, env: dict[str, str]) -> CheckResult:
    if check.type == "js-syntax":
        files = _files_with(changed, workdir, frozenset({".js", ".mjs"}))
        runner = shutil.which("node")
        if not files:
            return CheckResult(check.name, check.type, "skip", detail="対象の .js/.mjs なし")
        if runner is None:
            return CheckResult(check.name, check.type, "fail", excerpt="node が見つからない")
        cmds = [[runner, "--check", f] for f in files]
    elif check.type == "lf":
        files = _files_with(changed, workdir, LF_EXTENSIONS)
        if not files:
            return CheckResult(check.name, check.type, "skip", detail="対象のテキストファイルなし")
        cmds = [[sys.executable, str(TOOLS / "ensure_lf.py"), "--check", *files[i:i + 40]]
                for i in range(0, len(files), 40)]
    else:  # ruff
        files = _files_with(changed, workdir, frozenset({".py"}))
        if not files:
            return CheckResult(check.name, check.type, "skip", detail="対象の .py なし")
        exe = shutil.which("ruff")
        base = [exe] if exe else [sys.executable, "-m", "ruff"]
        cmds = [[*base, "check", *files[i:i + 40]] for i in range(0, len(files), 40)]
    total, failed_out = 0.0, []
    for cmd in cmds:
        code, seconds, out = run_command(cmd, workdir, env, check.timeout)
        total += seconds
        if code != 0:
            failed_out.append(out)
            if len(failed_out) >= 5:
                break
    if failed_out:
        text = "\n".join(failed_out)
        if check.type == "ruff" and "No module named ruff" in text:
            return CheckResult(check.name, check.type, "skip", total, detail="ruff が入っていない")
        return CheckResult(check.name, check.type, "fail", total, tail(text, 60, 6000))
    return CheckResult(check.name, check.type, "ok", total, detail=f"{len(files)} ファイル")


def run_checks(
    manifest: Manifest, workdir: Path, changed: list[str] | None, logdir: Path,
) -> list[CheckResult]:
    """定義順に検査を回す。前段（blocking）が落ちたら cmd / e2e は回さず skip にする。"""
    tmp, shots = logdir / "tmp", logdir / "shots"
    tmp.mkdir(parents=True, exist_ok=True)
    shots.mkdir(parents=True, exist_ok=True)
    ctx = {"port": "", "base_url": "", "tmp": str(tmp), "shots": str(shots),
           "workdir": str(workdir), "python": sys.executable}
    results: list[CheckResult] = []
    blocked = False
    server: ServeHandle | None = None
    server_error = ""
    try:
        for check in manifest.checks:
            if check.type in DYNAMIC_TYPES and blocked:
                results.append(CheckResult(check.name, check.type, "skip", detail="前段の検査が失敗したため回さない"))
                continue
            env = clean_env({"LV0_WORKDIR": str(workdir), "LV0_SHOT_DIR": str(shots), "LV0_TMP": str(tmp)})
            if check.type in STATIC_TYPES:
                result = _builtin_check(check, workdir, changed, env)
            else:
                if check.needs_serve and server is None and not server_error:
                    try:
                        server = ServeHandle(manifest.serve, workdir, ctx, logdir / "serve.log")  # type: ignore[arg-type]
                        server.__enter__()
                    except ServeError as exc:
                        server, server_error = None, str(exc)
                if check.needs_serve and server_error:
                    result = CheckResult(check.name, check.type, "fail", excerpt=server_error)
                else:
                    if check.needs_serve:
                        env["LV0_BASE_URL"] = ctx["base_url"]
                    cwd = within(workdir, check.cwd)
                    code, seconds, out = run_command(expand_cmd(check.cmd, ctx, cwd), cwd, env, check.timeout)
                    (logdir / "checks").mkdir(parents=True, exist_ok=True)
                    (logdir / "checks" / f"{check.name}.txt").write_text(out, encoding="utf-8", newline="")
                    ok = code == 0
                    result = CheckResult(check.name, check.type, "ok" if ok else "fail", seconds,
                                         "" if ok else tail(out, 60, 6000))
            results.append(result)
            if result.status == "fail" and check.blocking:
                blocked = True
    finally:
        if server is not None:
            server.__exit__(None, None, None)
    return results


def failing(results: list[CheckResult]) -> list[CheckResult]:
    return [r for r in results if r.status == "fail"]


def failure_signature(results: list[CheckResult]) -> frozenset[tuple[str, str]]:
    return frozenset(
        (r.name, hashlib.sha1(normalize_failure(r.excerpt).encode("utf-8")).hexdigest()[:12])
        for r in failing(results)
    )


# ---------------------------------------------------------------- 下請け（cgd_lv0_codex.py）


class _Buf(io.StringIO):
    """engine.main が最初に呼ぶ sys.stdout.reconfigure を受けるだけの StringIO。"""

    def reconfigure(self, **_: object) -> None:
        return None


class Engine:
    """cgd_lv0_codex.py を呼ぶ。本番は別プロセス、テストは同じプロセス内（inprocess=True）。"""

    def __init__(self, inprocess: bool = False) -> None:
        self.inprocess = inprocess
        self.script = TOOLS / "cgd_lv0_codex.py"

    def _call(self, args: list[str], timeout: int) -> tuple[int, str]:
        if self.inprocess:
            buf = _Buf()
            with contextlib.redirect_stdout(buf):
                code = engine.main(args)
            return code, buf.getvalue()
        try:
            proc = subprocess.run(
                [sys.executable, str(self.script), *args], capture_output=True, timeout=timeout,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
        except subprocess.TimeoutExpired:
            return engine.EXIT_TIMEOUT, f"cgd_lv0_codex.py が {timeout} 秒で応答しない（{args[0]}）"
        return proc.returncode, proc.stdout.decode("utf-8", errors="replace")

    def rundir(self, run: str) -> Path:
        return engine.RUNS_BASE / f"cgd_lv0_{run}"

    def new_run_id(self) -> str:
        moment = time.time()
        while True:
            run = time.strftime("%Y%m%d_%H%M%S", time.localtime(moment))
            if not self.rundir(run).exists():
                return run
            moment += 1

    def prepare(self, run: str, workdir: Path) -> tuple[int, str]:
        return self._call(["prepare", "--workdir", str(workdir), "--run", run], 900)

    def run(self, run: str, workdir: Path, spec: Path, effort: str, timeout: int) -> tuple[int, str]:
        return self._call(
            ["run", "--workdir", str(workdir), "--run", run, "--spec", str(spec),
             "--effort", effort, "--timeout", str(timeout)], timeout + 300)

    def diff(self, run: str, workdir: Path) -> tuple[int, str]:
        return self._call(["diff", "--workdir", str(workdir), "--run", run], 900)

    def read_json(self, run: str, name: str) -> dict:
        try:
            return json.loads((self.rundir(run) / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def overall_diff(self, base_run: str, workdir: Path) -> dict:
        manifest = self.read_json(base_run, "manifest.json")
        if not manifest:
            return {"modified": [], "added": [], "deleted": [], "untracked_changed": [],
                    "notes": ["最初の写しが読めない"], "plus": 0, "minus": 0, "patch": ""}
        return engine.diff_snapshot(workdir, self.rundir(base_run) / "before", manifest)

    def weekly_used(self) -> float | None:
        return engine.weekly_percent(engine.latest_rate_limits())


# ---------------------------------------------------------------- 出し直しの依頼文・凍結の強制


def render_fix_spec(spec: str, failed: list[CheckResult], frozen: list[str], round_no: int) -> str:
    lines = [
        f"【自動検査による修正依頼（第 {round_no} 周）】",
        "前の実装に自動検査を回した結果、次の検査が失敗した。失敗の原因になっているコードだけを直し、"
        "それ以外は変えない。出力は末尾の抜粋。原因が仕様側にあると思うときは直さず、最終報告で質問する。",
        "e2e の検査は、依頼元が起動したアプリに環境変数 LV0_BASE_URL で接続して実行される。",
    ]
    if frozen:
        lines.append("凍結ファイル（すでにあるものは変更・削除しない。新しく足すのは可）: " + " / ".join(frozen))
    for r in failed[:3]:
        lines += ["", f"### 失敗: {r.name}（{r.type}）", "```", tail(r.excerpt, 40, 4000), "```"]
    if len(failed) > 3:
        lines.append(f"（ほか {len(failed) - 3} 件の失敗あり。まず上の 3 件を直す）")
    lines += ["", "【元の仕様】", spec.strip()]
    return "\n".join(lines) + "\n"


def enforce_frozen(workdir: Path, rundir: Path, diff: dict, frozen: list[str]) -> list[str]:
    """この周で凍結ファイルを変更・削除していたら、周の開始時の写しから戻す。戻した相対パスを返す。"""
    violations: list[str] = []
    for rel in list(diff.get("modified", [])) + list(diff.get("deleted", [])):
        if not matches_frozen(rel, frozen):
            continue
        src = rundir / "before" / rel
        if src.is_file():
            dest = within(workdir, rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            violations.append(rel)
    return violations


# ---------------------------------------------------------------- 実行本体


@dataclass
class Config:
    workdir: Path
    spec: str
    manifest: Manifest
    effort: str = "medium"
    max_fix_rounds: int = 2
    timeout: int = 3600
    quota_stop: float = 80.0
    review: bool = False
    review_min_plus: int = 100
    contract: str = ""


@dataclass
class RoundInfo:
    index: int
    run: str
    effort: str
    seconds: int | None = None
    tokens: int | None = None
    plus: int = 0
    minus: int = 0
    codex_exit: int | None = None
    failed: list[str] = field(default_factory=list)
    frozen_restored: list[str] = field(default_factory=list)


@dataclass
class Outcome:
    status: str = "engine_error"
    note: str = ""
    workdir: str = ""
    base_run: str = ""
    autodir: str = ""
    rounds: list[RoundInfo] = field(default_factory=list)
    results: list[CheckResult] = field(default_factory=list)
    overall: dict = field(default_factory=dict)
    review: str = ""
    weekly: float | None = None
    shots: list[str] = field(default_factory=list)
    questions: str = ""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def run_review(patch: str, autodir: Path) -> str:
    """DeepSeek reviewer に全体の差分を渡す。戻りは 1 行の要約（全文は review_ds.txt）。"""
    data = patch.encode("utf-8")
    if not data.strip():
        return "レビュー対象の差分なし"
    if engine.content_secret_hits(data):
        return "差分に鍵らしき文字列があるため送信しなかった（要確認）"
    if len(data) > REVIEW_MAX_BYTES:
        patch = patch[:REVIEW_MAX_BYTES] + "\n[以降は長すぎるため省略]\n"
    src = autodir / "review_input.txt"
    _write(src, "Codex が生成したコードの差分レビュー。バグ・設計・規約逸脱を厳密評価。日本語回答。\n\n" + patch)
    proc = subprocess.run(
        [sys.executable, str(TOOLS / "deepseek_coder.py"), "--role", "reviewer", str(src)],
        capture_output=True, timeout=900, env={**os.environ, "PYTHONIOENCODING": "utf-8"}, creationflags=NO_WINDOW,
    )
    text = proc.stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0 or not text.strip():
        return f"DeepSeek レビュー失敗（exit {proc.returncode}）"
    _write(autodir / "review_ds.txt", text)
    counts = {mark: text.count(mark) for mark in ("🔴", "🟠", "🟡")}
    return f"🔴{counts['🔴']} 🟠{counts['🟠']} 🟡{counts['🟡']}（全文: {autodir / 'review_ds.txt'}）"


def execute(cfg: Config, api: Engine) -> Outcome:
    out = Outcome(workdir=str(cfg.workdir))
    weekly = api.weekly_used()
    out.weekly = weekly
    if weekly is not None and weekly >= cfg.quota_stop:
        out.status, out.note = "quota_stop", f"開始前の週の利用枠が {weekly:.0f}%（上限 {cfg.quota_stop:.0f}%）"
        return out
    spec_text = cfg.spec.strip() + ("\n\n" + cfg.contract.strip() if cfg.contract else "")
    prev_sig: frozenset | None = None
    for index in range(cfg.max_fix_rounds + 1):
        run = api.new_run_id()
        if index == 0:
            out.base_run = run
            autodir = engine.RUNS_BASE / f"cgd_lv0_auto_{run}"
            out.autodir = str(autodir)
            autodir.mkdir(parents=True, exist_ok=True)
        autodir = Path(out.autodir)
        info = RoundInfo(index, run, cfg.effort)
        out.rounds.append(info)
        code, text = api.prepare(run, cfg.workdir)
        if code != 0:
            out.status, out.note = "engine_error", "prepare が失敗: " + tail(text, 8)
            return out
        spec_path = autodir / f"spec_r{index}.txt"
        _write(spec_path, spec_text)
        code, text = api.run(run, cfg.workdir, spec_path, cfg.effort, cfg.timeout)
        run_json = api.read_json(run, "run.json")
        info.codex_exit, info.seconds, info.tokens = run_json.get("exit", code), run_json.get("seconds"), run_json.get("tokens")
        out.weekly = run_json.get("weekly_used_percent", out.weekly)
        if code == engine.EXIT_GENERIC and not run_json:  # 前段のガードで止まった（写しと違う等）
            out.status, out.note = "engine_error", "run が始まらなかった: " + tail(text, 8)
            return out
        api.diff(run, cfg.workdir)
        diff = api.read_json(run, "diff.json")
        info.plus, info.minus = diff.get("plus", 0), diff.get("minus", 0)
        last = api.rundir(run) / "last.txt"
        if cfg.manifest.frozen:  # 周の開始時に存在したものが対象（初回も、もとからあるテストは守る）
            info.frozen_restored = enforce_frozen(cfg.workdir, api.rundir(run), diff, cfg.manifest.frozen)
        changed_now = bool(diff.get("modified") or diff.get("added") or diff.get("deleted"))
        codex_ok = code == engine.EXIT_OK
        if not codex_ok or not changed_now:
            out.questions = tail(last.read_text(encoding="utf-8", errors="replace"), 20, 2000) if last.exists() else ""
        if not codex_ok and not changed_now:
            out.status, out.note = "codex_failed", f"Codex が失敗（exit {code}）で変更なし"
            break
        if codex_ok and not changed_now:
            out.status, out.note = ("no_change", "Codex が何も変更しなかった（質問が返ってきた可能性）") if index == 0 \
                else ("stalled", "出し直しの周で何も変更されなかった")
            break
        overall = api.overall_diff(out.base_run, cfg.workdir)
        changed = list(overall["modified"]) + list(overall["added"])
        out.overall = {k: v for k, v in overall.items() if k != "patch"}
        _write(autodir / "overall.patch", overall.get("patch", ""))
        out.results = run_checks(cfg.manifest, cfg.workdir, changed, autodir / f"checks_r{index}")
        info.failed = [r.name for r in failing(out.results)]
        if not codex_ok:
            out.status, out.note = "codex_failed", f"Codex が途中で失敗（exit {code}）。変更は検査だけ回した"
            break
        if not info.failed:
            out.status = "ok"
            break
        sig = failure_signature(out.results)
        if index >= cfg.max_fix_rounds:
            out.status, out.note = "checks_failed", f"出し直し {cfg.max_fix_rounds} 周でも検査が通らない"
            break
        if prev_sig is not None and sig == prev_sig:
            out.status, out.note = "stalled", "出し直しても同じ失敗が続く"
            break
        weekly = api.weekly_used()
        out.weekly = weekly if weekly is not None else out.weekly
        if weekly is not None and weekly >= cfg.quota_stop:
            out.status, out.note = "quota_stop", f"週の利用枠が {weekly:.0f}%（上限 {cfg.quota_stop:.0f}%）に達した"
            break
        prev_sig = sig
        spec_text = render_fix_spec(cfg.spec, failing(out.results), cfg.manifest.frozen, index + 1)
    if out.status == "ok" and cfg.review and out.overall.get("plus", 0) >= cfg.review_min_plus:
        patch = api.overall_diff(out.base_run, cfg.workdir).get("patch", "")
        out.review = run_review(patch, Path(out.autodir))
    shots = sorted(Path(out.autodir).glob("checks_r*/shots/*.png")) if out.autodir else []
    out.shots = [str(p) for p in shots[-6:]]
    return out


# ---------------------------------------------------------------- レポート


NEXT_STEPS = {
    "ok": "変更一覧とスクリーンショットを確認 → 画面はブラウザで要点だけ見る → 各プロジェクトの手順で反映",
    "checks_failed": "失敗の抜粋を読み、小さければ Claude が直して `check` で再検査。大きければ仕様を直して新しい run",
    "stalled": "同じ失敗が続いた。原因が仕様・検査側にないか確認し、Claude が直すか仕様を直して新しい run",
    "quota_stop": "Codex の週枠が上限。代替手順（DS/Qwen）か、枠が戻るまで待つ",
    "codex_failed": "Codex 側の失敗。最終報告と stderr を確認し、新しい run で再実行するか Lv2 に切り替える",
    "no_change": "最終報告の質問に答えて仕様を直し、新しい run で再実行する",
    "engine_error": "下請け（prepare/run）が止まった。メッセージを確認して新しい run で再実行する",
}
STATUS_LABEL = {
    "ok": "OK（全検査合格）", "checks_failed": "検査が通らない", "stalled": "停滞（同じ失敗）",
    "quota_stop": "利用枠で停止", "codex_failed": "Codex 失敗", "no_change": "変更なし",
    "engine_error": "実行エラー",
}


def render_report(out: Outcome) -> str:
    rounds_tokens = sum(r.tokens or 0 for r in out.rounds)
    rounds_sec = sum(r.seconds or 0 for r in out.rounds)
    lines = [
        f"# Lv0 自動実行レポート — {STATUS_LABEL.get(out.status, out.status)}",
        f"作業フォルダ: {out.workdir}",
        f"基準の RUN: {out.base_run}（記録: {out.autodir}）" if out.base_run else "",
    ]
    if out.note:
        lines.append(f"理由: {out.note}")
    if out.rounds:
        lines += ["", "| 周 | RUN | 強度 | 秒 | Codex tokens | +行 | -行 | 失敗した検査 |", "|---|---|---|---|---|---|---|---|"]
        for r in out.rounds:
            label = "初回" if r.index == 0 else f"出し直し{r.index}"
            lines.append(
                f"| {label} | {r.run} | {r.effort} | {r.seconds if r.seconds is not None else '-'} "
                f"| {r.tokens if r.tokens is not None else '-'} | {r.plus} | {r.minus} | {', '.join(r.failed) or '-'} |")
        weekly = f"{out.weekly:.0f}%" if out.weekly is not None else "不明"
        lines.append(f"Codex 合計: {rounds_tokens:,} tokens / {rounds_sec} 秒 / 週の利用枠 {weekly}")
    if out.results:
        lines += ["", "最終の検査: " + " / ".join(
            f"{r.name}={'ok' if r.status == 'ok' else ('FAIL' if r.status == 'fail' else 'skip')}" for r in out.results)]
        for r in failing(out.results)[:3]:
            lines += ["", f"失敗: {r.name}（{r.type}）", "```", tail(r.excerpt, 12, 1200), "```"]
    ov = out.overall
    if ov:
        names = [f"{k}:{p}" for k in ("modified", "added", "deleted") for p in ov.get(k, [])]
        lines += ["", f"変更: 更新 {len(ov.get('modified', []))} / 新規 {len(ov.get('added', []))} / 削除 "
                      f"{len(ov.get('deleted', []))}  +{ov.get('plus', 0)} -{ov.get('minus', 0)} 行（全周の合計）"]
        lines.append("  " + ", ".join(names[:30]) + (f" …ほか {len(names) - 30} 件" if len(names) > 30 else ""))
        for label in ("untracked_changed", "notes"):
            for item in ov.get(label, []):
                lines.append(f"  注意({label}): {item}")
    restored = [f"{r.index}周目:{p}" for r in out.rounds for p in r.frozen_restored]
    lines += ["", "凍結ファイルの違反（写しから戻した）: " + (", ".join(restored) if restored else "なし")]
    if out.review:
        lines.append(f"DeepSeek レビュー: {out.review}")
    if out.shots:
        lines.append("スクリーンショット: " + " , ".join(out.shots))
    if out.questions:
        lines += ["", "Codex の最終報告（抜粋）:", "```", tail(out.questions, 12, 1200), "```"]
    lines += ["", f"次にすること: {NEXT_STEPS.get(out.status, '')}"]
    return "\n".join(line for line in lines if line is not None) + "\n"


# ---------------------------------------------------------------- サブコマンド


def _load_inputs(args: argparse.Namespace) -> tuple[Path, Manifest]:
    workdir = Path(args.workdir).resolve()
    return workdir, load_manifest(Path(args.checks))


def cmd_plan(args: argparse.Namespace) -> int:
    workdir, manifest = _load_inputs(args)
    errors = engine.validate_workdir(workdir)
    for e in errors:
        print(f"NG: {e}")
    choice = engine.choose_bin(engine.candidate_bins())
    print(f"作業フォルダ: {workdir}")
    print(f"Codex CLI: {'OK ' + str(choice.exe) if choice.exe else 'NG（使える CLI が無い）'}")
    print(f"直近の Codex 利用枠: {engine.describe_limits(engine.latest_rate_limits())}")
    if not errors:
        secrets = engine.scan_secrets(workdir)
        print(f"秘密情報らしいファイル: {len(secrets)} 件（Codex が開ける場所にある。写しには複製しない）")
        for p, k in secrets[:20]:
            print(f"  [{k}] {p}")
        excluded = engine.excluded_dirs_present(workdir)
        if excluded:
            print(f"見ないフォルダ（写しも差分も無い）: {', '.join(excluded)}")
    print("自動検査（機械が実行する）:")
    for i, c in enumerate(manifest.checks, start=1):
        detail = " ".join(c.cmd) if c.cmd else "(組み込み)"
        flags = ("前段ゲート " if c.blocking else "") + ("アプリ起動あり " if c.needs_serve else "")
        print(f"  {i}. {c.name} [{c.type}] {flags}{detail}（{c.timeout}秒）")
    if manifest.serve:
        print("アプリ起動: " + " ".join(manifest.serve.cmd) + "（空きポート・検査用の一時データで動かすこと）")
    print("凍結ファイル（出し直しで変更不可）: " + (" / ".join(manifest.frozen) or "なし"))
    print(f"出し直し: 最大 {args.max_fix_rounds} 周・週の利用枠 {args.quota_stop:.0f}% で停止・失敗が変わらなければ停止")
    print("送信先: Codex（OpenAI）" + (" ＋ DeepSeek（中国本土サーバ・差分レビュー）" if args.review == "deepseek" else ""))
    return EXIT_GENERIC if errors or choice.exe is None else EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    workdir, manifest = _load_inputs(args)
    spec = Path(args.spec).read_text(encoding="utf-8")
    contract = ""
    if any(c.type == "e2e" for c in manifest.checks) and CONTRACT_PATH.exists():
        contract = CONTRACT_PATH.read_text(encoding="utf-8")
    cfg = Config(workdir, spec, manifest, args.effort, args.max_fix_rounds, args.timeout,
                 args.quota_stop, args.review == "deepseek", args.review_min_plus, contract)
    out = execute(cfg, Engine())
    report = render_report(out)
    if out.autodir:
        _write(Path(out.autodir) / "report.md", report)
        _write(Path(out.autodir) / "outcome.json", json.dumps(
            {"status": out.status, "note": out.note, "base_run": out.base_run,
             "rounds": [r.__dict__ for r in out.rounds]}, ensure_ascii=False, indent=2))
    print(report)
    return STATUS_EXIT.get(out.status, EXIT_GENERIC)


def cmd_check(args: argparse.Namespace) -> int:
    workdir, manifest = _load_inputs(args)
    logdir = engine.RUNS_BASE / f"cgd_lv0_check_{time.strftime('%Y%m%d_%H%M%S')}"
    results = run_checks(manifest, workdir, None, logdir)
    for r in results:
        print(f"{r.name}: {r.status}" + (f"（{r.detail}）" if r.detail else "") + f"  {r.seconds:.1f}秒")
        if r.status == "fail":
            print(tail(r.excerpt, 12, 1200))
    print(f"記録: {logdir}")
    return EXIT_CHECKS_FAILED if failing(results) else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="cgd Lv0 自動実行ドライバ（Codex 実装 → 検査 → 出し直し → 報告）")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("plan", "承認表に載せる情報を出す（何も書かない）"),
        ("run", "Codex に実装させ、検査し、失敗なら出し直す"),
        ("check", "今の作業フォルダに検査だけを回す"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--workdir", required=True)
        p.add_argument("--checks", required=True, help="検査定義 JSON（作業フォルダの外に置く）")
        if name in ("plan", "run"):
            p.add_argument("--max-fix-rounds", type=int, default=2, choices=range(0, 4))
            p.add_argument("--quota-stop", type=float, default=80.0, help="週の利用枠がこの % 以上なら止める")
        if name == "plan":
            p.add_argument("--review", choices=("none", "deepseek"), default="none")
        if name == "run":
            p.add_argument("--spec", required=True, help="仕様を書いたファイル（UTF-8）")
            p.add_argument("--effort", default="medium", choices=("low", "medium", "high"))
            p.add_argument("--timeout", type=int, default=3600, help="Codex 1 周あたりの打ち切り秒")
            p.add_argument("--review", choices=("none", "deepseek"), default="none")
            p.add_argument("--review-min-plus", type=int, default=100)
    return parser


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    handlers = {"plan": cmd_plan, "run": cmd_run, "check": cmd_check}
    try:
        return handlers[args.command](args)
    except ValueError as exc:
        print(f"NG: {exc}")
        return EXIT_GENERIC


if __name__ == "__main__":
    sys.exit(main())
