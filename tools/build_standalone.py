"""docs/ の資材を 1 枚の自己完結 HTML にまとめる。

GitHub Pages では index.html / app.js / data/*.json を別々に配信するが、
プレビューや `file://` で開く場合は fetch が使えないため、JSON と JS を
インライン化した単一ファイルを作れるようにしておく。

単一 HTML には 1 リポジトリ分の解析結果だけを埋め込む。

    python3 tools/build_standalone.py out.html
    python3 tools/build_standalone.py out.html --repo=owner/name
    python3 tools/build_standalone.py out.html --artifact   # 断片だけ出力
"""

import json
import sys
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"

# 置き換え対象。app.js の main() が索引を読む部分を、埋め込みデータの
# 読み込みに差し替える。app.js 側を変更したらここも合わせること
# （一致しなければ黙って壊れないようエラーにする）。
FETCH_SNIPPET = """    const res = await fetch(INDEX_URL, { cache: "no-cache" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    INDEX = await res.json();
    if (!INDEX.analyses || !INDEX.analyses.length) throw new Error("解析結果が空です");
    await loadAnalysis();"""

INLINE_SNIPPET = """    DATA = JSON.parse(document.getElementById("embedded-data").textContent);
    PR = new Map(DATA.pull_requests.map((p) => [p.id, p]));
    INDEX = { analyses: [{ repo: DATA.source.repo, file: "" }] };
    state.repo = DATA.source.repo;"""


def main(argv: list[str]) -> int:
    index_path = DOCS / "data" / "index.json"
    if not index_path.exists():
        print("解析結果がありません。先に analyzer.analyze を実行してください。", file=sys.stderr)
        return 1
    index = json.loads(index_path.read_text())
    if not index.get("analyses"):
        print("索引が空です。", file=sys.stderr)
        return 1

    wanted = next((a.split("=", 1)[1] for a in argv if a.startswith("--repo=")), None)
    if wanted:
        entry = next((a for a in index["analyses"] if a["repo"] == wanted), None)
        if entry is None:
            print(f"索引に {wanted} がありません。", file=sys.stderr)
            return 1
    else:
        entry = index["analyses"][0]

    data = json.loads((DOCS / "data" / entry["file"]).read_text())

    html = (DOCS / "index.html").read_text()
    css = (DOCS / "styles.css").read_text()
    js = (DOCS / "app.js").read_text()

    if FETCH_SNIPPET not in js:
        print(
            "app.js の読み込み部分が想定と違います。"
            "tools/build_standalone.py の FETCH_SNIPPET を更新してください。",
            file=sys.stderr,
        )
        return 1
    js = js.replace(FETCH_SNIPPET, INLINE_SNIPPET)

    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = html.replace('<link rel="stylesheet" href="styles.css">', f"<style>\n{css}\n</style>")
    html = html.replace(
        '<script type="module" src="app.js"></script>',
        f'<script type="application/json" id="embedded-data">{payload}</script>\n'
        f'<script type="module">\n{js}\n</script>',
    )
    # 単一ファイルでは対象を切り替えられないのでセレクタを隠す
    html = html.replace(
        '<label class="small muted" for="repo-select">対象',
        '<label class="small muted" for="repo-select" hidden>対象',
    )

    if "--artifact" in argv:
        # Artifact として publish する場合、<!doctype>/<html>/<head>/<body> は
        # publish 時に付与されるので、中身だけを出す。
        body = html.split("<body>", 1)[1].rsplit("</body>", 1)[0]
        head = html.split("<head>", 1)[1].split("</head>", 1)[0]
        style = head[head.index("<style>") : head.index("</style>") + len("</style>")]
        html = "<title>PR干渉ビューア</title>\n" + style + "\n" + body

    out = Path(next((a for a in argv if not a.startswith("--")), DOCS.parent / "standalone.html"))
    out.write_text(html)
    print(f"{out} ({len(html)//1024} KB) — 埋め込み: {entry['repo']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
