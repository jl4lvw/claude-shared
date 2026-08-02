"""PostCompact hook: 台帳へ「圧縮が起きた」印を早書きする **保険経路**。

2026-08-02 再設計で主経路から格下げした。この環境 (デスクトップアプリ) では
PostCompact フックに stdin ペイロードが渡らず (実測: 空 stdin)、本番の圧縮
3/3 回すべてを取り逃した。圧縮検知の主経路は ctx_inject.py の
トランスクリプト走査に移し、本フックは「正常なペイロード (または環境変数の
セッション識別子) が来た場合に、次ターンを待たず印を打つ」役割だけを持つ。

二重記録にはならない: ctx_inject 側は「トランスクリプトの boundary 数 −
台帳の COMPACT 行数」の差分だけを追記する件数ベースの突合を行うため、
本フックが先に書けばその分は追記されない。

**どの失敗経路も必ずログを残す** (旧実装は stdin 失敗を無ログ exit 0 して
障害が3日間潜伏した)。additionalContext は返せる仕様だが、告知の文面制御は
ctx_inject に一本化しているため、ここではファイルへの副作用だけを行う。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

import ctx_ledger as L  # noqa: E402

L._SRC = "compact_mark"

_ALLOWED_TRIGGERS = ("auto", "manual")


def _run(payload: dict) -> None:
    key = L.session_key(payload)  # 環境変数フォールバック込み
    if key is None:
        L.log(f"no session identity; payload keys={sorted(payload.keys())}")
        L.orphan_heartbeat("PostCompact", "no-session")
        return
    path = L.ledger_path(key)
    assert path is not None

    trigger = payload.get("trigger")
    if not isinstance(trigger, str) or trigger not in _ALLOWED_TRIGGERS:
        L.log(f"unexpected trigger={trigger!r}")
        trigger = "unknown"

    L.ensure_ledger(path, key)

    # 追記前に transcript と突合する。inject (transcript 検知) と並走したときに
    # 1圧縮へ2行書くと、次の実圧縮の告知が件数突合に飲み込まれる (レビュー実証)。
    # boundary はフック起動時点で既に transcript に書かれている (実データで確認)
    result = "ok"
    skip = False
    tpath = L.derive_transcript(payload, key)
    if tpath is not None:
        scan = L.scan_transcript(tpath, 0)
        rows = len(L.compact_events(L.parse(path)))
        if scan.ok and rows >= len(scan.boundaries):
            L.log(
                f"skip COMPACT append: ledger has {rows} rows for "
                f"{len(scan.boundaries)} boundaries"
            )
            result = "dedup-skip"
            skip = True

    if not skip:
        ok = L.append(path, "COMPACT", f"{trigger} src=hook")
        if not ok:
            L.log(f"append failed for {path}")
            result = "append-failed"

    st_path = L.state_path(key)
    state = L.load_state(st_path)
    L.heartbeat(state, "PostCompact", result, trigger)
    L.save_state(st_path, state)


def main() -> int:
    payload = L.read_payload("compact_mark")
    if payload is None:
        # stdin が空でも環境変数からセッションを特定できれば印は打てる。
        # trigger は失われるが、実際の trigger はトランスクリプト側の突合で補える
        payload = {}
        if L.session_key(payload) is None:
            L.orphan_heartbeat("PostCompact", "no-payload")
            return 0
        L.log("payload unavailable; falling back to env session id")
    try:
        _run(payload)
    except Exception as exc:
        L.log(f"compact_mark failed: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # pragma: no cover — フックは絶対に本流を止めない
        sys.exit(0)
