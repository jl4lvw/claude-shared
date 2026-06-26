---
name: sgw-purchase
description: SGW仕入れ請求を管理するスキル。受領済の請求書を蓄積し、毎月20日締めでFUJIへ転送する。精算データ(sgw-settlement)とは別表として完全分離。
---

# sgw-purchase スキル

## 目的
業者から既に受領済の仕入れ請求書を蓄積し、月次（20日締め）でFUJIへ転送請求するデータを管理する。
**精算（sgw-settlement）とは完全に別物**として扱う。

## CLI
場所: `006.secretary/008.仕入れ/purchase_cli.py`
データ: `purchases.json`（同ディレクトリ）

## サブコマンド

| コマンド | 用途 |
|---|---|
| `list [--month YYYY-MM]` | 一覧表示（日付降順・合計付き） |
| `add --date --name --quantity --price-ex --price-in` | 1件追加 |
| `delete <No>` | list表示のNoで削除 |
| `check-dup` | 登録重複チェック |
| `archive [--label YYYY-MM-DD] [--force]` | 全件アーカイブ→リセット |
| `export-csv [--output PATH] [--month YYYY-MM]` | CSV出力（freee取込用） |

## 起動キーワード
- `/sgw-purchase`
- 「仕入れリスト」「仕入れ表示」「仕入れ請求」「purchase」など

## 既定動作
- 引数なしで `/sgw-purchase` → list 表示
- スクショ等で具体的な請求情報があれば確認なしで即 add する運用も可

## 関連
- 精算: [sgw-settlement](../sgw-settlement/SKILL.md)
- 原価蓄積: [import-cost](../import-cost/SKILL.md)
