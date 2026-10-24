"""pipeline.py の純粋な判定ロジックのテスト(外部システムには触れない)."""
from __future__ import annotations

import copy
import json
import os
import sys
import time as _time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline as pl  # noqa: E402


def _single() -> dict:
    return {
        "g": "G2225", "manage_number": "g2225", "sku_main": "G2225", "is_variation": False,
        "title": "自衛隊 ワッペン 護衛艦ちょうかい派米記念",
        "variants": [{"variant_id": "g2225", "selectors": {}, "price": 1694, "estore_price": 1540, "stock": 20}],
        "estore_price_main": 1540, "estore_product_name": "ワッペン(護衛艦ちょうかい派米記念)ベルクロ付", "lead_time_days": 5,
        "estore": {"register": True, "publish": True},
        "yahoo": {"register": True, "copy_code": "g2223", "publish": True, "reserve_publish": True,
                  "pending_before": {"rows": 0, "item_pages": 0}, "pending_allow": 4},
        "goq": {"resync": True},
    }


def _variation() -> dict:
    p = _single()
    p.update(g="G2300", manage_number="g2300", sku_main="G2300-parent", is_variation=True)
    p["variants"] = [
        {"variant_id": "v1", "selectors": {"色": "ネイビー", "サイズ": "M"}, "price": 3520, "estore_price": 3200, "stock": 5},
        {"variant_id": "v2", "selectors": {"色": "ネイビー", "サイズ": "L"}, "price": 3520, "estore_price": 3200, "stock": 5},
    ]
    return p


def test_valid_plans_pass():
    pl.validate_plan(_single())
    pl.validate_plan(_variation())


@pytest.mark.parametrize("mutate", [
    lambda p: p["variants"][0].update(stock=None),
    lambda p: p["variants"][0].update(stock=-1),
    lambda p: p["variants"][0].update(stock=True),
    lambda p: p["variants"][0].update(estore_price=0),
    lambda p: p.update(manage_number="G2225"),
    lambda p: p.update(sku_main="G2225-parent"),
    lambda p: p["yahoo"].update(copy_code=None),
    lambda p: p["yahoo"].update(copy_code="g2225"),
    lambda p: p["yahoo"].update(register=False),  # 登録しないのに公開
    lambda p: p["estore"].update(register=False),  # 登録しないのに公開
    lambda p: p["yahoo"].update(publish=False),  # 公開しないのに店頭反映
    lambda p: p["variants"].append(dict(p["variants"][0])),  # 単品なのに variant が複数
    lambda p: p.update(estore_product_name=""),
    lambda p: p.update(estore_product_name=None),
    lambda p: p.update(estore_product_name=p["title"]),  # 楽天の長い名前をそのまま使っている
    lambda p: p.update(estore_product_name="あ" * 61),
])
def test_invalid_single_plans_rejected(mutate):
    p = _single()
    mutate(p)
    with pytest.raises(SystemExit):
        pl.validate_plan(p)


def test_estore_product_name_not_required_when_estore_register_false():
    p = _single()
    p["estore"]["publish"] = False
    p["estore"]["register"] = False
    p["estore_product_name"] = None
    pl.validate_plan(p)  # Eストアに登録しないなら短縮名は不要


def test_variation_rules():
    p = _variation()
    p["variants"][1]["variant_id"] = "v1"
    with pytest.raises(SystemExit):
        pl.validate_plan(p)
    p = _variation()
    p["variants"][0]["selectors"] = {}
    with pytest.raises(SystemExit):
        pl.validate_plan(p)


def test_approval_lifecycle():
    p = _single()
    with pytest.raises(SystemExit):
        pl.check_approval(p)  # 未承認
    p["approval"] = {"hash": pl.plan_hash(p), "approved_at": datetime.now().isoformat(timespec="seconds")}
    pl.check_approval(p)  # 承認済み
    edited = copy.deepcopy(p)
    edited["lead_time_days"] = 6
    with pytest.raises(SystemExit):
        pl.check_approval(edited)  # 承認後の編集
    old = copy.deepcopy(p)
    old["approval"]["approved_at"] = (datetime.now() - timedelta(hours=25)).isoformat(timespec="seconds")
    with pytest.raises(SystemExit):
        pl.check_approval(old)  # 期限切れ


def test_plan_hash_ignores_approval_only():
    p = _single()
    h = pl.plan_hash(p)
    p["approval"] = {"hash": "x", "approved_at": "2026-01-01T00:00:00"}
    assert pl.plan_hash(p) == h


class _C:
    def __init__(self, plan: dict):
        self.plan = plan
        self.g = plan["g"]


def _row(value: str, text: str) -> dict:
    return {"value": value, "text": text}


def test_goq_single_requires_one_row_with_code():
    c = _C(_single())
    assert pl._plan_goq_jobs(c, [_row("1", "G2225 ワッペン")]) == [("1", 20)]
    with pytest.raises(pl.NeedsUser):
        pl._plan_goq_jobs(c, [_row("1", "G2225"), _row("2", "G2225 Amazon")])
    with pytest.raises(pl.NeedsUser):
        pl._plan_goq_jobs(c, [_row("1", "別の商品 G9999")])


def test_goq_variation_requires_exact_one_to_one_mapping():
    c = _C(_variation())
    ok = [_row("10", "ネイビー M"), _row("11", "ネイビー L")]
    assert pl._plan_goq_jobs(c, ok) == [("10", 5), ("11", 5)]
    with pytest.raises(pl.NeedsUser):  # 行数が variant 数と違う(別チャネルの行が混入)
        pl._plan_goq_jobs(c, ok + [_row("12", "Amazon ネイビー M")])
    with pytest.raises(pl.NeedsUser):  # 同じ行に 2 つの variant が対応
        pl._plan_goq_jobs(c, [_row("10", "ネイビー M L"), _row("11", "ネイビー")])
    with pytest.raises(pl.NeedsUser):  # どの行にも対応しない
        pl._plan_goq_jobs(c, [_row("10", "ネイビー M"), _row("11", "ホワイト L")])


def test_excl_tax_examples():
    assert pl.excl_tax(1694) == 1540
    assert pl.excl_tax(1815) == 1650


# ------------------------------------------------------------ cgd Lv3 (2026-09-28) 健全性チェックの回帰テスト
@pytest.mark.parametrize("mutate", [
    lambda p: p["estore"].update(register="true"),
    lambda p: p["yahoo"].update(reserve_publish="false"),
    lambda p: p["goq"].update(resync=1),
    lambda p: p["yahoo"].update(pending_allow=-1),
    lambda p: p["yahoo"].update(pending_allow="4"),
])
def test_invalid_type_values_rejected(mutate):
    """bool 項目に文字列/整数を入れた plan は弾く(Python は "false" を真扱いするため)."""
    p = _single()
    mutate(p)
    with pytest.raises(SystemExit):
        pl.validate_plan(p)


def test_variation_axis_mismatch_rejected():
    p = _variation()
    p["variants"][1]["selectors"] = {"色": "ネイビー"}  # サイズ軸が欠けている
    with pytest.raises(SystemExit):
        pl.validate_plan(p)


def test_check_approval_rejects_future_and_malformed_timestamp():
    p = _single()
    p["approval"] = {"hash": pl.plan_hash(p),
                     "approved_at": (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds")}
    with pytest.raises(SystemExit):
        pl.check_approval(p)  # 未来日時
    p["approval"]["approved_at"] = "not-a-date"
    with pytest.raises(SystemExit):
        pl.check_approval(p)  # 形式不正


def test_g_number_match_avoids_prefix_collision():
    assert pl._g_number_match("G2225", "自衛隊 G2225 ワッペン")
    assert not pl._g_number_match("G2225", "自衛隊 G22250 ワッペン")  # 別商品への誤一致を防ぐ
    assert not pl._g_number_match("G2225", "自衛隊 XG2225 ワッペン")


def test_token_match_word_boundary_for_alnum_tokens():
    assert pl._token_match("M", "ネイビー M")
    assert not pl._token_match("M", "ネイビー ML")  # "M" が "ML" の一部に誤一致しない
    assert pl._token_match("ネイビー", "ネイビーM")  # 日本語トークンは単純部分一致のまま(既知の限界)


def test_ctx_save_is_atomic_and_leaves_no_tmp_file(tmp_path):
    state_path = tmp_path / "x.state.json"
    c = pl.Ctx(_single(), state_path)
    c.state["step"] = {"status": "OK"}
    c.save()
    assert state_path.exists()
    assert not state_path.with_suffix(state_path.suffix + ".tmp").exists()
    assert json.loads(state_path.read_text(encoding="utf-8"))["step"]["status"] == "OK"


def test_pid_alive_detects_self():
    assert pl._pid_alive(os.getpid()) is True


def test_lock_blocks_when_owner_pid_is_this_process(tmp_path):
    lock = tmp_path / "x.lock"
    lock.write_text(str(os.getpid()), encoding="utf-8")
    with pytest.raises(SystemExit):
        pl._acquire_lock(lock)


def test_lock_reclaims_immediately_when_pid_confirmed_dead(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "_pid_alive", lambda pid: False)
    lock = tmp_path / "x.lock"
    lock.write_text("12345", encoding="utf-8")
    pl._acquire_lock(lock)  # 生死を確認できて死んでいるなら、経過時間に関係なく即回収する
    assert lock.read_text(encoding="utf-8") == str(os.getpid())


def test_lock_falls_back_to_time_when_liveness_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "_pid_alive", lambda pid: None)
    lock = tmp_path / "x.lock"
    lock.write_text("12345", encoding="utf-8")
    with pytest.raises(SystemExit):
        pl._acquire_lock(lock)  # 生死不明・新しいロックはブロックする
    old = _time.time() - pl.LOCK_STALE.total_seconds() - 60
    os.utime(lock, (old, old))
    pl._acquire_lock(lock)  # 生死不明でも古ければ回収する(旧来のフォールバック)
    assert lock.read_text(encoding="utf-8") == str(os.getpid())


def _write_plan(tmp_path: Path, plan: dict, name: str = "test") -> Path:
    p = tmp_path / f"{name}.plan.json"
    approved = {**plan, "approval": {"hash": pl.plan_hash(plan),
                                     "approved_at": datetime.now().isoformat(timespec="seconds")}}
    p.write_text(json.dumps(approved, ensure_ascii=False), encoding="utf-8")
    return p


def test_run_rejects_unknown_only_step(tmp_path):
    plan_path = _write_plan(tmp_path, _single())
    with pytest.raises(SystemExit):
        pl.run(plan_path, only="not_a_real_step")


def test_run_blocks_only_on_already_completed_plan(tmp_path):
    """完了済み plan は --only を付けても単発の書き込み再実行を許さない(2026-09-28 修正の回帰テスト).

    以前は `--only` を付けると完了チェックそのものが素通りし、`--only goq_stock` のような
    破壊的な工程を承認済み・完了済みの plan に対して単発で再実行できてしまっていた。
    """
    plan = _single()
    plan_path = _write_plan(tmp_path, plan)
    state_path = plan_path.with_suffix(".state.json")
    state = {name: {"status": "OK", "detail": "", "at": "x"} for name, _ in pl.STEPS}
    state["_plan_hash"] = pl.plan_hash(plan)
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SystemExit):
        pl.run(plan_path, only="goq_stock")


def test_run_blocks_edited_plan_after_completion_without_reconcile(tmp_path):
    plan = _single()
    plan_path = _write_plan(tmp_path, plan)
    state_path = plan_path.with_suffix(".state.json")
    state = {name: {"status": "OK", "detail": "", "at": "x"} for name, _ in pl.STEPS}
    state["_plan_hash"] = pl.plan_hash(plan)
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    edited = copy.deepcopy(plan)
    edited["lead_time_days"] = 6
    _write_plan(tmp_path, edited)  # 同じファイルへ上書き承認(完了後に内容を変えて再承認したケース)
    with pytest.raises(SystemExit):
        pl.run(plan_path, only=None)


def test_run_allows_reapproved_plan_when_not_yet_complete(tmp_path, monkeypatch):
    """NEEDS_USER で止まった plan を直して承認し直す、という SKILL.md の案内どおりの手順が

    実装上も通ることを確認する(以前は hash 不一致を無条件で拒否しており、この案内が
    実装と食い違っていた: 2026-09-28 cgd Lv3 指摘)。
    """
    plan = _single()
    plan_path = _write_plan(tmp_path, plan)
    state_path = plan_path.with_suffix(".state.json")
    state_path.write_text(json.dumps(
        {"import": {"status": "NEEDS_USER", "detail": "x", "at": "x"}, "_plan_hash": pl.plan_hash(plan)},
        ensure_ascii=False), encoding="utf-8")
    edited = copy.deepcopy(plan)
    edited["yahoo"]["pending_allow"] = 10  # ユーザーが plan を直した(hash が変わる)
    _write_plan(tmp_path, edited)

    calls = []

    def fake_step(c):
        calls.append(c.plan["yahoo"]["pending_allow"])
        return "OK", "fake"

    monkeypatch.setattr(pl, "STEPS", [("import", fake_step)])
    assert pl.run(plan_path, only=None) == 0
    assert calls == [10]  # 変更後の plan の内容で実行された(hash 変化を理由に拒否されない)


# --- Yahoo!コピー元のパス(2026-09-28 G2231 でユーザー指摘: パスを見せないと判断できない) ---

_WAPPEN = "(海自・海軍・マリン)グッズ:ファッション:パッチ(ワッペン)・肩章:パッチ(ワッペン):海上自衛隊"
_LAND = "組織・キャラで選ぶ(グッズ関連):海上自衛隊:陸上部隊"


def _fake_db(monkeypatch, products, variant_codes=()):
    def q_all(sql, params=()):
        if "yahoo_snapshot_variants" in sql:
            return [{"code": c} for c in variant_codes]
        return products

    def q_one(sql, params=()):
        code = params[0]
        return next((p for p in products if p["code"].lower() == code), None)

    monkeypatch.setattr(pl, "q_all", q_all)
    monkeypatch.setattr(pl, "q_one", q_one)


def _yrow(code, path, name="自衛隊 ワッペン テスト", cat="42624"):
    return {"code": code, "name": name, "path": path, "product_category": cat}


def test_copy_candidates_grouped_by_path_and_thin_last(monkeypatch):
    _fake_db(monkeypatch, [
        _yrow("g2225", "【ネコポス可】"), _yrow("g2223", "【ネコポス可】"), _yrow("g2207", "【ネコポス可】"),
        _yrow("g2134", f"{_WAPPEN}\n【ネコポス可】\n{_LAND}"),
        _yrow("g2120", f"{_LAND}\n{_WAPPEN}\n【ネコポス可】"),  # 並び順が違っても同じパス集合
        _yrow("g2163", f"{_WAPPEN}\n【ネコポス可】"),
    ])
    c = pl.yahoo_copy_candidates("自衛隊 ワッペン 第１水陸両用戦隊", False, exclude="g2231")
    assert [d["code"] for d in c] == ["g2134", "g2163", "g2225"]
    assert c[0]["count"] == 2 and c[0]["other_codes"] == ["g2120"]
    assert c[0]["org_paths"] == [_LAND]
    assert c[-1]["thin_path"] is True and c[-1]["count"] == 3
    assert all("price" not in d for d in c)  # 価格はコピーされないので判断材料にしない


def test_copy_candidates_respect_variation_kind(monkeypatch):
    _fake_db(monkeypatch, [_yrow("g0129", _WAPPEN, name="Tシャツ"), _yrow("g0128", _WAPPEN, name="Tシャツ")],
             variant_codes=("g0128",))
    assert [d["code"] for d in pl.yahoo_copy_candidates("Tシャツ", True)] == ["g0128"]
    assert [d["code"] for d in pl.yahoo_copy_candidates("Tシャツ", False)] == ["g0129"]


def test_pin_copy_source_paths_records_expected(monkeypatch):
    _fake_db(monkeypatch, [_yrow("g2134", f"{_WAPPEN}\n【ネコポス可】\n{_LAND}")])
    p = _single()
    p["yahoo"]["copy_code"] = "g2134"
    pl.pin_copy_source_paths(p)
    assert p["yahoo"]["expected_paths"] == sorted([_WAPPEN, "【ネコポス可】", _LAND])


def test_pin_copy_source_paths_rejects_thin_path(monkeypatch):
    _fake_db(monkeypatch, [_yrow("g2225", "【ネコポス可】")])
    p = _single()
    p["yahoo"]["copy_code"] = "g2225"
    with pytest.raises(SystemExit, match="ネコポス"):
        pl.pin_copy_source_paths(p)
    p["yahoo"]["allow_thin_path"] = True
    pl.pin_copy_source_paths(p)
    assert p["yahoo"]["expected_paths"] == ["【ネコポス可】"]


def test_pin_copy_source_paths_rejects_missing_snapshot(monkeypatch):
    _fake_db(monkeypatch, [])
    p = _single()
    with pytest.raises(SystemExit, match="yahoo_snapshot_products"):
        pl.pin_copy_source_paths(p)


def test_pin_copy_source_paths_skips_when_yahoo_not_registered(monkeypatch):
    _fake_db(monkeypatch, [])
    p = _single()
    p["yahoo"]["register"] = False
    pl.pin_copy_source_paths(p)
    assert "expected_paths" not in p["yahoo"]


def test_yahoo_path_problems_compares_as_sets():
    p = _single()
    p["yahoo"]["expected_paths"] = sorted([_WAPPEN, _LAND])
    c = _C(p)
    assert pl._yahoo_path_problems(c, {"paths": [_LAND, _WAPPEN]}) == []
    probs = pl._yahoo_path_problems(c, {"paths": ["【ネコポス可】"]})
    assert len(probs) == 1 and "不足" in probs[0] and "ネコポス" in probs[0]


def test_yahoo_path_problems_skips_old_plans_without_expected():
    assert pl._yahoo_path_problems(_C(_single()), {"paths": []}) == []


def test_wait_goq_judgement_waits_until_row_exists(monkeypatch):
    # 判定行が無い(404)間は待ち、行ができたら返す(G2231: 取込まで約84秒かかった)
    seq = iter([(False, {"detail": "判定なし"}), (False, {"detail": "判定なし"}),
                (True, {"ok": True, "detail": {"rakuten": True}})])
    monkeypatch.setattr(pl, "_goq_all_ok", lambda c: next(seq))
    monkeypatch.setattr(pl.time, "sleep", lambda s: None)
    ok, body = pl._wait_goq_judgement(_C(_single()), wait=10_000, every=1)
    assert ok and body["ok"] is True


class _Clock:
    """試験用の時計。sleep すると進む(本物の時間は使わない)。"""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def time(self) -> float:
        return self.now

    def sleep(self, sec: float) -> None:
        self.now += sec


def _clock(monkeypatch) -> _Clock:
    clk = _Clock()
    monkeypatch.setattr(pl.time, "time", clk.time)
    monkeypatch.setattr(pl.time, "sleep", clk.sleep)
    return clk


NG = (False, {"sku": "G2245", "ok": False, "detail": {"reason": "GoQ に基本コードが存在しません"}, "checked_at": 5})


def _ok(at: float) -> tuple[bool, dict]:
    return True, {"ok": True, "detail": {"rakuten": True, "yahoo": True, "estore": True}, "checked_at": at}


def test_wait_goq_judgement_ng_is_waited_for_and_rechecked_then_gives_up(monkeypatch):
    """NG の判定行が在っても待つ(GoQ の反映の遅れ)。連携表の取り直しを最大回数だけ促し、期限で (False, 判定) を返す。"""
    clk = _clock(monkeypatch)
    syncs = []
    monkeypatch.setattr(pl, "_goq_all_ok", lambda c: NG)
    monkeypatch.setattr(pl, "_request_status_sync", lambda: syncs.append(clk.now) or True)
    ok, body = pl._wait_goq_judgement(_C(_single()), wait=300, every=15)
    assert not ok and body["ok"] is False
    assert len(syncs) == pl.GOQ_RECHECK_MAX  # 連打しない
    assert clk.now - 1_000_000 >= 300  # 期限まで待った


def test_wait_goq_judgement_ng_then_ok_after_status_sync(monkeypatch):
    """G2245(2026-10-23): 最初は NG、連携表の取り直しのあとに全OK になった。"""
    clk = _clock(monkeypatch)
    state = {"synced": False}
    monkeypatch.setattr(pl, "_goq_all_ok", lambda c: _ok(9) if state["synced"] else NG)
    monkeypatch.setattr(pl, "_request_status_sync", lambda: state.update(synced=True) or True)
    ok, body = pl._wait_goq_judgement(_C(_single()), wait=300, every=15)
    assert ok and body["ok"] is True
    assert clk.now - 1_000_000 < 120  # 期限までは待たない


def test_wait_goq_judgement_ignores_stale_ok(monkeypatch):
    """resync より前の判定(checked_at が古い)は、全OK でも採用しない。"""
    _clock(monkeypatch)
    monkeypatch.setattr(pl, "_goq_all_ok", lambda c: _ok(100))
    monkeypatch.setattr(pl, "_request_status_sync", lambda: True)
    ok, _ = pl._wait_goq_judgement(_C(_single()), wait=60, every=15, since_ms=200)
    assert not ok
    ok, _ = pl._wait_goq_judgement(_C(_single()), wait=60, every=15, since_ms=50)
    assert ok


def test_judgement_fresh_requires_numeric_checked_at():
    assert pl._judgement_fresh({}, None)
    assert not pl._judgement_fresh({}, 1)
    assert not pl._judgement_fresh({"checked_at": True}, 0)
    assert pl._judgement_fresh({"checked_at": 5}, 5)


def test_request_status_sync_uses_existing_023_job(monkeypatch):
    calls = []

    def fake_http(method, path, body=None, headers=None, timeout=300):
        calls.append((method, path))
        return 200, {"queued": True, "id": 7}

    monkeypatch.setattr(pl, "http", fake_http)
    monkeypatch.setattr(pl, "_wait_queue", lambda job_id, timeout_s=420: {"status": "ok"})
    assert pl._request_status_sync() is True
    assert calls == [("POST", "/api/queue/goq-status-sync")]
    monkeypatch.setattr(pl, "_wait_queue", lambda job_id, timeout_s=420: {"status": "error"})
    assert pl._request_status_sync() is False
    monkeypatch.setattr(pl, "http", lambda *a, **k: (500, {}))
    assert pl._request_status_sync() is False


def test_goq_resync_timeout_becomes_pending_and_blocks_stock(monkeypatch, tmp_path):
    clk = _clock(monkeypatch)
    plan = {"g": "G2245", "manage_number": "g2245", "sku_main": "G2245", "goq": {"resync": True}}
    c = pl.Ctx(plan, tmp_path / "p.state.json")
    enq = []
    monkeypatch.setattr(pl, "_goq_all_ok", lambda c: NG)
    monkeypatch.setattr(pl, "http", lambda m, path, *a, **k: enq.append(path) or (200, {"id": 1}))
    monkeypatch.setattr(pl, "_wait_queue", lambda _id, timeout_s=420: {"status": "ok"})
    monkeypatch.setattr(pl, "_request_status_sync", lambda: True)
    status, detail = pl.step_goq_resync(c)
    assert status == pl.PENDING and "確認できませんでした" in detail and "在庫の設定は行いません" in detail
    assert enq == ["/api/queue/goq-resync"]
    c.state["goq_resync"] = {"status": status}
    # 在庫の設定へは進まない(GoQ も触らない)
    monkeypatch.setattr(pl, "_goq_rows", lambda c: (_ for _ in ()).throw(AssertionError("GoQ を触った")))
    assert pl.step_goq_stock(c)[0] == pl.PENDING
    # 次の run: 連携更新は打ち直さず、確認から再開する(今度は全OK)
    seq = iter([NG, _ok(clk.now * 1000 + 5)])
    monkeypatch.setattr(pl, "_goq_all_ok", lambda c: next(seq))
    status, detail = pl.step_goq_resync(c)
    assert status == "OK" and "前回の連携更新の確認" in detail
    assert enq == ["/api/queue/goq-resync"]  # 打ち直していない


def test_pending_old_job_is_not_reused(monkeypatch, tmp_path):
    """前回の連携更新から時間がたちすぎていたら(6時間超)、確認待ちでも連携更新からやり直す。"""
    clk = _clock(monkeypatch)
    plan = {"g": "G2245", "manage_number": "g2245", "sku_main": "G2245", "goq": {"resync": True}}
    c = pl.Ctx(plan, tmp_path / "p.state.json")
    c.state["goq_resync"] = {"status": pl.PENDING}
    c.state["_goq_resync_job"] = {"id": 1, "finished_ms": clk.now * 1000 - 7 * 3600 * 1000}
    enq = []
    monkeypatch.setattr(pl, "http", lambda m, path, *a, **k: enq.append(path) or (200, {"id": 2}))
    monkeypatch.setattr(pl, "_wait_queue", lambda _id, timeout_s=420: {"status": "ok"})
    seq = iter([NG, _ok(clk.now * 1000 + 5)])
    monkeypatch.setattr(pl, "_goq_all_ok", lambda c: next(seq))
    assert pl.step_goq_resync(c)[0] == "OK"
    assert enq == ["/api/queue/goq-resync"]


def _drift_ctx(monkeypatch, tmp_path, *, has_row: bool):
    plan = {"g": "G2244", "manage_number": "g2244", "sku_main": "G2244", "is_variation": False,
            "rakuten_fingerprint": "v2:old",
            "rakuten_field_hashes": {"title": "aaaa", "price": "bbbb", "inventories": "cccc", "images": "dddd",
                                      "productDescription": "eeee", "variants": "ffff", "tagline": "gggg"}}
    c = pl.Ctx(plan, tmp_path / "p.state.json")
    monkeypatch.setattr(pl, "rakuten_remote", lambda mn: {"title": "T"})
    monkeypatch.setattr(pl, "rakuten_fingerprint", lambda r: "v2:new")
    monkeypatch.setattr(pl, "rakuten_field_hashes", lambda r: dict(plan["rakuten_field_hashes"], productDescription="zzzz", images="yyyy"))
    monkeypatch.setattr(pl.Ctx, "row", lambda self, sku: {"sku": sku} if has_row else None)
    monkeypatch.setattr(pl.Ctx, "children", lambda self: [])
    monkeypatch.setattr(pl, "q_one", lambda *a, **k: {"n": 1})
    return c


def test_import_skips_when_already_imported_even_if_rakuten_changed(monkeypatch, tmp_path):
    """取込済みなら楽天側が変わっていても止めない(取込をしないので古い内容を取り込む恐れが無い)。変わった項目は添える。"""
    c = _drift_ctx(monkeypatch, tmp_path, has_row=True)
    status, detail = pl.step_import(c)
    assert status == "SKIP" and "商品説明" in detail and "画像" in detail and "取込はしていない" in detail


def test_import_still_stops_when_not_imported_and_rakuten_changed(monkeypatch, tmp_path):
    c = _drift_ctx(monkeypatch, tmp_path, has_row=False)
    with pytest.raises(pl.NeedsUser) as e:
        pl.step_import(c)
    assert "商品説明" in str(e.value) and "plan を作り直し" in str(e.value)


def test_changed_rakuten_fields_without_record_says_unknown():
    assert "記録がありません" in pl.changed_rakuten_fields({}, {})


def _stock_ctx(monkeypatch, tmp_path, *, variation=False):
    plan = {"g": "G2244", "manage_number": "g2244", "sku_main": "G2244", "is_variation": variation,
            "goq": {"resync": True}, "variants": [{"variant_id": "g2244", "stock": 0}]}
    c = pl.Ctx(plan, tmp_path / "p.state.json")
    monkeypatch.setattr(pl, "_goq_rows", lambda c: [{"value": "175835", "text": "{}"}])
    monkeypatch.setattr(pl, "_plan_goq_jobs", lambda c, rows: [("175835", 0)])
    return c


def test_goq_stock_does_not_overwrite_nonzero_stock(monkeypatch, tmp_path):
    """再開で在庫を0に戻さない: すでに 20 が入っていたら書き換えない。"""
    c = _stock_ctx(monkeypatch, tmp_path)
    monkeypatch.setattr(pl, "_goq_current_quantity", lambda c: 20)
    monkeypatch.setattr(pl, "http", lambda *a, **k: (_ for _ in ()).throw(AssertionError("書き込んだ")))
    status, detail = pl.step_goq_stock(c)
    assert status == "SKIP" and "20" in detail and "書き換えません" in detail


def test_goq_stock_sets_zero_when_current_is_zero_or_unknown(monkeypatch, tmp_path):
    for current in (0, None):
        c = _stock_ctx(monkeypatch, tmp_path)
        monkeypatch.setattr(pl, "_goq_current_quantity", lambda c, v=current: v)
        monkeypatch.setattr(pl, "http", lambda m, path, body=None, *a, **k: (200, {"id": 3}))
        monkeypatch.setattr(pl, "_wait_queue", lambda _id, timeout_s=420: {
            "status": "ok", "result": {"changed_fields": ["stock[175835]"], "quantity": 0, "target_row_ids": ["175835"]}})
        status, detail = pl.step_goq_stock(c)
        assert status == "OK" and "175835=0" in detail


def test_goq_current_quantity_reads_preview(monkeypatch, tmp_path):
    c = _stock_ctx(monkeypatch, tmp_path)
    monkeypatch.setattr(pl, "http", lambda *a, **k: (200, {"snapshot": {"goq": {"quantity": 20}}}))
    assert pl._goq_current_quantity(c) == 20
    monkeypatch.setattr(pl, "http", lambda *a, **k: (500, {}))
    assert pl._goq_current_quantity(c) is None
    monkeypatch.setattr(pl, "http", lambda *a, **k: (200, {"snapshot": {"goq": {"quantity": True}}}))
    assert pl._goq_current_quantity(c) is None


def test_store_links_for_single_all_malls():
    assert pl.store_links(_single()) == [
        ("楽天", "https://item.rakuten.co.jp/seifukunofuji/g2225/"),
        ("Eストア", "https://seifukunofuji.co.jp/SHOP/G2225.html"),
        ("Yahoo!", "https://store.shopping.yahoo.co.jp/seifukunofuji/g2225.html"),
    ]


def test_store_links_omit_unregistered_malls():
    p = _single()
    p["yahoo"]["register"] = False
    assert [m for m, _ in pl.store_links(p)] == ["楽天", "Eストア"]


def test_goq_unreachable_defers_and_skips_stock(monkeypatch, tmp_path):
    """GoQ がメンテナンス中なら在庫連携は DEFERRED(登録は続ける)。在庫書込みも行わず、plan は完了にしない。"""
    import pipeline as pl

    plan = {"g": "G2244", "manage_number": "g2244", "sku_main": "G2244", "goq": {"resync": True}}
    c = pl.Ctx(plan, tmp_path / "p.state.json")
    monkeypatch.setattr(pl, "_goq_all_ok", lambda c: (False, {}))
    monkeypatch.setattr(pl, "http", lambda *a, **k: (200, {"id": 1}))
    monkeypatch.setattr(pl, "_wait_queue", lambda _id: {"status": "error", "error_msg": "メンテナンス中: GoQ がメンテナンス中のため操作できません"})
    status, detail = pl.step_goq_resync(c)
    assert status == pl.DEFERRED and "在庫連携は行いませんでした" in detail
    c.state["goq_resync"] = {"status": status}
    monkeypatch.setattr(pl, "_goq_rows", lambda c: (_ for _ in ()).throw(AssertionError("GoQ を触った")))
    assert pl.step_goq_stock(c)[0] == pl.DEFERRED


def test_goq_other_error_still_fails(monkeypatch, tmp_path):
    import pipeline as pl

    plan = {"g": "G2244", "manage_number": "g2244", "sku_main": "G2244", "goq": {"resync": True}}
    c = pl.Ctx(plan, tmp_path / "p.state.json")
    monkeypatch.setattr(pl, "_goq_all_ok", lambda c: (False, {}))
    monkeypatch.setattr(pl, "http", lambda *a, **k: (200, {"id": 1}))
    monkeypatch.setattr(pl, "_wait_queue", lambda _id: {"status": "error", "error_msg": "行が見つかりません"})
    import pytest as _pt

    with _pt.raises(pl.StepFail):
        pl.step_goq_resync(c)


# ---- Eストアのカテゴリ(2026-10-24 G2246) ----
def test_estore_categories_from_rakuten_paths():
    p = {"yahoo": {"paths_source": "rakuten", "paths": [
        "組織・キャラで選ぶ(グッズ関連):海上自衛隊:水上艦:訓練支援艦:訓練支援艦くろべ", "地域で選ぶ:広島県呉市"]}}
    assert pl.estore_categories_wanted(p) == [
        ["組織・キャラで選ぶ(グッズ関連)", "海上自衛隊", "水上艦", "訓練支援艦", "訓練支援艦くろべ"],
        ["地域で選ぶ", "広島県呉市"]]


def test_estore_categories_need_rakuten_source():
    assert pl.estore_categories_wanted({"yahoo": {"paths_source": "copy", "paths": ["a:b"]}}) == []
    assert pl.estore_categories_wanted({"yahoo": {}}) == []


def test_estore_categories_step_order():
    names = [n for n, _ in pl.STEPS]
    assert names.index("estore_register") < names.index("estore_categories") < names.index("estore_publish")


def test_estore_categories_step_sets_and_reads_back(monkeypatch):
    calls = []
    store = {"now": [["海上自衛隊(制服ほか)・船員服"]]}
    want = [["組織", "海自"], ["地域で選ぶ", "広島県呉市"], ["無い", "カテゴリ"]]

    def fake_api(method, path, body=None, kind="manager"):
        calls.append((method, path, body))
        if method == "PUT":
            store["now"] = [c["category"] for c in body["categories"]]
        return {}

    class C:
        plan = {"estore": {"register": True}, "yahoo": {"paths_source": "rakuten",
                                                        "paths": [":".join(p) for p in want]}}
        main = "G9999"

        def row(self, sku):
            return {"estore_item_code": "G9999"}

    monkeypatch.setattr(pl, "_estore_cat_api", fake_api)
    monkeypatch.setattr(pl, "_estore_category_exists", lambda p: p[0] != "無い")
    monkeypatch.setattr(pl, "_estore_categories_now", lambda code: store["now"])
    st, msg = pl.step_estore_categories(C())
    assert st == "OK" and store["now"] == want[:2] and "無い > カテゴリ" in msg
    assert pl.step_estore_categories(C())[0] == "SKIP"
