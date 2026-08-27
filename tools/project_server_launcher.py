"""Serena ProjectServerの薄い拡張ラッパー起動スクリプト。

背景:
  素のSerena `start-project-server` は、クエリされたプロジェクトを一度ロードしたら
  プロセス終了までメモリに保持し続け、外部から「今何がロードされているか」を見る
  手段も「特定のプロジェクトだけ解放する」手段も無い(2026-08-27、Serena本体の
  ソース(project_server.py)を確認して判明)。

  本スクリプトはSerenaの`ProjectServer`クラスをそのまま継承し、
  `/status`(ロード済みプロジェクト一覧)と`/unload_project`(指定プロジェクトの解放)
  という2つのHTTPエンドポイントだけを追加する。Serena本体のインストール済み
  ファイルは一切変更しない(uv tool更新時に上書きされて消える心配がない)。

なぜ共有ProjectServerを使うか:
  Serenaを素朴に使うと「1 Claude Codeセッション = 1 Serenaインスタンス」となり、
  同じサブプロジェクトを複数のteammateが見るだけでも、その数だけ同じLSP
  (pyright)のインデックスが重複してメモリに載る(2026-08-27の障害調査で実測)。
  読み取り専用のシンボル探索(find_symbol・参照検索・診断取得など)は
  `query_project`ツール経由でこの共有プロセスに集約でき、どのteammateから
  問い合わせても実体は1個分のインデックスで済む。実際にコードを編集する
  操作(replace_symbol_body等)だけは、editする本人のセッションが
  `activate_project`でそのサブプロジェクトだけをローカルに持つ必要がある。

用途:
  - 常駐起動: `<serena-agentのvenv python> project_server_launcher.py`
              (ログイン時にスケジュールタスクから起動する想定。フラグ無しで実行すると
              Flaskサーバーとして起動し、呼び出しはブロックしたままになる)
  - 状態確認: `python project_server_launcher.py --status`
  - 解放:     `python project_server_launcher.py --unload <project_nameまたはpath>`
              (--status/--unloadは`requests`だけあれば動くので、システムのpythonで
              呼んでよい。サーバー本体の起動だけはSerenaのvenvが必要)

**重要**: このスクリプトはSerenaのMCP標準入出力プロトコル(serena_launcher.py側)
とは無関係の、別系統の常駐HTTPプロセス。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HOST = "127.0.0.1"

# Serenaの既定ポート(0x5EA1 = 24225)をそのまま使う。
# .claude/launch.json の既存ポート一覧と衝突しないことを確認済み(2026-08-27)。
PORT = 0x5EA1

# 常駐サーバー本体の実行に必要な、serena-agentのuv tool venv内のpython。
# (`serena`パッケージがインポートできるのはこのvenvだけ。--status/--unloadは
# `requests`だけで動くので、この定数はサーバー起動時にしか使わない)
SERENA_AGENT_PYTHON = str(
    Path.home() / "AppData" / "Roaming" / "uv" / "tools" / "serena-agent" / "Scripts" / "python.exe"
)

# ログイン時にpythonw.exe(コンソール非表示)で起動する想定のため、標準出力・
# 標準エラーは既定では捨てられる。クラッシュ時に追えるよう、ファイルへ退避する。
_LOG_DIR = Path(__file__).resolve().parent / "logs"
_LOG_FILE = _LOG_DIR / "project_server.log"


def _redirect_output_to_log_file() -> None:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_fh = open(_LOG_FILE, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 — プロセス終了まで保持する意図的な常時オープン
    sys.stdout = log_fh
    sys.stderr = log_fh


def _run_server() -> None:
    _redirect_output_to_log_file()

    # サーバー実行時にのみSerena本体をインポートする(--status/--unloadは
    # serenaパッケージが無いシステムpythonからも呼べるようにするため)。
    from flask import request
    from serena.project_server import ProjectServer

    class ExtendedProjectServer(ProjectServer):
        """Serena純正のProjectServerに/status・/unload_projectを追加するだけの薄い拡張。"""

        def _setup_routes(self) -> None:
            super()._setup_routes()

            @self._app.route("/status", methods=["GET"])
            def status() -> dict:
                with self._loaded_projects_lock:
                    loaded = list(self._loaded_projects_by_root.keys())
                return {"loaded_projects": loaded}

            @self._app.route("/unload_project", methods=["POST"])
            def unload_project():
                payload = request.get_json(force=True, silent=True) or {}
                target = payload.get("project_root") or payload.get("project_name")
                if not target:
                    return {"error": "project_root or project_name is required"}, 400

                # 名前指定にも対応するため、Serena本体の解決ロジック(_get_project等)と
                # 同じ方法でrootパスを求める。未登録の場合は入力値をそのままキーとして扱う。
                registered_project = self._agent.serena_config.get_registered_project(target)
                key = str(registered_project.project_root) if registered_project is not None else target

                with self._loaded_projects_lock:
                    project = self._loaded_projects_by_root.pop(key, None)
                if project is None:
                    return {"status": "not_loaded", "project": key}, 404
                project.shutdown()
                return {"status": "unloaded", "project": key}

    server = ExtendedProjectServer(host=HOST, port=PORT)
    server.run()


def _cli_status() -> int:
    import requests

    try:
        resp = requests.get(f"http://{HOST}:{PORT}/status", timeout=5)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[project_server_launcher] 共有サーバーに接続できません: {exc}", file=sys.stderr)
        return 1

    loaded = resp.json().get("loaded_projects", [])
    if not loaded:
        print("ロード済みのプロジェクトはありません。")
    else:
        print("ロード済みプロジェクト:")
        for root in loaded:
            print(f"  - {root}")
    return 0


def _cli_unload(target: str) -> int:
    import requests

    try:
        resp = requests.post(
            f"http://{HOST}:{PORT}/unload_project",
            json={"project_root": target},
            timeout=10,
        )
    except requests.RequestException as exc:
        print(f"[project_server_launcher] 共有サーバーに接続できません: {exc}", file=sys.stderr)
        return 1

    data = resp.json()
    if resp.status_code == 200:
        print(f"解放しました: {data.get('project')}")
        return 0
    print(f"解放できませんでした(status={resp.status_code}): {data}", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--status", action="store_true", help="ロード済みプロジェクト一覧を表示する")
    parser.add_argument("--unload", metavar="NAME_OR_PATH", help="指定プロジェクトを解放する")
    args = parser.parse_args()

    if args.status:
        return _cli_status()
    if args.unload:
        return _cli_unload(args.unload)

    _run_server()
    return 0


if __name__ == "__main__":
    sys.exit(main())
