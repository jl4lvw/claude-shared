---
name: import-cost
description: 輸入・仕入れ原価を蓄積し、後日FUJIへの請求書作成に使うスキル。中国輸入の外貨原価管理にも対応。売価は後から手入力で設定する。
---

# import-cost スキル

## 目的
購入したが**まだ請求書を作っていない**仕入原価を蓄積し、後日売価を設定して FUJI への請求書を組み立てる。
中国輸入（外貨×為替）も国内仕入も同じテーブルで管理。

## CLI
場所: `006.secretary/009.原価蓄積/cost_cli.py`
データ: `costs.json`（同ディレクトリ）

## サブコマンド

| コマンド | 用途 |
|---|---|
| `list [--all] [--status 未発行\|発行済] [--supplier S]` | 既定は未発行のみ。`--all` で全件 |
| `add --date --supplier --name --quantity --currency [JPY/CNY/USD] --unit-price ...` | 1件追加 |
| `delete <No>` | list表示のNoで削除 |
| `check-dup` | 登録重複チェック |
| `set-price <No> <売価>` | 売価を後付け設定 |
| `mark-invoiced <No> [--invoice-no INV]` | 請求書発行済マーク（売価必須） |
| `export-csv [--output P] [--status S] [--include-missing]` | CSV出力（id/商品名/数量/売価） |

## 外貨入力例

```bash
# 中国仕入（CNY）
python cost_cli.py add --date 2026-06-24 --supplier "中国-Alibaba店名" \
    --name "雪駄 黒 26cm" --quantity 100 \
    --currency CNY --foreign-price 25.50 --exchange-rate 22.5 \
    --shipping-fee 8000 --customs-duty 2000

# 円単価を手入力で上書き（計算値と5%以上乖離すると警告）
python cost_cli.py add --date 2026-06-24 --supplier "中国-Taobao" \
    --name "雪駄 白 27cm" --quantity 50 \
    --currency CNY --foreign-price 25.50 --exchange-rate 22.5 \
    --unit-price 600

# 国内仕入（JPY）
python cost_cli.py add --date 2026-06-24 --supplier "モリフロッキー" \
    --name "Tシャツ M 黒" --quantity 30 \
    --currency JPY --unit-price 800
```

## 売価設定〜請求書発行フロー

```bash
# 1. 一覧で未発行を確認
python cost_cli.py list

# 2. 売価を設定（No指定）
python cost_cli.py set-price 1 1200

# 3. 請求書発行済をマーク（売価必須）
python cost_cli.py mark-invoiced 1 --invoice-no INV-2026-001

# 4. 月次でCSV出力（発行済のみ等）
python cost_cli.py export-csv --status 発行済
```

## フィールド

| 名前 | 必須 | 説明 |
|---|---|---|
| id | 自動 | YYYYMMDD_NNN |
| date | ○ | 仕入日 |
| supplier | ○ | 仕入先（例: 中国-Alibaba, モリフロッキー） |
| product_name | ○ | 商品名 |
| sku | - | 品番（任意） |
| currency | ○ | JPY / CNY / USD |
| foreign_unit_price | 外貨時○ | 外貨単価 |
| exchange_rate | 外貨時○ | 1外貨=何JPY |
| unit_price_jpy | ○ | 円換算単価 |
| quantity | ○ | 数量 |
| shipping_fee | - | 送料(円) 既定0 |
| customs_duty | - | 関税(円) 既定0 |
| other_costs | - | 諸経費(円) 既定0 |
| item_cost_jpy | 自動 | 商品単体原価 = unit_price_jpy × quantity |
| total_cost_jpy | 自動 | 全込 = item_cost_jpy + shipping_fee + customs_duty + other_costs |
| selling_price | - | 売価（set-price で後付け） |
| invoice_status | ○ | "未発行" / "発行済" 既定 "未発行" |
| invoice_no | - | 請求書番号 |
| memo | - | メモ |
| created_at | 自動 | ISO datetime |
| invoiced_at | 自動 | 発行済マーク時刻 |

## 注意
- **発行済→未発行への戻し**は未実装（必要なら手動でJSON編集）
- **archive は未実装**（長期蓄積前提。--status 発行済 で過去分を除外して運用）
- 売価未設定の行は export-csv で既定除外。`--include-missing` で含める

## 関連
- 精算: [sgw-settlement](../sgw-settlement/SKILL.md)
- 仕入れ: [sgw-purchase](../sgw-purchase/SKILL.md)
