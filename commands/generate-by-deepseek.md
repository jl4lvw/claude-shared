DeepSeek Coder でコードを生成し、Claude がレビューする skill。

## 実行手順

1. ユーザーの仕様・要件を確認する。不明点があれば質問して明確化する。
2. 仕様が固まったら、プロンプトを一時ファイルに書き出して DeepSeek API を呼び出す:
   - Bash で `C:/Users/jl4lv/tmp_deepseek_prompt.txt` にプロンプトを書き出す
   - `python "C:/Users/jl4lv/OneDrive/デスクトップ/0.フジ/900.ClaudeCode/.claude/tools/deepseek_coder.py" "C:/Users/jl4lv/tmp_deepseek_prompt.txt"` を実行する
3. 生成されたコードを Claude が第三者視点でレビューする:
   - バグ・論理ミスがないか
   - 型ヒント・エラーハンドリングが適切か
   - セキュリティ上の問題がないか
   - CLAUDE.md の検証チェックリストを満たしているか
4. 問題があれば修正案を提示し、ユーザーに確認する
5. ユーザーの承認を得てファイルに書き込む
6. CLAUDE.md のコーディング後の必須検証を実施する

## 注意事項

- 環境変数 `DEEPSEEK_API_KEY` が必要
- コーディング後の必須検証（CLAUDE.md）は省略しない
- レビューで重大な問題があれば、修正してから書き込む

回答はすべて日本語で行うこと。
