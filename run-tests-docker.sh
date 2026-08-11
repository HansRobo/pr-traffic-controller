#!/usr/bin/env bash
# git >= 2.40 を要求する層2テストを docker で実行する。
# ローカルの git が 2.34 のため、実 git を使うテストはここを経由する。
set -euo pipefail
cd "$(dirname "$0")"
# イメージは alpine/git のまま変えないこと。CI (.github/workflows/test.yml) が
# ubuntu (git 2.4x) と alpine/git (git 2.5x) の 2 レグで「git のバージョン差で
# 出力形式が変わっていないか」を見ており、これはその alpine 側の再現である。
# python 同梱の別イメージに替えると git のバージョンが動き、再現の意味が消える。
#
# pytest は venv + pip ではなく apk の py3-pytest を使う。musl なので
# actions/setup-python が使えず、この形が CI 側にもそのまま移せる。
# （venv + pip install は毎回 6.5s かかっていた。apk 版なら 2.0s）
exec docker run --rm --entrypoint sh \
  -v "$PWD":/w -w /w \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  alpine/git:latest -c '
  apk add --quiet python3 py3-pytest >/dev/null 2>&1
  git config --global --add safe.directory "*"
  exec python3 -m pytest -p no:cacheprovider '"$*"'
'
