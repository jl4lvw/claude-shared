---
name: conphas-search
description: コンパス(CONPHAS)の仕入・売上の明細を読み取り専用で検索する。「入荷済みか」「いつ仕入れたか」「コンパスで検索」などで使う
---
<!-- SKILL_VERSION: 2026-10-07_100000 -->

# conphas-search — コンパスの仕入/売上を検索（読み取り専用）

寺下さんが https://sfuji.f5.si/conphas/ で行う検索を、このセッションから行う。
**読み取りだけ。書き込み・登録は絶対にしない**（書込は別の手順・承認が必要）。

## 使い方

```bash
PYTHONIOENCODING=utf-8 python C:/ClaudeCode/.claude/skills/conphas-search/conphas_search.py --mode purchase --from 2026-09-01 --kw 制帽
```

| オプション | 内容 |
|---|---|
| `--mode` | `purchase`=仕入(SMS・既定) / `sales`=売上(UMS) / `both` |
| `--from` `--to` | 期間(YYYY-MM-DD)。既定は直近30日。90日を超えると自動で分割して取得 |
| `--kw` | 品名・得意先名・備考のキーワード(複数はAND)。**全角/半角・大小文字は同一視**(コンパスの品名は半角カナ) |
| `--customer` | 得意先/仕入先コード(例: プラナリア=269) |
| `--json` | JSON Lines で出力 |

## 使いどころ

- **入荷済みか**: `--mode purchase` で品名を検索。仕入の計上日が入荷日の目安（実際の到着日と数日ずれることがある）
- 売れた実績: `--mode sales`
- 発注メールを書く前の「すでに入荷済みか」確認は、TASKS 入荷予定と併せて見る

## 注意

- 認証は `013.CONPHAS-PWA/server/external_secrets.json` の読み取りキー(`fuji-main`)。**キーの値を表示・記録しない**
- API は 1分10回までのレート制限あり（スクリプトは分割ごとに待つ）
- 仕入の日付は「入力・計上日」。品名は半角カナで保存されている
- 検索の根拠にした例: 2026/09/30 伝票01000185 制帽ﾊﾞｯｸﾞ 500個×790円 (株)ﾌﾟﾗﾅﾘｱ

## 関連

- 013.CONPHAS-PWA の外部データ API: `server/routers/external_data.py`（`/extract/rows`）
