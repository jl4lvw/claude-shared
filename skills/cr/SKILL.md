---
name: cr
description: Codex 自動点検（codex-review）の指摘を一覧・確認し、採用／却下を判定表へ記録する。「/cr」「点検の指摘」「Codex の指摘を見せて」「指摘一覧」で使う。Claude は報告の本文を読まず、findings.py の出力をそのまま伝える。
---
<!-- SKILL_VERSION: 2026-10-25_123000 -->

# cr — Codex 自動点検の指摘を見る・判定する

設計: `C:\ClaudeCode\.codex-review\DESIGN.md` §13。報告は TS の共有（`\\192.168.1.130\codex-review$\reports\`）、つながらないときは端末 A の `.codex-review\reports\` を読む。

## 手順
引数をそのまま次のコマンドに対応させて **1 回だけ**実行し、標準出力を**そのまま**（表は本文に貼る）返す。説明・要約を足さない。

| 入力 | 実行 |
|---|---|
| `/cr` | `python C:/ClaudeCode/.codex-review/findings.py list` |
| `/cr 全部` | `python C:/ClaudeCode/.codex-review/findings.py list --all` |
| `/cr <ID>` | `python C:/ClaudeCode/.codex-review/findings.py show <ID>` |
| `/cr <ID> 採用` | `python C:/ClaudeCode/.codex-review/findings.py decide <ID> adopt` |
| `/cr <ID> 却下 <理由>` | `python C:/ClaudeCode/.codex-review/findings.py decide <ID> reject --reason "<理由>"` |

- ID は `1025-011-001` の形（一覧の ID 列）。行番号ではない
- 却下は理由が必須。無ければ実行せず「却下の理由を書いてください」とだけ返す
- 採用・却下は運用者が打ったときだけ実行する。Claude が自分で判定しない

## してはいけないこと
- 報告ファイル（`reports\...\*.md`）の本文を Read しない・要約しない（Claude の枠を使わないため）。中身を見たいと言われたら `show` のフルパスを示す
- 報告ファイルと `decisions.md` を直接編集しない（記録は `decide` だけ）
- 採用した指摘の実装は、このスキルではしない。運用者が報告のフルパスを Codex デスクトップ（または Claude）に渡す

## 終了コード
0 = 成功 / 1 = 入力の誤り（ID が無い・理由が無い）→ 出力をそのまま返す / 2 = 報告の置き場に接続できない → 出力をそのまま返す
