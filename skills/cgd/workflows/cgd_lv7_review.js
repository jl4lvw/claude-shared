// cgd_lv7_review.js — Lv7 (既定 Codex 多重med+high + DS + Qwen 4者並列、Gemini はオプトインで5者化) の review phase を Workflow 化
//
// Lv6 (cgd_lv6_review.js) との差分:
//   - Codex を medium + high の 2 並列に多重化 (収束=最強シグナル / high単独=深掘り発見)
//   - DS/Qwen には「関連関数抜粋 + 差分」(aux_input_path) を渡す (表層指摘を減らす)
//   - merge で codex_med ∩ codex_high の一致判定を自動化
//
// 2026-07: Gemini は AI Studio 無料枠のレート制限でエラーが頻発したため既定から除外。
//          args.include_gemini === true のときだけ5者目（補助）として追加参加させる。
//          あわせて、旧廃止済み `gemini` CLI を直接叩いていたバグ（呼べば必ず失敗する）を
//          `gemini_advisor.py`（OpenAI互換API）呼び出しに修正した。
//
// 主 context 側の事前準備 (この workflow の外):
//   - Lv7 限定: git diff + grep + Read で関連関数を抽出 → aux 入力ファイルに同梱
//   - Codex（オプトイン時は Gemini も）用入力 (input_path) と DS/Qwen 用入力 (aux_input_path) を準備
//
// 起動例（既定・Gemini なし）:
//   Workflow({ scriptPath: ".../cgd_lv7_review.js",
//              args: { input_path: "C:/tmp-ai/lv7_codex_input.txt",
//                      aux_input_path: "C:/tmp-ai/lv7_aux_input.txt", label: "pickorder-scan" } })
// 起動例（Gemini オプトイン）: 上記に include_gemini: true を追加

export const meta = {
  name: 'cgd-lv7-review',
  description: 'Lv7 既定Codex多重(med+high)+DS+Qwen 4者並列レビュー（Gemini はinclude_geminiでオプトイン・review phaseのみ）',
  phases: [
    { title: 'Preflight', detail: '入力ファイルの実在確認と内容の可視化（取り違え防止）' },
    { title: 'Review', detail: '既定4者並列 (Codex med + Codex high + DS + Qwen、Gemini はオプトイン)' },
    { title: 'Merge', detail: 'Codex多重の収束/乖離判定 + 統合表' },
  ],
}

// ---- args ----
// Workflow ツールは args を JSON 文字列で渡す (実証済: typeof args === 'string')。
// そのままだと args.input_path が undefined になるため JSON.parse でオブジェクト化する。
let _args = args
if (typeof _args === 'string') { try { _args = JSON.parse(_args) } catch (_) { _args = {} } }
if (!_args || typeof _args !== 'object') _args = {}

// A: input_path / aux_input_path are required. Their silent fallback defaults were removed
// (2026-08-11); wrong arg names used to still "work" by reading a stale default file.
// NOTE: only the *input paths* lost their defaults. label / include_gemini still fall back
// to defaults on purpose — those cannot silently review the wrong thing.
const _missing = []
if (!_args.input_path) _missing.push('input_path')
if (!_args.aux_input_path) _missing.push('aux_input_path')
if (_missing.length > 0) {
  return {
    halt: 'missing_args',
    missing: _missing,
    given_keys: Object.keys(_args),
    message: 'Required args are missing: ' + _missing.join(', ')
      + ' / given keys: ' + (Object.keys(_args).join(', ') || '(none)')
      + ' / correct form: args { input_path, aux_input_path, label } (wf_nonce is optional)'
      + ' / NOTE: codex_file and aux_file are NOT valid key names.',
  }
}
const codexInput = _args.input_path
const auxInput = _args.aux_input_path
// label は生ログの保存先パスに埋め込まれる。`/` や `..` が混ざると
// 意図しないディレクトリの .md を上書きしうるので、英数とハイフン等に限定する。
// 悪意より「打ち間違い」を止めるのが目的。
const _rawLabel = String(_args.label || 'target')
const label = _rawLabel.replace(/[^A-Za-z0-9_.-]/g, '_').slice(0, 60) || 'target'
// PreToolUse ゲート(cgd_wf_gate.py)のバイパス nonce。
// **通常は渡さなくてよい** — Preflight が `cgd_wf_gate.py status --json` から自分で取得する。
// ここで受けるのは手動で別の値を使いたい場合の上書き手段としてだけ (2026-08-11 に任意へ降格)。
const wfNonce = _args.wf_nonce || ''
const includeGemini = _args.include_gemini === true
// 取りまとめ(オーケストレーション=指示と判断)に使うモデル。
// ユーザー指示 (2026-08-12): Claude の最上位モデルを使い、使えなければ Opus に落とす。
// 並列で回すレビュアーは対象外（あれは作業側で、判断側ではない）。
const MERGE_MODEL = _args.merge_model || 'fable'
const MERGE_FALLBACK = _args.merge_fallback || 'opus'


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
    // 「コマンドが実際に走ったか」を findings とは独立に必ず報告させる (2026-08-11)。
    // findings が空でも auth_error=false なら成功扱いになり、タイムアウトや deny で
    // 死んだレビュアーが「指摘なし」として通過していた。
    executed: { type: 'boolean', description: 'コマンドが起動でき、正常終了したか' },
    exit_code: { type: 'integer', description: '終了コード。取得できなければ -1' },
  },
  required: ['reviewer', 'auth_error', 'findings', 'executed'],
}

// ---- レビュアー定義 ----
//   Codex（オプトイン時は Gemini も）は codexInput (ファイルパス渡し・sandbox で関連コードを自分で読める)
//   DS/Qwen は auxInput (関連関数抜粋 + 差分・API なのでファイルアクセス不可)
// 既定は Codex med+high+DS+Qwen の4者。Gemini は includeGemini のときだけ Codex(high) の次に追加し、
// SKILL.md の統合表の列順「Codex(med) | Codex(high) | (Gemini) | DS | Qwen」と揃える。
let reviewers = [
  {
    name: 'codex_med',
    cmd: `mkdir -p /c/tmp-ai && cd /c/tmp-ai && CGD_WF_RUN=__WF_NONCE__ codex exec -c model_reasoning_effort="medium" --sandbox read-only --skip-git-repo-check "まず __INPUT_0__ の全文を読み、記載の差分・対象・評価観点に従ってコードレビュー。関連関数の抜粋は入力に同梱済み。追加で開くのは最大5ファイルまでとし、超えるなら読まずに『情報不足: <欲しいファイル>』と書いて終えること。日本語で回答。" < /dev/null`,
    timeout: 300000,
    usage: false,
    authSignals: 'Not logged in / 401 / unauthorized',
    isCodex: true,
  },
  {
    name: 'codex_high',
    cmd: `mkdir -p /c/tmp-ai && cd /c/tmp-ai && CGD_WF_RUN=__WF_NONCE__ codex exec -c model_reasoning_effort="high" --sandbox read-only --skip-git-repo-check "まず __INPUT_0__ の全文を読み、記載の差分・対象・評価観点に従ってコードレビュー。関連関数の抜粋は入力に同梱済み。追加で開くのは最大5ファイルまでとし、超えるなら読まずに『情報不足: <欲しいファイル>』と書いて終えること。日本語で回答。" < /dev/null`,
    timeout: 600000,
    usage: false,
    authSignals: 'Not logged in / 401 / unauthorized',
    isCodex: true,
  },
  ...(includeGemini ? [{
    name: 'gemini',
    cmd: `python "C:/ClaudeCode/.claude/tools/gemini_advisor.py" --role reviewer "__INPUT_0__"`,
    timeout: 600000,
    usage: true,
    authSignals: 'AuthenticationError / 401 / invalid api key / GEMINI_API_KEY が設定されていません',
    isCodex: false,
  }] : []),
  {
    name: 'deepseek',
    cmd: `python "C:/ClaudeCode/.claude/tools/deepseek_coder.py" --role reviewer "__INPUT_1__"`,
    timeout: 600000,
    usage: true,
    authSignals: 'AuthenticationError / 401 / invalid api key / DEEPSEEK_API_KEY が設定されていません',
    isCodex: false,
  },
  {
    name: 'qwen',
    cmd: `python "C:/ClaudeCode/.claude/tools/qwen_advisor.py" --role reviewer "__INPUT_1__"`,
    timeout: 600000,
    usage: true,
    authSignals: 'AuthenticationError / 401 / InvalidApiKey / DASHSCOPE_API_KEY が設定されていません',
    isCodex: false,
  },
]

// args.reviewers が渡されていればそれを使う (2026-08-12)。
// 同じ定義が lv6/lv7/lv8 に 3 重複製されており、片方だけ直す事故を何度も踏んだ。
// Python (cgd_reviewers.py) を単一の出所にし、cgd_plan.py build が args に載せる。
// **Workflow はファイルを読めない**ので、LLM を介さずに届く経路は args しかない。
// 渡されなければ上の内蔵定義をそのまま使う（後方互換・build を経由しない起動を壊さない）。
if (Array.isArray(_args.reviewers) && _args.reviewers.length > 0) {
  const _bad = _args.reviewers.filter(
    (r) => !r || typeof r.name !== 'string' || typeof r.cmd !== 'string'
      || !Number.isSafeInteger(r.timeout) || r.timeout <= 0
  )
  if (_bad.length > 0) {
    return {
      halt: 'bad_reviewers',
      given: _args.reviewers.length,
      message: 'args.reviewers の形式が不正です (name/cmd は文字列・timeout は正の整数が必須)。'
        + ' cgd_plan.py build が出力した WORKFLOW_ARGS をそのまま渡してください。',
    }
  }
  reviewers = _args.reviewers.map((r) => ({
    name: r.name,
    kind: r.kind === 'critic' ? 'critic' : 'tech',
    cmd: r.cmd,
    timeout: r.timeout,
    usage: r.usage === true,
    isCodex: r.isCodex === true,
    authSignals: typeof r.authSignals === 'string' ? r.authSignals : '',
  }))
  log(`[preflight] レビュアー定義を args から採用しました (${reviewers.length} 者)`)
}


// ---- B + D: 入力の実在確認と内容の可視化 (2026-08-11 追加) ----
// Workflow スクリプトはファイルシステムを触れないので、専用の agent に確認させる。
// **主 context 側の事前チェックにはしない** — 任意の事前検証は急いでいるときに飛ばされる
// （AGENTS.md「検証を事前ではなく事後に置く」）。ここで落とせば 6 者分の呼出を無駄にしない。
phase('Preflight')

// 2026-08-11 改訂: 事実判定を agent の構造化出力に置かない。
// 初版は agent に exists/bytes/gate_armed を判断させていたが、Lv8 セルフレビューで
// Codex(med/high)/DS/Qwen の 4 者が揃って「ガードが LLM の自己申告依存で信頼境界が
// 成立していない」と 🔴 指摘した。実際 gate_armed は誤報した（ゲートは張られていたのに
// false を返した / INC-20260811-1440406a0dfc）。
// そこで agent の役割は「決められた 2 コマンドを実行して標準出力をそのまま返す」に縮小し、
// 判定は WF 側が生 JSON を厳格にパースして行う。
// (このブロックは lv6/lv7/lv8 に複製されている。WF スクリプトは import を持てないため
//  共通モジュール化ができない。片方だけ直さないこと。)
const PREFLIGHT_SCHEMA = {
  type: 'object',
  properties: {
    files_json: { type: 'string', description: 'preflight_inputs.py の標準出力を一字一句そのまま' },
    gate_json: { type: 'string', description: 'cgd_wf_gate.py status --json の標準出力を一字一句そのまま' },
  },
  required: ['files_json', 'gate_json'],
}

const _targetsRaw = [codexInput, auxInput]

// 入力パスは **絶対パス + スラッシュ区切り** に正規化してから使う (2026-08-11)。
// 理由が 2 つある:
//   1) 相対パスは Preflight agent と codex(cd /c/tmp-ai する)で別の cwd を基準に
//      解決されるため、「検証は通ったのに別ファイルをレビューしていた」が起き得る
//   2) バックスラッシュは bash のダブルクォート内で 1 段落ちる。UNC 表記が潰れ、
//      ガードが「含まれていない」と「要求していないものが混ざっている」を同時に
//      言う謎の失敗になる
// 正規表現ではなく split/join を使うのは、エスケープの取り違えを避けるため。
const _toPosix = (p) => String(p).split('\\').join('/')
const _isAbsolute = (p) => /^[A-Za-z]:\//.test(p) || p.startsWith('//')
// input_path と aux_input_path に同じファイルを渡された場合、そのまま 2 件並べると
// Preflight の 1:1 照合が「結果に 2 件重複している」と誤診して必ず halt する。
// 同じ入力を渡すこと自体は無害(lv6 は元から入力 1 本)なので、畳んでから検証する。
const _normalized = _targetsRaw.map(_toPosix)
const _targets = _normalized.filter(
  (p, i) => _normalized.findIndex((q) => q.toLowerCase() === p.toLowerCase()) === i
)
if (_targets.length !== _normalized.length) {
  log('[preflight] input_path と aux_input_path が同じファイルです。1 本として扱います。')
}
// シェルコマンド文字列へ埋め込むため、クォートを壊す文字を含むパスは受け付けない。
// エスケープで頑張るより「そういうパスは使わない」と決める方が確実で、
// レビュー入力は C:/tmp-ai/ に置く運用なので実害がない。
// 正規表現ではなく文字の配列で判定するのは、エスケープの取り違えを避けるため
// （実際にこの行を正規表現で書いて 1 度壊した）。
const _UNSAFE_CHARS = ['"', "'", '`', '$', ';', '&', '|', '<', '>', '*', '?',
  String.fromCharCode(10), String.fromCharCode(13)]
const _unsafe = _targets.filter((p) => _UNSAFE_CHARS.some((c) => String(p).indexOf(c) !== -1))
if (_unsafe.length > 0) {
  return {
    halt: 'unsafe_path',
    targets: _targets,
    message: 'パスにシェルで解釈される文字が含まれています（コマンドが壊れるため受け付けません）: '
      + _unsafe.join(' , ')
      + ' / 入力は C:/tmp-ai/ 配下の英数字パスに置いてください。',
  }
}

const _relative = _targets.filter((p) => !_isAbsolute(p))
if (_relative.length > 0) {
  return {
    halt: 'relative_path',
    targets: _targets,
    message: '入力は絶対パスで指定してください（相対パスは Preflight と codex で解決先がズレます）: '
      + _relative.join(' , ')
      + ' / 例: C:/tmp-ai/cgd_codex_20260811.txt （UNC は //server/share/... の形）',
  }
}
const pre = await agent(
  `cgd Lv7 の入力を検証します。**レビューはしません。ファイルの中身を解釈・要約しないでください。**

[手順] 次の 2 つのコマンドを Bash で実行し、**標準出力を一字一句そのまま**返してください。

1) python "C:/ClaudeCode/.claude/tools/preflight_inputs.py" ${_targets.map((f) => `"${f}"`).join(' ')}
   → 出力全体を files_json に入れる

2) python "C:/ClaudeCode/.claude/hooks/cgd_wf_gate.py" status --json
   → 出力全体を gate_json に入れる

[重要]
- 出力を要約・整形・修正しない。JSON を**文字列のまま**入れる。
- コマンドが失敗したら、そのエラー出力をそのまま入れる。**作文しない。**
- 追加のファイルを探したり、別のパスで再試行したりしない。`,
  { label: 'preflight', phase: 'Preflight', schema: PREFLIGHT_SCHEMA }
)

const _parseJson = (raw) => { try { return JSON.parse(String(raw)) } catch (_) { return null } }
// Windows なので大文字小文字と区切り文字を吸収して比較する。
const _norm = (p) => String(p).replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase()

const _filesDoc = _parseJson(pre && pre.files_json)
const _files = _filesDoc && Array.isArray(_filesDoc.files) ? _filesDoc.files : null
if (!_files) {
  return {
    halt: 'preflight_unparsable',
    raw: String((pre && pre.files_json) || '').slice(0, 500),
    message: 'Preflight の出力を JSON として解釈できませんでした。安全側に倒して停止します。',
  }
}

// 要求したパスと 1:1 で一致することを検証する。
// 件数と exists/bytes だけの比較では「別ファイル・重複・余剰」を検出できない (🔴 4者一致)。
const _problems = []
for (const t of _targets) {
  const hits = _files.filter((f) => f && _norm(f.path) === _norm(t))
  if (hits.length === 0) { _problems.push(`  ✗ ${t} — Preflight の結果に含まれていない`); continue }
  if (hits.length > 1) { _problems.push(`  ✗ ${t} — 結果に ${hits.length} 件重複している`); continue }
  const f = hits[0]
  // error があれば必ず出す。stat の権限エラーやパス長エラーを「存在しない」とだけ
  // 表示すると、実在するのに無いと言われて原因に辿り着けない (2026-08-11)。
  if (f.exists !== true) {
    _problems.push(`  ✗ ${t} — 存在しない (exists=${JSON.stringify(f.exists)})`
      + (f.error ? ` / 詳細: ${f.error}` : ''))
  }
  else if (f.is_file !== true) _problems.push(`  ✗ ${t} — 通常ファイルではない (ディレクトリ?)`)
  // 存在していても読めなければレビューできない。preflight_inputs は失敗時に
  // readable=false と error を返すので、そちらも見て安全側に倒す (2026-08-11 🔴)。
  else if (f.readable !== true) _problems.push(`  ✗ ${t} — 読み取れない (readable=${JSON.stringify(f.readable)})`)
  else if (f.error) _problems.push(`  ✗ ${t} — 検査でエラー: ${f.error}`)
  else if (!Number.isSafeInteger(f.bytes) || f.bytes <= 0) {
    // `!f.bytes` の falsy 判定では負数・文字列・型崩れを弾けない (🟠 4者一致)。
    _problems.push(`  ✗ ${t} — サイズが不正または空 (bytes=${JSON.stringify(f.bytes)})`)
  }
}
for (const f of _files) {
  if (f && !_targets.some((t) => _norm(t) === _norm(f.path))) {
    _problems.push(`  ✗ 要求していないファイルが結果に混ざっている: ${f.path}`)
  }
}

if (_problems.length > 0) {
  return {
    halt: 'input_missing',
    targets: _targets,
    files: _files,
    message: '入力の検証に失敗しました。渡したパスが正しいか確認してください:\n' + _problems.join('\n'),
  }
}

// 生ログのファイル名に入力の指紋を混ぜる。reviewer 名 + label だけだと、
// 同じ label で並行実行したときに別ランの生ログを上書きしてしまう。
const _runTag = String((_files[0] && _files[0].sha256) || 'nosha').slice(0, 8)

// D: 「何をレビューしたのか」を必ず残す。取り違えは中身を見れば一発で分かる。
// sha256 も出す（正しいパス・正しいサイズのまま中身だけ別物、を後から照合できるように）。
for (const f of _files) {
  log(`[preflight] 入力OK ${f.path} — ${f.bytes} bytes / 更新 ${f.mtime || '?'} / sha256 ${String(f.sha256 || '').slice(0, 12)}`)
  log(`[preflight]   冒頭: ${String(f.head || '').slice(0, 120).replace(/\n/g, ' ')}`)
}

// ゲート状態も boolean を agent に判断させない。壊れていたら fail-closed で止める。
const _gateDoc = _parseJson(pre.gate_json)
if (!_gateDoc || typeof _gateDoc.armed !== 'boolean') {
  return {
    halt: 'gate_unknown',
    raw: String(pre.gate_json || '').slice(0, 500),
    message: 'ゲート状態を判定できませんでした。誤って nonce 無しで進むと codex が全て deny されるため、安全側に倒して停止します。',
  }
}

// ドライラン: **Preflight を通した後**に返す (2026-08-11 改訂)。
// 以前は agent を呼ばずに即 return していたが、それでは今回の事故
// (正しいキー名・実在パスなのに中身が別物) を一切検出できず、
// 「dry-run が通った＝入力は大丈夫」という誤った安心を与えていた。
// agent 1 本ぶんの費用はかかるが、確かめたいのはまさにその 1 本の結果。
if (_args.dry_run === true) {
  return {
    dry_run: true,
    resolved_targets: _targets,
    resolved_label: label,
    files: _files.map((f) => ({
      path: f.path, bytes: f.bytes, mtime: f.mtime,
      sha256: f.sha256, head: String(f.head || '').slice(0, 120),
    })),
    gate: _gateDoc,
    message: '入力の実在確認まで済んでいます。レビューは実行していません。',
  }
}

// nonce は WF が自分で取る (2026-08-11)。args.wf_nonce は任意の上書き手段として残す。
// 「取得して args に貼る」手順は Lv8 批評で『安全機構が手順書運用に落ちている』と
// 困り度「高」で指摘され、実際に付け忘れ事故も起きた。nonce は元から nonce サブコマンドで
// 誰でも取得でき、意図的迂回も認めている「意図の印」なので、自己取得で防御力は変わらない。
// **複数ゲート / 破損ゲートがあるときは選ばない。**
// 先頭の nonce を無条件で採ると、他セッションのゲートの nonce を掴んで
// hook 側の照合(read_gate(session))と食い違い、全レビュアーが deny される。
// CLI の `nonce` サブコマンドは同じ条件で exit 1 拒否しているので、判断を揃える。
const _gates = Array.isArray(_gateDoc.gates) ? _gateDoc.gates : []
if (!wfNonce && _gateDoc.armed && (_gates.length > 1 || _gates.some((g) => g && g.corrupt))) {
  return {
    halt: 'gate_ambiguous',
    gate: _gateDoc,
    message: 'ゲートが複数(または破損)あり、どの nonce を使うべきか決められません。'
      + ' 次のいずれかで解消してください: '
      + 'python "C:/ClaudeCode/.claude/hooks/cgd_wf_gate.py" disarm --all'
      + ' / 残したいものだけ arm し直す / args.wf_nonce で明示する。',
  }
}
const _gateNonce = _gates.map((g) => g && g.nonce).find(Boolean) || ''
const _nonce = wfNonce || _gateNonce
if (_gateDoc.armed && !_nonce) {
  log('[preflight] ⚠ ゲートが張られているのに nonce を取得できませんでした。codex 起動は deny されます。')
  return {
    halt: 'nonce_missing',
    gate: _gateDoc,
    message: 'ゲート有効なのに nonce が取得できませんでした。`cgd_wf_gate.py status --json` の出力を確認してください。',
  }
}
// 各 codex コマンドに実際の nonce を差し込む (reviewers は Preflight より前に組み立てるため
// プレースホルダにしてある)。
for (const r of reviewers) {
  if (typeof r.cmd !== 'string') continue
  r.cmd = r.cmd.split('__WF_NONCE__').join(_nonce)
  // 入力パスも **検証を通した値** を差し込む。
  // reviewers は Preflight より前に組み立てるため、以前は生の _args.input_path を
  // 埋め込んでいた。正規化(_toPosix)前の値なので、バックスラッシュ入りパスでは
  // 「検証した文字列」と「実行に使う文字列」が別物になり、
  // 検証を通ったのに別ファイルを読む余地があった (pv Lv3 の棚卸し担当が指摘)。
  r.cmd = r.cmd.split('__INPUT_0__').join(_normalized[0] || '')
  r.cmd = r.cmd.split('__INPUT_1__').join(_normalized[1] || _normalized[0] || '')
}

phase('Review')

const reviews = await parallel(reviewers.map((r) => () =>
  agent(
    `あなたは外部レビュアー「${r.name}」を実行し、その出力を構造化レビュー結果に変換する担当です。

[手順]
1. Bash tool を timeout=${r.timeout} (ミリ秒) で使って次のコマンドを実行する:
${r.cmd}

2. 標準出力の全文を Write tool で C:/tmp-ai/cgd_raw_${r.name}_${label}_${_runTag}.md に保存する (人が生ログを検証できるように・これは必須)。
3. 出力を読み、指摘を findings 配列に構造化する:
   - severity: 🔴 (重大: セキュリティ/データ破壊/公開API逸脱/明白な論理バグ/integrationバグ) / 🟠 (重要) / 🟡 (注意)
   - title / location (file:line) / rationale (根拠1行) / recommended_fix
   - 重要: ${r.name} が **実際に挙げた severity をそのまま尊重** する。あなたが勝手に格上げ/格下げしない。
4. ${r.usage ? 'stderr の [DS Usage] / [Qwen Usage] / [Gemini Usage] の「今回:」行を usage_line に転記する。' : 'usage_line は空文字 ("")。'}
5. 認証エラー (${r.authSignals}) を検出したら auth_error=true、findings は空配列。それ以外は auth_error=false。
6. reviewer フィールドに "${r.name}" を入れる。

[重要]
- 最終出力 (schema JSON) だけが親に返る。生レビュー文を return に含めない (構造化 findings に圧縮)。
- **コマンドが起動できない/非0終了/タイムアウトした場合は executed=false と exit_code を必ず入れる。**
  auth 判定を試み、不能なら auth_error=false で findings に 🟠「${r.name} 実行失敗」を1件入れる。
  **正常に完走した場合だけ executed=true にする。** 出力が空でも「指摘なし」と executed=false は別物。
- 指摘が0件でも findings は空配列で返す（executed の値で成否を伝える）。
${r.isCodex ? '- あなた (Codex) は sandbox read-only で対象ファイルを読めるが、**追加で開くのは最大5ファイルまで**。関連関数の抜粋は入力に同梱済みなので、まずそれで判断すること（探索は1回約3,000トークン消費する）。' : ''}

JSON で返す。`,
    { label: `review:${r.name}`, phase: 'Review', schema: FINDING_SCHEMA }
  )
))

// ---- 認証エラー / 欠員チェック (Lv7 はその回の参加者全員 + Codex 両方成功が必須) ----
const ok = reviews.filter(Boolean)
const authFailed = ok.filter((r) => r.auth_error).map((r) => r.reviewer)
if (authFailed.length > 0) {
  log(`[review] 認証エラー検出: ${authFailed.join(', ')} → Lv7 中断`)
  return { halt: 'auth_error', failed: authFailed, message: `認証エラー: ${authFailed.join(', ')}。復旧後に再実行。` }
}
// **実行できたか**を findings とは独立に検査する (2026-08-11 追加)。
// これが無いと、タイムアウト/hang/ゲート deny で死んだレビュアーが
// {auth_error:false, findings:[]} を返して全ガードを通過し、
// 統合表に「指摘なし」と出て "レビュー済み" に見えてしまう。
const notExecuted = ok.filter((r) => r.executed !== true)
  .map((r) => `${r.reviewer}(exit=${r.exit_code === undefined ? '?' : r.exit_code})`)
if (notExecuted.length > 0) {
  log(`[review] 実行できていないレビュアー: ${notExecuted.join(', ')} → 中断`)
  return {
    halt: 'exec_failed',
    failed: notExecuted,
    message: `次のレビュアーはコマンドを完走できていません: ${notExecuted.join(', ')}。`
      + ' 生ログ (C:/tmp-ai/cgd_raw_*.md) を確認して原因を除いてから再実行してください。'
      + ' 指摘0件と実行失敗は別物なので、ここでは統合表を作りません。',
  }
}
const codexOk = ok.filter((r) => (r.reviewer === 'codex_med' || r.reviewer === 'codex_high') && r.executed === true).length
if (codexOk < 2) {
  log(`Codex 多重が片肺 (${codexOk}/2) → Lv7 中断 (Codex med+high 両方が本質)`)
  return { halt: 'codex_incomplete', got: codexOk, message: 'Lv7 は Codex medium と high の両方成功が必須です。' }
}
if (ok.length < reviewers.length) {
  log(`[review] レビュアー欠員: ${ok.length}/${reviewers.length} のみ成功 → Lv7 中断`)
  return { halt: 'incomplete', got: ok.length, expected: reviewers.length, message: `Lv7 は参加者${reviewers.length}者全員の成功が必要です。` }
}

phase('Merge')

const MERGE_SCHEMA = {
  type: 'object',
  properties: {
    table_md: { type: 'string', description: '統合表 (markdown)' },
    convergent_findings: {
      type: 'array',
      description: 'Codex med と high の両方が挙げた指摘 (最強の収束シグナル)',
      items: {
        type: 'object',
        properties: {
          severity: { type: 'string' },
          title: { type: 'string' },
          also_agreed_by: { type: 'array', items: { type: 'string' }, description: '補助(DS/Qwen、オプトイン時はGeminiも)で同調した者' },
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
      description: '補助(DS/Qwen、オプトイン時はGeminiも)のみが挙げた指摘 (Codex多重の盲点候補・要吟味)',
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

const reviewerNames = reviewers.map((r) => r.name)
const displayNames = { codex_med: 'Codex(med)', codex_high: 'Codex(high)', gemini: 'Gemini', deepseek: 'DS', qwen: 'Qwen' }
const tableColumns = ['指摘 (🔴/🟠/🟡 + 根拠1行)', ...reviewerNames.map((n) => displayNames[n] || n), '採用判断']

const mergePrompt =
  `${reviewerNames.length}者のコードレビュー結果を統合してください。Lv7 は Codex を medium + high で多重化した「Codex 集中」構成です。

[各レビュアーの findings]
${JSON.stringify(ok.map((r) => ({ reviewer: r.reviewer, findings: r.findings })), null, 2)}

[タスク]
1. 同一の指摘を突き合わせる (location + 内容の類似で対応付け)。
2. Codex 多重シグナルを判定:
   - convergent_findings: codex_med と codex_high の **両方** が挙げた指摘 = 最強の収束シグナル。補助で同調した者を also_agreed_by に。
   - codex_divergent_findings: codex_med か codex_high の **片方のみ**。high 単独は深掘り発見、med 単独は過剰反応の可能性として source を明記。
   - aux_only_findings: 補助 (DS/Qwen、オプトイン時はGeminiも) のみが挙げ Codex 両方とも挙げていない指摘 = Codex 多重の盲点候補。
3. 統合表 table_md を markdown で作成。列は必ずこの順番・この列名にする: ${JSON.stringify(tableColumns)}
   各列は ✅ / ❌ / 🔄 を記入。
4. next_actions: 実装すべき項目 (severity 高い順)。
5. summary: 総評 1-3行。

[重要 — over-attribution 禁止]
- 各 reviewer が実際に挙げた severity を尊重し、**あなたが勝手に severity を格上げしない**。
  (例: DS が 🟠「可能性」と書いたものを 🔴「真バグ」に格上げしない)
- 「複数者が一致」と書くのは、本当にその者の findings に該当指摘が存在する場合のみ。
- 各指摘の根拠は raw_log_path (C:/tmp-ai/cgd_raw_*.md) にあり、主 context で後から検証できることを前提に、確信度を誇張しない。

JSON で返す。`

let merged = await agent(mergePrompt,
  { label: 'merge', phase: 'Merge', schema: MERGE_SCHEMA, model: MERGE_MODEL }
)

// 取りまとめ(判断)は Claude の最上位モデルで行う。障害時のみ 1 回だけ代替で再試行する。
// 「どちらで統合したか」だけでなく **primary が落ちた事実** も戻り値に残す
// (used だけだと fallback が発火したこと自体が消える / pv と同じ設計)。
let mergeModelUsed = MERGE_MODEL
let mergeFallbackFired = false
if (!merged) {
  mergeFallbackFired = true
  log(`[merge] 取りまとめ (${MERGE_MODEL}) が失敗したため ${MERGE_FALLBACK} で再試行します`)
  merged = await agent(mergePrompt, { label: 'merge-fallback', phase: 'Merge', schema: MERGE_SCHEMA, model: MERGE_FALLBACK })
  mergeModelUsed = MERGE_FALLBACK
}
if (!merged) {
  return { halt: 'merge_failed', tried: [MERGE_MODEL, MERGE_FALLBACK], label }
}

return {
  level: 7,
  label,
  include_gemini: includeGemini,
  participants: reviewerNames,
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
