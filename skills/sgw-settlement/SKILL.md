---
name: sgw-settlement
description: SGW精算を管理するスキル。購入証憑のスクリーンショットから商品データを読み取ってJSONに登録、または登録済みの一覧を表示する。毎月20日締めで制服のフジに請求する精算データを扱う。
trigger: ユーザーが /sgw-settlement を実行、または「SGW精算」「SGWせいさん」と発言したとき
---

# SGW精算スキル

## 概要
SGWの購入証憑（Amazon・プリントパック・その他EC）のスクリーンショットから
商品データを読み取り、精算用JSONに蓄積する。毎月20日頃に締めて制服のフジで
請求する業務に使用する。

## データ仕様

### JSON格納先
`C:/Users/jl4lv/OneDrive/デスクトップ/0.フジ/900.ClaudeCode/006.secretary/007.SGW精算/settlements.json`

### 1レコードのフィールド
| フィールド | 型 | 説明 |
|---|---|---|
| id | str | `YYYYMMDD_NNN` 形式 |
| date | str | 注文日 `YYYY-MM-DD` |
| product_name | str | 商品名（数量・仕様含む） |
| quantity | int | 数量 |
| unit_price_ex | int | 単価（税抜） |
| unit_price_in | int | 単価（税込） |
| total_ex | int | 合計（税抜） = 単価税抜 × 数量 |
| total_in | int | 合計（税込） = 単価税込 × 数量 |
| created_at | str | 登録日時 ISO8601 |

## CLI

実行ディレクトリ: `006.secretary/007.SGW精算/`
スクリプト: `sgw_cli.py`

| コマンド | 用途 |
|---|---|
| `python sgw_cli.py` | 一覧表示（日付降順・合計付き） |
| `python sgw_cli.py list` | 一覧表示 |
| `python sgw_cli.py list --month 2026-04` | 月次フィルタ（20日締め: 3/21〜4/20） |
| `python sgw_cli.py add --date YYYY-MM-DD --name "..." --quantity N --price-ex N --price-in N` | 1件追加（重複チェック付き） |
| `python sgw_cli.py delete <No>` | list表示のNo指定で削除 |
| `python sgw_cli.py check-dup` | 登録重複チェック |

### 重複判定
- **キー**: 日付 + 商品名 + 税込合計（完全一致）
- `add` 時に自動警告＆スキップ。`--force` で強制登録可
- 注文番号が異なる同一商品（別日発注）は重複扱いしない

## 実行フロー

### パターンA: 引数なし / 「一覧」「見せて」
1. `python sgw_cli.py` を実行
2. 結果をそのままユーザーに提示（マークダウンテーブル＋合計）

### パターンB: スクリーンショット添付
1. 画像から以下を読み取る:
   - 注文日（無ければ今日の日付 `currentDate`）
   - 商品名（サイズ・枚数・仕様を含めて完全な文字列で）
   - 数量（`〇〇枚×N` の N）
   - 単価（税抜 / 税込 両方。片方しかなければ 1.1 倍/÷1.1 で逆算）
2. 1商品ずつ `python sgw_cli.py add ...` を実行
3. 重複警告が出たらユーザーに確認（「別日なら登録、二重なら無視」）
4. 最後に `python sgw_cli.py` で更新後の一覧を表示

### パターンC: 「今月分」「4月分」
1. 現在日付から月次指定を解釈（毎月20日締めなので、4/21〜5/20 なら "5月分"）
2. `python sgw_cli.py list --month YYYY-MM` を実行
3. 結果を提示

### パターンD: 「No.5 削除」
1. `python sgw_cli.py delete 5` を実行
2. 結果を提示

## 表示ルール

- テーブル形式は固定:
  `| No | 日付 | 商品名 | 数量 | 単価（税抜） | 単価（税込） | 合計（税抜） | 合計（税込） |`
- 日付降順ソート
- テーブル下に税抜・税込の合計を別テーブルで表示

## 金額の読み取り時の注意

- **Amazon**: 税込のみ表示される → 税抜 = 税込 ÷ 1.1（小数切り捨て）
- **プリントパック**: 税抜・税込の両方が明記される → そのまま使用
- **セブンエス（制服のフジ宛）**: 税抜単価 + 税込合計 → 税込単価は合計から逆算

## 関連メモ
- `memory/project_sgw_settlement.md` — SGW精算のルール
- 締め日: 毎月20日頃
- 請求先: 制服のフジ
