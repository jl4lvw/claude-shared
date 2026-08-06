// cgd_lv8_review.js — Lv8 (既定 6者: 技術 Codex med+high / DS / Qwen + 批評 Codex high / DS critic、
//                        Gemini はオプトインで7者化) の review phase を Workflow 化
//
// Lv7 (cgd_lv7_review.js) との差分:
//   - 批評パス 2 本を追加 (Codex high 批評 / DeepSeek critic)。技術構成は Lv7 と完全に同一
//   - 批評 findings は severity ではなく **困り度 (高/中/低)** を持つ (Lv3 の批評表と同型)
//   - merge が「技術レビュー表」と「批評レビュー表」の 2 表を生成する
//
// 2026-08-05: Lv8 も Workflow 実行を必須化したため新規作成 (それまで Lv8 は inline のみだった)。
//   Codex 呼出が 3 回・出力量が Lv7 以上に多く、主 context 圧迫が最も大きいレベルなので
//   本来いちばん Workflow 化が要る構成だった。
//   併せて codex 起動には CGD_WF_RUN=1 を前置し、PreToolUse ゲート (cgd_wf_gate.py) を通す。
//
// 主 context 側の事前準備 (この workflow の外):
//   - 関連関数を grep + Read で抽出 → aux 入力ファイルに同梱 (Lv7 と同じ)
//   - Codex(オプトイン時は Gemini も) 用入力 (input_path) と DS/Qwen 用入力 (aux_input_path) を準備
//
// 起動例（既定・Gemini なし）:
//   Workflow({ scriptPath: ".../cgd_lv8_review.js",
//              args: { input_path: "C:/tmp-ai/cgd_codex_<RUN>.txt",
//                      aux_input_path: "C:/tmp-ai/cgd_aux_<RUN>.txt", label: "target_<RUN>" } })
// 起動例（Gemini オプトイン）: 上記に include_gemini: true を追加

export const meta = {
  name: 'cgd-lv8-review',
  description: 'Lv8 既定6者(技術 Codex med+high/DS/Qwen + 批評 Codex high/DS critic)並列レビュー（Gemini は include_gemini でオプトイン・review phase のみ）',
  phases: [
    { title: 'Review', detail: '既定6者並列 (技術4 + 批評2、Gemini はオプトイン)' },
    { title: 'Merge', detail: '技術表(収束/乖離判定) + 批評表(困り度)の2表を生成' },
  ],
}

// ---- args ----
// Workflow ツールは args を JSON 文字列で渡す (実証済: typeof args === 'string')。
let _args = args
if (typeof _args === 'string') { try { _args = JSON.parse(_args) } catch (_) { _args = {} } }
if (!_args || typeof _args !== 'object') _args = {}

const codexInput = _args.input_path || 'C:/tmp-ai/lv8_codex_input.txt'
const auxInput = _args.aux_input_path || 'C:/tmp-ai/lv8_aux_input.txt'
const label = _args.label || 'target'
// PreToolUse ゲート(cgd_wf_gate.py)のバイパス nonce。主 context が
//   python cgd_wf_gate.py nonce
// で取得して args.wf_nonce に渡す。合致しない値では codex 起動が deny される。
const wfNonce = _args.wf_nonce || ''
const includeGemini = _args.include_gemini === true

// ドライラン: args パース + パス解決の確認用 (agent を呼ばず即 return)。
if (_args.dry_run === true) {
  log('[dry-run] codexInput=' + codexInput + ' / auxInput=' + auxInput + ' / label=' + label + ' / includeGemini=' + includeGemini + ' / wfNonce=' + (wfNonce ? 'あり' : '**未指定**'))
  return { dry_run: true, resolved_input_path: codexInput, resolved_aux_input_path: auxInput, resolved_label: label, resolved_include_gemini: includeGemini }
}

// ---- 構造化出力スキーマ ----
// 技術レビュー (Lv6/Lv7 と共通)
const TECH_SCHEMA = {
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

// 批評レビュー (Lv3 の批評表と同型。severity ではなく困り度)
const CRITIC_SCHEMA = {
  type: 'object',
  properties: {
    reviewer: { type: 'string' },
    auth_error: { type: 'boolean' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          difficulty: { type: 'string', enum: ['高', '中', '低'], description: '現場の困り度' },
          axis: { type: 'string', enum: ['現場の不満', 'あるべき論とのギャップ', 'そもそも論'] },
          title: { type: 'string' },
          rationale: { type: 'string', description: '根拠1行 (利用者の生の言葉に近いほどよい)' },
          suggested_direction: { type: 'string', description: '改善の方向 (詳細設計まではしない)' },
        },
        required: ['difficulty', 'axis', 'title', 'rationale'],
      },
    },
    usage_line: { type: 'string' },
    raw_log_path: { type: 'string' },
  },
  required: ['reviewer', 'auth_error', 'findings'],
}

// ---- 批評プロンプト (SKILL.md Step 2-8D Bash #5 と同一文面) ----
const CRITIC_PROMPT =
  'まず ' + codexInput + ' の全文を読んでください。あなたは辛口の評価者です。' +
  '技術的な正しさ（バグの有無）ではなく『使う人が困らないか』『本来この仕様はどうあるべきか』の観点で、' +
  '遠慮なく否定的に評価してください。次の2つの立場を併せ持ってください: ' +
  '(1) ITに疎い現場担当者 — 実際に使うときの使いにくさ・わかりにくさ・手数の多さ・エラー時の困りごとを利用者の生の言葉で指摘する。' +
  '(2) 熟練ITアーキテクト — 『本来この仕様はどうあるべきか』を理想形から逆算し、現状の妥協・場当たり対応・本質を外した設計・優先度の誤りを批判する。' +
  '出力は次の構造で: 1.現場の不満（各項目に困り度: 高/中/低を付ける） 2.あるべき論とのギャップ 3.そもそも論（この機能は本当に要るか） 4.辛口総評（1〜2行で断言）。' +
  '擁護・肯定・『概ね良い』は禁止。技術的なバグ指摘には深入りしない。日本語で回答。'

// ---- レビュアー定義 ----
//   技術: Codex(med) / Codex(high) / DS / Qwen  (+ Gemini オプトイン)
//   批評: Codex(high 批評・新規セッション) / DS critic
// codex 起動には CGD_WF_RUN=1 を前置する (PreToolUse の WF 必須ゲートを通すため)。
const reviewers = [
  {
    name: 'codex_med', kind: 'tech',
    cmd: `mkdir -p /c/tmp-ai && cd /c/tmp-ai && CGD_WF_RUN=${wfNonce} codex exec -c model_reasoning_effort="medium" --sandbox read-only --skip-git-repo-check "まず ${codexInput} の全文を読み、記載の差分・対象・評価観点に従ってコードレビュー。関連関数の抜粋は入力に同梱済み。追加で開くのは最大5ファイルまでとし、超えるなら読まずに『情報不足: <欲しいファイル>』と書いて終えること。日本語で回答。" < /dev/null`,
    timeout: 300000, usage: false, isCodex: true,
    authSignals: 'Not logged in / 401 / unauthorized',
  },
  {
    name: 'codex_high', kind: 'tech',
    cmd: `mkdir -p /c/tmp-ai && cd /c/tmp-ai && CGD_WF_RUN=${wfNonce} codex exec -c model_reasoning_effort="high" --sandbox read-only --skip-git-repo-check "まず ${codexInput} の全文を読み、記載の差分・対象・評価観点に従ってコードレビュー。関連関数の抜粋は入力に同梱済み。追加で開くのは最大5ファイルまでとし、超えるなら読まずに『情報不足: <欲しいファイル>』と書いて終えること。日本語で回答。" < /dev/null`,
    timeout: 600000, usage: false, isCodex: true,
    authSignals: 'Not logged in / 401 / unauthorized',
  },
  ...(includeGemini ? [{
    name: 'gemini', kind: 'tech',
    cmd: `python "C:/ClaudeCode/.claude/tools/gemini_advisor.py" --role reviewer "${codexInput}"`,
    timeout: 180000, usage: true, isCodex: false,
    authSignals: 'AuthenticationError / 401 / invalid api key / GEMINI_API_KEY が設定されていません',
  }] : []),
  {
    name: 'deepseek', kind: 'tech',
    cmd: `python "C:/ClaudeCode/.claude/tools/deepseek_coder.py" --role reviewer "${auxInput}"`,
    timeout: 180000, usage: true, isCodex: false,
    authSignals: 'AuthenticationError / 401 / invalid api key / DEEPSEEK_API_KEY が設定されていません',
  },
  {
    name: 'qwen', kind: 'tech',
    cmd: `python "C:/ClaudeCode/.claude/tools/qwen_advisor.py" --role reviewer "${auxInput}"`,
    timeout: 180000, usage: true, isCodex: false,
    authSignals: 'AuthenticationError / 401 / InvalidApiKey / DASHSCOPE_API_KEY が設定されていません',
  },
  {
    name: 'codex_critic', kind: 'critic',
    cmd: `mkdir -p /c/tmp-ai && cd /c/tmp-ai && CGD_WF_RUN=${wfNonce} codex exec -c model_reasoning_effort="high" --sandbox read-only --skip-git-repo-check "${CRITIC_PROMPT}" < /dev/null`,
    timeout: 600000, usage: false, isCodex: true,
    authSignals: 'Not logged in / 401 / unauthorized',
  },
  {
    name: 'deepseek_critic', kind: 'critic',
    cmd: `python "C:/ClaudeCode/.claude/tools/deepseek_coder.py" --role critic "${auxInput}"`,
    timeout: 180000, usage: true, isCodex: false,
    authSignals: 'AuthenticationError / 401 / invalid api key / DEEPSEEK_API_KEY が設定されていません',
  },
]

phase('Review')

const reviews = await parallel(reviewers.map((r) => () =>
  agent(
    `あなたは外部レビュアー「${r.name}」を実行し、その出力を構造化レビュー結果に変換する担当です。
これは cgd Lv8（技術の最深掘り + 複眼批評）の${r.kind === 'tech' ? '**技術レビュー**' : '**批評レビュー**'}枠です。

[手順]
1. Bash tool を timeout=${r.timeout} (ミリ秒) で使って次のコマンドを実行する:
${r.cmd}

2. 標準出力の全文を Write tool で C:/tmp-ai/cgd_raw_${r.name}_${label}.md に保存する (人が生ログを検証できるように・これは必須)。
3. 出力を読み、指摘を findings 配列に構造化する:
${r.kind === 'tech'
      ? `   - severity: 🔴 (重大: セキュリティ/データ破壊/公開API逸脱/明白な論理バグ/integrationバグ) / 🟠 (重要) / 🟡 (注意)
   - title / location (file:line) / rationale (根拠1行) / recommended_fix
   - 重要: ${r.name} が **実際に挙げた severity をそのまま尊重** する。あなたが勝手に格上げ/格下げしない。`
      : `   - difficulty: 現場の困り度 高/中/低（${r.name} が付けた困り度をそのまま尊重する）
   - axis: 「現場の不満」/「あるべき論とのギャップ」/「そもそも論」のどれか
   - title / rationale (根拠1行・利用者の生の言葉に近いほどよい) / suggested_direction (改善の方向)
   - 重要: 批評は severity を持たない。技術的なバグ指摘が混ざっていたら **批評 findings には入れない**。`}
4. ${r.usage ? 'stderr の [DS Usage] / [Qwen Usage] / [Gemini Usage] の「今回:」行を usage_line に転記する。' : 'usage_line は空文字 ("")。'}
5. 認証エラー (${r.authSignals}) を検出したら auth_error=true、findings は空配列。それ以外は auth_error=false。
6. reviewer フィールドに "${r.name}" を入れる。

[重要]
- 最終出力 (schema JSON) だけが親に返る。生レビュー文を return に含めない (構造化 findings に圧縮)。
- ${r.isCodex ? 'あなた (Codex) は sandbox read-only で対象ファイルを直接読めるので、必要なら関連箇所を確認すること。' : `コマンド失敗/タイムアウト時は auth 判定を試み、不能なら auth_error=false で findings に「${r.name} 実行失敗」を1件入れる。`}

JSON で返す。`,
    { label: `review:${r.name}`, phase: 'Review', schema: r.kind === 'tech' ? TECH_SCHEMA : CRITIC_SCHEMA }
  )
))

// ---- 認証エラー / 欠員チェック (Lv8 は参加者全員 + Codex 3 回すべて成功が必須) ----
const ok = reviews.filter(Boolean)
const authFailed = ok.filter((r) => r.auth_error).map((r) => r.reviewer)
if (authFailed.length > 0) {
  log(`認証エラー検出: ${authFailed.join(', ')} → Lv8 中断`)
  return { halt: 'auth_error', failed: authFailed, message: `認証エラー: ${authFailed.join(', ')}。復旧後に再実行。` }
}
const codexNames = ['codex_med', 'codex_high', 'codex_critic']
const codexOk = ok.filter((r) => codexNames.indexOf(r.reviewer) !== -1).length
if (codexOk < 3) {
  log(`Codex が欠けている (${codexOk}/3) → Lv8 中断 (med技術 + high技術 + high批評 の3本が本質)`)
  return { halt: 'codex_incomplete', got: codexOk, message: 'Lv8 は Codex medium(技術) / high(技術) / high(批評) の3本すべての成功が必須です。' }
}
if (ok.length < reviewers.length) {
  log(`レビュアー欠員: ${ok.length}/${reviewers.length} のみ成功 → Lv8 中断`)
  return { halt: 'incomplete', got: ok.length, expected: reviewers.length, message: `Lv8 は参加者${reviewers.length}者全員の成功が必要です。` }
}

phase('Merge')

const MERGE_SCHEMA = {
  type: 'object',
  properties: {
    tech_table_md: { type: 'string', description: '技術レビュー統合表 (markdown)' },
    critic_table_md: { type: 'string', description: '批評レビュー表 (markdown・困り度ベース)' },
    convergent_findings: {
      type: 'array',
      description: '技術: Codex med と high の両方が挙げた指摘 (最強の収束シグナル)',
      items: {
        type: 'object',
        properties: {
          severity: { type: 'string' },
          title: { type: 'string' },
          also_agreed_by: { type: 'array', items: { type: 'string' } },
          recommended_fix: { type: 'string' },
        },
        required: ['severity', 'title'],
      },
    },
    codex_divergent_findings: {
      type: 'array',
      description: '技術: Codex 片方のみ (high単独=深掘り発見 / med単独=過剰反応の可能性)',
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
      description: '技術: 補助(DS/Qwen、オプトイン時はGeminiも)のみが挙げた指摘 (Codex多重の盲点候補)',
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
    critic_convergent: {
      type: 'array',
      description: '批評: Codex(批評) と DS critic の両方が挙げた観点 (収束・信頼度高)',
      items: {
        type: 'object',
        properties: {
          difficulty: { type: 'string' },
          axis: { type: 'string' },
          title: { type: 'string' },
          suggested_direction: { type: 'string' },
        },
        required: ['difficulty', 'title'],
      },
    },
    next_actions: { type: 'array', items: { type: 'string' } },
    summary: { type: 'string', description: '技術・批評あわせた総評 1-3行' },
  },
  required: ['tech_table_md', 'critic_table_md', 'convergent_findings', 'codex_divergent_findings', 'aux_only_findings', 'critic_convergent', 'next_actions', 'summary'],
}

const techReviewers = reviewers.filter((r) => r.kind === 'tech').map((r) => r.name)
const displayNames = {
  codex_med: 'Codex(med)', codex_high: 'Codex(high)', gemini: 'Gemini', deepseek: 'DS', qwen: 'Qwen',
  codex_critic: 'Codex(high)（批評）', deepseek_critic: 'DeepSeek（批評）',
}
const techColumns = ['指摘 (🔴/🟠/🟡 + 根拠1行)', ...techReviewers.map((n) => displayNames[n] || n), '採用判断']
const criticColumns = ['観点 (困り度 高/中/低)', 'Codex(high)（批評）', 'DeepSeek（批評）', '採用']

const techFindings = ok.filter((r) => techReviewers.indexOf(r.reviewer) !== -1)
const criticFindings = ok.filter((r) => techReviewers.indexOf(r.reviewer) === -1)

const merged = await agent(
  `cgd Lv8 のレビュー結果を統合してください。Lv8 は「技術の最深掘り (Codex を medium + high で多重化 + DS/Qwen 補助)」と
「複眼批評 (Codex high + DeepSeek critic)」を同時に走らせる構成です。**技術表と批評表の 2 表**を作ります。

[技術レビュアーの findings]
${JSON.stringify(techFindings.map((r) => ({ reviewer: r.reviewer, findings: r.findings })), null, 2)}

[批評レビュアーの findings]
${JSON.stringify(criticFindings.map((r) => ({ reviewer: r.reviewer, findings: r.findings })), null, 2)}

[タスク]
1. 技術: 同一の指摘を突き合わせ (location + 内容の類似)、Codex 多重シグナルを判定する。
   - convergent_findings: codex_med と codex_high の **両方** が挙げた指摘 = 最強の収束シグナル
   - codex_divergent_findings: 片方のみ。high 単独は深掘り発見、med 単独は過剰反応の可能性として source を明記
   - aux_only_findings: 補助のみが挙げ Codex 両方とも挙げていない指摘 = 盲点候補
   - tech_table_md の列は必ずこの順番・この列名: ${JSON.stringify(techColumns)}（各列に ✅ / ❌ / 🔄）
2. 批評: 困り度ベースで統合する。**severity（🔴🟠🟡）を付けない**。
   - critic_convergent: 2者が同じ観点を挙げたもの（信頼度高）
   - critic_table_md の列は必ずこの順番・この列名: ${JSON.stringify(criticColumns)}
   - 軸は「現場の不満」「あるべき論とのギャップ」「そもそも論」で整理する
3. next_actions: 実装すべき項目（技術は severity 高い順、批評は困り度高い順。**どちらの表由来かを明記**）
4. summary: 技術・批評あわせた総評 1-3行

[重要 — over-attribution 禁止]
- 各 reviewer が実際に挙げた severity / 困り度を尊重し、**あなたが勝手に格上げしない**
- 「複数者が一致」と書くのは、本当にその者の findings に該当指摘が存在する場合のみ
- **批評の指摘を技術表に混ぜない**。批評は severity を持たず、Step C2 の自動修正ループの対象外である
- 各指摘の根拠は raw_log_path (C:/tmp-ai/cgd_raw_*.md) にあり、主 context で後から検証できることを前提に、確信度を誇張しない

JSON で返す。`,
  { label: 'merge', phase: 'Merge', schema: MERGE_SCHEMA }
)

return {
  level: 8,
  label,
  include_gemini: includeGemini,
  participants: reviewers.map((r) => r.name),
  tech_table_md: merged.tech_table_md,
  critic_table_md: merged.critic_table_md,
  convergent_findings: merged.convergent_findings,
  codex_divergent_findings: merged.codex_divergent_findings,
  aux_only_findings: merged.aux_only_findings,
  critic_convergent: merged.critic_convergent,
  next_actions: merged.next_actions,
  summary: merged.summary,
  usage: ok.map((r) => ({ reviewer: r.reviewer, usage_line: r.usage_line || '' })),
  raw_log_paths: ok.map((r) => ({ reviewer: r.reviewer, path: r.raw_log_path || '' })),
  note: '各 findings の根拠は raw_log_paths で検証可能。merge の severity/収束判定を鵜呑みにせず、🔴 採用前に raw を確認すること。批評表の指摘は Step C2 の自動修正対象外（技術表の 🔴 のみ）。',
}
