---
name: cr
description: Codex 自動点検（codex-review）の指摘を一覧・確認し、採用／却下を判定表へ記録する。「/cr」「点検の指摘」「Codex の指摘を見せて」「指摘一覧」で使う。Claude は報告の本文を読まず、findings.py の出力をそのまま伝える。
---
<!-- SKILL_VERSION: 2026-10-28_185000 -->

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
| `/cr <ID> 却下 <種類> <理由>` | `python C:/ClaudeCode/.codex-review/findings.py decide <ID> reject --type <種類> --reason "<理由>"` |
| `/cr 記録` / `/cr 記録 <フォルダ番号 or YYYYMMDD>` | `python C:/ClaudeCode/.codex-review/findings.py record [<対象>]` |
| `/cr 評価` / `/cr 評価 先週` | `python C:/ClaudeCode/.codex-review/findings.py evaluate [先週]` |
| `/cr 内訳` / `/cr 内訳 先週` / `/cr 内訳 全期間` | `python C:/ClaudeCode/.codex-review/findings.py 内訳 [先週 または 全期間]`（トークンの使い道を ①採用・②不採用・③有用・④無駄・判定待ちに分けた棒グラフ。設計書 §19） |
| `/cr 観点` | `python C:/ClaudeCode/.codex-review/findings.py 観点`（観点の配分＝CPU・メモリの過剰消費の洗い出しに回したトークンの実績と目標。設計書 §20） |
| `/cr 見逃し <フォルダ> <内容>` | `python C:/ClaudeCode/.codex-review/findings.py miss <フォルダ> "<内容>"` |

- ID は `1025-011-001` の形（一覧の ID 列）。行番号ではない
- 却下は種類（`誤り` / `既知` / `軽微` / `範囲外`）と理由が必須。無ければ実行せず「却下の種類（誤り・既知・軽微・範囲外）と理由を書いてください」とだけ返す
- サブコマンド名・引数は `findings.py --help` が正。食い違ったら `--help` に従う
- 採用・却下は運用者が打ったときだけ実行する。Claude が自分で判定しない

## してはいけないこと
- 報告ファイル（`reports\...\*.md`）の本文を Read しない・要約しない（Claude の枠を使わないため）。中身を見たいと言われたら `show` のフルパスを示す
- 報告ファイルと `decisions.md` を直接編集しない（記録は `decide` だけ）
- 採用した指摘の実装は、このスキルではしない。運用者が報告のフルパスを Codex デスクトップ（または Claude）に渡す

## 指摘の再チェック・改善を頼まれたとき（2026-10-25 運用者決定）
「必要か確かめて直して」など、指摘の改善を頼まれたら、**いつもサブエージェント（Agent ツール）で行う**。このセッションで直接直さない。サブセッション（start_session）も使わない（スマホからは始められず、子が止まっても気づけないため）。
1. `show <ID>` で報告のフルパスを取り、分野の近い指摘を 2 件ずつ組にしてサブエージェントへ並行で渡す（報告の本文はサブエージェントが読む）
2. 依頼文に必ず書く: 今のコードで本当に起きるか確かめてから判定する（点検はコミット済みの版を見るので、未コミットの修正で直っていることがある）／判定は 必要・不要(誤り|既知|軽微|範囲外)・要相談／安全装置を緩める・新しい API を公開する修正は「要相談」で止める／編集前に `.bak_YYYYMMDD_HHMMSS`／自分の変更以外に触れない／コミット・再起動・本番 API・送信・承認の操作はしない／テストはタイムアウト付きで本番に届かないよう塞ぐ
3. 戻った結果は、修正した差分とテストを Claude が自分で確かめてから、判定の一覧（ID・判定・内容）を本文の表で見せる
4. コミット・再起動・`decide` の記録は、運用者の承認を受けてから行う

## 終了コード
0 = 成功 / 1 = 入力の誤り（ID が無い・理由が無い）→ 出力をそのまま返す / 2 = 報告の置き場に接続できない → 出力をそのまま返す
