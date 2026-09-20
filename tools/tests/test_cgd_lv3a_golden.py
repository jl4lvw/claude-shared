"""lv3 (従来の組) の出力が、roster 拡張の前後で変わっていないことを固定する回帰テスト。

ゴールデンは拡張前の cgd_lv3a.py から作った (lv3a_golden_data.py)。ここが落ちたら、lv3 の利用者に見える出力
(レポート・run.json・レビュアーへの入力・強度) が変わっている。ゴールデンを書き換えて済ませず、コードを直す。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import cgd_lv3a as target  # noqa: E402
import lv3a_roster_harness as harness  # noqa: E402
from lv3a_golden_data import GOLDEN_JSON  # noqa: E402

GOLDEN: dict[str, Any] = json.loads(GOLDEN_JSON)


@pytest.mark.parametrize("scenario", sorted(harness.SCENARIOS))
def test_lv3_outputs_are_identical_to_the_pre_roster_golden(
    scenario: str, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    actual = harness.run_scenario(target, tmp_path, capsys, monkeypatch, scenario)
    expected = GOLDEN["runs"][scenario]
    for key in expected:  # キーごとに比べる (どこが違うかが失敗の表示に出る)
        assert actual[key] == expected[key], key
    assert set(actual) == set(expected)


@pytest.mark.parametrize("scenario", ["default", "no_ds"])
def test_explicit_roster_lv3_is_the_same_as_the_default(
    scenario: str, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    actual = harness.run_scenario(target, tmp_path, capsys, monkeypatch, scenario, ["--roster", "lv3"])
    assert actual == GOLDEN["runs"][scenario]


def test_build_report_lv3_is_identical_to_the_pre_roster_golden(tmp_path: Path) -> None:
    assert harness.direct_report_scenarios(target, tmp_path) == GOLDEN["direct"]


def test_lv3_review_inputs_have_no_focus_block() -> None:
    """lv3 のレビュアーへの入力に、重点観点ブロックが 1 バイトも入らない (ゴールデンの sha256 に加えて直接も見る)。"""
    for spec in target.REVIEWERS:
        assert target.INTEGRATION_FOCUS not in target.reviewer_input(spec, "対象")


def test_the_four_lv3_reviewers_still_build_positionally() -> None:
    """既存の位置引数 (6 個) での生成が動き続け、effort は None (= --effort に従う)。"""
    item = target.ReviewerSpec("x", "Codex", "technical", "XT", ("🔴",), "reviewer")
    assert item.effort is None
    assert [(r.name, r.vendor, r.prefix, r.effort) for r in target.REVIEWERS] == [
        ("codex_tech", "Codex", "CT", None), ("codex_crit", "Codex", "CC", None),
        ("ds_tech", "DeepSeek", "DT", None), ("ds_crit", "DeepSeek", "DC", None),
    ]
