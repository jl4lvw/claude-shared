from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Sequence


SCHEMA = 1
EXCLUDED_DIRS = frozenset(
    {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache"}
)
DEFAULT_IGNORED_CHANGES = (
    ".hq/**",
    ".ctx/**",
    ".claude/incidents/cpu_snapshots.csv",
    ".claude/incidents/telemetry.jsonl",
    ".claude/tools/.*_usage_session.json",
    ".claude/tools/cgd_usage_*.sqlite3",
    "*.db-shm",
    "*.db-wal",
    "*.db-journal",
    "*.sqlite-journal",
    "*.sqlite3-journal",
    "__pycache__/**",
)
MAX_FILES = 30_000
MAX_BYTES = 500 * 1024 * 1024
MAX_DIRTY_PATHS = 20_000
LARGE_OUTSIDE_FILE = 2 * 1024 * 1024
DEFAULT_BASE = Path("C:/tmp-ai/cx")
ALLOWED_ENV = frozenset(
    {
        "PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "USERPROFILE", "HOME",
        "TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "COMSPEC", "PATHEXT", "LANG",
        "PYTHONIOENCODING", "PYTHONUTF8", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
        "ALL_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    }
)
SECRET_PATTERNS = (
    re.compile(rb"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(
        rb"(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"]{8,}['\"]",
        re.IGNORECASE,
    ),
)

Runner = Callable[[Sequence[str], Path, int, dict[str, str]], dict[str, object]]


class CxError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def _project_relative(project: Path, repo: Path) -> str:
    if _same_path(project, repo) or not _inside(project, repo):
        raise CxError("project は git リポジトリ内のサブフォルダである必要があります", 2)
    try:
        return project.relative_to(repo).as_posix()
    except ValueError as exc:
        raise CxError("project は git リポジトリ内のサブフォルダである必要があります", 2) from exc


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)


def _atomic_new(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    _write_new(temporary, data)
    os.rename(temporary, path)


def child_env(source: dict[str, str] | None = None) -> dict[str, str]:
    source = os.environ if source is None else source
    result: dict[str, str] = {}
    for name, value in source.items():
        upper = name.upper()
        if upper not in ALLOWED_ENV and not upper.startswith("CGD_"):
            continue
        if upper.endswith("PROXY") and re.match(r"^[a-z]+://[^/@]+@", value, re.IGNORECASE):
            continue
        result[name] = value
    result["PYTHONIOENCODING"] = "utf-8"
    result["PYTHONUTF8"] = "1"
    return result


def run_command(args: Sequence[str], cwd: Path, timeout: int, env: dict[str, str]) -> dict[str, object]:
    started = time.monotonic()
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        list(args), cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, creationflags=creationflags,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return {
            "exit_code": process.returncode, "stdout": stdout, "stderr": stderr,
            "timeout": False, "duration_ms": round((time.monotonic() - started) * 1000),
        }
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, shell=False,
            )
        else:
            process.kill()
        process.communicate()
        return {
            "exit_code": None, "stdout": b"", "stderr": b"", "timeout": True,
            "duration_ms": round((time.monotonic() - started) * 1000),
        }


def _command(runner: Runner, args: Sequence[str], cwd: Path, timeout: int = 60) -> dict[str, object]:
    try:
        return runner(args, cwd, timeout, child_env())
    except (OSError, subprocess.SubprocessError) as exc:
        return {"exit_code": None, "stdout": b"", "stderr": b"", "timeout": False, "error": type(exc).__name__}


def _redacted_output(value: object) -> str:
    data = value if isinstance(value, bytes) else str(value).encode("utf-8", "replace")
    for pattern in SECRET_PATTERNS:
        data = pattern.sub(b"[REDACTED]", data)
    return data.decode("utf-8", errors="replace")[:200]


def _failure_detail(result: dict[str, object]) -> str:
    return (
        f"終了コード={result.get('exit_code')}; "
        f"stdout={_redacted_output(result.get('stdout', b''))!r}; "
        f"stderr={_redacted_output(result.get('stderr', b''))!r}"
    )


def _git(runner: Runner, repo: Path, *args: str) -> bytes:
    result = _command(runner, ["git", *args], repo)
    if result.get("timeout") or result.get("exit_code") != 0:
        raise CxError(f"git の確認に失敗しました: {' '.join(args)}; {_failure_detail(result)}", 2)
    output = result.get("stdout", b"")
    return output if isinstance(output, bytes) else str(output).encode("utf-8")


def validate_project(project_arg: str, runner: Runner) -> tuple[Path, Path]:
    project = Path(project_arg).resolve()
    if not project.is_dir():
        raise CxError("project は実在するフォルダである必要があります", 2)
    if project.parent == project:
        raise CxError("ドライブ直下は指定できません", 2)
    if any(part.casefold() == ".claude" for part in project.parts):
        raise CxError(".claude 配下は指定できません", 2)
    root_text = _git(runner, project, "rev-parse", "--show-toplevel").decode("utf-8").strip()
    root = Path(root_text).resolve()
    if _same_path(project, root):
        raise CxError("git リポジトリ最上位は指定できません", 2)
    if not _inside(project, root):
        raise CxError("project は git リポジトリ内である必要があります", 2)
    _project_relative(project, root)
    return project, root


def _regular_files(
    root: Path, excluded: frozenset[str] = EXCLUDED_DIRS, *, include_links: bool = False,
    skip_roots: Sequence[Path] = (),
) -> Iterable[tuple[str, Path]]:
    pending = [root]
    found: list[tuple[str, Path]] = []
    while pending:
        directory = pending.pop()
        for entry in os.scandir(directory):
            path = Path(entry.path)
            if entry.is_symlink():
                if include_links:
                    found.append((path.relative_to(root).as_posix(), path))
                continue
            if entry.is_dir(follow_symlinks=False):
                if entry.name not in excluded and not any(_same_path(path, skipped) for skipped in skip_roots):
                    pending.append(path)
            elif entry.is_file(follow_symlinks=False) and _inside(path, root):
                found.append((path.relative_to(root).as_posix(), path))
    yield from sorted(found)


def snapshot(
    root: Path, *, max_files: int = MAX_FILES, max_bytes: int = MAX_BYTES,
    skip_roots: Sequence[Path] = (),
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    total = 0
    for relative, path in _regular_files(root, include_links=True, skip_roots=skip_roots):
        if len(result) >= max_files:
            raise CxError("対象が大きすぎる。--project を狭く", 3)
        if path.is_symlink():
            metadata = path.lstat()
            result[relative] = {"type": "symlink", "size": metadata.st_size, "mtime_ns": metadata.st_mtime_ns}
            continue
        fingerprint: dict[str, object] | None = None
        for _attempt in range(3):
            try:
                before = path.stat()
                digest = hashlib.sha256()
                size = 0
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        size += len(block)
                        if total + size > max_bytes:
                            raise CxError("対象が大きすぎる。--project を狭く", 3)
                        digest.update(block)
                after = path.stat()
            except OSError:
                continue
            if before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns:
                fingerprint = {"size": size, "sha256": digest.hexdigest()}
                total += size
                break
        if fingerprint is None:
            fingerprint = {"type": "unstable"}
        result[relative] = fingerprint
    return result


def _path_fingerprint(path: Path) -> dict[str, object]:
    if path.is_symlink():
        metadata = path.lstat()
        return {"type": "symlink", "size": metadata.st_size, "mtime_ns": metadata.st_mtime_ns}
    if path.is_dir():
        return {"type": "directory"}
    if not path.is_file():
        return {"type": "missing"}
    for _attempt in range(3):
        try:
            before = path.stat()
            if before.st_size > LARGE_OUTSIDE_FILE:
                after = path.stat()
                if before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns:
                    return {"type": "large", "size": before.st_size, "mtime_ns": before.st_mtime_ns}
                continue
            data = path.read_bytes()
            after = path.stat()
        except OSError:
            continue
        if before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns:
            return {"type": "file", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    return {"type": "unstable"}


def _repo_relative_root(path: Path, repo: Path) -> str | None:
    if not _inside(path, repo):
        return None
    relative = path.resolve().relative_to(repo.resolve()).as_posix()
    return relative.rstrip("/")


def _under(relative: str, root: str) -> bool:
    return relative == root or relative.startswith(root + "/")


def _ignored_change(relative: str) -> bool:
    normalized = relative.rstrip("/")
    parts = Path(normalized).parts
    for pattern in DEFAULT_IGNORED_CHANGES:
        if pattern.endswith("/**"):
            root_parts = Path(pattern[:-3]).parts
            width = len(root_parts)
            if any(parts[index:index + width] == root_parts for index in range(len(parts) - width + 1)):
                return True
        elif fnmatch.fnmatchcase(normalized, pattern) or (
            "/" not in pattern and fnmatch.fnmatchcase(Path(normalized).name, pattern)
        ):
            return True
    return False


def _dirty_groups(
    paths: Sequence[str], repo: Path, project: Path, ignored_roots: Sequence[Path],
) -> tuple[list[str], list[str]]:
    project_root = _project_relative(project, repo)
    ignored = [item for root in ignored_roots if (item := _repo_relative_root(root, repo))]
    outside: list[str] = []
    protected: list[str] = []
    for relative in paths:
        candidate = (repo / Path(relative)).resolve()
        if not _inside(candidate, repo) or _under(relative, project_root):
            continue
        if any(_under(relative, root) for root in ignored):
            continue
        if _ignored_change(relative):
            continue
        if _under(relative, ".claude"):
            protected.append(relative)
        else:
            outside.append(relative)
    return sorted(set(outside)), sorted(set(protected))


def _ignored_dirty_paths(
    paths: Sequence[str], repo: Path, project: Path, ignored_roots: Sequence[Path],
) -> list[str]:
    project_root = _project_relative(project, repo)
    generated = [item for root in ignored_roots if (item := _repo_relative_root(root, repo))]
    return sorted({
        relative for relative in paths
        if not _under(relative.rstrip("/"), project_root)
        and not any(_under(relative.rstrip("/"), root) for root in generated)
        and _ignored_change(relative)
    })


def _fingerprints(repo: Path, paths: Sequence[str]) -> dict[str, dict[str, object]]:
    return {relative: _path_fingerprint(repo / Path(relative)) for relative in sorted(set(paths))}


def _porcelain_paths(data: bytes) -> list[str]:
    fields = data.split(b"\0")
    paths: list[str] = []
    index = 0
    while index < len(fields) and fields[index]:
        item = fields[index]
        if len(item) < 4:
            raise CxError("git status の形式が不正です", 3)
        status = item[:2]
        paths.append(item[3:].decode("utf-8", "surrogateescape").replace("\\", "/"))
        index += 1
        if b"R" in status or b"C" in status:
            if index >= len(fields) or not fields[index]:
                raise CxError("git rename の形式が不正です", 3)
            paths.append(fields[index].decode("utf-8", "surrogateescape").replace("\\", "/"))
            index += 1
    return sorted(set(paths))


def git_state(
    runner: Runner, root: Path, ignored_roots: Sequence[Path] = (),
) -> dict[str, object]:
    dirty_paths = _porcelain_paths(
        _git(runner, root, "status", "--porcelain=v1", "-z", "--untracked-files=normal")
    )
    ignored = [item for path in ignored_roots if (item := _repo_relative_root(path, root))]
    dirty_paths = [
        path for path in dirty_paths
        if not any(_under(path.rstrip("/"), ignored_root) for ignored_root in ignored)
    ]
    if len(dirty_paths) > MAX_DIRTY_PATHS:
        raise CxError("他のセッションの変更が多すぎます。--project を狭くしてください", 3)
    return {
        "head": _git(runner, root, "rev-parse", "HEAD").decode("ascii").strip(),
        "branch": _git(runner, root, "branch", "--show-current").decode("utf-8").strip(),
        "dirty_paths": dirty_paths,
    }


def _slug(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-")[:48]
    return result or "task"


def _random6(generator: Callable[[], str]) -> str:
    value = generator().lower()
    if not re.fullmatch(r"[0-9a-f]{6}", value):
        raise CxError("乱数生成器は6桁16進を返す必要があります", 3)
    return value


def _read_request(request: str | None, request_file: Path | None) -> tuple[bytes, str]:
    if (request is None) == (request_file is None):
        raise CxError("--request と --request-file はどちらか一方だけ指定してください")
    if request is not None:
        raw = request.encode("utf-8")
        text = request
    else:
        assert request_file is not None
        path = request_file
        try:
            if path.is_symlink():
                raise CxError("依頼ファイルにリンクは指定できません")
            raw = path.read_bytes()
            text = raw.decode("utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            raise CxError(f"依頼ファイルを UTF-8 で読めません: {type(exc).__name__}") from exc
    if not raw or not text:
        raise CxError("依頼は空にできません")
    return raw, text


def _request_source_path(request_file: Path | None) -> str | None:
    return str(request_file.resolve()) if request_file is not None else None


def _ensure_disjoint(base: Path, sources: Sequence[Path]) -> None:
    for source in sources:
        if _inside(base, source) or _inside(source, base):
            raise CxError("base-dir は project・context 元・memory と分離してください", 2)


def _copy_context(repo: Path, memory: Path, destination: Path) -> tuple[bool, list[str]]:
    sources: list[tuple[Path, Path]] = []
    for name in ("CLAUDE.md", "AGENTS.md"):
        path = repo / name
        if path.exists():
            sources.append((path, Path(name)))
    rules = repo / ".claude" / "rules"
    if rules.is_dir():
        sources.extend((path, Path("rules") / path.relative_to(rules)) for _, path in _regular_files(rules) if path.suffix == ".md")
    memory_present = memory.is_dir()
    if memory_present:
        sources.extend((path, Path("memory") / path.relative_to(memory)) for _, path in _regular_files(memory) if path.suffix == ".md")
    manifest: list[str] = []
    seen: set[str] = set()
    for source, relative in sorted(sources, key=lambda pair: pair[1].as_posix()):
        if source.is_symlink() or not source.is_file() or not _inside(source, repo if not relative.parts[0] == "memory" else memory):
            continue
        key = relative.as_posix()
        if key in seen:
            raise CxError(f"context のコピー先が重複しました: {key}", 3)
        seen.add(key)
        data = source.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        target = destination / relative
        if not _inside(target, destination):
            raise CxError("context のコピー先が範囲外です", 3)
        _write_new(target, data)
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest or hashlib.sha256(source.read_bytes()).hexdigest() != digest:
            raise CxError(f"context のコピー検証に失敗しました: {key}", 3)
        manifest.append(f"{key}\t{digest}")
    manifest.append(f"count\t{len(seen)}")
    _write_new(destination / "manifest.txt", ("\n".join(manifest) + "\n").encode("utf-8"))
    return memory_present, ([] if memory_present else ["メモリの写しなし"])


def _load_json(path: Path) -> dict[str, object]:
    try:
        if path.is_symlink():
            raise CxError(f"リンクの JSON は読めません: {path.name}")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CxError(f"JSON を読めません: {path.name}") from exc
    if not isinstance(value, dict):
        raise CxError(f"JSON の形式が不正です: {path.name}")
    return value


def load_lv0_prepare_contract(run_id: str, artifact_root: Path, project: Path) -> dict[str, object]:
    root = artifact_root.resolve()
    prepare = _load_json(root / "prepare.json")
    workdir = prepare.get("workdir")
    if not isinstance(workdir, str) or not workdir or not _same_path(Path(workdir), project):
        raise CxError("Lv0 workdir が一致しません", 3)
    if prepare.get("run") != run_id:
        raise CxError("Lv0 run ID が一致しません", 3)
    secrets_value = prepare.get("secrets", [])
    if not isinstance(secrets_value, list):
        raise CxError("Lv0 secrets の形式が不正です", 3)
    clean: list[dict[str, str]] = []
    for item in secrets_value:
        if not isinstance(item, dict) or set(item) != {"path", "kind"} or not all(isinstance(item[key], str) for key in item):
            raise CxError("Lv0 secrets は path/kind のみ許可されます", 3)
        clean.append({"path": item["path"], "kind": item["kind"]})
    manifest, before = root / "manifest.json", root / "before"
    if manifest.is_symlink() or before.is_symlink() or not manifest.is_file() or not before.is_dir():
        raise CxError("Lv0 artifact が不足しています", 3)
    return {"secrets": clean}


def _request_document(
    project: Path,
    repo: Path,
    run_dir: Path,
    raw: bytes,
    secret_items: list[dict[str, str]],
    memory: bool,
    git: dict[str, object],
    go: bool,
) -> bytes:
    secret_summary = json.dumps(secret_items, ensure_ascii=False, separators=(",", ":")) if secret_items else "なし"
    project_rel = _project_relative(project, repo)
    dirty_paths = git.get("dirty_paths", [])
    dirty_count = sum(
        path == project_rel or path.startswith(project_rel + "/")
        for path in dirty_paths if isinstance(path, str)
    ) if isinstance(dirty_paths, list) else 0
    lines = [
        "# Codex デバッグ依頼", "", "## 依頼（原文）", "--- BEGIN ORIGINAL REQUEST ---",
    ]
    prefix = ("\n".join(lines) + "\n").encode("utf-8")
    suffix_lines = [
        "--- END ORIGINAL REQUEST ---", "", "## 対象",
        f"- project: {project.as_posix()}", f"- repository: {repo.as_posix()}",
        f"- git HEAD: {git.get('head', '')}", f"- branch: {git.get('branch', '')}",
        f"- 対象フォルダの未コミット件数: {dirty_count}", f"- run directory: {run_dir.as_posix()}",
        f"- memory: {'context/memory に写しあり' if memory else 'メモリの写しなし'}", "",
        "## 最初にやること（順番どおり）",
        "1. context/AGENTS.md、context/CLAUDE.md、context/rules/ を読み、内容に従う。",
        "2. context/memory/MEMORY.md を索引として読み、触る領域に関係するメモリを該当ファイルまで読む。説明に「厳守」とあるものは特に守る。",
        "3. Claude 用手順書は repository の .claude/skills/ にある。依頼に関係するものは読んで従ってよいが、勝手に実行・連鎖させない。",
        "4. 対象 project の README と仕様書を読む。",
        "5. run directory に plan.md を作り、再現手順・完了条件・触る範囲を各1〜3行で書く。",
        (
            "6. plan.md を書いたら、OK を待たずに進めてよい（plan.md は残す）。"
            if go else
            "6. plan.md の内容をユーザーに見せ、OK をもらうまで対象ファイルを変更しない。"
        ), "",
        "## 範囲と禁止事項",
        "- Claude 側には承認ゲート（hook）があるが、こちら側には無い。禁止事項は自分で守る。",
        "- 変更してよいのは project と run directory の中だけ。成果物はこの範囲に配置する。",
        "- .claude/ 配下は変更しない。ファイルを削除せず、不要なファイルは残して result.md に書く。",
        "- 『Lv0 の秘密候補』のファイルは開かない・写さない・書き換えない。鍵・パスワード・トークンの値をファイル・ログ・result.md・チャットに書かない。",
        "- 既存ファイルを編集する前に、同じ場所へ <元名>.bak_YYYYMMDD_HHMMSS を残す。",
        "- git commit/push/reset/stash/clean/checkout による巻き戻しを行わない。",
        "- 本番サーバーの再起動、書込 API、本番 DB 書込、外部送信、メール送信は、その都度ユーザーの承認を得る。",
        "- デプロイ、反映、/g-ul を行わない。秘密値を表示・記録しない。", "",
        "## 検証記録と配置先",
        "- 調査・修正・検証を行い、コマンド、終了コード、結果の要点を run directory の detail/ に残す。",
        "- 長く待つコマンドにはタイムアウトを付ける。",
        "- 画面確認はヘッドレス Playwright だけを使い、許可ダイアログを出さない。本番に対する非 GET は abort して読み取りだけにする。",
        "- plan.md、result.md、detail/ は run directory に置く。対象コードの変更は project 内だけに置く。", "",
        "## result.md の構成（40行以内）",
        "1. 要約（1行）", "2. 原因", "3. 変更したファイル（project からの相対パス）と内容",
        "4. 仕様変更（あれば。仕様書・README も更新する）", "5. 実行した検証と結果",
        "6. 未解決・手作業・本番未実施", "7. 戻し方",
        "コード・差分・長いログは貼らず、10行を超えるコードブロックを入れない。詳細は detail/（log.md・changes.patch など）に置く。",
        "終了時にチャットへ書くのは「完了。結果: <result.md のフルパス>」だけ。", "",
        "## Lv0 の秘密候補（値は記録しない）", secret_summary,
    ]
    suffix = ("\n" + "\n".join(suffix_lines) + "\n").encode("utf-8")
    return prefix + raw + suffix


@contextmanager
def _new_lifecycle(run_dir: Path, moment: datetime) -> Iterable[None]:
    try:
        yield
    except BaseException:
        try:
            _append_event(run_dir, "new_failed", moment)
        except OSError:
            pass
        raise


def new_run(
    project_arg: str, request_file: Path | None, title: str | None, base_dir: Path, memory_dir: Path | None,
    *, runner: Runner = run_command, clock: Callable[[], datetime] = datetime.now,
    random_hex: Callable[[], str] = lambda: secrets.token_hex(3),
    artifact_resolver: Callable[[str], Path] = lambda run_id: Path(f"C:/tmp-ai/cgd_lv0_{run_id}"),
    lv0_script: Path | None = None,
    request: str | None = None,
    go: bool = False,
) -> Path:
    raw, text = _read_request(request, request_file)
    project, repo = validate_project(project_arg, runner)
    memory = memory_dir.resolve() if memory_dir else Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(repo.resolve())) / "memory"
    base = base_dir.resolve()
    _ensure_disjoint(base, [project, repo / ".claude", memory])
    moment = clock()
    timestamp = moment.strftime("%Y%m%d_%H%M%S")
    slug = _slug(title if title is not None else text[:200])
    run_dir: Path | None = None
    for _ in range(32):
        candidate = base / f"{timestamp}_{slug}_{_random6(random_hex)}"
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            run_dir = candidate
            break
        except FileExistsError:
            continue
    if run_dir is None:
        raise CxError("run 名を確保できませんでした", 3)
    with _new_lifecycle(run_dir, moment):
        artifact: Path | None = None
        lv0_id = ""
        reservation_root = base / ".lv0_ids"
        reservation_root.mkdir(parents=True, exist_ok=True)
        for offset in range(32):
            lv0_id = (moment + timedelta(seconds=offset)).strftime("%Y%m%d_%H%M%S")
            candidate_artifact = artifact_resolver(lv0_id).resolve()
            try:
                (reservation_root / lv0_id).mkdir(exist_ok=False)
            except FileExistsError:
                continue
            if candidate_artifact.exists():
                continue
            artifact = candidate_artifact
            break
        if artifact is None:
            raise CxError("Lv0 run ID を確保できませんでした", 3)
        lv0 = _command(
            runner,
            [
                sys.executable,
                str(lv0_script or Path(__file__).with_name("cgd_lv0_codex.py")),
                "prepare", "--workdir", str(project), "--run", lv0_id,
            ],
            project, 300,
        )
        if lv0.get("timeout") or lv0.get("exit_code") != 0:
            raise CxError(f"Lv0 prepare に失敗しました: {_failure_detail(lv0)}", 3)
        contract = load_lv0_prepare_contract(lv0_id, artifact, project)
        git = git_state(runner, repo, (base, run_dir))
        dirty_paths = git.get("dirty_paths", [])
        if not isinstance(dirty_paths, list) or not all(isinstance(path, str) for path in dirty_paths):
            raise CxError("git 状態の形式が不正です", 3)
        project_snapshot = snapshot(project)
        outside_paths, protected_paths = _dirty_groups(dirty_paths, repo, project, (base, run_dir))
        outside_files = _fingerprints(repo, outside_paths)
        protected = _fingerprints(repo, protected_paths)
        context = run_dir / "context"
        context.mkdir()
        memory_present, warnings = _copy_context(repo, memory, context)
        unstable_project = sorted(
            path for path, value in project_snapshot.items() if value.get("type") == "unstable"
        )
        unstable_outside = sorted(
            path for values in (outside_files, protected) for path, value in values.items()
            if value.get("type") == "unstable"
        )
        if unstable_project:
            warnings.append(f"走査中に変更あり: {len(unstable_project)} 件（委任先がまだ作業中の可能性）")
        if unstable_outside:
            warnings.append(f"不安定な対象外ファイル: {len(unstable_outside)} 件")
        baseline = {
            "schema": SCHEMA, "project": str(project), "repo_root": str(repo), "files": project_snapshot,
            "outside_files": outside_files, "protected_claude": protected, "git": git,
            "excluded_dirs": sorted(EXCLUDED_DIRS),
        }
        _write_new(run_dir / "baseline.json", _json_bytes(baseline))
        request_document = _request_document(
            project, repo, run_dir, raw, contract["secrets"], memory_present, git, go  # type: ignore[arg-type]
        )
        _write_new(run_dir / "request.md", request_document)
        state = {
            "schema": SCHEMA, "phase": "created", "created_at": moment.isoformat(), "project": str(project),
            "repo_root": str(repo), "run_dir": str(run_dir.resolve()), "base_dir": str(base),
            "arguments": {
                "request_file": _request_source_path(request_file),
                "request_argument": request is not None,
                "title": title,
                "go": go,
            },
            "request_sha256": hashlib.sha256(raw).hexdigest(), "lv0_run_id": lv0_id,
            "lv0_artifact": str(artifact), "memory_copied": memory_present, "warnings": warnings,
        }
        _write_new(run_dir / "state.json", _json_bytes(state))
        _append_event(run_dir, "created", moment)
    return run_dir / "request.md"


def _append_event(run_dir: Path, phase: str, moment: datetime) -> None:
    events = run_dir / "state-events"
    events.mkdir(exist_ok=True)
    sequence = len(list(events.glob("*.json"))) + 1
    _atomic_new(events / f"{sequence:04d}.json", _json_bytes({"schema": SCHEMA, "phase": phase, "at": moment.isoformat()}))


def _validate_run(run_arg: str) -> tuple[Path, dict[str, object], dict[str, object], Path, Path]:
    run_dir = Path(run_arg).resolve()
    if not run_dir.is_dir():
        raise CxError("run dir が見つかりません")
    state = _load_json(run_dir / "state.json")
    baseline = _load_json(run_dir / "baseline.json")
    if state.get("schema") != SCHEMA or baseline.get("schema") != SCHEMA or not _same_path(Path(str(state.get("run_dir", ""))), run_dir):
        raise CxError("run の schema/path が不正です")
    project = Path(str(state.get("project", ""))).resolve()
    repo = Path(str(state.get("repo_root", ""))).resolve()
    if not project.is_dir() or not repo.is_dir() or baseline.get("project") != str(project) or baseline.get("repo_root") != str(repo):
        raise CxError("run と project/repo が一致しません")
    _project_relative(project, repo)
    return run_dir, state, baseline, project, repo


def _diff(before: dict[str, object], after: dict[str, object]) -> dict[str, list[str]]:
    old, new = set(before), set(after)

    def modified(key: str) -> bool:
        left, right = before[key], after[key]
        if isinstance(left, dict) and isinstance(right, dict) and (
            left.get("type") == "unstable" or right.get("type") == "unstable"
        ):
            return left.get("type") == "missing" or right.get("type") == "missing"
        return left != right

    return {
        "added": sorted(new - old), "modified": sorted(key for key in old & new if modified(key)),
        "deleted": sorted(old - new),
    }


def _project_diff(before: dict[str, object], after: dict[str, object]) -> dict[str, list[str]]:
    result = _diff(before, after)
    result["incomparable"] = sorted(
        path for path in set(before) & set(after)
        if isinstance(before[path], dict) and before[path].get("type") == "unstable"
    )
    return result


def _tool_missing(result: dict[str, object]) -> bool:
    error = result.get("error")
    stderr = result.get("stderr", b"")
    return error in {"FileNotFoundError"} or (
        isinstance(stderr, bytes) and (b"No module named ruff" in stderr or b"No module named pytest" in stderr)
    )


def _check(name: str, command: Sequence[str], project: Path, timeout: int, runner: Runner) -> dict[str, object]:
    result = _command(runner, command, project, timeout)
    if _tool_missing(result):
        status, reason = "skipped", "tool unavailable"
    elif result.get("timeout"):
        status, reason = "failed", "timeout"
    elif result.get("exit_code") == 0:
        status, reason = "passed", "exit 0"
    elif name == "ensure_lf" and result.get("exit_code") == 2:
        status, reason = "failed", "検査の実行方法の異常"
    else:
        status, reason = "failed", "nonzero exit"
    return {"name": name, "status": status, "reason": reason, "exit_code": result.get("exit_code"), "duration_ms": result.get("duration_ms", 0)}


def _batches(paths: list[str], prefix: Sequence[str], limit: int = 7000) -> Iterable[list[str]]:
    batch: list[str] = []
    length = sum(len(part) + 1 for part in prefix)
    for path in paths:
        if batch and length + len(path) + 1 > limit:
            yield [*prefix, *batch]
            batch, length = [], sum(len(part) + 1 for part in prefix)
        batch.append(path)
        length += len(path) + 1
    if batch:
        yield [*prefix, *batch]


def _load_checks(path: Path | None) -> list[tuple[str, list[str], int]]:
    if path is None:
        return []
    value = _load_json(path).get("commands")
    if not isinstance(value, list):
        raise CxError("checks.commands は配列である必要があります")
    result: list[tuple[str, list[str], int]] = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]:
            raise CxError("check name が不正です")
        command, timeout = item.get("cmd"), item.get("timeout")
        if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command) or not isinstance(timeout, int) or timeout <= 0:
            raise CxError("check cmd/timeout が不正です")
        result.append((item["name"], command, timeout))
    return result


def _run_checks(
    project: Path,
    repo: Path,
    changed: list[str],
    checks_file: Path | None,
    timeout: int,
    runner: Runner,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    py = [path for path in changed if path.endswith(".py")]
    for index, command in enumerate(_batches(py, [sys.executable, "-m", "ruff", "check", "--no-cache"]), 1):
        results.append(_check(f"ruff-{index}", command, project, timeout, runner))
    for path in py:
        results.append(_check(f"py_compile:{path}", [sys.executable, "-m", "py_compile", path], project, timeout, runner))
    for path in changed:
        if path.endswith((".js", ".mjs")):
            results.append(_check(f"node:{path}", ["node", "--check", path], project, timeout, runner))
    tests = [path for path in py if Path(path).name.startswith("test_") or Path(path).name.endswith("_test.py")]
    for index, command in enumerate(_batches(tests, [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]), 1):
        results.append(_check(f"pytest-{index}", command, project, timeout, runner))
    lf_script = repo / ".claude" / "tools" / "ensure_lf.py"
    if lf_script.is_file() and not lf_script.is_symlink():
        text_paths: list[str] = []
        for relative in changed:
            path = project / relative
            try:
                data = path.read_bytes()
                if b"\0" not in data:
                    data.decode("utf-8")
                    text_paths.append(relative)
            except (OSError, UnicodeDecodeError):
                continue
        if text_paths:
            results.append(_check(
                "ensure_lf", [sys.executable, str(lf_script), "--check", *text_paths],
                project, timeout, runner,
            ))
        else:
            results.append({
                "name": "ensure_lf", "status": "skipped", "reason": "対象なし",
                "exit_code": None, "duration_ms": 0,
            })
    for name, command, timeout in _load_checks(checks_file):
        results.append(_check(name, command, project, timeout, runner))
    return results


def _secret_scan(project: Path, paths: list[str]) -> dict[str, object]:
    hit_files: list[str] = []
    hits = 0
    unscanned = 0
    for relative in paths:
        path = project / relative
        try:
            if path.is_symlink() or not path.is_file() or not _inside(path, project):
                unscanned += 1
                continue
            data = path.read_bytes()
        except OSError:
            unscanned += 1
            continue
        candidates = [data]
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            try:
                candidates.append(data.decode("utf-16").encode("utf-8"))
            except UnicodeError:
                unscanned += 1
                continue
        elif b"\0" in data:
            unscanned += 1
            continue
        file_hits = 0
        for candidate in candidates:
            for pattern in SECRET_PATTERNS:
                for _match in pattern.finditer(candidate):
                    file_hits += 1
        if file_hits:
            hits += file_hits
            hit_files.append(relative)
    return {"hits": hits, "files": sorted(set(hit_files)), "unscanned_files": unscanned}


def _metadata(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        return {"exists": False, "lines": 0}
    data = path.read_bytes()
    lines = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
    return {"exists": True, "lines": lines, "size": len(data)}


def _receipt_targets(run_dir: Path) -> tuple[int, Path, Path]:
    if any(run_dir.glob(".receipt*.tmp")):
        raise CxError("未完成の受領書一時ファイルがあります")
    pairs: dict[int, set[str]] = {}
    for path in run_dir.glob("receipt*.json"):
        match = re.fullmatch(r"receipt(?:\.(\d{4}))?\.json", path.name)
        if match:
            pairs.setdefault(int(match.group(1) or "1"), set()).add("json")
    for path in run_dir.glob("receipt*.txt"):
        match = re.fullmatch(r"receipt(?:\.(\d{4}))?\.txt", path.name)
        if match:
            pairs.setdefault(int(match.group(1) or "1"), set()).add("txt")
    if any(parts != {"json", "txt"} for parts in pairs.values()):
        raise CxError("未完成の受領書世代があります")
    if pairs and set(pairs) != set(range(1, max(pairs) + 1)):
        raise CxError("受領書の世代が連続していません")
    generation = max(pairs, default=0) + 1
    if generation == 1:
        return generation, run_dir / "receipt.json", run_dir / "receipt.txt"
    return generation, run_dir / f"receipt.{generation:04d}.json", run_dir / f"receipt.{generation:04d}.txt"


def receipt(
    run_arg: str,
    checks_file: Path | None,
    timeout: int = 300,
    *,
    runner: Runner = run_command,
    clock: Callable[[], datetime] = datetime.now,
) -> tuple[int, str]:
    if timeout <= 0:
        raise CxError("--timeout は正の整数で指定してください")
    run_dir, state, baseline, project, repo = _validate_run(run_arg)
    generation, json_path, text_path = _receipt_targets(run_dir)
    before_files = baseline.get("files")
    protected_before = baseline.get("protected_claude")
    outside_before = baseline.get("outside_files")
    baseline_git = baseline.get("git")
    if not all(isinstance(value, dict) for value in (before_files, protected_before, outside_before, baseline_git)):
        raise CxError("baseline の形式が不正です")
    baseline_ignored_paths = sorted({
        path for files in (outside_before, protected_before) for path in files
        if _ignored_change(path)
    })
    outside_before = {
        path: value for path, value in outside_before.items() if not _ignored_change(path)
    }
    protected_before = {
        path: value for path, value in protected_before.items() if not _ignored_change(path)
    }
    base_value = state.get("base_dir")
    ignored_roots = (run_dir, Path(base_value).resolve()) if isinstance(base_value, str) else (run_dir,)
    preliminary_git = git_state(runner, repo, ignored_roots)
    preliminary_dirty = preliminary_git.get("dirty_paths", [])
    baseline_dirty = baseline_git.get("dirty_paths", [])
    if not all(
        isinstance(paths, list) and all(isinstance(path, str) for path in paths)
        for paths in (preliminary_dirty, baseline_dirty)
    ):
        raise CxError("git 状態の形式が不正です")
    preliminary = snapshot(project)
    preliminary_diff = _project_diff(before_files, preliminary)
    targets = [
        relative for relative in (
            preliminary_diff["added"] + preliminary_diff["modified"]
            + preliminary_diff["incomparable"]
        )
        if not (project / relative).is_symlink()
        and (project / relative).is_file()
        and _inside(project / relative, project)
    ]
    checks = _run_checks(project, repo, targets, checks_file, timeout, runner)
    current_git = git_state(runner, repo, ignored_roots)
    current_dirty = current_git.get("dirty_paths", [])
    if not isinstance(current_dirty, list) or not all(isinstance(path, str) for path in current_dirty):
        raise CxError("git 状態の形式が不正です")
    final_files = snapshot(project)
    changes = _project_diff(before_files, final_files)
    changed_during_checks = any(_diff(preliminary, final_files).values())
    current_outside_paths, current_protected_paths = _dirty_groups(
        current_dirty, repo, project, ignored_roots
    )
    ignored_paths = sorted(set(baseline_ignored_paths) | set(
        _ignored_dirty_paths(current_dirty, repo, project, ignored_roots)
    ))
    outside_after = _fingerprints(repo, [*outside_before, *current_outside_paths])
    protected_after = _fingerprints(repo, [*protected_before, *current_protected_paths])
    outside_content = _diff(outside_before, outside_after)
    protected_changes = _diff(protected_before, protected_after)
    outside_git = sorted(set(current_outside_paths) - set(outside_before))
    protected_git = sorted(set(current_protected_paths) - set(protected_before))
    outside = sorted({path for values in outside_content.values() for path in values})
    protected = sorted({path for values in protected_changes.values() for path in values})
    current_unstable_project = sorted({
        path for files in (preliminary, final_files) for path, value in files.items()
        if value.get("type") == "unstable"
    })
    baseline_unstable_project = sorted(
        path for path, value in before_files.items()
        if isinstance(value, dict) and value.get("type") == "unstable"
    )
    unstable_project = sorted(set(current_unstable_project) | set(baseline_unstable_project))
    unstable_outside = sorted({
        path for files in (outside_after, protected_after) for path, value in files.items()
        if value.get("type") == "unstable"
    })
    outside_review = (
        {path for values in outside_content.values() for path in values} | set(outside_git)
    ) - set(unstable_outside)
    protected_review = (
        {path for values in protected_changes.values() for path in values} | set(protected_git)
    ) - set(unstable_outside)
    outside = sorted(set(outside) - set(unstable_outside))
    protected = sorted(set(protected) - set(unstable_outside))
    scanned_paths = changes["added"] + changes["modified"] + changes["incomparable"]
    secret_scan = _secret_scan(project, scanned_paths)
    plan = _metadata(run_dir / "plan.md")
    result = _metadata(run_dir / "result.md")
    backup_count = sum(
        1 for path in changes["added"]
        if re.search(r"\.bak_\d{8}_\d{6}$", Path(path).name)
    )
    reasons: list[str] = []
    check_invocation_error = any(
        item["name"] == "ensure_lf" and item["status"] == "failed" and item["exit_code"] == 2
        for item in checks
    )
    if check_invocation_error:
        reasons.append("検査の実行方法の異常")
    if any(
        item["status"] == "failed" and not (item["name"] == "ensure_lf" and item["exit_code"] == 2)
        for item in checks
    ):
        reasons.append("検査失敗")
    if changed_during_checks:
        reasons.append("検査中に対象が変更")
    if baseline_unstable_project:
        reasons.append(
            f"基準時点で不安定だったファイル {len(baseline_unstable_project)} 件は比較できない"
        )
    if current_unstable_project:
        reasons.append(f"走査中に変更あり: {len(current_unstable_project)} 件（委任先がまだ作業中の可能性）")
    if outside_review or protected_review:
        reasons.append("対象外または .claude の変更")
    if secret_scan["hits"]:
        reasons.append("秘密候補あり")
    if secret_scan["unscanned_files"]:
        reasons.append("秘密未検査ファイルあり")
    if not result["exists"]:
        reasons.append("result.md なし")
    if int(result["lines"]) > 40:
        reasons.append("result.md が40行超")
    reasons = reasons[:5]
    change_count = sum(len(changes[key]) for key in ("added", "modified", "deleted"))
    machine = "要確認" if reasons else (
        "変更なし（検査対象なし）" if change_count == 0 else "合格"
    )
    report = {
        "schema": SCHEMA, "generation": generation, "created_at": clock().isoformat(),
        "changes": changes, "outside_changes": {
            "count": len(outside), "paths": outside[:20], "content": outside_content,
            "git_paths": outside_git, "note": "他のセッションの可能性あり",
        },
        "protected_changes": {
            "count": len(protected), "paths": protected[:20], "content": protected_changes,
            "git_paths": protected_git,
        },
        "ignored": {"count": len(ignored_paths), "paths": ignored_paths},
        "checks": checks, "secret_scan": secret_scan,
        "unstable": {
            "count": len(unstable_project) + len(unstable_outside),
            "project": unstable_project, "outside": unstable_outside,
        },
        "plan": plan, "result": result, "backup_count": backup_count, "changed_during_checks": changed_during_checks,
        "judgement": {"machine_checks": machine, "request_fix": "未確認", "reasons": reasons},
    }
    passed = sum(item["status"] == "passed" for item in checks)
    executed = sum(item["status"] != "skipped" for item in checks)
    check_summary = f"{passed}/{executed}" if executed else "実行なし"
    review_paths = sorted(set(outside_review) | set(protected_review))
    summary = [
        f"機械検査: {machine}", "依頼の修正: 未確認（plan.md の完了条件と result.md をユーザーが確認）",
        f"変更: 追加{len(changes['added'])} 変更{len(changes['modified'])} 削除{len(changes['deleted'])} 比較不能{len(changes['incomparable'])}",
        f"検査: {check_summary}",
        f"対象外: {len(outside)} / 保護領域: {len(protected)} / 秘密候補: {secret_scan['hits']} / 未検査: {secret_scan['unscanned_files']}",
        f"不安定: {len(unstable_project) + len(unstable_outside)} 件",
        f"無視 {len(ignored_paths)} 件",
        f"バックアップ: {backup_count}",
    ]
    if review_paths:
        summary.append(f"対象外・保護領域のパス: {', '.join(review_paths[:3])}")
    if reasons:
        summary.append(f"要確認: {' / '.join(reasons)}")
    text = "\n".join(summary[:12]) + "\n"
    _atomic_new(json_path, _json_bytes(report))
    try:
        _atomic_new(text_path, text.encode("utf-8"))
    except BaseException as exc:
        raise CxError(f"受領書世代 {generation} の作成が未完了です") from exc
    _append_event(run_dir, "receipted_review" if machine == "要確認" else "receipted_ok", clock())
    return (20 if machine == "要確認" else 0), text


def status(run_arg: str) -> str:
    run_dir = Path(run_arg).resolve()
    if not run_dir.is_dir():
        raise CxError("run dir が見つかりません")
    events = sorted((run_dir / "state-events").glob("*.json")) if (run_dir / "state-events").is_dir() else []
    if not (run_dir / "state.json").is_file():
        if events and _load_json(events[-1]).get("phase") == "new_failed":
            names = ("request.md", "plan.md", "result.md", "receipt.json")
            return "\n".join(["phase: new_failed", *(f"{name}: {'yes' if (run_dir / name).is_file() else 'no'}" for name in names)]) + "\n"
        raise CxError("state.json が見つかりません")
    run_dir, state, _baseline, _project, _repo = _validate_run(run_arg)
    phase = str(state.get("phase", "unknown"))
    if events:
        phase = str(_load_json(events[-1]).get("phase", phase))
    names = ("request.md", "plan.md", "result.md", "receipt.json")
    return "\n".join([f"phase: {phase}", *(f"{name}: {'yes' if (run_dir / name).is_file() else 'no'}" for name in names)]) + "\n"


class CxArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = CxArgumentParser(prog="cgd_cx.py")
    commands = parser.add_subparsers(dest="command", required=True)
    new = commands.add_parser("new", allow_abbrev=False)
    new.add_argument("--project", required=True)
    request_source = new.add_mutually_exclusive_group(required=True)
    request_source.add_argument("--request")
    request_source.add_argument("--request-file", type=Path)
    new.add_argument("--title")
    new.add_argument("--go", action="store_true")
    new.add_argument("--base-dir", type=Path, default=DEFAULT_BASE)
    new.add_argument("--memory-dir", type=Path)
    receive = commands.add_parser("receipt", allow_abbrev=False)
    receive.add_argument("--run", required=True)
    receive.add_argument("--timeout", type=int, default=300)
    receive.add_argument("--checks", type=Path)
    show = commands.add_parser("status", allow_abbrev=False)
    show.add_argument("--run", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "new":
            request_path = new_run(
                args.project, args.request_file, args.title, args.base_dir, args.memory_dir,
                request=args.request, go=args.go,
            )
            new_state = _load_json(request_path.parent / "state.json")
            warnings = new_state.get("warnings", [])
            if isinstance(warnings, list):
                for warning in warnings:
                    print(f"warning: {warning}", file=sys.stderr)
            print("デバッグを開始してください。最初に次のファイル全体を読み、その指示に従ってください。")
            print(request_path.resolve().as_posix())
            return 0
        if args.command == "receipt":
            code, output = receipt(args.run, args.checks, args.timeout)
            sys.stdout.write(output)
            return code
        sys.stdout.write(status(args.run))
        return 0
    except CxError as exc:
        print(str(exc), file=sys.stderr)
        return 1 if args.command in {"receipt", "status"} else exc.code
    except Exception as exc:
        detail = _redacted_output(str(exc))
        print(f"予期しないエラー: {type(exc).__name__}: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
