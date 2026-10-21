"""LINE グループへの報告の「夜間の抑止」と「朝のまとめ送り」(line_notify.py が使う)。

2026-10-20 運用者決定:
  - 19:00〜翌 9:00 は LINE グループへの報告を送らない(抑止期間)。対象は全グループ(quiet_hours.json の exempt_groups で外せる)
  - 抑止期間中に出た報告はキューに溜め、9:00 にまとめて、1 件ずつ今と同じ LINE WORKS の承認を求める。承認されたものだけ送る
  - 19:00 より前に承認されたものは、送るのが 19:00 を過ぎても送る(承認が 19:00 以降なら溜めて翌朝送る)
  - 9:00 に許可を求めて、その日の 19:00 までに返事が無かったものは、承認を取り消してキューに戻し、翌朝 9:00 にもう一度求める

キューの状態(items.status):
  queued  承認をまだ求めていない(朝に求める)
  asking  承認の返事を待っている(code あり。夜に承認済みで溜めたものも、ここに承認済みの code を持って入る)
  sent / rejected(中止)/ revise(修正の依頼。Claude が作り直す)/ failed(送信に失敗)/ cancelled(手で取り消した)

送る・承認を求める・知らせる処理は呼び出し側(line_notify.py)が関数で渡す。ここは LINE にも承認の表にも直接触らない。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Callable

CONFIG = Path(__file__).with_name("quiet_hours.json")
DEFAULT_START = "19:00"
DEFAULT_END = "09:00"
POLL_SEC = 30
ASK_RETRY_SEC = 300  # 承認を求められなかったとき(見張り役が止まっている等)にやり直す間隔
OPEN = ("queued", "asking")


@dataclass(frozen=True)
class Quiet:
    start: dtime
    end: dtime
    exempt: frozenset[str] = field(default_factory=frozenset)


def _hm(text: str) -> dtime:
    h, m = text.split(":")
    return dtime(int(h), int(m))


def load_config(path: Path = CONFIG) -> Quiet:
    """設定が無ければ 19:00〜9:00・全グループが対象。"""
    d: dict[str, Any] = {}
    if path.exists():
        d = json.loads(path.read_text(encoding="utf-8"))
    return Quiet(_hm(d.get("start", DEFAULT_START)), _hm(d.get("end", DEFAULT_END)),
                 frozenset(d.get("exempt_groups", [])))


def in_quiet(dt: datetime, q: Quiet) -> bool:
    t = dt.time()
    if q.start <= q.end:  # 日をまたがない設定
        return q.start <= t < q.end
    return t >= q.start or t < q.end


def held(group: str, dt: datetime, q: Quiet) -> bool:
    """このグループへの報告を、いまは送らずに溜めるか。"""
    return group not in q.exempt and in_quiet(dt, q)


def next_open(dt: datetime, q: Quiet) -> datetime:
    """次に送れるようになる時刻(抑止期間の終わり)。"""
    cand = datetime.combine(dt.date(), q.end)
    return cand if cand > dt else cand + timedelta(days=1)


def day_close(dt: datetime, q: Quiet) -> datetime:
    """その日の抑止期間の始まり(朝のまとめ送りが待つ期限)。"""
    cand = datetime.combine(dt.date(), q.start)
    return cand if cand > dt else cand + timedelta(days=1)


SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_name TEXT NOT NULL,
  text TEXT NOT NULL,
  url TEXT NOT NULL DEFAULT '',
  mention TEXT NOT NULL DEFAULT '',
  sha TEXT NOT NULL,
  status TEXT NOT NULL,
  code TEXT,
  note TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""


class Store:
    """キュー(SQLite)。本文はファイルではなく中身を保存する(本文ファイルが消えても送れるように)。"""

    def __init__(self, path: Path, now: Callable[[], datetime] = datetime.now) -> None:
        self.path = path
        self.now = now
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._conn()) as c, c:
            c.executescript(SCHEMA)
            cols = {r["name"] for r in c.execute("PRAGMA table_info(items)")}
            if "purpose" not in cols:  # 依頼元の説明の目的(2026-10-21 追加。前から積んであるものは空)
                c.execute("ALTER TABLE items ADD COLUMN purpose TEXT NOT NULL DEFAULT ''")

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def _ts(self) -> str:
        return self.now().isoformat(timespec="seconds")

    def enqueue(self, group: str, text: str, url: str, mention: str, sha: str,
                code: str | None = None, purpose: str = "") -> tuple[int, bool]:
        """積む。同じ送り先・本文(sha)が待ちにあれば積まずにその番号を返す(二重送信の防止)。
        code: 夜に承認済みになったものを溜めるときの承認コード(朝に承認を求め直さずに送る)。"""
        with closing(self._conn()) as c, c:
            row = c.execute(f"SELECT id, code FROM items WHERE sha=? AND group_name=? AND status IN {OPEN}",
                            (sha, group)).fetchone()
            if row:
                if code and not row["code"]:
                    c.execute("UPDATE items SET status='asking', code=?, updated_at=? WHERE id=?",
                              (code, self._ts(), row["id"]))
                return int(row["id"]), False
            cur = c.execute("INSERT INTO items(group_name,text,url,mention,sha,status,code,created_at,updated_at,purpose)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (group, text, url, mention, sha, "asking" if code else "queued", code,
                             self._ts(), self._ts(), purpose or ""))
            return int(cur.lastrowid), True

    def items(self, *statuses: str) -> list[dict[str, Any]]:
        with closing(self._conn()) as c:
            if statuses:
                marks = ",".join("?" * len(statuses))
                rows = c.execute(f"SELECT * FROM items WHERE status IN ({marks}) ORDER BY id", statuses).fetchall()
            else:
                rows = c.execute("SELECT * FROM items ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def set(self, item_id: int, status: str, *, code: str | None | object = ..., note: str | None = None) -> None:
        with closing(self._conn()) as c, c:
            if code is ...:
                c.execute("UPDATE items SET status=?, note=COALESCE(?, note), updated_at=? WHERE id=?",
                          (status, note, self._ts(), item_id))
            else:
                c.execute("UPDATE items SET status=?, code=?, note=COALESCE(?, note), updated_at=? WHERE id=?",
                          (status, code, note, self._ts(), item_id))

    def cancel(self, item_id: int) -> bool:
        with closing(self._conn()) as c, c:
            return c.execute(f"UPDATE items SET status='cancelled', updated_at=? WHERE id=? AND status IN {OPEN}",
                             (self._ts(), item_id)).rowcount == 1


@dataclass
class Hooks:
    """朝のまとめ送りが使う外の処理(line_notify.py が本物を、テストが偽物を渡す)。"""
    ask: Callable[[dict[str, Any]], str]                     # 承認を求めて確認コードを返す(失敗は例外)
    read: Callable[[str], dict[str, Any] | None]             # 承認の表の行
    deliver: Callable[[dict[str, Any], str], list[str]]      # 承認コードでグループへ送る(使用済みにしてから送る)
    void: Callable[[str], None]                              # 返事の無い承認を取り消す(翌朝に回すとき)
    tell: Callable[[str], None]                              # 運用者への知らせ(失敗しても止めない)
    now: Callable[[], datetime] = datetime.now
    sleep: Callable[[float], None] = lambda s: None
    now_ms: Callable[[], int] = lambda: int(datetime.now().timestamp() * 1000)


class Flusher:
    """9:00 に溜まった報告を順に承認にかけ、承認されたものを送る。その日の 19:00 まで返事を待つ。"""

    def __init__(self, store: Store, hooks: Hooks, quiet: Quiet, log: Callable[[str], None] = print) -> None:
        self.store, self.h, self.q, self.log = store, hooks, quiet, log
        self.done: list[str] = []

    def _tell(self, text: str) -> None:
        try:
            self.h.tell(text)
        except Exception as exc:  # noqa: BLE001
            self.log(f"知らせを送れませんでした: {exc}")

    def run(self) -> str:
        start = self.h.now()
        if in_quiet(start, self.q):
            msg = f"抑止期間中なので何もしません({start:%H:%M})"
            self.log(msg)
            return msg
        close = day_close(start, self.q)
        retry_at = start
        while True:
            now = self.h.now()
            if now >= retry_at:
                retry_at = self._ask_all(now)
            for item in self.store.items("asking"):
                self._check(item)
            if not self.store.items(*OPEN):
                break
            if self.h.now() >= close:
                self._carry_over()
                break
            self.h.sleep(POLL_SEC)
        summary = "夜間に溜めた LINE 報告のまとめ送り: " + (" / ".join(self.done) if self.done else "対象なし")
        self.log(summary)
        return summary

    def _ask_all(self, now: datetime) -> datetime:
        failed = False
        for item in self.store.items("queued"):
            try:
                code = self.h.ask(item)
            except BaseException as exc:  # noqa: BLE001  SystemExit も受ける(line_notify は SystemExit で止まる)
                if isinstance(exc, KeyboardInterrupt):
                    raise
                failed = True
                self.log(f"#{item['id']} 承認を求められません: {exc}({ASK_RETRY_SEC // 60} 分後にやり直します)")
                break  # 見張り役が止まっている等は全件同じなので、残りも後でまとめてやり直す
            self.store.set(item["id"], "asking", code=code)
            self.log(f"#{item['id']} 承認を求めました(コード {code}・{item['group_name']})")
        # 失敗したら 5 分後に。成功したら次の周回でも求める(期限切れ等でキューに戻ったものを求め直すため)
        return now + timedelta(seconds=ASK_RETRY_SEC) if failed else now

    def _requeue(self, item: dict[str, Any], note: str) -> None:
        self.store.set(item["id"], "queued", code=None, note=note)

    def _check(self, item: dict[str, Any]) -> None:
        code = item["code"]
        row = self.h.read(code)
        label = f"#{item['id']}「{item['group_name']}」"
        if row is None:
            self._requeue(item, f"承認 {code} が見つからない")
            return
        st = row["status"]
        if st in ("pending", "approved") and row["expires_at"] <= self.h.now_ms():
            self._requeue(item, f"承認 {code} の期限切れ")  # 承認を求め直す(新しいコード)
            return
        if st == "approved":
            if row["target_name"] != item["group_name"] or row["sha"] != item["sha"]:
                self.store.set(item["id"], "failed", note=f"承認 {code} の送り先・本文が一致しない")
                self.done.append(f"{label} 照合が合わず送っていません")
                self._tell(f"{label} の報告は、承認 {code} と中身が一致しないため送っていません")
                return
            try:
                self.h.deliver(item, code)
            except BaseException as exc:  # noqa: BLE001
                if isinstance(exc, KeyboardInterrupt):
                    raise
                again = self.h.read(code) or {}
                if again.get("status") == "used":
                    self.store.set(item["id"], "sent", note="送信済み(別の送信で使用済み)")
                    return
                self.store.set(item["id"], "failed", note=str(exc)[:300])
                self.done.append(f"{label} 送信失敗")
                self._tell(f"{label} の報告を送れませんでした: {str(exc)[:200]}")
                return
            self.store.set(item["id"], "sent")
            self.done.append(f"{label} 送信")
            self._tell(f"{label} へ、夜間に溜めた報告を送りました(コード {code})")
        elif st == "used":
            self.store.set(item["id"], "sent", note="送信済み(別の送信で使用済み)")
        elif st == "rejected":
            self.store.set(item["id"], "rejected", note=row.get("decision_text"))
            self.done.append(f"{label} 中止")
        elif st == "revise":
            self.store.set(item["id"], "revise", note=row.get("decision_text"))
            self.done.append(f"{label} 修正の依頼")
            self._tell(f"{label} の報告は修正の依頼のため送っていません。Claude に作り直しを頼んでください"
                       f"(依頼: {(row.get('decision_text') or '')[:200]})")
        elif st == "expired":
            self._requeue(item, f"承認 {code} の期限切れ")
        # pending / sending は待つ

    def _carry_over(self) -> None:
        left = self.store.items(*OPEN)
        for item in left:
            if item["code"]:
                try:
                    self.h.void(item["code"])
                except Exception as exc:  # noqa: BLE001
                    self.log(f"#{item['id']} 承認 {item['code']} を取り消せませんでした: {exc}")
            self._requeue(item, "返事が無いため翌朝へ")
        if left:
            self.done.append(f"返事の無い {len(left)} 件は翌朝 9:00 にもう一度")
            self._tell(f"夜間に溜めた LINE 報告のうち {len(left)} 件は返事が無かったので送っていません。"
                       "承認を取り消し、翌朝 9:00 にもう一度許可を求めます")
