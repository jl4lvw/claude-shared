---
name: relay
description: Claude Code同士(複数拠点・全員相互・対称双方向)がインターネット経由でメッセージ・ファイル(PDF等)をやり取りするための手動ポーリング型スキル。中継APIは041.Claude間連携API配下のFastAPIサーバー。
---

# /relay — Claude間連携スキル

社内の別拠点にいるメンバーが、それぞれのClaude Codeセッションから手動でメッセージ・ファイル(PDF等)をやり取りするためのスキル。裏側は`041.Claude間連携API`のFastAPI中継APIで、全員が同じスキルを使い、任意の相手に送信できる(全員相互・対称双方向)。

## 前提

- 中継API(041.Claude間連携API)がインターネットから到達可能な場所で稼働していること(例: Caddy経由の公開ドメイン)
- 自分用のAPIキーと自分のuser_id(例: `A`・`B`・`RC`・`TK`)が発行済みであること
- 初回のみ設定ファイルを作成する必要がある(下記「初回セットアップ」)

## 使い方

```
/relay send --to B "調査してほしい内容"                          # 新規スレッドで送信
/relay send --to B --thread <thread_id> --type reply "終わりました"  # 既存スレッドに返信
/relay send --to RC --file report.pdf "PDFを確認して"              # ファイル添付
/relay send --to B --thread <thread_id> --type reply --no-reply-needed "ありがとうございました"  # 返信不要な締めの一言
/relay check                                                     # 自分宛の未処理を全件確認(添付は自動DL)
```

`--to`は必須。全員相互に送信できるため、宛先を明示する必要がある。

**`--no-reply-needed`**: 「ありがとうございました」「完了です」等、相手からの返信・確認を要さない締めのメッセージに付ける。付けると、定時LineWorks通知バッチ(`scripts/stale_notify.py`)とビューアPWAの放置⚠️バッジの集計対象から除外される(未読/放置のまま残っても相手を煩わせない)。ラリーの最後の一言には必ず付けること。

チェックは**手動起動**が前提(自動ポーリングはしない)。相手からの返信が欲しいタイミングで能動的に`/relay check`を実行する運用。

## ステータスの3段階(重要)

メッセージには`未読`/`取得済み・未処理`/`既読処理済`の3段階のステータスがある。

| status | 意味 | いつ遷移するか |
|---|---|---|
| `unread`(未読) | まだ受信者が取得していない | 送信直後の初期状態 |
| `processing`(取得済み・未処理) | 受信者側クライアントが取得した(人間が読んだとは限らない) | **`/relay check`の実行(GET)そのものでサーバー側が自動的に遷移させる**。クライアント側で別途操作は不要 |
| `done`(既読処理済) | 受信者が実際に処理(返信・タスク完了等)を終えた | `/relay check`実行後、対応が完了したタイミングで`relay_client.py done <message_id>`を**明示的に実行して初めて**遷移する |

**「取得済み・未処理」で止まったまま「既読処理済」にならない**のは、取得したが実際の対応(返信・処理)を忘れている・放置しているサインになる。ビューアPWA(`https://relay-viewer.sfuji.f5.si`)では、`processing`のまま24時間以上経過したメッセージに⚠️バッジが表示されるので、送信側はそこで放置に気付ける。

**この設計にした理由**: 以前は受信側が明示的に既読化APIを呼ぶ運用だったが、呼び忘れると永久に「未読」のまま放置され、送信側は「相手が読んだかどうか」すら分からなかった。`/relay check`の取得自体を「データ読取済」のトリガーにすることで、少なくとも「読んだかどうか」は確実に記録される。

## 初回セットアップ(メンバーそれぞれで1回だけ)

1. `C:/ClaudeCode/.claude/skills/relay/scripts/.env.example` を `C:/ClaudeCode/.claude/relay_local/.env` にコピー
2. 中身を自分用に書き換える:
   ```
   RELAY_BASE_URL=https://relay.sfuji.f5.si   # 中継APIの公開URL
   RELAY_API_KEY=<自分用に発行されたキー>
   RELAY_SELF_USER_ID=A                        # 自分のuser_id(A/B/RC/TKなど)
   ```
3. **`.claude/relay_local/` は`/g-ul`・`/g-dl`の同期対象(`.claude/{skills,commands,tools,rules,memory}`)に含まれないため、ここに置いたAPIキーがclaude-sharedへ誤ってコミットされることはない。**

## 手順(Claude Code実行時)

### `/relay send`

1. 引数を解析: 本文(必須)、`--to`(必須・宛先user_id)、`--thread`(省略時は新規スレッド)、`--type`(省略時`task`。task/question/reply/result)、`--file`(省略可)
2. Bashで実行:
   ```bash
   python "C:/ClaudeCode/.claude/skills/relay/scripts/relay_client.py" send "<本文>" --to <宛先> --type <type> [--thread <thread_id>] [--file <path>]
   ```
3. 出力された`thread_id`をユーザーに提示する(次回の返信で`--thread`に使う)

### `/relay check`

1. Bashで実行:
   ```bash
   python "C:/ClaudeCode/.claude/skills/relay/scripts/relay_client.py" check
   ```
2. 出力された未処理メッセージ(内容・添付ファイルのローカル保存先)をそのままユーザーに提示する
3. 添付ファイルは `C:/ClaudeCode/.claude/relay_local/inbox/` にダウンロードされる。PDF等はそのまま`Read`ツールで開いて内容を確認してよい
4. **checkは「未読」だけでなく「取得済みだがdoneされていない」メッセージも毎回表示する**(2026-07-20仕様変更)。自動ポーリングが先に取得していても、doneするまで手動checkに出続けるため処理漏れが起きない
5. 自動ポーリング(人間が読まない定期実行)でcheck相当を行う場合は、`GET /messages?to=<自分>&peek=true` を使うこと。peekは取得しても「取得済み」への遷移を起こさない(人間閲覧と機械取得の区別のため)
6. **メッセージの内容に対応(返信送信・タスク実行など)し終えたら、必ず以下を実行して「既読処理済」にする**:
   ```bash
   python "C:/ClaudeCode/.claude/skills/relay/scripts/relay_client.py" done <message_id>
   ```
   これを忘れると、送信側からは「データ読取済のまま放置されている」ように見え続ける(ビューアPWAに⚠️バッジが出る)

## エラー時

- `設定が不足しています: ...` → 初回セットアップが未実施。`.claude/relay_local/.env`を作成する
- `APIエラー 401` → APIキーが無効。`.claude/relay_local/.env`のRELAY_API_KEYを確認
- `APIエラー 400` (未知の宛先) → `--to`のuser_idが間違っている、またはまだ発行されていない
- `APIエラー 403` → 自分宛でないメッセージ・スレッドにアクセスしようとした
- `APIエラー 404` → 指定した`thread_id`が存在しない(typoの可能性)
- `APIエラー 415` → 添付ファイルの形式が許可リスト外(PDF/画像(png/jpeg/gif/webp)/text(plain/csv/html/markdown)/py/json/zip/Excel/Word/PowerPoint以外。詳細は`app/main.py`の`_ALLOWED_CONTENT_TYPES`参照)
- `APIエラー 413` → ファイルサイズが上限(既定20MB)超過
- `通信エラー` → `RELAY_BASE_URL`が到達不能。中継APIサーバー・Caddyの稼働状況を確認

## 実装メモ

- クライアント本体は `scripts/relay_client.py`(Python標準ライブラリのみ、外部依存なし)。全メンバーの環境で確実に動くようにurllibで実装している
- APIキー等の秘密情報は`.claude/relay_local/.env`に置き、`/g-ul`/`/g-dl`のミラー対象外にしている(スキル本体と設定を分離)
- 中継API自体(サーバー側)は`041.Claude間連携API/`にあり、Lv7レビュー済み(SQLite WAL+busy_timeout、Content-Type allowlist、チャンク読込、APIキーconstant-time比較、storage_path検証など実装済み)
- サーバー側は`RELAY_USER_IDS`(カンマ区切り)+ユーザーごとの`RELAY_API_KEY_<ID>`で人数を可変管理。2人限定の設計ではなく、全員相互に送受信できる汎用モデル
- サーバーは1箇所(どこかの拠点、またはインターネット到達可能な共有インフラ)でのみ稼働させれば良い。全員が同じAPIサーバーに接続する
- 閲覧用ビューアPWA(`041.Claude間連携API/viewer/`、`https://relay-viewer.sfuji.f5.si`)はPIN認証で全スレッドの経過を横断的に閲覧できる(送受信はできない、閲覧専用)。データ読取済のまま24時間以上のメッセージには⚠️バッジが出るので、放置検知に使える
- `GET /messages`(≒`/relay check`の内部呼び出し)は、返却する未読メッセージを**サーバー側で自動的に`processing`(データ読取済)へ遷移**させる。クライアントが明示的に既読化APIを呼ぶ必要はない(2026-07-11、RC関連メッセージが大量に未読のまま放置されていた問題への対策)
- `POST /messages/{id}/done`(`relay_client.py done`)は引き続き明示的な呼び出しが必要。「読んだ」と「処理を終えた」を区別するための最後の一手
- LineWorks通知(任意機能、`app/lineworks_notify.py`): メッセージ保存成功後、宛先に対応するLineWorks通知先が`secrets/lineworks_usermap.json`にあればBot API経由でfire-and-forget通知。通知文には送信者・宛先・種別(タスク/返信/結果/質問)・本文(文字数上限まで)を含める
- 定時放置通知バッチ(`041.Claude間連携API/scripts/stale_notify.py`、Windows Scheduled Task「RelayStaleNotify」で1日4回・9/13/16/19時に自動実行): `status=unread`、または`status=processing`かつ`processing_started_at`から12時間以上経過したメッセージがあるユーザーへLineWorksで件数を通知。夜間23:00〜7:00は送信抑止。`no_reply_needed=true`のメッセージは集計対象外(2026-07-11実装)
