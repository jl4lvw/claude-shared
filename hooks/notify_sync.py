"""端末の通知ルールの同期: 041 からルールを取得して手元に置き、Stop の判定の記録を送る (2026-10-17)。

sound_notify.py から切り離した別プロセスとして起動される (フックは待たない)。単独でも使える:
  python notify_sync.py              # 取得と送信の両方
  python notify_sync.py --fetch      # ルールの取得だけ
  python notify_sync.py --upload     # 判定の送信だけ

取得: GET /notify/config?since=<版> … 変わっていれば sanitize して relay_local/notify_rules_cache.json に書く。
      取得できなくても端末は動く (キャッシュ → 内蔵の既定の順で使う)。
送信: relay_local/notify_decisions.jsonl のうち未送信の行を POST /notify/decisions へ (1 回 50 件まで)。
      環境変数 CLAUDE_NOTIFY_UPLOAD=0 で送信だけ止められる。本文は notify_rules が伏字化済み。
      送信済みの印は relay_local/notify_sync_state.json に残す (記録ファイルが切り詰められても二重に送らない)。

失敗は relay_local/notify_hook.log に残し、必ず exit 0。API キーはログに出さない。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib import error, request

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lw_phone_notify as lw  # noqa: E402
import notify_rules as rules  # noqa: E402
from notify_common import LOCAL, file_lock, log  # noqa: E402

STATE = LOCAL / "notify_sync_state.json"
BATCH = 50
SENT_KEEP = 500
TIMEOUT_SEC = 10


def _call(method: str, base: str, key: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = request.Request(base + path, method=method, data=data)
    req.add_header("X-API-Key", key)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with lw._OPENER.open(req, timeout=TIMEOUT_SEC) as resp:  # リダイレクトは辿らない
        return json.loads(resp.read().decode("utf-8") or "{}")


def _read_state() -> dict:
    try:
        raw = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    sent = raw.get("sent_ids")
    ver = raw.get("config_version")
    return {
        "config_version": ver if isinstance(ver, int) and not isinstance(ver, bool) else None,
        "sent_ids": [s for s in sent if isinstance(s, str)] if isinstance(sent, list) else [],
    }


def _write_state(state: dict) -> None:
    state["sent_ids"] = state["sent_ids"][-SENT_KEEP:]
    tmp = STATE.with_name(f"{STATE.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, STATE)
    except OSError as exc:
        log(f"同期の状態を書けません: {type(exc).__name__}")


def _write_cache(cfg: dict) -> None:
    tmp = rules.RULES_CACHE.with_name(f"{rules.RULES_CACHE.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, rules.RULES_CACHE)


def fetch(base: str, key: str) -> bool:
    """ルールを取得してキャッシュする。変更があって書けたら True。"""
    state = _read_state()
    path = "/notify/config" + (f"?since={state['config_version']}" if state["config_version"] is not None else "")
    try:
        out = _call("GET", base, key, path)
    except error.HTTPError as exc:
        log(f"ルール取得失敗: HTTP {exc.code}")
        return False
    except Exception as exc:  # noqa: BLE001
        log(f"ルール取得失敗: {type(exc).__name__}: {str(exc)[:120]}")
        return False
    version = out.get("version")
    if out.get("unchanged") or not isinstance(out.get("config"), dict) or not isinstance(version, int):
        return False
    cfg = rules.sanitize(out["config"])
    cfg["version"] = version
    try:
        _write_cache(cfg)
    except OSError as exc:
        log(f"ルールのキャッシュを書けません: {type(exc).__name__}")
        return False
    state["config_version"] = version
    _write_state(state)
    return True


def _pending(state: dict) -> list[tuple[str, dict]]:
    """未送信の判定の記録 (id, 送る内容) を古い順に返す。id は 日時|セッション。"""
    try:
        lines = rules.DECISIONS.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    sent = set(state["sent_ids"])
    out: list[tuple[str, dict]] = []
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict) or not isinstance(d.get("at"), str):
            continue
        rid = f"{d['at']}|{d.get('sid', '')}"
        if rid in sent:
            continue
        out.append((rid, {
            "at": d["at"], "sid": str(d.get("sid", ""))[:64], "title": str(d.get("title", ""))[:80],
            "notify": bool(d.get("notify")), "reason": str(d.get("reason", ""))[:60],
            "rule": str(d.get("rule", ""))[:120], "tail": str(d.get("tail", ""))[-rules.SNIPPET_CHARS:],
        }))
    return out


def upload(base: str, key: str) -> int:
    """未送信の判定の記録を送る。送れた件数を返す。"""
    if os.environ.get("CLAUDE_NOTIFY_UPLOAD") == "0":
        return 0
    with file_lock(STATE):  # 同時に動いた別プロセスと二重に送らない
        state = _read_state()
        todo = _pending(state)[:BATCH]
        if not todo:
            return 0
        try:
            _call("POST", base, key, "/notify/decisions", {"items": [item for _, item in todo]})
        except error.HTTPError as exc:
            log(f"判定の送信失敗: HTTP {exc.code}")
            return 0
        except Exception as exc:  # noqa: BLE001
            log(f"判定の送信失敗: {type(exc).__name__}: {str(exc)[:120]}")
            return 0
        state["sent_ids"] += [rid for rid, _ in todo]
        _write_state(state)
        return len(todo)


def run(do_fetch: bool = True, do_upload: bool = True) -> None:
    try:
        base, key = lw.load_env()
    except OSError:
        log("同期: .env が読めない")
        return
    if not base or not key or not lw.base_url_ok(base):
        log("同期: RELAY_BASE_URL/RELAY_API_KEY が無い、または https ではないので行わない")
        return
    if do_fetch:
        fetch(base, key)
    if do_upload:
        upload(base, key)


def main(argv: list[str]) -> int:
    try:
        want_fetch, want_upload = "--fetch" in argv, "--upload" in argv
        if not want_fetch and not want_upload:
            want_fetch = want_upload = True
        run(want_fetch, want_upload)
    except Exception as exc:  # noqa: BLE001
        log(f"同期: 例外 {type(exc).__name__}: {str(exc)[:150]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
