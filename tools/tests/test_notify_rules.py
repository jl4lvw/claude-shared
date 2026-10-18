"""通知ルール(notify_rules.py: 判定エンジン・スイッチ・判定の記録)のテスト。

固定したいこと (2026-10-17):
  - 実データで見つかった取りこぼしの3型(付け足しの文・括弧書き・疑問符のない選択)を拾う
  - 条件つきの依頼(「気になったら教えてください」)や完了報告(「承認済み」)では鳴らさない
  - 末尾の目印【対応待ち】があれば、言い回しに関係なく通知する
  - 設定が壊れていても通知は止まらない (不正な項目だけ捨て、足りない項目は既定で補う)
  - 語句はそのまま一致だけ (正規表現として解釈しない)
  - 判定の記録は鍵や長い英数字を伏せ、本文は300字まで、行数は上限で切る
  - スイッチ・ミュート・静音時間
"""
from __future__ import annotations

import copy
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hooks"))

import notify_rules as nr  # noqa: E402


def cfg_with(**overrides) -> dict:
    return nr.sanitize(overrides)


# --- 実データの取りこぼしの3型 ---------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        # ① 依頼の後ろに「セッション名は…にしました」が付く
        "GoQ 内の Yahoo 表示は 0 のまま残ります。どちらかで進める場合は、そう伝えてください。\n"
        "セッション名は「023★Yahoo在庫連携 1」にしました。",
        # ② 依頼の後ろに括弧書きが付く
        "[071.ファイル転送アプリ] を作成しました。要件を教えてください"
        "(誰が誰にどんなファイルを送るのか、送受信側の環境、サイズ上限など)。",
        # ③ 疑問符のない選択の提示
        "次は、宣伝系 101 件の件名確認と実行、または段階 2 のどちらでも進められます。",
        # その他: 依頼の後ろに箇条書き / 確認の依頼 / 質問
        "実行するか教えてください。\n- 内部 ID は変えていません。\n- 他の変更はありません。",
        "7S に取り付けを依頼できるか、寺下さんの確認をお願いします。次は本体への反映です。",
        "どれにしますか？",
        "Please provide the target directory.",
        "次のどれで進めますか？\n1. A案\n2. B案",
    ],
)
def test_real_world_misses_are_now_caught(text: str) -> None:
    assert nr.evaluate(text, nr.sanitize({})).notify is True


@pytest.mark.parametrize(
    "text",
    [
        "修正しました。テストも通っています。",
        "承認済みです。修正を完了しました。",
        "質問「どれにしますか？」の表示を修正しました。作業は完了です。",
        # 条件つきの依頼 (今すぐの対応を求めていない)
        "直りました。気になったら教えてください。判定の語を調整します。",
        "別件の run です。そちらの検証が必要であれば教えてください。不要なら削除すれば消えます。",
        "動かない場合は教えてください。",
        "セッション名は「X」にしました。",
        "OK、完了です",
        "",
        "   ",
        None,
    ],
)
def test_completion_and_conditional_requests_do_not_notify(text) -> None:
    assert nr.evaluate(text, nr.sanitize({})).notify is False


def test_marker_wins_regardless_of_wording() -> None:
    d = nr.evaluate("反映しました。\n【対応待ち】", nr.sanitize({}))
    assert d.notify and d.reason == "目印"


def test_marker_only_counts_at_the_end() -> None:
    long_tail = "x" * 300
    assert nr.evaluate("【対応待ち】という目印について説明します。" + long_tail, nr.sanitize({})).notify is False


def test_marker_can_be_changed_and_disabled() -> None:
    assert nr.evaluate("完了です。【要対応】", cfg_with(marker="【要対応】")).notify is True
    assert nr.evaluate("完了です。【対応待ち】", cfg_with(builtin={"marker": False})).notify is False


def test_exclusion_only_looks_at_the_last_two_sentences() -> None:
    """文章の頭のほうの「承認済み」で、末尾の本物の依頼を握りつぶさない。"""
    text = "A は承認済みです。B と C を反映しました。D は後で見ます。進めてよいですか？"
    assert nr.evaluate(text, nr.sanitize({})).notify is True


def test_phrases_are_literal_not_regex() -> None:
    cfg = cfg_with(rules=[{"type": "req", "phrase": "(a+)+$", "on": True}])
    assert nr.evaluate("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa!", cfg).notify is False  # 正規表現として解釈しない
    assert nr.evaluate("文末に (a+)+$ という語句がある。", cfg).notify is True


def test_rule_can_be_disabled_and_added() -> None:
    cfg = nr.sanitize({})
    text = "経過をまとめました。ご判断のほどをよろしく。"
    assert nr.evaluate(text, cfg).notify is True  # 既定の「ご判断」
    for r in cfg["rules"]:
        if r["phrase"] == "ご判断":
            r["on"] = False
    assert nr.evaluate(text, cfg).notify is False
    cfg["rules"].append({"type": "req", "phrase": "よろしく", "on": True})
    assert nr.evaluate(text, cfg).notify is True


def test_builtin_endings_can_be_turned_off() -> None:
    text = "終わりました。どうでしょう？"
    assert nr.evaluate(text, nr.sanitize({})).notify is True
    assert nr.evaluate(text, cfg_with(builtin={"question_end": False, "request_end": False})).notify is False


def test_numbered_completion_list_does_not_notify() -> None:
    assert nr.evaluate("変更点:\n1. ログを修正\n2. テストを追加", nr.sanitize({})).notify is False


# --- 設定の検証 ----------------------------------------------------------------------------
@pytest.mark.parametrize("raw", [None, [], "x", 3, {"rules": "bad"}, {"rules": [1, "a", None]}, {"builtin": 5}, {"switches": []}])
def test_sanitize_never_raises_and_returns_defaults(raw) -> None:
    cfg = nr.sanitize(raw)
    assert cfg["marker"] == "【対応待ち】" and set(cfg["switches"]) == set(nr.KINDS)
    assert nr.evaluate("どれにしますか？", cfg).notify is True


def test_sanitize_drops_only_the_bad_entries() -> None:
    cfg = nr.sanitize({"rules": [
        {"type": "req", "phrase": "有効な語句"},
        {"type": "weird", "phrase": "型が不正"},
        {"type": "exc", "phrase": ""},
        {"type": "exc", "phrase": "x" * 200},
        {"type": "req", "phrase": "有効な語句"},      # 重複
        {"type": "ign", "phrase": "無視する文", "on": "yes"},  # on が真偽値でない → 既定 True
    ]})
    assert [(r["type"], r["phrase"], r["on"]) for r in cfg["rules"]] == [("req", "有効な語句", True), ("ign", "無視する文", True)]


def test_sanitize_caps_rules_and_muted() -> None:
    cfg = nr.sanitize({"rules": [{"type": "req", "phrase": f"p{i}"} for i in range(900)], "muted": [f"m{i}" for i in range(300)]})
    assert len(cfg["rules"]) == nr.MAX_RULES and len(cfg["muted"]) == nr.MAX_MUTED


def test_empty_rules_list_is_respected() -> None:
    """利用者が語句を全部消したら、既定の語句を勝手に復活させない (組み込みの終わり方だけが残る)。"""
    cfg = nr.sanitize({"rules": []})
    assert cfg["rules"] == []
    assert nr.evaluate("ご指示をお待ちしています。", cfg).notify is False


def test_load_config_order_and_fallback(tmp_path: Path) -> None:
    local, cache = tmp_path / "local.json", tmp_path / "cache.json"
    assert nr.load_config(local, cache) == nr.DEFAULTS
    cache.write_text(json.dumps({"marker": "【C】"}), encoding="utf-8")
    assert nr.load_config(local, cache)["marker"] == "【C】"
    local.write_bytes(("﻿" + json.dumps({"marker": "【L】"})).encode("utf-8"))  # BOM 付きでも読める
    assert nr.load_config(local, cache)["marker"] == "【L】"
    local.write_text("{broken", encoding="utf-8")                                   # 壊れたら次の出所へ
    assert nr.load_config(local, cache)["marker"] == "【C】"
    cache.write_text("[]", encoding="utf-8")
    assert nr.load_config(local, cache) == nr.DEFAULTS


def test_defaults_are_not_shared_between_calls() -> None:
    a = nr.sanitize({})
    a["rules"].append({"type": "req", "phrase": "汚染", "on": True})
    assert all(r["phrase"] != "汚染" for r in nr.DEFAULTS["rules"])
    assert copy.deepcopy(nr.DEFAULTS) == nr.load_config(Path("/nonexistent/a"), Path("/nonexistent/b"))


# --- スイッチ・ミュート・静音時間 -----------------------------------------------------------------
def test_channel_switches() -> None:
    cfg = cfg_with(switches={"idle": {"sound": True, "speech": False, "phone": False}})
    assert nr.channel_allowed(cfg, "idle", "sound") is True
    assert nr.channel_allowed(cfg, "idle", "speech") is False
    assert nr.channel_allowed(cfg, "idle", "phone") is False
    assert nr.channel_allowed(cfg, "permission", "phone") is True


def test_muted_sessions_match_by_id_or_title() -> None:
    cfg = cfg_with(muted=["085★出港くれ工房", "abc123"])
    assert nr.channel_allowed(cfg, "reply", "sound", "zzz", "085★出港くれ工房 1") is False
    assert nr.channel_allowed(cfg, "reply", "phone", "abc123", "") is False
    assert nr.channel_allowed(cfg, "reply", "sound", "zzz", "083★業務写真PWA 1") is True


@pytest.mark.parametrize(
    "hhmm, expected",
    [("21:59", False), ("22:00", True), ("23:30", True), ("00:10", True), ("06:59", True), ("07:00", False), ("12:00", False)],
)
def test_quiet_hours_wrap_midnight_and_apply_to_phone_only(hhmm: str, expected: bool) -> None:
    cfg = cfg_with(quiet={"phone": {"on": True, "from": "22:00", "to": "07:00"}})
    now = datetime(2026, 10, 17, int(hhmm[:2]), int(hhmm[3:]))
    assert nr.channel_allowed(cfg, "reply", "phone", now=now) is (not expected)
    assert nr.channel_allowed(cfg, "reply", "sound", now=now) is True  # 音は静音時間の対象外


def test_quiet_hours_off_or_invalid() -> None:
    now = datetime(2026, 10, 17, 23, 0)
    assert nr.channel_allowed(cfg_with(), "reply", "phone", now=now) is True
    bad = cfg_with(quiet={"phone": {"on": True, "from": "25:99", "to": "x"}})
    assert (bad["quiet"]["phone"]["from"], bad["quiet"]["phone"]["to"]) == ("22:00", "07:00")


# --- 判定の記録 -------------------------------------------------------------------------------------
def test_redact_masks_secrets() -> None:
    out = nr.redact("api_key=ABCDEF123456 と token: xyz987654 と sk-abcdefghijklmnopqrstuvwxyz0123 と a@b.co.jp と 090123456789")
    for leaked in ("ABCDEF123456", "xyz987654", "abcdefghijklmnopqrstuvwxyz0123", "a@b.co.jp", "090123456789"):
        assert leaked not in out


def test_record_decision_redacts_truncates_and_trims(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    d = nr.Decision(False, "一致なし")
    nr.record_decision("sid1", "題名" * 100, "あ" * 1000 + " password=hunter2hunter2", d, path)
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert row["sid"] == "sid1" and row["notify"] is False and row["reason"] == "一致なし"
    assert len(row["tail"]) <= nr.SNIPPET_CHARS and "hunter2" not in row["tail"]
    assert len(row["title"]) <= 80
    for i in range(nr.DECISIONS_MAX_LINES + 5):
        nr.record_decision(f"s{i}", "t", "本文", d, path)
    assert len(path.read_text(encoding="utf-8").splitlines()) <= nr.DECISIONS_MAX_LINES


def test_record_decision_never_raises_when_unwritable(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    nr.record_decision("s", "t", "本文", nr.Decision(True, "r"), blocker / "sub" / "d.jsonl")  # 親がファイル → OSError を握る


# --- 文単位の判定 (2026-10-17: 除外は同じ文の依頼だけを打ち消す) ----------------------------------------
def test_exclusion_only_cancels_the_request_in_the_same_sentence() -> None:
    """条件つきの文(必要なら…)と本物の依頼(様子を教えてください)が同じ返答に混ざっても、本物は拾う。"""
    text = ("調査しました。その後の様子を教えてください。必要なら、再発時にその場で負荷を記録する監視を入れられます。"
            "入れる場合は指示してください。")
    d = nr.evaluate(text, nr.sanitize({}))
    assert d.notify is True


def test_only_conditional_request_is_cancelled() -> None:
    d = nr.evaluate("直しました。必要であれば教えてください。", nr.sanitize({}))
    assert d.notify is False and d.reason == "除外語句" and d.rule == "必要であれば"


@pytest.mark.parametrize(
    "text",
    [
        "質問「どれにしますか？」の表示を修正しました。",
        "例:『進めてよいですか？』と聞かれます。",
        "ボタンの文言は「教えてください」に変えました。",
        "URL は https://example.test/?x=1&y=2 です。完了しました。",
    ],
)
def test_quotes_and_url_queries_do_not_trigger(text: str) -> None:
    assert nr.evaluate(text, nr.sanitize({})).notify is False


def test_question_outside_quotes_still_triggers() -> None:
    assert nr.evaluate("「A案」と「B案」のどちらにしますか？", nr.sanitize({})).notify is True
