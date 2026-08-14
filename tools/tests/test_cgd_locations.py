"""findings の location を差分に照らして分類する機能のテスト.

pv Lv3 の反証担当の指摘「location と hunk 範囲の照合は決定論的に実装できるのに
未実装なだけ」への対応。差分パースは pv_review.py の純関数を再利用している。

**ここで守りたいのは「やりすぎない」こと。**

  - cgd の Codex は read-only sandbox で周辺ファイルを読める設計なので、
    差分に無いファイルへの指摘を **幻覚と断定してはいけない**（pv とはここが違う）
  - したがって合否は変えない。出すのは内訳だけ
  - **照合できなかったことを「問題なし」と表示しない。** 差分が無い入力で
    件数 0 とだけ出すと合格したように誤読される
"""

from __future__ import annotations

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from cgd_plan import _parse_location, locate_findings  # noqa: E402

DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -10,6 +10,8 @@ def handler():
     a = 1
     b = 2
+    c = 3
+    d = 4
     return a

@@ -100,3 +102,4 @@ def other():
     pass
+    extra()
"""


# --- location のパース --------------------------------------------------------

def test_parses_path_and_line() -> None:
    assert _parse_location("app.py:12") == ("app.py", 12)


def test_parses_range_and_takes_the_start() -> None:
    assert _parse_location("app.py:12-20") == ("app.py", 12)


def test_windows_path_is_not_split_on_the_drive_letter() -> None:
    """`C:/x/app.py:12` の `C:` を行番号の区切りと誤読しない。"""
    assert _parse_location("C:/ClaudeCode/app.py:12") == ("C:/ClaudeCode/app.py", 12)


def test_path_without_line_keeps_the_path() -> None:
    assert _parse_location("app.py") == ("app.py", None)


def test_empty_and_non_string_are_rejected() -> None:
    assert _parse_location("") == (None, None)
    assert _parse_location(None) == (None, None)
    assert _parse_location(123) == (None, None)


# --- 分類 --------------------------------------------------------------------

def test_line_inside_a_hunk_is_in_diff() -> None:
    out = locate_findings(DIFF, [{"location": "app.py:12"}])
    assert out["checked"] is True
    assert out["counts"]["in_diff"] == 1


def test_line_outside_hunks_is_not_treated_as_a_problem() -> None:
    """同じファイルの範囲外は正当（呼び出し元への言及など）。"""
    out = locate_findings(DIFF, [{"location": "app.py:500"}])
    assert out["counts"]["out_of_diff"] == 1
    assert out["counts"]["in_diff"] == 0


def test_file_outside_the_diff_is_counted_but_not_called_a_hallucination() -> None:
    """cgd の Codex は周辺ファイルを読める。**幻覚と断定しない。**"""
    out = locate_findings(DIFF, [{"location": "other/util.py:3"}])
    assert out["counts"]["file_not_in_diff"] == 1
    assert "other/util.py" in out["files_outside_diff"]
    blob = str(out).lower()
    assert "幻覚" not in blob and "hallucination" not in blob


def test_missing_or_broken_location_is_counted_separately() -> None:
    out = locate_findings(DIFF, [{"title": "x"}, {"location": ""}, "not a dict"])
    assert out["counts"]["unparsed"] == 3


def test_path_only_location_is_counted_separately() -> None:
    out = locate_findings(DIFF, [{"location": "app.py"}])
    assert out["counts"]["no_line"] == 1


def test_every_finding_lands_in_exactly_one_bucket() -> None:
    """どこにも入らない finding があると、合計が静かに減って割合が狂う。"""
    findings = [{"location": "app.py:12"}, {"location": "app.py:500"},
                {"location": "zzz.py:1"}, {"location": "app.py"}, {"nope": 1}]
    out = locate_findings(DIFF, findings)
    assert sum(out["counts"].values()) == len(findings)


# --- 照合できなかった場合 ----------------------------------------------------

def test_input_without_a_diff_reports_that_it_did_not_check(monkeypatch) -> None:
    """**差分が無いことを「問題なし」と出さない。**"""
    out = locate_findings("背景の説明だけで差分が無い入力", [{"location": "app.py:1"}])
    assert out["checked"] is False
    assert "counts" not in out, "照合していないのに件数を出している"
    assert out["reason"]


def test_no_findings_still_reports_checked_when_a_diff_exists() -> None:
    out = locate_findings(DIFF, [])
    assert out["checked"] is True
    assert sum(out["counts"].values()) == 0
