// cgd_lv6_review.js — Lv6 (既定 Codex+DS+Qwen 3者並列レビュー、Gemini はオプトイン) の review phase を Workflow 化
//
// 目的: Codex high の巨大 raw 出力 (160KB+) を subagent context に閉じ込め、
//       主 context には構造化 findings + 統合表 (数KB) だけ返す。
//
// 2026-07: Gemini は AI Studio 無料枠のレート制限でエラーが頻発したため既定から除外。
//          args.include_gemini === true のときだけ4者目として追加参加させる。
//          あわせて、旧廃止済み `gemini` CLI を直接叩いていたバグ（呼べば必ず失敗する）を
//          `gemini_advisor.py`（OpenAI互換API）呼び出しに修正した。
//
// 主 context 側の責務 (この workflow の外):
//   - Step 1 レベル選択 (AskUserQuestion)
//   - レビュー入力ファイル (差分+背景) の準備 → input_path で渡す
//   - この workflow 完了後、戻り値の table_md を描画
//   - 実装許可 (AskUserQuestion) → 実装は主 context で実施 (本 workflow は review のみ)
//
// 起動例（既定・Gemini なし）:
//   Workflow({ scriptPath: ".../cgd_lv6_review.js",
//              args: { input_path: "C:/tmp-ai/review_input.txt", codex_reasoning: "medium", label: "pickorder-scan" } })
// 起動例（Gemini オプトイン）:
//   Workflow({ scriptPath: ".../cgd_lv6_review.js",
//              args: { input_path: "C:/tmp-ai/review_input.txt", codex_reasoning: "medium", label: "pickorder-scan", include_gemini: true } })

export const meta = {
  name: 'cgd-lv6-review',
  description: 'Lv6 既定Codex+DS+Qwen 3者並列レビュー（Gemini はinclude_geminiでオプトイン・review phaseのみ）',
  phases: [
    { title: 'Review', detail: '既定3者並列レビュー (Codex/DeepSeek/Qwen、Gemini はオプトイン)' },
    { title: 'Merge', detail: '収束判定 + 統合表生成' },
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
// PreToolUse ゲート(cgd_wf_gate.py)のバイパス nonce。主 context が
//   python cgd_wf_gate.py nonce
// で取得して args.wf_nonce に渡す。合致しない値では codex 起動が deny される。
const wfNonce = _args.wf_nonce || ''
const includeGemini = _args.include_gemini === true

// ドライラン: args パース + パス解決の確認用 (agent を呼ばず即 return)。dry_run=true 時のみ。
// 起動例: Workflow({scriptPath, args:{input_path:"...", label:"...", dry_run:true}}) → 課金0
if (_args.dry_run === true) {
  log('[dry-run] inputPath=' + inputPath + ' / reasoning=' + reasoning + ' / label=' + label + ' / includeGemini=' + includeGemini + ' / wfNonce=' + (wfNonce ? 'あり' : '**未指定**'))
  return { dry_run: true, resolved_input_path: inputPath, resolved_reasoning: reasoning, resolved_label: label, resolved_include_gemini: includeGemini }
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
// 既定は Codex+DeepSeek+Qwen の3者。Gemini は includeGemini のときだけ追加（Codexの次に挿入し、
// SKILL.md の統合表の列順「Codex | (Gemini) | DS | Qwen」と揃える）。
const reviewers = [
  {
    name: 'codex',
    cmd: `mkdir -p /c/tmp-ai && cd /c/tmp-ai && CGD_WF_RUN=${wfNonce} codex exec -c model_reasoning_effort="${reasoning}" --sandbox read-only --skip-git-repo-check "まず ${inputPath} の全文を読み、記載の差分・対象・評価観点に従ってコードレビュー。関連関数の抜粋は入力に同梱済み。追加で開くのは最大5ファイルまでとし、超えるなら読まずに『情報不足: <欲しいファイル>』と書いて終えること。日本語で回答。" < /dev/null`,
    timeout: reasoning === 'high' ? 600000 : 300000,
    usage: false,
    authSignals: 'Not logged in / 401 / unauthorized',
  },
  ...(includeGemini ? [{
    name: 'gemini',
    cmd: `python "C:/ClaudeCode/.claude/tools/gemini_advisor.py" --role reviewer "${inputPath}"`,
    timeout: 180000,
    usage: true,
    authSignals: 'AuthenticationError / 401 / invalid api key / GEMINI_API_KEY が設定されていません',
  }] : []),
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
4. ${r.usage ? 'stderr に出る [DS Usage] / [Qwen Usage] / [Gemini Usage] の「今回:」行を usage_line にそのまま転記する。' : 'usage_line は空文字 ("") にする (サブスク認証で料金可視化なし)。'}
5. 認証エラー (${r.authSignals}) を検出したら auth_error=true にして findings は空配列にする。それ以外は auth_error=false。
6. reviewer フィールドに "${r.name}" を入れる。

[重要]
- あなたの最終出力 (schema JSON) だけが親に返る。生のレビュー文を return に含めない (構造化 findings に圧縮すること)。
- コマンドがタイムアウト/失敗した場合も auth_error 判定を試み、判断不能なら auth_error=false で findings に 🟠「${r.name} 実行失敗」を1件入れる。

JSON で返す。`,
    { label: `review:${r.name}`, phase: 'Review', schema: FINDING_SCHEMA }
  )
))

// ---- 認証エラー / 欠員チェック (Lv6 はその回の参加者全員成功が必須) ----
const ok = reviews.filter(Boolean)
const authFailed = ok.filter((r) => r.auth_error).map((r) => r.reviewer)
if (authFailed.length > 0) {
  log(`認証エラー検出: ${authFailed.join(', ')} → Lv6 中断 (参加者${reviewers.length}者揃わないと意味がない)`)
  return {
    halt: 'auth_error',
    failed: authFailed,
    message: `認証エラー: ${authFailed.join(', ')}。復旧後に再実行してください。`,
  }
}
if (ok.length < reviewers.length) {
  log(`レビュアー欠員: ${ok.length}/${reviewers.length} のみ成功 → Lv6 中断`)
  return {
    halt: 'incomplete',
    got: ok.length,
    expected: reviewers.length,
    message: `Lv6 は参加者${reviewers.length}者全員の成功が必要です。`,
  }
}

phase('Merge')

const MERGE_SCHEMA = {
  type: 'object',
  properties: {
    table_md: { type: 'string', description: '統合表 (markdown)' },
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

const reviewerNames = reviewers.map((r) => r.name)
const tableColumns = ['指摘 (🔴/🟠/🟡 + 根拠1行)', ...reviewerNames.map((n) => n[0].toUpperCase() + n.slice(1)), '採用判断']

const merged = await agent(
  `${reviewerNames.length}者のコードレビュー結果を統合してください。

[各レビュアーの findings]
${JSON.stringify(ok.map((r) => ({ reviewer: r.reviewer, findings: r.findings })), null, 2)}

[タスク]
1. 同一の指摘を突き合わせる。location と内容が一致/類似する指摘は同じ行にまとめる。
2. 収束シグナル判定: 2者以上が挙げた指摘は convergent_findings (信頼度高)、1者のみは divergent_findings (false positive の可能性も含め要吟味) に分類。
3. 統合表 table_md を markdown で作成。列は必ずこの順番・この列名にする: ${JSON.stringify(tableColumns)}
   各レビュアー列は ✅ (指摘あり) / ❌ (なし) / 🔄 (部分一致) を記入。
4. next_actions: 実装すべき項目を箇条書き (severity 高い順、ファイル・修正方針)。
5. summary: 全体の総評を1-3行。

JSON で返す。`,
  { label: 'merge', phase: 'Merge', schema: MERGE_SCHEMA }
)

return {
  level: 6,
  label,
  include_gemini: includeGemini,
  participants: reviewerNames,
  table_md: merged.table_md,
  convergent_findings: merged.convergent_findings,
  divergent_findings: merged.divergent_findings,
  next_actions: merged.next_actions,
  summary: merged.summary,
  usage: ok.map((r) => ({ reviewer: r.reviewer, usage_line: r.usage_line || '' })),
  raw_log_paths: ok.map((r) => ({ reviewer: r.reviewer, path: r.raw_log_path || '' })),
}
