"""Gemini API クライアント — 設計相談（advisor）/ レビュー（reviewer）対応.

廃止された Gemini CLI（@google/gemini-cli 無料tier）の代替。
Google 公式の OpenAI 互換エンドポイントを叩くため、追加 SDK は不要
（既存の openai ライブラリを流用。deepseek_coder.py / qwen_advisor.py と同作法）。

cgd Lv2 の「2 枚目のレビュアー」、Lv4-5 の別案出し（advisor）に使う。
呼び出しごとに usage を取得し、トークン数と概算料金を stderr に出力。
セッション累計を JSON に保存し、4 時間以内の呼び出しは累計加算、それ以降は
新規セッションとして自動リセット（qwen_advisor.py と同じパターン）。

主な仕様:
- セッションファイル: .gemini_usage_session.json（atomic write 保護）
- 為替: 既定 1USD=150JPY。環境変数 GEMINI_USD_TO_JPY で上書き可能
- 既定モデル: gemini-flash-latest（環境変数 GEMINI_MODEL / --model で上書き可）
- エンドポイント: 環境変数 GEMINI_BASE_URL で上書き可
- 未知モデルは gemini-flash-latest 料金にフォールバック（stderr に警告）
- AI Studio API キーは無料枠があり、通常運用ではほぼ ¥0（レート制限に注意）
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
    "advisor": (
        "あなたは熟練のソフトウェア設計アドバイザーです。"
        "ユーザーから提示される設計案・実装方針・既存コードに対して、"
        "別視点・別アプローチ・見落とし・代替案を日本語で簡潔に提示します。"
        "出力は必ず以下の構造に従ってください:\n"
        "1. **別案** — 提示された案とは異なるアプローチ（1〜3個、各2〜4行）\n"
        "2. **見落としの可能性** — 元案で考慮が薄い点（箇条書き、最大5項目）\n"
        "3. **採否コメント** — 各別案の長所/短所を1行ずつ\n"
        "コード断片を出すときも要点に絞り、長大な実装を貼らないこと。"
        "あなたの強みは広い文脈把握と横断的な視野です。"
        "Codex が見るであろう厳密な実装細部より、全体設計・代替アーキテクチャ・"
        "他システムとの整合・運用観点での別案を優先してください。"
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
        "あなたの強みは広い文脈把握と長文読解です。"
        "Codex が見るであろうコードレベルの細部より、"
        "全体設計の一貫性・仕様との整合・見落とされがちな影響範囲・"
        "ドキュメントや既存資料との齟齬を優先的に評価してください。"
        "コード断片を出すときも要点に絞り、長大な実装を貼らないこと。"
    ),
}

# 料金（USD / 1M tokens）: (input, output)
# Google AI Studio (Gemini API) レート（2026 時点・best effort）。変更時はここだけ書き換える。
# ※ AI Studio の無料枠で運用する限り実費はほぼ発生しない（レート制限あり）。
PRICING: dict[str, tuple[float, float]] = {
    "gemini-flash-latest": (0.30, 2.50),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-pro-latest": (1.25, 10.00),
    "gemini-3.5-flash": (0.30, 2.50),
}

DEFAULT_USD_TO_JPY: float = 150.0
DEFAULT_MODEL: str = "gemini-2.5-flash"
DEFAULT_BASE_URL: str = "https://generativelanguage.googleapis.com/v1beta/openai/"


def _resolve_usd_to_jpy() -> float:
    """環境変数 GEMINI_USD_TO_JPY があればそれを採用、なければ既定値."""
    raw = os.environ.get("GEMINI_USD_TO_JPY")
    if not raw:
        return DEFAULT_USD_TO_JPY
    try:
        value = float(raw)
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    print(
        f"[Gemini Usage] WARN: GEMINI_USD_TO_JPY='{raw}' が不正のため既定値 {DEFAULT_USD_TO_JPY} を使用",
        file=sys.stderr,
    )
    return DEFAULT_USD_TO_JPY


SESSION_FILE: Path = Path(__file__).parent / ".gemini_usage_session.json"
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
    """OpenAI 互換からキャッシュヒットトークン数を抽出する（無ければ 0）."""
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

    Gemini はキャッシュヒット分も簡易的に input 同率で概算（厳密な context cache
    料金は別だが、AI Studio 無料枠運用が前提のため近似で十分）.

    Returns:
        (cost_usd, is_fallback) — is_fallback は未知モデルで既定料金を使った場合 True.
    """
    rates = PRICING.get(model)
    is_fallback = rates is None
    if rates is None:
        rates = PRICING[DEFAULT_MODEL]
    in_rate, out_rate = rates
    cost = (
        (cache_miss + cache_hit) * in_rate / 1_000_000
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
    """セッション累計を読み込む。TTL 超過 / reset=True なら新規セッション."""
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
    """累計を atomic write で保存する."""
    try:
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=SESSION_FILE.parent,
            prefix=".gemini_usage_",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            json.dump(state, tmp, ensure_ascii=False, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, SESSION_FILE)
    except OSError as exc:
        print(f"[Gemini Usage] WARN: セッションファイル保存失敗: {exc}", file=sys.stderr)


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
        f"[Gemini Usage] {label}: 入力 {cache_miss:,} (miss) + {cache_hit:,} (hit) "
        f"/ 出力 {out_tokens:,} tok "
        f"(¥{yen:.2f} / ${cost_usd:.4f}){extra}"
    )


def _track_usage(model: str, usage_obj: Any, reset: bool = False) -> None:
    """API レスポンスの usage を集計し、stderr に今回 / 累計を表示する."""
    if usage_obj is None:
        print("[Gemini Usage] WARN: usage 情報がレスポンスに含まれていません", file=sys.stderr)
        return

    prompt_tokens = _to_int(usage_obj, "prompt_tokens")
    completion_tokens = _to_int(usage_obj, "completion_tokens")
    cache_hit = _extract_cache_hit(usage_obj)
    cache_miss = max(prompt_tokens - cache_hit, 0)

    cost_now, is_fallback = _calc_cost(model, cache_miss, cache_hit, completion_tokens)
    if is_fallback:
        print(
            f"[Gemini Usage] WARN: 未登録モデル '{model}' のため {DEFAULT_MODEL} 料金で概算しています",
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


def call_gemini(
    prompt: str,
    role: str = "reviewer",
    model: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    track: bool = True,
    reset_session: bool = False,
) -> str:
    """Gemini API（OpenAI 互換エンドポイント）を呼び出す.

    Args:
        prompt: ユーザープロンプト.
        role: 'reviewer'（レビュー・既定）または 'advisor'（設計相談・別案出し）.
        model: モデル名. 省略時は GEMINI_MODEL 環境変数 → DEFAULT_MODEL.
        max_tokens: 最大出力トークン数.
        temperature: サンプリング温度.
        track: True なら usage を集計して stderr に出力する.
        reset_session: True なら累計をリセットしてから今回分を記録する.

    Returns:
        Gemini からの応答テキスト.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY が設定されていません", file=sys.stderr)
        sys.exit(1)

    if role not in ROLE_PROMPTS:
        print(f"ERROR: 未知の role '{role}'. 使えるのは {list(ROLE_PROMPTS)}", file=sys.stderr)
        sys.exit(1)

    if model is None:
        model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)

    base_url = os.environ.get("GEMINI_BASE_URL", DEFAULT_BASE_URL)
    client = OpenAI(api_key=api_key, base_url=base_url)

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
        print("[Gemini Usage] セッションファイルなし（未使用または期限切れ）")
        return 0
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"ERROR: セッションファイル読み込み失敗: {exc}", file=sys.stderr)
        return 1

    if not isinstance(data, dict):
        print("[Gemini Usage] セッションファイル形式不正のため期限切れ扱い")
        return 0

    now = time.time()
    last_at = _coerce_number(
        data.get("last_at", data.get("started_at", 0)), 0.0, as_int=False
    )
    if now - last_at > SESSION_TTL_SEC:
        elapsed_h = (now - last_at) / 3600
        print(
            f"[Gemini Usage] セッション期限切れ（最終呼び出しから {elapsed_h:.1f} 時間経過 / TTL {SESSION_TTL_SEC // 3600}h）"
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
        f"[Gemini Usage] 累計: {calls} calls / "
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

    parser = argparse.ArgumentParser(description="Gemini API client (reviewer/advisor)")
    parser.add_argument(
        "input",
        nargs="?",
        help="プロンプト文字列、またはプロンプトを書いたファイルのパス. 省略時は stdin から読む.",
    )
    parser.add_argument(
        "--role",
        choices=list(ROLE_PROMPTS),
        default="reviewer",
        help="動作モード: reviewer=レビュー（既定）, advisor=設計相談・別案出し",
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

    result = call_gemini(
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
