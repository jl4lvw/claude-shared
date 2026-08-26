---
name: module-graph
description: Memory MCPのmemory.jsonに、サブプロジェクト間のHTTP依存関係を自動検出して反映するスキル。.claude/launch.jsonのポート定義とソースコード中の127.0.0.1:<port>参照を突き合わせて依存辺を検出する(モノレポではPythonのimportではなくHTTP呼び出しで依存するため)。手動で作成したエンティティ/リレーションは専用タグ(http_calls_detected)で区別し、絶対に上書きしない。dry-runで検出結果を必ず目視確認してから反映する(一時ファイル置き場や無関係スクリプトの寄せ集めフォルダを誤ってモジュール扱いする既知の誤検出パターンがあるため)。「モジュール依存を更新して」「memory.jsonを最新化して」「3Dビューアーのグラフを更新して」等で起動。
---

# module-graph — サブプロジェクト間依存の自動検出・反映

`.claude/tools/refresh_module_graph.py` を安全な手順で実行するためのスキル。
スクリプト自体は単純だが、**dry-runの結果を鵜呑みにせず目視確認する**規律を
毎回再現することが目的(過去に一時ファイル置き場や無関係スクリプトの寄せ集め
フォルダを誤ってモジュールとして検出した実績があるため)。

## 使い方

```
/module-graph          # 通常実行(dry-run→確認→反映→検証)
/module-graph check    # dry-runのみ、反映しない
```

## 手順

### Step 1: dry-run

```bash
python "C:/ClaudeCode/.claude/tools/refresh_module_graph.py" --dry-run
```

### Step 2: 検出結果を目視確認(省略禁止)

以下の観点で、検出された各エンティティ・辺が「実態のある単一のモジュール」を
指しているか確認する:

- **一時ファイル・バックアップ・アーカイブ置き場**(例: `000.tmp`)が紛れていないか
- **無関係な個別スクリプトの寄せ集めフォルダ**(例: `001.Python`)が単一モジュール
  扱いになっていないか
- 検出された依存の向き(caller→callee)が直感に反していないか(逆向きに見える場合、
  ポート番号の取り違えの可能性がある)

問題があれば、`.claude/tools/refresh_module_graph.py` の `_EXCLUDE_FOLDERS` に
追記してから Step 1 に戻る(スクリプト自体を手で書き直さず、除外リストの追加のみ)。

### Step 3: 反映

Step 2 で問題なければ実行:

```bash
python "C:/ClaudeCode/.claude/tools/refresh_module_graph.py"
```

手動で作成したエンティティ・リレーションは`http_calls_detected`以外の型を持つため、
このスクリプトによって変更・削除されることはない(安全設計)。

### Step 4: 検証(省略禁止)

```bash
python "C:/ClaudeCode/.claude/tools/ensure_lf.py" --check "C:/ClaudeCode/900.ClaudeCode/mcp-memory-3d-viewer/memory.json"
```

`[ensure_lf] OK` を確認する。NGの場合は反映前のバックアップは無いため、
Gitの差分(`git diff -- 900.ClaudeCode/mcp-memory-3d-viewer/memory.json`)から
手動で復旧するか、`git checkout -- <path>` で直前のコミット状態に戻す。

### Step 5: 3Dビューアーへの反映確認(任意)

3Dビューアー(`900.ClaudeCode/mcp-memory-3d-viewer/`)が起動中であれば、
ブラウザで開いているタブの「再読込」ボタンを押し、エンティティ/リレーション数が
増えていることを目視確認する。ビューアーが未起動でも本スキルの主目的には影響しない。

## いつ使うか

- 新しいサブプロジェクトを追加した後
- サービス間のHTTP連携(あるモジュールが別モジュールのAPIを呼ぶ構成)を新設・変更した後
- Memory MCPのグラフが古い気がする、と思ったとき

**毎回の編集のたびに実行する必要はない**(構造的な変化があったときのメンテナンス作業)。

## 注意

- `.claude/launch.json` にポート定義が無いサービスは検出対象外(launch.jsonの
  更新が先に必要)
- HTTP呼び出し以外の依存(共有ライブラリのimport、DBの直接共有等)は検出しない
  (今回のモノレポで支配的な依存形態がHTTP呼び出しだったための設計)
- 検出精度は正規表現ベースのヒューリスティックであり、完全ではない。
  Step 2の目視確認を省略しないこと
