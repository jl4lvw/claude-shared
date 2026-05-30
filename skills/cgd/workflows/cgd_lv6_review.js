// cgd_lv6_review.js — Lv6 (C+G+DS+Qwen 4者並列レビュー) の review phase を Workflow 化
//
// 目的: Codex high/medium の巨大 raw 出力 (160KB+) を subagent context に閉じ込め、
//       主 context には構造化 findings + 統合表 (数KB) だけ返す。
//
// 主 context 側の責務 (この workflow の外):
//   - Step 1 レベル選択 (AskUserQuestion)
//   - レビュー入力ファイル (差分+背景) の準備 → input_path で渡す
//   - この workflow 完了後、戻り値の table_md を描画
//   - 実装許可 (AskUserQuestion) → 実装は主 context で実施 (本 workflow は review のみ)
//
// 起動例:
//   Workflow({ scriptPath: ".../cgd_lv6_review.js",
//              args: { input_path: "C:/tmp-ai/review_input.txt", codex_reasoning: "medium", label: "pickorder-scan" } })

export const meta = {
  name: 'cgd-lv6-review',
  description: 'Lv6 C+G+DS+Qwen 4者並列レビュー (review phase のみ・主context汚染回避)',
  phases: [
    { title: 'Review', detail: '4者並列レビュー (Codex/Gemini/DeepSeek/Qwen)' },
    { title: 'Merge', detail: '収束判定 + 6列統合表生成' },
  ],
}

// ---- args (主 context から渡す) ----
// Workflow ツールは args を JSON 文字列で渡す (実証済: typeof args === 'string')。
// そのままだと args.input_path が undefined になるため JSON.parse でオブジェクト化する。
let _args = args
if (typeof _args === 'string') { try { _args = JSON.parse(_args) } catch (_) { _args = {} } }
if (!_args || typeof _args !== 'object') _args = {}

const inputPath = _args.input_path || 'C:/tmp-ai/review_input.txt'
const reasoning = _args.codex_reasoning || 'medium'
const label = _args.label || 'target'

// ドライラン: args パース + パス解決の確認用 (agent を呼ばず即 return)。dry_run=true 時のみ。
// 起動例: Workflow({scriptPath, args:{input_path:"...", label:"...", dry_run:true}}) → 課金0
if (_args.dry_run === true) {
  log('[dry-run] inputPath=' + inputPath + ' / reasoning=' + reasoning + ' / label=' + label)
  return { dry_run: true, resolved_input_path: inputPath, resolved_reasoning: reasoning, resolved_label: label }
}

// ---- 各レビュアーの構造化出力スキーマ ----
const FINDING_SCHEMA = {
  type: 'object',
  properties: {
    reviewer: { type: 'string' },
    auth_error: { type: 'boolean', description: '認証エラーを検出したら true' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          severity: { type: 'string', enum: ['🔴', '🟠', '🟡'] },
          title: { type: 'string' },
          location: { type: 'string', description: 'file:line 形式が望ましい' },
          rationale: { type: 'string', description: '根拠1行' },
          recommended_fix: { type: 'string' },
        },
        required: ['severity', 'title', 'rationale'],
      },
    },
    usage_line: { type: 'string', description: 'DS/Qwen の stderr [Usage] 行をそのまま転記。なければ空文字' },
    raw_log_path: { type: 'string', description: '生出力を保存したファイルパス' },
  },
  required: ['reviewer', 'auth_error', 'findings'],
}

// ---- レビュアー定義 (Bash コマンドと timeout) ----
const reviewers = [
  {
    name: 'codex',
    cmd: `mkdir -p /c/tmp-ai && cd /c/tmp-ai && codex exec -c model_reasoning_effort="${reasoning}" --sandbox read-only --skip-git-repo-check "まず ${inputPath} の全文を読み、記載の差分・対象・評価観点に従ってコードレビュー。必要なら対象実ファイルも読んでよい。日本語で回答。" < /dev/null`,
    timeout: reasoning === 'high' ? 600000 : 300000,
    usage: false,
    authSignals: 'Not logged in / 401 / unauthorized',
  },
  {
    name: 'gemini',
    cmd: `mkdir -p /c/tmp-ai && cd /c/tmp-ai && gemini --skip-trust -p "まず ${inputPath} の全文を読み、記載の差分・対象・評価観点に従ってコードレビュー。日本語で回答。" < /dev/null`,
    timeout: 180000,
    usage: false,
    authSignals: '/auth 要求 / 401 / permission denied',
  },
  {
    name: 'deepseek',
    cmd: `python "C:/ClaudeCode/.claude/tools/deepseek_coder.py" --role reviewer "${inputPath}"`,
    timeout: 180000,
    usage: true,
    authSignals: 'AuthenticationError / 401 / invalid api key / DEEPSEEK_API_KEY が設定されていません',
  },
  {
    name: 'qwen',
    cmd: `python "C:/ClaudeCode/.claude/tools/qwen_advisor.py" --role reviewer "${inputPath}"`,
    timeout: 180000,
    usage: true,
    authSignals: 'AuthenticationError / 401 / InvalidApiKey / DASHSCOPE_API_KEY が設定されていません',
  },
]

phase('Review')

const reviews = await parallel(reviewers.map((r) => () =>
  agent(
    `あなたは外部レビュアー「${r.name}」を実行し、その出力を構造化レビュー結果に変換する担当です。

[手順]
1. Bash tool を timeout=${r.timeout} (ミリ秒) で使って次のコマンドを実行する:
${r.cmd}

2. 標準出力の全文を Write tool で C:/tmp-ai/cgd_raw_${r.name}_${label}.md に保存する (後で人が生ログを確認できるように)。
3. 出力を読み、指摘を findings 配列に構造化する:
   - severity は 🔴 (重大: セキュリティ/データ破壊/公開API逸脱/明白な論理バグ/integrationバグ) / 🟠 (重要) / 🟡 (注意) のいずれか
   - title: 指摘の要約 (1行)
   - location: file:line 形式が望ましい (分かる範囲で)
   - rationale: 根拠を1行
   - recommended_fix: 推奨修正 (あれば)
4. ${r.usage ? 'stderr に出る [DS Usage] / [Qwen Usage] の「今回:」行を usage_line にそのまま転記する。' : 'usage_line は空文字 ("") にする (サブスク認証で料金可視化なし)。'}
5. 認証エラー (${r.authSignals}) を検出したら auth_error=true にして findings は空配列にする。それ以外は auth_error=false。
6. reviewer フィールドに "${r.name}" を入れる。

[重要]
- あなたの最終出力 (schema JSON) だけが親に返る。生のレビュー文を return に含めない (構造化 findings に圧縮すること)。
- コマンドがタイムアウト/失敗した場合も auth_error 判定を試み、判断不能なら auth_error=false で findings に 🟠「${r.name} 実行失敗」を1件入れる。

JSON で返す。`,
    { label: `review:${r.name}`, phase: 'Review', schema: FINDING_SCHEMA }
  )
))

// ---- 認証エラー / 欠員チェック (Lv6 は4者全員成功が必須) ----
const ok = reviews.filter(Boolean)
const authFailed = ok.filter((r) => r.auth_error).map((r) => r.reviewer)
if (authFailed.length > 0) {
  log(`認証エラー検出: ${authFailed.join(', ')} → Lv6 中断 (4者揃わないと意味がない)`)
  return {
    halt: 'auth_error',
    failed: authFailed,
    message: `認証エラー: ${authFailed.join(', ')}。復旧後に再実行してください。`,
  }
}
if (ok.length < 4) {
  log(`レビュアー欠員: ${ok.length}/4 のみ成功 → Lv6 中断`)
  return {
    halt: 'incomplete',
    got: ok.length,
    message: 'Lv6 は4者全員の成功が必要です。',
  }
}

phase('Merge')

const MERGE_SCHEMA = {
  type: 'object',
  properties: {
    table_md: { type: 'string', description: '6列統合表 (markdown)' },
    convergent_findings: {
      type: 'array',
      description: '2者以上が一致した指摘 (信頼度高)',
      items: {
        type: 'object',
        properties: {
          severity: { type: 'string' },
          title: { type: 'string' },
          agreed_by: { type: 'array', items: { type: 'string' } },
          recommended_fix: { type: 'string' },
        },
        required: ['severity', 'title', 'agreed_by'],
      },
    },
    divergent_findings: {
      type: 'array',
      description: '1者のみの指摘 (要吟味)',
      items: {
        type: 'object',
        properties: {
          severity: { type: 'string' },
          title: { type: 'string' },
          source: { type: 'string' },
        },
        required: ['severity', 'title', 'source'],
      },
    },
    next_actions: { type: 'array', items: { type: 'string' } },
    summary: { type: 'string', description: '総評 1-3行' },
  },
  required: ['table_md', 'convergent_findings', 'divergent_findings', 'next_actions', 'summary'],
}

const merged = await agent(
  `4者のコードレビュー結果を統合してください。

[各レビュアーの findings]
${JSON.stringify(ok.map((r) => ({ reviewer: r.reviewer, findings: r.findings })), null, 2)}

[タスク]
1. 同一の指摘を突き合わせる。location と内容が一致/類似する指摘は同じ行にまとめる。
2. 収束シグナル判定: 2者以上が挙げた指摘は convergent_findings (信頼度高)、1者のみは divergent_findings (false positive の可能性も含め要吟味) に分類。
3. 6列統合表 table_md を markdown で作成:
   | 指摘 (🔴/🟠/🟡 + 根拠1行) | Codex | Gemini | DS | Qwen | 採用判断 |
   各 AI 列は ✅ (指摘あり) / ❌ (なし) / 🔄 (部分一致) を記入。
4. next_actions: 実装すべき項目を箇条書き (severity 高い順、ファイル・修正方針)。
5. summary: 全体の総評を1-3行。

JSON で返す。`,
  { label: 'merge', phase: 'Merge', schema: MERGE_SCHEMA }
)

return {
  level: 6,
  label,
  table_md: merged.table_md,
  convergent_findings: merged.convergent_findings,
  divergent_findings: merged.divergent_findings,
  next_actions: merged.next_actions,
  summary: merged.summary,
  usage: ok.map((r) => ({ reviewer: r.reviewer, usage_line: r.usage_line || '' })),
  raw_log_paths: ok.map((r) => ({ reviewer: r.reviewer, path: r.raw_log_path || '' })),
}
