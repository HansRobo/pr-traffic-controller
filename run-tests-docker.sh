#!/usr/bin/env bash
# git >= 2.40 を要求する層2テストを docker で実行する。
# ローカルの git が 2.34 のため、実 git を使うテストはここを経由する。
set -euo pipefail
cd "$(dirname "$0")"
exec docker run --rm --entrypoint sh -v "$PWD":/w -w /w alpine/git:latest -c '
  apk add --quiet python3 py3-pip >/dev/null 2>&1
  python3 -m venv /tmp/venv >/dev/null 2>&1
  /tmp/venv/bin/pip install -q pytest >/dev/null 2>&1
  git config --global --add safe.directory "*"
  exec /tmp/venv/bin/python -m pytest '"$*"'
'
