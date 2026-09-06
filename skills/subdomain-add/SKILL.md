---
name: subdomain-add
description: 新しい PWA / Web サービスを `<name>.sfuji.f5.si` で公開するときの手順。Caddy に vhost を追加し、本社ルーター(RTX1300)の静的 DNS 追加を TK へ依頼し、LAN 内から到達できることを検証するまでの一式。「サブドメイン追加」「DNS追加」「新しいPWAを公開」などで起動。
---

# /subdomain-add — sfuji.f5.si サブドメインの追加

新しいサブドメインを公開するとき、**Caddy に vhost を足すだけでは社内から開けない**。
本社ルーターの静的 DNS 追加を毎回依頼する必要がある。

## 前提: なぜ毎回 DNS 追加が要るのか

| 経路 | 挙動 |
|---|---|
| 外部(インターネット) | DDNS で `118.6.181.74` に解決 → Caddy(192.168.1.166) に届く。**何もしなくても動く** |
| LAN 内 | ルーター(RTX1300)が MAP-E 回線で**ヘアピン NAT 非対応**。公開 IP のままでは**絶対に到達できない** |

そのため「サブドメイン → 192.168.1.166」をルーターの静的 DNS へ 1 件ずつ登録する。

**ワイルドカード(`*.sfuji.f5.si`)は 2026-09 に廃止された。依頼してはいけない。**
担当サーバー(192.168.1.74)の障害が繰り返されたため。RTX1300 の `dns static` コマンド
自体もワイルドカード非対応(`dns static a *.sfuji.f5.si ...` は設定できない)。

> 2026-08-15〜2026-09 の間だけワイルドカードが効いていた時期があり、当時の記録には
> 「DNS 依頼は不要」と書かれているものがある。**現在は個別登録のみ**。

## 手順

### 1. Caddy に vhost を追加して reload

`C:\ClaudeCode\inventory-replenishment-pwa\Caddyfile` に vhost を追記し、反映する。

```bash
powershell -File "C:\ClaudeCode\040.管理者権限コマンド実行\reload_caddy.ps1"
```

管理者権限は不要(Admin API 経由のグレースフル reload)。外部向け証明書はこれで自動取得される。

### 2. DNS 追加を TK へ依頼

伝えるのは**サブドメイン名だけ**でよい。転送先が `192.168.1.166` 以外の場合のみ IP を添える
(例外は `rdp200.sfuji.f5.si → 192.168.1.200` のみ)。

```bash
python ".claude/skills/relay/scripts/relay_client.py" send --to TK --type task "DNS追加依頼: <name>.sfuji.f5.si -> 192.168.1.166"
```

口頭・チャットで運用者(寺下)に「<name> の DNS 追加お願いします」と伝えるのでも可。所要 30 秒。

複数まとめて依頼するときは 1 通に列挙する(スレッドを分けない)。

### 3. 反映を検証する(必須・ここまでで 1 セット)

**「依頼した」で終えない。** 完了連絡を受けたら LAN 内 PC で実際に確かめる。

```bash
python .claude/skills/subdomain-add/scripts/audit_subdomain_dns.py --host <name>
```

`exit 0` かつ `192.168.1.166` が返れば完了。手で確かめるなら:

```bash
nslookup <name>.sfuji.f5.si
curl -I https://<name>.sfuji.f5.si/
```

反映まで最大 60 秒(ttl=60)。古い値が出るときは `ipconfig /flushdns` してから再確認する。

## 登録漏れの監査

Caddy が配信している全 vhost と実際の名前解決を突き合わせる:

```bash
python .claude/skills/subdomain-add/scripts/audit_subdomain_dns.py
```

未登録があれば `exit 1` で、そのまま送れる依頼文を出力する。
**新規公開のたびだけでなく、Caddyfile を触ったときにも回す。**

> 2026-09-07 にこの監査を初めて行い、**Caddy 配信中の 8 件が未登録**だったことが判明した
> (camera / goqslip / goqstock / letter / memory3d / ql800 / session-watchdog / tanaoroshi)。
> 外部からは開けるため誰も気づかないまま埋もれていた。

判定はルーター設定の写しではなく**実際の名前解決**で行う(解決先がプライベート IP なら登録済み)。
一覧を転記した表は古くなるが、名前解決は常に現在の状態を返す。

## トラブルシュート

| 症状 | 原因と対処 |
|---|---|
| LAN 内で `118.6.181.74` に解決される / タイムアウト | DNS 未登録。手順 2 で依頼する |
| 登録済みなのに古い IP が出る | `ipconfig /flushdns` してから再確認 |
| 外部からは開けるのに社内から開けない | 同上(未登録の典型症状) |
| 登録済みなのに 502 | DNS は正常。Caddy の背後のアプリが起動していない。サーバー側を見る |

## 参考: 本社側で実行される作業(依頼側の作業ではない)

本社 PC で 1 コマンド。RTX1300 に `dns static a <name>.sfuji.f5.si 192.168.1.166 ttl=60` を
追加して save するだけで、既存エントリには触らない。

```
python C:/Users/jl4lv/router_dns_add.py <サブドメイン名>
```

## 登録済み一覧(2026-09-07 時点・15 件)

**正はルーター側の設定**。この表は参考で、確認は監査スクリプトで行う。

`sfuji` / `airadio` / `ecm` / `inbox` / `museum-sales` / `mytasks` / `portal` / `regi` /
`relay` / `relay-mobile` / `relay-viewer` / `rickiey` / `sgw-seisan` / `staging`
… すべて `192.168.1.166`。`rdp200` のみ `192.168.1.200`(RDP 用の例外)。

## 呼び出し名

- `/subdomain-add`
- 「サブドメイン追加」「DNS追加依頼」「新しい PWA を公開したい」

出典: 2026-09-07「【社内共有】*.sfuji.f5.si サブドメインの LAN 内 DNS 追加ルール」
(本社ルーター管理セッション作成)。
