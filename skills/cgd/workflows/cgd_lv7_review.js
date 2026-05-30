// cgd_lv7_review.js — Lv7 (Codex 多重 med+high + G + DS + Qwen 5者並列) の review phase を Workflow 化
//
// Lv6 (cgd_lv6_review.js) との差分:
//   - Codex を medium + high の 2 並列に多重化 (収束=最強シグナル / high単独=深掘り発見)
//   - DS/Qwen には「関連関数抜粋 + 差分」(aux_input_path) を渡す (表層指摘を減らす)
//   - merge で codex_med ∩ codex_high の一致判定を自動化
//
// 主 context 側の事前準備 (この workflow の外):
//   - Lv7 限定: git diff + grep + Read で関連関数を抽出 → aux 入力ファイルに同梱
//   - Codex/Gemini 用入力 (input_path) と DS/Qwen 用入力 (aux_input_path) を準備
//
// 起動例:
//   Workflow({ scriptPath: ".../cgd_lv7_review.js",
//              args: { input_path: "C:/tmp-ai/lv7_codex_input.txt",
//                      aux_input_path: "C:/tmp-ai/lv7_aux_input.txt", label: "pickorder-scan" } })

export const meta = {
  name: 'cgd-lv7-review',
  description: 'Lv7 Codex多重(med+high)+G+DS+Qwen 5者並列レビュー (review phase のみ・Codex集中)',
  phases: [
    { title: 'Review', detail: '5者並列 (Codex med + Codex high + Gemini + DS + Qwen)' },
    { title: 'Merge', detail: 'Codex多重の収束/乖離判定 + 7列統合表' },
  ],
}

// ---- args ----
// Workflow ツールは args を JSON 文字列で渡す (実証済: typeof args === 'string')。
// そのままだと args.input_path が undefined になるため JSON.parse でオブジェクト化する。
let _args = args
if (typeof _args === 'string') { try { _args = JSON.parse(_args) } catch (_) { _args = {} } }
if (!_args || typeof _args !== 'object') _args = {}

const codexInput = _args.input_path || 'C:/tmp-ai/lv7_codex_input.txt'
const auxInput = _args.aux_input_path || 'C:/tmp-ai/lv7_aux_input.txt'
const label = _args.label || 'target'

// ドライラン: args パース + パス解決の確認用 (agent を呼ばず即 return)。dry_run=true 時のみ。
if (_args.dry_run === true) {
  log('[dry-run] codexInput=' + codexInput + ' / auxInput=' + auxInput + ' / label=' + label)
  return { dry_run: true, resolved_input_path: codexInput, resolved_aux_input_path: auxInput, resolved_label: label }
}

// ---- 構造化出力スキーマ (Lv6 と共通) ----
const FINDING_SCHEMA = {
  type: 'object',
  properties: {
    reviewer: { type: 'string' },
    auth_error: { type: 'boolean' },
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
    usage_line: { type: 'string' },
    raw_log_path: { type: 'string' },
  },
  required: ['reviewer', 'auth_error', 'findings'],
}

// ---- 5レビュアー定義 ----
//   Codex/Gemini は codexInput (ファイルパス渡し・sandbox で関連コードを自分で読める)
//   DS/Qwen は auxInput (関連関数抜粋 + 差分・API なのでファイルアクセス不可)
const reviewers = [
  {
    name: 'codex_med',
    cmd: `mkdir -p /c/tmp-ai && cd /c/tmp-ai && codex exec -c model_reasoning_effort="medium" --sandbox read-only --skip-git-repo-check "まず ${codexInput} の全文を読み、記載の差分・対象・評価観点に従ってコードレビュー。必要なら対象実ファイルも読んでよい。日本語で回答。" < /dev/null`,
    timeout: 300000,
    usage: false,
    authSignals: 'Not logged in / 401 / unauthorized',
    isCodex: true,
  },
  {
    name: 'codex_high',
    cmd: `mkdir -p /c/tmp-ai && cd /c/tmp-ai && codex exec -c model_reasoning_effort="high" --sandbox read-only --skip-git-repo-check "まず ${codexInput} の全文を読み、記載の差分・対象・評価観点に従ってコードレビュー。必要なら対象実ファイルも読んでよい。日本語で回答。" < /dev/null`,
    timeout: 600000,
    usage: false,
    authSignals: 'Not logged in / 401 / unauthorized',
    isCodex: true,
  },
  {
    name: 'gemini',
    cmd: `mkdir -p /c/tmp-ai && cd /c/tmp-ai && gemini --skip-trust -p "まず ${codexInput} の全文を読み、記載の差分・対象・評価観点に従ってコードレビュー。日本語で回答。" < /dev/null`,
    timeout: 180000,
    usage: false,
    authSignals: '/auth 要求 / 401 / permission denied',
    isCodex: false,
  },
  {
    name: 'deepseek',
    cmd: `python "C:/ClaudeCode/.claude/tools/deepseek_coder.py" --role reviewer "${auxInput}"`,
    timeout: 180000,
    usage: true,
    authSignals: 'AuthenticationError / 401 / invalid api key / DEEPSEEK_API_KEY が設定されていません',
    isCodex: false,
  },
  {
    name: 'qwen',
    cmd: `python "C:/ClaudeCode/.claude/tools/qwen_advisor.py" --role reviewer "${auxInput}"`,
    timeout: 180000,
    usage: true,
    authSignals: 'AuthenticationError / 401 / InvalidApiKey / DASHSCOPE_API_KEY が設定されていません',
    isCodex: false,
  },
]

phase('Review')

const reviews = await parallel(reviewers.map((r) => () =>
  agent(
    `あなたは外部レビュアー「${r.name}」を実行し、その出力を構造化レビュー結果に変換する担当です。

[手順]
1. Bash tool を timeout=${r.timeout} (ミリ秒) で使って次のコマンドを実行する:
${r.cmd}

2. 標準出力の全文を Write tool で C:/tmp-ai/cgd_raw_${r.name}_${label}.md に保存する (人が生ログを検証できるように・これは必須)。
3. 出力を読み、指摘を findings 配列に構造化する:
   - severity: 🔴 (重大: セキュリティ/データ破壊/公開API逸脱/明白な論理バグ/integrationバグ) / 🟠 (重要) / 🟡 (注意)
   - title / location (file:line) / rationale (根拠1行) / recommended_fix
   - 重要: ${r.name} が **実際に挙げた severity をそのまま尊重** する。あなたが勝手に格上げ/格下げしない。
4. ${r.usage ? 'stderr の [DS Usage] / [Qwen Usage] の「今回:」行を usage_line に転記する。' : 'usage_line は空文字 ("")。'}
5. 認証エラー (${r.authSignals}) を検出したら auth_error=true、findings は空配列。それ以外は auth_error=false。
6. reviewer フィールドに "${r.name}" を入れる。

[重要]
- 最終出力 (schema JSON) だけが親に返る。生レビュー文を return に含めない (構造化 findings に圧縮)。
- ${r.isCodex ? 'あなた (Codex) は sandbox read-only で対象ファイルを直接読めるので、必要なら関連関数を確認して integration バグを精査すること。' : 'コマンド失敗/タイムアウト時は auth 判定を試み、不能なら auth_error=false で findings に 🟠「${r.name} 実行失敗」を1件。'}

JSON で返す。`,
    { label: `review:${r.name}`, phase: 'Review', schema: FINDING_SCHEMA }
  )
))

// ---- 認証エラー / 欠員チェック (Lv7 は5者全員 + Codex 両方成功が必須) ----
const ok = reviews.filter(Boolean)
const authFailed = ok.filter((r) => r.auth_error).map((r) => r.reviewer)
if (authFailed.length > 0) {
  log(`認証エラー検出: ${authFailed.join(', ')} → Lv7 中断`)
  return { halt: 'auth_error', failed: authFailed, message: `認証エラー: ${authFailed.join(', ')}。復旧後に再実行。` }
}
const codexOk = ok.filter((r) => r.reviewer === 'codex_med' || r.reviewer === 'codex_high').length
if (codexOk < 2) {
  log(`Codex 多重が片肺 (${codexOk}/2) → Lv7 中断 (Codex med+high 両方が本質)`)
  return { halt: 'codex_incomplete', got: codexOk, message: 'Lv7 は Codex medium と high の両方成功が必須です。' }
}
if (ok.length < 5) {
  log(`レビュアー欠員: ${ok.length}/5 のみ成功 → Lv7 中断`)
  return { halt: 'incomplete', got: ok.length, message: 'Lv7 は5者全員の成功が必要です。' }
}

phase('Merge')

const MERGE_SCHEMA = {
  type: 'object',
  properties: {
    table_md: { type: 'string', description: '7列統合表 (markdown)' },
    convergent_findings: {
      type: 'array',
      description: 'Codex med と high の両方が挙げた指摘 (最強の収束シグナル)',
      items: {
        type: 'object',
        properties: {
          severity: { type: 'string' },
          title: { type: 'string' },
          also_agreed_by: { type: 'array', items: { type: 'string' }, description: '補助(G/DS/Qwen)で同調した者' },
          recommended_fix: { type: 'string' },
        },
        required: ['severity', 'title'],
      },
    },
    codex_divergent_findings: {
      type: 'array',
      description: 'Codex 片方のみ (high単独=深掘り発見 / med単独=過剰反応の可能性)',
      items: {
        type: 'object',
        properties: {
          severity: { type: 'string' },
          title: { type: 'string' },
          source: { type: 'string', enum: ['codex_med', 'codex_high'] },
        },
        required: ['severity', 'title', 'source'],
      },
    },
    aux_only_findings: {
      type: 'array',
      description: '補助(G/DS/Qwen)のみが挙げた指摘 (Codex多重の盲点候補・要吟味)',
      items: {
        type: 'object',
        properties: {
          severity: { type: 'string' },
          title: { type: 'string' },
          sources: { type: 'array', items: { type: 'string' } },
        },
        required: ['severity', 'title', 'sources'],
      },
    },
    next_actions: { type: 'array', items: { type: 'string' } },
    summary: { type: 'string' },
  },
  required: ['table_md', 'convergent_findings', 'codex_divergent_findings', 'aux_only_findings', 'next_actions', 'summary'],
}

const merged = await agent(
  `5者のコードレビュー結果を統合してください。Lv7 は Codex を medium + high で多重化した「Codex 集中」構成です。

[各レビュアーの findings]
${JSON.stringify(ok.map((r) => ({ reviewer: r.reviewer, findings: r.findings })), null, 2)}

[タスク]
1. 同一の指摘を突き合わせる (location + 内容の類似で対応付け)。
2. Codex 多重シグナルを判定:
   - convergent_findings: codex_med と codex_high の **両方** が挙げた指摘 = 最強の収束シグナル。補助で同調した者を also_agreed_by に。
   - codex_divergent_findings: codex_med か codex_high の **片方のみ**。high 単独は深掘り発見、med 単独は過剰反応の可能性として source を明記。
   - aux_only_findings: 補助 (Gemini/DS/Qwen) のみが挙げ Codex 両方とも挙げていない指摘 = Codex 多重の盲点候補。
3. 7列統合表 table_md を markdown で作成:
   | 指摘 (🔴/🟠/🟡 + 根拠1行) | Codex(med) | Codex(high) | Gemini | DS | Qwen | 採用判断 |
   各列は ✅ / ❌ / 🔄 を記入。
4. next_actions: 実装すべき項目 (severity 高い順)。
5. summary: 総評 1-3行。

[重要 — over-attribution 禁止]
- 各 reviewer が実際に挙げた severity を尊重し、**あなたが勝手に severity を格上げしない**。
  (例: DS が 🟠「可能性」と書いたものを 🔴「真バグ」に格上げしない)
- 「複数者が一致」と書くのは、本当にその者の findings に該当指摘が存在する場合のみ。
- 各指摘の根拠は raw_log_path (C:/tmp-ai/cgd_raw_*.md) にあり、主 context で後から検証できることを前提に、確信度を誇張しない。

JSON で返す。`,
  { label: 'merge', phase: 'Merge', schema: MERGE_SCHEMA }
)

return {
  level: 7,
  label,
  table_md: merged.table_md,
  convergent_findings: merged.convergent_findings,
  codex_divergent_findings: merged.codex_divergent_findings,
  aux_only_findings: merged.aux_only_findings,
  next_actions: merged.next_actions,
  summary: merged.summary,
  usage: ok.map((r) => ({ reviewer: r.reviewer, usage_line: r.usage_line || '' })),
  raw_log_paths: ok.map((r) => ({ reviewer: r.reviewer, path: r.raw_log_path || '' })),
  note: '各 findings の根拠は raw_log_paths で検証可能。merge の severity/収束判定を鵜呑みにせず、🔴 採用前に raw を確認すること。',
}
