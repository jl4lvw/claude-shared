# cgd フルパイプライン評価レポート — バグ原因仮説出し

**テーマ**: Tkinter Treeview ヘッダークリックソートが稀（10回に1回）にクリックを無視する症状の原因仮説
**モード**: 2 (full / 6段直列パイプライン)
**Codex reasoning**: medium
**実行日**: 2026-05-05

---

## パイプライン実行結果

| Step | エージェント | 状態 |
|---|---|---|
| 2F-A | Gemini (案出し) | OK — 5仮説 |
| 2F-B | Claude (検討) | OK — 低レイヤ視点不足を指摘 |
| 2F-C | DeepSeek (advisor / 別案) | OK — 3仮説 (ttk境界 / Win32 / 再描画競合) |
| 2F-D | Claude (統合) | OK — 8仮説に統合 |
| 2F-E | Codex (検証手順レビュー / medium) | OK — A/B/C 切り分け方針を提示 |
| 2F-F | Claude (最終まとめ) | 本ドキュメント |

---

## 6列統合表

| # | 仮説 (重大度+根拠) | Gemini | DS | Codex | Claude最終判断 |
|---|---|---|---|---|---|
| H1 | 🟡 ダブルクリック判定でシングルクリック消失 — クリック間隔依存 | ✓ 提案 | — | △ 妥当性中 / `<Double-Button-1>` 追加は副作用大 | 🔄部分採用: 追加バインドではなく `<Button-1>` 時刻列で連打パターン観測 |
| H2 | 🟠 メインスレッド一時ブロック — `after`/重い処理がイベント受付を遅延 | ✓ 提案 | — | ✓ 高評価 + heartbeat ジッタ計測追加 | ✅採用: heartbeat (`after(50,...)`) ジッタ併用 |
| H3 | 🟡 ウィンドウ非アクティブ→1クリック目がフォーカス取得に消費 | ✓ 提案 | — | △ 中 / `focus_get()` ログ化を推奨 | 🔄部分採用: `focus_get()` / `focus_displayof()` を計測 |
| H4 | 🔴 heading(command=) と bind() 競合 / `return "break"` でイベント停止 | ✓ 提案 | — | ✓ 高評価 / コメントアウトより `bindtags` 特定が先 | ✅採用: `bindtags` と `break` 発生箇所を最初に確認 |
| H5 | 🔴 関数は呼ばれたが見た目変化なし（データ不整合・ロック） | ✓ 提案 | — | ✓ 高評価 / ソートキー先頭3件も出す | ✅採用: A/B/C 切り分けの中核 |
| H6 | 🔴 ttk element ヒットテスト境界 — テーマ/パディング/DPI でピクセル境界外判定 | — | ✓ 提案 | ✓ 高評価 / 列固定でない症状とも整合 | ✅採用: A/B/C 切り分けの中核 |
| H7 | 🟡 Win32 メッセージ取りこぼし — Tk 内部 GetMessage キュー | — | ✓ 提案 | △ 低〜中 / 10%は高すぎ・アプリ層を先に疑え | ⏭️ 後回し: 全アプリ層仮説が外れたときのみ検証 |
| H8 | 🟡 ヘッダー再描画とクリック競合 — ソート直後の連続クリック | — | ✓ 提案 | △ 中 / `update_idletasks()` 挿入は診断ノイズ化 | 🔄部分採用: 計測のみ（連続クリック時の失敗率）、修正試行は最後 |

---

## 採用方針

「無視された」の実体を **A/B/C 3分岐**（A: イベント未到達 / B: イベント来たが command 未呼び出し / C: command は走ったが表示不変）に確定するための **共通計測の先入れ** を最優先とする。Codex 提案を全面採用。個別仮説の検証はその計測ログから自動的に絞り込まれる。OS層の H7 は最後。

---

## 次アクション（検証順序）

### Phase 0: 共通計測の先入れ（最重要・必須）
1. Treeview に `<Button-1>` バインド追加: `(perf_counter, x, y, identify_region(x,y), identify_column(x,y), focus_get(), bindtags, prev_click_dt_ms)` を記録
2. `heading(command=sort_func)` の `sort_func` 冒頭/末尾に: `(perf_counter, column, item_count, first_3_sort_keys)` を記録
3. GUI heartbeat: `root.after(50, beat)` で間隔ジッタを記録（メインスレッドブロック検知用）
4. ログは **失敗時も成功時も同形式** で出力（比較必須）

### Phase 1: 高優先仮説の検証
- **H4** (bind 競合 / return "break"): bindtags と break 発生箇所を grep + ログで特定
- **H6** (ヒットテスト境界): `region == "heading"` かつ command 未発火パターンを Phase 0 ログから抽出
- **H5** (見た目変化なし): command 入口ログの有無で判別

### Phase 2: 中優先仮説の検証
- **H2** (メインスレッド遅延): heartbeat ジッタと sort 関数遅延の相関
- **H3** (フォーカス取得消費): focus 状態ログから判別
- **H1** (ダブルクリック): 連打間隔 ms ログから判別

### Phase 3: 低優先仮説の検証
- **H8** (再描画競合): 「ソート直後の連続クリック」での失敗率上昇の有無を確認
- **H7** (Win32 取りこぼし): Phase 0〜2 が全部空振りしたときのみ。`<ButtonRelease-1>` 併用ログ

### 共通の計測実装ガイド
- すべてのログに `time.perf_counter()` と連番を付与
- `event.widget`, `event.x`, `event.y`, `tree.identify_region`, `tree.identify_column`, `tree.bindtags()`, `tree.focus_get()` を記録
- 100回程度操作してログを集める → 「無視された」イベントの A/B/C を 1 回で確定

---

## 各エージェント原文抜粋（折りたたみ）

<details>
<summary>Gemini (Step 2F-A) - 5仮説</summary>

H1 ダブルクリック判定 / H2 メインスレッドブロック / H3 ウィンドウ非アクティブ / H4 bind 競合 / H5 データ一時ロック。
各仮説は「概要・推測根拠・検証方法」の3部構成で提示された。
</details>

<details>
<summary>DeepSeek advisor (Step 2F-C) - 3仮説 (低レイヤ視点)</summary>

案1: ttk element ヒットテスト境界（テーマ・パディング・DPI）
案2: Win32 メッセージ取りこぼし
案3: ヘッダー再描画とクリック競合（ダブルバッファリング不整合）
DS 自身による採否コメント: 案1 は検証容易だが標準テーマでは稀。案2 は症状と整合だが検証困難。案3 はソート直後限定なら検証しやすい。
</details>

<details>
<summary>Codex review (Step 2F-E, medium) - A/B/C 切り分け方針</summary>

最優先: A=イベント未到達 / B=イベントあるが command 未呼び出し / C=command 走ったが見た目不変 を共通計測で 3 分岐に確定する。
推奨順: 共通計測 → H4 → H6 → H5 → H2 → H3 → H1 → H8 → H7。
H1 の `<Double-Button-1>` 追加バインド、H8 の `update_idletasks()` 挿入は副作用が大きく診断ノイズになるため、まず計測のみで判断すべき。
H7 (Win32) は 10% という頻度では high すぎるため最後。
</details>
