# pr-traffic-controller — PR干渉ビューア

複数の Pull Request のあいだに生じる **意図しない干渉** を可視化し、
その解消を助けるツール。マージ順の推奨まで出す。

スタックした PR（意図して積んだ依存）は解決対象ではない。順序の
ハード制約としては扱うが、干渉には数えない。

解析したいリポジトリを GitHub Actions から指定すると結果が蓄積され、以後は
定期実行で自動更新される。解析結果は Actions のキャッシュに置いて git には
コミットせず、サイトは Pages へ直接デプロイする。対象リポジトリには
一切書き込まない（読み取りのみ）。

## 何がわかるか

- **干渉の段階** — L0 無干渉 / L1 同一ファイル・別領域 / L2 テキスト衝突 /
  L3 構造衝突（追加・削除・改名が絡む）
- **致命的になりうる干渉** — 同じ関数を双方が変更、依存・設定ファイルを双方が変更
- **衝突の中身** — ぶつかっている箇所の双方のコードを並べて表示する
- **修正が必要な箇所** — レビューでの指摘を、ファイル・行つきで PR ごとにまとめる
- **マージ順の推奨** — 上から順にマージすればよい一列の手順として提示する。
  スタック依存をハード制約、Approve・滞留・規模・衝突コストをソフト要因にする
- **推奨順の検証** — 推奨した順序を実際に git でマージし、何番目のどの PR が
  どのファイルで衝突するかを実証する
- **調整が要る範囲** — 意図しない干渉の連結成分に分解し、話し合いが要る
  範囲と、干渉が無いものを分離する
- **ファイル・関数ごとの関係者** — 同じファイル、同じ関数を誰がどの PR で
  触っているか。混んでいる領域をディレクトリ単位でも見られる
- **スタック PR** — フォークを跨ぐスタック鎖も追跡する

## 使い方

`Actions` → `解析` → `Run workflow`。

| 入力 | 説明 |
|---|---|
| `repo` | `owner/name`。空にすると蓄積済みを全件更新して再デプロイする |
| `lines` | 統合ブランチ（カンマ区切り）。**分岐した統合先が複数あるなら全て挙げる**。互いに独立した解析単位として扱われる |
| `include_forks` | フォーク側に開かれた PR も追跡するか |
| `forget` | 有効にすると、そのリポジトリを蓄積から削除する（解析はしない） |

一度解析したリポジトリは以後 6 時間ごとに自動更新される。対象一覧を
リポジトリ内に持たないので、追加も削除も Actions の実行だけで完結する。
蓄積は Actions のキャッシュにあり、7 日アクセスが無いと退避されるが、
定期実行があるので通常は保たれる。退避された場合は指定し直せばよい。

**初回のみ**: `Settings` → `Pages` → `Source` を **GitHub Actions** にする。

### ローカル

`gh` の認証（`gh auth login`）と **git 2.40 以上**が必要。

```bash
python -m analyzer.analyze --repo OWNER/NAME --lines main --outdir docs/data
python -m analyzer.analyze --refresh --outdir docs/data          # 蓄積を全件更新
python -m analyzer.analyze --forget OWNER/NAME --outdir docs/data # 蓄積から外す

python -m http.server -d docs 8000
```

`--out` を指定すると、蓄積を汚さず結果だけ書き出せる。
単一 HTML にまとめる場合は `python3 tools/build_standalone.py out.html`。

git が古い環境では docker を使う:

```bash
docker run --rm --entrypoint sh -e GH_TOKEN="$(gh auth token)" \
  -v "$PWD":/w -w /w alpine/git:latest -c '
    apk add --quiet python3 github-cli
    git config --global --add safe.directory "*"
    python3 -m analyzer.analyze --repo OWNER/NAME --lines main --outdir /w/docs/data'
```

## 仕組み

衝突の検出は `git merge-tree --write-tree -z` だけで行う（**git ≥ 2.40 必須**）。
古い git 向けのフォールバックは作らない。検出器が 2 つあって食い違うのは
バグの温床なので、バージョンをアサートして落とす。

```
着地tree(T, head) = merge-tree --merge-base=$(merge-base T head)  T  head
ペア(T, treeA, treeB) = merge-tree --merge-base=T  treeA  treeB
```

ペアを `merge-base(A,B)` で判定すると「A と B が抽象的に衝突するか」という
**別の問い**になる。知りたいのは「A が先にマージされたら B は衝突するか」なので、
各 PR を統合ラインへ着地させた tree 同士を比較する。

マージ順はハード制約付きの線形順序付け問題として解く。干渉グラフの連結成分に
分解し、各クラスタを部分集合ビットマスク DP で厳密に解く（18 件まで）。

ただし **衝突するペアはどちらの順でマージしても誰かが必ず解決する**。順序が
決めるのは主に「誰が払うか」であって、マージできる件数が変わるとは限らない。
そこで毎回、順序を変えて実際にマージし、件数が動くかどうかを実測して報告する
（`order_sensitivity`）。推測では書かない。

順序の方針は 4 つ。`max-landing` だけは目的関数が異なり、衝突なくマージできる
件数を最大化する（rebase の総負担はむしろ増えうる）。これは代理モデルでは
作れないので、実際にマージしながら貪欲に構成し、走査順を変えたリスタートで
最良を採る。

実装上の判断と落とし穴は、各モジュールの docstring に理由つきで書いてある。

## 開発

```bash
./run-tests.sh          # git 不要な層（どの環境でも走る）
./run-tests-docker.sh   # 全テスト（git >= 2.40 が要るので docker）
node tools/smoke.mjs    # ビューアの全ビューを実データに対して描画
```

テストは 3 層に分かれ、git のバージョン依存を最下層だけに閉じ込めてある。

- `tests/test_mergetree_parse.py` — 実 git が吐いた `-z` の生バイト列を固定した
  パーサの純関数テスト
- `tests/test_interference.py` — 合成リポジトリで L0〜L3 と警告を再現する
- `tests/test_dag.py` — フォークを跨ぐスタック鎖・重複 head・親PR不在・循環
- `tests/test_index_cache.py` — 解析結果の蓄積が壊れないこと

## 今後の拡張

- Python AST によるクロスファイルのシンボル参照追跡（「A が関数をリネームし、
  B がその呼び出しを追加した」）。誤検知が多く実装量も大きいため見送っている
- 干渉行列のヒートマップ表示（現状は衝突が疎なので一覧の方が読みやすい）
