/**
 * wf_guard_test — cgd の WF スクリプト (lv6/lv7/lv8) の入力ガードを検証する.
 *
 * 実 Workflow を起動せずに検証するため、agent/log/phase/parallel をスタブして
 * スクリプト本体を評価し、返ってくる halt の種類を確認する。
 * Codex/DS/Qwen への課金は一切発生しない。
 *
 * 使い方:
 *     node .claude/tools/wf_guard_test.mjs              # 全レベル × 全ケース
 *     node .claude/tools/wf_guard_test.mjs cgd_lv8_review.js
 *     node .claude/tools/wf_guard_test.mjs cgd_lv8_review.js path_mismatch
 *
 * 終了コード: 0 = 全件期待どおり / 1 = 1 件でも不一致
 *
 * 前身は %TEMP% に置いていたが消えるので、2026-08-11 にリポジトリへ移した。
 */

import fs from 'fs'
import path from 'path'

const WF_DIR = 'C:/ClaudeCode/.claude/skills/cgd/workflows'
const LEVELS = ['cgd_lv6_review.js', 'cgd_lv7_review.js', 'cgd_lv8_review.js']

// Preflight を通過したら投げる番兵。ガードが「正しい入力を通す」ことの確認に使う。
const PASSED = '__PASSED_PREFLIGHT__'

function loadScript(file) {
  const src = fs.readFileSync(path.join(WF_DIR, file), 'utf8')
  return src.replace(/^export const meta/m, 'const meta')
}

/** スクリプト本体を評価する。agentImpl は呼び出し回数つきで差し替える。
 *
 * args は **既定で JSON 文字列として渡す**。本番の Workflow ツールは args を
 * JSON 文字列で渡すので、オブジェクトを直渡ししていた旧版はスクリプト冒頭の
 * `if (typeof _args === 'string') JSON.parse(...)` を一度も通っていなかった。
 * その行を壊しても全件グリーンのままになる偽の安全網だった (2026-08-11)。
 */
async function run(file, args, agentImpl, { asString = true } = {}) {
  const src = loadScript(file)
  const stubs = {
    log: () => {},
    phase: () => {},
    parallel: async (thunks) => Promise.all(thunks.map((t) => t())),
    agent: (...a) => agentImpl(...a),
  }
  const fn = new Function('args', 'log', 'phase', 'agent', 'parallel',
    `return (async () => { ${src} })()`)
  const payload = asString && typeof args !== 'string' ? JSON.stringify(args) : args
  return fn(payload, stubs.log, stubs.phase, stubs.agent, stubs.parallel)
}

/** lv6 は入力 1 本、lv7/lv8 は 2 本（この非対称は仕様）。 */
const isLv6 = (file) => file.includes('lv6')
const targetsFor = (file, args) =>
  isLv6(file) ? [args.input_path] : [args.input_path, args.aux_input_path]

const fileEntry = (p, over = {}) => ({
  path: p, exists: true, is_file: true, readable: true, bytes: 1234,
  mtime: '2026-08-11T12:26:00+09:00', mtime_ns: 1, sha256: 'a'.repeat(64), head: 'ok', ...over,
})

const NO_GATE = '{"armed":false,"count":0,"gates":[]}'

const preflight = (filesJson, gateJson = NO_GATE) =>
  async () => ({ files_json: filesJson, gate_json: gateJson })

/** 実物の preflight_inputs.py と同じ挙動 —— **渡された引数の数だけ**エントリを返す。
 *
 * 固定の files 配列を返すスタブだと、WF が同じパスを 2 回渡しても 1 件しか
 * 返らない世界をテストすることになる。実際は 2 件返るので「重複」と誤診される。
 * 2026-08-11 にその乖離のせいで、重複畳み込みを消してもテストが緑のままだった。
 * ここではエージェントに渡されたプロンプトから実際のコマンド引数を読み取る。
 */
const preflightEcho = (gateJson = NO_GATE, over = {}) => async (prompt) => {
  const m = String(prompt).match(/preflight_inputs\.py"?([^\n]*)/)
  const paths = m ? (m[1].match(/"([^"]+)"/g) || []).map((s) => s.slice(1, -1)) : []
  return {
    files_json: JSON.stringify({ files: paths.map((p) => fileEntry(p, over)) }),
    gate_json: gateJson,
  }
}

const okFiles = (file, args, over = {}) =>
  JSON.stringify({ files: targetsFor(file, args).map((p) => fileEntry(p, over)) })

const GOOD = { input_path: 'C:/tmp-ai/a.txt', aux_input_path: 'C:/tmp-ai/b.txt', label: 'x', wf_nonce: 'n' }

/** Review 段のプロンプトから reviewer 名を拾う（本物の agent と同じ振る舞いを模す）。 */
const reviewerOf = (prompt) => {
  const m = String(prompt).match(/reviewer フィールドに "([^"]+)"/)
    || String(prompt).match(/外部レビュアー「([^」]+)」/)
  return m ? m[1] : 'unknown'
}

// expect は文字列、またはレベル別の差分を持つオブジェクト。
// undefined = halt せず Preflight を通過する想定。
const CASES = {
  // --- A: 引数ガード（Preflight 前に落ちる） ---
  wrong_keys: {
    args: { codex_file: 'C:/tmp-ai/a.txt', aux_file: 'C:/tmp-ai/b.txt', label: 'x' },
    expect: 'missing_args',
  },
  partial: {
    // lv6 は input_path だけで充足するので Preflight に進み、そこで nonce 無しでも
    // ゲート未設定なら通過する。lv7/lv8 は aux_input_path 必須なので missing_args。
    args: { input_path: 'C:/tmp-ai/a.txt', label: 'x', wf_nonce: 'n' },
    expect: { default: 'missing_args', lv6: undefined },
    agent: (file, args) => preflight(okFiles(file, args)),
  },
  empty: {
    args: { input_path: '', aux_input_path: '', label: 'x' },
    expect: 'missing_args',
  },
  nonce_omitted_but_gate_off: {
    // ゲートが張られていなければ nonce 無しでも進んでよい。
    args: { input_path: 'C:/tmp-ai/a.txt', aux_input_path: 'C:/tmp-ai/b.txt', label: 'x' },
    expect: undefined,
    agent: (file, args) => preflight(okFiles(file, args)),
  },

  // --- 正常系 ---
  ok: {
    args: GOOD,
    expect: undefined,
    agent: (file, args) => preflight(okFiles(file, args)),
  },

  // --- B: 実在・型のガード ---
  missing_file: {
    args: GOOD,
    expect: 'input_missing',
    agent: (file, args) => preflight(okFiles(file, args, { exists: false, is_file: false, bytes: 0 })),
  },
  empty_file: {
    args: GOOD,
    expect: 'input_missing',
    agent: (file, args) => preflight(okFiles(file, args, { bytes: 0 })),
  },
  negative_bytes: {
    // `!f.bytes` の falsy 判定では通ってしまっていたケース。
    args: GOOD,
    expect: 'input_missing',
    agent: (file, args) => preflight(okFiles(file, args, { bytes: -1 })),
  },
  string_bytes: {
    args: GOOD,
    expect: 'input_missing',
    agent: (file, args) => preflight(okFiles(file, args, { bytes: '1234' })),
  },
  directory: {
    args: GOOD,
    expect: 'input_missing',
    agent: (file, args) => preflight(okFiles(file, args, { is_file: false })),
  },
  unreadable: {
    // 存在する通常ファイルだが読めない（権限など）。ツール側は readable=false + error を返す。
    args: GOOD,
    expect: 'input_missing',
    agent: (file, args) => preflight(okFiles(file, args, {
      readable: false, sha256: '', head: '', error: '読取失敗: Permission denied',
    })),
  },
  has_error_field: {
    // readable=true でも error が載っていたら信用しない。
    args: GOOD,
    expect: 'input_missing',
    agent: (file, args) => preflight(okFiles(file, args, { error: '想定外の例外: RuntimeError: x' })),
  },

  // --- 🔴 パス一致のガード（旧実装はここが素通りだった） ---
  path_mismatch: {
    // 件数もサイズも合っているが、**別のファイル**を見ている。
    args: GOOD,
    expect: 'input_missing',
    agent: (file, args) => preflight(JSON.stringify({
      files: targetsFor(file, args).map(() => fileEntry('C:/tmp-ai/lv8_codex_input.txt')),
    })),
  },
  duplicate: {
    // 同じファイルが 2 件。件数比較だけだと充足して見える。
    // レベルに依らず必ず重複させる（lv6 は入力 1 本なので map では重複にならない）。
    args: GOOD,
    expect: 'input_missing',
    agent: (_file, args) => preflight(JSON.stringify({
      files: [fileEntry(args.input_path), fileEntry(args.input_path)],
    })),
  },
  duplicate_with_both_present: {
    // lv7/lv8 向け: 要求した 2 本が **両方揃った上で** 片方が重複している。
    // 上の duplicate は「b が欠けている」でも halt するため、lv7/lv8 の
    // 重複検出そのものを消してもグリーンのままだった (2026-08-11 の監査 tests 観点)。
    args: GOOD,
    expect: { default: 'input_missing', lv6: 'input_missing' },
    agent: (file, args) => preflight(JSON.stringify({
      files: isLv6(file)
        ? [fileEntry(args.input_path), fileEntry(args.input_path)]
        : [fileEntry(args.input_path), fileEntry(args.aux_input_path), fileEntry(args.aux_input_path)],
    })),
  },
  extra_file: {
    // 要求していないファイルが混ざっている。
    args: GOOD,
    expect: 'input_missing',
    agent: (file, args) => preflight(JSON.stringify({
      files: [...targetsFor(file, args).map((p) => fileEntry(p)), fileEntry('C:/tmp-ai/zzz.txt')],
    })),
  },
  case_and_sep: {
    // 大文字小文字・区切り文字の違いは同一視する（Windows なので通ってよい）。
    args: GOOD,
    expect: undefined,
    agent: (file, args) => preflight(JSON.stringify({
      files: targetsFor(file, args).map((p) => fileEntry(p.replace(/\//g, '\\').toUpperCase())),
    })),
  },

  // --- 構造が壊れている場合は fail-closed ---
  unparsable: {
    args: GOOD,
    expect: 'preflight_unparsable',
    agent: () => preflight('Traceback (most recent call last): ...'),
  },
  files_not_array: {
    args: GOOD,
    expect: 'preflight_unparsable',
    agent: () => preflight('{"files": "nope"}'),
  },
  gate_unparsable: {
    args: GOOD,
    expect: 'gate_unknown',
    agent: (file, args) => preflight(okFiles(file, args), '[cgd wf-gate] 未設定'),
  },
  gate_not_boolean: {
    args: GOOD,
    expect: 'gate_unknown',
    agent: (file, args) => preflight(okFiles(file, args), '{"armed":"yes"}'),
  },

  // --- ゲート有効時の nonce ---
  nonce_missing: {
    // ゲートは有効だが nonce が取り出せない（gates が空）→ 停止する。
    args: { input_path: 'C:/tmp-ai/a.txt', aux_input_path: 'C:/tmp-ai/b.txt', label: 'x' },
    expect: 'nonce_missing',
    agent: (file, args) => preflight(okFiles(file, args), '{"armed":true,"count":1,"gates":[]}'),
  },
  nonce_self_fetched: {
    // args に wf_nonce が無くても status --json から自分で取れれば進む (2026-08-11)。
    args: { input_path: 'C:/tmp-ai/a.txt', aux_input_path: 'C:/tmp-ai/b.txt', label: 'x' },
    expect: undefined,
    agent: (file, args) => preflight(
      okFiles(file, args),
      '{"armed":true,"count":1,"gates":[{"key":"s","corrupt":false,"level":8,"nonce":"deadbeef"}]}',
    ),
    // halt しないことだけ見ていては不十分。**取得した nonce が実際にコマンドへ
    // 差し込まれたか**を見る。置換ループを消しても緑のままだった (2026-08-11)。
    assert: ({ reviewPrompts }) => {
      const joined = reviewPrompts.join('\n')
      if (joined.includes('__WF_NONCE__')) {
        throw new Error('プレースホルダ __WF_NONCE__ が置換されずに残っている')
      }
      if (!joined.includes('CGD_WF_RUN=deadbeef')) {
        throw new Error('自己取得した nonce がコマンドに反映されていない')
      }
    },
  },
  gate_ambiguous: {
    // ゲートが複数あるときは nonce を選ばず止まる (他セッションの nonce を掴むと全 deny)。
    args: { input_path: 'C:/tmp-ai/a.txt', aux_input_path: 'C:/tmp-ai/b.txt', label: 'x' },
    expect: 'gate_ambiguous',
    agent: (file, args) => preflight(okFiles(file, args), JSON.stringify({
      armed: true, count: 2,
      gates: [{ key: 'a', corrupt: false, level: 8, nonce: 'aaaa' },
              { key: 'b', corrupt: false, level: 7, nonce: 'bbbb' }],
    })),
  },
  gate_corrupt: {
    args: { input_path: 'C:/tmp-ai/a.txt', aux_input_path: 'C:/tmp-ai/b.txt', label: 'x' },
    expect: 'gate_ambiguous',
    agent: (file, args) => preflight(okFiles(file, args), JSON.stringify({
      armed: true, count: 1,
      gates: [{ key: 'a', corrupt: true, level: null, nonce: null }],
    })),
  },
  explicit_nonce_wins_over_ambiguity: {
    // args で明示されていれば曖昧でも進める（明示は上書き手段として残す）。
    args: { input_path: 'C:/tmp-ai/a.txt', aux_input_path: 'C:/tmp-ai/b.txt', label: 'x', wf_nonce: 'mine' },
    expect: undefined,
    agent: (file, args) => preflight(okFiles(file, args), JSON.stringify({
      armed: true, count: 2,
      gates: [{ key: 'a', corrupt: false, level: 8, nonce: 'aaaa' },
              { key: 'b', corrupt: false, level: 7, nonce: 'bbbb' }],
    })),
    assert: ({ reviewPrompts }) => {
      const joined = reviewPrompts.join('\n')
      if (!joined.includes('CGD_WF_RUN=mine')) throw new Error('明示 nonce が使われていない')
      if (joined.includes('CGD_WF_RUN=aaaa')) throw new Error('ゲート側の nonce を誤って採用している')
    },
  },

  same_input_and_aux: {
    // input_path と aux_input_path が同じでも通す（1 本に畳む）。
    // 畳まないと 1:1 照合が「2 件重複」と誤診して必ず halt していた (2026-08-11 実害)。
    args: { input_path: 'C:/tmp-ai/a.txt', aux_input_path: 'C:/tmp-ai/a.txt', label: 'x' },
    expect: undefined,
    // 実物と同じく「渡された引数の数だけ返す」スタブを使う。
    // 畳み込みを消すと 2 件返り、1:1 照合が重複を検出して halt する。
    agent: () => preflightEcho(),
  },

  // --- パスの形式（相対パスと区切り文字） ---
  relative_path: {
    // 相対パスは Preflight agent と codex で cwd が違い、別ファイルを見得る。
    args: { input_path: 'tmp-ai/a.txt', aux_input_path: 'C:/tmp-ai/b.txt', label: 'x' },
    expect: 'relative_path',
  },
  relative_path_both: {
    args: { input_path: './a.txt', aux_input_path: '../b.txt', label: 'x' },
    expect: 'relative_path',
  },
  reviewer_uses_validated_path: {
    // reviewer コマンドには **検証を通した(正規化済みの)** パスが入ること。
    // 生の args を埋め込んでいた版では、バックスラッシュ入りパスで
    // 「検証した文字列」と「実行に使う文字列」が別物になっていた。
    args: {
      input_path: 'C:\\tmp-ai\\aa.txt', aux_input_path: 'C:\\tmp-ai\\bb.txt',
      label: 'x', wf_nonce: 'n',
    },
    expect: undefined,
    agent: () => preflightEcho(),
    assert: ({ reviewPrompts }) => {
      const joined = reviewPrompts.join('\n')
      if (joined.includes('C:\\tmp-ai')) throw new Error('生のバックスラッシュ入りパスが埋め込まれている')
      if (!joined.includes('C:/tmp-ai/aa.txt')) throw new Error('正規化後のパスが使われていない')
      if (joined.includes('__INPUT_0__') || joined.includes('__INPUT_1__')) {
        throw new Error('入力プレースホルダが置換されていない')
      }
    },
  },
  unc_path_ok: {
    // UNC は //server/share/... の形なら通す（バックスラッシュは正規化する）。
    args: { input_path: '//srv/share/a.txt', aux_input_path: '\\\\srv\\share\\b.txt', label: 'x' },
    expect: undefined,
    agent: (file, args) => preflight(JSON.stringify({
      files: (file.includes('lv6') ? ['//srv/share/a.txt'] : ['//srv/share/a.txt', '//srv/share/b.txt'])
        .map((p) => fileEntry(p)),
    })),
    assert: ({ prompts }) => {
      // Preflight へ渡すコマンドもスラッシュに正規化されていること。
      if (prompts[0].includes('\\')) throw new Error('バックスラッシュが正規化されていない')
    },
  },

  unsafe_path_quote: {
    // パスにダブルクォートが入るとコマンド文字列が壊れる。埋め込む前に弾く。
    args: { input_path: 'C:/tmp-ai/a".txt', aux_input_path: 'C:/tmp-ai/b.txt', label: 'x' },
    expect: 'unsafe_path',
  },
  unsafe_path_subshell: {
    args: { input_path: 'C:/tmp-ai/$(whoami).txt', aux_input_path: 'C:/tmp-ai/b.txt', label: 'x' },
    expect: 'unsafe_path',
  },
  unsafe_path_semicolon: {
    args: { input_path: 'C:/tmp-ai/a.txt; rm -rf /', aux_input_path: 'C:/tmp-ai/b.txt', label: 'x' },
    expect: 'unsafe_path',
  },
  label_is_sanitized: {
    // label は生ログの保存先に埋め込まれる。`/` や `..` を通さない。
    args: {
      input_path: 'C:/tmp-ai/a.txt', aux_input_path: 'C:/tmp-ai/b.txt',
      label: '../../etc/passwd', wf_nonce: 'n',
    },
    expect: undefined,
    agent: () => preflightEcho(),
    assert: ({ reviewPrompts }) => {
      const joined = reviewPrompts.join('\n')
      if (joined.includes('../../etc/passwd')) throw new Error('label が素通りしている')
      if (!joined.includes('cgd_raw_')) throw new Error('生ログのパスが見当たらない')
    },
  },
  raw_log_has_run_fingerprint: {
    // 同じ label で並行実行しても生ログが上書きされないこと。
    args: { input_path: 'C:/tmp-ai/a.txt', aux_input_path: 'C:/tmp-ai/b.txt', label: 'same', wf_nonce: 'n' },
    expect: undefined,
    agent: () => preflightEcho(),
    assert: ({ reviewPrompts }) => {
      const joined = reviewPrompts.join('\n')
      // fileEntry の sha256 は 'a'.repeat(64) なので先頭 8 文字は aaaaaaaa
      if (!joined.includes('_same_aaaaaaaa.md')) {
        throw new Error('生ログ名に入力の指紋が入っていない')
      }
    },
  },
  dry_run_validates_inputs: {
    // dry_run でも入力の実在確認まで行う（通っただけで安心させない）。
    args: {
      input_path: 'C:/tmp-ai/a.txt', aux_input_path: 'C:/tmp-ai/b.txt',
      label: 'x', wf_nonce: 'n', dry_run: true,
    },
    expect: undefined,
    agent: () => preflightEcho(),
    assert: ({ prompts }) => {
      if (prompts.length !== 1) throw new Error('dry_run で Preflight が走っていない')
    },
  },
  dry_run_still_catches_missing_input: {
    args: {
      input_path: 'C:/tmp-ai/a.txt', aux_input_path: 'C:/tmp-ai/b.txt',
      label: 'x', wf_nonce: 'n', dry_run: true,
    },
    expect: 'input_missing',
    agent: (file, args) => preflight(okFiles(file, args, { exists: false, is_file: false, readable: false, bytes: 0 })),
  },

  // --- Review 段以降（欠員 / 認証 / 実行失敗 / 分類）---
  // ここは PASSED 番兵で必ず打ち切られており、3 レベルとも一度も動いていなかった。
  review_exec_failed: {
    args: { ...GOOD, label: 'x' },
    expect: 'exec_failed',
    agent: () => preflightEcho(),
    review: (prompt) => ({
      reviewer: reviewerOf(prompt), auth_error: false, findings: [],
      executed: false, exit_code: 1,
    }),
  },
  review_auth_error: {
    args: { ...GOOD, label: 'x' },
    expect: 'auth_error',
    agent: () => preflightEcho(),
    review: (prompt) => ({
      reviewer: reviewerOf(prompt), auth_error: true, findings: [], executed: true, exit_code: 0,
    }),
  },
  review_unknown_reviewer_name: {
    // reviewer 名が定義とずれたら黙って批評扱いにせず止まること（lv8 のみ該当）。
    // codex 群の名前はそのまま返す（先に codex_incomplete で止まってしまうため）。
    args: { ...GOOD, label: 'x' },
    expect: { default: undefined, lv8: 'unclassified_reviewer' },
    agent: () => preflightEcho(),
    review: (prompt) => {
      const name = reviewerOf(prompt)
      return {
        reviewer: name.indexOf('codex') === 0 ? name : 'mystery_reviewer',
        auth_error: false, findings: [], executed: true, exit_code: 0,
      }
    },
  },

  // --- args.reviewers（Python 側を単一の出所にする経路） ---
  prompt_from_args: {
    // build が生成した依頼テキストをそのまま使うこと（3 本の複製をやめるため）。
    args: {
      ...GOOD, label: 'x',
      reviewers: [{ name: 'r1', kind: 'tech', cmd: 'echo hi', timeout: 1000,
                    usage: false, isCodex: false, authSignals: 'a',
                    prompt: 'PROMPT-FROM-PYTHON' }],
    },
    expect: undefined,
    agent: () => preflightEcho(),
    assert: ({ reviewPrompts }) => {
      if (reviewPrompts[0] !== 'PROMPT-FROM-PYTHON') {
        throw new Error('args の prompt が使われていない: ' + reviewPrompts[0].slice(0, 60))
      }
    },
  },
  args_prompt_gets_nonce_substituted: {
    // **本番経路の再現 (2026-08-12 実走で判明した停止の原因)。**
    // Python が生成した prompt には、実行すべきコマンドが本文として埋め込まれている。
    // agent が実際に叩くのはこの prompt の中身であって r.cmd ではない。
    // 以前は r.cmd にしか nonce / 入力パスを差し込んでおらず、
    // **ゲートを張った本来の運用で** hook が codex を deny → 毎回 exec_failed で停止した。
    // 既存の nonce_self_fetched は内蔵文面しか見ていないため、この経路を素通りさせていた。
    args: {
      input_path: 'C:/tmp-ai/a.txt', aux_input_path: 'C:/tmp-ai/b.txt', label: 'x',
      reviewers: [{ name: 'r1', kind: 'tech', timeout: 1000, usage: false,
                    isCodex: true, authSignals: 'a',
                    cmd: 'CGD_WF_RUN=__WF_NONCE__ run __INPUT_0__',
                    prompt: '次を実行: CGD_WF_RUN=__WF_NONCE__ run __INPUT_0__' }],
    },
    expect: undefined,
    agent: () => preflightEcho(
      '{"armed":true,"count":1,"gates":[{"key":"s","corrupt":false,"level":8,"nonce":"deadbeef"}]}',
    ),
    assert: ({ reviewPrompts }) => {
      const p = reviewPrompts[0]
      if (p.includes('__WF_NONCE__')) throw new Error('prompt 側の __WF_NONCE__ が残っている')
      if (!p.includes('CGD_WF_RUN=deadbeef')) throw new Error('prompt に nonce が入っていない')
      if (p.includes('__INPUT_0__')) throw new Error('prompt 側の __INPUT_0__ が残っている')
      if (!p.includes('C:/tmp-ai/a.txt')) throw new Error('prompt に検証済みの入力パスが入っていない')
    },
  },
  prompt_absent_falls_back_to_builtin: {
    args: {
      ...GOOD, label: 'x',
      reviewers: [{ name: 'r1', kind: 'tech', cmd: 'echo hi', timeout: 1000,
                    usage: false, isCodex: false, authSignals: 'a' }],
    },
    expect: undefined,
    agent: () => preflightEcho(),
    assert: ({ reviewPrompts }) => {
      if (!reviewPrompts[0].includes('外部レビュアー')) {
        throw new Error('内蔵の文面に落ちていない')
      }
    },
  },
  reviewers_from_args: {
    // build が渡した定義をそのまま使うこと。
    args: {
      ...GOOD, label: 'x',
      reviewers: [{ name: 'only_one', kind: 'tech', cmd: 'echo hi',
                    timeout: 1000, usage: false, isCodex: false, authSignals: 'x' }],
    },
    expect: undefined,
    agent: () => preflightEcho(),
    assert: ({ opts, reviewPrompts }) => {
      const reviews = opts.filter((o) => o && String(o.label || '').startsWith('review:'))
      if (reviews.length !== 1) throw new Error(`args の定義が使われていない (${reviews.length} 者)`)
      if (!reviewPrompts.join('\n').includes('echo hi')) throw new Error('args の cmd が使われていない')
    },
  },
  reviewers_from_args_rejects_bad_shape: {
    // 形式が不正なら黙って内蔵定義に落ちず、止まること。
    args: { ...GOOD, label: 'x', reviewers: [{ name: 'x', cmd: 'y', timeout: 0 }] },
    expect: 'bad_reviewers',
    agent: () => preflightEcho(),
  },
  reviewers_absent_falls_back_to_builtin: {
    // build を経由しない起動を壊さない（後方互換）。
    args: { ...GOOD, label: 'x' },
    expect: undefined,
    agent: () => preflightEcho(),
    assert: ({ opts, file }) => {
      const reviews = opts.filter((o) => o && String(o.label || '').startsWith('review:'))
      const want = isLv6(file) ? 3 : (file.includes('lv7') ? 4 : 6)
      if (reviews.length !== want) throw new Error(`内蔵定義の人数が違う: ${reviews.length} != ${want}`)
    },
  },

  // --- 取りまとめ(判断)のモデル指定 ---
  merge_uses_top_model: {
    // オーケストレーション(指示と判断)は Claude の最上位モデルで行う。
    // 並列レビュアーは作業側なので対象外（既定モデルのまま）。
    args: { ...GOOD, label: 'x' },
    expect: undefined,
    agent: () => preflightEcho(),
    review: (prompt, calls, o) => {
      if (o && o.label === 'merge') {
        return { table_md: '|a|', tech_table_md: '|a|', critic_table_md: '|a|',
                 convergent_findings: [], divergent_findings: [], next_actions: [], summary: 'ok' }
      }
      return { reviewer: reviewerOf(prompt), auth_error: false, findings: [],
               executed: true, exit_code: 0 }
    },
    assert: ({ opts }) => {
      const merge = opts.find((o) => o && o.label === 'merge')
      if (!merge) throw new Error('merge agent が呼ばれていない')
      if (merge.model !== 'fable') throw new Error(`merge の model が fable でない: ${merge.model}`)
      const reviews = opts.filter((o) => o && String(o.label || '').startsWith('review:'))
      if (reviews.some((o) => o.model)) throw new Error('レビュアーにまでモデル指定が及んでいる')
    },
  },
  merge_falls_back_when_top_model_fails: {
    // 最上位モデルが落ちたら 1 回だけ代替で再試行する。
    args: { ...GOOD, label: 'x' },
    expect: undefined,
    agent: () => preflightEcho(),
    review: (prompt, calls, o) => {
      if (o && o.label === 'merge') return null            // 最上位モデルが失敗
      if (o && o.label === 'merge-fallback') {
        return { table_md: '|a|', tech_table_md: '|a|', critic_table_md: '|a|',
                 convergent_findings: [], divergent_findings: [], next_actions: [], summary: 'ok' }
      }
      return { reviewer: reviewerOf(prompt), auth_error: false, findings: [],
               executed: true, exit_code: 0 }
    },
    assert: ({ opts }) => {
      const fb = opts.find((o) => o && o.label === 'merge-fallback')
      if (!fb) throw new Error('fallback が呼ばれていない')
      if (fb.model !== 'opus') throw new Error(`fallback の model が opus でない: ${fb.model}`)
    },
  },

  // --- args の受け渡し形式（本番は JSON 文字列） ---
  args_broken_json: {
    // 壊れた JSON 文字列は {} に落ちて missing_args になるべき（例外で死なない）。
    args: '{oops',
    rawArgs: true,
    expect: 'missing_args',
  },
  args_as_object: {
    // 念のためオブジェクト直渡しでも動くこと（将来ランタイムが変わった場合の保険）。
    args: { input_path: 'C:/tmp-ai/a.txt', aux_input_path: 'C:/tmp-ai/b.txt', label: 'x' },
    rawArgs: true,
    expect: undefined,
    agent: (file, args) => preflight(okFiles(file, args)),
  },
}

function expectedFor(c, file) {
  if (c.expect && typeof c.expect === 'object') {
    const key = file.match(/lv(\d)/)[0]
    return key in c.expect ? c.expect[key] : c.expect.default
  }
  return c.expect
}

const [wantFile, wantCase] = process.argv.slice(2)
const files = wantFile ? [wantFile] : LEVELS
const cases = wantCase ? [wantCase] : Object.keys(CASES)

let failed = 0
for (const file of files) {
  for (const name of cases) {
    const c = CASES[name]
    if (!c) { console.log(`  ??   ${file} ${name}: 未知のケース`); failed++; continue }
    const expect = expectedFor(c, file)

    let calls = 0
    // agent に渡されたプロンプトを全部ためる。halt の有無だけを見ていると
    // 「Review 段に何を渡したか」が検証できず、nonce 置換を消しても緑になる。
    const prompts = []
    // agent の opts も記録する。model 指定 (取りまとめは最上位モデル) のような
    // 「呼び方」の退行は、halt もプロンプトも見ていない限り検出できない。
    const opts = []
    const inner = c.agent ? c.agent(file, c.args) : async () => ({})
    const agentImpl = async (...a) => {
      prompts.push(String(a[0] ?? ''))
      opts.push(a[1] || {})
      calls += 1
      if (calls === 1) return inner(...a)   // Preflight
      // c.review があるケースは Review 段も実際に走らせる。
      // 番兵で必ず打ち切っていた版では、欠員/認証/実行失敗の判定と Merge が
      // 3 レベルとも一度も実行されていなかった (2026-08-11 の監査 tests 観点)。
      if (c.review) return c.review(prompts[prompts.length - 1], calls, a[1] || {})
      // 2 回目以降 = Review 段に入った = ガードを通過した。
      throw new Error(PASSED)
    }

    let got
    try {
      const r = await run(file, c.args, agentImpl, { asString: c.rawArgs !== true })
      got = r && r.halt
    } catch (err) {
      got = String(err && err.message).includes(PASSED) ? undefined : `例外: ${err && err.message}`
    }

    let ok = got === expect
    let note = ''
    // 通過したケースは「何を Review 段へ渡したか」まで検査する。
    if (ok && c.assert) {
      try {
        c.assert({ prompts, reviewPrompts: prompts.slice(1), opts, file })
      } catch (err) {
        ok = false
        note = ` — ${err && err.message}`
      }
    }
    if (!ok) failed++
    const shown = (v) => (v === undefined ? '(通過)' : v)
    console.log(`  ${ok ? 'OK  ' : 'FAIL'} ${file.padEnd(20)} ${name.padEnd(24)} halt=${shown(got)} (期待 ${shown(expect)})${note}`)
  }
}

console.log(failed === 0 ? `\n全 ${files.length * cases.length} 件 OK` : `\n${failed} 件 FAIL`)
process.exitCode = failed === 0 ? 0 : 1
