---
name: remote-control
description: LINE WORKSのトーク(チャットルーム)経由でClaude Codeに指示を送り、Claude Codeが必要に応じて質問を返信して回答を待ち、指示+回答をもとに作業を継続する遠隔操作ループ。「リモートコントロール」「/remote-control」などで起動。1タスク=1トーク=1セッション。`/lineworks-check`(受信箱を人間が手動確認するだけのスキル)とは別系統で、こちらは受信→質問→待機→実行のループを自律的に回す。
---

# /remote-control — LINE WORKS経由の遠隔操作ループ

LINE WORKSの特定トーク(ルーム)に書かれた指示を受信し、必要なら確認質問をそのトークへ返信して回答を待ち、指示+回答の内容に従って作業を実行する。完了したら結果をそのトークへ通知して終了する。

**トークは使い回してよい。1つのタスクが続く限り同じトークを使い続けるのはもちろん、タスクが完了した後も同じトークを次のタスクに再利用できる**(2026-07-13実機検証を経てユーザーから指摘・設計変更。トーク作成→Bot招待→room_id確認、という準備の手間は一度で済ませ、以後は使い回すのが基本方針)。途中でClaude Codeのセッション(会話)が失われても(PC再起動・プロセス終了等)、同じ`room_id`で改めて`/loop /remote-control <room_id> <room_type>`を起動すれば、サーバー側に残っているセッション状態(`lineworks_sessions`)から同じセッションに復帰できる。

**ただし、待受ループが終了した後(`close`後)は、Claude Codeが改めて`/loop /remote-control`を起動するまで、そのトークに届くメッセージは誰にも処理されない。** これはスコープ上あえて解消していない制約(常駐監視は将来課題)。同じトークを使い回せることと、自動的に監視され続けることは別。次のタスクを依頼する際は、トークにメッセージを書いた上で、改めてClaude Codeから`/loop /remote-control`を起動する必要がある。

設計はcgd Lv2(Codex+Gemini)+critic評価、cgd Lv7(Codex medium+high多重+Gemini+DeepSeek+Qwen)レビュー×2回(ポーリング高速化・ルーム永続化)を経て確定している。技術的な選択理由は「サーバー側の実装メモ」を参照。

## 前提

- **このスキルはA専用**(サーバー側の`LINEWORKS_INBOX_OWNER = "A"`固定は意図した仕様)。TKなど他ユーザーへの展開は2026-07-13に検討したが、サーバー改修規模と運用の複雑化を踏まえて見送りが確定している
- 中継API(`041.Claude間連携API`)にセッションAPI(`POST /lineworks/sessions`等)が実装済み・稼働中であること
- LineWorks Developer ConsoleでBotのCallback URLが設定済み・Botが対象トークに参加していること(`/lineworks-check`と共通)
- `.claude/relay_local/.env` に `RELAY_BASE_URL` / `RELAY_API_KEY`(Aのキー)が設定済みであること
- `.claude/relay_local/lineworks_authorized_senders.json` に、指示発行を許可するLineWorks `sender_id` の配列が登録されていること(初期値は空配列`[]`。未登録の状態では**いかなる指示も無条件実行しない**)
- **トークはユーザーが手動で作成する**(自動作成はしない)。一度作成・確認済みのトークは繰り返し使ってよい。新しいトークを作る必要があるのは、まだ一度もBotとやり取りしたことがないroomを新設する時だけ

## 使い方

このスキルは**必ず `/loop` スキル経由(動的ペース)で起動する**。ScheduleWakeupによる待機ループは`/loop`の仕組みに依存しているため、単独で`/remote-control`を実行しても回答待ちの継続ができない。

```
/loop /remote-control <room_id_or_label> <room_type>
```

- `room_id_or_label`: LINE WORKSのchannelId(グループトーク)/userId(1:1トーク)、または`lineworks_known_rooms.json`に登録済みのラベル
- `room_type`: `channel` または `user`(登録済みラベルを使う場合は省略可。登録値と矛盾する値を指定するとエラーになる)
- **`room_id_or_label`が分からない/覚えていない場合は省略してよい。** `/loop /remote-control`(引数なし)や「選ばせて」で起動すると、手順0(クリック選択)に入る

**`room_id`/`sender_id`は必ず実際にWebhookで受信したメッセージから取得する(推測・流用しない)**。実機検証(2026-07-12)で判明した重要な注意点:
- LINE WORKSは「Bot APIでの送信先ID」(`lineworks_usermap.json`のaccountId形式、例: `xxx@works-xxxxx`)と「Webhookが報告する`source.userId`」(UUID形式、例: `a4653138-...`)が**別形式**。usermapの値をroom_id/sender_idとして流用すると一致しない
- 1:1トークでは`source`に`channelId`が無く`userId`のみのため、`room_id`と`sender_id`が同じ値になる(この場合のみ)。グループトークでは`room_id`(channelId)と`sender_id`(userId)は別の値になる
- `room_id`が分からない場合は、まず対象トークに何かメッセージを送ってもらい、`GET /lineworks/messages`(または`/lineworks-check`)で実際に届いた`room_id`/`sender_id`を確認する。ユーザーから口頭やチャットで伝えられたIDも、必ずこの方法で実受信と突き合わせて検証する

### `lineworks_known_rooms.json`(ルームのラベル管理・任意機能)

一度実受信で確認したroom_idは、生のUUIDを毎回扱わずに済むよう`.claude/relay_local/lineworks_known_rooms.json`にラベル登録できる:

```json
{
  "ラベル名": {"room_id": "実際に受信確認したUUID", "room_type": "channel", "note": "用途メモ"}
}
```

- 一覧確認: `python remote_control_client.py rooms-list`
- **登録・更新はClaudeが黙って行わず、必ずAskUserQuestionでユーザーへ明示確認してから書き込む**(`lineworks_authorized_senders.json`と同様、永続的な設定変更のため)
- 登録は**実際にWebhookで受信確認済みのroom_id**のみ許可する(ユーザーから口頭で伝えられただけのIDを鵜呑みにしない)
- ラベル文字列はUUID形式と紛らわしいものを避ける(英数字とハイフン程度を推奨)。既存ラベルの上書きは特に明示確認する
- ラベル解決は登録キーとの**完全一致でのみ**行う(「UUIDっぽいから room_id」といった形式判定はしない)。未登録の文字列はそのまま生のroom_idとして扱われる

## 手順(Claude Code実行時、`/loop`の1イテレーションごと)

### 0. ルームをクリックで選ぶ(`room_id_or_label`が省略された場合・必須手順)

`ARGUMENTS`に`room_id_or_label`が無い場合、または「選ばせて」「一覧から選びたい」等と
言われた場合は、IDの手入力を求めずクリック選択させる(2026-07-13ユーザー指摘:
「IDを記入するのが難しい」への対応。このスキルの正式な最初の手順として扱う)。

```bash
python remote_control_client.py rooms-discover
```

Botが過去に受信したことのある全roomが、`lineworks_known_rooms.json`のラベル(あれば)・
最終メッセージ日時・メッセージ件数付きで返る。これを**必ず`AskUserQuestion`の選択肢にして**
ユーザーにクリックで選んでもらう(ラベルがあればラベル名を、無ければ`room_id`の先頭数文字+
最終メッセージ日時を表示に使う)。**テキストで「どのroomですか?」と聞かない。**

選ばれたroomにラベルが無ければ、続けて「このroomにラベルを付けますか?」と
AskUserQuestionで確認し、OKならラベル名を聞いて`lineworks_known_rooms.json`に登録する
(登録手順は「`lineworks_known_rooms.json`」セクション参照)。

選択(または解決)できたら、`room_id`・`room_type`を持って手順1へ進む。

### 1. セッション開始(初回イテレーション、またはセッション復帰時)

```bash
python "C:/ClaudeCode/.claude/skills/remote-control/scripts/remote_control_client.py" session-start <room_id> <room_type>
```

返ってきた`id`(session_id)と`lease_token`を**会話内で覚えておく**(`/loop`は同一会話を再開するので、通常は以降のイテレーションでもそのまま使える。ただし会話が失われて改めて`/loop /remote-control`を起動した場合も、このコマンドがサーバー側の状態から正しく復帰させてくれる)。

`409エラー`が返る場合は、既に別プロセス(まだ生きている古いセッション)がこのroomを保持している。ユーザーに状況を伝えて指示を仰ぐ。

**返ってきた`status`で分岐する(必須)**:
- `active` → 通常の新規フロー(手順2へ)。この`active`は「新規作成」と「前タスクが`close`済みで、このroomで新しいセッションが作られた」の両方を含む
- `waiting_reply` → 前回の質問に対する回答待ちの状態から復帰したことを意味する。**新規指示探索(手順2〜6)は行わず、直ちに手順7(待機ループ)の先頭を実行する**(=まず`heartbeat`+`messages --after-id <waiting_since_message_id>`を確認する。復帰直後に回答や新しい指示が届いている可能性があるため、確認を省略しない)

(`done`/`expired`は`session-start`が対象を探すのが`active`/`waiting_reply`のセッションのみのため、通常はここに現れない。現れた場合は新規セッションが作られている)

**新規セッションなら「待受を開始しました」を必ず通知する(必須・省略禁止)。**
`created_at == updated_at`(=今まさに新規作成された。他プロセスからの引き継ぎではない)であれば、
手順2に進む前に`notify`でユーザーへ知らせる:

```bash
python remote_control_client.py notify <session_id> <lease_token> "このトークの待受を開始しました。ご依頼をお送りください。"
```

これが無いと、ユーザーは「投げたメッセージに反応があるか分からず不安」になる
(2026-07-13実機運用でユーザーから指摘)。`close`時に必ず「待受を終了した」旨を通知するのと対称の設計。
`created_at != updated_at`(=lease切れの既存セッションを引き継いだ)の場合はこの通知を送らない
(引き継ぎのたびに毎回通知すると煩雑になるため)。

レスポンスの`created_from_message_id`を**必ず控えておく**(手順2で使う)。

### 2. 未処理メッセージの確認

**ルーム永続利用化のため、必ず`--after-id <created_from_message_id>`を付けて呼ぶ(省略禁止)**:

```bash
python remote_control_client.py messages <room_id> --after-id <created_from_message_id>
```

このセッション作成より前のメッセージ(前タスク完了後の「了解」「ありがとう」等の相槌・雑談)を新タスクの指示として誤って拾わないための境界線(2026-07-13実機検証後のcgd Lv7レビューで、Codex medium/highが独立に発見した問題への対応)。`--after-id`を付け忘れると、無関係な過去の発言が指示として誤処理される事故になる。

メッセージが無ければ、5番(待機・次回起床のスケジュール)へ進む。

### 3. 送信者の認可チェック(必須・省略禁止)

`.claude/relay_local/lineworks_authorized_senders.json` を読み、メッセージの`sender_id`が含まれるか確認する。

- 含まれる場合 → 4へ進む
- **含まれない場合 → 無条件に指示として実行しない。** AskUserQuestionでユーザーに「未登録の送信者(sender_id=...)からの指示です。実行しますか?」と確認する(`/lineworks-check`/`message-check`の既存ルールに準拠)

### 4. owner確定・メッセージ処理済みマーク

```bash
python remote_control_client.py claim <session_id> <lease_token> <sender_id>
python remote_control_client.py done <message_id>
```

### 5. 指示内容の検討

- 指示が明確 → 8(作業実行)へ
- 不明点がある → 6(質問)へ

### 6. 質問の送信(不明点がある場合)

```bash
python remote_control_client.py ask <session_id> <lease_token> "<質問文>"
```

これにより`status`が`waiting_reply`になる。

### 7. 待機ループ(質問送信後、`status=waiting_reply`の間)

`/loop`の次のイテレーション、および手順1でセッション復帰した直後は、必ず以下を**先に**行う(新着確認を飛ばさない):

```bash
python remote_control_client.py heartbeat <session_id> <lease_token>
python remote_control_client.py messages <room_id> --after-id <waiting_since_message_id>
```

`waiting_since_message_id`は`heartbeat`のレスポンスに含まれる。

取得したメッセージのうち、以下**すべて**を満たす最初の1件を「回答」とみなす:
- **`sender_id`が、このセッションの`owner_sender_id`(`heartbeat`のレスポンスに含まれる)と一致すること**(3番のallowlistは指示発行時の認可判定に使うもので、回答判定は`claim`で確定した`owner_sender_id`固定で行う)
- text種別であること(スタンプ等はそもそも受信箱に保存されない)

該当メッセージがあれば`done`でマークし、8(作業実行)へ進む。

無ければ、`waiting_started_at`(最初にこの質問をした時刻。24h/72h判定の基準)と`waiting_since_asked_at`(直近のask/remind時刻。ポーリング間隔の基準)から、以下のように次の動作を決める:

**a. `waiting_started_at`からの経過が72時間以上**:

```bash
python remote_control_client.py notify <session_id> <lease_token> "72時間回答がなかったため、このタスクを終了しました。このトークでの待受は終了しています。自動監視はしていないため、次のご依頼はこのトークに書いた後、Claude Code側で改めて/loop /remote-controlを起動してください"
python remote_control_client.py close <session_id> <lease_token> expired
```

`/loop`のこのイテレーションを終了する(`ScheduleWakeup`を呼ばない)。

**b. `waiting_started_at`からの経過が24時間以上、かつ`reminder_sent_at`が未設定**:

```bash
python remote_control_client.py remind <session_id> <lease_token>
```

これにより元の質問文が「【リマインド】」付きで再送され、`waiting_since_asked_at`がリセットされる(`waiting_started_at`は変わらない=72h期限は延びない)。`reminder_sent_at`が既に設定済みの場合は409が返るので、そのまま次のcに進む。

**c. それ以外(通常の待機継続)**: `waiting_since_asked_at`からの経過時間で次回起床間隔を決め、`ScheduleWakeup`を呼ぶ(`/loop /remote-control <room_id> <room_type>`を`prompt`にそのまま渡す):

| `waiting_since_asked_at`からの経過 | 次回起床間隔 |
|---|---|
| 10分以内 | 60秒 |
| 10〜30分 | 180秒 |
| 30分以降 | 1200秒 |

(60秒起床はScheduleWakeupのガイダンスで「ハーネスが通知できない外部状態を能動的にポーリングする用途に適する」とされる範囲。人間の返信の多くはこの帯に集中する想定で、迅速な応答性を優先する。cgd Lv7レビュー: 現行270秒始まりは「チャットとして遅すぎる」というcritic評価を受けての改善)

### 8. 作業の実行

指示(+回答があれば回答)の内容に従って、通常のClaude Codeとしての作業を行う(このスキル自体は作業内容を規定しない)。

### 9. 完了報告とセッション終了

**`close`を呼ぶ(=待受ループを止める)時は必ず、`notify`の本文に「このトークでの待受を終了した」旨と「自動監視はしていない」旨を明示する(必須・省略禁止)。**
「完了しました」だけだと、ユーザーが「続けてメッセージを送れば拾ってもらえる」と誤解し、
送ったメッセージが誰にも処理されないまま放置される事故につながる(2026-07-12実機検証で
ユーザーから指摘)。逆に「このトークは今後も使えます」とだけ書くと、今度は「常時監視されている」
という別の誤解を招く(2026-07-13、cgd Lv7レビューでDeepSeek/Codexが指摘)。**「トークは
再利用できるが、自動では拾われない」の両方を毎回明示する。** この注意書きは`close`を呼ぶ
あらゆる箇所(このステップ・72h expiry・異常終了時など)で毎回付ける。

```bash
python remote_control_client.py notify <session_id> <lease_token> "<完了報告メッセージ>。このトークでの待受は終了しました。このトークは次回以降も再利用できますが、自動監視はしていません。次のご依頼はこのトークに書いた後、Claude Code側で改めて/loop /remote-controlを起動してください"
python remote_control_client.py close <session_id> <lease_token> done
```

これで`/loop`のこのイテレーションは終了する(`ScheduleWakeup`を呼ばない)。同じトークで次のタスクを行いたい場合は、ユーザーがそのトークに指示を書いた上で、改めて`/loop /remote-control <room_id_or_label> <room_type>`を起動する(新しいトークを作る必要はない)。

## サーバー側の実装メモ

- `POST /lineworks/sessions`: room_id+room_typeでセッションを作成、または既存セッションのleaseが切れている場合のみ引き継いで取得する。leaseがまだ有効なら409(二重起動防止)
- `owner_sender_id`はセッション作成時点では未確定(NULL)。`POST .../claim`で、送信者をallowlist検証した後にサーバー側で一度だけ確定させる(cgd Lv2レビュー: 作成直後にowner確定を強制すると安全に決められないという指摘への対応)
- `lease_token`/`lease_expires_at`は外部攻撃対策ではなく(APIキー認証がその役目)、同一roomを複数のClaude Codeセッションが同時にポーリングして二重askするのを防ぐための排他制御
- `POST .../ask`は質問送信+`waiting_reply`遷移、`POST .../notify`は回答を待たない一方向送信(完了報告用)。両者を分離しているのは、完了報告のたびに誤って待機状態に入るのを防ぐため
- `ask`は`status IN ('active','waiting_reply')`のセッションにのみ許可する(`done`/`expired`を誤って再開させない、cgd Lv7レビューでCodex highが実コードから発見した穴)
- `waiting_started_at`(質問サイクルの開始・24h/72h判定の基準)と`waiting_since_asked_at`(直近のask/remind・ポーリング段階判定の基準)は別カラム。通常の`ask`は両方をリセットするが、`remind`は`waiting_since_asked_at`のみ更新し`waiting_started_at`は変えない(リマインドで72h期限が延びるのを防ぐ、cgd Lv7レビューでの指摘)
- `POST .../remind`は`reminder_sent_at IS NULL`の条件付きUPDATEで1回だけに制限される専用エンドポイント。通常の`ask`をリマインドに流用すると「1回だけ」を担保できない(cgd Lv7レビュー)
- `GET /lineworks/rooms`: `lineworks_messages`から`room_id`ごとに集計(room_type/最終受信日時/件数)して返す。room_idの手入力を不要にするための一覧取得用(2026-07-13追加)。CLIの`rooms-discover`がこれとローカルの`lineworks_known_rooms.json`を突き合わせてラベルを付与する
- `created_from_message_id`(ルーム永続化v3で追加): セッション作成時点の`lineworks_messages.id`最大値。`close`後に同じroomで新セッションを作っても、それより前のメッセージ(前タスク完了後の相槌等)を新タスクの指示として拾わないための境界線
- `claim`/`notify`/`heartbeat`も(`ask`/`close`と同様)`status IN ('active','waiting_reply')`のセッションにのみ許可する。永続ルームでは古いsession_id+leaseで別タスクのroomへ操作が混線するのを防ぐため(cgd Lv7レビューでCodex highが指摘)
- Webhook受信(`/lineworks/callback`)は`dedup_key`(room_id+sender_id+text+issuedTimeのハッシュ)で重複配信を弾く(冪等化)
- 死活監視は別バッチ`041.Claude間連携API/scripts/lineworks_session_watchdog.py`(Windows Scheduled Taskから15分毎起動、`stale_notify.py`と同パターンだが書き込み可能な接続を使う)。`waiting_reply`のまま`updated_at`が45分以上更新されていないセッションを検出し、Aへ「待受ループが停止している可能性があります」と通知する。ループ自身は「自分が止まったこと」を報告できないため、この独立監視が必須

## エラー時

- `設定が不足しています: ...` → `.claude/relay_local/.env` に `RELAY_BASE_URL`/`RELAY_API_KEY` が未設定
- `APIエラー 403` → LineWorks系エンドポイントはAのAPIキーでのみアクセス可能
- `APIエラー 409`(session-start時) → 別プロセスが既にこのroomのセッションを保持中。しばらく待つか状況を確認
- `APIエラー 409`(claim/ask/heartbeat/close/notify/remind時) → lease_tokenが不一致または期限切れ。`session-start`をやり直す
- `APIエラー 409`(remind時) → 既にリマインド済み(1回制限)。そのまま通常のポーリング継続へ
- `APIエラー 409`(claim/ask/notify/heartbeat時) → セッションが既に`done`/`expired`。`session-start`をやり直して新しいsession_id/lease_tokenを取得する(同じトークのまま新規タスクとして扱える。新しいトークは不要)
- `エラー: ラベル'...'の登録room_typeは...`(CLI側) → `lineworks_known_rooms.json`の登録値とコマンド引数のroom_typeが矛盾している。どちらかを修正する
- `APIエラー 503`(ask/notify/remind時) → サーバーの`.env`に`LW_*`が未設定
- `通信エラー` → `RELAY_BASE_URL`が到達不能。中継APIサーバー・Caddyの稼働状況を確認
