import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hq_board_cli  # noqa: E402
import status_band  # noqa: E402


def _write(board: Path, sid: str, status: str, title: str, since_min: int, updated_min: int) -> None:
    now = datetime.now().astimezone()
    board.joinpath(f"{sid}.json").write_text(
        json.dumps(
            {
                "session_id": sid,
                "status": status,
                "title": title,
                "since": (now - timedelta(minutes=since_min)).isoformat(),
                "updated_at": (now - timedelta(minutes=updated_min)).isoformat(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _app_session(base: Path, app_id: str, cli_id: str, archived: bool = False) -> None:
    d = base / "acct" / "org"
    d.mkdir(parents=True, exist_ok=True)
    d.joinpath(f"{app_id}.json").write_text(
        json.dumps({"sessionId": app_id, "cliSessionId": cli_id, "isArchived": archived}),
        encoding="utf-8",
    )


def test_app_session_ids_maps_cli_id_and_skips_archived(tmp_path: Path) -> None:
    _app_session(tmp_path, "local_app1", "cli1")
    _app_session(tmp_path, "local_app2", "cli2", archived=True)

    got = status_band.app_session_ids({"cli1", "cli2", "cli3"}, tmp_path)

    assert got == {"cli1": "local_app1"}


def test_waiting_newest_first_and_counts(tmp_path: Path, monkeypatch) -> None:
    apps = tmp_path / "apps"
    _app_session(apps, "local_appb", "b")
    monkeypatch.setattr(status_band, "APP_SESSIONS_DIR", apps)
    tmp_path = tmp_path / "board"
    tmp_path.mkdir()
    monkeypatch.setattr(hq_board_cli, "BOARD_DIR", tmp_path)
    _write(tmp_path, "me", "waiting_answer", "自分", 1, 1)
    _write(tmp_path, "a", "waiting_answer", "古い待ち", 600, 600)
    _write(tmp_path, "b", "waiting_permission", "新しい待ち", 10, 10)
    _write(tmp_path, "c", "running", "動いている", 5, 1)
    _write(tmp_path, "d", "running", "止まった記録", 2000, 2000)
    _write(tmp_path, "e", "done", "終わった", 30, 30)

    snap = status_band.collect("me")

    assert snap.ok and snap.waiting_total == 2
    assert [w.title for w in snap.waiting] == ["新しい待ち", "古い待ち"]
    assert snap.waiting[0].kind == "許可待ち" and snap.waiting[0].elapsed == "10分"
    assert snap.waiting[0].session_id == "local_appb"  # アプリのID(HQ のIDに local_ を付けたものではない)
    assert snap.waiting[1].session_id == ""  # アプリの記録が無いものは空
    assert snap.running == 1 and snap.done == 1


def test_more_than_limit_keeps_total(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(status_band, "APP_SESSIONS_DIR", tmp_path / "no-apps")
    monkeypatch.setattr(hq_board_cli, "BOARD_DIR", tmp_path)
    for i in range(6):
        _write(tmp_path, f"s{i}", "waiting_answer", f"待ち{i}", i + 1, i + 1)

    snap = status_band.collect(None)

    assert len(snap.waiting) == status_band.MAX_WAITING and snap.waiting_total == 6


def test_missing_board_is_error_not_zero(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(hq_board_cli, "BOARD_DIR", tmp_path / "none")

    snap = status_band.collect(None)

    assert snap.ok is False and snap.error
