#!/usr/bin/env bash
# git >= 2.40 を要求する層2テストを docker で実行する。
# ローカルの git が 2.34 のため、実 git を使うテストはここを経由する。
#
# 実体は compose の test サービス（イメージ選定の理由は Dockerfile を見ること）。
# 直接 `docker compose run --rm test` と叩いてもよいが、その場合は
# DOCKER_UID/DOCKER_GID の export が要る。ここはそれを済ませる薄い委譲。
set -euo pipefail
cd "$(dirname "$0")"
export DOCKER_UID="$(id -u)" DOCKER_GID="$(id -g)"
exec docker compose run --rm test "$@"
