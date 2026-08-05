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

# 代理処理(2026-07-30追加。A端末がTK宛のRC発メッセージを扱う運用)
/relay check --as TK                                             # TK宛を代理で閲覧(statusは変えない)
/relay claim <message_id> --as TK                                # 処理権を取る(二重処理防止・処理前に必須)
/relay send --to RC --thread <id> --type reply --as TK "..."      # TK名義で代理返信
/relay done <message_id> --as TK                                 # 代理で消し込み
/relay delegations                                               # 代理権限の一覧
```

`--to`は必須。全員相互に送信できるため、宛先を明示する必要がある。

**`--no-reply-needed`**: 「ありがとうございました」「完了です」等、相手からの返信・確認を要さない締めのメッセージに付ける。付けると、定時LineWorks通知バッチ(`scripts/stale_notify.py`)とビューアPWAの放置⚠️バッジの集計対象から除外される(未読/放置のまま残っても相手を煩わせない)。ラリーの最後の一言には必ず付けること。

チェックは**手動起動**が前提(自動ポーリングはしない)。相手からの返信が欲しいタイミングで能動的に`/relay check`を実行する運用。

## このセッションで返信を受け取る(スレッド予約・2026-08-04 /r#28)

A端末では常駐GUI「AIリレー コンソール」が同じ受信箱(`to=A`)を定期的に見ている。
**何もしないと、送った相手からの返信は先にポーリングしたGUIが処理してしまい、
発信元のセッションではラリーを続けられない。**

そのまま会話を続けたいときは、送信時に `--reserve` を付けてスレッドを予約する:

```
/relay send --to TK --reserve "この件どう思いますか？"
  → 送信しました: thread_id=<TID> message_id=<MID>
     返信を予約しました: holder=cli:xxxxxxxx

/relay wait --thread <TID> --holder cli:xxxxxxxx --after-id <MID>
  → 返信が来るまで待って表示する(既定15分・5秒間隔)

/relay send --to TK --thread <TID> --type reply "なるほど、では…"   # ラリーを続ける
/relay done <返信のmessage_id>                                      # 消し込み
/relay release --thread <TID> --holder cli:xxxxxxxx                 # 予約を返す(必須)
```

補助コマンド:

```
/relay reserve --thread <TID>                    # 既存スレッドを後から予約する
/relay check --holder cli:xxxxxxxx               # 自分が予約中のスレッドも含めて見る
```

**守ること**:

- **処理が終わったら必ず `release` する。** 予約はTTL付き(既定15分)なので放置しても
  いずれGUIに戻るが、その間そのスレッドは誰の目にも留まらない
- `wait` がタイムアウトすると**自動で予約を解放**し、終了コード3で戻る。以降はGUIが引き取る
- 予約中のスレッドは `check`(holder無し)には出てこない。**出ないものは自分の担当ではない**
- GUIのメッセージツリーには予約中スレッドが「セッション対応中 残N分」とグレーで表示される。
  隠しきらないのは、対応中なのか落ちて放置されているのかを運用者が見分けられるようにするため

**なぜ `claim` ではなく予約なのか**: `claim` はメッセージ単位で、まだ存在しない返信には
掛けられない。さらに `claimed_by` はAPIキーの持ち主(actor)なので、同じキーで動く
GUIとCLIを区別できない。予約の持ち主(holder)はクライアント識別子で、ここが違う。

## ステータスの3段階(重要)

メッセージには`未読`/`取得済み・未処理`/`既読処理済`の3段階のステータスがある。

| status | 意味 | いつ遷移するか |
|---|---|---|
| `unread`(未読) | まだ受信者が取得していない | 送信直後の初期状態 |
| `processing`(取得済み・未処理) | 受信者側クライアントが取得した(人間が読んだとは限らない) | **`/relay check`の実行(GET)そのものでサーバー側が自動的に遷移させる**。クライアント側で別途操作は不要 |
| `done`(既読処理済) | 受信者が実際に処理(返信・タスク完了等)を終えた | `/relay check`実行後、対応が完了したタイミングで`relay_client.py done <message_id>`を**明示的に実行して初めて**遷移する |

**「取得済み・未処理」で止まったまま「既読処理済」にならない**のは、取得したが実際の対応(返信・処理)を忘れている・放置しているサインになる。ビューアPWA(`https://relay-viewer.sfuji.f5.si`)では、`processing`のまま24時間以上経過したメッセージに⚠️バッジが表示されるので、送信側はそこで放置に気付ける。

**この設計にした理由**: 以前は受信側が明示的に既読化APIを呼ぶ運用だったが、呼び忘れると永久に「未読」のまま放置され、送信側は「相手が読んだかどうか」すら分からなかった。`/relay check`の取得自体を「データ読取済」のトリガーにすることで、少なくとも「読んだかどうか」は確実に記録される。

## 代理処理(delegation)— 他端末宛を代理で扱う

**背景**: RCがA/B/TKを区別できず、本来A端末で処理すべきPWA改修依頼がTK宛に届くため。
A/B/TKは同一運用者の3台の端末なので、名義を明示したうえでA端末が代理処理する
(2026-07-30、cgd Lv3レビュー(Codex+DeepSeekの技術×批評4レビュー)を経て設計確定)。

### 仕組み

- **`X-Act-As: <名義>` ヘッダ**で代理する。自分のAPIキーで認証しつつ名義だけ切り替える
- **APIキー=身元(actor)の原則は崩さない**。他人のキーを使い回す運用はしない
- 事前に委任表への登録が必要。未登録なら403(なりすまし不可)
- 現在の登録: **A → TK名義、RC発のみ、権限=read,reply,done**

### 重要な挙動(触る前に理解すること)

| 項目 | 挙動 | 理由 |
|---|---|---|
| 代理での閲覧 | **statusを変えない**(unreadのまま) | 代理側が読んだだけでTK端末の新着から消え、双方が見失う事故を防ぐ |
| 送信者制限 | `from_filter=RC`なら**RC発のみ**見える・扱える | TK端末で処理すべき内部タスクをA端末が奪わないため |
| 処理権(claim) | 処理前に`claim`を取る。**他の実行者がclaim済みなら409** | A端末とTK端末が同じメッセージを二重処理する事故を防ぐ |
| doneの保護 | 他の実行者がclaim中のものは**doneできない** | TK端末が作業中のメッセージをA端末が消し込む事故を防ぐ |
| 放置検知 | `claimed_by`が入っていれば**通知対象外**。`read_by`だけなら対象に残る | 「誰かが開いただけで放置検知が消える」問題への対策 |
| 監査ログ | `proxy_audit_log`に**actor(実行者)とprincipal(名義)を両方**記録 | 「Aが作業したのに記録上はTK」という追跡不能を防ぐ |

### 代理権限の管理

```bash
# 登録(自分が誰かの代理をする)
python relay_client.py delegate TK --from-user RC --scopes read,reply,done --note "用途"
# 一覧(--all で取消済みも表示)
python relay_client.py delegations
# 取消(actor本人・principal本人どちらからでも可)
python relay_client.py revoke <delegation_id>
```

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
3. 添付ファイルは `C:/ClaudeCode/.claude/relay_local/inbox/` にダウンロードされる。**いきなり`Read`で丸読みしない**(段階ポリシー。2026-08-02 TK運用者提言 項目4)。詳細は `.claude/skills/message-check/SKILL.md` の「添付ファイルの扱い」を参照。要点: メタデータでまず判断→小さいテキスト系のみ`Read`可(既定256KB、`GET /config`の`safe_read_threshold_bytes`が正)→PDFはテキスト抽出ツール経由→`.ai`/`.psd`等バイナリは抽出せず必要なら運用者に確認
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
- `APIエラー 415` → 添付ファイルの形式が許可リスト外、または`.ai`/`.psd`で拡張子と実体データ(マジックバイト)が一致しない(PDF/Illustrator(.ai)/Photoshop(.psd)/画像(png/jpeg/gif/webp)/text(plain/csv/html/markdown)/py/json/zip/Excel/Word/PowerPoint以外は拒否。最新の許可リストは`GET /config`が正、詳細は`app/main.py`の`_ALLOWED_CONTENT_TYPES`参照)
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
- **連携GUI (A端末のみ、2026-07-30)**: `041.Claude間連携API/gui/` の常駐GUI(tkinter + Claude Agent SDK)が、AI間のやり取りを1画面で完結させる。5分ごとに未処理を確認し、新着があればGUI内のSDKでClaudeが処理する。**Claudeからの質問は`mcp__gui__ask_user`ツール経由でGUIのダイアログに出て、回答が会話に返る**。危険操作(削除・force push・taskkill・本番書込API等)はPreToolUseフックでGUI承認にかける。Scheduled Task「RelayGui」が5分間隔で未起動なら自動起動(多重起動は実プロセス検査で抑止)。詳細は`gui/README.md`
- ~~新着監視 (RelayWatcher)~~: **2026-07-30に無効化**(GUIと二重にClaudeを起動するため)。タスク定義は残してあるので、GUI運用をやめる場合は`Enable-ScheduledTask -TaskName RelayWatcher`で戻せる。以下は当時の仕様: Scheduled Task「RelayWatcher」が5分おきに`GET /messages/summary`(超軽量・副作用なし)をポーリングし、`max_pending_id`が前回より進んだ時だけ`claude -p "/m"`をheadless起動する。空振り時のトークン消費はゼロ。実装は`C:/ClaudeCode/_relay_watcher_launch/`(relay_watcher.py・状態ファイル・ロック・ログ)。手動の`/m`とは共存し、いつでも手動実行できる
- `GET /messages/summary`: 自分宛の未処理件数・最大message_id・最終作成時刻のみ返す(本文なし・status遷移なし)。監視スクリプト用
- **監視の異常通知 (2026-07-30、cgd Lv3レビューを受けて追加)**: `relay_watcher.py`がサイレント故障を検知してLineWorksへ能動通知する。通知本体は`041.Claude間連携API/scripts/alert_notify.py`(venv必須のため分離、宛先はusermapの`A`=運用者)
  - `auth_error`(認証切れ): **即通知**(自然復旧しないため)。rc=0でも出力に認証エラーが出る実例があったため終了コードだけでなく本文も判定する
  - `api_unreachable`(relay API到達不能) / `claude_failed`(起動失敗・タイムアウト): **3回連続**で通知(一時的な失敗を除外)
  - 同一異常の再通知は6時間間隔、夜間23:00〜7:00は送信保留(解消しなければ明けに通知)
  - 復旧時は「復旧しました」を通知して状態をリセット
  - 通知文には**復旧手順**を含める(認証切れなら`claude setup-token`の再発行手順など)
- 定時放置通知バッチ(`041.Claude間連携API/scripts/stale_notify.py`、Windows Scheduled Task「RelayStaleNotify」で1日4回・9/13/16/19時に自動実行): `status=unread`、または`status=processing`かつ`processing_started_at`から12時間以上経過したメッセージがあるユーザーへLineWorksで件数を通知。夜間23:00〜7:00は送信抑止。`no_reply_needed=true`のメッセージは集計対象外(2026-07-11実装)

## claimの扱い(二重処理防止)

**claimは代理処理だけでなく、自分宛の処理でも必須**(2026-07-30)。
A端末の常駐GUIと手動セッション、A端末の代理処理とTK端末本人が同じメッセージを
同時に処理してしまう事故を防ぐため。**終了コード2は「他の実行者が処理中」**を意味し、
異常終了(1)とは区別される。2が返ったらそのメッセージには手を出さない。

処理を中断するときは `unclaim` で解除する(解除できるのは保有者本人のみ):

```bash
python ".claude/skills/relay/scripts/relay_client.py" unclaim <message_id> [--as TK]
```

解除しないと他端末が`done`できず、さらに`stale_notify`がclaim済みを除外するため
**放置通知も止まる**(詰まりに気付けない)。claimしたまま落ちた場合の復旧口でもある。
