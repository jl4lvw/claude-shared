# cgd フルパイプライン報告 — PWA オフラインキューイング設計

- 実行日: 2026-05-05
- モード: 2 (フル, 6段直列)
- テーマ: PWA でオフライン時のデータ更新キューイング設計
- 要件: Service Worker で fetch 捕捉 → IndexedDB 退避 → オンライン復帰時に順次再送
- 論点: 再送順序保証 / 競合解決 / リトライ上限 / UI 進捗反映
- Codex reasoning: medium

---

## Step 2F-A: Gemini 案出し（実行成功）

<details><summary>Gemini 出力（要点）</summary>

Gemini は 4 案を提示:

1. 案1 透過的リクエスト・キャプチャ方式（FIFO） — SW で fetch 失敗をそのまま IndexedDB 保存、FIFO 再送。導入容易だが Head-of-line ブロッキング・409 競合に弱い。実装難度: 低
2. 案2 セマンティック・アクション方式 — UpdatePost 等の意図を保存し、シンクマネージャが最新状態と照合して API 呼び出し。柔軟・UX 良いが定義同期コスト。実装難度: 中〜高
3. 案3 リソース・ステート同期方式 — データに sync_status/version を付与し最終状態を PATCH。楽観ロック必須、冪等でない操作に不向き。実装難度: 高
4. 案4 Workbox BackgroundSync 活用 — 標準 API で実装最小、ただし iOS Safari 非対応問題。実装難度: 低

Gemini 推奨: 案2（バランス良）または 案4 + iOS フォールバックに案1 を併用。

</details>

---

## Step 2F-B: Claude 検討（Gemini 案レビュー）

Claude 視点での追加懸念:

- (a) マルチデバイス問題: 同一ユーザーが複数端末で操作した際の競合に Gemini の案は触れていない
- (b) IndexedDB 容量上限超過: 50MB〜数百MB のクォータを超えた際のフォールバック未検討
- (c) 認証トークン期限切れ: 復帰時に 401 でキュー全滅するリスク
- (d) Idempotency-Key の活用: サーバー側冪等性キーで「再送=安全」を担保すれば順序保証要件を緩められる

Claude 第1推し: 案2（セマンティックアクション）+ 冪等性キー併用ハイブリッド

---

## Step 2F-C: DeepSeek 別案（実行成功・advisor モード）

<details><summary>DS 出力（要点）</summary>

別案:

1. オペレーションログベース CQRS 分離 — SW は操作を「コマンド」として IndexedDB に append-only 記録。Sync Worker が逐次 API 送信、楽観 UI で即時反映、サーバー応答で補正。順序は seq_no で保証。
2. サーバー主導マージ戦略 (CRDT/タイムスタンプ) — クライアントは差分パッチをキュー、サーバーが受信時にマージ解決。順序保証不要に。
3. ローカルファースト + 定期ポーリング同期 (PouchDB/CouchDB 方式) — SW で fetch 捕捉せずアプリが直接 IndexedDB 読み書き、後で双方向同期。

DS 指摘の見落とし:
- 同一データの複数編集による重複 API 呼び出し
- Service Worker ライフサイクル（更新・kill）でのキュー消失リスク
- オフライン中にサーバー側で削除済み (404) の扱い
- 大量キューの再送スパイクとメモリ
- オフライン中のログアウト時のキュー破棄ルール

</details>

---

## Step 2F-D: Claude 統合検討（Gemini + DS）

| # | 出所 | 案概要 | Claude 評価 |
|---|---|---|---|
| 1 | Gemini 案1 | 透過 FIFO 捕捉 | 軽量だが HOL 詰まり致命的、補助用 |
| 2 | Gemini 案2 | セマンティック・アクション | バランス良、本命候補 |
| 3 | Gemini 案3 | ステート同期 (楽観ロック) | 冪等操作のみ可、業務系不向き |
| 4 | Gemini 案4 | Workbox BackgroundSync | iOS で穴、フォールバック必須 |
| 5 | DS 別案1 | CQRS 操作ログ追記 | 案2 と相性良、append-only で監査性向上 |
| 6 | DS 別案2 | サーバー主導 CRDT | 業務 API には過剰、リアルタイム共同編集向き |
| 7 | DS 別案3 | ローカルファースト + 双方向同期 | 別アーキ、現行 PWA に後付け不可 |

Claude 統合推し案（Codex に投入）:

セマンティックアクション方式 (Gemini 案2) + サーバー側 Idempotency-Key 必須化 + オペレーションログ追記 (DS 別案1) のハイブリッド。

- {action_type, payload, client_op_id (UUIDv4), client_ts, retry_count} を IndexedDB に append-only
- SW は通常 fetch をプロキシし、オフライン or 5xx でログにエンキュー → 202 Accepted 相当を返す
- SyncManager が client_ts 昇順で逐次 POST、Idempotency-Key ヘッダで重複弾く
- 競合: サーバー楽観ロックで 409 → UI 「要確認」状態
- リトライ: 指数バックオフ 1s〜32s、6回失敗で dead-letter
- UI 進捗: BroadcastChannel で全タブにキュー長/エラー件数配信
- 容量: navigator.storage.estimate() 80% 超で古い completed ログ削除
- 認証: 401 で SyncManager 一時停止 → トークン更新後再開

---

## Step 2F-E: Codex 最終レビュー（実行成功・medium・read-only）

<details><summary>Codex 指摘原文（要点抜粋）</summary>

重大（要修正）:

1. client_ts 昇順は順序保証として弱い → 端末時計ズレ・改ざん・複数タブ生成順で破綻。seq_no per device + depends_on_op_id[] 明示、サーバーで未解決依存は 409/422
2. オフライン時 202 Accepted は誤認誘発 → queued:true 契約に統一、状態を pending_local / accepted_remote / failed_conflict に分離
3. Idempotency-Key の寿命・スコープ未定義 → user_id + operation_scope + client_op_id で名前空間化、TTL 7-30 日、同一キーは同一結果
4. 401 時 Sync 停止/再開にレース → 単一リフレッシュロック（BroadcastChannel/IDB mutex）、reauth_required 状態を FSM で定義

高（設計懸念）:

5. 5xx 即エンキューは危険（処理成功後 5xx で二重副作用） → オフライン時のみ自動キュー、オンライン 5xx は通常リトライ。エンドポイント単位許可制
6. dead-letter 運用不足 → last_error_code/server_trace_id/first_failed_at 保持、UI に再実行/破棄/サポート送信導線、自動復旧 (429/503) と手動 (409/422/400) を分離
7. 容量 80% completed 削除は粗い → 削除順 completed → tombstoned → old_failed 段階化、未送信件数/バイト先保護、navigator.storage.persist()

セキュリティ:

8. IndexedDB 平文保存は XSS/共有端末で漏えい → payload 最小化、暗号化、CSP/Trusted Types
9. client_op_id の再利用攻撃 → サーバーでデバイス境界・署名付きメタ検証、リプレイ検知
10. BroadcastChannel は同一オリジン内に露出 → メッセージ最小化、チャネル名名前空間化

中（改善推奨）:

11. バックオフにジッター無し → exponential + full jitter
12. 409 のみ競合扱いは不十分 → 412/422/429 別ポリシーテーブル
13. SyncManager 依存は iOS で制約 → フォアグラウンド時フォールバック同期ループ
14. append-only ログ GC 不足 → 状態 (queued/sending/acked/failed/dlq) と TTL、複合インデックス

推奨追加: サーバー側「オペレーション結果テーブル」で同一キー同一レスポンス、契約テスト（重複/順序入替/時計逆行/複数タブ/トークン失効同時）、op_id 相関 ID 観測性

</details>

---

## Step 2F-F: 6列統合表

| # | 指摘 / 論点（重大度＋根拠1行） | Gemini | DS | Codex | Claude 最終判断 |
|---|---|---|---|---|---|
| 1 | 重大 順序保証は client_ts だけだと破綻 — 時計ズレ・複数タブで順序逆転 | — | seq_no と既に主張 | 重大1: seq_no per device + depends_on_op_id 明示 | 採用: seq_no + depends_on_op_id[] を必須化 |
| 2 | 重大 オフライン時 202 偽装はアプリ整合性破壊 — 呼び出し元が受理済みと誤認 | — | — | 重大2: queued:true 契約 + 状態分離 | 採用: pending_local/accepted_remote/failed_conflict の3状態 FSM |
| 3 | 重大 Idempotency-Key の TTL/スコープ未定義 — 無期限はサーバー圧迫 | — | — | 重大3: user_id+scope+op_id、TTL 7-30 日 | 採用: スコープ名前空間 + TTL 14 日 + 結果テーブル |
| 4 | 重大 401 リフレッシュにレース — 複数タブ同時更新で無限停止 | — | — | 重大4: 単一リフレッシュロック + FSM | 採用: BroadcastChannel mutex で単一リフレッシュ |
| 5 | 重要 5xx 即エンキューは二重副作用 — サーバー成功後 5xx で再送が破壊的 | — | — | 高5: オンライン 5xx は通常リトライ、許可制 | 採用: エンドポイント許可リスト + Idempotency-Key 必須 |
| 6 | 重要 dead-letter 運用設計不足 — 6回失敗後溜まるだけ | — | — | 高6: last_error_code/trace_id + UI 再実行導線 | 採用: DLQ にメタ情報 + UI に手動操作 |
| 7 | 重要 容量 80% completed 削除は粗い — 未送信を圧迫する恐れ | — | — | 高7: 段階化 + storage.persist() | 採用: 未送信先保護 + persist 要求 |
| 8 | 重要 マルチデバイス競合 — 同一ユーザー2端末で順序破綻 | — | (CRDT別案で間接対応) | (重大1で seq_no 帰着) | 部分採用: device_id を seq_no に含めサーバー側で per-device ストリーム化 |
| 9 | 注意 IndexedDB 容量上限フォールバック | — | — | 高7 と統合 | 採用: 上記7と統合 |
| 10 | 注意 IndexedDB 平文保存の漏えい — XSS / 共有端末 | — | — | セキュ8: payload 最小化 + 暗号化 + CSP | 採用: 機微フィールド暗号化 + CSP/Trusted Types |
| 11 | 注意 client_op_id 再利用攻撃 — UUIDv4 のみは推測耐性◎だがリプレイ可 | — | — | セキュ9: device_id 署名 + リプレイ検知 | 部分採用: device_id 紐付けは入れる、署名は v2 |
| 12 | 注意 BroadcastChannel の情報露出 — 同一オリジンの他コードに見える | — | — | セキュ10: メッセージ最小化 + 名前空間 | 採用: 件数のみ配信、エラー詳細は別チャネル |
| 13 | 注意 バックオフにジッター無し — 多数クライアント同時再送スパイク | — | — | 中11: full jitter | 採用: exponential + full jitter (1s〜32s base) |
| 14 | 注意 409 のみ競合扱いは不十分 — 412/422/429 別処理が必要 | (案3で言及) | — | 中12: コード別ポリシーテーブル | 採用: HTTP ステータス別ハンドラマップ |
| 15 | 注意 SyncManager iOS 非対応 — Safari で BackgroundSync 効かず | 案4で言及 | — | 中13: フォアグラウンドフォールバック | 採用: visibilitychange/online で fallback ループ |
| 16 | 注意 append-only ログの GC 不足 — 無制限成長 | — | DS見落とし指摘 | 中14: 状態列挙 + TTL + 複合インデックス | 採用: ログ状態 5 種 + 30 日 TTL |
| 17 | 注意 SW ライフサイクルでキュー消失 — SW kill / 更新時 | — | DS見落とし指摘 | (間接) | 採用: IndexedDB は SW 外永続なので OK、ただし SW 更新時の skipWaiting 制御 |
| 18 | 注意 オフライン中サーバー削除 (404) の扱い | — | DS見落とし指摘 | — | 採用: 404 は dead-letter 直送（非リトライ）+ UI 通知 |

---

## 採用方針

セマンティックアクション + Idempotency-Key + append-only ログのハイブリッドをベースに、Codex の重大4点（順序保証強化／queued 契約明示／キー寿命定義／401 単一ロック）を必ず織り込む。マルチデバイス対応として device_id + seq_no の per-device ストリームを導入。観測性として op_id をクライアント・サーバー・トレースで相関可能にする。

## 次アクション

1. データモデル定義 (pwa/db/schema.js): op_log ストアに {op_id, device_id, seq_no, action_type, payload, depends_on_op_id[], state, retry_count, last_error_code, created_at}
2. 状態機械実装 (pwa/sync/state-machine.js): pending_local → sending → acked_remote / failed_conflict / dlq
3. SyncManager (sw.js): per-device seq_no 順、Idempotency-Key 必須、HTTP コード別ポリシー (404→DLQ / 409,412,422→conflict / 429,503→retry / 401→pause)
4. バックオフ: exponential + full jitter (base*(1+random())、最大 32s、6 回上限)
5. BroadcastChannel (pwa/sync/progress-bus.js): 件数のみ配信、エラー詳細は別チャネル
6. 容量管理: navigator.storage.persist() を SW 起動時に要求、estimate 80% 時は acked_remote → tombstoned → old_failed の順で削除、未送信は常に保護
7. 認証連携: 単一リフレッシュロック (BroadcastChannel + IDB) を pwa/auth/refresh-mutex.js に実装
8. サーバー側 (gas/Code.gs): Idempotency-Key 受付・結果テーブル (TTL 14 日)・X-Op-Id レスポンス相関 ID
9. 契約テスト (pwa/tests/sync.contract.test.js): 重複送信・順序入替・時計逆行・複数タブ・トークン失効同時
10. 暗号化: 機微フィールドのみ Web Crypto API で AES-GCM、鍵は non-extractable CryptoKey

---

## 実行ログ

| ステップ | CLI | 結果 | 備考 |
|---|---|---|---|
| 2F-A | gemini --skip-trust -p | OK | 4案提示、推奨は案2 |
| 2F-C | deepseek_coder.py --role advisor | OK | 別案3個 + 見落とし5点 |
| 2F-E | codex exec medium read-only | OK | 14 指摘 + 推奨追加3項目 |

すべて成功。失敗・タイムアウト・レート制限は発生せず。
