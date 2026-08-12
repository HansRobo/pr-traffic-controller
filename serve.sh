#!/usr/bin/env bash
# clone 直後にサイトを見るための 1 コマンド。イメージのビルド・解析・配信を束ねる。
#
#   ./serve.sh OWNER/NAME                 解析してから配信（統合ラインは既定ブランチ）
#   ./serve.sh OWNER/NAME --lines a,b     統合ラインを明示する（分岐した統合先が複数ある場合）
#   ./serve.sh                            既存の解析結果をそのまま配信
#   ./serve.sh --refresh                  蓄積を全件更新して配信
#
# 環境変数: PORT（既定 8000）/ NO_OPEN=1 でブラウザを開かない。
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
export PORT DOCKER_UID="$(id -u)" DOCKER_GID="$(id -g)"

usage() {
  cat >&2 <<'EOS'
使い方:
  ./serve.sh OWNER/NAME                 解析してから配信
  ./serve.sh OWNER/NAME --lines a,b     統合ラインを明示する
  ./serve.sh                            既存の解析結果をそのまま配信
  ./serve.sh --refresh                  蓄積を全件更新して配信
EOS
}

# 解析は gh 経由で GitHub を叩く。トークンはホストの gh から借りるだけで、
# コンテナに ~/.config/gh をマウントはしない。
analyze() {
  local token
  if ! token="$(gh auth token 2>/dev/null)" || [ -z "$token" ]; then
    echo "エラー: gh が未認証です。'gh auth login' を実行してください。" >&2
    exit 1
  fi
  GH_TOKEN="$token" docker compose run --rm analyze "$@"
}

if [ $# -eq 0 ] && [ ! -f docs/data/index.json ]; then
  echo "解析結果がまだありません。対象リポジトリを指定してください。" >&2
  echo >&2
  usage
  exit 1
fi

repo=""
if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then
  # ハイフン始まりでない第1引数は OWNER/NAME として扱う。
  # ビルドに入る前に弾いて、打ち間違いを待たせない。
  repo="$1"
  shift
  case "$repo" in
    */*) ;;
    *) echo "エラー: 対象は OWNER/NAME 形式で指定してください（受け取った: $repo）" >&2
       echo >&2; usage; exit 1 ;;
  esac
fi

echo "==> イメージを用意します（初回のみ時間がかかります）"
docker compose build --quiet

if [ -n "$repo" ] || [ $# -gt 0 ]; then
  if [ -n "$repo" ]; then
    if ! printf '%s\n' "$@" | grep -qE '^--lines(=|$)'; then
      # 統合ラインは必須引数。既定ブランチが main とは限らない（master 等）ので
      # 決め打ちにせず gh に問い合わせる。
      line="$(gh repo view "$repo" --json defaultBranchRef \
                -q .defaultBranchRef.name 2>/dev/null || true)"
      [ -n "$line" ] || line=main
      echo "==> 統合ラインに既定ブランチ '$line' を使います（--lines で変更できます）"
      set -- --lines "$line" "$@"
    fi
    analyze --repo "$repo" "$@"
  else
    analyze "$@"
  fi
fi

url="http://localhost:$PORT/"
if [ -z "${NO_OPEN:-}" ] && command -v xdg-open >/dev/null 2>&1 \
   && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
  # 配信開始を待ってから開く。開けなくても配信は続ける。
  (
    for _ in $(seq 20); do
      sleep 0.3
      curl -sf -o /dev/null "$url" 2>/dev/null && break
    done
    xdg-open "$url" >/dev/null 2>&1 || true
  ) &
fi

echo "==> $url で配信します（Ctrl-C で停止）"
# compose を前面で待つと、bash は SIGINT の trap を子の終了まで走らせない。
# 端末の Ctrl-C はプロセスグループ全体に届くので普段は困らないが、
# スクリプトだけにシグナルが来た場合に停まらなくなる。子へ転送して待つ。
docker compose up viewer &
viewer_pid=$!
trap 'kill -INT "$viewer_pid" 2>/dev/null || true' INT TERM HUP
wait "$viewer_pid" || true
docker compose down >/dev/null 2>&1 || true
