"""test_pv_review — pv review モードの差分パーサと finding 検証の単体テスト.

なぜ必要か:
    このモジュールの存在意義は「幻覚した指摘を非 LLM で落とす」ことなので、
    退行に気づけないと意味が消える。2026-08-12 の cgd Lv2 レビューで
    Codex / DeepSeek の両者が「差分パーサと finding 検証は独立にテストできる
    純関数として先に作れ」と勧めた。

    テストケースはレビューで挙がったエッジケースをそのまま写している:
    新規 / 削除 / rename / copy / binary / 複数 hunk / 件数省略 / CRLF /
    日本語・空白入りパス / a,b プレフィックス無し / severity 表記ゆれ /
    書式崩れ / 総行数超過 / ID 重複。

実行方法:
    python -m pytest .claude/tools/tests/test_pv_review.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from pv_review import (  # noqa: E402
    check_merge_coverage,
    normalize_path,
    normalize_severity,
    parse_findings,
    parse_unified_diff,
    validate_findings,
)


# --------------------------------------------------------------------------
# normalize_path
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("a/server/x.py", "server/x.py"),
    ("b/server/x.py", "server/x.py"),
    ("./server/x.py", "server/x.py"),
    ("server\\x.py", "server/x.py"),
    ('"a/\\346\\227\\245.py"', "日.py"),  # git の 8 進エスケープ（日本語パスの実形式）
    ("  a/x.py  ", "x.py"),
    ("", ""),
])
def test_normalize_path(raw, expected):
    assert normalize_path(raw) == expected


def test_normalize_path_quoted_japanese():
    # git は特殊文字を含むパスを C 形式でクォートする
    assert normalize_path('"a/日本語 ファイル.py"') == "日本語 ファイル.py"


# --------------------------------------------------------------------------
# parse_unified_diff
# --------------------------------------------------------------------------
DIFF_MODIFIED = """diff --git a/server/x.py b/server/x.py
index 1111111..2222222 100644
--- a/server/x.py
+++ b/server/x.py
@@ -10,3 +10,4 @@ def f():
     a = 1
-    b = 2
+    b = 3
+    c = 4
@@ -40,2 +41,2 @@ def g():
-    old
+    new
"""


def test_parse_modified_two_hunks():
    idx = parse_unified_diff(DIFF_MODIFIED)
    assert len(idx.files) == 1
    f = idx.files[0]
    assert f.new_path == "server/x.py"
    assert f.old_path == "server/x.py"
    assert f.status == "modified"
    assert f.new_hunks == [(10, 13), (41, 42)]
    assert f.old_hunks == [(10, 12), (40, 41)]


def test_parse_added_file():
    diff = """diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1,3 @@
+one
+two
+three
"""
    idx = parse_unified_diff(diff)
    f = idx.files[0]
    assert f.status == "added"
    assert f.old_path is None
    assert f.new_path == "new.py"
    assert f.new_hunks == [(1, 3)]
    assert f.old_hunks == []          # 長さ 0 の側は範囲を作らない


def test_parse_deleted_file():
    diff = """diff --git a/gone.py b/gone.py
deleted file mode 100644
--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-one
-two
"""
    idx = parse_unified_diff(diff)
    f = idx.files[0]
    assert f.status == "deleted"
    assert f.new_path is None
    assert f.old_path == "gone.py"
    assert f.old_hunks == [(1, 2)]


def test_parse_rename():
    diff = """diff --git a/old_name.py b/new_name.py
similarity index 92%
rename from old_name.py
rename to new_name.py
--- a/old_name.py
+++ b/new_name.py
@@ -5,1 +5,1 @@
-x
+y
"""
    idx = parse_unified_diff(diff)
    f = idx.files[0]
    assert f.status == "renamed"
    assert f.old_path == "old_name.py"
    assert f.new_path == "new_name.py"
    # 旧名で指摘されても引ける（レビューで挙がった誤判定ケース）
    assert idx.find("old_name.py") is f
    assert idx.find("new_name.py") is f


def test_parse_copy():
    diff = """diff --git a/src.py b/dst.py
similarity index 100%
copy from src.py
copy to dst.py
"""
    f = parse_unified_diff(diff).files[0]
    assert f.status == "copied"
    assert f.old_path == "src.py"
    assert f.new_path == "dst.py"


def test_parse_binary():
    diff = """diff --git a/img.png b/img.png
index 1111111..2222222 100644
Binary files a/img.png and b/img.png differ
"""
    idx = parse_unified_diff(diff)
    f = idx.files[0]
    assert f.is_binary is True
    assert f.status == "binary"
    assert any("binary" in w for w in idx.parse_warnings)


def test_parse_hunk_without_counts():
    # `@@ -1 +1 @@` は件数省略で 1 行の意味
    diff = """--- a/x.py
+++ b/x.py
@@ -7 +7 @@
-a
+b
"""
    f = parse_unified_diff(diff).files[0]
    assert f.new_hunks == [(7, 7)]
    assert f.old_hunks == [(7, 7)]


def test_parse_crlf_and_no_newline_marker():
    diff = ("--- a/x.py\r\n+++ b/x.py\r\n@@ -1,2 +1,2 @@\r\n-a\r\n+b\r\n"
            "\\ No newline at end of file\r\n")
    f = parse_unified_diff(diff).files[0]
    assert f.new_path == "x.py"
    assert f.new_hunks == [(1, 2)]


def test_parse_plain_diff_without_git_header():
    # `diff -u` 出力（`diff --git` 行が無い）
    diff = """--- old/x.py	2026-08-12 10:00:00
+++ new/x.py	2026-08-12 10:01:00
@@ -3,2 +3,2 @@
-a
+b
"""
    f = parse_unified_diff(diff).files[0]
    assert f.new_path == "new/x.py"
    assert f.old_path == "old/x.py"


def test_parse_quoted_japanese_path():
    diff = 'diff --git "a/日本語 ファイル.py" "b/日本語 ファイル.py"\n@@ -1,1 +1,1 @@\n-a\n+b\n'
    f = parse_unified_diff(diff).files[0]
    assert f.new_path == "日本語 ファイル.py"
    assert f.new_hunks == [(1, 1)]


def test_parse_multiple_files():
    idx = parse_unified_diff(DIFF_MODIFIED + """diff --git a/y.py b/y.py
--- a/y.py
+++ b/y.py
@@ -1,1 +1,1 @@
-a
+b
""")
    assert len(idx.files) == 2
    assert sorted(idx.all_paths()) == ["server/x.py", "y.py"]


def test_parse_empty_input():
    idx = parse_unified_diff("")
    assert idx.files == []


def test_find_by_trailing_match():
    idx = parse_unified_diff(DIFF_MODIFIED)
    # LLM がリポジトリ絶対パスで書いてきた場合も引ける
    assert idx.find("C:/ClaudeCode/server/x.py") is not None


# --------------------------------------------------------------------------
# normalize_severity
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected,normalized", [
    ("critical", "critical", False),
    ("major", "major", False),
    ("minor", "minor", False),
    ("🔴", "critical", True),
    ("HIGH", "critical", True),
    ("Medium", "major", True),
    ("low", "minor", True),
    ("重大", "critical", True),
    ("🟡 注意", "minor", True),
])
def test_normalize_severity(raw, expected, normalized):
    assert normalize_severity(raw) == (expected, normalized)


@pytest.mark.parametrize("raw", ["", "???", None, 3])
def test_normalize_severity_unreadable(raw):
    assert normalize_severity(raw)[0] is None


# --------------------------------------------------------------------------
# parse_findings
# --------------------------------------------------------------------------
def test_parse_findings_fenced_array():
    text = """所見は以下のとおり。

```json
[
  {"finding_id": "bug:1", "severity": "critical", "file": "server/x.py",
   "side": "new", "line": 11, "title": "境界値", "rationale": "..."},
  {"finding_id": "bug:2", "severity": "minor", "file": "server/x.py",
   "title": "命名", "rationale": "..."}
]
```
以上。"""
    findings, errors = parse_findings(text)
    assert [f["finding_id"] for f in findings] == ["bug:1", "bug:2"]
    assert errors == []


def test_parse_findings_json_lines():
    text = ('{"finding_id": "a:1", "severity": "major", "file": "x.py", "title": "t"}\n'
            '{"finding_id": "a:2", "severity": "minor", "file": "x.py", "title": "u"}\n')
    findings, errors = parse_findings(text)
    assert len(findings) == 2
    assert errors == []


def test_parse_findings_broken_line_does_not_kill_the_rest():
    text = """```jsonl
{"finding_id": "a:1", "severity": "major", "file": "x.py", "title": "t"}
{"finding_id": "a:2", "severity": broken}
{"finding_id": "a:3", "severity": "minor", "file": "x.py", "title": "u"}
```"""
    findings, errors = parse_findings(text)
    assert [f["finding_id"] for f in findings] == ["a:1", "a:3"]
    assert len(errors) == 1


def test_parse_findings_object_with_findings_key():
    text = '```json\n{"findings": [{"finding_id": "z:1", "severity": "minor", "file": "x.py", "title": "t"}]}\n```'
    findings, _ = parse_findings(text)
    assert findings[0]["finding_id"] == "z:1"


def test_parse_findings_none_found():
    findings, errors = parse_findings("指摘はありません。")
    assert findings == []
    assert errors == []


# --------------------------------------------------------------------------
# validate_findings — 3 層判定
# --------------------------------------------------------------------------
@pytest.fixture()
def idx():
    return parse_unified_diff(DIFF_MODIFIED)


def _f(**kw):
    base = {"finding_id": "t:1", "severity": "major", "file": "server/x.py",
            "side": "new", "line": 11, "title": "t", "rationale": "r"}
    base.update(kw)
    return base


def test_in_diff(idx):
    (c,) = validate_findings([_f(line=11)], idx)
    assert c.ok and c.scope == "in_diff" and c.errors == []


def test_out_of_diff_is_not_rejected(idx):
    """差分の外でも弾かない。呼び出し元への言及は正当（弾きすぎ防止）。"""
    (c,) = validate_findings([_f(line=999)], idx)
    assert c.ok is True
    assert c.scope == "out_of_diff"
    assert any("範囲外" in w for w in c.warnings)


def test_unknown_file_is_rejected(idx):
    (c,) = validate_findings([_f(file="not/in/diff.py")], idx)
    assert c.ok is False
    assert any("差分に存在しない" in e for e in c.errors)


def test_line_beyond_total_is_rejected_only_when_known(idx):
    """総行数が分かっているときだけ超過を NG にする（推測しない）。"""
    (c1,) = validate_findings([_f(line=999)], idx)
    assert c1.ok is True                       # 総行数不明なので判定しない
    (c2,) = validate_findings([_f(line=999)], idx, file_line_counts={"server/x.py": 100})
    assert c2.ok is False
    assert any("総行数を超えて" in e for e in c2.errors)


def test_file_level_when_line_omitted(idx):
    (c,) = validate_findings([_f(line=None, side="file")], idx)
    assert c.ok and c.scope == "file_level"


def test_old_side_uses_old_hunks(idx):
    """削除された行への指摘は old 側で照合する。"""
    (c,) = validate_findings([_f(side="old", line=11)], idx)
    assert c.scope == "in_diff"
    (c2,) = validate_findings([_f(side="old", line=13)], idx)
    assert c2.scope == "out_of_diff"           # old 側 hunk は (10,12) と (40,41)


def test_bad_side_is_rejected(idx):
    (c,) = validate_findings([_f(side="left")], idx)
    assert c.ok is False
    assert any("side が不正" in e for e in c.errors)


def test_severity_normalized_is_warning_not_error(idx):
    (c,) = validate_findings([_f(severity="HIGH")], idx)
    assert c.ok is True
    assert c.severity == "critical"
    assert any("正規化" in w for w in c.warnings)


def test_unreadable_severity_is_rejected(idx):
    (c,) = validate_findings([_f(severity="???")], idx)
    assert c.ok is False


def test_missing_required_field_is_rejected(idx):
    (c,) = validate_findings([_f(title=None)], idx)
    assert c.ok is False
    assert any("必須項目" in e for e in c.errors)


def test_duplicate_finding_id_is_rejected(idx):
    checks = validate_findings([_f(finding_id="dup"), _f(finding_id="dup", line=12)], idx)
    assert checks[0].ok is True
    assert checks[1].ok is False
    assert any("重複" in e for e in checks[1].errors)


def test_string_line_is_coerced_with_warning(idx):
    (c,) = validate_findings([_f(line="11")], idx)
    assert c.ok is True and c.scope == "in_diff"
    assert any("数値へ変換" in w for w in c.warnings)


def test_non_numeric_line_is_rejected(idx):
    (c,) = validate_findings([_f(line="十一")], idx)
    assert c.ok is False


def test_zero_or_negative_line_is_rejected(idx):
    (c,) = validate_findings([_f(line=0)], idx)
    assert c.ok is False


def test_binary_file_finding_becomes_file_level():
    idx2 = parse_unified_diff("""diff --git a/img.png b/img.png
Binary files a/img.png and b/img.png differ
""")
    (c,) = validate_findings([_f(file="img.png", line=5)], idx2)
    assert c.ok is True
    assert c.scope == "file_level"
    assert any("binary" in w for w in c.warnings)


# --------------------------------------------------------------------------
# check_merge_coverage
# --------------------------------------------------------------------------
def test_merge_coverage_detects_missing_and_derived(idx):
    checks = validate_findings(
        [_f(finding_id="bug:1"), _f(finding_id="bug:2", line=12)], idx)
    result = check_merge_coverage({"bug": checks}, merged_ids=["bug:1", "merge:1"])
    assert result["missing"] == ["bug:2"]      # 取りこぼし
    assert result["derived"] == ["merge:1"]    # 統合時にだけ見えた指摘（正当）
    assert result["covered"] == ["bug:1"]


def test_merge_coverage_ignores_invalid_findings(idx):
    """NG だった finding は取りこぼし判定の対象にしない。"""
    checks = validate_findings(
        [_f(finding_id="ok:1"), _f(finding_id="ng:1", file="nope.py")], idx)
    result = check_merge_coverage({"bug": checks}, merged_ids=["ok:1"])
    assert result["missing"] == []
