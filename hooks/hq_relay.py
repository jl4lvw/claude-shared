"""HQ 状態ボード ↔ 中継API(041) の薄いクライアント。

フック側の pusher (hq_push.py) と CLI の `--remote` / `push` が共用する。
標準ライブラリのみ (relay_client.py と同じ方針: A/TK で Python 環境が違っても動く)。

中継APIには 2026-09-01 の仕様書を受けて A 側が実装した `/claude-sessions`
(登録・ハートビート・終了・一覧) が既にある。本モジュールはそれに乗り、
登録本文へ HQ 拡張項目 `hq` (状態・理由・題名・質問文・応答末尾など、すべて伏字済み)
を同梱する。サーバーが `hq` 未対応 (422) の間は `hq` 無しで再送し、
一定時間は `hq` を付けずに送る (hq_rejected マーカー)。

設定は relay_local/.env (RELAY_BASE_URL / RELAY_API_KEY / RELAY_SELF_USER_ID)。
環境変数が .env より優先 (relay_client.py と同じ)。PC 名は CLAUDE_HQ_PC_LABEL、
既定は "<RELAY_SELF_USER_ID>-desktop" (A 側の "A-desktop" に合わせる)。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import urlencode

HERE = Path(__file__).resolve().parent
LOCAL_DIR = HERE.parents[0] / "relay_local"
ENV_PATH = Path(os.environ.get("RELAY_ENV_FILE") or (LOCAL_DIR / ".env")).expanduser()
HQ_DIR = Path(os.environ.get("CLAUDE_HQ_DIR", r"C:/ClaudeCode/.hq"))
HQ_REJECTED_MARK = HQ_DIR / "hq_rejected.json"
HQ_REJECTED_TTL = timedelta(hours=6)
TIMEOUT_SEC = 8

HQ_FIELDS = (
    "status",
    "reason",
    "since",
    "title",
    "last_prompt",
    "last_assistant_tail",
    "question",
    "permission_mode",
    "updated_at",
    "last_event",
)


def _load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        if not path.exists():
            return values
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    except OSError:
        pass
    return values


def load_settings() -> dict[str, str]:
    env = _load_env(ENV_PATH)

    def get(name: str) -> str:
        return (os.environ.get(name) or env.get(name, "")).strip()

    user_id = get("RELAY_SELF_USER_ID")
    return {
        "base_url": get("RELAY_BASE_URL").rstrip("/"),
        "api_key": get("RELAY_API_KEY"),
        "user_id": user_id,
        "pc_label": get("CLAUDE_HQ_PC_LABEL") or (f"{user_id}-desktop" if user_id else ""),
    }


def configured(settings: dict[str, str] | None = None) -> bool:
    s = settings or load_settings()
    return bool(s["base_url"] and s["api_key"] and s["pc_label"])


def request_json(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
    settings: dict[str, str] | None = None,
    timeout: float = TIMEOUT_SEC,
) -> tuple[int, Any]:
    """(HTTP status, body) を返す。通信エラーは (0, エラー文字列)。例外は投げない。"""
    s = settings or load_settings()
    url = f"{s['base_url']}{path}"
    if params:
        url += "?" + urlencode(params)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = request.Request(url, data=data, method=method)
    req.add_header("X-API-Key", s["api_key"])
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, (json.loads(body) if body else None)
            except ValueError:
                return resp.status, body
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(detail)
        except ValueError:
            return exc.code, detail
    except (error.URLError, OSError, ValueError) as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def hq_payload(state: dict) -> dict[str, Any]:
    out: dict[str, Any] = {k: state.get(k) for k in HQ_FIELDS if state.get(k) is not None}
    pt = state.get("pending_tool")
    if isinstance(pt, dict) and pt.get("name"):
        out["pending_tool"] = {"name": pt.get("name"), "detail": pt.get("detail"), "at": pt.get("at")}
    out["schema"] = 1
    return out


def hq_rejected_recently(now: datetime | None = None) -> bool:
    try:
        if not HQ_REJECTED_MARK.exists():
            return False
        data = json.loads(HQ_REJECTED_MARK.read_text(encoding="utf-8"))
        at = datetime.fromisoformat(str(data.get("at")))
    except (OSError, ValueError, TypeError):
        return False
    return (now or datetime.now().astimezone()) - at < HQ_REJECTED_TTL


def mark_hq_rejected(detail: Any) -> None:
    try:
        HQ_DIR.mkdir(parents=True, exist_ok=True)
        HQ_REJECTED_MARK.write_text(
            json.dumps({"at": datetime.now().astimezone().isoformat(timespec="seconds"), "detail": str(detail)[:300]}),
            encoding="utf-8",
        )
    except OSError:
        pass


def register_body(state: dict, include_hq: bool, settings: dict[str, str]) -> dict[str, Any]:
    body: dict[str, Any] = {"pc_label": settings["pc_label"], "cwd": state.get("cwd")}
    if state.get("end_reason"):
        body["stop_reason"] = str(state["end_reason"])
    if include_hq:
        body["hq"] = hq_payload(state)
    return body


def push_state(state: dict, end: bool = False, settings: dict[str, str] | None = None) -> dict[str, Any]:
    """1 セッション分を PUT (終了なら POST /end)。結果 dict を返す (例外なし)。"""
    s = settings or load_settings()
    sid = str(state.get("session_id") or "")
    if not sid or not configured(s):
        return {"ok": False, "code": 0, "detail": "not configured or no session_id", "hq_accepted": None}
    if end:
        code, body = request_json(
            "POST", f"/claude-sessions/{sid}/end", {"pc_label": s["pc_label"], "reason": state.get("end_reason")}, settings=s
        )
        return {"ok": 200 <= code < 300, "code": code, "detail": body, "hq_accepted": None, "end": True}
    include_hq = not hq_rejected_recently()
    code, body = request_json("PUT", f"/claude-sessions/{sid}", register_body(state, include_hq, s), settings=s)
    # hq_accepted: True = 応答に hq が入って返った (保存された) / False = 送ったが応答に無い
    # (サーバーが未知項目として黙って捨てた。2026-09-04 時点の実挙動) または 422 / None = 送っていない
    hq_accepted: bool | None = None
    if include_hq and 200 <= code < 300:
        hq_accepted = isinstance(body, dict) and "hq" in body
    if code == 422 and include_hq:
        mark_hq_rejected(body)
        code, body = request_json("PUT", f"/claude-sessions/{sid}", register_body(state, False, s), settings=s)
        hq_accepted = False
    return {"ok": 200 <= code < 300, "code": code, "detail": body, "hq_accepted": hq_accepted}


def list_remote(status_filter: str | None = "active", settings: dict[str, str] | None = None) -> tuple[list[dict], str]:
    """GET /claude-sessions。(rows, error) を返す。error は空文字なら成功。"""
    s = settings or load_settings()
    if not configured(s):
        return [], "relay 設定なし (relay_local/.env)"
    params = {"status_filter": status_filter} if status_filter else None
    code, body = request_json("GET", "/claude-sessions", params=params, settings=s)
    if not 200 <= code < 300:
        return [], f"HTTP {code}: {str(body)[:200]}"
    if isinstance(body, dict):
        body = body.get("sessions") or body.get("items") or []
    return [r for r in body if isinstance(r, dict)] if isinstance(body, list) else [], ""
