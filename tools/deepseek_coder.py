"""DeepSeek API クライアント — コード生成 / 設計相談（advisor）の2モード対応.

呼び出しごとに API レスポンスの usage を取得し、トークン数と概算料金を
stderr に出力する。同時にセッション累計を JSON に保存し、4 時間以内の
呼び出しは累計加算、それ以降は新規セッションとして自動リセットする。

主な仕様:
- セッションファイル: .deepseek_usage_session.json（atomic write 保護）
- 為替: 既定 1USD=150JPY。環境変数 DEEPSEEK_USD_TO_JPY で上書き可能
- 未知モデルは deepseek-chat 料金にフォールバック（stderr に警告）
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

ROLE_PROMPTS: dict[str, str] = {
    "coder": (
        "You are an expert Python programmer. "
        "Write clean, production-quality code with type hints and proper error handling. "
        "Use Japanese for comments and docstrings. "
        "Output only the code unless explicitly asked for explanations."
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
}

# 料金（USD / 1M tokens）: (input_cache_miss, input_cache_hit, output)
# DeepSeek 公式レート（2026/05 時点）。変更時はここだけ書き換える。
PRICING: dict[str, tuple[float, float, float]] = {
    "deepseek-chat": (0.27, 0.07, 1.10),
    "deepseek-coder": (0.27, 0.07, 1.10),
    "deepseek-reasoner": (0.55, 0.14, 2.19),
}

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
        rates = PRICING["deepseek-chat"]
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
            f"[DS Usage] WARN: 未登録モデル '{model}' のため deepseek-chat 料金で概算しています",
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
    max_tokens: int = 4096,
    temperature: float = 0.0,
    track: bool = True,
    reset_session: bool = False,
) -> str:
    """DeepSeek API を呼び出す.

    Args:
        prompt: ユーザープロンプト.
        role: 'coder'（コード生成・既存挙動）または 'advisor'（設計相談・別案出し）.
        model: モデル名. 省略時は role に応じて自動選択（coder→deepseek-coder, advisor→deepseek-chat）.
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
        sys.exit(1)

    if role not in ROLE_PROMPTS:
        print(f"ERROR: 未知の role '{role}'. 使えるのは {list(ROLE_PROMPTS)}", file=sys.stderr)
        sys.exit(1)

    if model is None:
        model = "deepseek-coder" if role == "coder" else "deepseek-chat"

    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": ROLE_PROMPTS[role]},
            {"role": "user", "content": prompt},
        ],
        max_tokens=max_tokens,
        temperature=temperature,
    )

    if track:
        _track_usage(model, getattr(response, "usage", None), reset=reset_session)

    return response.choices[0].message.content


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
    parser.add_argument("--max-tokens", type=int, default=4096)
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
        sys.exit(1)

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
