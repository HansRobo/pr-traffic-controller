#!/usr/bin/env bash
# ROS 2 等が PYTHONPATH 経由で注入する pytest プラグインを排除して実行する。
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  # 実行時依存は 0（stdlib のみ）なので pytest だけで足りる。CI と同じ形。
  # import は pytest の rootdir 挿入で解決するので editable install も不要。
  echo "エラー: .venv が無い。先に作ること:" >&2
  echo "  python3 -m venv .venv && .venv/bin/pip install pytest" >&2
  exit 1
fi
exec env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH= .venv/bin/python -m pytest "$@"
