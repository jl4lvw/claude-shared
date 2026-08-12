/**
 * dump_wf_raw_paths — WF が「生ログをここに書け」と指示しているパスを取り出す.
 *
 * なぜ必要か:
 *   cgd_plan.py は「WF はこのパスに生ログを書くはず」を **予測** して collect の
 *   判定に使う。予測式 (`cgd_raw_<reviewer>_<label>_<run_tag>.md`) が WF 側と
 *   ずれると、**成功した run でも「生ログが存在しない」と誤報する**。
 *   誤報する検査は「無視してよい警告」になり、いずれ本当の失敗も見逃す。
 *
 *   そこで両者を突き合わせる契約テストを置く。ここは LLM を通さず、
 *   WF スクリプトを評価して agent に渡るプロンプト文字列から実際のパスを拾う。
 *
 * 使い方:
 *     node dump_wf_raw_paths.mjs <script.js> <argsJson>
 *     → {"paths": {"<reviewer>": "<path>", ...}}
 *
 * agent は呼ばない（スタブで打ち切る）ので課金も副作用も無い。
 */

import fs from 'fs'
import path from 'path'

const WF_DIR = path.resolve(path.dirname(new URL(import.meta.url).pathname.slice(1)),
  '..', 'skills', 'cgd', 'workflows')

const file = process.argv[2]
const argsJson = process.argv[3] || '{}'
if (!file) {
  console.error('usage: node dump_wf_raw_paths.mjs <script.js> <argsJson>')
  process.exit(2)
}

const full = path.isAbsolute(file) ? file : path.join(WF_DIR, file)
const src = fs.readFileSync(full, 'utf8').replace(/^export const meta/m, 'const meta')

// Preflight は「渡された引数の数だけ返す」実物と同じ挙動にする。
// sha256 は run_tag の元になるので、呼び出し側が指定できるようにする。
const parsed = JSON.parse(argsJson)
const SHA = parsed._sha256 || 'a'.repeat(64)
delete parsed._sha256

const prompts = []
let calls = 0
const DONE = '__DONE__'

const agent = async (prompt, opts) => {
  prompts.push(String(prompt ?? ''))
  calls += 1
  if (calls === 1) {
    const m = String(prompt).match(/preflight_inputs\.py"?([^\n]*)/)
    const paths = m ? (m[1].match(/"([^"]+)"/g) || []).map((s) => s.slice(1, -1)) : []
    return {
      files_json: JSON.stringify({
        files: paths.map((p) => ({
          path: p, exists: true, is_file: true, readable: true, bytes: 1234,
          mtime: '2026-08-12T00:00:00+09:00', mtime_ns: 1, sha256: SHA, head: 'ok',
        })),
      }),
      gate_json: '{"armed":false,"count":0,"gates":[]}',
    }
  }
  throw new Error(DONE)   // Review 段に入ったら打ち切る
}

const fn = new Function('args', 'log', 'phase', 'agent', 'parallel',
  `return (async () => { ${src} })()`)

try {
  await fn(JSON.stringify(parsed), () => {}, () => {}, agent,
    async (thunks) => Promise.all(thunks.map((t) => t())))
} catch (err) {
  if (!String(err && err.message).includes(DONE)) {
    console.error(`[dump] 実行に失敗: ${err && err.message}`)
    process.exit(1)
  }
}

// Review 段のプロンプトから「reviewer 名」と「生ログの保存先」を拾う。
const out = {}
for (const p of prompts.slice(1)) {
  const name = (p.match(/reviewer フィールドに "([^"]+)"/)
    || p.match(/外部レビュアー「([^」]+)」/) || [])[1]
  const raw = (p.match(/(C:\/tmp-ai\/cgd_raw_[^\s"'`]+\.md)/) || [])[1]
  if (name && raw) out[name] = raw
}

if (Object.keys(out).length === 0) {
  console.error('[dump] 生ログのパスを 1 件も拾えませんでした（プロンプトの書式が変わった可能性）')
  process.exit(1)
}
console.log(JSON.stringify({ paths: out }, null, 1))
