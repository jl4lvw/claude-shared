"""通知フック(sound_notify.py / lw_phone_notify.py / notify_common.py)のテスト。

固定したいこと (2026-10-10 の Lv3 レビュー ×5 で指摘された点を含む):
  - AskUserQuestion の 3 経路は全て「選択肢」に畳み、権限待ちを二重に鳴らさない
  - 重複の単位は「同じセッションの同じ種類」。別セッション・別種類(質問の直後の本物の権限)は落とさない
  - 判定と記録は並行実行でも 1 回だけ許可し、記録を失わない。壊れた記録・時計の巻き戻しで止まらない
  - 音の起動失敗でスマホ通知を止めない (逆も同様)。環境変数の効く範囲が説明どおり
  - Stop は末尾の依頼文だけに反応し、完了報告(「承認済み」「1. 2. の列挙」)では鳴らさない。hq_board に依存しない
  - 読み上げは PowerShell に埋め込むので、波括弧を str.format に通すと無言で落ちる
  - 送信に失敗したら窓を縮めて再通知を通す。404 で旧経路に落ちない。http への鍵送信・リダイレクトを拒否
  - ログにイベント本文(message)を残さない
"""
from __future__ import annotations

import inspect
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib import error

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hooks"))

import lw_phone_notify as lw  # noqa: E402
import notify_common as nc  # noqa: E402
import sound_notify as sn  # noqa: E402


def ev(name: str, **kw: object) -> dict:
    return {"hook_event_name": name, **kw}


# --- classify ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "event, expected",
    [
        (ev("PermissionRequest", tool_name="Bash"), sn.PERMISSION),
        (ev("PermissionRequest", tool_name="AskUserQuestion"), sn.QUESTION),
        (ev("PreToolUse", tool_name="AskUserQuestion"), sn.QUESTION),
        (ev("Notification", notification_type="permission_prompt",
            message="Claude needs your permission to use AskUserQuestion"), sn.QUESTION),
        (ev("Notification", notification_type="permission_prompt",
            message="Claude needs your permission to use Bash"), sn.PERMISSION),
        (ev("Notification", notification_type="idle_prompt"), sn.IDLE),
        (ev("Notification", notification_type="elicitation_dialog"), sn.CONFIRM),
        (ev("Stop", last_assistant_message="どれにしますか？"), sn.REPLY),
    ],
)
def test_classify_text(event: dict, expected: str) -> None:
    hit = sn.classify(event)
    assert hit is not None and hit[1] == expected


@pytest.mark.parametrize(
    "event",
    [
        ev("PreToolUse", tool_name="Bash"),
        ev("Notification", notification_type="auth_success"),
        ev("Stop", last_assistant_message="修正しました。テストも通っています。"),
        ev("SessionStart"),
        ev("Stop", stop_hook_active=True, last_assistant_message="どれにしますか？"),
        ev("Stop"),
    ],
)
def test_classify_ignored(event: dict) -> None:
    assert sn.classify(event) is None


@pytest.mark.parametrize(
    "text",
    [
        "どれにしますか？",
        "実装が完了しました。確認してください。",
        "対象フォルダの指定をお願いします。",
        "進めてよろしいですか",
        "次のどれで進めますか？\n1. A案\n2. B案",
        "Please provide the target directory.",
        "which environment should I use",
        "Should I continue?",
    ],
)
def test_stop_requests_notify(text: str) -> None:
    assert sn.looks_like_request(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "承認済みです。修正を完了しました。",
        "承認処理の不具合を修正しました。",
        "質問「どれにしますか？」の表示を修正しました。作業は完了です。",
        "変更点:\n1. ログを修正\n2. テストを追加",
        "OK、完了です",
        "URL は https://example.test/?x=1 です。修正完了。",
        "",
        "   ",
    ],
)
def test_stop_completion_reports_do_not_notify(text: str) -> None:
    assert sn.looks_like_request(text) is False


def test_stop_does_not_depend_on_hq_board() -> None:
    src = inspect.getsource(sn)
    assert "import hq_board" not in src and "_transcript_info" not in src


# --- 読み上げ・種類コードの定義 --------------------------------------------------------
def test_definitions_are_consistent() -> None:
    assert set(sn.SPEECH_EN) == set(sn.PHONE_KIND)
    assert set(sn.PHONE_KIND.values()) == set(lw.KINDS) - {"test"}


def test_speak_template_survives_braces() -> None:
    """PS_SPEAK は波括弧を含む。str.format を使うと KeyError で無言で落ちる退行を防ぐ。"""
    out = sn.PS_SPEAK.replace("__TEXT__", sn.SPEECH_EN[sn.PERMISSION])
    assert "Permission needed" in out and "__TEXT__" not in out


def test_play_builds_real_command(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sn, "SOUND_DIR", tmp_path)  # WAV 無し = 従来の TTS
    got: list[list[str]] = []
    monkeypatch.setattr(sn, "_spawn", lambda args: got.append(args))
    monkeypatch.delenv("CLAUDE_SOUND_SPEAK", raising=False)
    sn.play(sn.CHIME_P, sn.PERMISSION)
    exe, *flags, cmd = got[0]
    assert exe.lower().endswith(("powershell.exe", "pwsh.exe", "powershell", "pwsh"))
    assert "Windows Exclamation.wav" in cmd and "Permission needed" in cmd
    assert "__" not in cmd and "Test-Path" in cmd  # 置換漏れなし・wav が無い端末のフォールバックあり


def test_play_uses_wav_when_present(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_SOUND_VOICE", "a")
    (tmp_path / "a_reply.wav").write_bytes(b"x")
    monkeypatch.setattr(sn, "SOUND_DIR", tmp_path)
    monkeypatch.delenv("CLAUDE_SOUND_SPEAK", raising=False)
    got: list[list[str]] = []
    monkeypatch.setattr(sn, "_spawn", lambda args: got.append(args))
    sn.play(sn.CHIME_Q, sn.REPLY)
    cmd = got[0][-1]
    assert "reply.wav" in cmd and "SoundPlayer" in cmd and "Speak" not in cmd and "__" not in cmd
    sn.play(sn.CHIME_Q, sn.QUESTION)  # question.wav は無い -> TTS
    assert "Question waiting" in got[1][-1]


def test_voice_prefix_from_pc_name_and_override(monkeypatch, tmp_path: Path) -> None:
    for pre in ("a", "tk", "zz"):
        (tmp_path / f"{pre}_idle.wav").write_bytes(b"x")
    monkeypatch.setattr(sn, "SOUND_DIR", tmp_path)
    monkeypatch.delenv("CLAUDE_SOUND_VOICE", raising=False)
    monkeypatch.setattr(sn.platform, "node", lambda: "DESKTOP-7OSURHD")
    assert sn.voice_wav(sn.IDLE).name == "a_idle.wav"
    monkeypatch.setattr(sn.platform, "node", lambda: "RYZEN7-5800X")
    assert sn.voice_wav(sn.IDLE).name == "tk_idle.wav"
    monkeypatch.setattr(sn.platform, "node", lambda: "UNKNOWN-PC")
    assert sn.voice_wav(sn.IDLE) is None
    monkeypatch.setenv("CLAUDE_SOUND_VOICE", "zz")
    assert sn.voice_wav(sn.IDLE).name == "zz_idle.wav"
    monkeypatch.setenv("CLAUDE_SOUND_VOICE", "../x")  # 不正な接頭辞は無視して TTS
    assert sn.voice_wav(sn.IDLE) is None


def test_speak_off_silences_wav(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "a_idle.wav").write_bytes(b"x")
    monkeypatch.setattr(sn, "SOUND_DIR", tmp_path)
    monkeypatch.setenv("CLAUDE_SOUND_VOICE", "a")
    monkeypatch.setenv("CLAUDE_SOUND_SPEAK", "0")
    got: list[list[str]] = []
    monkeypatch.setattr(sn, "_spawn", lambda args: got.append(args))
    sn.play(sn.CHIME_Q, sn.IDLE)
    assert "a_idle.wav" not in got[0][-1] and "Speak" not in got[0][-1]


def test_shipped_wavs_cover_all_kinds() -> None:
    for text in sn.PHONE_KIND:
        assert sn.voice_wav(text) is not None, text


def test_play_without_speech(monkeypatch) -> None:
    got: list[list[str]] = []
    monkeypatch.setattr(sn, "_spawn", lambda args: got.append(args))
    monkeypatch.setenv("CLAUDE_SOUND_SPEAK", "0")
    sn.play(sn.CHIME_Q, sn.QUESTION)
    assert "Speak" not in got[0][-1]


# --- 重複抑止 (notify_common.claim) ------------------------------------------------------
def test_claim_window_boundaries(tmp_path: Path) -> None:
    st = tmp_path / "s.json"
    assert nc.claim(st, "k", 15, now=1000.0) is not None
    assert nc.claim(st, "k", 15, now=1014.9) is None       # 窓の直前
    assert nc.claim(st, "k", 15, now=1015.0) is not None   # ちょうど
    assert nc.claim(st, "other", 15, now=1015.1) is not None  # 別キーは落とさない


def test_claim_survives_clock_going_backwards(tmp_path: Path) -> None:
    st = tmp_path / "s.json"
    nc.claim(st, "k", 15, now=10_000.0)
    assert nc.claim(st, "k", 15, now=10_000.0 - 3600) is not None  # 1 時間戻っても抑止し続けない


@pytest.mark.parametrize("content", ["[]", "not json", '{"k": "bad"}', '{"k": null}', '{"k": true}', '{"k": NaN}', ""])
def test_claim_survives_broken_state(tmp_path: Path, content: str) -> None:
    st = tmp_path / "s.json"
    st.write_text(content, encoding="utf-8")
    assert nc.claim(st, "k", 15, now=2000.0) is not None
    assert nc.claim(st, "k", 15, now=2001.0) is None


def test_claim_allows_exactly_one_under_concurrency(tmp_path: Path) -> None:
    st = tmp_path / "s.json"
    with ThreadPoolExecutor(16) as pool:
        results = list(pool.map(lambda _: nc.claim(st, "same", 60), range(16)))
    assert sum(r is not None for r in results) == 1


def test_claim_does_not_lose_updates_under_concurrency(tmp_path: Path) -> None:
    st = tmp_path / "s.json"
    keys = [f"k{i}" for i in range(12)]
    with ThreadPoolExecutor(12) as pool:
        list(pool.map(lambda k: nc.claim(st, k, 60), keys))
    assert set(json.loads(st.read_text(encoding="utf-8"))) == set(keys)


def test_claim_proceeds_when_lock_cannot_be_taken(tmp_path: Path, monkeypatch) -> None:
    """ロックが取れなくても通知は止めない (fail-open)。"""
    st = tmp_path / "s.json"
    (tmp_path / "s.json.lock").write_text("x", encoding="utf-8")
    monkeypatch.setattr(nc, "LOCK_WAIT_SEC", 0.05)
    monkeypatch.setattr(nc, "LOCK_STALE_SEC", 3600)
    assert nc.claim(st, "k", 15, now=3000.0) is not None


def test_fail_backoff_allows_retry_after_short_wait(tmp_path: Path) -> None:
    st = tmp_path / "s.json"
    _, at = nc.claim(st, "k", 15, now=5000.0)
    nc.fail_backoff(st, "k", at, window=15, retry_after=5)
    assert nc.claim(st, "k", 15, now=5004.0) is None
    assert nc.claim(st, "k", 15, now=5005.1) is not None


def test_debounce_is_per_session_and_kind(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sn, "STAMP", tmp_path / "stamp.json")
    assert sn.debounced("S1", sn.QUESTION) is False
    assert sn.debounced("S1", sn.QUESTION) is True      # 同じセッションの同じ待ち(別経路)は畳む
    assert sn.debounced("S2", sn.QUESTION) is False     # 別セッションは落とさない
    assert sn.debounced("S1", sn.PERMISSION) is False   # 質問の直後の本物の権限待ちも落とさない
    assert sn.debounced("S1", sn.REPLY) is False


# --- main: 音とスマホの独立・環境変数 -------------------------------------------------------
@pytest.fixture()
def run_main(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sn, "STAMP", tmp_path / "stamp.json")
    monkeypatch.setattr(sn, "EVENT_LOG", tmp_path / "events")
    monkeypatch.setattr(sn.rules, "DECISIONS", tmp_path / "decisions.jsonl")
    monkeypatch.setattr(sn, "spawn_sync", lambda *a, **k: None)   # 実際に同期プロセスを起動しない
    monkeypatch.setattr(sn, "SYNC_STAMP", tmp_path / "sync.json")
    monkeypatch.setattr(sn.rules, "RULES_LOCAL", tmp_path / "none_local.json")
    monkeypatch.setattr(sn.rules, "RULES_CACHE", tmp_path / "none_cache.json")
    for name in ("CLAUDE_NOTIFY", "CLAUDE_SOUND_NOTIFY", "CLAUDE_PHONE_NOTIFY"):
        monkeypatch.delenv(name, raising=False)
    calls: dict[str, list] = {"play": [], "phone": [], "log": []}
    monkeypatch.setattr(sn, "play", lambda *a, **k: calls["play"].append(a))
    monkeypatch.setattr(sn, "phone", lambda *a, **k: calls["phone"].append(a))
    monkeypatch.setattr(sn, "phone_enabled", lambda: True)
    monkeypatch.setattr(sn, "log", lambda m: calls["log"].append(m))

    def run(event: dict) -> int:
        monkeypatch.setattr(sn, "read_event", lambda: event)
        return sn.main()

    run.calls = calls  # type: ignore[attr-defined]
    return run


PERM_EVENT = ev("PermissionRequest", tool_name="Bash", session_id="S1")


def test_main_plays_and_notifies_phone(run_main) -> None:
    assert run_main(PERM_EVENT) == 0
    assert len(run_main.calls["play"]) == 1 and len(run_main.calls["phone"]) == 1


def test_main_phone_still_runs_when_sound_start_fails(run_main, monkeypatch) -> None:
    def boom(*a, **k):
        raise FileNotFoundError("powershell")

    monkeypatch.setattr(sn, "play", boom)
    assert run_main(PERM_EVENT) == 0
    assert len(run_main.calls["phone"]) == 1
    assert any("音の起動失敗" in m for m in run_main.calls["log"])


def test_main_sound_still_runs_when_phone_start_fails(run_main, monkeypatch) -> None:
    def boom(*a, **k):
        raise OSError("x")

    monkeypatch.setattr(sn, "phone", boom)
    assert run_main(PERM_EVENT) == 0
    assert len(run_main.calls["play"]) == 1


def test_main_phone_is_not_gated_by_sound_debounce(run_main) -> None:
    run_main(PERM_EVENT)
    run_main(PERM_EVENT)  # 同じ待ちの別経路: 音は畳むが、スマホ側の判定は lw_phone_notify に任せる
    assert len(run_main.calls["play"]) == 1 and len(run_main.calls["phone"]) == 2


def test_main_other_session_is_not_dropped(run_main) -> None:
    run_main(PERM_EVENT)
    run_main(ev("PermissionRequest", tool_name="Bash", session_id="S2"))
    assert len(run_main.calls["play"]) == 2


@pytest.mark.parametrize(
    "var, plays, phones",
    [("CLAUDE_NOTIFY", 0, 0), ("CLAUDE_SOUND_NOTIFY", 0, 1), ("CLAUDE_PHONE_NOTIFY", 1, 0)],
)
def test_env_switches(run_main, monkeypatch, var: str, plays: int, phones: int) -> None:
    monkeypatch.setenv(var, "0")
    run_main(PERM_EVENT)
    assert (len(run_main.calls["play"]), len(run_main.calls["phone"])) == (plays, phones)


def test_main_ignores_irrelevant_event(run_main) -> None:
    assert run_main(ev("SessionStart")) == 0
    assert run_main.calls["play"] == [] and run_main.calls["phone"] == []


def test_main_never_raises_on_bad_event(run_main, monkeypatch) -> None:
    monkeypatch.setattr(sn, "classify", lambda e: (_ for _ in ()).throw(RuntimeError("x")))
    assert run_main(PERM_EVENT) == 0
    assert any("判定失敗" in m for m in run_main.calls["log"])


def test_read_event_non_dict_json(monkeypatch) -> None:
    class FakeStdin:
        buffer = type("B", (), {"read": staticmethod(lambda: b"[1,2]")})()

    monkeypatch.setattr(sn.sys, "stdin", FakeStdin())
    assert sn.read_event() == {}


# --- スマホ起動・セッション名・ログ ------------------------------------------------------------
def test_phone_passes_kind_code_title_and_sid(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sn, "BOARD_DIR", tmp_path)
    (tmp_path / "abc123.json").write_text(json.dumps({"title": "★テスト 1"}), encoding="utf-8")
    seen: list[list[str]] = []
    monkeypatch.setattr(sn, "_spawn", lambda args: seen.append(args))
    sn.phone(sn.QUESTION, {"session_id": "abc123"})
    assert seen[0][-3:] == ["question", "★テスト 1", "abc123"]


def test_session_title_fallback_and_path_safety(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sn, "BOARD_DIR", tmp_path)
    assert sn.session_title({"session_id": "zzzzzzzzzz"}) == "(セッション zzzzzzzz)"
    (tmp_path.parent / "secret.json").write_text(json.dumps({"title": "漏れてはいけない"}), encoding="utf-8")
    for bad in ("../secret", "C:/x/secret", "a/b", ""):
        assert "漏れ" not in sn.session_title({"session_id": bad})


def test_log_event_never_records_message_body(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sn, "EVENT_LOG", tmp_path / "events")
    sn.log_event(ev("Notification", notification_type="auth_success", message="API_KEY=SECRET123"))
    sn.log_event(ev("Notification", notification_type="permission_prompt", message="use AskUserQuestion"))
    text = (tmp_path / "events").read_text(encoding="utf-8")
    assert "SECRET123" not in text and "AskUserQuestion" not in text and "auq" in text


# --- phone_enabled -----------------------------------------------------------------------
@pytest.mark.parametrize(
    "content, expected",
    [
        (None, False),
        ('{"enabled": true}', True),
        ('{"enabled": false}', False),
        ("{}", True),
        ("broken json", True),
        ('\ufeff{"enabled": false}', False),  # BOM 付き (PowerShell 5 で作った場合)
    ],
)
def test_phone_enabled(tmp_path: Path, content: str | None, expected: bool) -> None:
    p = tmp_path / "phone_notify.json"
    if content is not None:
        p.write_bytes(content.encode("utf-8"))
    assert nc.phone_enabled(p) is expected


# --- lw_phone_notify.send --------------------------------------------------------------------
@pytest.fixture()
def phone_env(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "phone_notify.json"
    cfg.write_text(json.dumps({"enabled": True}), encoding="utf-8")
    monkeypatch.setattr(nc, "PHONE_CONFIG", cfg)
    monkeypatch.setattr(lw, "STAMP", tmp_path / "last.json")
    monkeypatch.setattr(lw, "load_env", lambda: ("https://relay.invalid", "key"))
    logs: list[str] = []
    monkeypatch.setattr(lw, "log", lambda m: logs.append(m))
    return tmp_path, logs


def http_error(code: int) -> error.HTTPError:
    return error.HTTPError("https://relay.invalid/notify/phone", code, "x", {}, None)  # type: ignore[arg-type]


def test_send_uses_api_with_kind_and_title(phone_env, monkeypatch) -> None:
    got: list[tuple] = []
    monkeypatch.setattr(lw, "post_api", lambda b, k, kind, title: got.append((kind, title)) or {"sent": True})
    assert lw.send("permission", "★テスト 1", "S1") == {"sent": True}
    assert got == [("permission", "★テスト 1")]


def test_send_dedupes_same_session_and_kind_only(phone_env, monkeypatch) -> None:
    got: list[tuple] = []
    monkeypatch.setattr(lw, "post_api", lambda b, k, kind, title: got.append((kind, title)) or {"sent": True})
    lw.send("permission", "t", "S1")
    lw.send("permission", "t", "S1")   # 同じ待ちの別経路
    lw.send("permission", "t", "S2")   # 別セッション
    lw.send("question", "t", "S1")     # 別種類
    assert len(got) == 3


@pytest.mark.parametrize("code", [403, 404, 405, 502, 503])
def test_send_http_errors_are_logged_without_fallback(phone_env, monkeypatch, code: int) -> None:
    _, logs = phone_env

    def fail(*a):
        raise http_error(code)

    monkeypatch.setattr(lw, "post_api", fail)
    assert lw.send("idle", "t", "S1") is None
    assert any(f"HTTP {code}" in m for m in logs)
    assert not hasattr(lw, "send_legacy")  # 旧経路(lease 付きセッション)は撤去済み


def test_send_failure_shortens_window_for_retry(phone_env, monkeypatch) -> None:
    def fail(*a):
        raise http_error(502)

    monkeypatch.setattr(lw, "post_api", fail)
    lw.send("reply", "t", "S1")
    state = json.loads(lw.STAMP.read_text(encoding="utf-8"))
    value = state["S1|reply"]
    assert nc.claim(lw.STAMP, "S1|reply", lw.MIN_INTERVAL_SEC, now=value + lw.MIN_INTERVAL_SEC - 1) is None
    assert nc.claim(lw.STAMP, "S1|reply", lw.MIN_INTERVAL_SEC, now=value + lw.MIN_INTERVAL_SEC + 5.1) is not None


def test_send_logs_when_server_suppresses(phone_env, monkeypatch) -> None:
    _, logs = phone_env
    monkeypatch.setattr(lw, "post_api", lambda *a: {"sent": False, "reason": "rate_limited"})
    assert lw.send("idle", "t", "S1") == {"sent": False, "reason": "rate_limited"}
    assert any("抑制" in m and "rate_limited" in m for m in logs)


def test_send_unknown_kind_or_disabled_does_nothing(phone_env, monkeypatch) -> None:
    tmp, _ = phone_env
    monkeypatch.setattr(lw, "post_api", lambda *a: pytest.fail("送ってはいけない"))
    assert lw.send("free text", "t") is None
    (tmp / "phone_notify.json").write_text(json.dumps({"enabled": False}), encoding="utf-8")
    assert lw.send("permission", "t") is None


@pytest.mark.parametrize(
    "base, ok",
    [
        ("https://relay.example", True),
        ("http://relay.example", False),
        ("http://localhost:8302", True),
        ("http://127.0.0.1:8302", True),
        ("ftp://x", False),
        ("", False),
    ],
)
def test_api_key_only_goes_to_https_or_localhost(phone_env, monkeypatch, base: str, ok: bool) -> None:
    sent: list[int] = []
    monkeypatch.setattr(lw, "load_env", lambda: (base, "key"))
    monkeypatch.setattr(lw, "post_api", lambda *a: sent.append(1) or {"sent": True})
    lw.send("permission", "t", "S1")
    assert bool(sent) is ok


def test_post_api_request_shape_and_no_redirect(monkeypatch) -> None:
    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"sent": true}'

    class FakeOpener:
        def open(self, req, timeout):
            captured["url"], captured["data"] = req.full_url, json.loads(req.data)
            captured["key"] = req.get_header("X-api-key")
            return FakeResp()

    monkeypatch.setattr(lw, "_OPENER", FakeOpener())
    assert lw.post_api("https://relay.example", "KEY", "permission", "あ" * 100) == {"sent": True}
    assert captured["url"] == "https://relay.example/notify/phone"
    assert captured["key"] == "KEY" and len(captured["data"]["title"]) == lw.TITLE_MAX
    assert lw._NoRedirect().redirect_request(None, None, 302, "x", {}, "http://evil") is None


def test_load_env_handles_bom_and_quotes(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".env").write_bytes('\ufeffRELAY_BASE_URL="https://relay.example/"\nRELAY_API_KEY=abc\n'.encode("utf-8"))
    monkeypatch.setattr(lw, "LOCAL", tmp_path)
    assert lw.load_env() == ("https://relay.example", "abc")


@pytest.mark.parametrize(
    "result, rc",
    [(None, 1), ({"sent": False, "reason": "rate_limited"}, 1), ({"sent": True}, 0)],
)
def test_run_test_exit_codes(monkeypatch, result, rc: int) -> None:
    monkeypatch.setattr(lw, "send", lambda *a, **k: result)
    assert lw.run_test() == rc


def test_main_is_always_zero_for_hook_calls(monkeypatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(lw, "send", boom)
    monkeypatch.setattr(lw, "log", lambda m: None)
    assert lw.main(["permission", "t", "S1"]) == 0
