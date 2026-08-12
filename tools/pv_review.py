"""pv review モード — unified diff のパースと finding の機械検証（純関数のみ）.

なぜ独立モジュールなのか (2026-08-12):
  pv に「差分レビュー」を足すにあたり、cgd Lv2 レビュー（Codex + DeepSeek）で
  「差分パーサと finding 検証は**独立にテストできる純関数**として先に作れ」と
  両者が一致して勧めた。ここは I/O を持たず、文字列とデータ構造だけを扱う。
  ファイルの読み書き・プロセス起動は呼び出し側 (pv_plan.py) の責務。

設計の要点（すべて cgd Lv2 レビューの指摘を反映）:
  - **finding は JSON**。自前の `key=value | key=value` 1 行書式は `|`・改行・
    コロン・全角記号でパース事故を起こすと 🔴 指摘された。
  - **行モデルは new / old / file の 3 種**。「新側の行番号だけ」では、削除行に
    しか無いバグ・削除されたファイル・ファイル単位の指摘を表現できない（🔴）。
  - **severity は ASCII enum** (`critical` / `major` / `minor`)。絵文字は
    文字化け・モデル差・CLI 差で壊れるため内部では使わない。表示時だけ絵文字化する（🟠）。
  - **判定は 3 層**。NG にするのは「**構造的に不可能な指摘**」だけ。
    疑わしいものは warning に落とし、位置は scope で分類する。
    DeepSeek の「『弾きすぎない』方針が『NG 判定の甘さ』に転化している」という
    指摘への対応で、**弾きすぎと通しすぎを同時に防ぐ**のが狙い。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# --- severity ---------------------------------------------------------------
# 内部表現は ASCII。表示だけ絵文字にする。
SEVERITIES = ("critical", "major", "minor")
SEVERITY_MARK = {"critical": "🔴", "major": "🟠", "minor": "🟡"}
# LLM は表記が揺れる。落とす前に正規化する（正規化したことは warning に残す）。
_SEVERITY_ALIASES = {
    "🔴": "critical", "critical": "critical", "high": "critical", "crit": "critical",
    "重大": "critical", "blocker": "critical", "severe": "critical",
    "🟠": "major", "major": "major", "medium": "major", "mid": "major",
    "重要": "major", "warning": "major", "warn": "major",
    "🟡": "minor", "minor": "minor", "low": "minor", "注意": "minor",
    "nit": "minor", "info": "minor",
}

SIDES = ("new", "old", "file")

# --- unified diff -----------------------------------------------------------
_DIFF_GIT_RE = re.compile(r"^diff --git (.+)$")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_BINARY_RE = re.compile(r"^(?:Binary files .* differ|GIT binary patch)\s*$")


_C_ESCAPES = {"n": b"\n", "t": b"\t", "r": b"\r", "a": b"\a", "b": b"\b",
              "f": b"\f", "v": b"\v", '"': b'"', "\\": b"\\"}


def unquote_git_path(quoted: str) -> str:
    """git がクォートしたパスを元に戻す。

    git は既定 (`core.quotepath=true`) で**非 ASCII を 8 進エスケープ**にする。
    日本語パスは `"a/\\346\\227\\245.py"` のような形で出てくるので、
    素朴に引用符を外すだけだとバックスラッシュが残り、パス比較が壊れる
    （テストで実際に検出した）。バイト列へ戻してから UTF-8 で読む。
    """
    body = quoted[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out.extend(ch.encode("utf-8"))
            i += 1
            continue
        i += 1
        if i >= len(body):
            out.extend(b"\\")
            break
        nxt = body[i]
        if nxt in _C_ESCAPES:
            out.extend(_C_ESCAPES[nxt])
            i += 1
        elif nxt.isdigit() and len(body) - i >= 3 and all(c in "01234567" for c in body[i:i + 3]):
            out.append(int(body[i:i + 3], 8))
            i += 3
        else:
            out.extend(nxt.encode("utf-8"))
            i += 1
    return out.decode("utf-8", errors="replace")


def normalize_path(path: str) -> str:
    """比較用にパスをそろえる。判断はせず、形をそろえるだけ。"""
    p = (path or "").strip()
    if p.startswith('"') and p.endswith('"') and len(p) >= 2:
        p = unquote_git_path(p)
    p = p.replace("\\", "/").strip()
    for prefix in ("a/", "b/", "./"):
        if p.startswith(prefix):
            p = p[len(prefix):]
    return p


def _split_diff_git_paths(rest: str) -> tuple[str, str]:
    """`diff --git a/x b/x` の 2 パスを取り出す。空白入りパスがあるので素朴に割らない。"""
    rest = rest.strip()
    if rest.startswith('"'):
        # `"a/日本語 ファイル.py" "b/..."` の形
        m = re.match(r'^("(?:[^"\\]|\\.)*")\s+("(?:[^"\\]|\\.)*")$', rest)
        if m:
            return normalize_path(m.group(1)), normalize_path(m.group(2))
    # 空白を含まない一般形
    parts = rest.split()
    if len(parts) == 2:
        return normalize_path(parts[0]), normalize_path(parts[1])
    # 空白入りだがクォートされていない場合: `a/` と `b/` の境目を探す
    m = re.match(r"^(a/.+?)\s+(b/.+)$", rest)
    if m:
        return normalize_path(m.group(1)), normalize_path(m.group(2))
    return normalize_path(rest), normalize_path(rest)


@dataclass
class DiffFile:
    """差分に現れた 1 ファイル。"""

    new_path: str | None            # 削除されたファイルは None
    old_path: str | None            # 追加されたファイルは None
    status: str                     # added / deleted / modified / renamed / copied / binary
    new_hunks: list[tuple[int, int]] = field(default_factory=list)  # (開始行, 終了行) 新側
    old_hunks: list[tuple[int, int]] = field(default_factory=list)  # (開始行, 終了行) 旧側
    is_binary: bool = False

    def names(self) -> set[str]:
        return {p for p in (self.new_path, self.old_path) if p}


@dataclass
class DiffIndex:
    """差分全体。finding の位置照合に必要な情報だけを持つ。"""

    files: list[DiffFile] = field(default_factory=list)
    parse_warnings: list[str] = field(default_factory=list)

    def find(self, path: str) -> DiffFile | None:
        """パス名から差分中のファイルを引く。完全一致 → 末尾一致の順で探す。"""
        target = normalize_path(path)
        if not target:
            return None
        for f in self.files:
            if target in f.names():
                return f
        # LLM が絶対パスやリポジトリ相対パスで書くことがある。末尾一致で救う。
        for f in self.files:
            for name in f.names():
                if target.endswith("/" + name) or name.endswith("/" + target):
                    return f
        return None

    def all_paths(self) -> list[str]:
        out: list[str] = []
        for f in self.files:
            out.extend(sorted(f.names()))
        return out


def parse_unified_diff(text: str) -> DiffIndex:
    """unified diff を読み、ファイルと hunk の行範囲を取り出す。

    対応する形: `diff --git` あり / なし、新規・削除（`/dev/null`）、rename、copy、
    mode 変更のみ、binary、複数 hunk、件数省略 `@@ -1 +1 @@`、CRLF、
    `\\ No newline at end of file`、クォートされた日本語・空白入りパス。

    **判定はしない。** 読めなかったものは parse_warnings に残して呼び出し側へ返す。
    """
    index = DiffIndex()
    cur: DiffFile | None = None
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")

    def flush() -> None:
        nonlocal cur
        if cur is not None:
            index.files.append(cur)
            cur = None

    for raw in lines:
        line = raw

        m = _DIFF_GIT_RE.match(line)
        if m:
            flush()
            a, b = _split_diff_git_paths(m.group(1))
            cur = DiffFile(new_path=b, old_path=a, status="modified")
            continue

        if cur is None and (line.startswith("--- ") or line.startswith("+++ ")):
            # `diff --git` の無い素の unified diff（`diff -u` 出力）
            cur = DiffFile(new_path=None, old_path=None, status="modified")

        if cur is not None:
            if line.startswith("new file mode"):
                cur.status = "added"
                cur.old_path = None
                continue
            if line.startswith("deleted file mode"):
                cur.status = "deleted"
                cur.new_path = None
                continue
            if line.startswith("rename from "):
                cur.status = "renamed"
                cur.old_path = normalize_path(line[len("rename from "):])
                continue
            if line.startswith("rename to "):
                cur.status = "renamed"
                cur.new_path = normalize_path(line[len("rename to "):])
                continue
            if line.startswith("copy from "):
                cur.status = "copied"
                cur.old_path = normalize_path(line[len("copy from "):])
                continue
            if line.startswith("copy to "):
                cur.status = "copied"
                cur.new_path = normalize_path(line[len("copy to "):])
                continue
            if _BINARY_RE.match(line):
                cur.is_binary = True
                cur.status = "binary"
                continue
            if line.startswith("--- "):
                p = line[4:].split("\t")[0].strip()
                cur.old_path = None if p == "/dev/null" else normalize_path(p)
                if p == "/dev/null":
                    cur.status = "added"
                continue
            if line.startswith("+++ "):
                p = line[4:].split("\t")[0].strip()
                cur.new_path = None if p == "/dev/null" else normalize_path(p)
                if p == "/dev/null":
                    cur.status = "deleted"
                continue

            hm = _HUNK_RE.match(line)
            if hm:
                o_start = int(hm.group(1))
                o_len = int(hm.group(2)) if hm.group(2) is not None else 1
                n_start = int(hm.group(3))
                n_len = int(hm.group(4)) if hm.group(4) is not None else 1
                # 長さ 0 は「その側に行が無い」(純粋な追加/削除)。範囲は作らない。
                if o_len > 0:
                    cur.old_hunks.append((o_start, o_start + o_len - 1))
                if n_len > 0:
                    cur.new_hunks.append((n_start, n_start + n_len - 1))
                continue

    flush()

    for f in index.files:
        if not f.new_path and not f.old_path:
            index.parse_warnings.append("ファイル名を特定できない差分ブロックがありました")
        if f.is_binary:
            index.parse_warnings.append(
                f"binary 差分のため行を照合できません: {f.new_path or f.old_path}")
    return index


# --- finding ----------------------------------------------------------------
REQUIRED_FIELDS = ("finding_id", "severity", "file", "title")


@dataclass
class FindingCheck:
    """1 件の finding に対する検証結果。"""

    finding_id: str
    ok: bool
    scope: str                       # in_diff / out_of_diff / file_level / unknown
    severity: str | None
    file: str | None
    errors: list[str] = field(default_factory=list)    # 構造的に不可能 → NG
    warnings: list[str] = field(default_factory=list)  # 疑わしいが通す


def normalize_severity(value) -> tuple[str | None, bool]:
    """(正規化した severity, 正規化を要したか) を返す。読めなければ (None, False)。"""
    if not isinstance(value, str):
        return None, False
    raw = value.strip()
    if raw in SEVERITIES:
        return raw, False
    key = raw.lower().strip("：: 　")
    if key in _SEVERITY_ALIASES:
        return _SEVERITY_ALIASES[key], True
    for token, mapped in _SEVERITY_ALIASES.items():
        if token and token in raw:
            return mapped, True
    return None, False


def parse_findings(text: str) -> tuple[list[dict], list[str]]:
    """回答本文から finding を取り出す。

    受け付ける形（LLM の揺らぎを吸収する）:
      1. ```json ... ``` のフェンス内にある配列 or オブジェクト
      2. 1 行 1 JSON オブジェクト（JSON Lines）で `"finding_id"` を含む行

    戻り値は (findings, parse_errors)。**壊れた 1 件で全体を捨てない。**
    """
    findings: list[dict] = []
    errors: list[str] = []
    body = (text or "").replace("\r\n", "\n")

    for m in re.finditer(r"```(?:json|jsonl)?\s*\n(.*?)```", body, re.DOTALL):
        block = m.group(1).strip()
        if not block:
            continue
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            # 配列でなく JSON Lines がフェンスに入っている場合
            for line in block.split("\n"):
                line = line.strip().rstrip(",")
                if not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append(f"JSON として読めない行: {line[:60]} ({exc.msg})")
                    continue
                if isinstance(obj, dict):
                    findings.append(obj)
            continue
        if isinstance(data, list):
            findings.extend(x for x in data if isinstance(x, dict))
        elif isinstance(data, dict):
            if isinstance(data.get("findings"), list):
                findings.extend(x for x in data["findings"] if isinstance(x, dict))
            else:
                findings.append(data)

    if not findings:
        for line in body.split("\n"):
            line = line.strip().rstrip(",")
            if line.startswith("{") and "finding_id" in line:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append(f"JSON として読めない行: {line[:60]} ({exc.msg})")
                    continue
                if isinstance(obj, dict):
                    findings.append(obj)
    return findings, errors


def validate_findings(
    findings: list[dict],
    index: DiffIndex,
    file_line_counts: dict[str, int] | None = None,
) -> list[FindingCheck]:
    """finding を差分に照らして検証する。

    **NG にするのは「構造的に不可能な指摘」だけ**:
      - 必須項目の欠落 / finding_id の重複
      - severity がどうしても解釈できない
      - side が new/old/file 以外
      - line が 0 以下、または **判明している総行数を超える**
      - 差分に存在しないファイルへの指摘（＝幻覚）

    それ以外は warning に落とし、位置は scope で分類する:
      - in_diff     … hunk の範囲内
      - out_of_diff … 同じファイルだが hunk の外（呼び出し元への言及等。**正当**）
      - file_level  … ファイル単位の指摘（line 無し / side=file）

    `file_line_counts` は「新側ファイルの総行数」を呼び出し側が実ファイルから
    数えて渡す任意情報。**渡されなければ行数超過の判定はしない**（推測しない）。
    """
    counts = file_line_counts or {}
    seen_ids: set[str] = set()
    out: list[FindingCheck] = []

    for i, f in enumerate(findings):
        fid = str(f.get("finding_id") or f"(no-id-{i})")
        errors: list[str] = []
        warnings: list[str] = []

        for key in REQUIRED_FIELDS:
            if not f.get(key):
                errors.append(f"必須項目がありません: {key}")
        if fid in seen_ids:
            errors.append(f"finding_id が重複しています: {fid}")
        seen_ids.add(fid)

        sev, normalized = normalize_severity(f.get("severity"))
        if sev is None:
            errors.append(f"severity を解釈できません: {f.get('severity')!r}")
        elif normalized:
            warnings.append(f"severity を正規化しました: {f.get('severity')!r} -> {sev}")

        side = (f.get("side") or "new")
        if not isinstance(side, str) or side not in SIDES:
            errors.append(f"side が不正です: {side!r}（new / old / file）")
            side = "new"

        path = f.get("file")
        target = index.find(path) if isinstance(path, str) else None
        if isinstance(path, str) and target is None:
            errors.append(f"差分に存在しないファイルへの指摘です: {path}")

        line = f.get("line")
        if line is not None and not isinstance(line, int):
            try:
                line = int(str(line).strip())
                warnings.append("line を数値へ変換しました")
            except (TypeError, ValueError):
                errors.append(f"line を数値として読めません: {f.get('line')!r}")
                line = None
        if isinstance(line, int) and line <= 0:
            errors.append(f"line が不正です: {line}")
            line = None

        scope = "unknown"
        if target is not None:
            if side == "file" or line is None:
                scope = "file_level"
            else:
                if target.is_binary:
                    scope = "file_level"
                    warnings.append("binary 差分のため行を照合できません")
                else:
                    ranges = target.new_hunks if side == "new" else target.old_hunks
                    scope = "in_diff" if any(a <= line <= b for a, b in ranges) else "out_of_diff"
                    if scope == "out_of_diff":
                        warnings.append("差分の範囲外です（呼び出し元等への言及なら正当）")
                # 総行数が分かっている場合だけ、超過を構造エラーにする
                key = target.new_path if side == "new" else target.old_path
                total = counts.get(key or "")
                if isinstance(line, int) and isinstance(total, int) and line > total:
                    errors.append(f"line がファイルの総行数を超えています: {line} > {total}")

        out.append(FindingCheck(
            finding_id=fid, ok=not errors, scope=scope,
            severity=sev, file=normalize_path(path) if isinstance(path, str) else None,
            errors=errors, warnings=warnings,
        ))
    return out


def check_merge_coverage(
    raw_checks: dict[str, list[FindingCheck]],
    merged_ids: list[str],
) -> dict:
    """統合結果が各担当の finding を取りこぼしていないかを ID で突き合わせる。

    title 一致やテキスト類似で突合するのは危険だと Codex に 🔴 指摘されたため、
    **finding_id の集合演算だけ**で判定する。

    - missing  … raw にあるのに統合結果に無い（取りこぼし）
    - derived  … 統合結果にあるが raw に無い（統合時に見えた相互作用バグ。**正当**）
    """
    valid_ids = {c.finding_id for checks in raw_checks.values() for c in checks if c.ok}
    merged = set(merged_ids)
    return {
        "missing": sorted(valid_ids - merged),
        "derived": sorted(merged - valid_ids),
        "covered": sorted(valid_ids & merged),
    }
