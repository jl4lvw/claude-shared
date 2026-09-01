"""session_watchdog_hook.py — Claude Codeセッション死活監視のhook(Stop/SessionStart/SessionEnd共通)。

`041.Claude間連携API`の`claude_sessions`テーブルへheartbeatを送るだけの通知専用hook。
自動起動などの副作用は一切持たない(それはTrack2=`lineworks_session_watchdog.py`の責務)。

**opt-in設計(最重要)**: `.claude/settings.local.json`はgit管理下でリポジトリと共に
配布されるため、このhookは他のPC(TK端末等)でも発火しうる。`.claude/relay_local/`配下の
ローカル専用設定ファイル(`session_watchdog.local.json`)が無い/enabled=falseなら、
**サーバー通信を一切行わず即終了する**。運用者(A)のPCだけがこのファイルを作成する。

**fail-open(絶対規約)**: このリポジトリの全hookの規約として、例外発生時もexit 0で
終了する(hookがClaude Codeの動作を妨げてはならない)。HTTP呼び出しは短いtimeoutで
確実に打ち切る(Stopは毎ターン発火するため、遅いhookは体感速度に直結する)。

hook仕様(公式ドキュメント code.claude.com/docs/en/hooks で確認済み):
- stdin JSON共通: session_id, transcript_path, cwd, permission_mode, hook_event_name
- SessionStart固有: reason (startup/resume/clear/compact/fork)
- SessionEnd固有: reason (clear/resume/logout/prompt_input_exit/other)
- 3イベントともexit code 2によるblockは対象外(このhookはnotification専用なので無関係)
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_HTTP_TIMEOUT_SEC = 3


def _find_project_root() -> Path:
    p = Path(__file__).resolve()
    for parent in [p.parent, *p.parents]:
        if (parent / "CLAUDE.md").exists():
            return parent
    raise RuntimeError("project root (CLAUDE.md) が見つかりません")


_ROOT = _find_project_root()
_LOCAL_DIR = _ROOT / ".claude" / "relay_local"
_OPT_IN_PATH = _LOCAL_DIR / "session_watchdog.local.json"
_ENV_PATH = _LOCAL_DIR / ".env"


def _read_input() -> dict[str, Any]:
    """stdin から hook 入力 JSON を読む。失敗時は空 dict。"""
    try:
        raw = sys.stdin.read()
    except OSError:
        return {}
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _load_opt_in() -> dict[str, Any] | None:
    """opt-in設定を読む。無効/未設定/壊れている場合は None(=何もしない)。"""
    if not _OPT_IN_PATH.exists():
        return None
    try:
        config = json.loads(_OPT_IN_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(config, dict) or not config.get("enabled"):
        return None
    pc_label = config.get("pc_label")
    if not isinstance(pc_label, str) or not pc_label.strip():
        return None
    return config


def _load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not _ENV_PATH.exists():
        return values
    for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def _request(base_url: str, api_key: str, method: str, path: str, body: dict[str, Any]) -> None:
    """relay_client.py同様urllibのみ使用(外部依存なし)。失敗しても呼び出し側で握りつぶす。"""
    url = f"{base_url.rstrip('/')}{path}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-API-Key", api_key)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SEC):
        pass


def main() -> int:
    payload = _read_input()
    session_id = payload.get("session_id")
    hook_event_name = payload.get("hook_event_name")
    if not session_id or not hook_event_name:
        return 0

    opt_in = _load_opt_in()
    if opt_in is None:
        return 0  # サーバー通信すら発生させない(他PCでの無害な即終了を優先)

    env = _load_env()
    base_url = env.get("RELAY_BASE_URL", "").strip()
    api_key = env.get("RELAY_API_KEY", "").strip()
    if not base_url or not api_key:
        return 0

    pc_label = opt_in["pc_label"]
    cwd = payload.get("cwd")
    reason = payload.get("reason")

    try:
        if hook_event_name == "SessionEnd":
            _request(
                base_url, api_key, "POST", f"/claude-sessions/{session_id}/end",
                {"pc_label": pc_label, "reason": reason},
            )
        else:
            # SessionStart / Stop はどちらも「生きている」ことのupsert通知として扱う
            body: dict[str, Any] = {"pc_label": pc_label}
            if cwd:
                body["cwd"] = cwd
            if reason:
                body["stop_reason"] = reason
            _request(base_url, api_key, "PUT", f"/claude-sessions/{session_id}", body)
    except (urllib.error.URLError, OSError, TimeoutError):
        pass  # fail-open: 041 APIが落ちていてもセッションを止めない
    except Exception:
        pass  # fail-open: 想定外の例外でも同様

    return 0


if __name__ == "__main__":
    sys.exit(main())
