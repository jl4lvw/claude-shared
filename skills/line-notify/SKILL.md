---
name: line-notify
description: 作業の完了や状況を、LINE のグループへ「状況を説明した画像(A4・1枚)のURL」つきで通知する標準手順。画像を作り、自社の公開置き場(pub.sfuji.f5.si)へ置いてURLを作り、文面と一緒に承認を得てから、ボット FujiWorks の名義で送る。「LINEで通知して」「LINEに投稿して」「楽天メルマガ配信に知らせて」「作業完了を共有」などで起動。単純なテキストだけで済ませない。
---

<!-- SKILL_VERSION: 2026-10-16_130000 -->

# line-notify — LINE グループへ、画像つきで状況を知らせる

作業フォルダ: `C:\ClaudeCode\.claude\skills\line-notify`。道具: `make_status_image.py`(画像)/ `line_notify.py`(公開・送信)。

**2026-10-16 寺下さん決定**: LINE で通知するときは、**単純なテキストだけにしない**。
**状況を説明した画像を作り、その公開アドレス(URL)を添えて送る**。受け取る人(社外の端末・スマホ)が URL を押せば画像が見える形にする。

## 前提(ここを間違えない)

| 項目 | 内容 |
|---|---|
| どの LINE か | **通常の LINE**(Messaging API)。LINE WORKS ではない。LINE WORKS 受信窓口(041)は関係ない |
| 送信者 | ボット **FujiWorks**(`090.LINE業務指示エージェント` が持つ)。グループの全員に見える |
| 画像の渡し方 | LINE は画像を「公開URL」でしか渡せない。ボット経由で画像ファイルは送れない |
| 公開置き場 | `https://pub.sfuji.f5.si/`(Caddy の静的置き場・`900.ClaudeCode/public-share/`・**ログイン不要・一覧表示なし・GET/HEADのみ**) |
| URL の守り方 | 推測されにくい長い乱数ファイル名。**URL を知っている人は誰でも見える**。社内・個人情報の画像は置かない |
| 社内LANの罠 | 社内 LAN からは公開ドメインが引けない(ヘアピンNAT非対応)。`pub.sfuji.f5.si` は **DNS 登録済み**(2026-10-16・TK へ依頼)。新しい公開名を足したときは `/subdomain-add` の手順 |
| グループIDの置き場 | `090.LINE業務指示エージェント/data/line_groups.json`(**Git対象外**)。**グループIDをスキル・メモリ・会話に書かない**(`line_notify.py groups` は先頭と末尾だけ表示) |

## 手順

### 1. 何を伝えるか決め、画像の仕様(JSON)を書く

通知の中身(何をしたか・どこに反映したか・注意点・確認方法)を画像にまとめる。A4 縦 1 枚。
仕様の書き方は `make_status_image.py` の冒頭を見る(見出し・表・変更前後・写真・注意・メモ)。

- **写真は元ファイルから**(例: 削除した写真・残した写真を並べる)。赤枠=削除、緑枠=残す
- 文字は短く。1 ページに収まらないときは**項目を減らす**(道具は収まらないと止まる。小さくして詰めない)
- お客様向けの価格は**税込**で書く。G番号は**商品名を括弧書きで併記**する

```bash
python C:/ClaudeCode/.claude/skills/line-notify/make_status_image.py <spec.json> <out.png>
```

作ったら**画像を自分で開いて確認**する(見切れ・誤字・数字)。

### 2. 画像を公開してURLを作る

```bash
python C:/ClaudeCode/.claude/skills/line-notify/line_notify.py publish <out.png> --prefix g0964
```

- 乱数つきのファイル名で置き、この端末から自分自身(127.0.0.1)に向けて「HTTP 200・サイズ一致」を確認する
- 画像は元ファイルとは別に、**Downloads と NAS(`\\192.168.1.50\共有スペース\寺下\999.一時ファイル`)にも 1 部コピー**する(成果物の置き場の決まり)
- **寺下さんに、スマホ(モバイル通信または社内 Wi-Fi)で URL を開いて確認してもらう**。開けたと言われるまで送らない

### 3. 投稿の下書きを見せて、承認を得る(アーティファクト)

送る**全文**(本文+URL)と、送信先グループ名、送信者がボットで全員に見えることを、アーティファクトで見せる。
本文は短く: 題名の行 → 何をしたか(2〜4 行)→ 対象/反映先 → 注意。最後に画像の URL。

```bash
python C:/ClaudeCode/.claude/skills/line-notify/line_notify.py send --group <グループ名> --text-file <本文.txt> --url <URL>
```

確認だけ(送らない)。全文と**確認コード(sha8)**が出る。**勝手に送らない。承認の前に `--send` を付けない。**

### 4. 承認後に送る

```bash
python C:/ClaudeCode/.claude/skills/line-notify/line_notify.py send --group <グループ名> --text-file <本文.txt> --url <URL> --send --approved <確認コード>
```

- 確認コードが全文と一致しないと送れない(承認した文面以外は送れない)
- 送ったら **LINE のメッセージID が返る**。寺下さんに、LINE の画面で届いたか・文面が合っているか確認してもらう
- 送信ログ: `090.LINE業務指示エージェント/data/line_notify_log.jsonl`

## グループの追加

新しいグループへ送るとき: ボットをグループに招待 → そのグループで誰かが `@FujiWorks` をつけて 1 通投稿 → 090 の受付記録に
グループID が残る(`090.../data/line_agent.db` の `line_tasks.group_id` / `line_messages.target`)。
`line_notify.py register --name 名前 --group-id Cxxxx` で登録する。**グループの人たちに見える投稿になるので、試験投稿は事前にひとこと断る**。
(`@FujiWorks 〜` の投稿は 090 に「受付 #N」の依頼として登録される点に注意。不要なら取り下げる)

## やってはいけないこと

- **承認なしの送信**、文面を承認後に変えて送ること
- **グループID・トークン・PIN を会話・スキル・メモリに書く**(`090/.env`・`065.../.env` の値を表示しない)
- 画像を**社外のサービス**にアップロードして URL を作る(社内画像が社外に残る)
- 個人情報・原価・顧客名の入った画像を公開置き場に置く
- 公開置き場の README や一覧を置く(`public-share/` は `.gitignore` で中身を Git 対象外にしてある)

## 公開した画像の片づけ

「ずっと残す」と決めたもの以外は、不要になったら `900.ClaudeCode/public-share/` からファイルを削除する(削除すると URL は開けなくなる)。

## 関連

- 商品の変更・追加の作業そのものは `/mall-product-update`。終わったら、この `/line-notify` を**別途**呼んで通知する(スキルから別スキルを自動で呼ばない)
- Caddy の公開名の追加・DNS: `/subdomain-add`
