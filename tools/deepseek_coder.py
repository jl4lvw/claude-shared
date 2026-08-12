"""DeepSeek API クライアント — コード生成 / 設計相談（advisor）の2モード対応.

呼び出しごとに API レスポンスの usage を取得し、トークン数と概算料金を
stderr に出力する。同時にセッション累計を JSON に保存し、4 時間以内の
呼び出しは累計加算、それ以降は新規セッションとして自動リセットする。

主な仕様:
- セッションファイル: .deepseek_usage_session.json（atomic write 保護）
- 為替: 既定 1USD=150JPY。環境変数 DEEPSEEK_USD_TO_JPY で上書き可能
- 未知モデルは deepseek-v4-flash 料金にフォールバック（stderr に警告）
- usage は属性 / dict / OpenAI 互換 prompt_tokens_details.cached_tokens に対応
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from openai import OpenAI

# === cgd exit code registry (keep in sync with gemini_advisor.py / qwen_advisor.py / cgd_doctor.py) ===
EXIT_OK = 0
EXIT_AUTH = 10
EXIT_QUOTA = 20
EXIT_TIMEOUT = 30
EXIT_NETWORK = 40
EXIT_INVALID_INPUT = 50
EXIT_GENERIC = 1


def _classify_openai_error(exc: BaseException) -> int:
    """OpenAI 互換クライアントの例外を cgd 終了コード規約に振り分ける."""
    name = type(exc).__name__
    msg = str(exc).lower()
    if name in {"AuthenticationError", "PermissionDeniedError"}:
        return EXIT_AUTH
    if name == "RateLimitError":
        return EXIT_QUOTA
    if name in {"APITimeoutError", "Timeout"}:
        return EXIT_TIMEOUT
    if name in {"APIConnectionError", "ConnectionError"}:
        return EXIT_NETWORK
    if name in {"BadRequestError", "UnprocessableEntityError"}:
        return EXIT_INVALID_INPUT
    if "401" in msg or "unauthorized" in msg or "invalid_api_key" in msg or "invalid api key" in msg:
        return EXIT_AUTH
    if "429" in msg or "quota" in msg or "rate limit" in msg or "rate_limit" in msg:
        return EXIT_QUOTA
    if "timeout" in msg or "timed out" in msg:
        return EXIT_TIMEOUT
    if "connection" in msg or "dns" in msg or "could not resolve" in msg:
        return EXIT_NETWORK
    return EXIT_GENERIC


ROLE_PROMPTS: dict[str, str] = {
    "coder": (
        "You are an expert programmer. "
        "Write clean, production-quality code in the target language indicated by the user prompt "
        "(Python, JavaScript / TypeScript, Go, Bash, HTML / CSS, SQL, etc.), "
        "following that language's idioms, standard library conventions, and the project's existing style. "
        "Apply type hints / annotations when the language supports them (Python type hints, TypeScript, Go types). "
        "Add proper error handling at boundaries (I/O, network, user input), not inside trusted internal code. "
        "Use Japanese for comments and docstrings. "
        "Output only the code unless explicitly asked for explanations. "
        "If the user prompt mentions AGENTS.md / CLAUDE.md, follow those project rules "
        "(e.g. no shebang on Windows, explicit encoding=\"utf-8\" for Python file I/O, no unnecessary abstractions)."
    ),
    "advisor": (
        "あなたは熟練のソフトウェア設計アドバイザーです。"
        "ユーザーから提示される設計案・実装方針・既存コードに対して、"
        "別視点・別アプローチ・見落とし・代替案を日本語で簡潔に提示します。"
        "出力は必ず以下の構造に従ってください:\n"
        "1. **別案** — 提示された案とは異なるアプローチ（1〜3個、各2〜4行）\n"
        "2. **見落としの可能性** — 元案で考慮が薄い点（箇条書き、最大5項目）\n"
        "3. **採否コメント** — 各別案の長所/短所を1行ずつ\n"
        "コード断片を出すときも要点に絞り、長大な実装を貼らないこと。"
    ),
    "reviewer": (
        "あなたは熟練のソフトウェアレビュアーです。"
        "ユーザーから提示される差分・実装・既存コードを、"
        "バグ・設計上の懸念・セキュリティ・副作用・既存仕様との整合性の観点で厳密に評価し、"
        "日本語で簡潔に指摘してください。"
        "出力は必ず以下の構造に従ってください:\n"
        "1. **🔴 重大指摘** — セキュリティ脆弱性 / データ破壊リスク / 公開API仕様逸脱 / 明白な論理バグ（各指摘1〜3行＋根拠1行）\n"
        "2. **🟠 重要指摘** — 設計上の懸念・副作用・テスト不足等（各指摘1〜3行）\n"
        "3. **🟡 注意事項** — スタイル・命名・軽微な改善提案（箇条書き、最大5項目）\n"
        "4. **総評** — 全体評価を1〜3行\n"
        "別案・代替案の提示は不要。レビューに徹してください。"
        "なお、別の AI (Qwen) も並列で同じ対象をレビューするので、"
        "Qwen が見るであろう実装観点・コードレベルの細部より、"
        "論理整合性・設計の理屈・データフローの正しさ・推論寄りの観点を優先的に評価してください。"
        "コード断片を出すときも要点に絞り、長大な実装を貼らないこと。"
    ),
    "critic": (
        "あなたは辛口の評価者です。提示される仕様・画面・コード・差分を、技術的な正しさ（バグの有無）ではなく"
        "『使う人が困らないか』『本来この仕様はどうあるべきか』の観点で、遠慮なく否定的に評価します。"
        "次の2つの立場を併せ持ってください:\n"
        "(1) ITに疎い現場担当者 — 実際に使うときの使いにくさ・わかりにくさ・手数の多さ・"
        "エラー時の困りごと・専門用語の不親切さを、利用者の生の言葉で指摘する\n"
        "(2) 熟練ITアーキテクト — 『本来この仕様はどうあるべきか』を理想形から逆算し、"
        "現状の妥協・場当たり対応・本質を外した設計・優先度の誤り・過剰な複雑さを批判する\n"
        "出力は必ず以下の構造に従ってください:\n"
        "1. **現場の不満** — 使う人視点の困りごと（各項目に 困り度: 高/中/低 を付ける）\n"
        "2. **あるべき論とのギャップ** — 理想形と現状の差、なぜそれが問題か（各2〜4行）\n"
        "3. **そもそも論** — この機能/仕様は本当に要るか・優先度は妥当か（疑問があれば）\n"
        "4. **辛口総評** — 一番の問題点を1〜2行で断言\n"
        "擁護・肯定・『概ね良い』は禁止。粗探しに徹し、改善の方向だけ各指摘に短く添える。"
        "技術的なバグ指摘は他のレビュアーの担当なので深入りしない（使い勝手とあるべき論に集中）。"
    ),
}

# 料金（USD / 1M tokens）: (input_cache_miss, input_cache_hit, output)
# DeepSeek 公式レート（2026/07 時点）。変更時はここだけ書き換える。
# 2026-07-24 に deepseek-chat / deepseek-reasoner は廃止され、
# deepseek-v4-pro / deepseek-v4-flash に統合された（旧エントリは過去ログ参照用に残す）。
PRICING: dict[str, tuple[float, float, float]] = {
    "deepseek-v4-pro": (0.435, 0.003625, 0.87),
    "deepseek-v4-flash": (0.14, 0.0028, 0.28),
    "deepseek-chat": (0.27, 0.07, 1.10),
    "deepseek-coder": (0.27, 0.07, 1.10),
    "deepseek-reasoner": (0.55, 0.14, 2.19),
}

# 未知モデルの料金概算に使うフォールバック先（廃止された deepseek-chat の後継）。
FALLBACK_MODEL: str = "deepseek-v4-flash"

# デフォルトの max_tokens。v4-pro / v4-flash は reasoning_tokens が completion_tokens 予算を
# 消費するため、旧 deepseek-chat 時代の 4096 では reviewer 役などで本文が空になる
# （実測: 500行規模のファイルレビューで reasoning だけで 4096 を使い切り finish_reason=length）。
# 16000 なら同条件で余裕を持って収まることを実測済み（最大でも 4205/16000 = 26% 消費）。
# 2026-08-05: 16000 でも Lv7 の大きめレビューで**予算を使い切って本文が空**になった
# (実測 completion_tokens がぴったり 16000 = finish_reason=length)。
# API は 65536 まで受理することを実測したので 32000 へ引き上げる。
# 課金は実際に生成した分だけなので、上限を上げること自体のコスト増はない。
DEFAULT_MAX_TOKENS: int = 32000

# API が受理する上限（実測）。予算切れで本文が空になったときの**増枠リトライ先**。
# 2026-08-12: 26KB 差分の reviewer 役で reasoning 111,701 文字を出して 32,000 を
# 使い切り、finish_reason=length で本文が空になった (INC-20260812-1117419055b1)。
# 同じ差分でも JSON 出力を要求する pv review モードは完走しており、
# 自由記述の reviewer 役だけが暴発する。呼び出し側が --max-tokens を
# 付け直して再実行するしかなかったが、それは人間が気づいた場合に限られる。
API_MAX_TOKENS: int = 65536

# 為替レート（USD → JPY 換算用）。環境変数 DEEPSEEK_USD_TO_JPY で上書き可能。
DEFAULT_USD_TO_JPY: float = 150.0


def _resolve_usd_to_jpy() -> float:
    """環境変数 DEEPSEEK_USD_TO_JPY があればそれを採用、なければ既定値."""
    raw = os.environ.get("DEEPSEEK_USD_TO_JPY")
    if not raw:
        return DEFAULT_USD_TO_JPY
    try:
        value = float(raw)
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    print(
        f"[DS Usage] WARN: DEEPSEEK_USD_TO_JPY='{raw}' が不正のため既定値 {DEFAULT_USD_TO_JPY} を使用",
        file=sys.stderr,
    )
    return DEFAULT_USD_TO_JPY


SESSION_FILE: Path = Path(__file__).parent / ".deepseek_usage_session.json"
SESSION_TTL_SEC: int = 4 * 3600


def _to_int(source: Any, key: str) -> int:
    """属性アクセス or dict 参照で値を取り出し、安全に int 化する."""
    if source is None:
        return 0
    value: Any
    if isinstance(source, dict):
        value = source.get(key, 0)
    else:
        value = getattr(source, key, 0)
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _extract_cache_hit(usage_obj: Any) -> int:
    """DeepSeek / OpenAI 互換の両方からキャッシュヒットトークン数を抽出する.

    DeepSeek: usage.prompt_cache_hit_tokens
    OpenAI 互換: usage.prompt_tokens_details.cached_tokens
    """
    direct = _to_int(usage_obj, "prompt_cache_hit_tokens")
    if direct > 0:
        return direct
    details: Any
    if isinstance(usage_obj, dict):
        details = usage_obj.get("prompt_tokens_details")
    else:
        details = getattr(usage_obj, "prompt_tokens_details", None)
    return _to_int(details, "cached_tokens")


def _calc_cost(
    model: str, cache_miss: int, cache_hit: int, out_tokens: int
) -> tuple[float, bool]:
    """モデルとトークン内訳から USD 概算料金を算出する.

    Returns:
        (cost_usd, is_fallback) — is_fallback は未知モデルで既定料金を使った場合 True.
    """
    rates = PRICING.get(model)
    is_fallback = rates is None
    if rates is None:
        rates = PRICING[FALLBACK_MODEL]
    in_miss_rate, in_hit_rate, out_rate = rates
    cost = (
        cache_miss * in_miss_rate / 1_000_000
        + cache_hit * in_hit_rate / 1_000_000
        + out_tokens * out_rate / 1_000_000
    )
    return cost, is_fallback


def _fresh_state(now: float) -> dict[str, Any]:
    return {
        "started_at": now,
        "calls": 0,
        "in_miss": 0,
        "in_hit": 0,
        "out": 0,
        "cost": 0.0,
        "last_at": now,
    }


def _coerce_number(value: Any, default: float, as_int: bool) -> float:
    """壊れた JSON 値で例外を投げず、既定値にフォールバックして数値を返す."""
    try:
        if value is None:
            return default
        return int(value) if as_int else float(value)
    except (TypeError, ValueError):
        return default


def _load_session(now: float, reset: bool = False) -> dict[str, Any]:
    """セッション累計を読み込む。TTL 超過 / reset=True なら新規セッション.

    JSON 内の数値が文字列・None・NaN 等で破損していても例外を出さず、
    その項目だけ fresh state の既定値にフォールバックする。
    """
    fresh = _fresh_state(now)
    if reset or not SESSION_FILE.exists():
        return fresh
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return fresh
    if not isinstance(data, dict):
        return fresh

    last_at = _coerce_number(
        data.get("last_at", data.get("started_at", 0)), 0.0, as_int=False
    )
    if now - last_at > SESSION_TTL_SEC:
        return fresh

    int_keys = ("calls", "in_miss", "in_hit", "out")
    sanitized: dict[str, Any] = {
        "started_at": _coerce_number(data.get("started_at", now), now, as_int=False),
        "last_at": last_at if last_at > 0 else now,
        "cost": _coerce_number(data.get("cost", 0.0), 0.0, as_int=False),
    }
    for key in int_keys:
        sanitized[key] = int(_coerce_number(data.get(key, 0), 0, as_int=True))
    return sanitized


def _save_session(state: dict[str, Any]) -> None:
    """累計を atomic write で保存する（read-modify-write の取り違え対策）."""
    try:
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=SESSION_FILE.parent,
            prefix=".deepseek_usage_",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            json.dump(state, tmp, ensure_ascii=False, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, SESSION_FILE)
    except OSError as exc:
        print(f"[DS Usage] WARN: セッションファイル保存失敗: {exc}", file=sys.stderr)


def _format_usage_line(
    label: str,
    cache_miss: int,
    cache_hit: int,
    out_tokens: int,
    cost_usd: float,
    rate: float,
    extra: str = "",
) -> str:
    yen = cost_usd * rate
    return (
        f"[DS Usage] {label}: 入力 {cache_miss:,} (miss) + {cache_hit:,} (hit) "
        f"/ 出力 {out_tokens:,} tok "
        f"(¥{yen:.2f} / ${cost_usd:.4f}){extra}"
    )


def _track_usage(model: str, usage_obj: Any, reset: bool = False) -> None:
    """API レスポンスの usage を集計し、stderr に今回 / 累計を表示する."""
    if usage_obj is None:
        print("[DS Usage] WARN: usage 情報がレスポンスに含まれていません", file=sys.stderr)
        return

    prompt_tokens = _to_int(usage_obj, "prompt_tokens")
    completion_tokens = _to_int(usage_obj, "completion_tokens")
    cache_hit = _extract_cache_hit(usage_obj)
    cache_miss = max(prompt_tokens - cache_hit, 0)

    cost_now, is_fallback = _calc_cost(model, cache_miss, cache_hit, completion_tokens)
    if is_fallback:
        print(
            f"[DS Usage] WARN: 未登録モデル '{model}' のため {FALLBACK_MODEL} 料金で概算しています",
            file=sys.stderr,
        )

    rate = _resolve_usd_to_jpy()
    now = time.time()
    state = _load_session(now, reset=reset)
    state["calls"] = int(state.get("calls", 0)) + 1
    state["in_miss"] = int(state.get("in_miss", 0)) + cache_miss
    state["in_hit"] = int(state.get("in_hit", 0)) + cache_hit
    state["out"] = int(state.get("out", 0)) + completion_tokens
    state["cost"] = float(state.get("cost", 0.0)) + cost_now
    state["last_at"] = now
    _save_session(state)

    started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(state["started_at"]))
    print(
        _format_usage_line(
            "今回",
            cache_miss,
            cache_hit,
            completion_tokens,
            cost_now,
            rate,
            f" [model={model}]",
        ),
        file=sys.stderr,
    )
    print(
        _format_usage_line(
            "累計",
            int(state["in_miss"]),
            int(state["in_hit"]),
            int(state["out"]),
            float(state["cost"]),
            rate,
            f" [{state['calls']} calls / since {started} / 1USD=¥{rate:.2f}]",
        ),
        file=sys.stderr,
    )


def call_deepseek(
    prompt: str,
    role: str = "coder",
    model: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = 0.0,
    track: bool = True,
    reset_session: bool = False,
) -> str:
    """DeepSeek API を呼び出す.

    Args:
        prompt: ユーザープロンプト.
        role: 'coder'（コード生成・既存挙動）または 'advisor'（設計相談・別案出し）.
        model: モデル名. 省略時は role に応じて自動選択（coder→deepseek-v4-pro, それ以外→deepseek-v4-flash）.
        max_tokens: 最大出力トークン数.
        temperature: サンプリング温度.
        track: True なら usage を集計して stderr に出力する.
        reset_session: True なら累計をリセットしてから今回分を記録する.

    Returns:
        DeepSeek からの応答テキスト.
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("ERROR: DEEPSEEK_API_KEY が設定されていません", file=sys.stderr)
        sys.exit(EXIT_AUTH)

    if role not in ROLE_PROMPTS:
        print(f"ERROR: 未知の role '{role}'. 使えるのは {list(ROLE_PROMPTS)}", file=sys.stderr)
        sys.exit(EXIT_INVALID_INPUT)

    if model is None:
        model = "deepseek-v4-pro" if role == "coder" else "deepseek-v4-flash"

    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

    def _create(budget: int):
        try:
            return client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": ROLE_PROMPTS[role]},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=budget,
                temperature=temperature,
            )
        except Exception as exc:
            code = _classify_openai_error(exc)
            print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
            sys.exit(code)

    response = _create(max_tokens)
    if track:
        _track_usage(model, getattr(response, "usage", None), reset=reset_session)

    choice = response.choices[0]
    content = (choice.message.content or "").strip()
    finish = getattr(choice, "finish_reason", None)

    # **予算切れで本文が空なら、一度だけ増枠して取り直す。**
    # 以前はここで非 0 終了するだけだったので、レビュアー 1 人が丸ごと欠けた状態で
    # run が失敗し、人間が --max-tokens を付け直して回し直すしかなかった。
    # reasoning が予算を食う挙動はモデル側の都合で、呼び出し側からは事前に読めない。
    # 課金は生成した分だけなので、上限を上げること自体のコストは無い
    # (使い切った 1 回分は捨てる形になるが、失敗して回し直すのと同額)。
    if not content and finish == "length" and max_tokens < API_MAX_TOKENS:
        print(
            f"WARNING: 出力予算({max_tokens} tok)を推論で使い切り本文が空でした。"
            f" {API_MAX_TOKENS} tok に増枠して 1 回だけ取り直します。",
            file=sys.stderr,
        )
        response = _create(API_MAX_TOKENS)
        if track:
            # セッションのリセットは 1 回目で済んでいる。ここで再度 reset すると
            # 1 回目の消費が集計から消える。
            _track_usage(model, getattr(response, "usage", None), reset=False)
        max_tokens = API_MAX_TOKENS
        choice = response.choices[0]
        content = (choice.message.content or "").strip()
        finish = getattr(choice, "finish_reason", None)

    # 出力予算を使い切ったのに黙って空文字を返すのが最悪の失敗の仕方だった
    # (呼び出し側は「レビュー結果なし」と区別できない)。必ず気づける形にする。
    if finish == "length":
        print(
            f"WARNING: 出力が上限({max_tokens} tok)で打ち切られました。"
            f" --max-tokens を上げるか、入力を分割してください。",
            file=sys.stderr,
        )
    if not content:
        # 推論(reasoning)だけで予算を使い切ると本文が空になる。
        # 空を返して正常終了すると「指摘なし」と誤解されるので落とす。
        reasoning = getattr(choice.message, "reasoning_content", None) or ""
        print(
            "ERROR: 本文が空です"
            + (f"(finish_reason={finish})" if finish else "")
            + "。推論だけで出力予算を使い切った可能性があります。"
            + " --max-tokens を上げて再実行してください。",
            file=sys.stderr,
        )
        if reasoning.strip():
            print(
                f"(参考: reasoning は {len(reasoning)} 文字ありました)", file=sys.stderr
            )
        sys.exit(EXIT_GENERIC)

    return content


def _print_session_summary() -> int:
    """--show-session: 現在のセッション累計を表示。TTL 超過時は期限切れ扱い."""
    if not SESSION_FILE.exists():
        print("[DS Usage] セッションファイルなし（未使用または期限切れ）")
        return 0
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"ERROR: セッションファイル読み込み失敗: {exc}", file=sys.stderr)
        return 1

    if not isinstance(data, dict):
        print("[DS Usage] セッションファイル形式不正のため期限切れ扱い")
        return 0

    now = time.time()
    last_at = _coerce_number(
        data.get("last_at", data.get("started_at", 0)), 0.0, as_int=False
    )
    if now - last_at > SESSION_TTL_SEC:
        elapsed_h = (now - last_at) / 3600
        print(
            f"[DS Usage] セッション期限切れ（最終呼び出しから {elapsed_h:.1f} 時間経過 / TTL {SESSION_TTL_SEC // 3600}h）"
        )
        return 0

    started_at = _coerce_number(data.get("started_at", 0), 0.0, as_int=False)
    started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started_at))
    last = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_at))
    rate = _resolve_usd_to_jpy()
    cost_usd = _coerce_number(data.get("cost", 0.0), 0.0, as_int=False)
    cost_jpy = cost_usd * rate
    calls = int(_coerce_number(data.get("calls", 0), 0, as_int=True))
    in_miss = int(_coerce_number(data.get("in_miss", 0), 0, as_int=True))
    in_hit = int(_coerce_number(data.get("in_hit", 0), 0, as_int=True))
    out_tokens = int(_coerce_number(data.get("out", 0), 0, as_int=True))
    print(
        f"[DS Usage] 累計: {calls} calls / "
        f"入力 {in_miss + in_hit:,} (miss {in_miss:,} / hit {in_hit:,}) "
        f"/ 出力 {out_tokens:,} tok "
        f"(¥{cost_jpy:.2f} / ${cost_usd:.4f}) "
        f"[since {started} / last {last} / 1USD=¥{rate:.2f}]"
    )
    return 0


def main() -> None:
    """エントリポイント: ファイルパスまたは stdin からプロンプトを受け取る."""
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="DeepSeek API client (coder/advisor)")
    parser.add_argument(
        "input",
        nargs="?",
        help="プロンプト文字列、またはプロンプトを書いたファイルのパス. 省略時は stdin から読む.",
    )
    parser.add_argument(
        "--role",
        choices=list(ROLE_PROMPTS),
        default="coder",
        help="動作モード: coder=コード生成（既定）, advisor=設計相談・別案出し",
    )
    parser.add_argument("--model", default=None, help="モデル名を明示指定する場合")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument(
        "--no-usage",
        action="store_true",
        help="usage 集計と stderr 表示を抑止する",
    )
    parser.add_argument(
        "--reset-session",
        action="store_true",
        help="累計をリセットしてから今回分を記録する",
    )
    parser.add_argument(
        "--show-session",
        action="store_true",
        help="現在のセッション累計を表示して終了する（API 呼び出しなし）",
    )
    args = parser.parse_args()

    if args.show_session:
        sys.exit(_print_session_summary())

    if args.input:
        path = Path(args.input)
        prompt = path.read_text(encoding="utf-8") if path.exists() else args.input
    else:
        prompt = sys.stdin.read()

    if not prompt.strip():
        print("ERROR: プロンプトが空です", file=sys.stderr)
        sys.exit(EXIT_INVALID_INPUT)

    result = call_deepseek(
        prompt,
        role=args.role,
        model=args.model,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        track=not args.no_usage,
        reset_session=args.reset_session,
    )
    print(result)


if __name__ == "__main__":
    main()
