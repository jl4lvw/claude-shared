"""DeepSeek Coder API クライアント — コード生成用."""

import os
import sys
from pathlib import Path

from openai import OpenAI


def call_deepseek(
    prompt: str,
    model: str = "deepseek-coder",
    max_tokens: int = 4096,
    temperature: float = 0.0,
) -> str:
    """DeepSeek API を呼び出してコードを生成する."""
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("ERROR: DEEPSEEK_API_KEY が設定されていません", file=sys.stderr)
        sys.exit(1)

    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an expert Python programmer. "
                    "Write clean, production-quality code with type hints and proper error handling. "
                    "Use Japanese for comments and docstrings. "
                    "Output only the code unless explicitly asked for explanations."
                ),
            },
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

    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
        if path.exists():
            prompt = path.read_text(encoding="utf-8")
        else:
            prompt = sys.argv[1]
    else:
        prompt = sys.stdin.read()

    if not prompt.strip():
        print("ERROR: プロンプトが空です", file=sys.stderr)
        sys.exit(1)

    result = call_deepseek(prompt)
    print(result)


if __name__ == "__main__":
    main()
