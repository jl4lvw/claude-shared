---
name: mail-reply
description: メール返信案をアーティファクトで表示して確認→修正→EML生成→Thunderbird送信する標準手順。返信は必ず引用形式で、アーティファクト表示で URL・金額・敬語をチェックしてからメーラーで送信する。
trigger: 顧客対応メール・取引先との交渉メール等、返信メールを作成して送信したいとき
---

<!-- SKILL_VERSION: 2026-09-18_000000 -->

# mail-reply — メール返信案の作成・確認・送信

メール返信を安全かつ確実に送信するための標準手順。**アーティファクト表示→承認→EML生成→
Thunderbird送信の共通土台は[[mail-send]]に切り出した**(2026-09-18。新規スレッドメールにも
同じ土台を使うため)。本スキルは**返信特有**の要件(引用形式・案件管理システム連携)に絞る。
EML生成のコード例(`policy=SMTP`+`write_bytes`+添付対応)・Thunderbird起動コマンドは
[[mail-send]]の該当節を参照(下記3️⃣4️⃣は要点のみ)。

## 🎯 手順概要

```
1. メール本文を作成（本文→区切り線→引用元の正しい形式）
2. アーティファクト(HTML)で表示・確認
3. 修正があれば Edit で該当部分を差し替え
4. ユーザーが「OK」承認
5. EML ファイル生成
6. Thunderbird で送信
7. 066.業務秘書 など案件管理システムで「処理済み」マーク
```

---

## 1️⃣ メール本文の正しい形式（必須）

### ✅ 正しい形式: 本文→区切り線→引用元

```
柴山様

いつもお世話になっております。
制服のフジ　寺下です。

[ここに返信本文]

今後ともよろしくお願いいたします。

制服のフジ
寺下

―――――――――――――――――――――――――――――――――

> [元のメールの差出人]
>
> [元のメールの本文]
```

### ❌ 禁止：引用が上に来る形式

引用文を本文の上に置くと、読み手が前置きなく引用を読まされることになり、**ビジネスマナー上あり得ません**。(2026-07-XX ユーザー指示: 「本題に入る前に自分の送った文章を全て見直すというのは、ビジネスマナー上あり得ません」)

---

## 2️⃣ アーティファクトで表示・確認（重要）

### 利点
- ✅ テキストより見やすい
- ✅ URL や金額が一目瞭然
- ✅ 修正指示が簡単（「URLを新しいものに」など）
- ✅ 複数メール並行処理で進捗が視覚的に明確

### HTML アーティファクトの構成

```html
<!DOCTYPE html>
<html>
<head>
    <style>
        .header { background: #f5f5f5; padding: 16px; border-left: 4px solid #27ae60; }
        .email-body { font-family: monospace; white-space: pre-wrap; padding: 20px; }
        .quoted { border-left: 3px solid #ddd; padding-left: 12px; color: #666; }
        .warning { background: #fff3cd; border: 1px solid #ffc107; padding: 12px; margin: 20px 0; }
    </style>
</head>
<body>
    <div class="header">
        <h2>✉️ 返信メール（案）</h2>
        <div>宛先: xxx@example.com（相手先名）</div>
        <div>件名: Re: 元の件名</div>
    </div>

    <div class="warning">
        ⚠️ <strong>送信前確認:</strong> 商品ページURLが正確か確認してください
    </div>

    <div class="email-body">柴山様

いつもお世話になっております。
制服のフジ　寺下です。

[本文ここから]

商品ページ：https://seifukunofuji.co.jp/SHOP/G1956.html

[本文ここまで]

―――――――――――――――――――――――――――――――――

<div class="quoted">&gt; [引用元のメール本文]</div>
    </div>
</body>
</html>
```

### チェックポイント

送信前に必ず以下を確認：
- [ ] 敬語・表現は適切か（「いつもお世話になっております」等）
- [ ] URL は正確か（ドメイン・パス・クエリ）
- [ ] 金額・数字は正確か
- [ ] 商品名は正確か（G1956(掃海艇みやじま)等、括弧書き併記）
- [ ] 引用文は正しい形式か（区切り線の下に正しく配置）
- [ ] AI作成の断り書きが**入っていないか**（禁止: 2026-08-25ユーザー指示）

---

## 3️⃣ EML ファイル生成・4️⃣ Thunderbird で送信

**[[mail-send]]の3️⃣4️⃣をそのまま使う**(`EmailMessage(policy=SMTP)` + `write_bytes()`。
`write_text()`は使わない、Windowsの改行変換でMIME構造が壊れるおそれがあるため)。
返信の場合、本文は上記1️⃣の「本文→区切り線→引用元」形式にする点だけが新規スレッドとの違い。

添付ファイルが無い返信メールでも書き方は同じ(`msg.add_attachment(...)`の行を省くだけ)。

---

## 5️⃣ 案件管理システムで「処理済み」マーク

### 066.業務秘書の場合

```powershell
$body = @{
    account = "fw-terashita@seifukunofuji.com"
    case_id = "案件ID"
    on = $false
    state = "done"
    revision = 3
    reason = "柴山豊美さんへフライトジャケット販売再開のお知らせを送付"
} | ConvertTo-Json

$response = Invoke-WebRequest `
    -Uri "http://127.0.0.1:8318/api/case/claude" `
    -Method Post `
    -Body $body `
    -ContentType "application/json" `
    -Headers @{ "X-Requested-With" = "hisho" } `
    -UseBasicParsing

Write-Host "✓ 処理済みとしてマーク: $($response.StatusCode)"
```

---

## 💡 ベストプラクティス

### 複数メールを並行処理する場合

1. 全メールについて**アーティファクト作成**
2. 順番に確認・修正
3. 確認済みのものから EML 生成・Thunderbird 送信
4. **すべて送信完了した後**に案件管理システムを一括更新

### 修正指示のサンプル

ユーザー → Claude:
- 「そのURLを新しいものに (`https://...新しいURL`) に変えて」
- 「敬語を『お待たせいたしました』から『本日お待たせいたしました』に」
- 「商品名に(G1956)を併記して」

Claude → Edit で該当部分を Edit ツールで修正 → アーティファクト再表示

---

## 🔗 関連

- [[mail-send]] — アーティファクト表示・EML生成・Thunderbird起動の共通手順(返信・新規スレッド共通)
- [[feedback_email_reply_must_quote_original]] — メール返信は引用形式必須
- [[feedback_no_ai_disclaimer_in_emails]] — AI作成断り書き禁止
- [[feedback_thunderbird_compose_via_eml]] — EML方式での Thunderbird起動
- [[feedback_email_reply_artifact_preview]] — このスキル化を実施した判断

---

**更新日**: 2026-09-15
**作成背景**: メール返信案をアーティファクトで表示する方法が「非常に良いやり方」と判定されたため、スキル化した。
