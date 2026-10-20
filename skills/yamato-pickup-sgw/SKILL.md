---
name: yamato-pickup-sgw
description: ヤマトビジネスメンバーズ（bmypage.kuronekoyamato.co.jp）で、サスガワークス合同会社（山口県岩国市）の集荷依頼を出す。通常は端末 A の yamato_pickup（Playwright）へ申請を積み、運用者が LINE WORKS で「実行」を押すと、履歴で二重依頼を確認→入力→確認画面の照合→「確定」まで自動で進む。Claude は申請を積んで結果を読むだけ。「ヤマトの集荷依頼」「集荷を頼んで」「クロネコ集荷」「/yamato-pickup-sgw」で使う。
---

# /yamato-pickup-sgw — ヤマト 集荷依頼（サスガワークス・岩国）

- **通常（A）**: `900.ClaudeCode/yamato_pickup`（Playwright・keyring・専用プロファイル）。使い方の詳細は `900.ClaudeCode/yamato_pickup/README.md`
- **予備（B）**: Claude in Chrome で手で進める（自動が使えないとき・画面の観察）
- 画面の観察記録（2026-10-20）: `C:\ClaudeCode\094.Chrome自動操作\docs\yamato_pickup_screens.md`
- **状態: 試作（入力・確認・履歴は観察済み。完了画面・確定の確認ダイアログ・訂正/キャンセルは未観察）。** 初回の実依頼の後、結果とスクリーンショットで観察記録・README・この文書を直す。

## 🚨 絶対ルール

1. **Claude は `run` を起動しない。** パスワードでログインする実行は、見張り役（a_browser_watch）が運用者の LINE WORKS「実行」の後に起動する。Claude がやるのは `enqueue` と `show` だけ。承認の表（line_approvals）に書き込まない。
2. **確定の承認は LINE WORKS の「実行」1 回**（2026-10-20 運用者決定）。積む前に、申請の中身（日付・時間帯・荷物・ご要望）を表でユーザーに見せて確かめる。
3. 集荷希望日・時間帯・荷物の種類と個数は、ユーザーの指示にあるものだけを入れる。足りなければ推測せず質問する。合計 30 個まで・本日〜6 日後まで。
4. 集荷先は画面の既定値（サスガワークス合同会社・岩国市）のまま。ツールは違っていたら止める。住所変更はしない。
5. **同じ集荷希望日の追加**は、ユーザーが「別便で追加」と明示したときだけ `"allow_same_day": true` と `"extra_reason": "<理由>"` を付ける（運用者決定）。既存の依頼の個数を変えたい・大口は自動にしていない（ヤマトの画面で人が行うと伝える）。
5b. **当日の集荷**は、ユーザーに「担当者が当日に集荷できることを確かめたか」を聞き、確かめた答えがあったときだけ `"same_day_confirmed": true` を付ける（運用者決定。無ければツールが断る）。
6. **needs_human・確かめていない記録があるときは再申請しない。** 運用者に「履歴・再依頼」を見てもらい、A の窓で `resolve <id> yes|no` を実行してもらう（Claude は実行しない）。
7. ID・パスワードは扱わない。keyring への登録は運用者が `python -m a_browser_common.setup_credentials yamato` で行う。

## A. 通常: 申請を積む → LINE WORKS で承認 → 結果を読む

1. 指示から申請を作り、表で見せて確かめる（当日なら「締切を過ぎると翌日扱いの恐れ」を添える）。
2. `C:/tmp-ai/yamato_pickup_<日時>.json` に書く（UTF-8）:
   ```json
   {"source":"A","pickup_date":"2026-10-21","time_slot":"none","items":{"takkyu":3},"comment":""}
   ```
   - `time_slot`: `none`=指定なし / `am`=13時まで / `14-16` / `16-18` / `17-1830`
   - `items`: `takkyu` 宅急便・`cool`・`time_service`・`compact` 宅急便コンパクト・`nekopos` ネコポス/ゆうパケット・`yumail` ゆうメール(冊)・`other`
3. `C:\ClaudeCode\900.ClaudeCode` で `python -m yamato_pickup.cli enqueue <json>` → `queued` と申請番号を伝え、見張り役の記録（`a_browser_data\watch.log`）に「yamato id=N 承認を依頼」が出たことを確かめて、「LINE WORKS に届く承認の『実行』を押してください。押されしだい自動で続けます」と伝える。`rejected` なら理由をそのまま伝える。
   **「押したら知らせて」と頼まない**（2026-10-20 ユーザー指示）。続けて Bash の `run_in_background` で待ち受けを始める（watch.log に `yamato id=N 実行終了` か `yamato id=N 終了` が出るまで 15 秒ごと・最大 2 時間 15 分）。終わりの通知が来たら手順 4 へ自動で進む。
4. 待ち受けが終わったら `python -m yamato_pickup.cli show <id>` の `status`・`result.detail` を伝える:
   - `confirmed`: 日付・時間帯・荷物・（あれば）受付番号。`warnings`・`note` があれば必ず添える
   - `stopped` / `failed`: 確定していない。`detail`・`problems` を伝える
   - `needs_human`: 依頼が入ったか分からない。**再申請しない**（ルール 6）
   - `queued` のまま: まだ承認されていない（承認の期限は 2 時間）
5. 見張り役の状態は `python -m a_browser_watch.cli status`。

### 試運転（確定を押さない）
画面の確認や初回の動作確認には、申請に `"mode": "dry_run"` を付ける。確認画面の照合まで本番と同じに進め、「確定」は押さずに止まる（status `dry_run`。依頼は入らない）。当日の担当者確認・別便の理由は要らない。LINE の承認文の先頭に【試運転】と出る。結果は `problems`（空なら照合はすべて一致）と `detail` を伝える。**試運転の結果を「依頼した」と伝えない。**

## B. 予備: Chrome で手で進める（自動が使えないとき）

予備の手順では、**「確定」は確認画面の内容を表で見せ、チャットでユーザーの明示の承認を得てから Claude が押す**（承認は 1 回ごと）。ID・パスワードは入力しない（欄が空ならユーザーに手動ログインを頼む）。入力の前に「履歴・再依頼」で同じ日の依頼を確かめる。

### 0. 準備
- ToolSearch で Chrome ツールを一括ロード（`select:mcp__claude-in-chrome__tabs_context_mcp,mcp__claude-in-chrome__navigate,mcp__claude-in-chrome__computer,mcp__claude-in-chrome__find,mcp__claude-in-chrome__get_page_text,mcp__claude-in-chrome__javascript_tool,mcp__claude-in-chrome__browser_batch,mcp__claude-in-chrome__tabs_create_mcp,mcp__claude-in-chrome__tabs_close_mcp`）。
- 指示から次を確かめる:
  - **集荷希望日**（今日〜6 日後のみ選べる）
  - **時間帯**: 指定なし / 13時まで / 14〜16時 / 16〜18時 / 17〜18時30分
  - **荷物**: 宅急便・クール宅急便・タイムサービス・宅急便コンパクト・ネコポス/ゆうパケット（個）、ゆうメール（冊）、その他（個）
  - **ご要望**（任意・全角 100 文字以内）
- 当日の依頼は締切を過ぎると翌日扱いになる場合がある（締切時刻は画面に出ない）。午後の当日依頼はその旨をユーザーに伝える。

### 1. ログイン
1. `tabs_context_mcp`（createIfEmpty）→ URL を開いて 4 秒待つ。
2. `javascript_tool` で `#code1` `#password` の空/入力済みを確かめる（値は読まない・出さない）。空ならユーザーに手動ログインを頼んで止まる。
3. ポータル（「ご利用中のサービス」が出る画面）になったら次へ。

### 2. 集荷依頼の画面を開く（同じタブで）
「集荷依頼」は普通に押すと**別ウィンドウ**で開き、Claude から見えなくなる。先に次の JS を実行してから、左メニュー「ご利用中のサービス」の「集荷依頼」を `find` → ref クリック → 6 秒待つ:

```js
const o=HTMLFormElement.prototype.submit;
HTMLFormElement.prototype.submit=function(){ this.target='_self'; return o.call(this); };
window.open=function(u){ location.href=u; return null; };
```

URL が `ybmshuka.kuronekoyamato.co.jp/ybmshuka/YbmShukaRequestAction_doCollect.action` になれば成功。

### 3. 二重依頼の確認
上部のタブ「履歴・再依頼」を**座標クリック**（`find` では見つからない。screenshot で位置を見る）→ `get_page_text` で一覧を読む。同じ集荷希望日の依頼があれば、表で見せて止まる。無ければ「集荷依頼」タブを座標クリックで戻る。

### 4. 入力
`javascript_tool` で値を入れて `change` を発火する:

```js
const set=(n,v)=>{const e=document.querySelector(`[name=${n}]`); e.value=v; e.dispatchEvent(new Event('change',{bubbles:true}));};
set('shukaYmd','YYYYMMDD');        // 例 20261026
set('shukaTimeAll','0000-2400');    // 指定なし。13時まで=0000-1300 / 1400-1600 / 1600-1800 / 1700-1830
set('nimotsuTakkyu','1');           // 宅急便の個数
```

| 荷物 | name |
|---|---|
| 宅急便 | `nimotsuTakkyu` |
| クール宅急便 | `nimotsuCool` |
| タイムサービス | `nimotsuTimeService` |
| 宅急便コンパクト | `nimotsuCompact` |
| ネコポス・ゆうパケット | `nimotsuNecoPos` |
| ゆうメール（冊） | `nimotsuDM` |
| その他 | `nimotsuOther` |
| ご要望 | `shukaComment` |

集荷先情報（法人名・担当者・電話・メール・住所）は既定値のまま触らない。
「次へ」を `find`（「次へ」画像ボタン。2 件出るがどちらでも可）→ `scroll_to` → ref クリック → 5 秒待つ。

### 5. 確認画面の報告（ここで止まる）
「以下の内容で登録します」の画面を `get_page_text` で読み、次の表で返して承認を求める（返答の最後に `【対応待ち】`）:

| 項目 | 内容 |
|---|---|
| 集荷先 | 法人名・ご担当者名・住所（電話・メールは省略） |
| 集荷希望日 | |
| 時間帯 | |
| 荷物 | 種類ごとの個数 |
| ご要望 | |

- 修正が要るときは「戻る」（`action:ShukaDataConfirmAction_doBack`）で入力画面へ。入力値は残っている。

### 6. 確定（承認後のみ）
承認を得たら「確定」（`action:ShukaDataConfirmAction_doDecision`）を `find` → ref クリック → 6 秒待つ → 完了画面を `get_page_text` + screenshot で読み、受付番号などを報告する。
**確定は 1 回だけ押す。** 反応が無くても連打せず、「履歴・再依頼」タブで登録されたかを確かめる。

### 7. 片付け
自分で開いたタブを `tabs_close_mcp` で閉じる（ユーザーが残してと言った場合を除く）。

## 待ち時間・注意

- 確認画面の後のステップ表示は `1.入力 → 2.住所選択 → 3.確認 → 4.完了`。「住所変更」を押さなければ 2 は飛ばされる。
- 画面にサイズ・重さ・伝票番号の欄は無い（種類ごとの個数だけ）。
- `javascript_tool` の結果に URL のクエリが含まれると `[BLOCKED]` になる。関数の中身を調べるときは URL を含めない形で返す。
- 実績: 2026-10-20 観察のみ（確定は押していない）。

## 更新ルール

初回の実依頼で、完了画面の項目（受付番号の有無など）と、これからの依頼の訂正・キャンセルの画面を `docs/yamato_pickup_screens.md` とこの SKILL.md に追記し、冒頭の「試作」表記を外す。
