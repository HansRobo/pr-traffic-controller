# pr-traffic-controller — PR干渉ビューア

滞留した Pull Request 群の**相互干渉を可視化し、マージ順を推奨する**ツール。

マージが長期間止まって PR が積み上がると、どれとどれが干渉するのか、
何から流せばよいのかが誰にも分からなくなる。このツールはその状態を
可視化し、開発者間の「交通整理」を合意形成できるようにする。

- 解析したいリポジトリを Actions から指定すると、結果が蓄積されていく
- 一度解析したリポジトリは以後、定期実行で自動更新される
- **解析結果は Actions のキャッシュに置き、git にはコミットしない。**
  サイトはキャッシュの内容を含めて Pages へ直接デプロイされるので、
  リポジトリの履歴が解析結果の更新で埋まらない
- `docs/` の静的サイト（ビルド不要）がそれを描画する
- 対象リポジトリ自体には一切書き込まない（読み取りのみ）

## 何がわかるか

| | |
|---|---|
| **干渉の解像度** | L0 無干渉 / L1 同一ファイル・別領域 / L2 テキスト衝突 / L3 構造衝突（add-add・modify-delete・rename-delete） |
| **致命的な干渉** | 構造衝突に加え、「同じ関数を双方が変更」「依存・設定ファイルを双方が変更」を警告 |
| **マージ順の推奨** | スタック依存をハード制約、Approve・滞留・規模・衝突コストをソフト要因として順序付け |
| **順序の検証** | 推奨順を**実際に git でマージして**「何番目のどのPRがどのファイルで衝突するか」を実証 |
| **並行に流せるもの** | 干渉グラフの連結成分に分解し、「順序を議論する必要がない N 件」を明示 |
| **スタックPR** | 外部フォークを跨ぐスタック鎖を含めて追跡 |

## 使い方

### GitHub Actions（推奨）

`Actions` → `解析` → `Run workflow` で、解析したいリポジトリと統合ブランチを
指定して実行する。

| 入力 | 説明 |
|---|---|
| `repo` | `owner/name` |
| `lines` | 統合ブランチ。**分岐した統合先が複数あるなら全て挙げる**（`main,develop` など）。互いに独立した解析単位として扱われる |
| `include_forks` | フォーク側に開かれた PR も追跡するか |
| `publish` | 外すと結果を公開せず artifact だけに出す |

| `forget` | 有効にすると、そのリポジトリを蓄積から削除する（解析はしない） |

**結果は蓄積される。** 一度解析したリポジトリは以後、6 時間ごとの定期実行で
まとめて更新される。対象一覧はリポジトリ内に持たないので、追加も削除も
Actions の実行だけで完結する。

蓄積は Actions のキャッシュに置かれる。キャッシュは 7 日アクセスが無いと
退避されるが、6 時間ごとの定期実行があるので通常は保たれる。万一退避された
場合は、対象を再度 Run workflow で指定し直せばよい。

**初回のみ**: `Settings` → `Pages` → `Source` を **GitHub Actions** にする
（ブランチ公開ではない）。

### ローカル

```bash
# 解析して蓄積に追加する
python -m analyzer.analyze --repo OWNER/NAME --lines main --outdir docs/data

# 蓄積にある全リポジトリを再解析する
python -m analyzer.analyze --refresh --outdir docs/data

# 蓄積から外す
python -m analyzer.analyze --forget OWNER/NAME --outdir docs/data

python -m http.server -d docs 8000   # http://localhost:8000
```

索引を汚さず結果だけ見たい場合は `--out` を指定する:

```bash
python -m analyzer.analyze --repo OWNER/NAME --lines main --out out.json
```

`gh` CLI の認証（`gh auth login`）と **git 2.40 以上**が必要。

ローカルの git が古い場合は docker を使う:

```bash
docker run --rm --entrypoint sh -e GH_TOKEN="$(gh auth token)" \
  -v "$PWD":/w -w /w alpine/git:latest -c '
    apk add --quiet python3 github-cli
    git config --global --add safe.directory "*"
    python3 -m analyzer.analyze --repo OWNER/NAME --lines main --outdir /w/docs/data'
```

単一 HTML にまとめたい場合（`file://` で開ける・共有しやすい）:

```bash
python3 tools/build_standalone.py standalone.html
```

## 仕組み

### 衝突の検出

唯一の検出器は `git merge-tree --write-tree -z`（**git ≥ 2.40 必須**）。

```
着地tree(T, head) = merge-tree --merge-base=$(merge-base T head)  T  head
ペア(T, treeA, treeB) = merge-tree --merge-base=T  treeA  treeB
```

ペアを `merge-base(A,B)` で判定すると「A と B が抽象的に衝突するか」という
**別の問い**になる。知りたいのは「A が先にマージされたら B は衝突するか」なので、
各 PR を統合ラインへ着地させた tree 同士を比較する。

古い git 向けのフォールバック検出器は**作らない**。検出器が 2 つあって
食い違うのはバグの温床なので、バージョンをアサートして落とす。

### 実装上の落とし穴（すべて実測で踏んだもの）

- **終了コードは 0/1/その他の 3 分岐**。「非ゼロ＝衝突」にすると引数エラー(129)や
  壊れたオブジェクトが偽の衝突として静かに混入する
- **`add/add` は型フィールドでは判別できない**。型は `CONFLICT (contents)` で、
  `add/add` は人間向けの本文にしか現れない。**ステージ番号の集合**で判定する
  （`{1,2,3}`=内容衝突、`{2,3}`=add/add、`{1,2}`/`{1,3}`=片側削除）
- **`git diff --name-only` は既定で改名を検出する**。そのままだと
  「片方が削除・片方が改名」のペアで変更ファイル集合が重ならず、L0 と誤判定して
  merge-tree を呼ばずに終わる。`--no-renames` が必須
- **統合ラインは推移的に継承する**。他 PR のブランチにスタックした PR は
  base 欄を見てもラインに属さず、順序推奨から丸ごと消える
- **`-W`（関数境界まで hunk を拡張）は同一関数の判定に使えない**。実データでは
  交差範囲の中央値が 162 行・最大 848 行に膨らんだ。代わりに
  `git diff -U0` の hunk ヘッダが持つ**関数名**を突き合わせる
- **累積ツリーには `--merge-base` を明示ピン留めする**。合成コミットに対して
  git がマージベースを推論すると静かに誤った結果を返す
- **完全 clone を使う**。`--filter=blob:none` は merge-tree が blob を
  オンデマンド取得して桁違いに遅くなる

### マージ順の推奨

ハード制約付きの線形順序付け問題（LOP）として解く。干渉グラフの連結成分に
分解し、各クラスタを部分集合ビットマスク DP で**厳密に**解く（18 件まで）。

正直に言うと、**衝突するペアはどちらの順で流しても誰かが必ず解決する。
順序が決めるのは「誰が払うか」と「支払い総額」だけ**である。
順序を変えれば多く流せるのかどうかは、ランダムな順序を実際にマージして
確かめる（`order_sensitivity`）。件数が変わらないラインでは、ツールは
「順序が決めるのは誰が rebase するかだけ」と明示する。推測ではなく実測で報告する。

プリセット:

| | 目的関数 |
|---|---|
| `balanced` | rebase の総負担を最小化（既定） |
| `approve-first` | Approve 済みを優先 |
| `least-conflict` | 待ち時間を無視して衝突コストのみ最小化 |
| `max-landing` | **clean に landing できる件数を最大化**（他とは目的関数が違う。件数は増えるが rebase 総負担は増えうる） |

`max-landing` だけは代理モデルでは作れないので、実際にマージしながら
貪欲に構成し、走査順を変えたリスタートで最良を採る。

## 開発

```bash
./run-tests.sh          # 層1・層3（git 不要。どの環境でも走る）
./run-tests-docker.sh   # 層2 も含む全テスト（git >= 2.40 が要るので docker）
node tools/smoke.mjs    # ビューアの全ビューを実データに対して描画
```

テストは 3 層に分かれ、git のバージョン依存を最下層だけに閉じ込めてある。

- **層1** `tests/test_mergetree_parse.py` — 実 git が吐いた `-z` の生バイト列を
  `tests/fixtures/mergetree/*.bin` に固定してパースする純関数テスト
- **層2** `tests/test_interference.py` — 合成リポジトリ（`tests/repofixture.py`）で
  L0〜L3 とセマンティック警告を実際に再現する
- **層3** `tests/test_dag.py` — 実運用で遭遇する構造（フォークを跨ぐ
  スタック鎖・重複 head・親PR不在・循環）を PR オブジェクトのモックで再現する

## 今後の拡張

- Python AST によるクロスファイルのシンボル参照追跡（「A が関数をリネームし、
  B がその呼び出しを追加した」）。誤検知が多く実装量も大きいため v1 では見送った
- 干渉行列のヒートマップ表示（現状は衝突が疎なので一覧の方が読みやすい）

## 公開範囲について

解析結果には対象リポジトリの PR タイトル・ブランチ名・著者名が含まれ、
それがそのまま Pages に載る。**このリポジトリを公開する場合、
解析してよいのは公開リポジトリだけ。** private リポジトリを解析するなら、
このリポジトリ自体も private にすること（Pages も非公開になる）。
