"""cgd Lv0 — Codex に作業フォルダ内の実装を任せるための補助.

cgd/SKILL.md の Lv0 節から呼ぶ。手順を決定論的にして、毎回の書き起こしで
条件判定を端折らないようにする（pv_plan.py と同じ考え方）。

サブコマンド:
    resolve                                   使う Codex CLI を選び、直近の利用枠を表示する
    prepare --workdir D --run R               作業フォルダの点検・秘密情報らしいファイル・実行前の写し
    run     --workdir D --run R --spec F      Codex に実装させる（tokens・利用枠を記録）
    diff    --workdir D --run R               写しと比べた差分・.bak 作成・行数集計
    restore --workdir D --run R [--apply]     写しから戻す（新規ファイルは消さない・第三者の変更は上書きしない）

背景（2026-09-18 実測）:
    - Codex CLI の Windows サンドボックス（elevated）は補助 exe を起動する。npm を winget 版 Node の
      下に入れると補助 exe のパスが 273 文字になり、MAX_PATH(260) を超えて起動できない
      （INC-20260918-155756c98ac7）。C:\\tools\\codex-cli に入れ直すと 160 文字で動く。
    - サンドボックスの実行ユーザー（CodexSandboxOffline）はユーザープロファイル配下の Python を
      起動できない。だからテストは Codex ではなく Claude が回す。
    - C:\\ClaudeCode は未コミット変更が常に多く並行セッションもある。worktree（HEAD 基準）では
      古いコードを直すことになるので、対象サブフォルダを作業フォルダにし、実行前の写しと比べる。

写しと巻き戻しの範囲（誇張しないこと）:
    - 写すのは 2MB 以下の通常ファイル。巻き戻せるのはこれだけ
    - 秘密情報らしいファイルと 2MB 超のファイルは写さない（ハッシュ/大きさだけ記録）。変化は検知するが戻せない
    - EXCLUDE_DIRS 配下と .bak ファイルは見ない（写しも差分も無い）
    - Codex が新しく作ったファイルは restore でも消さない（一覧を出して人が判断する）
"""

from __future__ import annotations

import argparse
import difflib
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_NO_CODEX = 2
EXIT_CODEX_FAILED = 3
EXIT_TIMEOUT = 30  # cgd の exit code 規約（advisor スクリプトと同じ）

MAX_PATH = 260
HELPER_NAME = "codex-windows-sandbox-setup.exe"
NPM_PACKAGE_REL = Path("node_modules/@openai/codex")
NPM_VENDOR_REL = Path("node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc")
PREFERRED_BIN = Path(r"C:\tools\codex-cli\codex.cmd")
RUNS_BASE = Path("C:/tmp-ai")
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
WEEK_MINUTES = 10080

# 見ないフォルダ（依存物・キャッシュ）。ここは写しも差分も無いので、依頼文でも触らせない
EXCLUDE_DIRS = frozenset({
    ".git", "node_modules", ".venv", "venv", "env", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
})
BACKUP_RE = re.compile(r"\.bak(_|$)")
MAX_SNAPSHOT_FILE = 2 * 1024 * 1024
MAX_SNAPSHOT_TOTAL = 200 * 1024 * 1024

# 名前で判定する（中身は表示しない）
SECRET_NAME_PATTERNS: tuple[tuple[str, str], ...] = (
    (".env", "環境変数"), (".env.*", "環境変数"), ("*.env", "環境変数"),
    ("*.pem", "鍵"), ("*.key", "鍵"), ("*.pfx", "鍵"), ("*.p12", "鍵"),
    ("id_rsa*", "鍵"), ("id_ed25519*", "鍵"),
    ("*credential*", "認証情報"), ("*secret*", "認証情報"), ("*token*.json", "認証情報"),
    ("auth.json", "認証情報"), ("*.kdbx", "認証情報"),
    ("*.db", "業務データ"), ("*.sqlite", "業務データ"), ("*.sqlite3", "業務データ"),
)
# 中身で判定する（一致した文字列そのものは記録も表示もしない）
SECRET_CONTENT_PATTERNS = tuple(re.compile(p) for p in (
    r"sk-[A-Za-z0-9_\-]{20,}",
    r"AKIA[0-9A-Z]{16}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"gh[pousr]_[A-Za-z0-9]{30,}",
    r"xox[baprs]-[A-Za-z0-9-]{10,}",
    r"(?i)(api[_-]?key|secret|passw(or)?d|token)\s*[:=]\s*['\"][^'\"\s]{8,}['\"]",
))

RUN_ID_RE = re.compile(r"^\d{8}_\d{6}$")
TOKENS_RE = re.compile(r"tokens used\s*\r?\n\s*([\d,]+)")
SESSION_RE = re.compile(r"session id:\s*([0-9a-f]{8}-[0-9a-f-]{27,})")
SANDBOX_ERROR_MARKERS = (
    "orchestrator_helper_launch_failed",
    "CreateProcessAsUserW failed",
    "blocked by policy",
)

PREAMBLE = """あなたは実装担当です。以下の【仕様】に従い、現在の作業フォルダ内のファイルだけを編集・作成してください。

守ること:
- 作業フォルダの外は編集しない
- テスト・python・node などの実行はしない。サンドボックスからは起動できない。検証は依頼元（Claude Code）が行う
- 秘密情報らしいファイル（.env、鍵、認証情報、DB ファイル等）は開かない
- 作業フォルダの外のファイルは読まない。上位フォルダの AGENTS.md が読み込まれても、そこに書かれた .claude/ 配下のスキルや手順書（cgd の SKILL.md など）を開いて従うことはしない。指示はこの【仕様】だけ
- 依存物・キャッシュのフォルダ（node_modules、.venv、.git など）と .bak ファイルは変更しない
- 既存コードの書き方・命名・コメント量に合わせる。仕様と無関係な整形・改名・削除をしない
- Python: shebang 行を書かない / ファイル I/O は encoding="utf-8" を明示 / 型ヒントを付ける / Python 3.12
- 既存ファイルの文字コードと改行コードを変えない。新規ファイルは UTF-8・LF
- バックアップ（.bak）は依頼元が作るので作らない
- 仕様があいまいで判断が要る箇所は推測で進めず、その部分は実装せずに最終報告で質問する

最終報告（短く・日本語）:
1. 変更・作成したファイルの一覧（1 行ずつ、何を変えたか）
2. 依頼元に実行してほしい検証（コマンド）
3. 未解決事項・質問（無ければ「なし」）

【仕様】
"""


# ---------------------------------------------------------------- CLI の選択


@dataclass
class BinChoice:
    exe: Path | None
    env: dict[str, str] = field(default_factory=dict)
    source: Path | None = None
    rejected: list[str] = field(default_factory=list)


def native_exe_for(candidate: Path) -> tuple[Path, dict[str, str]] | None:
    """shim（codex.cmd）なら本体の codex.exe と、shim が付ける環境変数を返す。

    shim を起動すると cmd.exe が引数を解釈し直す（& や % を含むパスで誤動作しうる）ので、
    本体を直接起動する。環境変数は bin/codex.js が付けているものと同じ。
    """
    if candidate.suffix.lower() == ".exe":
        return candidate, {}
    package = candidate.parent / NPM_PACKAGE_REL
    exe = package / NPM_VENDOR_REL / "bin" / "codex.exe"
    if not exe.exists():
        return None
    return exe, {"CODEX_MANAGED_PACKAGE_ROOT": str(package.resolve()), "CODEX_MANAGED_BY_NPM": "1"}


def helper_path_for(exe: Path) -> Path | None:
    """本体 exe に対応するサンドボックス補助 exe（無ければ None）。"""
    for helper in (exe.parent / HELPER_NAME, exe.parent.parent / "codex-resources" / HELPER_NAME):
        if helper.exists():
            return helper
    return None


def candidate_bins() -> list[Path]:
    found: list[Path] = []
    env = os.environ.get("CGD_CODEX_BIN")
    if env:
        found.append(Path(env))
    found.append(PREFERRED_BIN)
    which = shutil.which("codex")
    if which:
        found.append(Path(which))
    unique: list[Path] = []
    seen: set[str] = set()
    for p in found:
        p = p.absolute()
        key = os.path.normcase(str(p))
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def choose_bin(candidates: list[Path], max_path: int = MAX_PATH, windows: bool = os.name == "nt") -> BinChoice:
    """補助 exe を起動できる CLI を選ぶ。Windows 以外は最初に実在するものを使う。"""
    choice = BinChoice(exe=None)
    for cand in candidates:
        if not cand.exists():
            choice.rejected.append(f"{cand}: 存在しない")
            continue
        if not windows:
            choice.exe, choice.source = cand, cand
            return choice
        native = native_exe_for(cand)
        if native is None:
            choice.rejected.append(f"{cand}: 本体の codex.exe が見つからない")
            continue
        exe, env = native
        helper = helper_path_for(exe)
        if helper is None:
            choice.rejected.append(f"{cand}: サンドボックス補助 exe が見つからない")
            continue
        if len(str(helper)) >= max_path:
            choice.rejected.append(
                f"{cand}: 補助 exe のパスが {len(str(helper))} 文字（{max_path} 以上）で起動できない"
            )
            continue
        choice.exe, choice.env, choice.source = exe, env, cand
        return choice
    return choice


# ---------------------------------------------------------------- 作業フォルダ


def validate_run_id(run: str) -> None:
    if not RUN_ID_RE.match(run):
        raise ValueError(f"--run は YYYYMMDD_HHMMSS 形式にしてください（{run!r}）")


def _git_root(path: Path) -> Path | None:
    for p in (path, *path.parents):
        if (p / ".git").exists():
            return p
    return None


def _is_link(p: Path) -> bool:
    return p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction())


def validate_workdir(workdir: Path) -> list[str]:
    """作業フォルダとして不適切な理由を返す（空なら OK）。

    許すのは「git リポジトリの内側」のフォルダだけ。一番外側のリポジトリの最上位・
    プロジェクト最上位（.claude を含む）・ドライブ直下・ホーム・UNC・リンク経由は拒否する。
    """
    if str(workdir).startswith(("\\\\", "//")):
        return ["UNC パスは作業フォルダにできない"]
    if not workdir.is_dir():
        return [f"フォルダが無い: {workdir}"]
    resolved = workdir.resolve()
    errors: list[str] = []
    if resolved.parent == resolved:
        errors.append("ドライブ直下は作業フォルダにできない")
    if resolved == Path.home().resolve():
        errors.append("ホームフォルダは作業フォルダにできない")
    if (resolved / ".claude").is_dir():
        errors.append("プロジェクトの最上位（.claude を含むフォルダ）は広すぎる。対象のサブフォルダを指定する")
    root = _git_root(resolved)
    if root is None:
        errors.append("git リポジトリの外は作業フォルダにできない")
    elif root == resolved and _git_root(root.parent) is None:
        errors.append("リポジトリの最上位は広すぎる。対象のサブフォルダを指定する")
    if root is not None:
        p = workdir.absolute()
        while True:
            if _is_link(p):
                errors.append(f"リンク（シンボリックリンク/ジャンクション）を経由している: {p}")
                break
            if p.resolve() == root or p.parent == p:
                break
            p = p.parent
    return errors


def iter_files(root: Path):
    """root 配下のファイルを相対パスで返す（EXCLUDE_DIRS・.bak・シンボリックリンクは除く）。"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
        for name in sorted(filenames):
            if BACKUP_RE.search(name):
                continue
            full = Path(dirpath) / name
            if full.is_symlink():
                continue
            yield full.relative_to(root)


def excluded_dirs_present(root: Path) -> list[str]:
    found: list[str] = []
    for dirpath, dirnames, _ in os.walk(root):
        for d in list(dirnames):
            if d in EXCLUDE_DIRS:
                found.append((Path(dirpath) / d).relative_to(root).as_posix())
                dirnames.remove(d)
    return sorted(found)


def secret_kind_by_name(name: str) -> str | None:
    lower = name.lower()
    for pattern, kind in SECRET_NAME_PATTERNS:
        if fnmatch.fnmatch(lower, pattern):
            return kind
    return None


def content_secret_hits(data: bytes) -> int:
    """鍵らしき文字列の件数（文字列そのものは返さない）。"""
    if b"\0" in data:
        return 0
    text = data.decode("utf-8", errors="replace")
    return sum(len(p.findall(text)) for p in SECRET_CONTENT_PATTERNS)


def scan_secrets(root: Path, max_file: int = MAX_SNAPSHOT_FILE) -> list[tuple[str, str]]:
    """秘密情報らしいファイルを (相対パス, 種類) で返す。中身の一致は件数だけ見る。"""
    hits: list[tuple[str, str]] = []
    for rel in iter_files(root):
        kind = secret_kind_by_name(rel.name)
        if kind is None:
            path = root / rel
            if path.stat().st_size <= max_file:
                n = content_secret_hits(path.read_bytes())
                if n:
                    kind = f"中身に鍵らしき文字列 {n} 件"
        if kind:
            hits.append((rel.as_posix(), kind))
    return hits


# ---------------------------------------------------------------- 写しと差分


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def take_snapshot(
    root: Path,
    dest: Path,
    secrets: set[str] | None = None,
    max_file: int = MAX_SNAPSHOT_FILE,
    max_total: int = MAX_SNAPSHOT_TOTAL,
) -> dict:
    """root の写しを dest に取り、照合用の目録を返す。

    秘密情報らしいファイル（secrets）は写さずハッシュだけ、2MB 超は大きさと更新時刻だけ記録する。
    写したファイルのハッシュは**写しの側**から取る（コピー中に原本が変わっても目録と写しが食い違わない）。
    """
    secrets = secrets or set()
    files: dict[str, dict] = {}
    untracked: dict[str, dict] = {}
    total = 0
    for rel in iter_files(root):
        src = root / rel
        st = src.stat()
        key = rel.as_posix()
        if key in secrets:
            untracked[key] = {"reason": "secret", "sha256": _sha256(src), "size": st.st_size}
            continue
        if st.st_size > max_file:
            untracked[key] = {"reason": "large", "size": st.st_size, "mtime_ns": st.st_mtime_ns}
            continue
        total += st.st_size
        if total > max_total:
            raise ValueError(
                f"写しが {max_total // (1024 * 1024)}MB を超える。作業フォルダをもっと絞ってください"
            )
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
        files[key] = {"sha256": _sha256(out), "size": out.stat().st_size}
    return {"files": files, "untracked": untracked, "total_bytes": total}


def _decode(data: bytes) -> str | None:
    if b"\0" in data:
        return None
    for enc in ("utf-8", "cp932"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def newline_style(data: bytes) -> str:
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n") - crlf
    if crlf and lf:
        return "mixed"
    if crlf:
        return "crlf"
    if lf:
        return "lf"
    return "none"


def _lines(text: str) -> list[str]:
    parts = text.split("\n")
    if parts and parts[-1] == "":
        parts.pop()
    return [p[:-1] if p.endswith("\r") else p for p in parts]


def _file_diff(rel: str, before: bytes, after: bytes) -> tuple[list[str], list[str], int, int]:
    """(diff 行, 注記, 追加行数, 削除行数) を返す。"""
    notes: list[str] = []
    a, b = _decode(before), _decode(after)
    if a is None or b is None:
        return [], [f"{rel}: バイナリのため差分なし（変化あり）"], 0, 0
    sa, sb = newline_style(before), newline_style(after)
    if "none" not in (sa, sb) and sa != sb:
        notes.append(f"{rel}: 改行コードが変わった（{sa} → {sb}）")
    lines = list(difflib.unified_diff(
        _lines(a), _lines(b), fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="",
    ))
    body = [ln for ln in lines[2:] if not ln.startswith("@@")]
    plus = sum(1 for ln in body if ln.startswith("+"))
    minus = sum(1 for ln in body if ln.startswith("-"))
    if not lines and before != after:
        notes.append(f"{rel}: 改行・末尾だけの変化")
    return lines, notes, plus, minus


def diff_snapshot(root: Path, snapdir: Path, manifest: dict, max_file: int = MAX_SNAPSHOT_FILE) -> dict:
    files: dict = manifest["files"]
    untracked: dict = manifest["untracked"]
    current = {rel.as_posix(): rel for rel in iter_files(root)}
    out: dict = {
        "modified": [], "added": [], "deleted": [], "untracked_changed": [],
        "notes": [], "plus": 0, "minus": 0, "after": {},
    }
    patch: list[str] = []

    def add_diff(key: str, before: bytes, after: bytes) -> None:
        lines, notes, plus, minus = _file_diff(key, before, after)
        patch.extend(lines)
        out["notes"].extend(notes)
        out["plus"] += plus
        out["minus"] += minus

    for key, meta in files.items():
        if key not in current:
            out["deleted"].append(key)
            add_diff(key, (snapdir / key).read_bytes(), b"")
            continue
        now = root / current[key]
        digest = _sha256(now)
        if digest == meta["sha256"]:
            continue
        out["modified"].append(key)
        out["after"][key] = digest
        add_diff(key, (snapdir / key).read_bytes(), now.read_bytes())

    for key, meta in untracked.items():
        label = "秘密情報らしいファイル" if meta["reason"] == "secret" else "2MB 超"
        if key not in current:
            out["untracked_changed"].append(f"{key}（削除・{label}・写し無し）")
            continue
        path = root / current[key]
        st = path.stat()
        if meta["reason"] == "secret":
            changed = _sha256(path) != meta["sha256"]
        else:
            changed = st.st_size != meta["size"] or st.st_mtime_ns != meta["mtime_ns"]
        if changed:
            out["untracked_changed"].append(f"{key}（変更・{label}・写し無し）")

    for key, rel in current.items():
        if key in files or key in untracked:
            continue
        path = root / rel
        size = path.stat().st_size
        if size > max_file or secret_kind_by_name(rel.name):
            out["untracked_changed"].append(f"{key}（新規・差分に含めない・{size} bytes）")
            continue
        data = path.read_bytes()
        if content_secret_hits(data):
            out["untracked_changed"].append(f"{key}（新規・中身に鍵らしき文字列・差分に含めない）")
            continue
        out["added"].append(key)
        out["after"][key] = hashlib.sha256(data).hexdigest()
        add_diff(key, b"", data)

    out["patch"] = "\n".join(patch) + ("\n" if patch else "")
    return out


def is_unchanged(result: dict) -> bool:
    return not (result["modified"] or result["added"] or result["deleted"] or result["untracked_changed"])


def write_baks(root: Path, snapdir: Path, rels: list[str], run: str) -> list[str]:
    """変更・削除されたファイルの実行前の中身を <file>.bak_<run> として残す（AGENTS.md の規約）。"""
    made: list[str] = []
    for rel in rels:
        target = root / f"{rel}.bak_{run}"
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snapdir / rel, target)
        made.append(target.relative_to(root).as_posix())
    return made


def restore(root: Path, snapdir: Path, recorded: dict, apply: bool) -> list[str]:
    """diff 時に記録した状態のファイルだけを写しから戻す。

    - 変更: 今の中身が diff 時の中身と同じときだけ戻す（その後に誰かが変えていたら上書きしない）
    - 削除: まだ無いときだけ戻す
    - 新規: 消さない（一覧だけ）。写しの無いファイルは戻せない旨を出す
    """
    actions: list[str] = []
    after: dict = recorded.get("after", {})
    for rel in recorded["modified"]:
        dest = root / rel
        if not dest.is_file():
            actions.append(f"戻さない（種類が変わった・要判断）: {rel}")
            continue
        if _sha256(dest) != after.get(rel):
            actions.append(f"戻さない（diff 後に変わった・要判断）: {rel}")
            continue
        actions.append(f"戻す: {rel}")
        if apply:
            shutil.copy2(snapdir / rel, dest)
    for rel in recorded["deleted"]:
        dest = root / rel
        if dest.exists():
            actions.append(f"戻さない（既に何かある・要判断）: {rel}")
            continue
        actions.append(f"戻す: {rel}")
        if apply:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(snapdir / rel, dest)
    for rel in recorded["added"]:
        actions.append(f"新規（消さない・要判断）: {rel}")
    for item in recorded["untracked_changed"]:
        actions.append(f"戻せない（写し無し）: {item}")
    return actions


# ---------------------------------------------------------------- 依頼と記録


def build_prompt(spec: str) -> str:
    return PREAMBLE + spec.strip() + "\n"


def parse_codex_stderr(text: str) -> dict:
    tokens = TOKENS_RE.search(text)
    session = SESSION_RE.search(text)
    return {
        "tokens": int(tokens.group(1).replace(",", "")) if tokens else None,
        "session_id": session.group(1) if session else None,
        "sandbox_errors": sum(text.count(m) for m in SANDBOX_ERROR_MARKERS),
    }


def _windows(rate_limits: dict) -> list[dict]:
    out: list[dict] = []
    for slot in ("primary", "secondary"):
        w = rate_limits.get(slot) or {}
        if w.get("used_percent") is not None:
            out.append({"slot": slot, "window_minutes": w.get("window_minutes"),
                        "used_percent": float(w["used_percent"])})
    return out


def _last_rate_limits(path: Path) -> list[dict]:
    found: list[dict] = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"token_count"' not in line or '"rate_limits"' not in line:
                continue
            try:
                payload = json.loads(line).get("payload", {})
            except json.JSONDecodeError:
                continue
            windows = _windows(payload.get("rate_limits") or {})
            if windows:
                found = windows
    return found


def session_rate_limits(session_id: str | None, codex_home: Path = CODEX_HOME) -> list[dict]:
    """そのセッションの記録にある最後の利用枠（primary/secondary）。無ければ空。"""
    if not session_id:
        return []
    hits = sorted((codex_home / "sessions").glob(f"*/*/*/rollout-*{session_id}.jsonl"))
    return _last_rate_limits(hits[-1]) if hits else []


def latest_rate_limits(codex_home: Path = CODEX_HOME, look: int = 8) -> list[dict]:
    """直近のセッション記録から最新の利用枠を探す（run 前の判断用）。"""
    files = sorted((codex_home / "sessions").glob("*/*/*/rollout-*.jsonl"), key=lambda p: p.name, reverse=True)
    for path in files[:look]:
        windows = _last_rate_limits(path)
        if windows:
            return windows
    return []


def weekly_percent(windows: list[dict]) -> float | None:
    """週（10080 分）の枠の使用率。週の枠が見つからなければ None（短期枠と取り違えない）。"""
    for w in windows:
        if w.get("window_minutes") == WEEK_MINUTES:
            return w["used_percent"]
    return None


def describe_limits(windows: list[dict]) -> str:
    if not windows:
        return "不明"
    parts = []
    for w in windows:
        m = w.get("window_minutes")
        label = "週" if m == WEEK_MINUTES else (f"{m // 60}時間" if isinstance(m, int) and m >= 60 else f"{m}分")
        parts.append(f"{label} {w['used_percent']:.0f}%")
    return " / ".join(parts)


def _kill_tree(proc: subprocess.Popen) -> str:
    """プロセスツリーを止める。どこも無期限には待たない。"""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=30)
        else:
            proc.kill()
        proc.wait(timeout=30)
        return "killed"
    except (subprocess.SubprocessError, OSError):
        try:
            proc.kill()
            proc.wait(timeout=10)
            return "killed(fallback)"
        except (subprocess.SubprocessError, OSError):
            return "kill_failed"


def run_codex(choice: BinChoice, workdir: Path, prompt: Path, rundir: Path, effort: str, timeout: int) -> dict:
    cmd = [
        str(choice.exe), "exec", "--sandbox", "workspace-write", "--skip-git-repo-check",
        "-C", str(workdir), "-c", f"model_reasoning_effort={effort}",
        "-o", str(rundir / "last.txt"), "-",
    ]
    env = {**os.environ, **choice.env}
    started = time.monotonic()
    info: dict = {"timed_out": False, "kill": None}
    with prompt.open("rb") as fin, (rundir / "stdout.txt").open("wb") as fout, \
            (rundir / "stderr.txt").open("wb") as ferr:
        try:
            proc = subprocess.Popen(cmd, stdin=fin, stdout=fout, stderr=ferr, env=env)
        except OSError as exc:
            return {**info, "exit": None, "launch_error": str(exc), "seconds": 0,
                    "bin": str(choice.exe), "effort": effort, "tokens": None,
                    "session_id": None, "sandbox_errors": 0, "rate_limits": [], "weekly_used_percent": None}
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            info["timed_out"] = True
            info["kill"] = _kill_tree(proc)
            code = proc.returncode
        except BaseException:
            info["kill"] = _kill_tree(proc)
            raise
    stderr = (rundir / "stderr.txt").read_text(encoding="utf-8", errors="replace")
    info.update(parse_codex_stderr(stderr))
    windows = session_rate_limits(info["session_id"])
    info.update({
        "exit": code, "launch_error": None, "seconds": round(time.monotonic() - started),
        "bin": str(choice.exe), "effort": effort,
        "rate_limits": windows, "weekly_used_percent": weekly_percent(windows),
    })
    return info


# ---------------------------------------------------------------- CLI


def _rundir(run: str) -> Path:
    return RUNS_BASE / f"cgd_lv0_{run}"


def _load_prepare(run: str, workdir: Path) -> tuple[Path, dict, dict]:
    rundir = _rundir(run)
    meta_path = rundir / "prepare.json"
    if not meta_path.exists():
        raise ValueError(f"先に prepare を実行してください（{meta_path} が無い）")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if os.path.normcase(meta["workdir"]) != os.path.normcase(str(workdir.resolve())):
        raise ValueError(f"--workdir が prepare 時と違う（prepare: {meta['workdir']}）")
    manifest = json.loads((rundir / "manifest.json").read_text(encoding="utf-8"))
    return rundir, meta, manifest


def _save(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="")


def cmd_resolve(_: argparse.Namespace) -> int:
    choice = choose_bin(candidate_bins())
    for r in choice.rejected:
        print(f"  見送り: {r}")
    print(f"直近の Codex 利用枠: {describe_limits(latest_rate_limits())}")
    if choice.exe is None:
        print("NG: 使える Codex CLI が無い。C:\\tools\\codex-cli に入れ直す"
              "（npm install -g @openai/codex --prefix C:\\tools\\codex-cli）")
        return EXIT_NO_CODEX
    print(f"OK: {choice.source} → {choice.exe}")
    return EXIT_OK


def cmd_prepare(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir)
    errors = validate_workdir(workdir)
    if errors:
        for e in errors:
            print(f"NG: {e}")
        return EXIT_GENERIC
    rundir = _rundir(args.run)
    if rundir.exists():
        print(f"NG: {rundir} は既にある。別の --run を使う")
        return EXIT_GENERIC
    secrets = scan_secrets(workdir)
    rundir.mkdir(parents=True)
    try:
        manifest = take_snapshot(workdir, rundir / "before", secrets={p for p, _ in secrets})
    except BaseException:
        shutil.rmtree(rundir, ignore_errors=True)  # 自分で作った一時フォルダだけを消す
        raise
    excluded = excluded_dirs_present(workdir)
    _save(rundir / "manifest.json", manifest)
    _save(rundir / "prepare.json", {
        "workdir": str(workdir.resolve()), "run": args.run,
        "secrets": [{"path": p, "kind": k} for p, k in secrets], "excluded_dirs": excluded,
    })
    print(f"写し: {rundir / 'before'}（{len(manifest['files'])} ファイル・"
          f"{manifest['total_bytes'] // 1024} KB。巻き戻せるのはこの範囲だけ）")
    large = [k for k, v in manifest["untracked"].items() if v["reason"] == "large"]
    if large:
        print(f"2MB 超のため写さない（変化は検知・戻せない）: {len(large)} 件")
    if excluded:
        print(f"見ないフォルダ（写しも差分も無い）: {', '.join(excluded)}")
    if secrets:
        print(f"要確認・秘密情報らしいファイル（Codex が開ける場所にある。写しには複製しない）: {len(secrets)} 件")
        for p, k in secrets:
            print(f"  [{k}] {p}")
    else:
        print("秘密情報らしいファイル: なし（名前と中身の簡易判定）")
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir)
    rundir, _, manifest = _load_prepare(args.run, workdir)
    marker = rundir / "run.started"
    if marker.exists():
        print(f"NG: この RUN は実行済み（{marker}）。やり直すときは新しい RUN で prepare から")
        return EXIT_GENERIC
    baseline = diff_snapshot(workdir, rundir / "before", manifest)
    if not is_unchanged(baseline):
        print("NG: prepare の後に作業フォルダが変わっている（他のセッション？）。新しい RUN で prepare からやり直す")
        for label in ("modified", "added", "deleted", "untracked_changed"):
            for rel in baseline[label]:
                print(f"  {label}: {rel}")
        return EXIT_GENERIC
    choice = choose_bin(candidate_bins())
    if choice.exe is None:
        for r in choice.rejected:
            print(f"  見送り: {r}")
        print("NG: 使える Codex CLI が無い（resolve を参照）")
        return EXIT_NO_CODEX
    spec = Path(args.spec).read_text(encoding="utf-8")
    prompt = rundir / "prompt.txt"
    prompt.write_text(build_prompt(spec), encoding="utf-8", newline="")
    marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S") + "\n", encoding="utf-8", newline="")
    info = run_codex(choice, workdir.resolve(), prompt, rundir, args.effort, args.timeout)
    _save(rundir / "run.json", info)
    print(f"exit={info['exit']} 秒={info['seconds']} tokens={info['tokens']} "
          f"利用枠={describe_limits(info['rate_limits'])} sandbox_errors={info['sandbox_errors']}")
    print(f"最終報告: {rundir / 'last.txt'}")
    if info["launch_error"]:
        print(f"NG: Codex を起動できない: {info['launch_error']}")
        return EXIT_CODEX_FAILED
    if info["timed_out"]:
        print(f"NG: {args.timeout} 秒で打ち切った（後始末: {info['kill']}）")
        return EXIT_TIMEOUT
    if info["exit"] != 0 or info["sandbox_errors"]:
        print(f"NG: Codex の実行に失敗（{rundir / 'stderr.txt'} を確認）")
        return EXIT_CODEX_FAILED
    return EXIT_OK


def cmd_diff(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir)
    rundir, _, manifest = _load_prepare(args.run, workdir)
    result = diff_snapshot(workdir, rundir / "before", manifest)
    (rundir / "changes.patch").write_text(result["patch"], encoding="utf-8", newline="")
    baks = [] if args.no_bak else write_baks(
        workdir, rundir / "before", result["modified"] + result["deleted"], args.run)
    summary = {k: v for k, v in result.items() if k != "patch"}
    summary["baks"] = baks
    _save(rundir / "diff.json", summary)
    print(f"変更 {len(result['modified'])} / 新規 {len(result['added'])} / 削除 {len(result['deleted'])}"
          f"  +{result['plus']} -{result['minus']} 行")
    for label in ("modified", "added", "deleted", "untracked_changed"):
        for rel in result[label]:
            print(f"  {label}: {rel}")
    for note in result["notes"]:
        print(f"  注意: {note}")
    if baks:
        print(f"バックアップ: {len(baks)} 件（<file>.bak_{args.run}）")
    print(f"差分: {rundir / 'changes.patch'}")
    return EXIT_OK


def cmd_restore(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir)
    rundir, _, _ = _load_prepare(args.run, workdir)
    recorded_path = rundir / "diff.json"
    if not recorded_path.exists():
        raise ValueError("先に diff を実行してください（戻す対象は diff 時の記録で決める）")
    recorded = json.loads(recorded_path.read_text(encoding="utf-8"))
    for line in restore(workdir, rundir / "before", recorded, args.apply):
        print(line)
    if not args.apply:
        print("（確認のみ。戻すには --apply）")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="cgd Lv0: Codex に作業フォルダ内の実装を任せる")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("resolve", help="使う Codex CLI を選び、直近の利用枠を表示する")
    for name, help_text in (
        ("prepare", "作業フォルダの点検と実行前の写し"),
        ("run", "Codex に実装させる"),
        ("diff", "写しと比べた差分と .bak 作成"),
        ("restore", "写しから戻す（既定は確認のみ）"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--workdir", required=True)
        p.add_argument("--run", required=True, help="YYYYMMDD_HHMMSS（date で取得した値をそのまま書く）")
        if name == "run":
            p.add_argument("--spec", required=True, help="仕様を書いたファイル（UTF-8）")
            p.add_argument("--effort", default="medium", choices=("low", "medium", "high"))
            p.add_argument("--timeout", type=int, default=3600)
        if name == "diff":
            p.add_argument("--no-bak", action="store_true")
        if name == "restore":
            p.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    handlers = {
        "resolve": cmd_resolve, "prepare": cmd_prepare, "run": cmd_run,
        "diff": cmd_diff, "restore": cmd_restore,
    }
    try:
        if args.command != "resolve":
            validate_run_id(args.run)
        return handlers[args.command](args)
    except ValueError as exc:
        print(f"NG: {exc}")
        return EXIT_GENERIC


if __name__ == "__main__":
    sys.exit(main())
