# ローカル開発用イメージ。ホストの git が 2.40 未満でも全テストと解析が走る。
#
# ベースは alpine/git のまま変えないこと。CI (.github/workflows/test.yml) が
# ubuntu (git 2.4x) と alpine/git (git 2.5x) の 2 レグで「git のバージョン差で
# merge-tree -z の出力形式が変わっていないか」を見ており、これはその alpine 側の
# 再現である。python 同梱の別イメージに替えると git のバージョンが動き、
# 再現の意味が消える。
FROM alpine/git:latest

# 実行時 apk ではなくビルド時に焼く。apk は root を要するので、焼いておけば
# 実行時は非 root で回せる。bind mount した docs/data に root 所有の
# 解析結果を残さないためにこれが要る（毎回の apk add 2.0s も消える）。
#
# pytest は venv + pip ではなく apk の py3-pytest を使う。musl なので
# actions/setup-python が使えず、この形が CI 側にもそのまま移せる。
# （venv + pip install は毎回 6.5s かかっていた。apk 版なら 2.0s）
RUN apk add --no-cache python3 py3-pytest github-cli

# alpine/git は ENTRYPOINT ["git"] を持つ。compose.yaml の 3 サービスは
# いずれも entrypoint を明示的に上書きしてこれを潰している。素の
# docker run では git が起動するので、用途に応じて --entrypoint を渡すこと。

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
    PYTHONPATH= \
    HOME=/tmp

WORKDIR /w
