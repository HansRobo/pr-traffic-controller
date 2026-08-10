#!/usr/bin/env bash
# ROS 2 等が PYTHONPATH 経由で注入する pytest プラグインを排除して実行する。
set -euo pipefail
cd "$(dirname "$0")"
exec env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH= .venv/bin/python -m pytest "$@"
