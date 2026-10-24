"""notify_queue.py(夜間の抑止と朝のまとめ送り)のテスト。時刻・承認・送信はすべて偽物(LINE・本物の表には行かない)。"""
from __future__ import annotations

import sys
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import notify_queue as nq  # noqa: E402

Q = nq.Quiet(dtime(19, 0), dtime(9, 0), frozenset({"例外グループ"}))
D = datetime(2026, 10, 21)


@pytest.mark.parametrize("hm,quiet", [((18, 59), False), ((19, 0), True), ((23, 30), True), ((0, 0), True),
                                      ((8, 59), True), ((9, 0), False), ((12, 0), False)])
def test_in_quiet_boundaries(hm, quiet) -> None:
    assert nq.in_quiet(D.replace(hour=hm[0], minute=hm[1]), Q) is quiet


def test_held_respects_exempt_groups() -> None:
    night = D.replace(hour=22)
    assert nq.held("制服のフジ情報共有", night, Q)
    assert not nq.held("例外グループ", night, Q)
    assert not nq.held("制服のフジ情報共有", D.replace(hour=10), Q)


def test_group_start_delays_quiet_for_that_group_only() -> None:
    q = nq.Quiet(dtime(19, 0), dtime(9, 0), frozenset(), (("楽天メルマガ配信", dtime(22, 0)),))
    assert not nq.held("楽天メルマガ配信", D.replace(hour=21, minute=59), q)
    assert nq.held("楽天メルマガ配信", D.replace(hour=22), q)
    assert nq.held("楽天メルマガ配信", D.replace(hour=8, minute=59), q)
    assert not nq.held("楽天メルマガ配信", D.replace(hour=9), q)
    assert nq.held("制服のフジ情報共有", D.replace(hour=20), q)


def test_load_config_reads_group_start(tmp_path) -> None:
    cfg = tmp_path / "q.json"
    cfg.write_text('{"start": "19:00", "end": "09:00", "group_start": {"楽天メルマガ配信": "22:00"}}', encoding="utf-8")
    q = nq.load_config(cfg)
    assert nq.quiet_for("楽天メルマガ配信", q).start == dtime(22, 0)
    assert nq.quiet_for("ほかのグループ", q).start == dtime(19, 0)


def test_next_open_and_day_close() -> None:
    assert nq.next_open(D.replace(hour=22), Q) == D.replace(hour=9) + timedelta(days=1)
    assert nq.next_open(D.replace(hour=3), Q) == D.replace(hour=9)
    assert nq.day_close(D.replace(hour=9), Q) == D.replace(hour=19)


def test_load_config_defaults(tmp_path: Path) -> None:
    q = nq.load_config(tmp_path / "none.json")
    assert (q.start, q.end, q.exempt) == (dtime(19, 0), dtime(9, 0), frozenset())
    real = nq.load_config()  # 同梱の設定(2026-10-20 決定: 全グループが対象)
    assert (real.start, real.end, real.exempt) == (dtime(19, 0), dtime(9, 0), frozenset())


def test_enqueue_dedupes_same_text(tmp_path: Path) -> None:
    s = nq.Store(tmp_path / "q.sqlite3")
    assert s.enqueue("g", "本文", "", "", "sha1") == (1, True)
    assert s.enqueue("g", "本文", "", "", "sha1") == (1, False)
    assert s.enqueue("g", "別", "", "", "sha2") == (2, True)
    assert s.enqueue("g", "本文", "", "", "sha1", code="APPR01") == (1, False)  # 夜の承認を後から持たせる
    assert s.items("asking")[0]["code"] == "APPR01"


class Fake:
    """承認の表・LINE・時計の偽物。approve_at などで「何分後に押されるか」を決める。"""

    def __init__(self, start: datetime) -> None:
        self.t = start
        self.rows: dict[str, dict] = {}
        self.asked: list[int] = []
        self.sent: list[int] = []
        self.voided: list[str] = []
        self.told: list[str] = []
        self.plan: dict[int, tuple[str, int]] = {}  # 申請番号 → (押す内容, 何秒後)
        self.ask_fail = 0
        self.n = 0

    def now(self) -> datetime:
        return self.t

    def now_ms(self) -> int:
        return int(self.t.timestamp() * 1000)

    def sleep(self, sec: float) -> None:
        self.t += timedelta(seconds=sec)
        for code, row in self.rows.items():
            act = row.get("_plan")
            if act and row["status"] == "pending" and self.t >= row["_at"]:
                row["status"] = act

    def ask(self, item: dict) -> str:
        if self.ask_fail:
            self.ask_fail -= 1
            raise SystemExit("見張り役が止まっています")
        self.n += 1
        code = f"C{self.n:05d}"
        self.asked.append(item["id"])
        act, after = self.plan.get(item["id"], (None, 0))
        self.rows[code] = {"status": "pending", "sha": item["sha"], "target_name": item["group_name"],
                           "expires_at": self.now_ms() + 24 * 3600 * 1000, "decision_text": "ここを直して",
                           "_plan": act, "_at": self.t + timedelta(seconds=after)}
        return code

    def read(self, code: str) -> dict | None:
        return self.rows.get(code)

    def deliver(self, item: dict, code: str) -> list[str]:
        assert self.rows[code]["status"] == "approved"
        self.rows[code]["status"] = "used"
        self.sent.append(item["id"])
        return ["m1"]

    def void(self, code: str) -> None:
        self.voided.append(code)
        self.rows[code]["status"] = "rejected"

    def hooks(self) -> nq.Hooks:
        return nq.Hooks(ask=self.ask, read=self.read, deliver=self.deliver, void=self.void, tell=self.told.append,
                        now=self.now, sleep=self.sleep, now_ms=self.now_ms)


def _store(tmp_path: Path, fake: Fake) -> nq.Store:
    return nq.Store(tmp_path / "q.sqlite3", fake.now)


def test_flush_does_nothing_during_quiet_hours(tmp_path: Path) -> None:
    fake = Fake(D.replace(hour=22))
    s = _store(tmp_path, fake)
    s.enqueue("制服のフジ情報共有", "a", "", "", "s1")
    nq.Flusher(s, fake.hooks(), Q, log=lambda t: None).run()
    assert fake.asked == [] and fake.sent == [] and s.items("queued")


def test_flush_at_nine_asks_each_and_sends_only_approved(tmp_path: Path) -> None:
    fake = Fake(D.replace(hour=9))
    s = _store(tmp_path, fake)
    for i in range(3):
        s.enqueue("制服のフジ情報共有", f"報告{i}", "", "", f"s{i}")
    fake.plan = {1: ("approved", 60), 2: ("rejected", 120), 3: ("revise", 30)}
    out = nq.Flusher(s, fake.hooks(), Q, log=lambda t: None).run()
    assert fake.asked == [1, 2, 3]  # 9:00 に溜まった件数分、1 件ずつ承認を求める
    assert fake.sent == [1]  # 承認されたものだけ送る
    assert [r["status"] for r in s.items()] == ["sent", "rejected", "revise"]
    assert fake.t < D.replace(hour=9, minute=5)  # 全部決まったらすぐ終わる(19:00 まで待たない)
    assert "送信" in out and any("修正の依頼" in t for t in fake.told)


def test_unanswered_until_close_is_voided_and_requeued(tmp_path: Path) -> None:
    fake = Fake(D.replace(hour=9))
    s = _store(tmp_path, fake)
    s.enqueue("制服のフジ情報共有", "返事なし", "", "", "s1")
    s.enqueue("制服のフジ情報共有", "夕方に承認", "", "", "s2")
    fake.plan = {2: ("approved", int(timedelta(hours=9, minutes=59).total_seconds()))}  # 18:59 に承認
    nq.Flusher(s, fake.hooks(), Q, log=lambda t: None).run()
    assert fake.sent == [2]
    left = s.items()[0]
    assert left["status"] == "queued" and left["code"] is None  # 翌朝に承認を求め直す
    assert fake.voided == ["C00001"]  # 後から押されても承認にならない
    assert fake.t >= D.replace(hour=19) and fake.t < D.replace(hour=19, minute=1)
    assert any("翌朝 9:00" in t for t in fake.told)


def test_night_approved_item_is_sent_without_asking_again(tmp_path: Path) -> None:
    fake = Fake(D.replace(hour=9))
    s = _store(tmp_path, fake)
    fake.rows["NIGHT1"] = {"status": "approved", "sha": "s1", "target_name": "制服のフジ情報共有",
                           "expires_at": fake.now_ms() + 3600 * 1000}
    s.enqueue("制服のフジ情報共有", "夜に承認済み", "", "", "s1", code="NIGHT1")
    nq.Flusher(s, fake.hooks(), Q, log=lambda t: None).run()
    assert fake.asked == [] and fake.sent == [1]


def test_mismatched_approval_is_not_sent(tmp_path: Path) -> None:
    fake = Fake(D.replace(hour=9))
    s = _store(tmp_path, fake)
    fake.rows["X1"] = {"status": "approved", "sha": "other", "target_name": "制服のフジ情報共有",
                       "expires_at": fake.now_ms() + 3600 * 1000}
    s.enqueue("制服のフジ情報共有", "本文", "", "", "s1", code="X1")
    nq.Flusher(s, fake.hooks(), Q, log=lambda t: None).run()
    assert fake.sent == [] and s.items()[0]["status"] == "failed"


def test_expired_approval_is_asked_again(tmp_path: Path) -> None:
    fake = Fake(D.replace(hour=9))
    s = _store(tmp_path, fake)
    fake.rows["OLD1"] = {"status": "approved", "sha": "s1", "target_name": "g", "expires_at": fake.now_ms() - 1}
    s.enqueue("g", "本文", "", "", "s1", code="OLD1")
    fake.plan = {1: ("approved", 30)}
    nq.Flusher(s, fake.hooks(), Q, log=lambda t: None).run()
    assert fake.asked == [1] and fake.sent == [1]


def test_ask_failure_retries_later(tmp_path: Path) -> None:
    fake = Fake(D.replace(hour=9))
    s = _store(tmp_path, fake)
    s.enqueue("g", "本文", "", "", "s1")
    fake.ask_fail = 1  # 最初は見張り役が止まっている
    fake.plan = {1: ("approved", 30)}
    nq.Flusher(s, fake.hooks(), Q, log=lambda t: None).run()
    assert fake.sent == [1]
    assert fake.t >= D.replace(hour=9, minute=5)  # 5 分後にやり直した


def test_deliver_failure_marks_failed_and_tells(tmp_path: Path) -> None:
    fake = Fake(D.replace(hour=9))
    s = _store(tmp_path, fake)
    s.enqueue("g", "本文", "", "", "s1")
    fake.plan = {1: ("approved", 30)}

    def boom(item, code):
        raise SystemExit("LINE への送信に失敗: HTTP 500")
    hooks = fake.hooks()
    hooks.deliver = boom
    nq.Flusher(s, hooks, Q, log=lambda t: None).run()
    assert s.items()[0]["status"] == "failed" and any("送れませんでした" in t for t in fake.told)
