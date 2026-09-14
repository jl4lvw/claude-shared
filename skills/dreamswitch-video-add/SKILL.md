---
name: dreamswitch-video-add
description: ドリームスイッチ(子供用動画プレイヤー)へ、YouTube動画をダウンロード・変換・サムネ生成・SYMV.LST登録し、SDカード接続PCへ転送するまでの一連の手順。069.ドリームスイッチを使う。
trigger: 「ドリームスイッチに動画追加」「YouTube動画をドリームスイッチに」「SYMV.LST登録」「dream switch」などで起動
---

<!-- SKILL_VERSION: 2026-09-15_000000 -->

# dreamswitch-video-add — ドリームスイッチ動画追加スキル

作業フォルダ: `C:\ClaudeCode\069.ドリームスイッチ`(固定)。
詳細な技術背景は [`ドリームスイッチ_動画追加手順書.html`](\\192.168.1.50\共有スペース\寺下\ドリームスイッチ_動画追加手順書.html) も参照。
本スキルはそこに無い「Claude Codeでの実運用フロー」と、実際にハマった罠を凝縮したもの。

---

## 0. 前提環境(このPC = Caddy/PWAホスト機で確認済み)

```
python  : winget管理下 Python 3.12(例: C:\Users\<user>\AppData\Local\Programs\Python\Python312\python.exe)
ffmpeg  : winget管理下(PATH通っていればOK)
deno    : winget install --id DenoLand.Deno (yt-dlpのJSチャレンジ解決に必要)
pipパッケージ: pip install Pillow numpy yt-dlp
実行シェル: Git Bash(bashコマンド。build_video.sh/add_youtube.shはbash前提)
```

別PC(または別ユーザーアカウント)で作業する場合、`_build/build_video.sh` と
`_build/add_youtube.sh` 冒頭の `ROOT=` `PY=` `DENO_DIR=` をそのPCの実パスに書き換える。

---

## 1. 絶対に外さないこと(技術制約)

- ffmpeg単体はJPEGサンプリング垂直係数=2でしか出力できず、本体で**緑ブロック/黒サムネ**になる。
  必ず **Pillow(`encode_frames.py`)で動画フレームを subsampling=1(4:2:2)**、
  **サムネを subsampling=0(4:4:4)** でJPEGエンコードすること。
- 音声は **PCM signed 16-bit LE / 44100Hz / モノラル(1ch)** 固定。
- `SYMV.LST` は **タブ区切り・CRLF改行を維持**。ファイル名は実ファイルと完全一致(大文字小文字含む)。
  LSTに実在しない名前を書くと本体がフリーズする → **名前を仮決めしたまま登録しない**。
- **GPUデコード(`-hwaccel cuda`等)は付けない。** AV1ソースでGTX1660世代のGPUが
  非対応のまま0フレームで失敗する実例あり(2026-09-14)。そもそものボトルネックは
  Pillowのフレーム単位JPEGエンコード(CPU限定・GPU化不可)なのでデコード高速化の
  恩恵も薄い。「GPUを使ってほしい」と言われたら、この理由を説明した上で見送るか、
  デコード段だけの限定的な高速化に留める判断をユーザーに委ねる。
- yt-dlp をBash経由のPythonワンライナーで呼ぶと **CP932コンソールでタイトルが文字化けする**。
  必ず環境変数 `PYTHONUTF8=1` を設定してから呼ぶ(`batch_convert.py` は既に対応済み)。
- 日本語パス(`069.ドリームスイッチ`)を含むため、直接の Edit ツールでの編集が不安定なことがある。
  スクリプトファイル経由(`python .py` 実行)で処理するのが安全。

---

## 2. 全体フロー

```
候補URL収集(個別+プレイリスト展開・重複除去)
        ↓
メタデータ取得(id/title/duration, PYTHONUTF8=1必須)
        ↓
出力名(3候補)・カテゴリ案の生成 → レビュー用Artifact表示(任意)
        ↓
選択用Artifact(名前3択チップ+カテゴリ4択トグル、artifact capability で自動保存)
  ユーザーが「これで」と言うまで待つ → 選択後のstate-dataを読み取る
        ↓
batch_convert.py で一括DL+変換+サムネ(SYMV.LST登録はまだしない)
        ↓
register_batch.py で SYMV.LST へ一括登録(CRLF維持・ファイル実在チェック付き)
        ↓
整合性チェック(LST記載 ⇔ 実ファイル)
        ↓
(必要なら) SDカード接続PCへ転送 → 転送後は必ず公開を撤去
```

### 2-1. 候補URL収集

ユーザーから個別URL・プレイリストURLが断続的に送られてくることが多い。都度メタデータ
取得を走らせず、ある程度まとまってから一括処理する。プレイリストは
`yt-dlp --flat-playlist -J <playlist_url>` で展開してから個別URLと合算し、
**video ID で重複除去**すること(同じ動画が違うURL形式で複数回送られてくることがある)。

### 2-2. メタデータ取得

```bash
PY="<python.exeの実パス>"
DENO_DIR="<denoのwingetパス>"
export PATH="$DENO_DIR:$PATH"
export PYTHONUTF8=1
"$PY" -m yt_dlp --remote-components ejs:github --skip-download \
  --print "%(id)s|%(title)s|%(duration)s" "<URL>"
```

`PYTHONUTF8=1` を忘れると日本語タイトルが `\x83` 等に化ける(構文エラーにはならず
気づきにくい)。化けたら該当分だけ再取得すればよい(ファイルは上書きされる)。

### 2-3. 出力名・カテゴリの提案

- 出力名は英語タイトルからCamelCaseで**3候補**を人間が読んでも自然な単語選びで作る
  (機械的な単語抽出だけでなく、意味のある短縮を意識する)。**全候補・全動画を通して
  重複が無いことをコードで検証**してから提示する。
- カテゴリは既存 `SYMV/SYMV.LST` を実際に読んで判断する。このドリームスイッチでは
  `stories1` = 英語絵本の読み聞かせ・一般的なおはなし、`stories2` = Sanrio/Cinnamoroll系の
  キャラクターストーリー、`song1`/`song2` = 童謡、という区分けが読み取れる
  (`oha1_*`/`oha2_*`/`uta1_*`/`uta2_*` の内容から判断・今後コンテンツが増えたら要再確認)。
- 迷うもの(既存カテゴリのテーマに完全一致しないもの等)は決め打ちせず、選択用Artifactの
  トグルでユーザーに選んでもらう。

### 2-4. レビュー・選択Artifact

`templates/review_artifact.example.html` と `templates/select_artifact.example.html` は
過去実装(2026-09-14, 37本のCinnamoroll/英語絵本バッチ)の実例。**そのまま使い回さず**、
今回のコンテンツに合わせてグループ分け・パレット・件数を作り直すこと(グループ分類は
コンテンツ依存のため機械的な使い回しが効かない)。共通して踏襲すべき実装パターン:

- 選択用Artifactは **`capabilities: {artifact: {}}`** を宣言し、`window.claude.use('artifact')`
  経由で `artifact.publish(document.documentElement.outerHTML)` により自己保存させる
  (クリックのたびにdebounce付きで自動保存 → ユーザーは「これで」と言うだけでよい)。
- 状態は `<script type="application/json" id="state-data">` に埋め込み、選択完了後は
  `Artifact action:"read"` で最新版を読み、保存されたローカルファイルから
  `id="state-data"` のJSONを抽出する(972行等になる大きいファイルなので `Grep` で
  行番号を特定してから `Read offset/limit` で該当行だけ読むと速い)。
- 出力名3候補・カテゴリ4択とも、初期状態で機械提案がすでに選択された状態(defaultあり)
  にしておく(「開いた瞬間から動く状態」を保つ)。

### 2-5. 一括ダウンロード+変換

```bash
"$PY" .claude/skills/dreamswitch-video-add/scripts/batch_convert.py \
  "C:\ClaudeCode\069.ドリームスイッチ" "<選択済みcandidates.jsonのパス>"
```

- `candidates.json` は `[{"id","url","proposedName"}, ...]` の配列。
- 失敗した動画があっても他は続行される(`_build/batch_log.txt` に記録)。再実行すると
  `.jmv`/`.jbm` が既に揃っている分は自動スキップされるので、失敗分だけ再試行できる。
- ダウンロード失敗はネットワークの一時的な問題であることが多い。`_build/dl_<name>.mp4`
  が残っていれば、再ダウンロードせず `build_video.sh` だけ手動で再実行して時間を節約できる。

### 2-6. SYMV.LST登録

```bash
"$PY" .claude/skills/dreamswitch-video-add/scripts/register_batch.py \
  "C:\ClaudeCode\069.ドリームスイッチ" "<選択済みselections.jsonのパス>"
```

- `selections.json` は `{"<videoId>": {"name","cat"}, ...}`(選択Artifactの `state-data` と同じ形)。
- 対象の `.jmv`/`.jbm` が両方揃っていない出力名が1つでもあれば**何も書き込まず中断**する
  (部分的な不整合を防ぐ)。
- 既に登録済みの名前は自動スキップされる(冪等)。

### 2-7. 整合性チェック(必須・省略しない)

```bash
cd "C:\ClaudeCode\069.ドリームスイッチ"
tr -d '\r' < SYMV/SYMV.LST | awk 'NF && $1!="*"{print $1,$2}' | while read jbm jmv; do
  [ -f "SYMV/JBM/$jbm" ] || echo "欠落: JBM/$jbm"
  [ -f "SYMV/JMV/$jmv" ] || echo "欠落: JMV/$jmv"
done
echo "上に何も出なければ全一致"
```

このPCには過去セッションで登録された分(別PC由来)の実ファイルが無いことがある。
「欠落」が出ても**今回追加した分が含まれていなければ問題ない**(今回分だけで絞り込んで
二重チェックするとよい)。

---

## 3. SDカード接続PC(別ネットワーク)への転送

`069.ドリームスイッチ/SYMV/` を丸ごと変換した後、SDカードへ書き戻すのは**別PC**で行う運用
(このスキルの担当範囲外)。転送は Caddy の一時 vhost 経由で行った実績がある:

1. `inventory-replenishment-pwa/Caddyfile` に一時 vhost を追記(**必ずバックアップを取ってから**)。
   `basic_auth` でユーザー名/ランダムパスワードを付け、`root * <SYMV実パス>` + `file_server browse`。
   ```
   caddy hash-password --plaintext "<ランダムパスワード>"
   ```
   でbcryptハッシュを生成して埋め込む。
2. `powershell -File "C:\ClaudeCode\040.管理者権限コマンド実行\reload_caddy.ps1"` でreload
   (管理者権限不要、[[reference_caddy_admin_no_admin_ops]]参照)。
3. 外部ネットワークの端末は sfuji.f5.si のDDNS経由でそのまま到達できる
   (**LAN内DNS登録(TK依頼)は不要** — 別ネットワークからのアクセスは
   [[reference_lan_dns_wildcard_resolved]] のLAN内ヘアピンNAT問題と無関係)。
   `nslookup <name>.sfuji.f5.si` で確認できる。
4. `curl --resolve <name>.sfuji.f5.si:443:127.0.0.1 -u <user>:<pass> https://<name>.sfuji.f5.si/`
   で認証あり200・認証なし401を確認してから、URLとパスワードをユーザーに伝える。
5. **転送完了の連絡を受けたら、追記したvhostブロックを削除してreloadし、
   SSL接続不可になることを確認する。** 公開を放置しない(容量も大きく、認証があっても
   長期公開は避ける)。

---

## 4. 罠まとめ(再訪時に読む)

| 症状 | 原因・対処 |
|---|---|
| yt-dlpの出力する日本語タイトルが化ける | `PYTHONUTF8=1` を忘れている |
| AV1ソースの動画変換が0フレームで失敗 | `-hwaccel cuda` 等GPUデコードを外す(セクション1参照) |
| build_video.sh/add_youtube.shが謎のパスエラー | `ROOT=`/`PY=`/`DENO_DIR=` がこのPCの実パスになっていない |
| SYMV.LST登録後に本体がフリーズ(実機検証時) | 存在しない`.jbm`/`.jmv`名を登録した。`register_batch.py`の事前チェックで防げるはず |
| 同じ動画が複数回リストに来る | video ID でのdedupを忘れている(プレイリスト展開分と個別URL分の重複によくある) |
| 別ネットワーク端末への転送でLAN内DNS登録を依頼してしまう | 不要。外部からはDDNSで直接届く([[reference_lan_dns_wildcard_resolved]]はLAN内専用の問題) |
