"""test_pv_plan_units — pv_plan.py の純粋関数まわりの単体テスト.

なぜ必要か:
    2026-08-12 の pv review 実走で「挙動を変えたのにテストが無い」と指摘された箇所を
    ここで固定する。とくに `_postprocess` は、マーカーの数え方を変えた結果
    「本文中に codex 行があると全文返し」という**挙動変化**が入っており、
    テストで明示しておかないと次の改修で静かに戻る。

実行方法:
    python -m pytest .claude/tools/tests/test_pv_plan_units.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import pv_plan as P  # noqa: E402


# --------------------------------------------------------------------------
# _postprocess — codex 出力の切り出し
# --------------------------------------------------------------------------
def _codex_output(*, marks: int, body: str = "最終回答です。" * 60) -> str:
    parts = ["preamble line", "thinking..."]
    for _ in range(marks):
        parts += ["codex", body]
    parts += ["tokens used", "12,345"]
    return "\n".join(parts)


def test_postprocess_single_marker_extracts_body():
    body, usage = P._postprocess("codex", _codex_output(marks=1))
    assert body.startswith("最終回答です。")
    assert "preamble line" not in body
    assert "12,345 tokens" in usage


def test_postprocess_two_markers_keeps_full_text():
    """マーカーが 2 個以上なら**切らずに全文**を返し、その事実を usage に残す。

    旧実装は rfind で最後のマーカー以降だけを本文にしていたため、
    本文中に codex 行があると冒頭が黙って落ちていた。
    """
    text = _codex_output(marks=2)
    body, usage = P._postprocess("codex", text)
    assert body == text                      # 全文保持
    assert "スキップ" in usage
    assert "2 個" in usage


def test_postprocess_no_marker_keeps_full_text():
    text = "マーカーの無い出力\ntokens used\n99\n"
    body, usage = P._postprocess("codex", text)
    assert body == text
    assert "マーカー無し" in usage


def test_postprocess_short_extract_falls_back_to_full_text():
    text = "preamble\ncodex\nちょい\ntokens used\n1\n"
    body, usage = P._postprocess("codex", text)
    assert body == text
    assert "短すぎ" in usage


def test_postprocess_non_codex_engine_is_untouched():
    body, usage = P._postprocess("deepseek", "そのまま")
    assert body == "そのまま" and usage == ""


# --------------------------------------------------------------------------
# _STRUCTURE_RE — 構造行の数え方
# --------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("- a\n- b\n- c", 3),
    ("・あ\n・い\n・う", 3),          # 日本語の中黒は空白なしで書く（後方互換）
    ("1. a\n2) b", 2),
    ("# 見出し\n## 小見出し", 2),
    ("**太字**\n**太字2**\n**太字3**", 0),   # 強調だけは構造ではない
    ("-ハイフン直後に文字", 0),               # 箇条書きではない
])
def test_structure_re_counts(text, expected):
    assert len(P._STRUCTURE_RE.findall(text)) == expected


# --------------------------------------------------------------------------
# _FINDING_ID_RE / _ADOPTED_RE — 採用 id の抽出
# --------------------------------------------------------------------------
def test_adopted_ids_across_multiple_lines():
    text = ("[pv-adopted] bug:1, bug:2\n"
            "本文\n"
            "[pv-adopted] integration:1 merge:1\n")
    ids = [x for line in P._ADOPTED_RE.findall(text)
           for x in P._FINDING_ID_RE.findall(line)]
    assert ids == ["bug:1", "bug:2", "integration:1", "merge:1"]


def test_finding_id_re_ignores_decoration():
    assert P._FINDING_ID_RE.findall("**bug:1** と `integration:2`") == ["bug:1", "integration:2"]


def test_finding_id_re_rejects_non_id_text():
    assert P._FINDING_ID_RE.findall("採用しました。以上。") == []


# --------------------------------------------------------------------------
# _halt_reason — 中止分類が実メッセージに追随しているか
# --------------------------------------------------------------------------
def test_halt_patterns_all_present_in_source():
    """分類の文字列が実メッセージから消えると、永久 0 件になって気づけない。

    2026-08-12 に実際に 3 分類が死んでいた。`env` でも検査しているが、
    テストでも固定して二重に守る。
    """
    src = (TOOLS / "pv_plan.py").read_text(encoding="utf-8")
    dead = [label for needle, label in P._HALT_PATTERNS if src.count(needle) < 2]
    assert dead == [], f"実メッセージに存在しない分類: {dead}"


def test_halt_reason_classifies_known_messages():
    assert P._halt_reason("[pv] Lv4 は pv にはありません（実装済み: ...）") == "level_unimplemented"
    assert P._halt_reason("[pv] --keep-raw は使えません: 前回と mode が違います") == "keep_raw_mode_changed"
    assert P._halt_reason("何にも当てはまらない文言") == "other"


# --------------------------------------------------------------------------
# extract_usage_line — 引用された usage マーカーを費用集計に混ぜない
# --------------------------------------------------------------------------
# INC-20260812-0728179d7054: 添付やプロンプトが stderr に echo されると、
# 旧実装 (`"Usage]" in line and "今回" in line`) が本文の一節を拾っていた。
# 実際に audit.jsonl の usage_line に cgd の依頼テキストが入った。
def test_usage_line_is_picked_when_emitted_at_line_start():
    for line in [
        "[DS Usage] 今回: 入力 1,234 + 出力 567 = 合計 3.2 円",
        "  [Qwen Usage] 今回: 100 tokens",
        "[Gemini Usage] 今回: 5,000 tokens",
    ]:
        assert P.extract_usage_line(line) == line.strip()


def test_quoted_usage_marker_in_prompt_is_not_picked():
    """誤検出の実物。両方の条件を満たすが、行頭ではないので拾ってはいけない。"""
    quoted = ("4. stderr の [DS Usage] / [Qwen Usage] / [Gemini Usage] の"
              "「今回:」行を usage_line に転記する。")
    assert P.extract_usage_line(quoted) == ""


def test_usage_line_requires_konkai():
    """WARN 行は費用ではないので拾わない。"""
    assert P.extract_usage_line("[DS Usage] WARN: usage 情報がありません") == ""


def test_usage_line_picks_first_and_skips_noise():
    stderr = "\n".join([
        "何かのログ",
        "  - 説明中に [DS Usage] と今回の話が出てくる行",
        "[DS Usage] 今回: 合計 12 円",
        "[DS Usage] 今回: 合計 99 円",
    ])
    assert P.extract_usage_line(stderr) == "[DS Usage] 今回: 合計 12 円"


def test_usage_line_empty_input():
    assert P.extract_usage_line("") == ""
