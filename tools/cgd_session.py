"""cgd のセッション解決を 1 箇所に集約する(2026-08-28)。

## なぜ共通化するか

ゲート・run 登録・未検証通知・collect が**それぞれ別の方法で**セッションを
判定していると、「通知には出るのに collect は拒む」のような食い違いが起きる。
2026-08-27 の cgd Lv8 で 4 者が「解決ロジックが不統一だと全件表示・
誰でも collect 可へ後退する」と一致指摘したのを受けて 1 本にした。

## fail closed

**取れなければ None を返す。** 呼び出し側は「所有者不明」として扱い、
勝手に共有キーへ落とさないこと。共有キーへ落とすと別セッションの状態が
混入する — 同リポジトリの ctx が同じ理由で同じ判断をしている
(`ctx_ledger.session_key`)。ここはその実装を踏襲している。

## 実測の背景(2026-08-27 の 1 時間で起きたこと)

  ・ゲートを session なしで張ったため**全セッションの codex が遮断**され、
    別セッションの WF が `gate_ambiguous` で停止した
  ・`disarm` が所有者を見ずに**他セッションのゲートまで解除**した
  ・未検証 run の通知に**別セッションの run** が並び、
    言われるまま collect すると他人の成果物を「検証済み」にしてしまう状態だった
"""
from __future__ import annotations

import os
import platform

# ctx が実データで確認している環境変数。順序も合わせる
# (`ctx_ledger.session_key` と食い違うと、同じセッションが別 ID になる)
ENV_VARS = ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID")

_MAX_LEN = 64


def _sanitize(raw: object) -> str | None:
    """ファイル名・JSON キーに使える形へ。使えない値は None。"""
    if not isinstance(raw, str):
        return None
    safe = "".join(c for c in raw.strip() if c.isalnum() or c in "-_")[:_MAX_LEN]
    return safe or None


def resolve_session(explicit: str | None = None) -> str | None:
    """このプロセスのセッション ID。**取れなければ None**(fail closed)。

    `explicit` は CLI の `--session` を想定。明示があれば最優先。
    """
    if explicit is not None:
        return _sanitize(explicit)
    for var in ENV_VARS:
        got = _sanitize(os.environ.get(var))
        if got:
            return got
    return None


def owner_stamp(session: str | None) -> dict:
    """run / ゲートに残す所有者情報。

    **session だけでは足りない。** セッション ID が取れない環境でも
    「誰が作ったか」を後から追えるように pid と host も残す
    (2026-08-27 Lv8 で「owner を構造化せよ」と 4 者が一致)。
    """
    return {
        "session": session,
        "pid": os.getpid(),
        "host": (platform.node() or os.environ.get("COMPUTERNAME") or "unknown"),
    }


def owner_session(owner: object) -> str | None:
    """`owner_stamp` の戻り値、または素の文字列から session を取り出す。

    古い run には owner が無い(`None`)。その場合も None を返し、
    呼び出し側が「所有者不明」として扱えるようにする。
    """
    if isinstance(owner, dict):
        return _sanitize(owner.get("session"))
    return _sanitize(owner)


def ownership(owner: object, me: str | None) -> str:
    """所有関係を 3 値で返す: `mine` / `others` / `unknown`。

    **`unknown` を `mine` に丸めない。** 丸めると、所有者不明の run を
    誰でも collect でき、通知にも全部出るという元の状態へ戻る。
    自分のセッションが分からない場合も `unknown`(自分のものだと断定できない)。
    """
    theirs = owner_session(owner)
    if theirs is None or me is None:
        return "unknown"
    return "mine" if theirs == me else "others"
