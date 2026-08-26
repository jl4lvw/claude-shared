---
name: s
description: スキル早見表(リファレンス)。「今どのスキルを使えばいいか分からない」ときに引く索引。汎用スキル(どのプロジェクトでも使う)と業務固有スキル(特定サブプロジェクト専用)を2階層に分けて一覧する。実行は行わない(他スキルを呼び出さない・スキル連鎖禁止のため)。索引を見て、該当するものを別途 `/名前` で呼び出す。「どのスキル使えばいい」「スキル一覧」「/s」などで起動。
---

# s — スキル早見表

**このスキルは何も実行しない。** 該当するスキルを見つけたら、`/<名前>` で別途呼び出すこと
(スキルからスキルを呼ぶ「スキル連鎖」はこのリポジトリ全体で禁止されている)。

新しいスキルを追加・改名したら、このファイルにも1行追記すること。

---

## 汎用スキル(どのプロジェクトでも使う)

### コーディング・レビュー
| 名前 | いつ使うか |
|---|---|
| `cgd` / `codex` | Codex+DeepSeek+Qwenでのコードレビュー・設計相談・実装・検証。迷ったらこれ |
| `critic` | 辛口ユーザー視点・あるべき論での仕様/UI評価(cgdの軽量版・実装前が最も効く) |
| `pv` | Workflowを使った仕様・設計の並列検討・多視点検証(コードでなく方針の検討) |
| `code-review` | 現在の差分/PRのレビュー(低〜ultraまで強度指定可) |
| `simplify` | 変更コードの重複削減・簡素化・効率化の適用 |
| `review` | 提示したコードの品質レビュー |
| `refactor` | コードのリファクタリング |
| `debug` | エラー・予期しない挙動の原因分析 |
| `security-review` | 現在のブランチのセキュリティレビュー |
| `plan` | コードベース分析からの開発計画作成 |
| `verify` | コーディング後の検証を一括実行 |
| `harden-error-handling` | エラーハンドリングの強化 |
| `make-pytest` | pytestテストの生成 |
| `add-types` / `add-docstrings` | 型ヒント/docstringの追加 |
| `generate-script` / `generate-api` / `generate-cli` / `generate-etl` | 新規Python成果物(スクリプト/API/CLI/ETL)の作成 |
| `generate-by-deepseek` | DeepSeekにコード生成させ、Claudeがレビュー |
| `init` | 新規CLAUDE.mdの初期化 |

### ナレッジ・メモリ
| 名前 | いつ使うか |
|---|---|
| `module-graph` | Memory MCPのグラフ(サブプロジェクト間依存)を最新化。3Dビューアーの元データ更新 |
| `ctx` | 文脈台帳(圧縮で失われるセッション限定情報の保全・復元) |
| `handoff` | セッション引継ぎ(状態を固定パスに保存/復元) |
| `incidents` | 外部AI・ハーネス不具合の台帳解析(専用セッションでまとめて) |

### 連携・コミュニケーション
| 名前 | いつ使うか |
|---|---|
| `relay` / `m` (=message-check) | 拠点間Claude Code同士のメッセージ・ファイルやり取り |
| `lineworks-check` | LineWorksトークの指示をBot Callback経由で確認 |
| `remote-control` | LineWorks経由の遠隔操作ループ(指示→質問→待機→実行) |
| `mail` | 顧客問合せへの返信メール・SMS下書き生成 |

### 同期・共有
| 名前 | いつ使うか |
|---|---|
| `g-ul` | `.claude/{skills,...}`をclaude-sharedへpush(他拠点へ配布) |
| `g-dl` | claude-sharedから最新をpull・反映 |
| `g-cmp` | `.claude`/claude-shared/originの三方比較(g-ul/g-dl前の確認) |

### ドキュメント・ビジュアル生成
| 名前 | いつ使うか |
|---|---|
| `docx` / `pdf` / `pptx` / `xlsx` | Word/PDF/PowerPoint/Excelファイルの作成・編集 |
| `design` | UIモックアップ・LP・ポスター等のデザインキャンバス作成 |
| `dataviz` | チャート・グラフ・ダッシュボードの作成 |

### 個人生産性・秘書
| 名前 | いつ使うか |
|---|---|
| `secretary` (=`b`) | Google Calendar・タスクの統合管理 |
| `cc-tasks` (=`c`) | Google Tasksの「ClaudeCode連携」リスト処理 |
| `t` | 050個人タスク管理PWAの「🤖 AI依頼」処理 |
| `r` | 遠隔指示取り込み(DB版) |

### 運用・設定
| 名前 | いつ使うか |
|---|---|
| `skill-creator` | 新規スキルの作成・既存スキルの改善・評価 |
| `update-config` | settings.jsonの設定変更(hook・permission・env var) |
| `keybindings-help` | キーボードショートカットのカスタマイズ |
| `fewer-permission-prompts` | 許可プロンプトを減らすためのallowlist追加 |
| `schedule` / `loop` | 定期実行タスク・繰り返し実行の設定 |
| `d` | Discord通知のON/OFF切替 |

---

## 業務固有スキル(特定サブプロジェクト専用・たまにしか見返さない)

| 名前 | 対象 |
|---|---|
| `goq-stock-set` | 023商品マスタDB経由でGoQ在庫連携画面の総在庫数を書き換え |
| `wholesale-order` | 052卸売注文(産経デジタル等)のGoQ登録 |
| `fbm-sync` | 022 Amazon在庫PWAのFBA→FBM在庫同期 |
| `sgw-settlement` / `sgw-purchase` | 049 SGW精算・仕入請求管理 |
| `import-cost` | 輸入・仕入原価の蓄積 |
| `generate-merumaga` | 楽天市場メルマガ(自衛隊テーマ)の自動生成 |

---

## 索引に無い場合

新設されたばかりで未掲載の可能性がある。会話冒頭の「利用可能なスキル」システムリマインダーを直接確認するか、Claudeに「〇〇したいけどスキルある？」と聞けば、descriptionと照合して案内する。
