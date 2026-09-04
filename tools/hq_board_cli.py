"""HQ 状態ボード読取 CLI — hq_board.py フックが書いた状態ファイルを集計して表示する。

司令塔セッション (/hq) はトランスクリプトを読まず、この CLI の出力だけで
「どのセッションが何で止まっているか」を把握する。

使い方:
  python hq_board_cli.py show  [--all] [--self SID]          Markdown 表 + 待ち中の詳細
  python hq_board_cli.py json  [--all] [--self SID]          機械可読 (JSON 配列)
  python hq_board_cli.py diff  [--commit] [--self SID] [--renotify-min 30]
                                                             前回スナップショットとの差分 (見張り番用)
  python hq_board_cli.py prune [--days 7]                    古い状態ファイルを削除

並び順: 許可待ち > 質問待ち > 回答待ち > 作業中 > 待機 > 完了 > 終了。
待ち状態の中では「止まっている時間が長い順」。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001, S110  # pragma: no cover
    pass


def _now() -> datetime:
    """tz 付きローカル時刻。フック側 (hq_board._now) と同じ基準。"""
    return datetime.now().astimezone()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
import hq_relay

HQ_DIR = Path(os.environ.get("CLAUDE_HQ_DIR", r"C:/ClaudeCode/.hq"))
BOARD_DIR = HQ_DIR / "board"
WATCH_STATE = HQ_DIR / "watch_state.json"

STALE_DAYS = 3  # これより古い updated_at は既定表示から外す

STATUS_LABEL = {
    "waiting_permission": "🔐許可待ち",
    "waiting_question": "❓質問待ち",
    "waiting_answer": "💬回答待ち",
    "running": "🔄作業中",
    "idle": "💤待機",
    "done": "✅完了",
    "remote_active": "🟢稼働中(HQ情報なし)",
    "remote_stale": "⚪応答なし",
    "ended": "⏹終了",
}
STATUS_ORDER = {k: i for i, k in enumerate(STATUS_LABEL)}
WAITING = ("waiting_permission", "waiting_question", "waiting_answer")

NEXT_ACTION = {
    "remote_active": "別PCで稼働中 (HQ拡張が未対応のため状態不明)。必要ならそのPCの前で確認する",
    "remote_stale": "別PCでハートビートが途絶えている。落ちたか放置の可能性。そのPCの前で確認する",
    "waiting_permission": "そのセッションのタブを開いて許可ダイアログに答える (メッセージでは解除できない)",
    "waiting_question": "そのセッションのタブを開いて選択肢ダイアログに答える (ダイアログはメッセージでは解除できない)",
    "waiting_answer": "質問への回答を `/hq <番号> <回答>` で中継する",
    "running": "待つ (作業中)",
    "idle": "次の依頼を投げるか、不要なら閉じる",
    "done": "結果を確認して次の依頼を投げる",
    "ended": "なし (終了済み)",
}


def _parse_ts(s: str | None) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:  # 旧形式 (tz 無し) はローカル時刻として扱う
        dt = dt.astimezone()
    return dt


def _elapsed(since: datetime | None, now: datetime) -> tuple[int, str]:
    if since is None:
        return 0, "-"
    mins = max(0, int((now - since).total_seconds() // 60))
    if mins < 60:
        return mins, f"{mins}分"
    if mins < 48 * 60:
        return mins, f"{mins // 60}時間{mins % 60}分"
    return mins, f"{mins // (60 * 24)}日"


def load_sessions(path: str | None) -> list[dict]:
    """list_sessions (MCP) の出力 JSON 配列を読む。無ければ空。"""
    if not path:
        return []
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"(sessions file unreadable: {exc})", file=sys.stderr)
        return []
    return [s for s in data if isinstance(s, dict) and s.get("sessionId")] if isinstance(data, list) else []


def resolve_send_ids(rows: list[dict], sessions: list[dict]) -> list[dict]:
    """ボード行に中継先 (CCD sessionId) を対応付け、ボードに無いセッションを返す。

    2026-09-04 実測: 新規セッションでは CCD の sessionId (local_2040d29d...) と
    フックが受け取る session_id (9a882cf7...) が**一致しない**ことがある。タイトル
    (トランスクリプトの custom-title = CCD のタイトル) で突き合わせ、ID 一致 →
    タイトル一意一致 → 未特定 の順に決める。
    """
    by_id = {s["sessionId"]: s for s in sessions}
    by_title: dict[str, list[dict]] = {}
    for s in sessions:
        t = str(s.get("title") or "").strip()
        if t:
            by_title.setdefault(t, []).append(s)
    matched: set[str] = set()
    for r in rows:
        cand = by_id.get(f"local_{r['session_id']}")
        how = "id"
        if cand is None:
            same = by_title.get(str(r["title"]).strip(), [])
            if len(same) == 1:
                cand, how = same[0], "title"
        if cand is None:
            r["send_id"] = None
            r["send_how"] = "未特定" if sessions else "未照合"
        else:
            r["send_id"] = cand["sessionId"]
            r["send_how"] = how
            r["ccd_title"] = cand.get("title")
            matched.add(cand["sessionId"])
    return [s for s in sessions if s["sessionId"] not in matched]


REMOTE_STALE_HIDE_MIN = 3 * 60  # HQ 情報の無い「応答なし」は、これより古ければ既定表示から畳む (件数だけ出す)


def remote_rows(remote: list[dict], own_label: str, include_all: bool) -> tuple[list[dict], dict[str, int]]:
    """GET /claude-sessions の行をボード行に変換する。自PC名義の行は除く (ローカルと二重になる)。

    戻り値: (行, PC別に畳んだ「古い応答なし」の件数)。HQ 情報が無い別PCの行は
    「稼働中 / 応答なし」しか分からず、単なる入力待ちとクラッシュを区別できない。
    3 時間以上応答の無いものまで並べると状況板が別PCの残骸で埋まる (実測: A-desktop 13 行中 12 行) ので畳む。
    """
    now = _now()
    rows: list[dict] = []
    hidden: dict[str, int] = {}
    for s in remote:
        pc = str(s.get("pc_label") or "?")
        if own_label and pc == own_label:
            continue
        status = str(s.get("status") or "")
        if status == "ended" and not include_all:
            continue
        hq = s.get("hq") if isinstance(s.get("hq"), dict) else {}
        hq_status = str(hq.get("status") or "")
        if hq_status and hq_status in STATUS_LABEL:
            st = hq_status
        elif status == "ended":
            st = "ended"
        else:
            st = "remote_stale" if s.get("stale") else "remote_active"
        since = _parse_ts(hq.get("since")) or _parse_ts(str(s.get("last_heartbeat_at") or "").replace("Z", "+00:00"))
        mins, elapsed = _elapsed(since, now)
        if st == "remote_stale" and mins >= REMOTE_STALE_HIDE_MIN and not include_all:
            hidden[pc] = hidden.get(pc, 0) + 1
            continue
        cwd = str(s.get("cwd") or "")
        title = hq.get("title") or hq.get("last_prompt") or (cwd.rstrip("\\/").split("\\")[-1] if cwd else "(無題)")
        sid = str(s.get("session_id") or "")
        pt = hq.get("pending_tool")
        rows.append(
            {
                "session_id": sid,
                "pc": pc,
                "send_id": None,
                "send_how": "別PC",
                "short": sid[:8],
                "title": str(title),
                "status": st,
                "label": STATUS_LABEL.get(st, st),
                "waiting": st in WAITING,
                "since": since.isoformat(timespec="seconds") if since else None,
                "elapsed_min": mins,
                "elapsed": elapsed,
                "updated_at": str(s.get("last_heartbeat_at") or ""),
                "cwd": cwd,
                "old_path": "OneDrive" in cwd,
                "reason": hq.get("reason") or ("stale" if s.get("stale") else status),
                "last_prompt": hq.get("last_prompt") or "",
                "last_assistant": "",
                "last_assistant_tail": hq.get("last_assistant_tail") or "",
                "question": hq.get("question") or "",
                "pending_tool": pt if isinstance(pt, dict) else None,
                "idle_notified": False,
                "next_action": NEXT_ACTION.get(st, "別PCのセッション。そのPCの前で対応する (メッセージ中継は不可)"),
                "events": {},
                "remote": True,
            }
        )
    return rows, hidden


def load_board(self_sid: str | None, include_all: bool) -> list[dict]:
    now = _now()
    rows: list[dict] = []
    if not BOARD_DIR.exists():
        return rows
    for p in sorted(BOARD_DIR.glob("*.json")):
        if p.name.endswith(".push.json"):  # hq_push の送信記録。状態ファイルではない
            continue
        try:
            st = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(st, dict) or not st.get("session_id"):
            continue
        sid = str(st["session_id"])
        if self_sid and sid == self_sid:
            continue
        status = str(st.get("status") or "idle")
        updated = _parse_ts(st.get("updated_at"))
        if not include_all:
            if status == "ended":
                continue
            if updated and now - updated > timedelta(days=STALE_DAYS):
                continue
        since = _parse_ts(st.get("since"))
        mins, elapsed = _elapsed(since, now)
        cwd = str(st.get("cwd") or "")
        title = st.get("title") or st.get("last_prompt") or "(無題)"
        rows.append(
            {
                "session_id": sid,
                "pc": "",  # main() で自PC名を入れる
                "remote": False,
                "send_id": f"local_{sid}",  # --sessions 指定時は resolve_send_ids が上書き
                "send_how": "推定",
                "short": sid[:8],
                "title": str(title),
                "status": status,
                "label": STATUS_LABEL.get(status, status),
                "waiting": status in WAITING,
                "since": st.get("since"),
                "elapsed_min": mins,
                "elapsed": elapsed,
                "updated_at": st.get("updated_at"),
                "cwd": cwd,
                "old_path": "OneDrive" in cwd,
                "reason": st.get("reason") or "",
                "last_prompt": st.get("last_prompt") or "",
                "last_assistant": st.get("last_assistant") or "",
                "last_assistant_tail": st.get("last_assistant_tail") or "",
                "question": st.get("question") or "",
                "pending_tool": st.get("pending_tool"),
                "idle_notified": bool(st.get("idle_notified")),
                "next_action": NEXT_ACTION.get(status, ""),
                "events": st.get("events") or {},
            }
        )
    return sort_rows(rows)


def sort_rows(rows: list[dict]) -> list[dict]:
    rows.sort(
        key=lambda r: (
            STATUS_ORDER.get(r["status"], 99),
            -r["elapsed_min"] if r["waiting"] else 0,
            r["updated_at"] or "",
        )
    )
    for i, r in enumerate(rows, 1):
        r["n"] = i
    return rows


def _cell(s: str, n: int) -> str:
    s = " ".join(str(s).split()).replace("|", "/")
    return s if len(s) <= n else s[: n - 1] + "…"


def _print_untracked(untracked: list[dict], self_sid: str | None) -> None:
    rest = [s for s in untracked if not (self_sid and s["sessionId"] == f"local_{self_sid}")]
    if not rest:
        return
    print()
    print("## 未計測 (ボードに未登録: フック登録後にまだイベントが無いセッション)")
    print("| タイトル | 動作中 | 最終更新 | cwd |")
    print("|---|---|---|---|")
    for s in rest:
        old = " ⚠旧パス" if "OneDrive" in str(s.get("cwd") or "") else ""
        print(
            f"| {_cell(s.get('title') or '(無題)', 30)} | {'はい' if s.get('isRunning') else '-'} "
            f"| {_cell(str(s.get('lastActivityAt') or '')[:16], 16)} | {_cell(str(s.get('cwd') or ''), 24)}{old} |"
        )
    print("(未計測のセッションを計測に乗せるには、そのセッションに何か 1 つ依頼を投げる)")


def cmd_show(
    rows: list[dict],
    untracked: list[dict] | None = None,
    self_sid: str | None = None,
    hidden: dict[str, int] | None = None,
) -> None:
    if not rows:
        print("(状態ボードにセッションがありません。フックが未登録か、まだ誰も動いていません)")
        _print_untracked(untracked or [], self_sid)
        return
    print("| # | PC | 状態 | 経過 | セッション | 最新 | 備考 |")
    print("|---|---|---|---|---|---|---|")
    for r in rows:
        latest = r["question"] or r["last_assistant_tail"] or r["last_prompt"]
        notes = []
        if r["old_path"]:
            notes.append("⚠旧パス")
        pt = r["pending_tool"]
        if isinstance(pt, dict) and pt.get("name"):
            notes.append(f"🔧{pt['name']}")
        if r["idle_notified"]:
            notes.append("放置")
        print(
            f"| {r['n']} | {_cell(r.get('pc') or '-', 12)} | {r['label']} | {r['elapsed']} | {_cell(r['title'], 28)} "
            f"| {_cell(latest, 60)} | {' '.join(notes)} |"
        )
    waiting = [r for r in rows if r["waiting"]]
    if waiting:
        print()
        print("## 待ち中の詳細")
        for r in waiting:
            if r.get("remote"):
                send = f"別PC {r.get('pc')} (メッセージ中継は不可)"
            elif r["send_id"]:
                send = f"send_id {r['send_id']} ({r['send_how']})"
            else:
                send = "send_id 未特定 (タイトルで手動照合)"
            print(f"### [{r['n']}] {r['title']}  (id {r['short']}, {send})")
            print(f"- 状態: {r['label']} / 経過 {r['elapsed']} / 理由: {r['reason']}")
            if r["old_path"]:
                print(f"- ⚠ 旧 OneDrive パスで動作中: {r['cwd']}")
            pt = r["pending_tool"]
            if isinstance(pt, dict) and pt.get("name"):
                print(f"- 待っているツール: {pt['name']} — {pt.get('detail', '')}")
            if r["question"]:
                print(f"- 質問: {r['question']}")
            if r["last_assistant_tail"]:
                print(f"- 最後の応答(末尾): {r['last_assistant_tail']}")
            if r["last_prompt"]:
                print(f"- 直前の依頼: {r['last_prompt']}")
            print(f"- 次の一手: {r['next_action']}")
    _print_untracked(untracked or [], self_sid)
    remote_n = sum(1 for r in rows if r.get("remote"))
    print()
    if hidden:
        folded = " / ".join(f"{pc}: 応答なし {n} 件" for pc, n in sorted(hidden.items()))
        print(f"(別PCで {REMOTE_STALE_HIDE_MIN // 60} 時間以上応答の無いセッションは畳んでいます: {folded}。`all` で表示)")
    print(
        f"({len(rows)} セッション (別PC {remote_n}) / 待ち {len(waiting)} 件 / {_now().strftime('%m-%d %H:%M')} 時点)"
    )


def cmd_json(rows: list[dict]) -> None:
    print(json.dumps(rows, ensure_ascii=False, indent=1))


def cmd_diff(rows: list[dict], commit: bool, renotify_min: int) -> None:
    now = _now()
    try:
        snap = json.loads(WATCH_STATE.read_text(encoding="utf-8")) if WATCH_STATE.exists() else {}
    except (OSError, ValueError):
        snap = {}
    if not isinstance(snap, dict):
        snap = {}
    new_waits: list[dict] = []
    renotify: list[dict] = []
    resolved: list[dict] = []
    next_snap: dict = {}
    seen: set[str] = set()
    for r in rows:
        sid = r["session_id"]
        seen.add(sid)
        prev = snap.get(sid) if isinstance(snap.get(sid), dict) else {}
        entry = {"status": r["status"], "since": r["since"], "title": r["title"], "notified_at": prev.get("notified_at")}
        if r["waiting"]:
            was_same_wait = prev.get("status") == r["status"] and prev.get("since") == r["since"]
            if not was_same_wait:
                new_waits.append(r)
                entry["notified_at"] = now.isoformat(timespec="seconds")
            else:
                last = _parse_ts(prev.get("notified_at"))
                if last is None or now - last >= timedelta(minutes=renotify_min):
                    renotify.append(r)
                    entry["notified_at"] = now.isoformat(timespec="seconds")
        else:
            if prev.get("status") in WAITING:
                resolved.append(r)
            entry["notified_at"] = None
        next_snap[sid] = entry
    for sid, prev in snap.items():
        if sid not in seen and isinstance(prev, dict) and prev.get("status") in WAITING:
            resolved.append({"n": "-", "title": prev.get("title", sid[:8]), "label": "(消滅)", "elapsed": "-"})
    if commit:
        try:
            HQ_DIR.mkdir(parents=True, exist_ok=True)
            WATCH_STATE.write_text(json.dumps(next_snap, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as exc:
            print(f"(snapshot write failed: {exc})", file=sys.stderr)
    if not new_waits and not renotify and not resolved:
        print("NOCHANGE")
        return
    for r in new_waits:
        print(f"NEW  [{r['n']}] {r['label']} {r['elapsed']} — {r['title']}")
    for r in renotify:
        print(f"STILL [{r['n']}] {r['label']} {r['elapsed']} — {r['title']}")
    for r in resolved:
        print(f"OK   [{r['n']}] 解消 — {r['title']}")
    print(
        json.dumps(
            {
                "new": [r["session_id"] for r in new_waits if "session_id" in r],
                "still": [r["session_id"] for r in renotify],
                "resolved": [r.get("session_id") for r in resolved],
            },
            ensure_ascii=False,
        )
    )


def cmd_push(self_sid: str | None) -> None:
    """ローカルの未終了セッションを中継APIへ一括送信する (取りこぼしの補完)。"""
    import hq_push

    if not hq_relay.configured():
        print("relay 設定がありません (relay_local/.env)")
        return
    rows = load_board(None, include_all=False)  # 自セッションも送る (別PCから見えるように)
    sent = 0
    for r in rows:
        res = hq_push.push_session(r["session_id"], end=False, board_dir=BOARD_DIR)
        mark = "OK" if res.get("ok") else f"NG({res.get('code')})"
        hq = res.get("hq_accepted")
        hq_txt = "保存された" if hq else ("サーバー未対応" if hq is False else "未送信")
        print(f"{mark} {r['session_id'][:8]} {r['label']} {_cell(r['title'], 24)} hq={hq_txt}")
        sent += 1 if res.get("ok") else 0
    print(f"pushed {sent}/{len(rows)} (pc_label={hq_relay.load_settings()['pc_label']})")


def cmd_prune(days: int) -> None:
    if not BOARD_DIR.exists():
        print("nothing to prune")
        return
    cutoff = _now() - timedelta(days=days)
    removed = 0
    for p in BOARD_DIR.glob("*.json"):
        try:
            st = json.loads(p.read_text(encoding="utf-8"))
            updated = _parse_ts(st.get("updated_at")) if isinstance(st, dict) else None
        except (OSError, ValueError):
            updated = None
        if updated is None:
            updated = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        if updated < cutoff:
            p.unlink(missing_ok=True)
            removed += 1
    print(f"pruned {removed} file(s) older than {days} day(s)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="HQ 状態ボード読取 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("show", "json", "diff"):
        sp = sub.add_parser(name)
        sp.add_argument("--all", action="store_true", help="終了済み・古いものも含める")
        sp.add_argument("--self", dest="self_sid", default=os.environ.get("HQ_SELF_SID"), help="自セッションIDを除外")
        sp.add_argument("--sessions", help="list_sessions の出力 JSON ファイル (中継先 ID の照合と未計測一覧に使う)")
        sp.add_argument("--remote", action="store_true", help="中継API(041)の /claude-sessions から別PCのセッションも取り込む")
        if name == "diff":
            sp.add_argument("--commit", action="store_true", help="スナップショットを更新する")
            sp.add_argument("--renotify-min", type=int, default=30)
    pp = sub.add_parser("prune")
    pp.add_argument("--days", type=int, default=7)
    ps = sub.add_parser("push", help="ローカルの未終了セッションを中継APIへ一括送信")
    ps.add_argument("--self", dest="self_sid", default=os.environ.get("HQ_SELF_SID"))
    args = ap.parse_args(argv)

    if args.cmd == "prune":
        cmd_prune(args.days)
        return 0
    if args.cmd == "push":
        cmd_push(args.self_sid)
        return 0
    own_label = hq_relay.load_settings()["pc_label"] or "local"
    rows = load_board(args.self_sid, args.all)
    for r in rows:
        r["pc"] = own_label
    sessions = load_sessions(args.sessions)
    untracked = resolve_send_ids(rows, sessions) if sessions else []
    hidden: dict[str, int] = {}
    if args.remote:
        remote, err = hq_relay.list_remote(None if args.all else "active")
        if err:
            print(f"(別PCの取得に失敗: {err})", file=sys.stderr)
        rrows, hidden = remote_rows(remote, own_label, args.all)
        rows = sort_rows(rows + rrows)
    if args.cmd == "show":
        cmd_show(rows, untracked, args.self_sid, hidden)
    elif args.cmd == "json":
        cmd_json(rows)
        if untracked:
            print(json.dumps({"untracked": untracked}, ensure_ascii=False))
    elif args.cmd == "diff":
        cmd_diff(rows, args.commit, args.renotify_min)
    return 0


if __name__ == "__main__":
    sys.exit(main())
