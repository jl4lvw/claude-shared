---
name: cgd
description: Codex+DeepSeek+Qwen の統合コードレビュー・設計相談・実装・委譲・検証スキル（**Gemini は2026-07にAPIエラー多発のため既定オフのオプトイン参加に格下げ済み**）。**9段階レベル（Lv0〜Lv8）**でトークン消費・所要時間・実装主体が決まる。**レベル・Codex reasoning(low/medium/high)・Gemini/critic観点はすべてClaudeが対象から自動選択して宣言する（ユーザーに選ばせない・明示指示が最優先）**。**Lv0=委譲レーン**（DS/Qwenにコード生成を任せClaudeは分解と検証に専念・scaffold/量産タスク/コスト節約・Antigravity Plugin相当） / Lv1=Codex単独 / Lv2=Codex+DeepSeek並列（既定推奨。旧/codex等価のC+G構成は「Geminiも」等の明示指示で再現可） / Lv3=Codex+DeepSeekの技術×批評「2社×2視点」4レビュー（実装なし・review専用） / Lv4=Claude初期案→[DS+Qwen並列advisor]→Codex直列フル相談+再レビュー（Gemini併用時は先頭にGemini案出しが直列で入る） / Lv5=Lv4+🔴重大指摘の自動修正1周 / Lv6=Codex+DS+Qwen 3者並列レビュー（全員reviewer役、Gemini併用で4者に拡張可）+実装+検証+Codex再レビュー+🔴自動修正1周（**Workflow実行必須**） / Lv7=Codex多重(medium+high)+補助(DS/Qwen)の4者並列「Codex集中」構成（Gemini併用で5者に拡張可）+実装+検証+Codex再レビュー+🔴自動修正1周（最深掘り・**Workflow実行必須**） / Lv8=Lv7の技術構成そのまま+Codex(high)とDeepSeekにLv3同様の批評視点を追加した6者並列（Gemini併用で7者）+実装+検証+Codex再レビュー+🔴自動修正1周（技術の最深掘り+複眼批評、最重量級・**Workflow実行必須**）。Lv0=実装主体の切替（コストレーン）、Lv1-8=レビュー強度の選択（品質レーン）で直交。Lv4-5はDS/Qwenをadvisor役で別案出し、Lv6は横並びreviewer、Lv7は深いintegrationバグ検出を狙ってCodex多重化+DS/Qwenに関連関数抜粋を渡して補助役を強化。差分レビュー、設計判断、別案出し、実装、委譲、検証まで一気通貫。**旧 `/codex` `/gemini` 単体スキルは廃止され、本スキル（`/cgd` または `/codex` 起動）が必ずレベル自動決定から始まる**。全Lv共通の任意オプションで『critic観点』（辛口ユーザー視点＝ITに疎い現場担当者の使い勝手の不満 + あるべき論＝本来この仕様はどうあるべきかの批判を Claude本体+DS criticで評価）を追加でき、技術的正しさとは別軸で使い勝手・仕様の妥当性を否定的にチェックする。環境チェックは `python C:/ClaudeCode/.claude/tools/cgd_doctor.py` で一括。「委譲」「scaffold」「量産」「DSで書かせる」「Qwenで書かせる」「コスト節約」「3者に相談」「フルパイプ」「4者レビュー」「Codex多重」「Codex集中」「辛口レビュー」「ユーザー視点」「あるべき論」「critic」「cgd」「Codexにレビュー」「セカンドオピニオン」「C+G」「cg」「Geminiも」などのキーワードで起動。重要な設計判断・難しいバグ・大きめのリファクタの検討時には積極的に提案すること。既存 /generate-by-deepseek（DS単発コード生成→Claudeレビュー）は薄い構成で並立。
---
<!-- SKILL_VERSION: 2026-08-12_104326 -->

# cgd — Codex + DeepSeek + Qwen 統合スキル（Lv0〜8、Gemini はオプトイン）

Claude Code は司令塔。Codex / DeepSeek / Qwen を **役割分担** で使い分ける（Gemini は既定オフの追加参加者）:

| エージェント | 役割 | 呼び出し |
|---|---|---|
| Codex | コードレビュー・厳密な品質ゲート（Lv7 では medium + high の **多重実行** で深さ・別視点を並列取得） | `codex exec -c model_reasoning_effort="<low\|medium\|high>" ...` |
| DeepSeek | **Lv2 既定第2エンジン**（reviewer） / Lv4-5: 推論寄り別案出し (`--role advisor`) / Lv6: 並列 reviewer / Lv7: 補助 reviewer（**関連関数抜粋 + 差分**で表層指摘を減らす） | `python "<絶対パス>/deepseek_coder.py" --role <advisor\|reviewer> "..."` |
| Qwen3-Coder-Plus | Lv4-5: 実装寄り別案出し / Lv6: 並列 reviewer / Lv7: 補助 reviewer（**関連関数抜粋 + 差分**で表層指摘を減らす） | `python "<絶対パス>/qwen_advisor.py" --role <advisor\|reviewer> "..."` |
| Claude Code | 統合判断・実装・検証（Lv7 では事前に grep + Read で関連関数を抜粋して DS/Qwen に渡す） | （本体） |
| Gemini **（オプトイン・既定オフ）** | 長文解析・調査・案出し・追加レビュー枠。**2026-07: AI Studio 無料枠のレート制限＋Workflow版の旧CLI残存バグで実運用エラーが頻発したため既定パスから除外**。「Geminiも」「4者/5者フルで」等の明示指示があった場合のみ Lv2/4/6/7 に追加参加させる | `python "<絶対パス>/gemini_advisor.py" --role <reviewer\|advisor> "<file>"` |

## 旧スキルとの関係

- **旧 `/codex` 単体・`/gemini` 単体スキルは廃止**
- `/codex` 起動でも本スキル `/cgd` と同じフローが動く（必ず Step 1 のレベル決定から始まる・後方互換なし）
- 旧 /codex の挙動（C+G 並列レビュー）が欲しい場合は **Lv2 + Gemini オプトイン**（「Geminiも」と明示）を選ぶ
- `/generate-by-deepseek`（DS コード生成→Claude レビュー）とは目的が異なる（実装代行）

## Gemini オプトイン運用（重要・全Lv共通の前提）

2026-07、Gemini API のエラー（AI Studio 無料枠のレート制限・503 high demand）が頻発し、Lv2/4/6/7 の既定パスから **格下げ**した。Gemini は消していない。**「Geminiも」「Gemini入れて」「4者で」「5者フルで」「長文調査も」等の明示指示があった場合のみ**、そのレベルの並列/直列レビュアーに追加参加させる。

- **既定（指示なし）**: Gemini を呼ばない。Lv2=Codex+DeepSeek、Lv4=Claude初期案→DS+Qwen advisor→Codex、Lv6=Codex+DS+Qwenの3者、Lv7=Codex×2+DS+Qwenの4者
- **オプトイン時**: 各レベルの節に記載の「Gemini併用」手順に従う（列が1つ増える・Lv6は4者必須ルール、Lv7は5者必須ルールに切り替わる）
- **Claudeからの提案は不要**（critic観点と違い自動提案しない）。ユーザーが明示した時だけ追加する
- 呼び出し方式自体は変更なし（`gemini_advisor.py`、要 `GEMINI_API_KEY`）

## 前提

- **Bash 必須**（Git Bash または WSL）。PowerShell では `< /dev/null` `cat <<EOF` `$(...)` が解釈されない
- `codex` CLI（`npm i -g @openai/codex`）がインストール済み
- `codex login status` が `Logged in using ChatGPT` を返す
- Codex は `OPENAI_API_KEY` をセットしない（ChatGPT サブスク認証）
- **Lv2 以降**では `DEEPSEEK_API_KEY` が必要（Lv2 既定第2エンジン）。**Lv4-5 / Lv6 / Lv7** では `DASHSCOPE_API_KEY` も追加で必要（DS と Qwen を並列で呼ぶため・Lv4-5 は advisor、Lv6/Lv7 は reviewer 役）
- **Gemini はオプトイン時のみ必須**: `gemini_advisor.py`（Google の OpenAI 互換エンドポイントを叩く Python ラッパー、旧 gemini CLI は廃止済み）を使う。環境変数 `GEMINI_API_KEY`（Google AI Studio で無料発行）が必要。既定モデル `gemini-2.5-flash`（`GEMINI_MODEL` で上書き可）。未設定でも Gemini を使わない限り Lv1-8 すべて実行可能
- 相談段はすべて read-only 運用：書き込み・実行は Claude Code 本体が行う
- 実装フェーズに入ったら AGENTS.md / CLAUDE.md ルール（バックアップ必須・shebang禁止・`encoding="utf-8"` 明示）を強制適用する

## 利用ログ（レベル別使用回数の記録）

2026-07 導入。Step 1 でレベルが確定するたびに `C:/ClaudeCode/.claude/tools/cgd_usage_log.py` が SQLite（`C:/ClaudeCode/.claude/tools/cgd_usage.sqlite3`）に1行記録する（記録手順の詳細は Step 1 末尾を参照）。

- スキーマ: `cgd_usage_log(id, logged_at, level, gemini_opted_in, critic_used, note)`。`logged_at` は `YYYY-MM-DD HH:MM:SS`（記録時点の実時刻）
- 集計コマンド: `python "C:/ClaudeCode/.claude/tools/cgd_usage_log.py" report [--since YYYY-MM-DD]`（Lv別件数・Gemini/critic併用件数を表示）
- **目的**: 「このレベル構成は実際に使われているか」を勘ではなくデータで検証するため。特定レベルが長期間0件のままなら、そのレベルの設計（存在意義・既定値・呼びかけ方）を見直す材料にする
- 記録は副次的な計測であり、失敗しても cgd 本体の実行には一切影響しない（Step 1 参照）

---

## Step 0: 起動時のスキル最新確認（必須・Step 1 の前に実行）

`/cgd` `/codex` 起動のたびに、**Step 1 に入る前に**スキル定義が最新か確認する。既存セッションに古い手順が残ったまま実行する事故（このスキルは頻繁に改修される）を防ぐ。

### 手順
1. **バージョン照合**（軽量・必須）: **自分のコンテキストにある本文冒頭のスタンプ**を引数で渡し、機械判定させる:
   ```bash
   python "C:/ClaudeCode/.claude/tools/skill_version_check.py" cgd <自分のコンテキストのスタンプ>
   ```
   例: `python "C:/ClaudeCode/.claude/tools/skill_version_check.py" cgd 2026-07-27_212057`
   - 目視比較は誤りやすいため**必ずこのツールに判定させる**（exit 0=OK / 3=STALE / 4=判定不能）
2. **判定**:
   - `[OK]` → 最新。そのまま Step 1 へ
   - `[STALE]` → **必ず** `Read` で cgd/SKILL.md を読み直してから Step 1 へ（`/sr` 相当）
   - `[UNKNOWN]` → `--show` で現物スタンプを確認し、判断がつかなければ `Read` で読み直す
   - **今セッションで cgd/SKILL.md を読んだ記憶がない**場合も読み直す

   > **なぜ必要か（2026-07-27 の実事故）**: スキル本文は `Skill` 呼び出し時点のスナップショットとして
   > 会話に固定される。5 月に読み込んだ cgd（Lv1-5 / Gemini 第2エンジン）を 7 月まで参照し続け、
   > 現行（Lv0-8 / DeepSeek 構成）と食い違ったまま Lv3 を実行してしまった。ディスク上のファイルは
   > 正しく更新されていても、**コンテキスト側は自動では更新されない**。
   > 補助として `UserPromptSubmit` hook (`.claude/hooks/skill_freshness.py`) が、セッション開始後に
   > 更新されたスキル/コマンドを検知して警告する（全スキル対象・スキル本文非依存）。
3. **claude-shared(Git) 未取込チェック**（他端末の更新検知・推奨・重い時はスキップ可）:
   ```bash
   git -C "$USERPROFILE/claude-shared" fetch --quiet && git -C "$USERPROFILE/claude-shared" status -sb | head -1
   ```
   - `behind` が出たら「claude-shared に未取込更新があります。`/g-dl` で取り込めます」と**通知のみ**（自動 pull しない・ユーザー判断）
   - claude-shared 無し / Git エラーは黙ってスキップ（致命ではない）

### 注意
- Step 0 は**起動直後に1回だけ**。同一セッションで連続使用する場合、2回目以降はスタンプ一致なら省略可
- **スキル連鎖禁止**: Step 0 で `/g-dl` `/sr` を Skill ツールで自動呼び出ししない。Read で読み直す / 通知するに留める
- スタンプ運用は末尾「スタンプ運用ルール」を参照（編集時に必ず更新）

---

## Step 0.5: 環境チェック（任意・推奨）— `cgd doctor`

初回 / 新環境 / 環境変数を変えた直後 / 認証エラーが頻発する時は、Step 1 に入る前に `cgd doctor` を回して **使えるレベル**を確認する（DASHSCOPE_API_KEY 未設定で Lv6 を選び 1 時間悩む、を防ぐ用途）:

```bash
python C:/ClaudeCode/.claude/tools/cgd_doctor.py            # オフラインチェックのみ（無料・即時）
python C:/ClaudeCode/.claude/tools/cgd_doctor.py --probe    # 各 API に最小プロンプトで実疎通（実費発生）
```

確認項目:
- Bash 環境 / openai (python) ライブラリ
- codex CLI 存在 + `codex login status`
- 環境変数: `GEMINI_API_KEY` / `DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY` / `QWEN_BASE_URL`
- advisor スクリプト存在: `gemini_advisor.py` / `deepseek_coder.py` / `qwen_advisor.py`
- `C:/tmp-ai` 書込権限 / SKILL_VERSION スタンプ / Lv6-WF / Lv7-WF workflow スクリプト

末尾に `[判定] 実行可能レベル: Lv1 / Lv2-3 / Lv4-8` のように、いま使えるレベルを表示する（Lv8 は Lv4-7 と同じ認証要件のため同じ括りに入る）。

- `--probe` は数円程度の実費（DS/Qwen が従量課金）。鍵を初めて入れた直後 / 認証が怪しい時のみ推奨
- doctor 自体が ❌ を 1 件でも出すと exit 1（OK は exit 0）。`/cgd` を回す前のヘルスチェックに使える
- 通常運用ではスキップして Step 1 へ。「使えるはずなのに認証エラー」が出たら戻ってきて回す
- ⚠️ **doctor は Step 1 より前に回すこと**。Lv6/7/8 を選ぶと WF 必須ゲートが張られ、doctor が叩く `codex login status` も遮断される（ゲート中は codex に触れるコマンドを一律止めるため）。ゲート中に回したい場合は先に `cgd_wf_gate.py disarm` する

---

## Step 1: レベル決定（**Claude が自動選択する**・ユーザーに聞かない）

> ### 🤖 選択はユーザーに投げず Claude が決める（2026-08-05 変更）
>
> レベル・Codex reasoning・Gemini 観点・critic 観点は **すべて Claude が推奨を自動選択**し、
> **選んだ結果と理由を 1 行で宣言してから実行する**。`AskUserQuestion` で選ばせない。
>
> - **ユーザーの明示指示が最優先**。「Lv7 で最も深く」「軽くでいい」「Geminiも」等があれば無条件でそれに従う
> - 宣言例:
>   > `Lv7`（大規模 IIFE 内の関数間整合性が論点のため）／ Codex reasoning: `high` ／ critic: なし
>   > — 強度を変えたい場合は言ってください
> - **承認ゲートは引き続き聞く**（自動化するのは「好み・強度の選択」だけ）:
>   実装許可（Step 2-XF）・Lv0 の委譲計画承認・外部 API 送信前の秘匿チェック・破壊的操作
> - 判断に迷ったら **1 段軽い方**を選ぶ。ユーザーは後から「もっと深く」と言えるが、
>   使いすぎたクォータは戻らない

**直前指示にレベル明示があればスキップ**:
- 「Lv0」「委譲」「scaffold」「量産」「DS で書かせる」「Qwen で書かせる」「テスト量産」「docstring 一括」「コスト節約」 → **Lv0**
- 「Lv1」「軽く」「Codex だけ」「クイック」 → Lv1
- 「Lv2」「通常」「軽量」 → Lv2
- 「Lv3」「批評も」「2社2視点」「4レビュー」「技術と批評両方」 → Lv3
- 「Lv4」「フル相談」「3 者で」 → Lv4
- 「Lv5」「厳しく」「自動修正も」 → Lv5
- 「Lv6」「複眼レビュー」「最重量」 → Lv6（既定 3 者。「DS もレビュー」は既定で満たすため Lv6 単体トリガから削除）
- 「Lv7」「Codex 多重」「Codex 集中」「Integration バグ重視」「最深掘り」 → Lv7
- 「Lv8」「批評も複数で」「Codex集中+批評」「技術も批評も最深掘り」「最重量級の批評込み」 → Lv8
- 「Geminiも」「Gemini入れて」「C+G」「4者で」「5者フルで」「長文調査も」 → 選ばれたレベルに **Gemini を追加参加**（既定オフのオプトイン。単体ではレベルを決定しない・上記と組み合わせて解釈）

明示指示がない場合は、**以下の判定基準で Claude が決める**（既定 = Lv2）:

| こういう対象・状況なら | レベル |
|---|---|
| scaffold・量産・docstring/テストの一括生成・コスト節約したい | **Lv0** |
| 小さな差分・低リスク・設定や文言の変更・軽い確認 | **Lv1** |
| **通常開発（迷ったらこれ）** | **Lv2** |
| 実装するか未定で、技術面と使い勝手の両方を先に見ておきたい | **Lv3** |
| 高リスク変更・設計判断・本番影響が大きい | **Lv4** |
| リリース直前・障害の再発防止・🔴 は自動で潰したい | **Lv5** |
| 広範囲の変更で盲点が怖い・複眼で潰したい | **Lv6** |
| 関数間の整合性・状態管理・大規模モジュール内のクロスリファレンスが論点 | **Lv7** |
| 技術の最深掘りと使い勝手の批評を同時にやる必要がある | **Lv8** |

**Lv6-8 を自動で選ぶときの歯止め**: 主 context とサブスククォータの消費が大きいので、**宣言に「なぜこの重さが要るか」を必ず添える**。理由が 1 行で書けないなら Lv2〜4 に落とす。

<details>
<summary>参考: 旧・ユーザー選択時の選択肢文言（宣言の言い回しに流用してよい）</summary>

```
0. Lv0 — 委譲レーン: DS/Qwen にコード生成を任せ Claude は分解と検証に専念（scaffold / 量産 / コスト節約・実装の主体を切り替える）
1. Lv1 — Codex のみ ×1（軽い差分チェック・小修正・低リスク）
2. Lv2 — Codex + DeepSeek 並列 ×1（通常開発の標準・既定推奨。旧 /codex 等価の C+G 構成が欲しければ「Geminiも」と明示）
3. Lv3 — Codex + DeepSeek「2社×2視点」4レビュー（技術×批評、実装なし・review専用）（実装するか未定だが技術面とユーザー視点/あるべき論の両方を見ておきたい時）
4. Lv4 — [DS+Qwen 並列 advisor]→Codex 直列フル相談 + 実装後 Codex 再レビュー ×1（高リスク変更・設計判断・本番影響大。Gemini併用で先頭にGemini案出しが追加）
5. Lv5 — Lv4 + 🔴 重大指摘の自動修正 1 周（Codex 再レビュー計 ×2、改善なしで停止）（リリース直前・障害再発防止）
6. Lv6 — **【Workflow 実行必須】** Codex + DS + Qwen の 3 者並列レビュー（全員 reviewer 役、advisor 段廃止） + 実装 + 検証 + Codex 再レビュー + 🔴 自動修正 1 周（最重量級・複眼レビューで盲点を潰したい・Lv4-5 で DS/Qwen 別案が機能しない対象の代替。Gemini併用で4者に拡張可）
7. Lv7 — **【Workflow 実行必須】** **Codex 多重（medium + high）** + DS + Qwen の **4 者並列「Codex 集中」レビュー** + 実装 + 検証 + Codex 再レビュー + 🔴 自動修正 1 周（最深掘り・integration バグ重視・関数間整合性・大規模 IIFE/モジュール内のクロスリファレンス検査。Gemini併用で5者に拡張可）
8. Lv8 — **【Workflow 実行必須】** Lv7 の技術構成そのまま + **Codex(high) と DeepSeek に批評視点を追加**した **6 者並列** + 実装 + 検証 + Codex 再レビュー + 🔴 自動修正 1 周（最重量級・技術の最深掘りと Lv3 相当の複眼批評を同時に欲しい時。Gemini併用で7者に拡張可）
```

</details>

### Codex reasoning の自動選択（全 Lv 共通・ユーザーに聞かない）

| こういう対象なら | reasoning |
|---|---|
| 差分 50 行未満・単一関数・設定/文言変更 | `low` |
| **通常の差分レビュー（既定）** | `medium` |
| 複数ファイル横断・状態管理/並行処理・本番データ書込・原因不明の障害調査 | `high` |

Lv7 / Lv8 は `medium + high` の多重が構成の本質なので、この選択自体が不要（固定）。

### Gemini 観点・critic 観点の自動選択

- **Gemini**: 既定オフのまま。ユーザーが「Geminiも」等と明示した場合のみ参加させ、**観点（要約 / 原因特定 / 参考情報収集 / 比較評価）は Claude が対象から判断して決める**（聞かない）
- **critic 観点**: 後述「critic 観点」節の自動判定基準に該当したら **聞かずに有効化**し、宣言に含める

**Lv0 と Lv1-8 の使い分け**:
- Lv0 = **実装主体を切り替える**（Claude が書く → DS/Qwen が書く）。コストレーン
- Lv1-8 = **レビュー強度を選ぶ**（Claude が書いてレビューする）。品質レーン
- 両者は直交。Lv0 の Step 2-0D の Codex 軽量レビューが「Lv0 内蔵の品質ゲート」

決定後、対象（差分／ファイル／貼り付けテキスト）と検討テーマが不明な場合のみ確認する（**強度は聞かない**）。
判断材料が乏しく決めきれない場合は **Lv2 を採用**して進める。

**critic 観点（任意・全 Lv 共通）**: 後述の自動判定基準に該当したら **聞かずに有効化**し、宣言に含める（既定オフ）。「辛口で」「ユーザー視点で」「あるべき論で」「現場目線で」「critic」等の指示があれば自動で有効。技術レビューとは別軸で「使う人が困らないか・本来どうあるべきか」を Claude 本体 + DS critic で否定的に評価する。詳細は後述「**critic 観点**」セクション。

**利用ログ記録（必須・省略禁止）**: レベル（+ Gemini オプトイン有無・critic 観点有無）が確定した時点で、Step 2 に入る前に1回だけ記録する:

```bash
python "C:/ClaudeCode/.claude/tools/cgd_usage_log.py" record --level <0-8> [--gemini] [--critic]
```

- `--gemini` は Gemini をオプトイン参加させた場合のみ付ける（既定=付けない）。`--critic` は critic 観点を併用する場合のみ
- **本流を止めない**: このコマンドが失敗・エラーを返しても無視して Step 2 に進む（利用ログは副次的な計測であり cgd 本体の実行を左右しない）。stdout/stderrをユーザーに見せる必要もない
- ⚠️ **Lv6 / Lv7 では副作用が1つある**: このコマンドが `.claude/hooks/cgd_wf_gate.py arm` を呼び、**inline の `codex exec` を遮断するゲート**を張る（`[cgd wf-gate] Lv7: WF 必須ゲートを張りました` が stderr に出る）。以後 `codex exec` を Bash で直接叩くと PreToolUse hook が deny する。**Lv6/Lv7 は Workflow で実行すること**（後述「Workflow 経由実行」）。ゲートは WF 起動を検知すると自動解除され、保険として 180 分で失効する
- 目的: 「今のレベル構成が実際に使われているか」を後から検証するため（例: 特定レベルの使用回数が恒常的に0件なら、そのレベルの設計を見直す材料にする）。集計は `python "C:/ClaudeCode/.claude/tools/cgd_usage_log.py" report` で随時確認できる

---

## 🔴 重大指摘の定義（Lv5〜Lv8 自動修正ループの対象）

以下を満たす指摘のみ「🔴 重大」とし、Lv5 / Lv6 / Lv7 / Lv8 では自動修正ループの対象になる（Lv8 では技術レビュー表の指摘のみが対象。批評レビュー表の指摘は severity を持たないため対象外）:

1. **セキュリティ脆弱性** — XSS / SQL injection / コマンドインジェクション / 認証認可バグ等 OWASP Top 10
2. **データ破壊リスク** — 誤った DELETE / UPDATE / マイグレーション不可逆操作・バックアップなし上書き
3. **公開 API 仕様逸脱** — 後方互換破壊・契約違反・破壊的変更
4. **明白な論理バグ** — テストで検出可能な失敗パス・既知の例外を握り潰す等
5. **Integration バグ** (Lv7 / Lv8 で特に重視) — 関数間の暗黙の前提違反・スコープを跨いだ状態管理の整合性破綻・呼出経路ごとの副作用差異

自動修正の周回上限は **Lv5〜Lv8 とも 1 周**（後述 Step C2）。これら以外は 🟠 重要 / 🟡 注意とし、自動修正ループの対象外（Lv5 / Lv6 / Lv7 / Lv8 でもユーザー判断扱い。Lv8 の批評レビュー指摘は severity を持たないため常にこの扱い）。

---

## 失敗時の扱い（レベル別）

| Lv | 検証 NG（Step B / Step 2-0C） | 再レビューで 🔴 検出 |
|---|---|---|
| 0 | Step 2-0C で Claude が 1 回まで書き直し→再検証。受領品質が連続 NG なら Lv0 中断し Lv2 等に上げる | Step 2-0D で生成 ≥100 行なら Codex 1 回・Lv5 と同じ自動修正 1 周（改善なしで停止） |
| 1-3 | 中断してユーザー判断（実装フェーズなし） | 該当なし |
| 4 | Step A に戻り 1 回まで自動修正→再検証 | 報告のみ（Step C2 自動修正なし） |
| 5 | 同上 | Step C2 で 1 周まで自動修正→再レビュー（改善なしで停止＋ユーザー判断必須） |
| 6 | 同上 | Step C2 で 1 周まで自動修正→再レビュー（Lv5 と同仕様） |
| 7 | 同上 | Step C2 で 1 周まで自動修正→再レビュー（再レビューは Codex 多重ではなく medium 単独） |
| 8 | 同上 | Step C2 で 1 周まで自動修正→再レビュー（Lv7 と同仕様・対象は技術表の🔴のみ、批評レビューの指摘は対象外） |

---

## Lv0: 委譲レーン（DS/Qwen にコード生成を任せて Claude は分解と検証に専念）

> ⚠️ **番号の慣例とのギャップに注意**: 他の Lv は「番号大=重い」だが、Lv0 は「軽量」ではなく「**量産特化の例外モード**」。小修正向きではない（Step 2-0A の小ライン警告で誘導）。一般的な開発作業の既定は依然として **Lv2**。

Lv1-8 が「**レビュー強度**を上げる」のに対し、Lv0 は「**実装の主体**を Claude → DS/Qwen に切り替える」コストレーン。scaffold / 定型実装 / pytest 量産 / docstring 追加 / 機械的リファクタ等、**量産的で判断が薄い作業**を委譲して Claude 本体のトークン消費・コンテキスト消費を抑える（Antigravity Plugin の「実行委譲」相当の思想）。

**Lv0 が機能する前提（重要・誤解されやすい）**:
- **Claude/Codex のサブスククォータを「有限資源」とみなす場合に節約効果**が出る（量産作業を外部委譲してクォータ温存）
- **サブスククォータが余っているなら Lv2 のほうが安定かつ安い**（Claude 自前は実費 ¥0、Codex/Gemini もサブスク内）
- **Lv0 は実費発生**（DS / Qwen は従量課金、scaffold 5 ファイル概算 ¥1〜5）
- → **動機**: 「サブスククォータ温存」or「Claude 本体 context の節約」なら Lv0、「総合コスト最小」だけなら Lv2 の方が安いこともある

**向く対象**:
- 複数ファイルの scaffold（API / CLI / ETL の雛形）
- 似たパターンの繰り返し実装（CRUD、データ変換）
- pytest テストケースの量産（仕様から N 件生成）
- docstring / 型ヒントの一括追加
- 機械的リネーム・import 整理
- 単一ファイルでも **100 行以上** の量産的 scaffold（API ハンドラ 1 本、テスト 1 ファイル 20 ケース等）

**向かない対象 → Lv1-2 へ誘導**:
- **想定総作業量 < 100 行 かつ 対象ファイル数 < 2**（小修正は委譲の往復コストが勝つ）
- 設計判断が重い変更（DB 設計・状態管理方式・セキュリティ）
- 既存仕様との微妙な整合性が必要なバグ修正

### Step 2-0A: 委譲計画（Claude 本体）

1. 仕様・対象を Claude が読み、**ファイル単位 / 関数単位** にユニット分解
2. 各ユニットを割振:
   - **DS coder** (`deepseek_coder.py --role coder`): 推論寄り・複雑なロジック・データ変換
   - **Qwen coder** (`qwen_advisor.py --role coder`): 実装寄り・scaffold・テスト量産
   - **Claude 自前**: 設計判断・既存コードとの精密な整合・少量修正
3. **小ライン警告**: 委譲対象の **想定総作業量**（追加+変更の合計行数）と **対象ファイル数** をユニット分解時に見積もる。以下を満たす場合は AskUserQuestion で「Lv0 はオーバースペック。Lv1-2 を推奨」と確認して中断 or 続行:
   - 想定総作業量 < 100 行 **AND** 対象ファイル数 < 2
   - つまり「1 ファイル 100 行以上の scaffold」「2 ファイル以上の量産」のいずれかなら OK、両方該当しない小修正だけ弾く
4. 計画を表で提示し AskUserQuestion で承認（**外部 API 送信前の秘匿チェック必須**）:

| # | ユニット | 担当 | 想定行数 | ファイル | 送信不可情報チェック |
|---|---|---|---|---|---|

- **送信不可情報チェック**: API キー / パスワード / 顧客個人情報 / 社内 DB 接続文字列 / 未公開仕様 等が **既存コード抜粋やプロンプト** に含まれないか目視確認。含まれる場合は伏字化（既存メモリ feedback_redact_credentials_before_llm 参照）
- DS は中国本土サーバ、Qwen は DashScope International（Singapore or US Virginia、`QWEN_BASE_URL` で切替）。送信先リージョンが業務 repo の制約に合うか確認

### Step 2-0B: 並列委譲実行（1 メッセージで Bash N 個）

承認後、DS / Qwen にコード生成依頼。

**ユニーク化（必須・複数回実行や他作業との衝突回避）**: 主 context で `RUN=$(date +%Y%m%d_%H%M%S)` を生成し、すべての委譲ファイル名に `_${RUN}` を付ける。

**プロンプト準備（ユニットごとに 1 ファイル・対象言語を必ず明示）**:

```bash
RUN=$(date +%Y%m%d_%H%M%S)
cat > "C:/tmp-ai/delegate_unit1_${RUN}.txt" <<'EOF'
以下の仕様に従ってコードを生成してください。
AGENTS.md / CLAUDE.md の規約に従う（shebang 禁止、Python なら encoding="utf-8" 明示と型ヒント、Python 3.12）。

[対象言語]
<Python / JS / TS / Go / Bash 等 — 必ず明示。DS/Qwen の coder ロールは多言語対応だが、未指定だと Python に偏る>

[仕様]
<1〜3 段落>

[既存コード抜粋（参考・最小限、5KB 以下推奨）]
<関連関数の本体のみ>

[出力]
コードのみ（説明文不要・コード断片で完結させる）。
EOF
```

**並列起動（1 メッセージで Bash N 個・out/err/status 3 ファイル wrapper）**:

並列で出力を確実に分離回収するため、各ユニットを **サブシェル `( ... )` で包んで stdout / stderr / exit code を別ファイルに保存** する。`>` で stdout だけリダイレクトする旧形式は `[DS Usage]`（stderr 出力）が落ちる + 並列で stderr が混線するため使わない。

```bash
# Bash #1: ユニット1 → DS
( python "C:/ClaudeCode/.claude/tools/deepseek_coder.py" --role coder \
    "C:/tmp-ai/delegate_unit1_${RUN}.txt" \
    > "C:/tmp-ai/delegate_unit1_${RUN}.out" \
    2> "C:/tmp-ai/delegate_unit1_${RUN}.err"; \
  echo $? > "C:/tmp-ai/delegate_unit1_${RUN}.status" )

# Bash #2: ユニット2 → Qwen
( python "C:/ClaudeCode/.claude/tools/qwen_advisor.py" --role coder \
    "C:/tmp-ai/delegate_unit2_${RUN}.txt" \
    > "C:/tmp-ai/delegate_unit2_${RUN}.out" \
    2> "C:/tmp-ai/delegate_unit2_${RUN}.err"; \
  echo $? > "C:/tmp-ai/delegate_unit2_${RUN}.status" )
```

**回収手順（Step 2-0C 直前で必ず実行）**:

```bash
for u in unit1 unit2; do
  STATUS=$(cat "C:/tmp-ai/delegate_${u}_${RUN}.status" 2>/dev/null || echo "?")
  USAGE=$(grep '^\[\(DS\|Qwen\) Usage\] 今回:' "C:/tmp-ai/delegate_${u}_${RUN}.err" 2>/dev/null | head -1)
  echo "[${u}] exit=${STATUS} ${USAGE}"
done
```

**使用量表示（必須・転記）**:
- `.err` ファイルから `[DS Usage] 今回:` / `[Qwen Usage] 今回:` 行を抽出し、Step 2-0C の手前で **そのまま表示**（料金可視化）
- `.status` の値が 0 以外のユニットはエラーとして下記の規約で扱う

**並列時の usage 累計の注意**: `deepseek_coder.py` / `qwen_advisor.py` のセッション累計 JSON は読み書き排他がないため、**同一ツールを複数ユニットで並列起動すると累計が lost update し得る**。今回ユニット単体の usage は `.err` から正確に取れるので集計は問題なし。セッション累計値は参考程度に。

**エラー時の扱い（Lv0 専用ルール・全体規約からの例外）**:

各ユニットの `.status` を見て exit code 別に判定:

| exit code | Lv0 専用ルール | 残り成功ユニットの扱い |
|---|---|---|
| 10 / 20 / 40 (auth / quota / network) | **即中断・全ユニット破棄** | 破棄（仕様の整合が崩れるため・全体規約と一致） |
| 30 / 50 (timeout / invalid input) | **該当ユニットだけ中断** | **Step 2-0C で部分適用可**（ユーザー確認必須・受領表に明示） |
| 1 (その他) | **即中断** + `.err` を Step 2-0C で表示 | ユーザー判断 |

**Lv0 のみ部分適用を許容する理由**: Lv0 の本質は「複数ユニット並列で量産」。1 ユニットの timeout/invalid input で残り全部を捨てると委譲費用が無駄になる。一方 auth/quota/network はシステム的問題なので全破棄が正解（全体規約と一致）。**Lv1-8 では部分適用は許容されない**（後述「認証エラー検出時の挙動」セクション参照）。

### Step 2-0C: 受領 + 適用（Claude 本体）

1. 生成物を `Read` で受領
2. **品質チェック**（受領時点で一読・委譲先のコードを鵜呑みにしない）:
   - 仕様との整合性
   - AGENTS.md / CLAUDE.md 規約準拠（shebang なし / `encoding="utf-8"` / 型ヒント）
   - 不要な抽象化・幻覚インポート・存在しない API 呼び出し
3. 対象ファイルに適用:
   - 既存ファイル編集前は **必ずバックアップ**（`cp file file.bak_$(date +%Y%m%d_%H%M%S)`）
   - 新規ファイルは `900.ClaudeCode/<サブフォルダ>/` 配下（CLAUDE.md フォルダルール厳守）
4. Step B 相当の検証（実 import / パス確認）を実施

### Step 2-0D: 規模依存レビュー（自動判定）

判定は **実際の `git diff` の +追加行数**（intent-to-add 含む・後述）で行う:

| diff +追加行数 | レビュー方針 |
|---|---|
| 100 行以上 | **Codex medium 1 回**で差分レビュー（Lv4-7 共通の Step C と同じ要領）・既定実行 |
| 50〜99 行 | **Claude が判断して既定は実行する**（品質側に倒す・聞かない） |
| 50 行未満 | **省略**（Claude の品質チェックのみ） |

（ちょうど 50 行 → 中段の任意レビュー、ちょうど 100 行 → 上段の Codex 実行）

**重要 1**: `git diff` 単独は **untracked file の中身を出さない**。scaffold で新規ファイル作成が主の Lv0 では、`git add -N` で intent-to-add してから diff を取らないと **Codex に空の差分を渡してレビュー成立しない**。

**重要 2**: Codex は `cwd=C:/tmp-ai` で起動するため、git コマンドは **必ず対象 repo の root を `-C` で明示** する。さもないと「C:/tmp-ai は git repo ではない」エラー or 別 repo の diff を拾う事故が起きる。

```bash
# 0. 対象 repo の root を特定（Step 2-0C で書き込んだ repo の任意のパスから）
REPO_ROOT=$(git -C "<Step 2-0C で作業した repo の任意のパス>" rev-parse --show-toplevel)
echo "REPO_ROOT=$REPO_ROOT"

# 1. 新規ファイルを intent-to-add（scaffold は新規ファイル中心なので必須）
git -C "$REPO_ROOT" add -N "<Step 2-0C で作成した新規ファイル...>"
# untracked を一括で含めたい場合（Windows / 空白入りパスにも安全な -z + -0 形式）:
git -C "$REPO_ROOT" ls-files -z -o --exclude-standard | xargs -0 -r -- git -C "$REPO_ROOT" add -N --

# 2. diff を取る（cwd 非依存）— 空差分は assert
git -C "$REPO_ROOT" diff > "C:/tmp-ai/delegate_diff.patch"
[ -s "C:/tmp-ai/delegate_diff.patch" ] || { echo "ERROR: delegate_diff.patch が空。git add -N の対象と REPO_ROOT を確認してください"; exit 1; }

# 3. Codex に読ませる
mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && codex exec -c model_reasoning_effort="medium" --sandbox read-only --skip-git-repo-check "委譲生成コードの差分レビュー。バグ・設計・規約逸脱を厳密評価。まず C:/tmp-ai/delegate_diff.patch を読む。日本語回答。" < /dev/null
```

**git add -N の限界（明示）**:
- バイナリファイルや生成物（lockfile / `.png` / `.jpg` 等）は diff に出ても意味が薄い → `.gitignore` 対象は除外される
- サブモジュール内ファイルは別 repo 扱いなので `-C` のパスを使い分ける必要あり
- 大量の新規ファイル（例: scaffold 50 ファイル）で diff が巨大化したら Codex のコンテキスト制限に当たる → ユニット分割を見直す

**🔴 検出時の自動修正**:
- 主体は **Claude 本体が書き直す**（Lv5 の Step C2 と同仕様）。同じ DS/Qwen への再委譲は **しない**（同じ間違いを繰り返しやすいため）
- 1 周のみ → 改善なし or 新規 🔴 で停止しユーザー判断
- 「Claude が書き直す」分 Lv0 のコスト節約効果は減るが、**品質ゲート優先**で割り切る
- 連続して 🔴 が出る対象は Lv0 が向かない兆候 → 次の Step 2-0E でユーザーに「Lv2 で再実行」を提案する

### Step 2-0E: 最終まとめ（Step D 準拠 + 委譲情報）

通常の Step D に **「委譲サマリ」セクション** を加える:

- 委譲ユニット数: N 件
- 担当内訳: DS X 件 / Qwen Y 件 / Claude 自前 Z 件
- 受領品質問題: あり / なし（あれば内訳）
- 自前で書き直したユニット: あれば一覧
- 費用集計（💰 セクション）は **通常の Step D と同じ**（DS / Qwen の生成 + Codex のレビュー）

```bash
cp <最終報告.md> "C:/tmp-ai/cgd_lv0_$(date +%Y%m%d_%H%M%S).md"
```

### Lv0 のガードレール（Antigravity 4 原則の取り込み）

- **分岐点の上で委譲**: 想定総作業量 < 100 行 **AND** ファイル数 < 2 は Lv1-2 へ誘導（Step 2-0A で警告）
- **コンテキストを薄く保つ**: 入力プロンプトは「仕様 + 関連コード抜粋（要点のみ）」。全ファイルを渡さない（Lv7 の関連関数抽出と同じ要領）
- **単発バッチ**: 1 メッセージで複数ユニットを並列起動（往復削減）
- **差分のみレビュー**: Step 2-0D は `git diff` のみ（`git add -N` で新規ファイルも diff に含める）。全ファイルは渡さない
- **受領物の検証は必須**: 委譲先のコードを鵜呑みにせず、Claude が一読して品質チェック
- **設計判断は委譲しない**: DB 設計・状態管理方式・セキュリティ要件は Claude / Lv4 以上に任せる
- **失敗時は中断 → Lv2 推奨**: 受領品質が連続 NG / 🔴 自動修正 1 周しても解決しない場合、**Lv0 を中断してユーザーに「Lv2 で再実行」を提案**。Claude が裏で延々書き直すフォールバックは **取らない**（取ると Lv0 の意義が消えるため）

---

## Lv1: Codex のみ

### Step 2-1A: Codex level 決定（自動・聞かない）

Step 1 の「Codex reasoning の自動選択」表で決める（既定 `medium`）。決めた値は宣言に含める。

### Step 2-1B: Codex 単独実行

```bash
mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && codex exec -c model_reasoning_effort="<low|medium|high>" --sandbox read-only --skip-git-repo-check "<Codex プロンプト>" < /dev/null
```

### Step 2-1C: 3 列レビュー表で出力

| 指摘（🔴/🟠/🟡＋根拠1行） | Codex | Claude採用 | 対応 |
|---|---|---|---|

- Claude採用: ✅採用 / ⏭️スキップ / 🔄部分採用
- 表の前後に **総評（1〜3行）** と **次アクション（箇条書き）** を必ず添える

**Lv1 はここで終了**（実装・再レビューなし）。次アクションは Claude Code 本体が必要に応じて実行。

---

## Lv2: Codex + DeepSeek 並列（既定。Gemini はオプトインで追加可）

### Step 2-2A: Codex level 決定（自動・聞かない）

Step 1 の「Codex reasoning の自動選択」表で決める（既定 `medium`）。

Gemini を追加参加させる指示（「Geminiも」「C+G」等）があった場合、**観点も Claude が対象から判断して決める**（要約 / 原因特定 / 参考情報収集 / 比較評価）。指示がなければ Gemini は呼ばない。

### Step 2-2B: 並列起動（既定は **1 メッセージで Bash 2 個**、Gemini併用時は3個）

```bash
# Bash #1（Codex）
mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && codex exec -c model_reasoning_effort="<low|medium|high>" --sandbox read-only --skip-git-repo-check "<Codex プロンプト>" < /dev/null

# Bash #2（DeepSeek reviewer）— プロンプトを先にファイル化してパス渡し
#   cat > "C:/tmp-ai/review_input.txt" <<'EOF' ... <レビュー対象+観点> ... EOF を先に書く
python "C:/ClaudeCode/.claude/tools/deepseek_coder.py" --role reviewer "C:/tmp-ai/review_input.txt"

# Bash #3（Gemini）— Gemini オプトイン時のみ追加起動
python "C:/ClaudeCode/.claude/tools/gemini_advisor.py" --role reviewer "C:/tmp-ai/review_input.txt"
```

`cd "C:/tmp-ai"` は日本語 CWD 文字化け回避。Codex は `< /dev/null` でハング防止（必須）。
DS/Gemini とも **プロンプトは必ずファイルに書いてパス渡し**（Windows の CP932 で日本語 argv/stdin が壊れるため）。DS は `DEEPSEEK_API_KEY`、Gemini（オプトイン時のみ）は `GEMINI_API_KEY` が要る。

### Step 2-2C: 統合表で出力（既定5列、Gemini併用時6列）

既定（Codex + DeepSeek）:

| 指摘（🔴/🟠/🟡＋根拠1行） | Codex | DeepSeek | Claude採用 | 対応 |
|---|---|---|---|---|

Gemini オプトイン時はこの表に Gemini 列を1本追加した6列表にする:

| 指摘（🔴/🟠/🟡＋根拠1行） | Codex | DeepSeek | Gemini | Claude採用 | 対応 |
|---|---|---|---|---|---|

- 「指摘」列に **根拠1行を内包**（横長を避ける）
- 表の前後に **総評（1〜3行）** と **次アクション（箇条書き）** を必ず添える

**Lv2 はここで終了**（実装・再レビューなし）。次アクションは Claude Code 本体が必要に応じて実行。

---

## Lv3: Codex + DeepSeek「2社×2視点」4レビュー（技術+批評、実装なし）

2026-07 に再設計。旧 Lv3（Lv2+実装+検証+Codex再レビュー）は「使いどころが思い浮かばない」との判断で廃止し、**実装フェーズを持たないレビュー専用レベル**に作り直した。Lv1/Lv2 と同じく **ここで終了**する（Step A〜D の共通フローには入らない）。

Codex と DeepSeek の **2社それぞれに、技術レビューと批評レビューの2視点**を求める。「2社×2視点＝4レビュー」が Lv3 の本質:

| | 技術視点（バグ・設計・セキュリティ） | 批評視点（辛口ユーザー視点・あるべき論） |
|---|---|---|
| Codex | 通常のコードレビュー | Codex に批評プロンプトで依頼 |
| DeepSeek | `--role reviewer` | `--role critic` |

技術視点は severity（🔴/🟠/🟡）、批評視点は困り度（高/中/低）と、Lv2 / 既存の「critic 観点」セクションと同じ2つの評価軸をそのまま使う。**全 Lv 共通の「critic 観点」オプション（Claude本体+DS critic）とは別物**（Lv3 は Codex/DeepSeek 双方が技術・批評の両方を担う設計であり、Lv3 選択時は全 Lv 共通 critic 観点を重ねて追加する必要は薄い。同時に指示された場合のみ Claude 本体の critic 観点も追加してよい）。

### Step 2-3A: Codex level 決定（自動・聞かない）

Step 1 の「Codex reasoning の自動選択」表で決める（既定 `medium`・技術レビューと批評レビューの両方に同じ値を適用）。

### Step 2-3B: 入力ファイル準備（先に1回だけ作る・4件で共有）

対象（差分／ファイル絶対パス／貼り付けテキスト）は技術視点・批評視点で同じものを見せる。視点の違いはプロンプト側で作る。

```bash
cat > "C:/tmp-ai/review_input.txt" <<'EOF'
以下の差分／実装をレビューしてください。

[対象（絶対パスまたは内容）]
<差分内容または絶対パス>

[背景・狙い]
<1〜3 行>
EOF
```

### Step 2-3C: 4件並列起動（**1 メッセージで Bash 4 個**）

```bash
# Bash #1（Codex 技術レビュー）
mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && codex exec -c model_reasoning_effort="<low|medium|high>" --sandbox read-only --skip-git-repo-check "まず C:/tmp-ai/review_input.txt の全文を読み、バグ・設計上の懸念・セキュリティ・副作用・既存仕様との整合性を厳密にレビューしてください。必要なら対象実ファイルも読んでよい。日本語で回答。" < /dev/null

# Bash #2（DeepSeek 技術レビュー）
python "C:/ClaudeCode/.claude/tools/deepseek_coder.py" --role reviewer "C:/tmp-ai/review_input.txt"

# Bash #3（Codex 批評レビュー）
mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && codex exec -c model_reasoning_effort="<low|medium|high>" --sandbox read-only --skip-git-repo-check "まず C:/tmp-ai/review_input.txt の全文を読んでください。あなたは辛口の評価者です。技術的な正しさ（バグの有無）ではなく『使う人が困らないか』『本来この仕様はどうあるべきか』の観点で、遠慮なく否定的に評価してください。次の2つの立場を併せ持ってください: (1) ITに疎い現場担当者 — 実際に使うときの使いにくさ・わかりにくさ・手数の多さ・エラー時の困りごとを利用者の生の言葉で指摘する。(2) 熟練ITアーキテクト — 『本来この仕様はどうあるべきか』を理想形から逆算し、現状の妥協・場当たり対応・本質を外した設計・優先度の誤りを批判する。出力は次の構造で: 1.現場の不満（各項目に困り度: 高/中/低を付ける） 2.あるべき論とのギャップ 3.そもそも論（この機能は本当に要るか） 4.辛口総評（1〜2行で断言）。擁護・肯定・『概ね良い』は禁止。技術的なバグ指摘には深入りしない。日本語で回答。" < /dev/null

# Bash #4（DeepSeek 批評レビュー）
python "C:/ClaudeCode/.claude/tools/deepseek_coder.py" --role critic "C:/tmp-ai/review_input.txt"
```

`cd "C:/tmp-ai"` は日本語 CWD 文字化け回避。Codex は `< /dev/null` でハング防止（必須）。DS は `DEEPSEEK_API_KEY` が要る。

**使用量表示（必須・転記、DS 2 回分とも）**:
- DS スクリプトが stderr に `[DS Usage] 今回: ... / 累計: ...` を呼出しごとに 2 行出力（reviewer / critic それぞれ）
- Claude はこの結果を **そのまま Step 2-3D の手前でユーザー向けに表示**（料金可視化目的・省略禁止）

**並列段の認証エラー時**:
4 件のいずれか一つでも認証エラーを返したら **即中断**（後述「認証エラー検出時の挙動」に従う）。欠けたまま統合表を作らない。

### Step 2-3D: 技術レビュー表 + 批評レビュー表で出力

**技術レビュー**（Lv2 と同型）:

| 指摘（🔴/🟠/🟡＋根拠1行） | Codex | DeepSeek | Claude採用 | 対応 |
|---|---|---|---|---|

**批評レビュー**（既存「critic 観点」と同型・評価軸は困り度）:

| 観点（困り度 高/中/低） | Codex（批評） | DeepSeek（批評） | 採用 |
|---|---|---|---|

- 技術レビュー表の「指摘」列に **根拠1行を内包**（横長を避ける）
- 批評レビュー表は severity ではなく **困り度** で表現する（技術OKでもユーザー視点NGを可視化する目的のため）
- 表の前後に **総評（技術・批評あわせて1〜3行）** と **次アクション（箇条書き）** を必ず添える

**Lv3 はここで終了**（実装・検証・再レビューなし）。次アクションは Claude Code 本体が必要に応じて実行。実装まで進めたい場合はユーザーに実装可否を確認してから Step A 以降を手動で実行する（Lv3 に自動遷移の仕組みはない）。

---

## Lv4: フル相談（直列）+ 実装 + 検証 + Codex 再レビュー

**既定（Gemini なし）は Step 2-4A〜B を Claude 単独の初期整理に短縮**し、Step 2-4C（DS+Qwen 並列 advisor）から実質スタートする。Gemini オプトイン時（「Geminiも」等の明示指示）のみ、旧来通り Gemini の直列案出しを先頭に挟む。

### Step 2-4A: 初期案の整理

**既定**: Claude 自身が対象を読み、**複数の実装案・設計案の方向性を 1〜3 個**、概要・メリット・デメリット・実装難度を簡潔に整理して提示する（外部呼び出しなし）。

**Gemini オプトイン時**: 代わりに Gemini に案出しを依頼する。

```bash
# 案出しプロンプトを gemini_input.txt に書いてからパス渡し（--role advisor）
mkdir -p "C:/tmp-ai"
cat > "C:/tmp-ai/gemini_input.txt" <<'EOF'
<案出しプロンプト（下の雛形）>
EOF
python "C:/ClaudeCode/.claude/tools/gemini_advisor.py" --role advisor "C:/tmp-ai/gemini_input.txt"
```

**プロンプト雛形**:
```
以下の[テーマ]について、複数の実装案・設計案を提示してください。
案ごとに「概要・メリット・デメリット・実装難度」を簡潔に整理してください。
日本語で回答。

[対象]
<差分／ファイル内容／説明文>
```

### Step 2-4B: Claude が検討

**既定**: Step 2-4A で Claude 自身が出した案について、追加の懸念点・改善余地を洗い出し、**第1推し案**を仮決めする（Step 2-4C の DS/Qwen 別案依頼への入力にする）。

**Gemini オプトイン時**: Claude Code 自身が Gemini 出力を読み、ユーザー向けに表示:
- Gemini が提示した案の一覧（番号付き）
- 各案について Claude が見た **追加の懸念点・改善余地**
- Claude の現時点での **第1推し案**

### Step 2-4C: DS と Qwen に別案依頼（並列・advisor モード）

DeepSeek（推論寄り）と Qwen3-Coder-Plus（実装寄り）に**並列で**別案を求める。
両者は **同じ入力プロンプト** を共有してよい（system prompt が違うので出力傾向は分かれる）。

**入力ルール（DS / Qwen 共通）**:
- 原文/差分は **絶対パス** で参照（コピペで肥大化させない）
- Claude の検討結果は **要約版**（500 字以内）を渡す。Gemini オプトイン時は Gemini の検討結果要約（500字以内）も追加で渡す
- 総入力は **2KB 以下** を目安（タイムアウト・品質低下回避）
- スクリプトは **絶対パス**で呼ぶ（CWD は `C:/tmp-ai` のため相対パス不可）
- 両者で同じ `advisor_prompt.txt` を再利用する（重複生成しない）

**プロンプト準備**（先に1回だけ作る・既定＝Gemini行なし）:

```bash
cat > "C:/tmp-ai/advisor_prompt.txt" <<'EOF'
以下の設計テーマについて、Claude が検討した結果を踏まえ、
**根本的に別アプローチ**を 1〜3 個提示してください。
既出案の改良ではなく、別の発想を求めます。

[原文/差分パス]
<絶対パス>

[Claude の検討要約（500字以内）]
<要約>
EOF
```

Gemini オプトイン時は `[Claude の検討要約]` の前に `[Gemini の案要約（500字以内）]` ブロックを追加する。

**並列起動（1 メッセージで Bash 2 個）**:

```bash
# Bash #1（DeepSeek — 推論寄り別案）
python "C:/ClaudeCode/.claude/tools/deepseek_coder.py" --role advisor "C:/tmp-ai/advisor_prompt.txt"

# Bash #2（Qwen3-Coder-Plus — 実装寄り別案）
python "C:/ClaudeCode/.claude/tools/qwen_advisor.py" --role advisor "C:/tmp-ai/advisor_prompt.txt"
```

両者とも `ROLE_PROMPTS["advisor"]` の構造（別案 / 見落とし / 採否コメント）で返す。
DS は推論ベースで「根本発想」を、Qwen はコーダー視点で「実装観点の別アプローチ」を出すように system prompt で誘導済み。

**使用量表示（必須・転記、両方とも）**:
- DS スクリプトが stderr に `[DS Usage] 今回: ... / 累計: ...` を 2 行出力
- Qwen スクリプトが stderr に `[Qwen Usage] 今回: ... / 累計: ...` を 2 行出力
- Claude は両者の実行結果のこの 2 行を **そのまま Step 2-4D の手前でユーザー向けに表示**（料金可視化目的・省略禁止）
- セッション累計は別ファイルで管理（DS: `.deepseek_usage_session.json` / Qwen: `.qwen_usage_session.json`）。最終呼出から 4 時間で自動リセット、手動リセットは各々 `--reset-session`
- 円換算レートは既定 1USD=150JPY。環境変数で上書き可能（DS: `DEEPSEEK_USD_TO_JPY` / Qwen: `QWEN_USD_TO_JPY`）

**並列段の認証エラー時**:
DS / Qwen のどちらか一方でも認証エラーを返したら即中断（後述「認証エラー検出時の挙動」に従う）。
片肺で続行しない・自動切替もしない。

### Step 2-4D: Claude が再検討

**既定**: DS 別案 + Qwen 別案を統合し、ユーザー向けに表示:

| # | 出所 | 案概要 | Claude評価 |
|---|---|---|---|
| 1 | DS別案 | ... | ... |
| 2 | Qwen別案 | ... | ... |

**Gemini オプトイン時**: 上表の先頭に `Gemini` 行を追加する（Gemini 案 + DS 別案 + Qwen 別案の3行）。

その上で **Claude の統合推し案**（各案のハイブリッドも可）を 1 つ決める。
DS と Qwen で似た案が出た場合は **収束したシグナル**として扱い、推し案の信頼度を上げる根拠にする。
逆に大きく食い違う場合は **トレードオフが大きい設計判断**としてユーザーに明示する。

### Step 2-4E: Codex に最終レビュー依頼

統合推し案を Codex にレビューさせる。reasoning level は原則 **medium**、設計判断が重い場合は **high**。

```bash
cat > "C:/tmp-ai/codex_prompt.txt" <<'EOF'
以下の実装方針を、バグ・設計上の懸念・セキュリティ観点・改善点で厳密にレビューしてください。
日本語回答。プロジェクトの AGENTS.md に従う。

[統合推し案]
<Step 2-4D の結論>

[対象ファイル（参考・絶対パス）]
<絶対パス>
EOF

mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && codex exec -c model_reasoning_effort="medium" --sandbox read-only --skip-git-repo-check "まず C:/tmp-ai/codex_prompt.txt の全文を読み、記載の実装方針を厳密にレビュー。必要なら対象実ファイルも読んでよい。日本語で回答。" < /dev/null
```

### Step 2-4F: Claude 相談まとめ（既定6列、Gemini併用時7列統合表）

既定:

| # | 指摘/論点（🔴/🟠/🟡＋根拠1行） | DS | Qwen | Codex | Claude最終判断 |
|---|---|---|---|---|---|

Gemini オプトイン時は `DS` 列の前に `Gemini` 列を追加した7列表にする。

表の前後に必ず添える:
1. **採用方針** — 最終的にどの案で進めるか（1〜3行）
2. **次アクション** — 実装する項目（箇条書き、ファイル・行・修正方針）

```bash
cp <相談まとめ.md> "C:/tmp-ai/cgd_lv4_$(date +%Y%m%d_%H%M%S).md"
```

### Step 2-4G: 実装許可確認

`AskUserQuestion`:
```
1. 実装する（→ Step A へ）
2. 実装せず終了
3. 修正して実装（次アクション差し替え後）
```

「実装まで一気に」等が指示済みならスキップして Step A へ。

### Step 2-4H〜K: 共通フロー実行

Step A（実装）→ Step B（検証）→ Step C（Codex 再レビュー・差分のみ）→ Step D（最終まとめ）

🔴 が検出されても **Step C2 は走らない**。報告のみで Step D へ。

---

## Lv5: Lv4 + 🔴 重大指摘の自動修正 1 周

Step 2-4A〜H までは Lv4 と同一。Step C の後に **Step C2** を追加。

### Step C2: 🔴 重大指摘の自動修正ループ（Lv5〜Lv8 共通・**最大 1 周**）

Step C の表で **🔴 重大指摘**（前述定義に該当）が 1 つ以上 ✅採用 になった場合のみ実行:

1. 該当箇所を Step A と同じ要領で修正（**バックアップ必須**）
2. Step B（検証）を再実行
3. Step C（Codex 再レビュー）を再実行

> **周回数の経緯（2026-08-05）**: 一度 Lv7 / Lv8 を 2 周に引き上げたが、Lv8 セルフレビューの批評パスから
> 「**1 周で足りないというデータが無いまま決めている**」「収束条件も安全弁も無いまま周回だけ増やすと事故が 2 倍になる」
> と指摘され、**1 周に差し戻した**。テレメトリで実測できる状態になったので、
> 「1 周で収束しなかった回」が積み上がってから上げ直す。

**安全弁**:
- 自動で回す上限は **1 周まで**。**2 周目以降は絶対に自動で回さない**
- 1 周回しても **同じ🔴指摘が再発した（改善なし）** 場合は **即座に停止**してユーザー判断を仰ぐ
- 別の新しい🔴が出た場合も **2 周目には進まず**ユーザー判断を仰ぐ
- 🟠 重要 / 🟡 注意のみの場合は自動ループせず、Step D の「未対応指摘」に記載
- Lv8 で自動修正の対象になるのは **技術レビュー表の 🔴 のみ**（批評レビュー表の指摘は困り度ベースで severity を持たないため対象外）

**「同じ🔴か」の判定**: 指摘文の表現一致で判定しない（言い回しが変わるだけで別物扱いになり安全弁が効かない）。
**`location`（file:line）+ 指摘の対象箇所**で同一性を見る。

**周回の記録（省略禁止）**: 自動修正を回したら、Step D の報告に必ず次を書く:

| 項目 | 例 |
|---|---|
| 1 周目で解消した 🔴 | 2 件中 1 件 |
| 残った 🔴 | 1 件（location 付き） |
| 停止理由 | 改善なし / 新規🔴 / 上限到達 |

この記録が「1 周で足りているのか」を後から判断する材料になる。**周回数を上げ直すのはこのデータが溜まってから。**

---

## Lv6: Codex+DS+Qwen 3 者並列レビュー + 実装 + 検証 + Codex 再レビュー + 🔴 自動修正 1 周（Gemini はオプトインで4者化）

Lv4-5 の DS / Qwen は **advisor 役（別案出し）** だが、実運用で「DS の別案が採用されることがほとんどなく実効性が薄い」という課題を受けて新設した最重量級レベル。advisor 段（[DS+Qwen 並列]→Codex の直列）を **廃止**し、代わりに **DS / Qwen を Codex と同じ原データ（差分・ファイル）を直接受け取る並列レビュアー** として運用する。

「複数の独立した目で同じコードを見る」をスケールアップさせた構成。同じ指摘が複数 AI から挙がれば信頼度が高い（収束シグナル）、1 者だけの指摘は false positive を疑う、という運用ができる。**既定は Codex+DS+Qwen の3者**。Gemini は「Geminiも」「4者で」等の明示指示があった時だけ4者目として追加する（2026-07 に API エラー多発のため既定から外した）。

> ## 🚨 Lv6 のレビュー段は **Workflow 実行が必須**（inline 禁止）
>
> **Step 2-6A → 2-6A2（WF 起動）→ 2-6C の順で進む。** 下の「Step 2-6B（フォールバック）」の inline 手順は
> **WF が使えない時だけ**の退避路であり、通常は読まなくてよい。
>
> - 手順の詳細は後述「**Workflow 経由実行（Lv6-WF / Lv7-WF）**」節（起動手順・raw 検証ガード・入力ファイルの罠）
> - inline で `codex exec` を叩くと **PreToolUse hook (`cgd_wf_gate.py`) が deny する**。
>   ゲートは Step 1 の `cgd_usage_log.py record --level 6` が自動で張っている
> - 2026-08-05 に「WF があるのに 2 セッションが独立に inline を実行」した事故があり、機械的な強制を入れた

### Step 2-6A: レビュー観点を決定（自動・聞かない）

Codex reasoning は Step 1 の自動選択表で決める（既定 `medium`）。

Gemini オプトイン時の観点も Claude が対象から判断して決める（要約 / 原因特定 / 参考情報収集 / 比較評価）。

DS / Qwen は両者とも `--role reviewer` 固定で呼び出す（advisor との切替はしない・Lv6 の本質）。

### Step 2-6A2: Workflow 起動（**正規ルート・必須**）

```bash
RUN=$(date +%Y%m%d_%H%M%S)
NONCE=$(python "C:/ClaudeCode/.claude/hooks/cgd_wf_gate.py" nonce)   # ゲート通過用（必須）
cat > "C:/tmp-ai/cgd_in_${RUN}.txt" <<'EOF'
<差分 + 背景 + 評価観点 + 対象ファイル絶対パス>
EOF
```

```
Workflow({ scriptPath: "C:/ClaudeCode/.claude/skills/cgd/workflows/cgd_lv6_review.js",
           args: { input_path: "C:/tmp-ai/cgd_in_<RUN>.txt", codex_reasoning: "<low|medium|high>",
                   label: "<対象名>_<RUN>", wf_nonce: "<nonce>" } })       // Gemini オプトイン時は include_gemini: true を追加
```

完了後は戻り値の `label` を確認 → `table_md` を描画 → 🔴 は `raw_log_paths` で検証（後述ガード）→ **Step 2-6C へ**。
実装（Step A）に進む前に、Step C の inline 再レビューが弾かれないようゲートを解除する:

```bash
python "C:/ClaudeCode/.claude/hooks/cgd_wf_gate.py" disarm
```

### Step 2-6B（フォールバック）: inline 並列起動 — ⚠️ 通常は使わない

**この節は Workflow が使えない時の退避路。** 使う場合はコマンド先頭に `CGD_WF_RUN=1` を付けてゲートを意図的に迂回し、**迂回した理由をユーザーに必ず伝えること**。既定は **1 メッセージで Bash 3 個**（Gemini併用時4個）。

入力データは参加者全員が **同じ原文（差分／ファイル絶対パス／貼り付けテキスト）** を受け取る。advisor 段でやっていた「Claude 検討要約」は **渡さない**（要約による情報損失を避け、各 AI が独自に原文を解釈する）。

**プロンプト準備**（先に 1 回だけ作る・全員で共有）:

```bash
cat > "C:/tmp-ai/review_input.txt" <<'EOF'
以下の差分／実装をレビューしてください。
バグ・設計上の懸念・セキュリティ・副作用・既存仕様との整合性を厳密に評価してください。
日本語で回答。AGENTS.md / CLAUDE.md がある場合はそれに従う。

[探索の上限 — 必須]
追加で開いてよい実ファイルは**最大5個まで**。超えるなら読まずに「情報不足: <欲しいファイル>」と書いて終えること（探索1回で約3,000トークン消費するため）。

[対象（絶対パスまたは内容）]
<差分内容または絶対パス>

[背景・狙い]
<1〜3 行>
EOF
```

**並列起動（既定・3個）**:

```bash
# Bash #1（Codex）
mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && codex exec -c model_reasoning_effort="<low|medium|high>" --sandbox read-only --skip-git-repo-check "まず C:/tmp-ai/review_input.txt の全文を読み、記載の差分・対象・評価観点に従ってレビュー。必要なら対象実ファイルも読んでよい。日本語で回答。" < /dev/null

# Bash #2（DeepSeek reviewer — 推論寄り）
python "C:/ClaudeCode/.claude/tools/deepseek_coder.py" --role reviewer "C:/tmp-ai/review_input.txt"

# Bash #3（Qwen reviewer — 実装寄り）
python "C:/ClaudeCode/.claude/tools/qwen_advisor.py" --role reviewer "C:/tmp-ai/review_input.txt"

# Bash #4（Gemini）— Gemini オプトイン時のみ追加起動
python "C:/ClaudeCode/.claude/tools/gemini_advisor.py" --role reviewer "C:/tmp-ai/review_input.txt"
```

`cd "C:/tmp-ai"` は日本語 CWD 文字化け回避。`< /dev/null` はハング防止（必須）。

**使用量表示（必須・転記、DS と Qwen 両方）**:
- DS スクリプトが stderr に `[DS Usage] 今回: ... / 累計: ...` を 2 行出力
- Qwen スクリプトが stderr に `[Qwen Usage] 今回: ... / 累計: ...` を 2 行出力
- Claude は両者の実行結果のこの 2 行を **そのまま Step 2-6C の手前でユーザー向けに表示**（料金可視化目的・省略禁止）

**reasoning を厳しくしたい場合**:
- DS: `--model deepseek-v4-pro` で高精度モデルに切替（料金 3 倍弱、2026-07-24 deepseek-reasoner 廃止に伴い移行）
- Qwen: 既定の `qwen3-coder-plus` で十分。`qwen3-coder-flash` で軽量化も可（精度は落ちる）
- Codex: `model_reasoning_effort="high"` に上げる（タイムアウト 600s）

**並列段の認証エラー時**:
**その回で参加している全員**（既定は Codex/DS/Qwen の3者、Gemini併用時は4者）のいずれか一つでも認証エラーを返したら **即中断**（後述「認証エラー検出時の挙動」に従う）。欠員のまま続行しない・自動切替もしない。Lv6 の価値は「参加者全員の視点」なので 1 者でも欠けたら Lv2 や Lv4 と同じ意味になる。

### Step 2-6C: 統合レビュー表で出力（既定5列、Gemini併用時6列）

既定（Codex+DS+Qwen）:

| 指摘（🔴/🟠/🟡＋根拠1行） | Codex | DS | Qwen | Claude 採用 | 対応 |
|---|---|---|---|---|---|

Gemini オプトイン時は `Codex` 列の後に `Gemini` 列を追加した6列表にする。

- 各 AI 列は ✅指摘あり / ❌指摘なし / 🔄部分一致 を記入
- **収束シグナル**: 同じ指摘が 2 者以上から挙がった場合は信頼度が高い扱いとし Claude 採用に強く反映
- **乖離シグナル**: 1 者だけが挙げた指摘は false positive の可能性も含めて吟味（特に Gemini / Qwen は推測寄りの指摘が混じる傾向）
- 表の前後に **総評（1〜3 行）** と **次アクション（箇条書き）** を必ず添える

```bash
cp <相談まとめ.md> "C:/tmp-ai/cgd_lv6_$(date +%Y%m%d_%H%M%S).md"
```

### Step 2-6D: 実装許可確認

`AskUserQuestion`:
```
1. 実装する（→ Step A へ）
2. 実装せず終了
3. 修正して実装（次アクション差し替え後）
```

「実装まで一気に」等が指示済みならスキップして Step A へ。

### Step 2-6E〜H: 共通フロー実行

Step A（実装）→ Step B（検証）→ Step C（Codex 再レビュー・差分のみ）→ **Step C2（🔴 自動修正ループ最大 1 周・Lv5 と同仕様）** → Step D（最終まとめ）

Step C2 の自動修正ループは **Lv5 の Step C2 セクションをそのまま適用** する（上限 1 周・改善なし／新規 🔴／上限到達で停止）。

---

## Lv7: Codex 集中 (medium + high 多重) + 補助 (DS/Qwen) 4 者並列レビュー + 実装 + 検証 + Codex 再レビュー + 🔴 自動修正 1 周（Gemini はオプトインで5者化）

Lv6 動作テストの観察知見から派生した **最深掘り構成**。Lv6 は「横並びレビュー」だが、実運用で **Codex が integration バグ（関数間の暗黙の前提違反・スコープを跨いだ状態管理破綻）の単独検出に圧倒的に強い**（sandbox read-only でファイル全体を探索できる）一方、DS/Qwen の reviewer は diff だけだと表層的になりがちだった。Lv7 はこのアンバランスを **Codex を medium と high で多重化** して底上げし、DS/Qwen には **関連関数を Claude が事前抽出して** 渡して補助役の質を上げる。既定は Codex×2+DS+Qwen の4者。Gemini は「Geminiも」「5者で」等の明示指示があった時だけ5者目の補助として追加する。

「複数の独立した目で同じコードを見る」（Lv6）より、「深い目 2 つ重ね + 補助で横から検査」（Lv7）のアプローチ。

> ## 🚨 Lv7 のレビュー段は **Workflow 実行が必須**（inline 禁止）
>
> **Step 2-7A → 2-7B（関連関数抽出）→ 2-7C（入力準備）→ 2-7C2（WF 起動）→ 2-7E の順で進む。**
> 下の「Step 2-7D（フォールバック）」の inline 手順は **WF が使えない時だけ**の退避路であり、通常は読まなくてよい。
>
> - 手順の詳細は後述「**Workflow 経由実行（Lv6-WF / Lv7-WF）**」節
> - inline で `codex exec` を叩くと **PreToolUse hook (`cgd_wf_gate.py`) が deny する**。
>   ゲートは Step 1 の `cgd_usage_log.py record --level 7` が自動で張っている
> - **なぜ必須か**: Codex high の生出力は 1 回で 100KB〜1.2MB に達し、主 context を 1〜2 回で枯渇させる。
>   WF は生出力を subagent に閉じ込め、主 context 流入を **約 94% 削減**する（実測）
> - ⚠️ **WF は Codex のトークン／クォータ消費は減らさない**（1 回 5〜10 万 tok は WF でも同じ）。
>   減るのは主 context の汚染だけ。トークン消費側の対策は別問題（対象ファイルを列挙して探索範囲を絞る等）

### Step 2-7A: レビュー観点を確認

Codex は **medium + high の 2 並列固定**（Lv7 の本質なので reasoning level 確認は不要）。
DS / Qwen は **`--role reviewer` 固定**。

Gemini オプトイン時の観点は Claude が対象から判断して決める（要約 / 原因特定 / 参考情報収集 / 比較評価）。聞かない。

### Step 2-7B: 関連関数の事前抽出（Claude 本体作業）

Lv6 と違い、DS / Qwen には **差分 + 関連関数抜粋** を渡す。Codex（オプトイン時は Gemini も）はファイル絶対パスを渡して自分で読みに行かせる（sandbox 探索能力を活用）。

**Claude 本体の事前作業手順**:

1. `git diff` または `diff -u` で差分を取得し、変更 hunk の行範囲を特定
2. 各 hunk の周辺で **関連する関数の境界を grep + Read で抽出**
   - JS: `function ` / `const X = function` / `X = (...) =>` の宣言行
   - Python: `def ` `class ` の宣言行
   - GAS: `function ` の宣言行
3. 関連関数の本体を Read で取得し、`C:/tmp-ai/lv7_related_funcs.txt` に結合保存
4. ファイル全体が小さい（< 500 行）場合は **全文をそのまま抜粋** として使う

**抽出量の目安**:
- 差分行数の **5〜10 倍程度**（差分 50 行 → 抜粋 300〜500 行）が現実的
- 5KB 未満を目標（DS/Qwen の argv 制限・トークン制約・タイムアウト回避）
- 大きすぎる場合は「変更 hunk 直近 ± 30 行 + 呼び出し元関数 1〜2 個」に絞る

### Step 2-7C: 入力ファイル準備（先に 1 回だけ作る）

**Codex（オプトイン時は Gemini も）用入力**（ファイルパス渡し・自分で読みに行ける）:

```bash
cat > "C:/tmp-ai/lv7_codex_input.txt" <<'EOF'
以下の差分／実装を厳密にレビューしてください。
バグ・設計上の懸念・セキュリティ・副作用・既存仕様との整合性、特に
**関数間の integration バグ**（暗黙の前提違反・スコープを跨いだ状態管理破綻・
呼出経路ごとの副作用差異・catch ブロックでの throw 握り潰し等）を重点的に評価してください。
日本語で回答。AGENTS.md / CLAUDE.md がある場合はそれに従う。

[探索の上限 — 必須]
**関連関数の抜粋は下に同梱済みです。** 追加で開いてよい実ファイルは **最大 5 個まで**。
それを超えて必要になったら、読み進めずに「**情報不足: <欲しいファイル/関数>**」と書いて
その指摘を終えてください（Claude 側が次のラウンドで抜粋を追加します）。

理由（実測）: Codex の消費は `14,000 + 0.75×入力バイト + 約3,000×探索回数` で近似できます。
入力 5.4KB のレビューで 29 回探索し 103,686 トークン使った実例があり、
探索が全体の 8 割を占めていました。上限に達したら「足りない」と言う方が、
黙って読み続けるより有用です。

[対象ファイル絶対パス]
<絶対パス>

[変更概要]
<1〜3 行>

[関連関数抜粋 (Claude が事前抽出・Step 2-7B の成果物)]
EOF
cat "C:/tmp-ai/lv7_related_funcs.txt" >> "C:/tmp-ai/lv7_codex_input.txt"
cat >> "C:/tmp-ai/lv7_codex_input.txt" <<'EOF'

[差分内容 — unified diff]
EOF
cat <diff-file> >> "C:/tmp-ai/lv7_codex_input.txt"
```

> **変更点（2026-08-05）**: 以前は Codex にパスだけ渡して「自分で grep+Read してよい」と
> 広く探索させていた。実測でこれが消費の主因（8 割）と判明したため、
> **DS/Qwen 用に既に抽出している抜粋を Codex にも同梱し、追加探索に上限を設ける**方式に変えた。

**DS / Qwen 用入力**（差分 + 関連関数抜粋・API なのでファイルアクセス不可）:

```bash
cat > "C:/tmp-ai/lv7_aux_input.txt" <<'EOF'
以下の差分と関連関数抜粋を厳密にレビューしてください。
バグ・設計上の懸念・セキュリティ・副作用・既存仕様との整合性を評価してください。
特に関連関数抜粋を読んで、差分が他関数の前提を破っていないか確認してください。
日本語で回答。

[対象ファイル絶対パス参考]
<絶対パス・API なので直接アクセス不可>

[変更概要]
<1〜3 行>

[関連関数抜粋 (Claude が事前抽出)]
EOF
cat "C:/tmp-ai/lv7_related_funcs.txt" >> "C:/tmp-ai/lv7_aux_input.txt"
cat >> "C:/tmp-ai/lv7_aux_input.txt" <<'EOF'

[差分内容 — unified diff]
EOF
cat <diff-file> >> "C:/tmp-ai/lv7_aux_input.txt"
```

### Step 2-7C2: Workflow 起動（**正規ルート・必須**）

Step 2-7C の入力ファイルは **ユニークサフィックス付き**で作る（固定名は他セッションに上書きされ、別プロジェクトをレビューする事故が実際に起きた）:

```bash
RUN=$(date +%Y%m%d_%H%M%S)
NONCE=$(python "C:/ClaudeCode/.claude/hooks/cgd_wf_gate.py" nonce)   # ゲート通過用（必須）
# Codex 用 = lv7_codex_input.txt 相当 / aux 用 = lv7_aux_input.txt 相当をユニーク名で作る
cp "C:/tmp-ai/lv7_codex_input.txt" "C:/tmp-ai/cgd_codex_${RUN}.txt"
cp "C:/tmp-ai/lv7_aux_input.txt"   "C:/tmp-ai/cgd_aux_${RUN}.txt"
```

```
Workflow({ scriptPath: "C:/ClaudeCode/.claude/skills/cgd/workflows/cgd_lv7_review.js",
           args: { input_path: "C:/tmp-ai/cgd_codex_<RUN>.txt",
                   aux_input_path: "C:/tmp-ai/cgd_aux_<RUN>.txt",
                   label: "<対象名>_<RUN>", wf_nonce: "<nonce>" } })       // Gemini オプトイン時は include_gemini: true を追加
```

完了後は戻り値の `label` を確認（`target` ならパース失敗を疑う）→ `table_md` を描画 → 🔴 は `raw_log_paths` で検証 → **Step 2-7E へ**。
実装（Step A）に進む前に、Step C の inline 再レビューが弾かれないようゲートを解除する:

```bash
python "C:/ClaudeCode/.claude/hooks/cgd_wf_gate.py" disarm
```

### Step 2-7D（フォールバック）: inline 並列起動 — ⚠️ 通常は使わない

**この節は Workflow が使えない時の退避路。** 使う場合は各コマンド先頭に `CGD_WF_RUN=1` を付けてゲートを意図的に迂回し、**迂回した理由をユーザーに必ず伝えること**。既定は **1 メッセージで Bash 4 個**（Gemini併用時5個）。

```bash
# Bash #1（Codex medium — バランス重視）
mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && codex exec -c model_reasoning_effort="medium" --sandbox read-only --skip-git-repo-check "まず C:/tmp-ai/lv7_codex_input.txt の全文を読み、記載の差分・対象・評価観点に従ってレビュー。必要なら対象実ファイルも読んでよい。日本語で回答。" < /dev/null

# Bash #2（Codex high — 深掘り）
mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && codex exec -c model_reasoning_effort="high" --sandbox read-only --skip-git-repo-check "まず C:/tmp-ai/lv7_codex_input.txt の全文を読み、記載の差分・対象・評価観点に従ってレビュー。必要なら対象実ファイルも読んでよい。日本語で回答。" < /dev/null

# Bash #3（DeepSeek reviewer — 補助・推論寄り）
python "C:/ClaudeCode/.claude/tools/deepseek_coder.py" --role reviewer "C:/tmp-ai/lv7_aux_input.txt"

# Bash #4（Qwen reviewer — 補助・実装寄り）
python "C:/ClaudeCode/.claude/tools/qwen_advisor.py" --role reviewer "C:/tmp-ai/lv7_aux_input.txt"

# Bash #5（Gemini — 補助・全体構造把握）— Gemini オプトイン時のみ追加起動
python "C:/ClaudeCode/.claude/tools/gemini_advisor.py" --role reviewer "C:/tmp-ai/lv7_codex_input.txt"
```

**Codex 多重の注意**:
- ChatGPT サブスククォータを **Codex 2 回分** 消費（同時実行）。Plus/Pro プラン上限を意識する
- reasoning=high は最大 10 分（timeout 600000 必須）
- 結果は別 session_id で独立（共有なし・互いに知らない）

**使用量表示（必須・転記、DS と Qwen 両方）**:
- DS / Qwen の stderr `[DS Usage]` / `[Qwen Usage]` の 2 行を Step 2-7E の手前で表示

**並列段の認証エラー時**:
**その回で参加している全員**（既定は Codex×2+DS+Qwen の4者、Gemini併用時は5者）のいずれか一つでも認証エラーを返したら **即中断**（後述「認証エラー検出時の挙動」に従う）。特に Codex は medium と high の **両方** が認証成功している必要がある（片方失敗で続行しない）。Codex 多重が Lv7 の本質なので 1 つで代用しない。

### Step 2-7E: 統合レビュー表で出力（既定6列、Gemini併用時7列）

既定（Codex×2+DS+Qwen）:

| 指摘（🔴/🟠/🟡＋根拠1行） | Codex(med) | Codex(high) | DS | Qwen | Claude 採用 | 対応 |
|---|---|---|---|---|---|---|

Gemini オプトイン時は `Codex(high)` 列の後に `Gemini` 列を追加した7列表にする。

- 各 AI 列は ✅指摘あり / ❌指摘なし / 🔄部分一致 を記入
- **Codex 多重収束シグナル**: med と high で同じ指摘 → reasoning level に依らない確度高（最強の信頼度）
- **Codex 乖離シグナル**: high のみが指摘 → 深掘り効果で発見された integration バグの可能性。med のみが指摘 → 過剰反応や false positive の可能性も含めて吟味
- **補助からの追加発見**: DS/Qwen（オプトイン時は Gemini も）単独指摘 → Codex 多重の盲点候補（採否は慎重に）
- 表の前後に **総評（1〜3 行）** と **次アクション（箇条書き）** を必ず添える

```bash
cp <相談まとめ.md> "C:/tmp-ai/cgd_lv7_$(date +%Y%m%d_%H%M%S).md"
```

### Step 2-7F: 実装許可確認

`AskUserQuestion`:
```
1. 実装する（→ Step A へ）
2. 実装せず終了
3. 修正して実装（次アクション差し替え後）
```

### Step 2-7G〜J: 共通フロー実行

Step A（実装）→ Step B（検証）→ Step C（Codex 再レビュー・差分のみ・**medium 単独**）→ **Step C2（🔴 自動修正ループ最大 1 周）** → Step D（最終まとめ）

**Step C を Codex 多重で回さない理由**:
- 再レビュー段は差分のみで規模が小さく、medium で十分な深さが出る
- Codex high をもう 1 回回すとサブスククォータ消費とタイムアウトのリスクが上がるが、得るものは少ない
- Lv7 の本質は **初回レビューでの深掘り**（Step 2-7D）にあり、再レビューはコスト圧縮を優先する

---

## Lv8: Lv7 + Codex(high)/DeepSeek 批評パス追加 — 技術の最深掘り + 複眼批評 + 🔴 自動修正 1 周（Gemini はオプトインで7者化）

2026-07 新設。**Lv7（技術レビューの最深掘り）に、Lv3 で確立した批評視点（現場担当者の使い勝手 + あるべき論）を重ねた最重量級レベル**。技術面は Lv7 と完全に同一（Codex medium+high の多重 + DS/Qwen 補助）。そこに **Codex(high) と DeepSeek の2者だけ**、追加で批評レビューを依頼する。

**なぜ全員でなく Codex(high) + DeepSeek の2者に絞るか**:
- **Codex(high)**: Lv7 の知見どおり reasoning が深いほど integration バグだけでなく設計思想レベルの疑問も拾いやすい。技術用に呼んでいる high をもう1回、視点だけ変えて再利用する（新規セッションなので技術レビューの結果に引きずられない）
- **DeepSeek**: Lv3 で `--role critic` の批評運用実績が既にある（プロンプト・出力構造とも流用可能）
- Qwen/Codex(medium)/Gemini まで全員に批評を広げると呼出数が指数的に増え（既定6→10）、費用・時間に対して収穫逓減と判断。**必要なら個別に追加できる余地は残すが、既定はこの2者**

**既定参加者(6者・呼出6本)**: Codex medium(技術) + Codex high(技術) + DeepSeek(技術補助) + Qwen(技術補助) + Codex high(批評) + DeepSeek(批評)
**Gemini併用時(7者・呼出7本)**: 上記 + Gemini(技術補助)

> ## 🚨 Lv8 のレビュー段は **Workflow 実行が必須**（inline 禁止）
>
> **Step 2-8A → 2-8B（関連関数抽出）→ 2-8C（入力準備）→ 2-8C2（WF 起動）→ 2-8E の順で進む。**
> 下の「Step 2-8D（フォールバック）」の inline 手順は **WF が使えない時だけ**の退避路であり、通常は読まなくてよい。
>
> - **Lv8 は全レベル中いちばん Workflow が要る構成**。Codex を 3 回（med技術 + high技術 + high批評）呼ぶため
>   生出力の合計が最大になり、inline で回すと主 context が 1 回で枯れる
> - inline で `codex exec` を叩くと **PreToolUse hook (`cgd_wf_gate.py`) が deny する**。
>   ゲートは Step 1 の `cgd_usage_log.py record --level 8` が自動で張っている
> - ⚠️ WF が減らすのは主 context の汚染だけで、**Codex のトークン／クォータ消費は減らない**

### Step 2-8A: レビュー観点を確認

Codex は **medium + high の 2 並列固定**（技術用、Lv7 と同じ）。DS / Qwen は **`--role reviewer` 固定**（技術用）。Codex(high) の批評パスと DeepSeek の `--role critic` は **常時固定**（reasoning level やロールの確認は不要）。

Gemini オプトイン時の観点は Claude が対象から判断して決める（Lv7 と同じ）。聞かない。

### Step 2-8B: 関連関数の事前抽出（Claude 本体作業）

**Lv7 の Step 2-7B と同一手順**（`git diff` → hunk 周辺の関数境界を grep+Read で抽出 → `C:/tmp-ai/lv8_related_funcs.txt` に結合保存。差分の5〜10倍、5KB未満が目安）。批評パスにも同じ抽出結果を再利用する（技術と批評で対象を分けない・見る角度だけ変える）。

### Step 2-8C: 入力ファイル準備（先に1回だけ作る・技術・批評で共有）

**Codex（オプトイン時は Gemini も）用入力**（ファイルパス渡し・自分で読みに行ける）:

```bash
cat > "C:/tmp-ai/lv8_codex_input.txt" <<'EOF'
以下の差分／実装を厳密にレビューしてください。
バグ・設計上の懸念・セキュリティ・副作用・既存仕様との整合性、特に
**関数間の integration バグ**（暗黙の前提違反・スコープを跨いだ状態管理破綻・
呼出経路ごとの副作用差異・catch ブロックでの throw 握り潰し等）を重点的に評価してください。
日本語で回答。AGENTS.md / CLAUDE.md がある場合はそれに従う。

[探索の上限 — 必須]
**関連関数の抜粋は下に同梱済みです。** 追加で開いてよい実ファイルは **最大 5 個まで**。
それを超えて必要になったら、読み進めずに「**情報不足: <欲しいファイル/関数>**」と書いて
その指摘を終えてください（Claude 側が次のラウンドで抜粋を追加します）。

理由（実測）: Codex の消費は `14,000 + 0.75×入力バイト + 約3,000×探索回数` で近似できます。
入力 5.4KB のレビューで 29 回探索し 103,686 トークン使った実例があり、
探索が全体の 8 割を占めていました。上限に達したら「足りない」と言う方が、
黙って読み続けるより有用です。

[対象ファイル絶対パス]
<絶対パス>

[変更概要]
<1〜3 行>

[関連関数抜粋 (Claude が事前抽出・Step 2-8B の成果物)]
EOF
cat "C:/tmp-ai/lv8_related_funcs.txt" >> "C:/tmp-ai/lv8_codex_input.txt"
cat >> "C:/tmp-ai/lv8_codex_input.txt" <<'EOF'

[差分内容 — unified diff]
EOF
cat <diff-file> >> "C:/tmp-ai/lv8_codex_input.txt"
```

**DS / Qwen 用入力**（差分 + 関連関数抜粋・API なのでファイルアクセス不可。技術・批評とも共通で使う）:

```bash
cat > "C:/tmp-ai/lv8_aux_input.txt" <<'EOF'
以下の差分と関連関数抜粋を厳密にレビューしてください。
バグ・設計上の懸念・セキュリティ・副作用・既存仕様との整合性を評価してください。
特に関連関数抜粋を読んで、差分が他関数の前提を破っていないか確認してください。
日本語で回答。

[対象ファイル絶対パス参考]
<絶対パス・API なので直接アクセス不可>

[変更概要]
<1〜3 行>

[関連関数抜粋 (Claude が事前抽出)]
EOF
cat "C:/tmp-ai/lv8_related_funcs.txt" >> "C:/tmp-ai/lv8_aux_input.txt"
cat >> "C:/tmp-ai/lv8_aux_input.txt" <<'EOF'

[差分内容 — unified diff]
EOF
cat <diff-file> >> "C:/tmp-ai/lv8_aux_input.txt"
```

### Step 2-8C2: Workflow 起動（**正規ルート・必須**）

Step 2-8C の入力ファイルを **ユニークサフィックス付き**にしてから起動する（固定名は他セッションに上書きされる）:

```bash
RUN=$(date +%Y%m%d_%H%M%S)
NONCE=$(python "C:/ClaudeCode/.claude/hooks/cgd_wf_gate.py" nonce)   # ゲート通過用（必須）
cp "C:/tmp-ai/lv8_codex_input.txt" "C:/tmp-ai/cgd_codex_${RUN}.txt"
cp "C:/tmp-ai/lv8_aux_input.txt"   "C:/tmp-ai/cgd_aux_${RUN}.txt"
```

```
Workflow({ scriptPath: "C:/ClaudeCode/.claude/skills/cgd/workflows/cgd_lv8_review.js",
           args: { input_path: "C:/tmp-ai/cgd_codex_<RUN>.txt",
                   aux_input_path: "C:/tmp-ai/cgd_aux_<RUN>.txt",
                   label: "<対象名>_<RUN>", wf_nonce: "<nonce>" } })       // Gemini オプトイン時は include_gemini: true を追加
```

戻り値は **`tech_table_md` と `critic_table_md` の 2 表**（Lv6-WF / Lv7-WF は 1 表なのでここが違う）。批評パスの findings は severity ではなく **困り度（高/中/低）** を持つ。
完了後は `label` を確認 → 2 表を描画 → 🔴 は `raw_log_paths` で検証 → **Step 2-8E へ**。

#### 🚨 結果を採用する前に `collect` を叩く（省略禁止・2026-08-12 追加）

**レビュアーの成否は agent の自己申告**（`executed` / `findings` / `raw_log_path`）で、
WF はそれを検証していない。codex がタイムアウトや deny で死んでも
`{executed:true, findings:[]}` と返せば統合表に「**指摘なし**」と出る。
生ログが 1 バイトも無くても、そのままでは誰も気づけない。

そこで pv と同じ形で、**成果物の判定を Python に固定**した。

```bash
# WF 起動の前: run を登録し、期待する生ログのパスを確定させる
python "C:/ClaudeCode/.claude/tools/cgd_plan.py" build --level 8 --label "<対象名>" --input "C:/tmp-ai/cgd_codex_<RUN>.txt" --aux "C:/tmp-ai/cgd_aux_<RUN>.txt"
# → WORKFLOW_ARGS の JSON をそのまま Workflow の args に渡す（キー名を手で書かない）

# WF 完了後: **主 context が自分で叩く**。これが唯一の非 LLM ゲート
python "C:/ClaudeCode/.claude/tools/cgd_plan.py" collect --run <RUN>
```

- 判定するのは「生ログが在るか・200 バイト以上か・見出し/箇条書きが 3 行以上か」だけ。
  **内容の妥当性は測れない**（測ろうとすると LLM に判定させることになり設計が壊れる）
- **exit 0 を確認してから結果を採用する**
- 忘れても気づけるようにしてある: `build` が `<run>/.pending_verify` を置き、
  `UserPromptSubmit` hook (`cgd_verify_reminder.py`) が未検証の run を毎ターン提示する。
  印が消えるのは `collect` が exit 0 したときだけで、**WF 側からは消さない**
- 詰まったら `cgd_plan.py doctor --run <RUN>`（どのレビュアーの生ログが欠けたか出る）
実装（Step A）に進む前にゲートを解除する:

```bash
python "C:/ClaudeCode/.claude/hooks/cgd_wf_gate.py" disarm
```

### Step 2-8D（フォールバック）: inline 並列起動 — ⚠️ 通常は使わない

**この節は Workflow が使えない時の退避路。** 使う場合は各コマンド先頭に `CGD_WF_RUN=1` を付けてゲートを意図的に迂回し、**迂回した理由をユーザーに必ず伝えること**。既定は **1 メッセージで Bash 6 個**（Gemini併用時7個）。

```bash
# Bash #1（Codex medium — 技術・バランス重視）
mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && codex exec -c model_reasoning_effort="medium" --sandbox read-only --skip-git-repo-check "まず C:/tmp-ai/lv8_codex_input.txt の全文を読み、記載の差分・対象・評価観点に従ってレビュー。必要なら対象実ファイルも読んでよい。日本語で回答。" < /dev/null

# Bash #2（Codex high — 技術・深掘り）
mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && codex exec -c model_reasoning_effort="high" --sandbox read-only --skip-git-repo-check "まず C:/tmp-ai/lv8_codex_input.txt の全文を読み、記載の差分・対象・評価観点に従ってレビュー。必要なら対象実ファイルも読んでよい。日本語で回答。" < /dev/null

# Bash #3（DeepSeek reviewer — 技術・補助・推論寄り）
python "C:/ClaudeCode/.claude/tools/deepseek_coder.py" --role reviewer "C:/tmp-ai/lv8_aux_input.txt"

# Bash #4（Qwen reviewer — 技術・補助・実装寄り）
python "C:/ClaudeCode/.claude/tools/qwen_advisor.py" --role reviewer "C:/tmp-ai/lv8_aux_input.txt"

# Bash #5（Codex high — 批評。新規セッションで技術レビューとは独立に評価させる）
mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && codex exec -c model_reasoning_effort="high" --sandbox read-only --skip-git-repo-check "まず C:/tmp-ai/lv8_codex_input.txt の全文を読んでください。あなたは辛口の評価者です。技術的な正しさ（バグの有無）ではなく『使う人が困らないか』『本来この仕様はどうあるべきか』の観点で、遠慮なく否定的に評価してください。次の2つの立場を併せ持ってください: (1) ITに疎い現場担当者 — 実際に使うときの使いにくさ・わかりにくさ・手数の多さ・エラー時の困りごとを利用者の生の言葉で指摘する。(2) 熟練ITアーキテクト — 『本来この仕様はどうあるべきか』を理想形から逆算し、現状の妥協・場当たり対応・本質を外した設計・優先度の誤りを批判する。出力は次の構造で: 1.現場の不満（各項目に困り度: 高/中/低を付ける） 2.あるべき論とのギャップ 3.そもそも論（この機能は本当に要るか） 4.辛口総評（1〜2行で断言）。擁護・肯定・『概ね良い』は禁止。技術的なバグ指摘には深入りしない。日本語で回答。" < /dev/null

# Bash #6（DeepSeek critic — 批評）
python "C:/ClaudeCode/.claude/tools/deepseek_coder.py" --role critic "C:/tmp-ai/lv8_aux_input.txt"

# Bash #7（Gemini — 技術・補助・全体構造把握）— Gemini オプトイン時のみ追加起動
python "C:/ClaudeCode/.claude/tools/gemini_advisor.py" --role reviewer "C:/tmp-ai/lv8_codex_input.txt"
```

**Codex 多重の注意（Lv7 からの変更点）**:
- ChatGPT サブスククォータを **Codex 3 回分**消費（medium技術 + high技術 + high批評。Lv7 の2回分より増える）。Plus/Pro プラン上限を強く意識する
- reasoning=high は最大10分（timeout 600000 必須）× 2 回（技術・批評）走る
- 結果は3つとも別 session_id で独立（技術と批評のCodex(high)は互いに影響しない）

**使用量表示（必須・転記、DS 2 回分・Qwen 1 回分とも）**:
- DS スクリプトが reviewer / critic それぞれで stderr に `[DS Usage] 今回: ...` を出力。Qwen も同様
- Claude はこの結果を **そのまま Step 2-8E の手前でユーザー向けに表示**（料金可視化目的・省略禁止）

**並列段の認証エラー時**:
**その回で参加している全員**（既定6者、Gemini併用時7者）のいずれか一つでも認証エラーを返したら **即中断**（後述「認証エラー検出時の挙動」に従う）。特に Codex は medium技術・high技術・high批評の **3回とも** 認証成功している必要がある。

### Step 2-8E: 技術レビュー表 + 批評レビュー表で出力

**技術レビュー**（Lv7 と同型・既定6列、Gemini併用時7列）:

| 指摘（🔴/🟠/🟡＋根拠1行） | Codex(med) | Codex(high) | DS | Qwen | Claude 採用 | 対応 |
|---|---|---|---|---|---|---|

Gemini オプトイン時は `Codex(high)` 列の後に `Gemini` 列を追加。判定方法（収束/乖離シグナル・over-attribution禁止）は Lv7 の Step 2-7E をそのまま適用する。

**批評レビュー**（Lv3 と同型・評価軸は困り度）:

| 観点（困り度 高/中/低） | Codex(high)（批評） | DeepSeek（批評） | 採用 |
|---|---|---|---|

- 批評レビュー表は severity ではなく **困り度** で表現する（技術OKでもユーザー視点NGを可視化する目的のため）
- **批評レビューの指摘は 🔴 重大指摘の定義に該当しない**（severity を持たないため）。Step C2 の自動修正ループの対象には入らない（技術表の 🔴 のみが対象）。批評側の採否は Step D の「未対応指摘」欄でユーザー判断に委ねる
- 表の前後に **総評（技術・批評あわせて1〜3行）** と **次アクション（箇条書き）** を必ず添える

```bash
cp <相談まとめ.md> "C:/tmp-ai/cgd_lv8_$(date +%Y%m%d_%H%M%S).md"
```

### Step 2-8F: 実装許可確認

`AskUserQuestion`:
```
1. 実装する（→ Step A へ）
2. 実装せず終了
3. 修正して実装（次アクション差し替え後）
```

### Step 2-8G〜J: 共通フロー実行

Step A（実装）→ Step B（検証）→ Step C（Codex 再レビュー・差分のみ・**medium 単独**）→ **Step C2（🔴 自動修正ループ最大 1 周・対象は技術表の🔴のみ）** → Step D（最終まとめ、批評レビュー表も含めて報告）

**Step C を Codex 多重・批評込みで回さない理由**（Lv7 と同じ判断）:
- 再レビュー段は差分のみで規模が小さく、medium で十分な深さが出る
- Codex high や批評パスをもう1回回すとサブスククォータ消費とタイムアウトのリスクが上がるが、得るものは少ない
- Lv8 の本質は **初回レビューでの技術深掘り + 複眼批評**（Step 2-8D）にあり、再レビューはコスト圧縮を優先する

---

## Workflow 経由実行（Lv6-WF / Lv7-WF / Lv8-WF）— **Lv6/Lv7/Lv8 の正規ルート（必須）**

Lv6 / Lv7 / Lv8 の **レビュー段は Workflow ツールに委譲する（2026-08-05 より必須化）**。Codex high の巨大出力（160KB+）を subagent context に閉じ込め、主 context には構造化 findings + 統合表（数KB）だけ返す方式。

### 🔒 機械的な強制（cgd_wf_gate.py）

2026-08-05 以前は「WF もある」という任意運用だったが、**1 日で Lv7 が 3 件実行され WF は 0 件**（独立した 2 セッションが同じ間違い）という実績が出たため、hook で強制するようにした。同日、未実装だった **Lv8-WF も新規作成して必須化**した（Codex 3 回で最も出力が大きく、本来いちばん WF が要る構成だった）。

**初版は「強制」になっていなかった**（同日の Lv8 セルフレビューで実証）: `bash -c "codex exec ..."` `env X=1 codex exec` `$(codex exec)` `time` `eval` の **6 形式中 5 つが素通り**し、`CGD_WF_RUN=1` という文字列がコマンドのどこかに含まれるだけでゲートが解除され、WF の 1 本目が通った瞬間に以後が無防備になり、単一グローバルファイルなので並行セッションが互いのゲートを破壊していた。

その後、シェル構文を正規表現で解析する方向で 3 周修正したが、**塞ぐたびに Codex 再レビューが新しい構文を見つけた**（`sudo`/`if`/`while` → 単一引用符 → `!` / `{ }`）。Lv8 批評の「**正規表現ゲートは迂回と誤検知を構造的に同時に抱える**」という指摘どおりだったので、解析をやめる方式に切り替えた。

| いつ | 何が起きるか |
|---|---|
| Step 1 で `cgd_usage_log.py record --level 6\|7\|8 --session <SID>` | ゲートが張られ、**バイパス用の `wf_nonce` が発行**される（90 分 TTL）。失敗したら ❌ ERROR で目立つように出る |
| ゲート中に **`codex` という語を含む Bash** | PreToolUse hook が **一律 deny**。シェルを解析しないので迂回構文が存在しない |
| WF 内の codex（`CGD_WF_RUN=<nonce> ... codex ...`） | nonce がその起動に束縛されている時だけ通過。**通過しても解除しない** |
| WF 完了後・Step A に進む前 | `python "C:/ClaudeCode/.claude/hooks/cgd_wf_gate.py" disarm` を**明示実行**（自動解除はしない） |
| 意図的な迂回 | 正しい nonce を付ける。**迂回理由をユーザーに必ず伝える** |

**設計の要点**:
- **シェルを解析しない**: `codex` の語が入っていれば起動でなくても止める。`bash -lc '...'` も heredoc も `!` も `{ }` も関係なく遮断される。**見逃すより過剰に止める方を選んでいる**
- ⚠️ **副作用**: ゲート中は `grep "codex exec" SKILL.md` や **`codex login status`（`cgd_doctor` が叩く）も止まる**。doctor は Step 1 より前に回すか、`disarm` してから回す。deny メッセージにこの旨と逃げ道が書いてある
- **nonce 照合**: `CGD_WF_RUN=<nonce> [他の代入] codex ...` の形のみ有効。文字列の混入や `echo` での先出しでは通らない
- **セッション単位**: `--session <SID>` を付ければ自セッションだけを止める。省略時は全体ゲート（全セッションを止める）。**所有権の移動（claim）はしない** — レースと「claim 後に disarm できないデッドロック」の原因だった
- **fail-closed は限定**: ゲートファイルが「在るのに壊れている」場合だけ遮断する。ファイルが無い場合と hook 自体の例外は通す（例外はトレースを stderr に出す）

状態確認は `cgd_wf_gate.py status`、nonce の取得は `cgd_wf_gate.py nonce`。

### ⚠️ WF が減らすのは context であってトークンではない

WF は **主 context への流入を約 94% 削減**するが、**Codex 側のトークン／クォータ消費は 1 円も減らない**。実測では入力 5.4KB のレビューでも 103,686 tokens 消費しており、**入力サイズとトークン消費に相関がない**（膨張の主因は `--sandbox read-only` での repo 探索）。トークン消費を抑えたい場合は WF ではなく、**対象ファイルを列挙して探索範囲を絞る**方向で手当てする。`.bak_*` が大量にあるディレクトリを読ませると特に膨張する。

**実測効果**（pickorder ScanCheck 差分で検証済・当時は Gemini 込み4者構成での計測。3者既定でも縮小率の傾向は同様）:
- 4者の生出力 計 **210KB**（うち Codex 198KB）が subagent 内に閉じ、主 context 流入は **約12KB**（**94%削減**）
- 費用 ¥1.06（DS+Qwen のみ・Codex/Gemini はサブスク）
- 同一セッションでの Lv6/Lv7 反復が現実的に（インライン版は 1-2 回で context 枯渇）

### スクリプト

| Lv | スクリプト | 構成 |
|---|---|---|
| Lv6-WF | `.claude/skills/cgd/workflows/cgd_lv6_review.js` | 既定 Codex+DS+Qwen 3者並列 → 5列表（`include_gemini:true` で Gemini 追加・4者・6列表） |
| Lv7-WF | `.claude/skills/cgd/workflows/cgd_lv7_review.js` | 既定 Codex(med)+Codex(high)+DS+Qwen 4者並列 → 収束/乖離判定 → 6列表（`include_gemini:true` で Gemini 追加・5者・7列表） |
| Lv8-WF | `.claude/skills/cgd/workflows/cgd_lv8_review.js` | 既定 6者並列（技術4 = Lv7 と同一 + 批評2 = Codex(high)/DS critic）→ **技術表 + 批評表の2表**（`include_gemini:true` で7者）|

### 責務分割（重要）

| 主 context が担当 | Workflow が担当 |
|---|---|
| Step 1 レベル決定（Claude が自動選択・宣言） | 並列レビュー段（agent が内部で codex/gemini/python 起動） |
| レビュー入力ファイルの準備（差分+背景） | 生出力を subagent に閉じ込め → 構造化 findings に圧縮 |
| Lv7: 関連関数の事前抽出（grep+Read） | 認証エラー/欠員チェック（全員成功で次段） |
| 完了後の table_md 描画・🔴 採否判断 | 収束/乖離判定 + 統合表生成 |
| 実装許可（AskUserQuestion）と **Step A〜D の実行** | 生ログを `C:/tmp-ai/cgd_raw_*.md` に保存しパス返却 |

**AskUserQuestion は workflow 内で使えない**ため、レベル決定・実装許可・Step D 確認は主 context に残す（レベル決定は自動なので実質 AskUserQuestion は実装許可のみ）。**Step A（実装）/B（検証）/C（再レビュー）/C2（自動修正）は当面 主 context 実行**（AGENTS.md 規約の対話的監査が必要なため。将来 implement_and_fix workflow 化を検討）。

### 起動手順（ユニーク名方式・衝突回避）

入力ファイルは **ユニークサフィックス付き**にして他作業との衝突を根絶する。args は JSON 文字列で届くが、両スクリプトが冒頭で `JSON.parse` するので `input_path`/`label` が確実に効く（プローブで実証済）。

```bash
# 1. 主context: ユニークサフィックス生成 (Workflow 内は Date.now 禁止なので主context側で date)
RUN=$(date +%Y%m%d_%H%M%S)
NONCE=$(python "C:/ClaudeCode/.claude/hooks/cgd_wf_gate.py" nonce)   # ゲート通過用（必須）   # 例 20260529_185800

# Lv6-WF: 入力をユニーク名で配置
cat > "C:/tmp-ai/cgd_in_${RUN}.txt" <<'EOF'
<差分 + 背景 + 評価観点 + 対象ファイル絶対パス>
EOF

# Lv7-WF: codex用 と aux用 を別ユニーク名で
cat > "C:/tmp-ai/cgd_codex_${RUN}.txt" <<'EOF'
<差分 + 背景 + 対象ファイル絶対パス (Codex は sandbox で読める)>
EOF
cat > "C:/tmp-ai/cgd_aux_${RUN}.txt" <<'EOF'
<差分 + 関連関数抜粋 (DS/Qwen 用)>
EOF
```

```
# 2. Workflow 起動 (label にも RUN を含めると raw も cgd_raw_<reviewer>_<対象>_<RUN>.md でユニーク化)
# Lv6-WF (既定・Gemini なし):
Workflow({ scriptPath: ".../cgd_lv6_review.js",
           args: { input_path: "C:/tmp-ai/cgd_in_<RUN>.txt", codex_reasoning: "medium", label: "<対象名>_<RUN>" } })
# Lv6-WF (Gemini オプトイン時): 上記に include_gemini: true を追加
# Lv7-WF (既定・Gemini なし):
Workflow({ scriptPath: ".../cgd_lv7_review.js",
           args: { input_path: "C:/tmp-ai/cgd_codex_<RUN>.txt", aux_input_path: "C:/tmp-ai/cgd_aux_<RUN>.txt", label: "<対象名>_<RUN>" } })
# Lv7-WF (Gemini オプトイン時): 上記に include_gemini: true を追加

# 3. 完了通知 → 戻り値の label が "<対象名>_<RUN>" なら args 到達OK ("target" ならパース失敗を疑う)
# 4. table_md を描画、🔴 を raw 検証 (下記ガード) → 実装許可 AskUserQuestion → Step A〜D を主contextで実行
```

### 🔴 raw 検証ガード（必須・省略禁止）

**merge agent の収束判定・severity を鵜呑みにしない。🔴 採用前に必ず `raw_log_paths` の該当 raw を主 context で確認する。**

理由（実例）: Lv6-WF 動作確認で merge agent が DS の **🟠「可能性」指摘を「🔴 4者一致の真バグ」に過剰格上げ**（over-attribution）した。raw を grep したところ Codex/Gemini は無指摘、DS は 🟠 だったと判明し、実コード検証で false positive と確定（unfullKeys 無限ループ — `_incrementItemAtIndex` の加算ループは shortageFlag を見ないため再現しない）。

→ **schema 圧縮 + merge 統合は severity/確信度を歪めうる**。raw_log_path 設計はこの検証のために存在する。DS は 5.8KB / Qwen は 2.6KB と軽いので主 context で全文確認可。Codex raw（198KB）は grep で該当箇所だけ抽出する。

### ⚠️ 入力ファイルの罠（必須・Lv7-WF 実走で発覚）

Lv7-WF 実走で **3連鎖事故** が起きた。**起動前後の確認を怠ると別プロジェクトをレビューしても気づけない**:

1. **args 不達**: Workflow に `args` が届かないことがある（実例: `args.label="pickorder-lv7wf"` を渡したのに結果が `label:"target"`=デフォルト値に化けた）。→ スクリプトは `args.input_path` を読めず **デフォルトパスを黙って読む**。
2. **C:/tmp-ai 固定ファイル名の上書き**: 他スキル/セッションが同名ファイルを使い回し、`lv7_codex_input.txt` が別プロジェクト（label designer）のレビュー入力に変質していた。→ デフォルトパスを読んだ結果、pickorder ではなく designer.js をレビューしてしまった。
3. **黙ってデフォルト読み**: args 不達時に halt せずデフォルトを読むため、誤対象に気づけない（Lv6-WF はデフォルトが偶然正しい `review_input.txt` のままで救われていた）。

**対策（実装済 + 必須運用）**:
- ✅ **args 不達は解決済み**: 真因は「Workflow が args を **JSON 文字列**で渡す」こと（プローブ workflow で実証: `typeof args === 'string'`、`JSON.parse` で完全復元可）。両スクリプト冒頭に `JSON.parse` フォールバックを実装済みで `input_path`/`label` が効くようになった。
- ✅ **固定ファイル名の上書きはユニーク名で根絶**: 上記「起動手順」の通り `cgd_in_<RUN>.txt` 等のユニークサフィックス（主 context で `date` 生成）を使う。`label` にも `<RUN>` を含め raw もユニーク化。これで他作業・並行実行と衝突しない。
- **起動後の `label` 確認は継続**: 戻り値の `label` が渡した `<対象名>_<RUN>` と一致するか確認（`target` なら JSON.parse 失敗 = args 経路が壊れた合図）。
- フォールバックのデフォルトパス（`review_input.txt` 等）はスクリプトに残してあるが、**ユニーク名 + args を正規ルート**とする。

### 費用集計

戻り値 `usage[].usage_line`（Gemini/DS/Qwen の `[Usage]` 行）から Step D の費用表を組み立てる。Codex のみサブスクで ¥0（呼出回数のみ記録）。Gemini は API 移行で従量だが AI Studio 無料枠中心で極小。Workflow の usage 集計が手作業転記より正確。

### スコープ判断

- **Lv1-3**: workflow 化しない（元々 30-50KB と軽量・overhead が見合わない）
- **Lv4-5**: 当面インライン（直列相談段の pipeline 化は将来課題）
- **Lv6-8**: review 段の workflow 化は **必須**（主 context 圧迫の主要因がここ。Lv8 は Codex 3 回で最も重い）

---

## critic 観点（辛口ユーザー視点 / あるべき論）— 全 Lv 共通オプション

技術レビュー（Codex/Gemini/DS/Qwen の reviewer）は「バグ・設計・セキュリティ」を見るが、**critic 観点は別軸**:「**使う人が困らないか**」「**本来この仕様はどうあるべきか**」を否定的に評価する。技術的に正しくても使い勝手が悪い・仕様が場当たり、という所を辛口で突く。

### いつ使うか / 自動提案ルール（重要）

critic は **Lv に組み込まず、Lv と直交するオプション**。技術レビュー（Lv1-8）の強度とは別軸（使えるか/あるべきか）。Lv に埋めると DS advisor のように選択肢の奥で埋もれて使われなくなるため、**「使うべき時に Claude が提案する」運用**で能動的に活かす。

**起動契機（3通り）**:
- ユーザーが「辛口で」「ユーザー視点で」「あるべき論で」「現場目線で」「critic」等を指示 → 即有効
- Step 1 の強度決定と同時に **Claude が要否を判断して自動で有効化する**（既定オフ・**ユーザーに聞かない**）
- 有効にした場合は宣言に含める（例: `Lv4 ／ critic: あり（現場担当者が触る画面のため）`）

**Claude が critic を自動で有効にするタイミング**:
以下を検知したら **聞かずに critic 観点を追加**する:
1. **実装前の仕様・設計の検討**（最重要 — 手戻り防止効果が最大。作る前に「使えるか/あるべきか」を潰す）
2. **新機能・UI・画面・操作フローの追加/変更**（現場担当者・エンドユーザーが触る部分）
3. **ユーザー向けメッセージ・エラー文言・確認ダイアログの追加/変更**
4. 技術レビュー（Lv6/Lv7 等）が「ほぼ問題なし」だったが、ユーザーが実際に使う機能の場合（「動くが使えるか」の最終チェック）

**critic を追加しないケース**:
- 純粋なバグ修正・内部リファクタ・ロジックのみの変更（UX に関わらない）
- ライブラリ更新・設定変更・テスト追加など、ユーザー操作に影響しない作業

**作法**:
- 有効/無効どちらでも **宣言に 1 語で明示**する（黙って足さない・黙って省かない）
- ユーザーが「critic は要らない」と言ったら **同一タスク中は二度と付けない**
- 逆に「辛口で」「ユーザー視点で」等の明示指示があれば、上の基準に関係なく必ず付ける

**使えるフェーズ**: 仕様評価（実装前）・コードレビュー（実装後）・単独、どこでも（Lv と直交）。最も価値が高いのは **実装前**。

### 担い手（2者）

| 担い手 | 役割 | 呼び出し |
|---|---|---|
| **Claude 本体** | 現場担当者になりきり使い勝手の不満を生の言葉で + あるべき論 | （本体） |
| **DS critic role** | 推論で「なぜこの仕様」「本来こうあるべき」を批判 | `python "<絶対パス>/deepseek_coder.py" --role critic "<入力ファイル>"` |

- DS critic は深掘りしたいとき `--model deepseek-v4-pro`（コスト3倍弱・あるべき論が深くなる。2026-07-24 deepseek-reasoner 廃止に伴い移行）
- Qwen / Codex は critic に使わない（日本語業務文脈・あるべき論は Claude/DS が適任）

### 実行手順
1. 対象（仕様説明 / 差分 / 画面の説明）を **ファイルに準備**（`C:/tmp-ai/critic_input.txt`。argv 肥大回避・ファイル読ませ方式）
2. DS critic を Bash で呼ぶ:
   ```bash
   python "C:/ClaudeCode/.claude/tools/deepseek_coder.py" --role critic "C:/tmp-ai/critic_input.txt"
   ```
3. Claude 本体も「現場担当者 + あるべき論」で辛口評価する（外部出力を待つ間に並行可）
4. 2者を統合した **critic 表**で出力。DS の `[DS Usage]` 行を費用集計に転記

### 出力フォーマット（技術レビューと別表）

| 観点（困り度 高/中/低） | Claude（現場 / あるべき） | DS critic | 採用 |
|---|---|---|---|

- 列挙する軸: **現場の不満**（使う人視点）/ **あるべき論とのギャップ** / **そもそも論**（要る機能か・優先度）
- severity（🔴🟠🟡）でなく **困り度（高/中/低）** で表現 — 技術 OK でもユーザー視点 NG を可視化する
- 表の前後に **辛口総評（一番の問題を断言・1〜2行）** と **改善の方向（箇条書き）**

### 注意
- **擁護・肯定・「概ね良い」は禁止**。甘い評価は無価値。粗探しに徹する
- 技術バグは reviewer 担当。critic は **使い勝手とあるべき論に集中**（バグに深入りしない）
- 否定で終わらせず、各指摘に **改善の方向を短く添える**（代替案の詳細出しは advisor の担当）
- critic は **採否を強制しない** — 「ユーザーが困る/あるべき姿と違う」という視点を可視化するのが目的。最終判断はユーザー

---

## Step A: 実装フェーズ（Lv4-7 共通）

AGENTS.md / CLAUDE.md ルールを **強制適用**:

- **既存ファイル編集前に必ず `cp <file> <file>.bak_$(date +%Y%m%d_%H%M%S)` でバックアップ**（バックアップなしの編集禁止）
- シバン行（`#!/usr/bin/env python3`）禁止（Windows で `py.exe` が即終了する）
- ファイル読み書きは常に `encoding="utf-8"` 明示
- CP932 コンソール対策: Python ワンライナー先頭に `sys.stdout.reconfigure(encoding="utf-8")`
- 日本語パスへの Edit / Write が失敗する場合は **Python スクリプトを `C:/ClaudeCode/` に書いて実行**
- バッチファイル（.bat）は **CP932** で書く（Write ツールは UTF-8 で書くため文字化け）
- API キー / シークレットを書き込まない・コミットしない

実装は前段で出した「次アクション」の項目順に進める。
TodoWrite で進捗を管理する（項目数が 3 個以上の場合は必須）。

---

## Step B: 検証フェーズ（Lv4-7 共通、CLAUDE.md「コーディング後の必須検証」を強制）

実装ファイルごとに以下 4 項目を確認し、**結果を表形式で必ず報告**:

| # | 検証項目 | 結果 |
|---|---|---|
| 1 | 対象ファイルの import 一覧（先頭 20 行）を確認 | ✅/⚠️ |
| 2 | 実 import で動作確認（`python -c "import sys; sys.path.insert(0, r'PATH'); from <mod> import <fn>; print('OK')"`） | ✅/❌ |
| 3 | パス定数が実在するか（`python -c "from pathlib import Path; p=Path(r'...'); print(p, p.exists())"`） | ✅/❌ |
| 4 | `ast.parse` だけで済ませていない（実 import まで実行した） | ✅ |

**`ast.parse` だけの確認は NG**。必ず実 import まで実行する。

検証 NG（❌）が 1 つでも出たら Step A に戻って **1 回まで自動修正**→再検証。2 回連続で NG なら停止してユーザー判断（無限ループ防止）。

Python 以外（JS / TS / シェル等）の場合は、その言語の実行可能な最小確認（`node --check`、`tsc --noEmit`、`bash -n` 等）に置き換える。

---

## Step C: Codex 再レビュー

> **探索は禁止（2026-08-05）**: 再レビューは差分だけを見る段なので、Codex に実ファイルを
> 探索させない。プロンプトに「**実ファイルは読まないこと。差分と同梱情報だけで判断すること**」
> を必ず入れる。実測で探索は 1 回約 3,000 トークン、初回レビューでは消費の 8 割を占めた。
> 差分レビューでこれを払う価値はない。（Lv4-8 共通、差分のみ・Lv7/Lv8 も medium 単独で OK）

実装した差分を **Codex 単独**でレビューする。Gemini は呼ばない（トークン節約）。

- 対象: 直近の `git diff`（**差分のみ**で全ファイル渡さない・時間とトークン圧縮）
- reasoning: 原則 **medium**（Lv5 でセキュリティに関わる場合は **high**）
- 観点: バグ・設計上の懸念・副作用・既存仕様との整合性・🔴重大指摘定義への該当性

```bash
git diff > "C:/tmp-ai/impl_diff.patch"
mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && codex exec -c model_reasoning_effort="medium" --sandbox read-only --skip-git-repo-check "<差分レビュープロンプト・絶対パスで diff 参照>" < /dev/null
```

結果は **3 列レビュー表**（Lv1 と同じフォーマット）で出力:

| 指摘（🔴/🟠/🟡＋根拠1行） | Codex | Claude採用 | 対応 |
|---|---|---|---|

🔴 が検出された場合の扱いは **Lv による**:
- Lv4: 報告のみ（Step C2 は走らない）
- Lv5: Step C2 へ進む（自動修正ループ **最大 1 周**）
- Lv6: Step C2 へ進む（Lv5 と同仕様）
- Lv7: Step C2 へ進む（Lv5 と同仕様・再レビューも medium 単独）
- Lv8: Step C2 へ進む（Lv5 と同仕様・再レビューも medium 単独・**対象は技術表の🔴のみ**）

周回上限は全レベル 1 周で統一されている。**レベルごとに違うのは「Step C2 に入るか否か」だけ**（Lv4 は入らない）。

---

## Step D: 最終まとめ（共通・必須出力）

以下を 1 つの報告にまとめる:

1. **実装した内容** — 変更ファイル（絶対パス）と主要な変更点を箇条書き
2. **検証結果** — Step B の表
3. **再レビュー結果**（Lv4-8）— Step C の 3 列レビュー表 + 修正ループの記録（Lv5-Lv8 のみ）
   — **周回数（0 / 1）・解消した🔴の件数・残った🔴（location 付き）・停止理由**を必ず書く（Step C2 参照）
4. **未対応指摘**（あれば） — 🟠 / 🟡 で残ったもの・Lv4 で残った 🔴・Lv8 の批評レビュー表の指摘（採否はユーザー判断）
5. **残課題・申し送り事項**（あれば）
6. **💰 費用集計**（**全 Lv 必須・省略禁止**） — 後述「費用集計の出力フォーマット」に従う

```bash
cp <最終報告.md> "C:/tmp-ai/cgd_impl_$(date +%Y%m%d_%H%M%S).md"
```

### 費用集計の出力フォーマット（全 Lv 必須・省略禁止）

Step D の最終報告に **「💰 費用集計」セクション**を必ず含める。
Lv1 でも Codex の呼出回数は記録する（サブスククォータ消費量の把握目的）。
Lv2 以降は DeepSeek（既定第2エンジン）、Lv4-5 / Lv6 / Lv7 / Lv8 は Qwen の従量課金もあるので必須中の必須。Lv8 は Codex 呼出が3回（medium技術+high技術+high批評）・DS呼出が2回（reviewer+critic）になる点に注意。Gemini はオプトインで呼んだ回のみ記載。

**1. 今回のセッションで発生した費用表（必須）**:

| AI | 呼出回数 | 今回入力 tok | 今回出力 tok | 今回費用 |
|---|---|---|---|---|
| Codex | N 回 (どの Step か注記) | — | — | ¥0（ChatGPT サブスク・料金可視化なし） |
| DeepSeek | N 回 | 計 X tok | 計 Y tok | ¥A |
| Qwen | N 回 | 計 X tok | 計 Y tok | ¥B |
| Gemini（オプトイン時のみ） | N 回 | 計 X tok | 計 Y tok | ¥G（従量・極小） |
| **合計（従量課金分）** | — | — | — | **¥A+B(+G)** |

- **呼出回数**: 今回のスキル実行中の Bash 呼出を Claude が記録（Step 2-XX / Step C / Step C2 のうちどこで呼んだか注記）
- **DS / Qwen / Gemini の入力・出力・費用**: stderr の `[DS Usage] 今回:` / `[Qwen Usage] 今回:` / `[Gemini Usage] 今回:` の数値を **複数回呼んだ場合は積算**（Gemini は AI Studio 無料枠中心なので通常 ¥0〜数十銭。呼ばなかった回はそもそも行を出さない）
- **Codex**: サブスク認証で料金可視化不可。呼出回数だけ計上する（無料という意味ではなくクォータを消費している）
- 呼ばなかった AI の行は省略可（ただし合計行は必ず出す）

**2. セッション累計（参考、過去 4 時間以内）**:

```
[Gemini Usage] 累計: ... (このセッション+過去呼出含む)
[DS Usage]     累計: ... (このセッション+過去呼出含む)
[Qwen Usage]   累計: ... (このセッション+過去呼出含む)
```

取得コマンド（必要に応じて Step D 出力前に Bash で実行）:
```bash
python "C:/ClaudeCode/.claude/tools/gemini_advisor.py" --show-session
python "C:/ClaudeCode/.claude/tools/deepseek_coder.py" --show-session
python "C:/ClaudeCode/.claude/tools/qwen_advisor.py" --show-session
```

セッション累計は今回の作業以外（過去 4 時間以内の別作業）を含むため、**当該作業の正確な費用は「1. 今回費用表」が真実のソース**。

**3. 費用観点の所感（任意・1〜3 行）**:

例: 「Lv6 を 1 周フルで回して従量課金 ¥0.99。Codex はサブスク内なので追加コストなし。検出 🔴 4 件に対し費用対効果は高い」

---

**省略禁止の理由**:
- ユーザーは Lv5/Lv6 など高コストレベルの継続判断に費用情報が必要
- セッション内で複数回 Lv6 を回したときの累計把握
- Step 2-XB での「今回」表示と Step D の最終まとめは別目的（前者は即時可視化、後者は確定報告）
- Lv1-3 でも Codex 呼出回数の蓄積は ChatGPT クォータ消費の目安になる

**書き忘れたら**:
ユーザーから「費用の表示がない」と指摘されたら、その時点で `--show-session` を取得して費用表を後追い追加する。同じ指摘を受けないよう Step D 出力前に必ずチェック。

---

## Bash タイムアウト

| ステップ | コマンド | timeout |
|---|---|---|
| Lv2-2B / Lv4-2A(オプトイン時) / Lv6-2B(オプトイン時) / Lv7-2D(オプトイン時) / Lv8-2D(オプトイン時) | gemini | 180000 (3分) |
| Lv2-2B / Lv3-2C | deepseek (reviewer) | 180000 (3分) |
| Lv3-2C | deepseek (critic) | 180000 (3分) |
| Lv4-2C | deepseek (advisor) | 120000 (2分) |
| Lv4-2C | qwen (advisor) | 120000 (2分) |
| Lv6-2B / Lv7-2D / Lv8-2D | deepseek (reviewer) | 180000 (3分) |
| Lv8-2D | deepseek (critic) | 180000 (3分) |
| Lv6-2B / Lv7-2D / Lv8-2D | qwen (reviewer) | 180000 (3分) |
| Lv1-2B / Lv2-2B / Lv3-2C(技術+批評とも) / Lv4-2E / Lv6-2B / Lv7-2D / Lv8-2D / C | codex medium | 300000 (5分) |
| Lv1-2B / Lv2-2B / Lv3-2C(技術+批評とも) / Lv4-2E / Lv6-2B / **Lv7-2D** / **Lv8-2D(技術+批評とも)** / C | codex high | 600000 (10分) |
| Step B | python -c "from <mod> ..." | 60000 (1分) |

---

## 認証エラー検出時の挙動（必須）

Codex CLI / DeepSeek API / DashScope (Qwen) API、および **Gemini をオプトインした回は Gemini API も含め**、その回参加している API の **いずれか一つでも認証エラーを返したら、そこで即中断**する。残り段を片肺で続行しない・自動で別ツールに切り替えない。

### exit code 規約（gemini_advisor.py / deepseek_coder.py / qwen_advisor.py 共通）

advisor 3 本は OpenAI 互換クライアントの例外を **構造化終了コード**に振り分ける。Bash で `$?` を見て一発分類できる:

| exit | 意味 | 扱い |
|---|---|---|
| 0 | 成功 | 通常処理 |
| 10 | 認証エラー（401 / invalid key / 環境変数未設定） | **即中断**・該当 API のキー確認を依頼 |
| 20 | quota / rate-limit（429） | **即中断**・少し待って再実行 or プラン確認を依頼 |
| 30 | timeout | **即中断**・入力を分割 or `--max-tokens` を下げて再実行 |
| 40 | network error（接続不能 / DNS） | **即中断**・ネットワーク確認を依頼 |
| 50 | invalid input（プロンプト空 / 未知 role / Bad Request） | **即中断**・入力プロンプトを確認 |
| 1 | その他汎用 | **即中断**・stderr の生メッセージを表示してユーザー判断 |

Codex CLI は exit code ではなく **stderr 文字列**で認証エラーを返す:
- `Not logged in` / `401` / `unauthorized` → 認証エラー扱い（exit 10 相当）
- `codex login` での再認証を依頼して中断

### 並列段で複数 advisor を起動した場合の判定

Bash 並列起動の各段の `$?` を確認し、**1 つでも非 0 が出たら即中断**（Lv1-8 の原則）:
- exit 10/20/30/40 → 該当 API の問題。残りの成功段でも続行しない
- exit 50/1 → 入力 or 起動側の問題。stderr を確認して再実行

**Lv0 のみ例外**: Lv0 の Step 2-0B では `30/50` (timeout / invalid input) を「該当ユニットだけ中断、残り成功ユニットは Step 2-0C で部分適用可」とする。詳細は **Step 2-0B「エラー時の扱い」** セクション参照。auth/quota/network (10/20/40) と「その他」(1) は Lv0 でも即中断・全ユニット破棄で全体規約と一致する。

**中断時にユーザーへ報告する内容**（1〜3 行）:
1. どの API で exit code いくつが返ったか（例: 「DS が exit 10（認証）」）
2. 復旧手順（`codex login` / `GEMINI_API_KEY` 等の環境変数確認 / quota の場合は時間を空ける）
3. 復旧後に `/cgd` を再実行する旨

**特にやってはいけないこと**:
- Lv2 で片方が認証エラーのまま、もう片方の出力だけで既定5列（Gemini併用時6列）統合表を作る
- Lv3 の Step 2-3C で4件（Codex技術/DS技術/Codex批評/DS批評）のうち1件でも認証エラーが出た場合、残りの結果だけで技術/批評レビュー表を作る（必ず4件全部成功してから Step 2-3D へ。「2社×2視点」が Lv3 の価値の本質なので1件欠けても Lv2 と同等になり Lv3 の意味が消える）
- Lv4-5 で途中段の認証エラーを無視して中間結果を「結論」として扱う
- 別の API（例: 認証成功している側）で代用して続行する
- Lv4-5 の Step 2-4C で DS と Qwen 片方だけ成功した場合、もう片方なしで Step 2-4D に進む（必ず両方成功してから次段へ）
- Lv6 の Step 2-6B でその回の参加者（既定 Codex/DS/Qwen の3者、Gemini併用時4者）のうち 1 者でも認証エラーが出た場合、残りの結果だけで統合表を作る（必ず参加者全部成功してから Step 2-6C へ。Lv6 は「参加者全員の視点」が価値の本質なので 1 者欠けたら Lv2/Lv4 と同等になり Lv6 の意味が消える）
- Lv7 の Step 2-7D でその回の参加者（既定 Codex×2+DS+Qwen の4者、Gemini併用時5者）のうち 1 者でも認証エラーが出た場合、残りの結果だけで統合表を作る（必ず参加者全部成功してから Step 2-7E へ。特に **Codex medium と Codex high の両方** が成功している必要があり、片方失敗で代用すると Lv7 の本質である Codex 多重が崩れる）
- Lv8 の Step 2-8D でその回の参加者（既定6者: Codex medium技術+Codex high技術+DS技術+Qwen技術+Codex high批評+DS批評、Gemini併用時7者）のうち 1 者でも認証エラーが出た場合、残りの結果だけで技術/批評レビュー表を作る（必ず全部成功してから Step 2-8E へ。特に **Codex の3呼出（medium技術・high技術・high批評）すべて** が成功している必要がある）

---

## 注意事項

- **直列実行**: Lv4-5 の相談段（2-4A→F）は基本的に順序が重要（前段の出力を後段に渡す）。ただし **Step 2-4C の DS と Qwen は同じ入力に対する別意見取得なので並列**
- **並列実行**: Lv2（Codex+DS、Bash 2個。Gemini併用時3個）、Lv3 の Step 2-3C（Codex技術+DS技術+Codex批評+DS批評の4件並列、Bash 4個）、Lv4-5 の Step 2-4C（DS+Qwen 並列、Bash 2 個）、Lv6 の Step 2-6B（既定 Codex+DS+Qwen 3 者並列、Bash 3 個。Gemini併用時4個）、**Lv7 の Step 2-7D（既定 Codex(med)+Codex(high)+DS+Qwen の 4 者並列、Bash 4 個。Gemini併用時5個）**、および **Lv8 の Step 2-8D（既定 Codex(med)技術+Codex(high)技術+DS技術+Qwen技術+Codex(high)批評+DS批評の6者並列、Bash 6個。Gemini併用時7個）** は 1 メッセージ内で並列起動
- **DS/Qwen 役割の Lv 別整理**:
  - Lv0: `--role coder`（並列実装委譲・量産用・Step 2-0B で起動・Claude は分解と検証に専念）
  - Lv1: 呼ばない
  - Lv2: `--role reviewer`（既定第2エンジン・Codexと同じ原データを直接渡す）
  - Lv3: `--role reviewer` と `--role critic` の両方（技術視点+批評視点。Codex にも同じ2視点を別プロンプトで依頼し「2社×2視点」を構成。実装フェーズなしでここで完結）
  - Lv4-5: `--role advisor`（別案出し・Claude の検討要約、Gemini オプトイン時は Gemini の検討要約も渡す）
  - Lv6: `--role reviewer`（Codex（オプトイン時は Gemini も）と同じ原データ＝差分のみを直接渡す並列レビュー・要約は使わない）
  - Lv7: `--role reviewer`（差分 + **関連関数抜粋**（Claude が事前抽出）を渡す補助レビュー・表層指摘を減らす）
  - Lv8: DS は `--role reviewer`（技術補助）と `--role critic`（批評）の両方。Qwen は `--role reviewer`（技術補助）のみ。Codex(high) にも技術に加え別プロンプトで批評を依頼（Lv7 の技術構成 + Lv3 の批評構成の合成）
- **Codex 多重 (Lv7 / Lv8)**: medium と high を **同じ入力で並列実行**。reasoning level の違いから別視点を取得。Lv8 は high をもう1回、批評プロンプトで再利用する（合計3呼出）。再レビュー（Step C）は両Lvとも medium 単独で OK
- **関連関数の事前抽出 (Lv7 / Lv8)**: Claude が grep + Read で関数境界を見つけて抜粋し、`C:/tmp-ai/lv7_related_funcs.txt`（Lv8 は `lv8_related_funcs.txt`）に結合保存してから DS/Qwen に渡す。差分の 5〜10 倍程度のサイズが目安。Lv8 は技術・批評の両方でこの抽出結果を再利用する
- **再レビュー（Step C）は Codex 単独**: トークン節約のため DS/Qwen/Gemini は呼ばない（Lv4-7 共通、Gemini を初回オプトインしていても Step C では呼ばない。Lv1-3 は実装フェーズがないため Step C 自体に到達しない）
- **差分のみ**: Step C は `git diff` を渡し、全ファイルは渡さない（時間とコスト圧縮）
- **スキル連鎖禁止**: 本スキル内から `/codex` や別スキルを Skill ツール経由で自動呼び出ししない
- **相談段は read-only**: Codex `--sandbox read-only`、DS/Qwen/Gemini は API 単発呼び出し（書き込み権限なし）
- **書き込みフェーズは Step A (Lv1-8) または Step 2-0C (Lv0) のみ**: ファイル編集・新規作成はこのフェーズに集約。Codex / DS / Qwen / Gemini は **API として read-only 利用**、書き込みは必ず Claude Code 本体が実行する（DS/Qwen の生成物も Step 2-0C で Claude が Read → Write で適用）
- **API キー**: DS は `DEEPSEEK_API_KEY`、Qwen は `DASHSCOPE_API_KEY` を読む。Codex はサブスク認証（`OPENAI_API_KEY` をセットしない）。**Gemini はオプトイン時のみ** `GEMINI_API_KEY` が必要
- **機密情報**: 顧客データ・社内 DB 接続情報を不必要に外部 API（DS / Qwen / Gemini）に渡さない。特に Qwen は DashScope International（Singapore リージョン）に送信されることを意識する
- **DS / Qwen パスは絶対パス**: 相対 `.claude/tools/...` は CWD=`C:/tmp-ai` で解決失敗するので必ず絶対パス
- **実装フェーズの規約強制**: Step A では AGENTS.md / CLAUDE.md ルール（バックアップ・shebang 禁止・`encoding="utf-8"` 明示・日本語パスは Python スクリプト経由・.bat は CP932）を必ず守る
- **検証フェーズの省略禁止**: Step B で `ast.parse` だけで済ませず、必ず実 import まで実行
- **Lv5 自動修正ループ上限**: 最大 1 周。改善なし / 新規🔴 / 上限到達のいずれかで停止しユーザー判断
- **巨大対象は要点抽出 + ファイル読ませ**: Codex / Gemini に丸投げせず Claude Code 側で要点抜粋。さらに **プロンプトを argv で渡さずファイル経由で読ませる**（`"$(cat file)"` は入力が大きいと `Argument list too long`（ARG_MAX 超過）になる。`"まず <file> を読み…"` 形式で Codex/Gemini にファイルを読ませる）。Gemini は cwd=`C:/tmp-ai` の workspace 内ファイルのみ読めるので、対象は必ず `C:/tmp-ai` に置く

---

## トラブルシュート

> ### 📝 不具合に気づいたら「その場で直す」前に台帳へ 1 行
>
> Codex/DS/Qwen/Gemini・Claude Code ハーネス側の不具合は、**セッションごとにその場で対処すると知見が消える**（同じ日に別セッションが同じ罠を踏んでも気づけない）。緊急でなければ **記録だけして先へ進み**、後日 `/incidents` の専用セッションでまとめて解析する:
>
> ```bash
> python "C:/ClaudeCode/.claude/tools/incident_log.py" add --tool codex --category token \
>   --severity high --title "<1行要約>" --detail "<観測値・再現条件・仮説>" --evidence "<証拠パス>"
> ```
>
> 外部 AI 呼出のトークン数・出力サイズは `ai_telemetry.py` hook が自動記録しているので、**数字は台帳に手写ししなくてよい**（`incident_log.py report` で突き合わせられる）。

- **Lv2/4/6/7 で Gemini が呼ばれない/表に出てこない** → **意図的な既定動作**（2026-07にオプトイン化）。「Geminiも」等を明示すれば追加参加する
- **`[cgd wf-gate] Lv7 は Workflow 実行が必須です` で codex が deny された** → 正常な動作。inline ではなく Workflow で実行する（「Workflow 経由実行」節）。WF 完了後に `cgd_wf_gate.py disarm` を忘れると Step C の再レビューも弾かれる
- **codex を実行していないのに deny された** → ゲートは「コマンドとして起動される位置」（行頭 / `&&` `;` `|` の直後 / 環境変数代入の直後）の `codex exec` だけを見るので、`grep "codex exec"` やコミットメッセージ内の文字列では発火しない。ただし **heredoc の本文に `codex exec` で始まる行がある**（手順書を `cat > f <<'EOF'` で書く等）と発火しうる。その場合は `cgd_wf_gate.py disarm` するか、コマンド先頭に `CGD_WF_RUN=1` を付ける
- **DS/Qwen が「本文が空です(finish_reason=length)」で exit 1** → 推論だけで出力予算を使い切った。
  `--max-tokens` を上げて再実行する(既定 32000 / API は 65536 まで受理を実測済み)。
  入力が大きいときは対象を分割する方が確実
- **DS/Qwen が「出力が上限で打ち切られました」と警告** → 本文は途中まで出ているが不完全。
  そのまま統合表に載せず、`--max-tokens` を上げて取り直す
  (2026-08-05以前は**空文字を黙って返して正常終了**していたため、レビュー結果ゼロを
   「指摘なし」と誤解する事故が起きた。現在は必ず気づける)
- **DS が `DEEPSEEK_API_KEY が設定されていません`** → 環境変数を確認
- **DS のレスポンスがコード生成っぽい** → `--role advisor` または `--role reviewer` 付け忘れ。デフォルトは `coder`
- **DS reviewer が別案を返す（advisor 風出力）** → Lv6 で `--role advisor` のまま実行している。必ず `--role reviewer` を付ける
- **DS が「ファイルが見つかりません」** → 相対パス指定の罠。絶対パスで指定し直す
- **Qwen が `DASHSCOPE_API_KEY が設定されていません`** → 環境変数を確認（Singapore リージョンのキーであることも確認、US/中国本土キーとは非互換）
- **Qwen が `InvalidApiKey` / `401`** → キーとエンドポイントのリージョン不一致。Singapore/US Virginia/China Beijing は**非互換**。環境変数 `QWEN_BASE_URL` でエンドポイント切替:
  - Singapore (既定): `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`
  - US Virginia: `https://dashscope-us.aliyuncs.com/compatible-mode/v1`
  - China Beijing: `https://dashscope.aliyuncs.com/compatible-mode/v1`
- **Qwen のレスポンスが英語コード** → `--role advisor` または `--role reviewer` 付け忘れ。Qwen のデフォルトは `advisor` だが明示推奨
- **Qwen reviewer が別案を返す（advisor 風出力）** → Lv6 で `--role reviewer` を明示しないと既定の `advisor` が選ばれる。必ず `--role reviewer` を付ける
- **Lv6 で参加者（既定3者、Gemini併用時4者）のうち 1 者が認証エラー** → 即中断（Lv6 は参加者全員揃ってこそ意味があるので欠員のまま続行しない）。該当 API の復旧後に再実行
- **Lv7 で参加者（既定4者、Gemini併用時5者）のうち Codex 片方 (med or high) が認証エラー** → 即中断（Lv7 の本質は Codex 多重なので片方では成立しない）。`codex login status` を確認し復旧後に再実行
- **Lv7 で関連関数抜粋が大きすぎてタイムアウト** → 抜粋を「変更 hunk 直近 ± 30 行 + 直接の呼出元 1〜2 関数」に絞る。argv 制限 32KB 以下を目標
- **Lv7 の Codex high が 10 分でタイムアウト** → 入力プロンプトを「対象ファイル絶対パス + 変更概要 + 差分」に圧縮（関数定義は Codex が sandbox で読みに行く想定なので Claude から渡さない）
- **Codex `Not logged in`** → `codex login` の実行を依頼して停止
- **Gemini `API key not valid` / `GEMINI_API_KEY が設定されていません`** → 環境変数 `GEMINI_API_KEY`（Google AI Studio で発行）を確認・再設定して停止
- **Gemini `503 high demand`** → 一時的な高負荷。少し待って再試行、または `--model gemini-2.5-pro` 等に切替（既定 `gemini-2.5-flash`。`gemini-flash-latest` は 503 が出やすいので非推奨）
- **Gemini 日本語が化ける / `surrogates not allowed`** → 日本語プロンプトを argv/stdin で渡すと CP932 で壊れる。**必ず utf-8 ファイルに書いてパス渡し**（`gemini_advisor.py "C:/tmp-ai/xxx.txt"`）
- **`Argument list too long`（Codex 起動時）** → 大きい入力を `"$(cat 'file')"` で argv 展開して ARG_MAX 超過（62KB で実発生）。**Codex にはファイルパスを渡して自分で読ませる**短いプロンプト（`codex exec ... "まず C:/tmp-ai/review_input.txt を読み…"`）にする。Gemini/DS/Qwen はファイルパス引数なので影響なし
- **無応答ハング** → `< /dev/null` 付け忘れ
- **日本語 CWD 文字化け** → `cd "C:/tmp-ai"` 忘れ
- **`unexpected argument '--ephemeral'` 等** → CLI バージョン差異。該当フラグを外して再試行
- **PowerShell でエラー** → 本スキルは Bash 必須。Git Bash で起動し直す
- **レート制限（特に Gemini 無料枠）** → 時間を空けて再試行
- **タイムアウト** → Codex は `reasoning=low` に落とす、Gemini はプロンプトを分割
- **Lv0 で DS/Qwen の生成品質が低い（幻覚 import / 規約逸脱 / 仕様外し）** → Step 2-0C で Claude が一読して書き直す。連続で品質 NG なら Lv0 中断 → Lv2 等に切替（委譲が割に合わない対象）
- **Lv0 で対象が小さすぎる**（1 ファイル < 50 行 / ユニット数 < 2） → Step 2-0A の小ライン警告で Lv1-2 に誘導。委譲の往復コストが勝つ
- **Lv0 で生成行数が 100 行を超えるのに Codex レビューを省略したい** → 規模依存自動判定の上書き。明示指示があった場合のみ、💰 費用集計に「Codex レビューを省略」と注記して進める
- **Lv0 で部分適用した成功ユニットを後でどう扱うか** → Step 2-0C の受領表で「適用済み / 未適用（失敗ユニット）」を明示。再実行時は失敗ユニットだけ Step 2-0B から再起動。**成功ユニットを二重委譲しない**（DS/Qwen 費用が二重発生する）
- **Lv0 の「受領品質 NG」の判定基準** → Step 2-0C で以下のいずれかに該当: (1) 仕様との明確な乖離、(2) AGENTS.md / CLAUDE.md 規約違反（shebang / 文字コード / 型ヒント漏れ）、(3) 幻覚インポート・存在しない API、(4) 構文エラー。1 ユニットでも NG なら Claude が 1 周だけ書き直し（Step C2 と同じ枠）
- **Lv0 のバックアップが失敗（Windows パスや空白）** → `cp "$FILE" "${FILE}.bak_$(date +%Y%m%d_%H%M%S)"` のように **必ずダブルクォート**。空白入りパスでも壊れない
- **環境変数が怪しい / 認証エラーが頻発** → `python C:/ClaudeCode/.claude/tools/cgd_doctor.py` で一括確認（reviewer 経路は `--probe`、Lv0 の coder 経路は `--probe-coder`）

---

## レベル選びの目安

| シーン | 推奨 Lv |
|---|---|
| **scaffold / 量産タスク（複数ファイル雛形・pytest 量産・docstring 一括）** | **Lv0** |
| **トークン節約優先（Claude 本体の消費を抑えたい）** | **Lv0** |
| 小修正・低リスク・急ぎ対応 | Lv1 |
| 通常開発の標準（機能追加・軽中規模リファクタ） | **Lv2（既定）** |
| 実装するか未定・まず技術面とユーザー視点/あるべき論の両方を見ておきたい | Lv3 |
| 仕様検討段階で Codex/DeepSeek 双方に辛口の使い勝手批評もさせたい（実装前） | Lv3 |
| マージ前の品質ゲート（PR 直前見落とし削減、実装後の再レビューまで欲しい） | Lv4 |
| 高リスク変更（DB 設計・API 設計・状態管理方式・セキュリティ） | Lv4 |
| 重要な設計判断（アーキ変更・性能影響大） | Lv4 |
| 難しいバグの原因仮説出し（症状から複数候補が必要） | Lv4 |
| 大規模リファクタの方針決め | Lv4 |
| 新規機能のアーキテクチャ案出し | Lv4 |
| リリース直前・障害再発防止・絶対に通したい | Lv5 |
| 複眼レビューで盲点を潰したい（4 者の視点を全部欲しい） | Lv6 |
| Lv4-5 を試したが DS/Qwen の別案が機能しなかった対象の代替 | Lv6 |
| 最重量複眼レビュー（Codex / DS / Qwen の 3 者、必要なら Gemini も加えて同じデータを評価） | Lv6 |
| **Integration バグ・関数間の暗黙の前提違反・スコープを跨いだ状態管理の検査** | **Lv7** |
| **大規模 IIFE / モジュール内のクロスリファレンス整合性チェック** | **Lv7** |
| **Lv6 で Codex 単独指摘が多く、他者からの追加発見が少なかった対象の深掘り** | **Lv7** |
| **長大ファイル（5000 行〜）に対する厳密なレビューが必要・diff だけだと文脈不足** | **Lv7** |
| Integration バグの最深掘りに加え、Codex(high)/DeepSeek の辛口批評も同時に欲しい・実装まで一気に進めたい最重要変更 | **Lv8** |
| Lv7 の技術レビューは十分だが「そもそもこの仕様でいいのか」まで突っ込んだ意見が欲しい | **Lv8** |

---

## スタンプ運用ルール（Step 0 のバージョン照合用）

冒頭の `<!-- SKILL_VERSION: YYYY-MM-DD_HHMMSS -->` は、Step 0 で「セッションのコンテキストが最新か」を軽量に判定するためのスタンプ。

- **このスキル（cgd/codex/critic のいずれか）を編集したら、必ずスタンプを実時刻で更新する**
  - 実時刻は **必ず `date '+%Y-%m-%d_%H%M%S'` で取得**（手入力の固定値は禁止・DEPLOY_TIME ルール準拠）
  - cgd / codex / critic は連動するので、**どれか1つを編集したら3ファイルとも同じスタンプに揃える**（Step 0 の照合が崩れないように）
- スタンプは frontmatter（`---` の閉じ）直後の1行に置く（`grep -m1 'SKILL_VERSION'` で確実に拾える位置）
- Step 0 はこのスタンプ1行だけ grep するので、900 行を毎回読まずに最新判定できる
- `/g-ul` で claude-shared に push する前にもスタンプ更新を確認（他端末の Step 0 が「最新」と誤判定しないように）
