#!/bin/bash
# codex execのハング防止ラッパー(2026-07-11、Lv2相談で設計)。
#
# 事故の経緯: `codex exec "<プロンプト>"` を `< /dev/null` なしでバックグラウンド実行したところ、
# stdinが追加入力待ち("Reading additional input from stdin...")のままハングし、16時間以上
# 完了しなかった(プロセス自体は消えていたが出力ファイルは更新されず、完了通知も来なかった)。
# 恒久対策として、`< /dev/null` と外側timeoutを必ず両方かけるラッパーに一本化する。
#
# 使い方:
#   bash codex_exec_safe.sh <出力ファイル> <reasoning_effort:low|medium|high> <プロンプトファイル> [timeout秒(既定600)]
#
# 例:
#   bash "C:/ClaudeCode/.claude/tools/codex_exec_safe.sh" \
#     "C:/tmp-ai/lv7_out_foo.txt" medium "C:/tmp-ai/lv7_input_foo.txt" 600

set -u

OUT_FILE="${1:?出力ファイルパスを指定してください}"
EFFORT="${2:?reasoning_effort(low|medium|high)を指定してください}"
PROMPT_FILE="${3:?プロンプトファイルパスを指定してください}"
TIMEOUT_SEC="${4:-600}"

if [[ ! -f "$PROMPT_FILE" ]]; then
  echo "エラー: プロンプトファイルが見つかりません: $PROMPT_FILE" > "$OUT_FILE"
  exit 1
fi

# --foreground: timeoutが子プロセスのプロセスグループを直接killできるようにする
# (バックグラウンドジョブとして起動されてもtimeout自体のシグナル配送を妨げない)
timeout --foreground "${TIMEOUT_SEC}s" \
  codex exec -c model_reasoning_effort="$EFFORT" --skip-git-repo-check \
  "$(cat "$PROMPT_FILE")" \
  < /dev/null \
  > "$OUT_FILE" \
  2>&1
STATUS=$?

if [[ $STATUS -eq 124 ]]; then
  echo "" >> "$OUT_FILE"
  echo "[codex_exec_safe] TIMEOUT after ${TIMEOUT_SEC}s - プロセスを強制終了しました" >> "$OUT_FILE"
fi

exit $STATUS
