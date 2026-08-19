"""050.個人タスク管理 PWA の AI 依頼キューを読み書きする決定論的 CLI。

読み取り (list / show / tabs) は無条件で実行できる。
書き込み (note / clear / done) は --approved が無いと実行を拒否する。
これは「ユーザー承認後にのみタスクを変更する」運用ルールをコード側で強制するため。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# 不正サロゲートを含むデータが混ざっても落ちないようにする (CP932 コンソール対策も兼ねる)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_URL = "http://127.0.0.1:8309"
TASK_NAME = "MyTasksAPIServer"
TIMEOUT_SEC = 10
IMAGE_DIR = Path(tempfile.gettempdir()) / "mytasks_images"

PRIORITY_LABEL = {"high": "高", "mid": "中", "low": "低"}


class ApiError(RuntimeError):
    """API 呼び出しが失敗した。"""


@dataclass(frozen=True)
class Task:
    """API から返るタスク 1 件 (必要なフィールドのみ)。"""

    raw: dict[str, Any]

    @property
    def id(self) -> str:
        return str(self.raw.get("id", ""))

    @property
    def title(self) -> str:
        return str(self.raw.get("title", ""))

    @property
    def done(self) -> bool:
        return bool(self.raw.get("done"))

    @property
    def image_count(self) -> int:
        return int(self.raw.get("imageCount") or 0)


def _request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    """API を叩いて JSON を返す。失敗は ApiError に正規化する。"""
    url = f"{BASE_URL}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as res:
            body = res.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise ApiError(f"{method} {path} → HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ApiError(f"{method} {path} → 到達不能: {exc.reason}") from exc
    return json.loads(body) if body else None


def _health() -> dict[str, Any] | None:
    """/health を叩いて結果を返す。到達不能なら None。"""
    try:
        return _request("GET", "/health")
    except ApiError:
        return None


def ensure_server() -> dict[str, Any]:
    """サーバーが落ちていればスケジュールタスクを起動し、復旧を検証して返す。"""
    health = _health()
    if health:
        return health

    print(f"[warn] API 応答なし ({BASE_URL}) → {TASK_NAME} を起動します", file=sys.stderr)
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"Start-ScheduledTask -TaskName {TASK_NAME}"],
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ApiError(f"{TASK_NAME} の起動に失敗: {exc}") from exc

    for _ in range(15):
        time.sleep(1)
        health = _health()
        if health:
            print(f"[ok] API 復旧: {health.get('version', '')}", file=sys.stderr)
            return health
    raise ApiError(
        f"{TASK_NAME} を起動しましたが {BASE_URL}/health が応答しません。"
        " サーバーログを確認してください。"
    )


def fetch_tasks(ai_requested: bool | None = None) -> list[Task]:
    """タスク一覧を取得する。ai_requested=None なら全件。"""
    query = ""
    if ai_requested is not None:
        query = f"?aiRequested={'true' if ai_requested else 'false'}"
    data = _request("GET", f"/api/tasks{query}")
    return [Task(t) for t in (data or {}).get("tasks", [])]


def fetch_task(task_id: str) -> Task:
    """ID でタスクを 1 件取得する (単体 GET が無いので一覧から絞る)。"""
    for task in fetch_tasks():
        if task.id == task_id:
            return task
    raise ApiError(f"タスクが見つかりません: {task_id}")


def fetch_tab_names() -> dict[str, str]:
    """tabId → タブ名 の対応表を返す。"""
    data = _request("GET", "/api/tabs")
    return {t["id"]: t.get("name", "") for t in (data or {}).get("tabs", [])}


def _format_line(task: Task, tabs: dict[str, str]) -> str:
    raw = task.raw
    marks = []
    if task.done:
        marks.append("済")
    if task.image_count:
        marks.append(f"画像{task.image_count}")
    suffix = f" [{'/'.join(marks)}]" if marks else ""
    tab = tabs.get(str(raw.get("tabId", "")), "?")
    prio = PRIORITY_LABEL.get(str(raw.get("priority", "")), str(raw.get("priority", "")))
    return (
        f"  {task.id}  期限 {raw.get('due', '-')}  優先度 {prio}  タブ {tab}\n"
        f"    {task.title}{suffix}"
    )


def cmd_list(args: argparse.Namespace) -> int:
    """AI 依頼フラグ付きタスクを表示する (既定は未完了のみ)。"""
    ensure_server()
    tasks = fetch_tasks(ai_requested=True)
    if not args.all:
        tasks = [t for t in tasks if not t.done]

    if args.json:
        print(json.dumps([t.raw for t in tasks], ensure_ascii=False, indent=2))
        return 0

    if not tasks:
        scope = "AI 依頼タスク" if args.all else "未完了の AI 依頼タスク"
        print(f"現在 {scope} はありません。")
        return 0

    tabs = fetch_tab_names()
    print(f"AI 依頼タスク {len(tasks)} 件" + ("" if args.all else " (未完了のみ)"))
    for task in tasks:
        print(_format_line(task, tabs))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """タスク 1 件の全文と添付画像を表示する。"""
    ensure_server()
    task = fetch_task(args.task_id)
    raw = task.raw
    tabs = fetch_tab_names()

    if args.json:
        print(json.dumps(raw, ensure_ascii=False, indent=2))
        return 0

    print(f"ID       : {task.id}")
    print(f"タイトル : {task.title}")
    print(f"期限     : {raw.get('due', '-')}")
    print(f"優先度   : {PRIORITY_LABEL.get(str(raw.get('priority')), raw.get('priority'))}")
    print(f"タブ     : {tabs.get(str(raw.get('tabId', '')), '?')}")
    print(f"状態     : {'完了' if task.done else '未完了'}"
          f" / AI依頼 {'ON' if raw.get('aiRequested') else 'OFF'}"
          f" ({raw.get('aiRequestedAt') or '-'})")
    print(f"作成     : {raw.get('createdAt', '-')}  更新: {raw.get('updatedAt', '-')}")
    print("--- メモ ---")
    print(raw.get("memo") or "(なし)")
    print("--- 登録時の原文 ---")
    print(raw.get("sourceText") or "(なし)")

    images = (_request("GET", f"/api/tasks/{task.id}/images") or {}).get("images", [])
    print(f"--- 添付画像 {len(images)} 件 ---")
    for img in images:
        print(f"  {img['id']}  {img.get('contentType')}  {img.get('size')} bytes")
    if images and args.save_images:
        dest = IMAGE_DIR / task.id
        dest.mkdir(parents=True, exist_ok=True)
        for img in images:
            ext = str(img.get("contentType", "")).split("/")[-1] or "bin"
            path = dest / f"{img['id']}.{ext}"
            with urllib.request.urlopen(f"{BASE_URL}{img['url']}", timeout=TIMEOUT_SEC) as res:
                path.write_bytes(res.read())
            print(f"  保存: {path}")
    return 0


def cmd_tabs(_args: argparse.Namespace) -> int:
    """タブ一覧を表示する。"""
    ensure_server()
    for tab_id, name in fetch_tab_names().items():
        print(f"  {tab_id}  {name}")
    return 0


def _require_approval(args: argparse.Namespace) -> None:
    """--approved が無い書き込みを拒否する (承認ゲート)。"""
    if not args.approved:
        raise SystemExit(
            "書き込み操作にはユーザー承認が必要です。\n"
            "結果をユーザーに報告し、承認を得てから --approved を付けて再実行してください。"
        )


def cmd_note(args: argparse.Namespace) -> int:
    """メモに処理結果を追記する (PATCH は全置換なので現行メモと結合する)。"""
    _require_approval(args)
    ensure_server()
    task = fetch_task(args.task_id)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"[AI {stamp}] {args.text}"
    current = str(task.raw.get("memo") or "").rstrip()
    memo = f"{current}\n\n{entry}" if current else entry
    _request("PATCH", f"/api/tasks/{task.id}", {"memo": memo})
    print(f"メモを追記しました: {task.id}")
    print(f"  {entry}")
    return 0


def cmd_clear(args: argparse.Namespace) -> int:
    """AI 依頼フラグを外す。"""
    _require_approval(args)
    ensure_server()
    task = fetch_task(args.task_id)
    result = _request("PATCH", f"/api/tasks/{task.id}", {"aiRequested": False})
    if result and result.get("aiRequested"):
        raise ApiError(f"フラグ解除が反映されていません: {task.id}")
    print(f"AI 依頼フラグを解除しました: {task.id} / {task.title}")
    return 0


def cmd_done(args: argparse.Namespace) -> int:
    """タスクを完了にする (フラグ解除は clear で別途行う)。"""
    _require_approval(args)
    ensure_server()
    task = fetch_task(args.task_id)
    result = _request("PATCH", f"/api/tasks/{task.id}", {"done": True})
    if result and not result.get("done"):
        raise ApiError(f"完了が反映されていません: {task.id}")
    print(f"完了にしました: {task.id} / {task.title}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mytasks_client.py",
        description="050.個人タスク管理 PWA の AI 依頼キュー操作 CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="AI 依頼タスク一覧 (既定は未完了のみ)")
    p_list.add_argument("--all", action="store_true", help="完了済みのフラグ付きも含める")
    p_list.add_argument("--json", action="store_true", help="生 JSON で出力")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="タスク 1 件の詳細")
    p_show.add_argument("task_id")
    p_show.add_argument("--save-images", action="store_true", help="添付画像を一時領域へ保存")
    p_show.add_argument("--json", action="store_true", help="生 JSON で出力")
    p_show.set_defaults(func=cmd_show)

    p_tabs = sub.add_parser("tabs", help="タブ一覧")
    p_tabs.set_defaults(func=cmd_tabs)

    p_note = sub.add_parser("note", help="[書込] メモに処理結果を追記")
    p_note.add_argument("task_id")
    p_note.add_argument("text")
    p_note.add_argument("--approved", action="store_true", help="ユーザー承認済み")
    p_note.set_defaults(func=cmd_note)

    p_clear = sub.add_parser("clear", help="[書込] AI 依頼フラグを解除")
    p_clear.add_argument("task_id")
    p_clear.add_argument("--approved", action="store_true", help="ユーザー承認済み")
    p_clear.set_defaults(func=cmd_clear)

    p_done = sub.add_parser("done", help="[書込] タスクを完了にする")
    p_done.add_argument("task_id")
    p_done.add_argument("--approved", action="store_true", help="ユーザー承認済み")
    p_done.set_defaults(func=cmd_done)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ApiError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
