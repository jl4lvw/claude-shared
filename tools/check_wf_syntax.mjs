/**
 * check_wf_syntax — Workflow スクリプトの構文を「実際に評価して」検査する.
 *
 * なぜ node --check ではダメか (2026-08-11 実測):
 *   cgd_lv8_review.js に `String(p).replace(/\/g, '/')` という壊れた正規表現が
 *   入った状態で `node --check cgd_lv8_review.js` は **exit 0 を返した**。
 *   同じ内容を切り出して単体でチェックすると
 *   `SyntaxError: missing ) after argument list` で落ちる。
 *   WF スクリプトはトップレベル await / return を含むため node が CJS ラッパーで
 *   包んで評価を諦める経路があるらしく、**構文ゲートとして信用できない**。
 *
 *   このセッションではその偽 OK を信じて壊れたファイルを 3 本作ってしまった。
 *   「実行した」ではなく「結果を確かめる」(AGENTS.md) の典型例。
 *
 * ここでは Workflow ランタイムと同じ形 —— `new Function('args', ..., 'return (async () => { ... })()')`
 * —— で **関数として構築**する。構築時点で構文エラーは必ず投げられる。
 * 本体は実行しない（agent を呼ばないので課金も副作用も無い）。
 *
 * 使い方:
 *     node .claude/tools/check_wf_syntax.mjs                    # cgd の WF 3 本
 *     node .claude/tools/check_wf_syntax.mjs path/to/script.js  # 個別指定
 *
 * 終了コード: 0 = 全部 OK / 1 = 構文エラーあり / 2 = 読めないファイルのみ
 *   (構文エラーと読取不能が混在した場合は 1 を優先する。直すべきは構文の方なので)
 */

import fs from 'fs'
import path from 'path'
import { fileURLToPath } from 'url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const WF_DIR = path.resolve(HERE, '..', 'skills', 'cgd', 'workflows')
const DEFAULTS = ['cgd_lv6_review.js', 'cgd_lv7_review.js', 'cgd_lv8_review.js']
  .map((f) => path.join(WF_DIR, f))

const targets = process.argv.slice(2)
const files = targets.length > 0 ? targets : DEFAULTS

let bad = 0
let unreadable = 0

for (const file of files) {
  let src
  try {
    src = fs.readFileSync(file, 'utf8')
  } catch (err) {
    console.error(`[wf-syntax] 読めません: ${file} (${err.message})`)
    unreadable += 1
    continue
  }

  // meta の export を外す（Workflow ランタイムも同様に扱う）
  // 行頭の `export const meta` だけを落とす。他の export 形式まで巻き込むと
  // 別物を構文チェックしてしまうので、行頭 + 語境界で限定する。
  const body = src.replace(/^export\s+const\s+meta\s*=/m, 'const meta =')
  try {
    // eslint-disable-next-line no-new-func
    new Function('args', 'log', 'phase', 'agent', 'parallel', 'budget', 'workflow',
      `return (async () => { ${body} })()`)
    console.log(`[wf-syntax] OK   ${file}`)
  } catch (err) {
    console.error(`[wf-syntax] NG   ${file}`)
    console.error(`             ${err.name}: ${err.message}`)
    bad += 1
  }
}

if (unreadable > 0 && bad === 0) process.exitCode = 2
else process.exitCode = bad > 0 ? 1 : 0
