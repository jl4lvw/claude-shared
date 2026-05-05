"""DeepSeek API クライアント — コード生成 / 設計相談（advisor）の2モード対応."""

import argparse
import os
import sys
from pathlib import Path

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


def call_deepseek(
    prompt: str,
    role: str = "coder",
    model: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.0,
) -> str:
    """DeepSeek API を呼び出す.

    Args:
        prompt: ユーザープロンプト.
        role: 'coder'（コード生成・既存挙動）または 'advisor'（設計相談・別案出し）.
        model: モデル名. 省略時は role に応じて自動選択（coder→deepseek-coder, advisor→deepseek-chat）.
        max_tokens: 最大出力トークン数.
        temperature: サンプリング温度.

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

    return response.choices[0].message.content


def main() -> None:
    """エントリポイント: ファイルパスまたは stdin からプロンプトを受け取る."""
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

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
    args = parser.parse_args()

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
    )
    print(result)


if __name__ == "__main__":
    main()
