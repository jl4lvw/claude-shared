"""register-product: 楽天の紐づけから Yahoo! の紐づけを決める連動(2026-10-23 カテゴリー統一)の試験。ネットにつながない。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline as pl  # noqa: E402

REMOTE = {"title": "テスト商品", "variants": {"v1": {"standardPrice": "1100", "shipping": {"shippingMethodGroup": "1"}}}}


def test_shipping_groups():
    assert pl.rakuten_shipping_groups(REMOTE) == {"1"}
    mixed = {"variants": {"a": {"shipping": {"shippingMethodGroup": "1"}}, "b": {"shipping": {"shippingMethodGroup": "2"}}}}
    assert pl.rakuten_shipping_groups(mixed) == {"1", "2"}
    assert pl.rakuten_shipping_groups({"variants": {"a": {}}}) == set()


def test_mixed_groups_are_not_derived(monkeypatch):
    monkeypatch.setattr(pl.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("呼ばない")))
    mixed = {"variants": {"a": {"shipping": {"shippingMethodGroup": "1"}}, "b": {"shipping": {"shippingMethodGroup": "2"}}}}
    assert pl.yahoo_paths_from_rakuten("g9999", mixed)["status"] == "needs_check"


def test_derive_is_called_with_group_and_parsed(monkeypatch):
    seen = {}

    def fake_run(args, **k):
        seen["args"], seen["cwd"] = args, k["cwd"]
        out = json.dumps({"status": "ok", "paths_api": ["(海自・海軍・マリン)グッズ:食品"], "missing": []}, ensure_ascii=False)
        return subprocess.CompletedProcess(args, 0, ("ログ\n" + out).encode("utf-8"), b"")
    monkeypatch.setattr(pl.subprocess, "run", fake_run)
    res = pl.yahoo_paths_from_rakuten("g9999", REMOTE)
    assert res["status"] == "ok" and seen["args"][-2:] == ["--shipping-group", "1"] and seen["cwd"] == pl.CAT_SYNC_DIR


def test_broken_output_is_failed(monkeypatch):
    monkeypatch.setattr(pl.subprocess, "run", lambda args, **k: subprocess.CompletedProcess(args, 1, b"", b"Traceback"))
    assert pl.yahoo_paths_from_rakuten("g9999", REMOTE)["status"] == "failed"


def _preflight(monkeypatch, derived):
    monkeypatch.setattr(pl, "rakuten_remote", lambda mn: REMOTE)
    monkeypatch.setattr(pl, "q_all", lambda *a, **k: [])
    monkeypatch.setattr(pl, "run_json_tool", lambda args: {"pending_rows": 0, "pending_item_pages": 0})
    monkeypatch.setattr(pl, "estore_name_examples", lambda t: [])
    monkeypatch.setattr(pl, "yahoo_copy_candidates", lambda *a, **k: [])
    monkeypatch.setattr(pl, "store_path_options", lambda: [])
    monkeypatch.setattr(pl, "yahoo_paths_from_rakuten", lambda mn, remote: derived)
    return pl.preflight("G9999")


def test_preflight_uses_paths_from_rakuten(monkeypatch):
    out = _preflight(monkeypatch, {"status": "ok", "paths_api": ["(海自・海軍・マリン)グッズ:食品"],
                                   "missing": ["組織・キャラで選ぶ(グッズ関連) > 海上自衛隊 > 護衛艦ちょうかい"],
                                   "needs_check": []})
    y = out["draft_plan"]["yahoo"]
    assert y["paths"] == ["(海自・海軍・マリン)グッズ:食品"] and y["paths_source"] == "rakuten"
    assert any("ちょうかい" in f for f in out["flags"])


def test_preflight_falls_back_to_copy_source(monkeypatch):
    out = _preflight(monkeypatch, {"status": "failed", "detail": "読み取りに失敗"})
    assert "paths" not in out["draft_plan"]["yahoo"]
    assert any("コピー元の紐づけで登録します" in f for f in out["flags"])


def _paths_plan():
    return {"yahoo": {"register": True, "copy_code": "g1280", "paths": [" B:c ", "A"]}}


def test_pin_paths_are_checked_on_live_tree(monkeypatch):
    seen = []
    monkeypatch.setattr(pl, "q_one", lambda *a, **k: {"path": "A"})
    monkeypatch.setattr(pl, "run_json_tool", lambda args: seen.append(args) or {"ok": True, "missing": []})
    p = _paths_plan()
    pl.pin_copy_source_paths(p)
    assert p["yahoo"]["expected_paths"] == ["A", "B:c"]
    assert seen == [["-m", "server.services.yahoo_store_tree", "check", "A", "B:c"]]


def test_pin_paths_missing_on_yahoo_is_rejected(monkeypatch):
    monkeypatch.setattr(pl, "q_one", lambda *a, **k: {"path": "A"})
    monkeypatch.setattr(pl, "run_json_tool", lambda args: {"ok": True, "missing": ["B:c"]})
    with pytest.raises(SystemExit, match="無いパス"):
        pl.pin_copy_source_paths(_paths_plan())


def test_pin_paths_unreadable_tree_is_rejected(monkeypatch):
    def fail(args):
        raise pl.StepFail("ツールが失敗: HTTP 503")
    monkeypatch.setattr(pl, "q_one", lambda *a, **k: {"path": "A"})
    monkeypatch.setattr(pl, "run_json_tool", fail)
    with pytest.raises(SystemExit, match="読めませんでした"):
        pl.pin_copy_source_paths(_paths_plan())
