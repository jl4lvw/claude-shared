---
description: Discord 通知の ON/OFF を切替えるスキル。ON 時は Stop hook が Claude のユーザー向け応答を Discord に転送し、`prompt_yesno` で Discord 経由の Yes/No 応答も受け取れる。`/d` 単独 = トグル / `/d on` / `/d off` / `/d alias <name>` / `/d send <text>` / `/d yesno <q>`。
---

# /d — Discord 通知トグルスキル

`/d` ON 時に、Claude のユーザー向けメッセージを Discord に自動転送する。
Discord からは ボタン または `r: 1`(=ok) / `r: 2`(=ng) / `r: ok` / `r: ng` テキストで Yes/No を返せる。

複数 PC で同時に `/d` を回している場合に備えて、メッセージ先頭に **セッション略称（≤10 文字）** を付与する。

---

## ▶ 起動とサブコマンド

| 入力 | 動作 |
|---|---|
| `/d` | ON/OFF をトグル |
| `/d on` | 明示的に ON |
| `/d off` | 明示的に OFF |
| `/d status` | 現在の ON/OFF と alias を表示 |
| `/d alias <name>` | セッション略称を設定（英数/_/- のみ、≤10 文字、超過分は切り捨て、不正文字は除去） |
| `/d alias` | alias を auto（ホスト名由来）に戻す |
| `/d send <text>` | 任意テキストを Discord に送信（force=True、ON/OFF と関係なく送る） |
| `/d yesno <question>` | Discord に Yes/No プロンプトを出して応答を待つ |

---

## ▶ 状態管理

- 状態は `.handoff/d_state.json`（OneDrive 同期）に **HOST_ID 単位** で保存
- ON/OFF と alias は PC ごとに独立
- ⚠️ 複数 PC が同時に状態書き込みすると片方の更新が消える可能性あり（CAS 未実装）

---

## ▶ Stop hook による自動転送

`/d` ON 時、Claude Code の Stop hook (`d_stop_hook.py`) がターン終了時に発火し、
Claude の最後の **user-visible テキスト** を Discord に転送する。

転送対象:
- `text` ブロック（ユーザーに見える応答本文）

転送しない:
- `thinking` ブロック（思考中の内部テキスト）
- `tool_use` / `tool_result` ブロック（ツール呼び出しと結果）

---

## ▶ Discord 双方向リファレンス

| 場面 | ボタン | テキスト |
|---|---|---|
| Yes/No プロンプト | `[1: はい]` `[2: いいえ]` | `r: 1` / `r: 2` / `r: ok` / `r: ng` |

複数 PC で同時待機している場合は末尾に `@HOST-ID` を付けて振り分け可能（既存 `/r` と同じ）。

---

## ▶ 動作手順

### Step 1: 引数を判別

ユーザー入力 `/d <args>` の `<args>` を以下に分岐:
- 空 → `discord_d.py` を引数なしで実行（toggle）
- `on` / `off` / `status` / `alias [name]` / `send <text>` / `yesno <q>` → そのまま渡す
- 上記以外 → エラー表示して終了

### Step 2: discord_d.py CLI を呼び出す

Bash ツールで以下を実行（プロジェクトルートで）:

```bash
python "006.secretary/scripts/discord_d.py" <args>
```

サブコマンドの戻り値:
- `0` 成功
- `1` 送信失敗
- `2` 引数エラー

### Step 3: 結果をユーザーに表示

CLI の標準出力をそのままユーザーに見せる。toggle / on / off の場合は新しい状態（ON/OFF + alias）を確認させる。

---

## ⚠️ 運用ルール

1. **alias の安全化**: `@everyone` / 改行 / Markdown 記号は自動除去（Discord での mention spam 防止）
2. **mention 抑止**: `discord_notify.py` で `allowed_mentions: {"parse": []}` を送信ペイロードに必ず含める。Stop hook で assistant 応答を流す際も mention は無効化される
3. **Stop hook の失敗は無視**: hook 内で例外が起きても exit 0 で終了し、Claude の停止は妨げない
4. **OneDrive 競合**: 複数 PC が同じ瞬間に `d_state.json` を書き換えると片方の更新が失われる可能性。常時 3 台以上で運用するなら CAS 実装を検討

---

## 関連ファイル

| ファイル | 役割 |
|---|---|
| `006.secretary/scripts/discord_d.py` | `/d` の本体 CLI とヘルパー関数（is_on / send / prompt_yesno） |
| `006.secretary/scripts/d_stop_hook.py` | Claude Code の Stop hook 用スクリプト |
| `006.secretary/scripts/discord_notify.py` | Webhook 送信。`allowed_mentions: {"parse": []}` 付き |
| `006.secretary/scripts/discord_outbox.py` | ボタン付きプロンプト依頼（既存） |
| `006.secretary/scripts/discord_inbox.py` | 応答ポーリング（既存、prompt_id 対応済み） |
| `001.Python/100.Discord/r_inbox.py` | Bot 側テキスト応答ハンドラ。VERB_MAP に `1`→`ok`, `2`→`ng` を追加済み |
| `.handoff/d_state.json` | 状態ファイル（OneDrive 同期、HOST_ID 単位） |
| `.claude/settings.local.json` | Stop hook 登録 |
