"""cgd の自動選択レベルとレベル定義の整合を固定する.

2026-08-13 の判断（実測 85 件・Lv1/Lv5/Lv6 が 0 件）:

    Lv1 / Lv5 / Lv6 は **隣のレベルに構造的に支配されていて、合理的に選ぶと
    決して選ばれない**。そこで自動選択の表から外し、明示指示専用にした。
    **廃止ではない。**

ここで守るのは 2 つだけ:

  1. 自動選択の表に Lv1 / Lv5 / Lv6 を戻さない
     （戻すと「選べるように見えて選ばれない」選択肢が判断を重くする。
      理由を知らない誰かが親切心で戻しうるので、機械で止める）
  2. **機能は消えていない** —— トリガ語と実装は残っていること
     （ドキュメントの整理が、いつのまにか機能削除にすり替わらないように）
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

SKILL_MD = TOOLS.parent / "skills" / "cgd" / "SKILL.md"
AUTO_SELECT_ONLY = ("Lv0", "Lv2", "Lv3", "Lv4", "Lv7", "Lv8")
EXPLICIT_ONLY = ("Lv1", "Lv5", "Lv6")


def _auto_select_table() -> list[str]:
    """「明示指示がない場合は…」の直後にある表の行を返す。"""
    text = SKILL_MD.read_text(encoding="utf-8")
    start = text.index("明示指示がない場合は")
    end = text.index("を自動で選ぶときの歯止め", start)
    rows = [ln for ln in text[start:end].splitlines() if ln.startswith("| ")]
    assert len(rows) >= 5, f"表の抽出に失敗している ({len(rows)} 行)"
    return rows


def test_auto_select_table_lists_only_the_levels_that_can_actually_win() -> None:
    joined = "\n".join(_auto_select_table())
    back = [lv for lv in EXPLICIT_ONLY if f"**{lv}**" in joined]
    assert not back, (
        f"支配されているレベルが自動選択の表に戻っている: {back}\n"
        "  → 戻す前に SKILL.md の『自動選択の対象から外してある』節を読むこと。"
        " 支配関係を解消したのなら、その節も一緒に更新する"
    )


def test_auto_select_table_still_covers_the_working_levels() -> None:
    joined = "\n".join(_auto_select_table())
    missing = [lv for lv in AUTO_SELECT_ONLY if f"**{lv}**" not in joined]
    assert not missing, f"自動選択の表からレベルが落ちている: {missing}"


def test_explicit_triggers_survive_for_the_demoted_levels() -> None:
    """**廃止ではない。** 名指しすれば従来どおり選べること。"""
    text = SKILL_MD.read_text(encoding="utf-8")
    for lv in EXPLICIT_ONLY:
        assert re.search(rf"「{lv}」.*→ {lv}", text), \
            f"{lv} のトリガ語が消えている（整理が機能削除にすり替わっている）"


def test_the_reason_is_written_down_next_to_the_decision() -> None:
    """理由が消えると、次の人が『なぜ 3 つ足りないのか』を復元できない。"""
    text = SKILL_MD.read_text(encoding="utf-8")
    assert "自動選択の対象から外してある" in text
    assert "支配" in text
    # 件数だけを根拠にしていない、という但し書きも残す
    assert "使用回数だけでは" in text


def test_lv7_and_lv8_share_the_same_technical_reviewers() -> None:
    """SKILL.md の「Lv8 は Lv7 + 批評 2 名」という記述を実装で裏取りする。

    ドキュメントの主張がコードとずれたら、この 1 行が先に落ちる。
    """
    from cgd_reviewers import build_reviewers

    lv7 = build_reviewers(7, "C:/x.txt", "C:/y.txt")
    lv8 = build_reviewers(8, "C:/x.txt", "C:/y.txt")
    tech8 = [r for r in lv8 if r.get("kind") != "critic"]
    assert lv7 == tech8, "Lv7 と Lv8 の技術枠が一致しなくなった"
    critics = [r["name"] for r in lv8 if r.get("kind") == "critic"]
    assert len(critics) == 2, f"Lv8 の批評枠が 2 名でない: {critics}"
