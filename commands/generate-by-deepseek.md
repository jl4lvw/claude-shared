DeepSeek Coder でコードを生成し、Claude がレビューする skill。

## 実行手順

1. ユーザーの仕様・要件を確認する。不明点があれば質問して明確化する。
2. 仕様が固まったら、プロンプトを一時ファイルに書き出して DeepSeek API を呼び出す:
   - Bash で `C:/Users/jl4lv/tmp_deepseek_prompt.txt` にプロンプトを書き出す
   - `python "C:/Users/jl4lv/OneDrive/デスクトップ/0.フジ/900.ClaudeCode/.claude/tools/deepseek_coder.py" "C:/Users/jl4lv/tmp_deepseek_prompt.txt"` を実行する
   - 呼び出し直後に **stderr に `[DS Usage] 今回: ... / 累計: ...` が出力される**（今回トークン数・累計・概算料金 ¥/$）。Claude は実行結果に含まれるこの 2 行を **必ずユーザー向け応答に転記** する（料金可視化のため）
   - セッション累計は `.claude/tools/.deepseek_usage_session.json` に atomic write で保存され、最終呼び出しから 4 時間で自動リセット。手動リセットは `--reset-session`、累計確認だけは `--show-session`
   - 円換算レートの既定は 1USD=150JPY。環境変数 `DEEPSEEK_USD_TO_JPY` で上書き可能（例: `DEEPSEEK_USD_TO_JPY=160`）
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
