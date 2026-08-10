/* PR干渉ビューア — 事前生成された解析JSONを描画する。
 *
 * ビルドツールチェーンを持たない素の ES module。理由:
 *  - マージが再開すれば役目を終える性質のツールで、半年後に
 *    `npm install` が通らない設定を残す価値がない
 *  - 交通整理は中立性が命で、「この順序は本当に正しいのか」と
 *    中身を見に来る人がいる。バンドル後のコードは読めない
 *
 * 状態は URL ハッシュに載せる。Slack にリンクを貼って議論するので、
 * 「この画面」を共有できることは機能要件。
 */

const INDEX_URL = "data/index.json";

const state = {
  view: "board",
  repo: null,
  line: null,
  author: "",
  preset: "balanced",
  hideDraft: false,
  cluster: null,   // クラスタ詳細で見ているクラスタ
  file: null,      // ファイルビューで開いているファイル
  fileDir: null,   // ファイルビューで絞り込んでいるディレクトリ
  fileQuery: "",
  filesSharedOnly: true,
  filesConflictOnly: false,
  pr: null,        // サイドパネルで開いている PR
  minLevel: 2,     // グラフに出す干渉レベルの下限
  showStack: true, // 意図したスタック依存を文脈として描くか
};

let INDEX = null;
let DATA = null;
let PR = new Map();

// --- ユーティリティ ---------------------------------------------------

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v === null || v === undefined) continue;
    if (k === "class") n.className = v;
    else if (k === "html") n.innerHTML = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    n.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return n;
};
const shortId = (id) => "#" + String(id).split("#").pop();
const pct = (a, b) => (b ? Math.round((a / b) * 100) : 0);

function relativeTime(iso) {
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 90) return "たった今";
  if (diff < 3600) return `${Math.round(diff / 60)}分前`;
  if (diff < 86400) return `${Math.round(diff / 3600)}時間前`;
  return `${Math.round(diff / 86400)}日前`;
}

// --- URL ハッシュ同期 -------------------------------------------------

function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  for (const k of ["view", "repo", "line", "author", "preset", "cluster", "pr", "file", "fileDir"]) {
    if (p.get(k)) state[k] = p.get(k);
  }
  state.hideDraft = p.get("hideDraft") === "1";
  if (p.get("minLevel")) state.minLevel = Number(p.get("minLevel"));
  if (state.file) {
    // 共有されたリンクで必ずその行が出るよう、絞り込みを合わせる
    state.fileQuery = state.file;
    state.filesSharedOnly = false;
  }
}

function writeHash() {
  const p = new URLSearchParams();
  p.set("view", state.view);
  if (state.repo) p.set("repo", state.repo);
  if (state.line) p.set("line", state.line);
  if (state.author) p.set("author", state.author);
  if (state.preset !== "balanced") p.set("preset", state.preset);
  if (state.hideDraft) p.set("hideDraft", "1");
  if (state.cluster) p.set("cluster", state.cluster);
  if (state.file) p.set("file", state.file);
  if (state.fileDir) p.set("fileDir", state.fileDir);
  if (state.pr) p.set("pr", state.pr);
  if (state.minLevel !== 2) p.set("minLevel", String(state.minLevel));
  history.replaceState(null, "", "#" + p.toString());
}

// --- 部品 --------------------------------------------------------------

// --- GitHub 風のアイコン ---------------------------------------------
// Pages 配信なので外部画像（アバター）を読める。読めなかった場合に
// 崩れないよう、代替としてイニシャルの丸を出す。

/** PR を表すアイコン。Draft かどうかで見分けられるようにする。 */
function prIcon(pr) {
  const draft = pr && pr.is_draft;
  const path = draft
    // git-pull-request-draft
    ? "M3.25 1a2.25 2.25 0 0 1 .75 4.372v5.256a2.251 2.251 0 1 1-1.5 0V5.372A2.25 2.25 0 0 1 3.25 1Zm9.5 14a2.25 2.25 0 1 1 0-4.5 2.25 2.25 0 0 1 0 4.5ZM12 2.5a.75.75 0 1 1 1.5 0 .75.75 0 0 1-1.5 0Zm.75 3.5a.75.75 0 0 1 .75.75v1.5a.75.75 0 0 1-1.5 0v-1.5A.75.75 0 0 1 12.75 6Z"
    // git-pull-request
    : "M1.5 3.25a2.25 2.25 0 1 1 3 2.122v5.256a2.251 2.251 0 1 1-1.5 0V5.372A2.25 2.25 0 0 1 1.5 3.25Zm5.677-.177L9.573.677A.25.25 0 0 1 10 .854V2.5h1A2.5 2.5 0 0 1 13.5 5v5.628a2.251 2.251 0 1 1-1.5 0V5a1 1 0 0 0-1-1h-1v1.646a.25.25 0 0 1-.427.177L7.177 3.427a.25.25 0 0 1 0-.354ZM3.75 2.5a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Zm0 9.5a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Zm8.25.75a.75.75 0 1 0 1.5 0 .75.75 0 0 0-1.5 0Z";
  const n = svg("svg", {
    class: "icon pr-icon" + (draft ? " draft" : ""),
    viewBox: "0 0 16 16", width: 14, height: 14, "aria-hidden": "true", focusable: "false",
  }, svg("path", { d: path, fill: "currentColor" }));
  return n;
}

/** PR 番号（アイコン付き）。 */
function prRef(id) {
  const pr = PR.get(id);
  return el("span", { class: "pr-ref" }, prIcon(pr), shortId(id));
}

/** 著者。アバター画像 + ログイン名。 */
function authorChip(login, avatarUrl, { compact = false } = {}) {
  const src = avatarUrl || (login && login !== "(unknown)"
    ? `https://github.com/${encodeURIComponent(login)}.png?size=48` : "");
  const initial = (login || "?").replace(/^[^A-Za-z0-9]*/, "").charAt(0).toUpperCase() || "?";
  const fallback = el("span", { class: "avatar avatar-fallback", "aria-hidden": "true" }, initial);
  const chip = el("span", { class: "author-chip", title: login });
  if (src) {
    const img = el("img", {
      class: "avatar", src, alt: "", loading: "lazy", width: 18, height: 18,
      // 画像が出せない環境ではイニシャルに差し替える
      onerror: () => { img.replaceWith(fallback); },
    });
    chip.append(img);
  } else {
    chip.append(fallback);
  }
  if (!compact) chip.append(el("span", { class: "author-name" }, login));
  return chip;
}



/** 「Draft を隠す」が効いているか。PR を一覧・集計する箇所すべてで使う。 */
function isHidden(id) {
  return !!state.hideDraft && !!(PR.get(id) || {}).is_draft;
}

/** 表示対象の PR だけに絞る。 */
function visiblePrs(ids) {
  return [...ids].filter((id) => !isHidden(id));
}

function prBadges(pr) {
  const out = [];
  if (pr.review_decision === "APPROVED") out.push(el("span", { class: "badge approved" }, "✓ Approved"));
  if (pr.review_decision === "CHANGES_REQUESTED") out.push(el("span", { class: "badge warn" }, "要修正"));
  if (pr.is_draft) out.push(el("span", { class: "badge draft" }, "Draft"));
  if (pr.base_conflict) out.push(el("span", { class: "badge rebase" }, "⚠ 要rebase"));
  if (pr.kind === "external_pr") out.push(el("span", { class: "badge fork" }, "外部fork"));
  if (pr.duplicate_of) {
    out.push(el("span", {
      class: "badge",
      title: `${pr.duplicate_of.join(", ")} と同じ head コミット。`
        + "マージ先が違えば、レビュー用と着地用を分けている場合がある",
    }, "⧉ 同一コミット"));
  }
  if (pr.blocks && pr.blocks.length) {
    out.push(el("span", { class: "badge blocks", title: pr.blocks.join(", ") }, `${pr.blocks.length}件をブロック`));
  }
  if (pr.stack && pr.stack.depth > 0) {
    out.push(el("span", { class: "badge", title: pr.stack.ancestors.join(" → ") }, `スタック深さ${pr.stack.depth}`));
  }
  return out;
}

function prCard(id, { step = null, note = null, reasons = null } = {}) {
  const pr = PR.get(id);
  if (!pr) return el("div", { class: "pr" }, id);
  const cls = ["pr", pr.is_draft ? "draft" : "", pr.base_conflict ? "base-conflict" : ""].join(" ");

  // 理由が複数ある場合は箇条書きにする。1 行に連結すると読めなくなる。
  const list = reasons && reasons.filter(Boolean);
  const body = el("span", { class: "pr-title" },
    pr.title,
    note ? el("span", { class: "small muted" }, " — " + note) : null,
    list && list.length
      ? el("ul", { class: "tight small muted" }, ...list.map((r) => el("li", {}, r)))
      : null,
  );

  // カード全体をクリック対象にする。番号だけを的にすると狙いにくい。
  // 内側のボタン（他のPRへのリンク等）は stopPropagation でここに来ない。
  // バッジはひとかたまりにして折り返す。個別に並べると flex 行の中で
  // タイトルと幅を奪い合い、タイトルが潰れる。
  return el(
    "div",
    {
      class: cls + " clickable",
      role: "button",
      tabindex: "0",
      "aria-label": `${shortId(id)} ${pr.title} の詳細を開く`,
      onclick: () => openPanel(id),
      onkeydown: (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openPanel(id); }
      },
    },
    step !== null ? el("span", { class: "step-no" }, step) : null,
    el("span", { class: "num" }, prRef(id)),
    body,
    el("span", { class: "pr-badges" }, ...prBadges(pr)),
    el("span", { class: "author" }, authorChip(pr.author, pr.author_avatar_url)),
  );
}

/** PR 番号のボタン。ページ遷移させず、サイドパネルで開く。 */
function prOpenButton(id, label = null) {
  const pr = PR.get(id);
  return el("button", {
    class: "pr-open",
    title: (pr ? pr.title + " — " : "") + "詳細をパネルで開く",
    onclick: (e) => { e.stopPropagation(); openPanel(id); },
  }, label ?? [prIcon(pr), shortId(id)]);
}

function levelChip(level) {
  return el("span", { class: `lv lv-${level}`, title: LEVEL_DESC[level] }, "L" + level);
}

/** コメント・文書だけの衝突であることを示す印。
 *  等級は下げない（git はマージできない）が、解決の負担がまるで違う。 */
function commentOnlyBadge() {
  return el("span", {
    class: "badge comment-only",
    title: "衝突しているのはコメント・文書だけ。git はマージできないが、"
      + "両方残すかどちらかを選べば済むことがほとんど",
  }, "コメントのみ");
}

/** 解析側のプリセット識別子 -> 画面に出す名前。 */
const PRESET_LABEL = {
  "balanced": "バランス重視",
  "approve-first": "Approve優先",
  "least-conflict": "衝突を最小に",
  "max-landing": "マージ数を最大に",
};

const LEVEL_DESC = {
  0: "無干渉（変更ファイルが重ならない）",
  1: "同一ファイル・別領域（テキスト上はマージ可能）",
  2: "テキスト衝突（git が自動マージできない）",
  3: "構造衝突（追加・削除・改名が絡む）",
};

// --- ビュー: 推奨マージ順 ---------------------------------------------

function viewBoard() {
  const line = state.line;
  const o = DATA.orders[line];
  const iv = DATA.interference[line];
  const root = el("div");

  // 行動可能なヘッドライン
  const actions = DATA.actions.filter(
    (a) => a.line === line || a.kind === "unlisted_integration_line");
  if (actions.length) {
    const box = el("div", { class: "actions" });
    for (const a of actions) {
      if (a.kind === "rebase_to_unblock_analysis") {
        box.append(
          el(
            "div",
            { class: "action" },
            el("div", { class: "big" }, a.pr_count),
            el(
              "div",
              {},
              el("p", {}, el("strong", {}, "件を rebase すると解析が進む")),
              el("div", { class: "sub" },
                `これらはベースブランチと衝突していて着地させられないため、`
                + `${a.unlocks_pairs} ペア（全体の ${pct(a.unlocks_pairs, iv.pairs_evaluated)}%）が判定不能になっている。`),
              el("div", { class: "sub mono" }, a.prs.map(shortId).join(" ")),
              // ここはリポジトリの状態であって「表示中の PR」ではない。
              // 絞り込み中に件数が食い違って見えるので、その旨を書く。
              state.hideDraft && a.prs.some((id) => isHidden(id))
                ? el("div", { class: "sub" },
                    `※ この件数は Draft を含みます（表示中は `
                    + `${visiblePrs(a.prs).length} 件）`)
                : null,
            ),
          ),
        );
      } else if (a.kind === "unlisted_integration_line") {
        box.append(
          el("div", { class: "action" },
            el("div", { class: "big" }, a.pr_count),
            el("div", {},
              el("p", {}, el("strong", {}, "件の PR が解析対象外です")),
              el("div", { class: "sub" },
                "これらは ", el("code", {}, a.branch),
                " に向いていますが、解析対象の統合ラインに含まれていません。"
                + "統合先として扱うなら、Actions の lines に追加して実行し直してください。"),
              el("div", { class: "sub" },
                "※ 除外された PR は衝突の集計にも入っていないので、"
                + "この画面の「衝突なし」はそれらを含みません。"))),
        );
      } else if (a.kind === "order_does_not_change_throughput") {
        box.append(
          el("div", { class: "action info" },
            el("div", { class: "big" }, a.merged),
            el("div", {},
              el("p", {}, el("strong", {}, "件 — どの順序でマージしても変わらない")),
              el("div", { class: "sub" },
                `${a.trials} 通りの順序を実際にマージして確認した。`
                + `順序が決めるのは「誰が rebase するか」であって、何件マージできるかではない。`),
            ),
          ),
        );
      }
    }
    root.append(box);
  }

  // 順序感度
  const sens = o.order_sensitivity || {};
  if (sens.trials && !sens.order_invariant) {
    root.append(
      el("div", { class: "action info" },
        el("div", { class: "big" }, `${sens.worst_observed}–${sens.best_observed}`),
        el("div", {},
          el("p", {}, el("strong", {}, "件 — 順序によってマージできる件数が変わる")),
          el("div", { class: "sub" },
            `${sens.trials} 通り試行。「マージ数を最大化」の方針がこの上限を狙う。`),
        ),
      ),
    );
  }

  // プリセット選択。キーは解析側の識別子なので、表示は日本語に置き換える。
  const presetRow = el("div", { class: "filters" }, el("span", { class: "small muted" }, "順序の方針:"));
  for (const [name, p] of Object.entries(o.presets)) {
    const merged = p.simulation ? p.simulation.merged : "?";
    presetRow.append(
      el("button", {
        "aria-pressed": state.preset === name,
        onclick: () => { state.preset = name; render(); },
        title: p.objective_note
          || "rebase の総負担（誰がどれだけ書き直すか）を最小化する。"
             + (p.optimal ? "この目的関数に対しては厳密最適。" : ""),
      }, `${PRESET_LABEL[name] || name}（衝突なし ${merged}件）`),
    );
  }
  root.append(presetRow);

  const preset = o.presets[state.preset] || o.presets.balanced;
  // 「厳密最適」は目的関数に対しての話であって landing 件数の最大ではない。
  // 明示しないと「最適なのに landing が少ない」と読めてしまう。
  root.append(el("p", { class: "hint" },
    preset.objective_note
      ? "ℹ️ " + preset.objective_note
      : "ℹ️ rebase の総負担を最小化する順序。"
        + (preset.optimal
            ? "クラスタごとに厳密最適だが、これは「rebase の負担が最小」という意味であって、"
              + "「衝突なくマージできる件数が最大」ではない"
              + "（件数を優先するなら「マージ数を最大に」）。"
            : "")));

  // --- マージ手順 ------------------------------------------------
  // 「結局どの順でマージすればいいのか」に一列で答える。独立PRとクラスタを
  // 別々の箱に分けていたときは、全体の順番が読み取れなかった。
  const clusterOf = new Map();
  for (const c of o.clusters) for (const m of c.members) clusterOf.set(m, c.id);
  const independent = new Set(o.independent);
  // 要rebase の PR は着地tree を作れず、干渉を計算できていない。
  // 「独立」と同じ扱いにすると、判定できていないものを
  // 「誰とも干渉しない」と誤って伝えることになる。
  const undetermined = new Set(o.undetermined || []);

  const steps = preset.simulation
    ? preset.simulation.steps
    : preset.order.map((pr, i) => ({ index: i + 1, pr }));
  const stepByPr = new Map(steps.map((s) => [s.pr, s]));

  const shown = steps.filter((s) => !isHidden(s.pr));
  const cleanCount = shown.filter((s) => s.result === "clean").length;
  const rebaseCount = shown.filter((s) => s.result === "skipped").length;
  const conflictCount = shown.filter((s) => s.result === "conflict").length;
  const hiddenCount = steps.length - shown.length;
  root.append(el("div", { class: "stat-row" },
    el("div", {}, el("strong", {}, shown.length), "対象PR"),
    el("div", {}, el("strong", {}, cleanCount), "そのままマージできる"),
    el("div", {}, el("strong", {}, conflictCount), "解決が必要"),
    el("div", {}, el("strong", {}, rebaseCount), "先にrebaseが必要"),
    el("div", { title: "他のPRとの意図しない干渉が無い。スタックの親を待つものは含む" },
      el("strong", {}, visiblePrs(independent).length), "干渉なし"),
    visiblePrs(undetermined).length
      ? el("div", { title: "ベースと衝突していて着地させられないため、他PRとの干渉を判定できていない" },
          el("strong", {}, visiblePrs(undetermined).length), "干渉は判定不能")
      : null,
    hiddenCount
      ? el("div", { class: "muted", title: "「Draft を隠す」で除外されている" },
          el("strong", {}, hiddenCount), "Draft を非表示")
      : null,
  ));

  const nextUp = preset.order.find(
    (id) => !isHidden(id) && (stepByPr.get(id) || {}).result === "clean");
  const visible = visiblePrs(preset.order);

  const seqBody = el("div", { class: "seq" });
  visible.forEach((id) => {
    const s = stepByPr.get(id) || {};
    const pr = PR.get(id);
    const cid = clusterOf.get(id);
    const pos = preset.order.indexOf(id) + 1;

    let mark, cls, note;
    if (s.result === "skipped") {
      mark = "⏸"; cls = "rebase";
      note = "ベースと衝突しているので、まず rebase する";
    } else if (s.result === "conflict") {
      const files = (s.conflict_files || []).map((f) => f.path.split("/").pop());
      const commentOnly = (s.conflict_files || []).length
        && (s.conflict_files || []).every((f) => f.comment_only);
      mark = commentOnly ? "△" : "⚠";
      cls = commentOnly ? "conflict-light" : "conflict";
      note = (commentOnly ? "コメント・文書だけの衝突: " : "この時点で衝突する: ")
        + `${files.slice(0, 3).join(", ")}${files.length > 3 ? " ほか" : ""}`;
    } else {
      mark = "✓"; cls = "clean";
      note = "そのままマージできる";
    }

    const deps = (pr && pr.stack ? pr.stack.ancestors : []).filter((a) => PR.has(a));
    seqBody.append(el("div", { class: "seq-row " + cls },
      el("span", { class: "seq-no" }, pos),
      el("span", { class: "seq-mark", title: note }, mark),
      el("div", { class: "seq-main" },
        prCard(id, { note }),
        deps.length
          ? el("div", { class: "small muted seq-dep" },
              "先に必要: ", ...deps.map((a, i) => el("span", {}, i ? " → " : "", prOpenButton(a))))
          : null),
      el("span", { class: "seq-tag" },
        undetermined.has(id)
          ? el("span", {
              class: "badge warn",
              title: "ベースと衝突しているため干渉を判定できていない。rebase して初めて分かる",
            }, "判定不能")
          : independent.has(id)
          ? ((PR.get(id) || {}).stack || {}).depth > 0
              ? el("span", {
                  class: "badge",
                  title: "意図しない干渉は無い。スタックの親を待つ必要があるだけ",
                }, "スタック順のみ")
              : el("span", { class: "badge", title: "他のどのPRとも干渉しないので、いつマージしてもよい" }, "独立")
          : cid
            ? el("button", {
                class: "badge cluster-link",
                title: "このクラスタの詳細を見る",
                onclick: (e) => { e.stopPropagation(); state.view = "cluster"; state.cluster = cid; render(); },
              }, cid)
            : null),
    ));
  });

  const copyBtn = el("button", {
    onclick: async (e) => {
      const lines = preset.order.map((id, i) => {
        const s = stepByPr.get(id) || {};
        const pr = PR.get(id);
        const st = s.result === "skipped" ? "要rebase"
          : s.result === "conflict" ? "要衝突解決" : "そのまま可";
        return `${i + 1}. [ ] ${pr ? pr.url : id} — ${pr ? pr.title : ""}（${st}）`;
      });
      const text = `マージ順（${state.line} / ${PRESET_LABEL[state.preset] || state.preset}）\n\n`
        + lines.join("\n");
      const btn = e.currentTarget;
      try {
        await navigator.clipboard.writeText(text);
        btn.textContent = "コピーしました";
      } catch {
        btn.textContent = "コピーできませんでした";
      }
      setTimeout(() => { btn.textContent = "手順をコピー"; }, 2000);
    },
  }, "手順をコピー");

  root.append(el("div", { class: "panel" },
    el("h3", {},
      "マージ手順",
      el("span", { class: "muted" }, `— ${PRESET_LABEL[state.preset] || state.preset}。上から順に`),
      el("span", { class: "head-spacer" }),
      copyBtn),
    el("div", { class: "panel-body" },
      nextUp
        ? el("p", { class: "next-up" },
            "次にマージするのは ", prOpenButton(nextUp),
            el("span", { class: "muted" }, `　${PR.get(nextUp) ? PR.get(nextUp).title : ""}`))
        : el("p", { class: "hint" }, "そのままマージできる PR がありません。まず rebase が必要です。"),
      seqBody)));

  // 順序が問題になる範囲への入口
  if (o.clusters.length) {
    const cl = el("div", { class: "cluster-cards" });
    for (const c of o.clusters) {
      const shownMembers = visiblePrs(c.members);
      if (!shownMembers.length) continue;
      const authors = authorsOf(shownMembers);
      cl.append(el("button", {
        class: "cluster-card",
        onclick: () => { state.view = "cluster"; state.cluster = c.id; render(); },
      },
        el("strong", {}, c.id),
        el("span", { class: "small muted" }, `${shownMembers.length}件 / 内部衝突 ${c.internal_pairs}ペア`),
        el("span", { class: "file-authors" },
          ...authors.slice(0, 5).map(([a, av]) => authorChip(a, av, { compact: true })))));
    }
    root.append(el("div", { class: "panel" },
      el("h3", {}, "調整が要る ", el("strong", {}, String(o.clusters.length)), " クラスタ",
        el("span", { class: "muted" }, "— 意図しない干渉で結ばれた範囲。ここだけ話し合いが要る")),
      el("div", { class: "panel-body" }, cl)));
  }

  // 逐次シミュレーションの証拠
  if (preset.simulation) {
    const sim = preset.simulation;
    const conflicts = sim.steps.filter((s) => s.result === "conflict");
    const body = el("div", { class: "panel-body" });
    body.append(
      el("p", { class: "hint" },
        `この順序を実際に git でマージした結果: ${sim.merged} 件が clean、`
        + `${conflicts.length} 件が衝突、`
        + `${sim.steps.filter((s) => s.result === "skipped").length} 件は要rebaseでスキップ。`),
    );
    if (conflicts.length) {
      const t = el("table");
      t.append(el("thead", {}, el("tr", {},
        el("th", { class: "num" }, "#"), el("th", {}, "PR"), el("th", {}, "衝突ファイル"))));
      const tb = el("tbody");
      for (const s of conflicts) {
        tb.append(el("tr", {},
          el("td", { class: "num" }, s.index),
          el("td", {}, prOpenButton(s.pr),
            " ", el("span", { class: "small muted" }, PR.get(s.pr)?.title || "")),
          el("td", { class: "small" },
            (s.conflict_files || []).map((f) => el("div", {}, el("code", {}, f.path))))));
      }
      t.append(tb);
      body.append(el("div", { class: "table-scroll" }, t));
    }
    root.append(el("div", { class: "panel" },
      el("h3", {}, "逐次マージによる検証",
        el("span", { class: "muted" }, "— 推測ではなく実際に git でマージした結果")),
      body));
  }

  return root;
}


// --- ビュー: クラスタ詳細 ---------------------------------------------

function viewCluster() {
  const o = DATA.orders[state.line];
  const iv = DATA.interference[state.line];
  const c = (o.clusters || []).find((x) => x.id === state.cluster);
  const root = el("div");

  root.append(el("p", { class: "crumb" },
    el("button", { onclick: () => { state.view = "board"; state.cluster = null; render(); } }, "← 推奨マージ順"),
    c ? `　/　クラスタ ${c.id}` : ""));

  if (!c) {
    root.append(el("div", { class: "empty" }, "クラスタが見つかりません。"));
    return root;
  }

  const preset = o.presets[state.preset] || o.presets.balanced;
  const rank = new Map(preset.order.map((id, i) => [id, i]));
  const members = visiblePrs(c.members).sort((a, b) => (rank.get(a) ?? 1e9) - (rank.get(b) ?? 1e9));
  const memberSet = new Set(members);
  const pairs = iv.pairs.filter(
    (x) => memberSet.has(x.a) && memberSet.has(x.b) && x.level !== undefined && x.level >= 1,
  );  // memberSet は表示対象だけなので、Draft を含むペアはここで落ちる
  const authors = [...new Set(members.map((m) => PR.get(m)?.author).filter(Boolean))];
  const blocked = members.filter((m) => PR.get(m)?.base_conflict);

  root.append(el("div", { class: "stat-row" },
    el("div", {}, el("strong", {}, members.length), "PR"),
    el("div", {}, el("strong", {}, pairs.filter((x) => x.level >= 2).length), "衝突ペア"),
    el("div", {}, el("strong", {}, authors.length), "人の作業"),
    el("div", {}, el("strong", {}, blocked.length), "要rebase"),
  ));

  root.append(el("p", { class: "hint" },
    "意図しない干渉で結ばれた範囲です。ここだけ調整が要ります。"
    + "他のクラスタの PR とは干渉しないので、どの順番でマージしても構いません。"
    + "（スタックの親子は作者が意図した依存なので、干渉には数えていません）"
  ));
  if (authors.length) {
    const who = el("div", { class: "filters" }, el("span", { class: "small muted" }, "関係者:"));
    for (const a of authors) {
      const some = members.map((m) => PR.get(m)).find((x) => x && x.author === a);
      who.append(authorChip(a, some && some.author_avatar_url));
    }
    root.append(who);
  }

  // グラフ
  root.append(el("div", { class: "panel" },
    el("h3", {}, "干渉グラフ",
      el("span", { class: "muted" }, "— 上側の弧が意図しない干渉。ノードをクリックすると詳細が開きます")),
    el("div", { class: "panel-body" },
      graphFilters(),
      interferenceGraph(members, { height: 300 }))));

  // 推奨順
  const stepByPr = new Map();
  if (preset.simulation) for (const s of preset.simulation.steps) stepByPr.set(s.pr, s);
  const list = el("div", {});
  members.forEach((id, i) => {
    const s = stepByPr.get(id);
    let note = null;
    if (s && s.result === "conflict") {
      note = `逐次マージで衝突: ${(s.conflict_files || []).map((f) => f.path.split("/").pop()).slice(0, 3).join(", ")}`;
    } else if (s && s.result === "skipped") note = "先に rebase が必要";
    else if (s && s.result === "clean") note = "この順なら clean にマージできる";
    list.append(prCard(id, { step: i + 1, note }));
  });
  root.append(el("div", { class: "panel" },
    el("h3", {}, "このクラスタの推奨順", el("span", { class: "muted" }, `— ${PRESET_LABEL[state.preset] || state.preset}`)),
    el("div", { class: "panel-body" }, list)));

  // 衝突ペア一覧
  if (pairs.length) {
    const tb = el("tbody");
    for (const x of pairs.sort((m, n) => n.level - m.level)) {
      const xHunks = (x.conflict_files || []).filter((f) => f.hunks && f.hunks.length);
      const xActions = el("td", {});
      const xRow = el("tr", {},
        el("td", {}, levelChip(x.level), x.comment_only ? commentOnlyBadge() : null),
        el("td", {}, prOpenButton(x.a)),
        el("td", {}, prOpenButton(x.b)),
        el("td", { class: "small" },
          (x.conflict_files || []).map((f) => el("div", {}, fileLink(f.path))),
          !x.conflict_files?.length && x.overlap_files
            ? el("div", { class: "muted" }, el("code", {}, x.overlap_files.slice(0, 2).join(", ")))
            : null),
        el("td", { class: "small" },
          (x.warnings || []).map((w) => el("div", {},
            el("span", { class: "badge warn" }, w.kind === "same_function_region" ? "同一関数" : "依存/設定"),
            " ", el("code", {}, (w.symbols || [w.path]).join(", "))))),
        xActions);
      if (xHunks.length) {
        xActions.append(expandableRow(tb, xRow, 6, `衝突箇所（${xHunks.length}）`, () =>
          el("div", {}, ...xHunks.map((f) => conflictHunks(f, x.a, x.b)))));
      } else {
        tb.append(xRow);
      }
    }
    root.append(el("div", { class: "panel" },
      el("h3", {}, `クラスタ内の干渉（${pairs.length}ペア）`),
      el("div", { class: "table-scroll" },
        el("table", {},
          el("thead", {}, el("tr", {},
            el("th", {}, "レベル"), el("th", {}, "PR A"), el("th", {}, "PR B"),
            el("th", {}, "ファイル"), el("th", {}, "警告"), el("th", {}, ""))),
          tb))));
  }

  return root;
}


// --- ビュー: ファイル・関数 -------------------------------------------
//
// 「このファイル（この関数）を誰がどの PR で触っているか」を軸にした見方。
// PR を軸にした一覧では、同じ場所を複数人が別々に触っている状況が
// 見えないため。関数の粒度は、干渉解析が出す「双方が変更した関数」から
// 組み立てる（＝2件以上の PR が触った関数だけが並ぶ）。

/** 1 つの hunk を diff として描く（+/- 付き）。 */
function changeHunk(h) {
  const pre = el("div", { class: "diff" });
  for (const [mark, body] of h.lines) {
    const cls = mark === "+" ? "b" : mark === "-" ? "a" : "same";
    pre.append(el("div", { class: "diff-line " + cls },
      el("span", { class: "diff-mark" }, mark === " " ? " " : mark === "+" ? "＋" : "−"),
      el("span", { class: "diff-text" }, body === "" ? " " : body)));
  }
  if (h.truncated) {
    pre.append(el("div", { class: "diff-line trunc" },
      el("span", { class: "diff-mark" }, " "),
      el("span", { class: "diff-text" }, "…（以降省略）")));
  }
  return pre;
}

/** 場所（関数・ファイル）ごとに、そこを触る PR の変更を並べる。
 *
 *  ペア単位（A と B）で並べると、3 件以上が同じ場所を触るときに
 *  1 対 1 の比較が組み合わせの数だけ並んで全体像が掴めない。
 *  軸を場所側にして、関係する PR の変更を縦に積む。 */
function changesByLocation(path, rows) {
  // 関数ごとにまとめ直す（関数が取れないものは「トップレベル」へ）
  const byFn = new Map();
  for (const r of rows) {
    for (const h of r.hunks) {
      const key = h.function || "(トップレベル)";
      if (!byFn.has(key)) byFn.set(key, []);
      byFn.get(key).push({ pr: r.pr, hunk: h });
    }
  }

  const out = el("div", {});
  const entries = [...byFn.entries()].sort((a, b) => b[1].length - a[1].length);
  for (const [fn, items] of entries) {
    const prs = [...new Set(items.map((i) => i.pr))];
    const body = el("div", { class: "loc-body" });
    for (const pr of prs) {
      const meta = PR.get(pr);
      body.append(el("div", { class: "loc-pr" },
        el("div", { class: "loc-pr-head" },
          prOpenButton(pr),
          meta ? authorChip(meta.author, meta.author_avatar_url) : null,
          meta ? el("span", { class: "small muted" }, meta.title.slice(0, 52)) : null),
        ...items.filter((i) => i.pr === pr).map((i) =>
          el("div", { class: "loc-hunk" },
            el("div", { class: "loc-hunk-loc small muted" }, `${path}:${i.hunk.line}`),
            changeHunk(i.hunk)))));
    }
    out.append(el("details", { class: "loc", open: entries.length <= 2 },
      el("summary", {},
        el("code", { class: "fn-name" }, fn),
        el("span", { class: "badge" }, `${prs.length} PR がこの場所を変更`),
        el("span", { class: "file-authors" },
          ...authorsOf(prs).slice(0, 6).map(([a, av]) => authorChip(a, av, { compact: true })))),
      body));
  }
  return out;
}

function buildFileIndex() {
  const iv = DATA.interference[state.line] || { pairs: [] };
  const prs = DATA.pull_requests.filter((p) => p.line === state.line && p.changed_files);
  const files = new Map();
  const get = (path) => {
    if (!files.has(path)) {
      files.set(path, { path, prs: new Set(), conflicts: [], functions: new Map() });
    }
    return files.get(path);
  };

  for (const p of prs) {
    if (isHidden(p.id)) continue;
    for (const f of p.changed_files) get(f).prs.add(p.id);
  }

  for (const pair of iv.pairs) {
    if (pair.level === undefined) continue;
    if (isHidden(pair.a) || isHidden(pair.b)) continue;
    for (const cf of pair.conflict_files || []) {
      get(cf.path).conflicts.push({
        a: pair.a, b: pair.b, level: pair.level, structural: cf.structural,
        comment_only: cf.comment_only, file: cf,
      });
    }
    for (const w of pair.warnings || []) {
      if (w.kind !== "same_function_region") continue;
      const rec = get(w.path);
      for (const s of w.symbols || []) {
        if (!rec.functions.has(s)) rec.functions.set(s, new Set());
        rec.functions.get(s).add(pair.a);
        rec.functions.get(s).add(pair.b);
      }
    }
  }
  return files;
}

function authorsOf(prIds) {
  const seen = new Map();
  for (const id of prIds) {
    const pr = PR.get(id);
    if (pr && !seen.has(pr.author)) seen.set(pr.author, pr.author_avatar_url);
  }
  return [...seen.entries()];
}

function viewFiles() {
  const root = el("div");
  const index = buildFileIndex();

  root.append(el("p", { class: "hint" },
    "同じファイル・同じ関数を、誰がどの PR で触っているかを見る画面です。"
    + "関数の行は、干渉解析が「双方が変更した」と判定したものだけが並びます。"));

  // 絞り込み
  const bar = el("div", { class: "filters" });
  bar.append(el("input", {
    type: "search", id: "file-q", placeholder: "パスで絞り込み", value: state.fileQuery || "",
    oninput: (e) => { state.fileQuery = e.target.value; renderFileList(); },
  }));
  bar.append(el("label", {},
    el("input", {
      type: "checkbox", checked: state.filesSharedOnly !== false,
      onchange: (e) => { state.filesSharedOnly = e.target.checked; renderFileList(); },
    }), "複数PRが触るファイルのみ"));
  bar.append(el("label", {},
    el("input", {
      type: "checkbox", checked: !!state.filesConflictOnly,
      onchange: (e) => { state.filesConflictOnly = e.target.checked; renderFileList(); },
    }), "衝突があるファイルのみ"));
  root.append(bar);

  // ディレクトリ単位のまとめ（どの領域が混んでいるか）
  const dirs = new Map();
  for (const rec of index.values()) {
    // ルート直下のファイルはディレクトリ名を持たない。
    // そのまま先頭要素を使うと「pyproject.toml/」のような偽の
    // ディレクトリが並ぶ。
    const top = rec.path.includes("/") ? rec.path.split("/")[0] : "(ルート)";
    if (!dirs.has(top)) dirs.set(top, { files: 0, prs: new Set(), conflicts: 0 });
    const d = dirs.get(top);
    d.files++;
    for (const id of rec.prs) d.prs.add(id);
    d.conflicts += rec.conflicts.length;
  }
  const dirRows = [...dirs.entries()].sort((a, b) => b[1].prs.size - a[1].prs.size).slice(0, 8);
  const dirTable = el("table", {},
    el("thead", {}, el("tr", {},
      el("th", {}, "ディレクトリ"), el("th", { class: "num" }, "PR"),
      el("th", { class: "num" }, "ファイル"), el("th", { class: "num" }, "衝突"))),
    el("tbody", {}, ...dirRows.map(([name, d]) =>
      el("tr", {
        class: "dir-row" + (state.fileDir === name ? " selected" : ""),
        role: "button",
        tabindex: "0",
        title: `${name} 配下だけに絞り込む`,
        onclick: () => {
          state.fileDir = state.fileDir === name ? null : name;
          renderFileList();
          syncDirSelection();
        },
        onkeydown: (e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            state.fileDir = state.fileDir === name ? null : name;
            renderFileList();
            syncDirSelection();
          }
        },
      },
        el("td", {}, el("code", {}, name === "(ルート)" ? name : name + "/")),
        el("td", { class: "num" }, d.prs.size),
        el("td", { class: "num" }, d.files),
        el("td", { class: "num" }, d.conflicts)))));
  root.append(el("div", { class: "panel" },
    el("h3", {}, "混んでいる領域",
      el("span", { class: "muted" }, "— トップレベルディレクトリ別。行をクリックすると絞り込めます")),
    el("div", { class: "table-scroll" }, dirTable)));

  const listHost = el("div", { class: "panel" },
    el("h3", { id: "file-list-head" }, "ファイル"),
    el("div", { class: "panel-body", id: "file-list" }));
  root.append(listHost);

  function renderFileList() {
    const host = $("#file-list");
    if (!host) return;
    const q = (state.fileQuery || "").toLowerCase();
    let rows = [...index.values()]
      .filter((r) => !q || r.path.toLowerCase().includes(q))
      .filter((r) => {
        if (!state.fileDir) return true;
        const top = r.path.includes("/") ? r.path.split("/")[0] : "(ルート)";
        return top === state.fileDir;
      })
      .filter((r) => r.prs.size > 0)
      .filter((r) => state.filesSharedOnly === false || r.prs.size > 1)
      .filter((r) => !state.filesConflictOnly || r.conflicts.length)
      .filter((r) => !state.author || [...r.prs].some((id) => PR.get(id)?.author === state.author))
      .sort((a, b) =>
        b.conflicts.length - a.conflicts.length
        || b.prs.size - a.prs.size
        || a.path.localeCompare(b.path));

    const head = $("#file-list-head");
    if (head) head.replaceChildren(...[
      document.createTextNode("ファイル"),
      el("span", { class: "muted" }, `— ${rows.length}件`),
      state.fileDir
        ? el("span", { class: "dir-chip" },
            el("code", {}, state.fileDir === "(ルート)" ? state.fileDir : state.fileDir + "/"),
            el("button", {
              title: "ディレクトリの絞り込みを解除",
              onclick: (e) => { e.stopPropagation(); state.fileDir = null; renderFileList(); syncDirSelection(); },
            }, "✕"))
        : null,
    ].filter(Boolean));

    host.replaceChildren();
    if (!rows.length) { host.append(el("div", { class: "empty" }, "該当なし")); return; }

    for (const rec of rows.slice(0, 200)) {
      const open = state.file === rec.path;
      const authors = authorsOf(rec.prs);
      const summary = el("summary", {},
        el("code", {}, rec.path),
        el("span", { class: "pr-badges" },
          el("span", { class: "badge" }, `${rec.prs.size} PR`),
          rec.conflicts.length
            ? el("span", { class: "badge warn" }, `衝突 ${rec.conflicts.length}`) : null,
          rec.functions.size
            ? el("span", { class: "badge warn" }, `同一関数 ${rec.functions.size}`) : null),
        el("span", { class: "file-authors" },
          ...authors.slice(0, 6).map(([a, av]) => authorChip(a, av, { compact: true })),
          authors.length > 6 ? el("span", { class: "small muted" }, `+${authors.length - 6}`) : null));

      const body = el("div", { class: "file-body" });

      // 主役: この場所を、どの PR がどう変えようとしているか
      const changes = ((DATA.file_changes && DATA.file_changes[state.line]) || {})[rec.path]
        ?.filter((r) => !isHidden(r.pr));
      if (changes && changes.length) {
        body.append(el("section", {},
          el("h4", {}, "この場所を変更しようとしている PR"),
          changesByLocation(rec.path, changes)));
      } else if (rec.functions.size) {
        // 差分を持っていない場合は、せめて関数名と PR の対応を出す
        const fl = el("div", {});
        for (const [fn, ids] of [...rec.functions.entries()].sort((a, b) => b[1].size - a[1].size)) {
          fl.append(el("div", { class: "fn-row" },
            el("code", { class: "fn-name" }, fn),
            el("span", { class: "small muted" }, `${ids.size} PR`),
            el("span", { class: "fn-prs" }, ...[...ids].sort().map((id) => prOpenButton(id)))));
        }
        body.append(el("section", {}, el("h4", {}, "双方が変更した関数・クラス"), fl));
      }

      // 衝突は要約だけ。詳しい 1 対 1 の比較は干渉一覧に任せる。
      if (rec.conflicts.length) {
        const cl = el("div", {});
        for (const c of rec.conflicts.sort((x, y) => y.level - x.level)) {
          cl.append(el("div", { class: "fn-row" },
            levelChip(c.level),
            c.structural ? el("span", { class: "badge warn" }, "構造") : null,
            c.comment_only ? commentOnlyBadge() : null,
            el("span", { class: "fn-prs" },
              prOpenButton(c.a), el("span", { class: "muted" }, "↔"), prOpenButton(c.b))));
        }
        body.append(el("section", {},
          el("h4", {}, `このファイルで実際に衝突しているペア（${rec.conflicts.length}）`), cl));
      }

      body.append(el("section", {},
        el("h4", {}, `このファイルを変更する PR（${rec.prs.size}件）`),
        ...[...rec.prs].sort().map((id) => prCard(id))));

      host.append(el("details", {
        open,
        ontoggle: (e) => { if (e.target.open) { state.file = rec.path; writeHash(); } },
      }, summary, body));
    }
    if (rows.length > 200) {
      host.append(el("p", { class: "hint" }, `${rows.length - 200} 件を省略しました。パスで絞り込んでください。`));
    }
  }

  function syncDirSelection() {
    const rows = dirTable.querySelectorAll("tr.dir-row");
    dirRows.forEach(([name], i) => {
      const tr = rows[i];
      if (!tr) return;
      if (state.fileDir === name) tr.classList.add("selected");
      else tr.classList.remove("selected");
    });
    writeHash();
  }

  // 初回描画は DOM 挿入後に行う
  queueMicrotask(renderFileList);
  return root;
}

/** ファイルビューへ飛ぶリンク。 */
function fileLink(path) {
  // "path:line" の形でも渡されるので、遷移にはファイル部分だけを使う
  const bare = path.replace(/:\d+$/, "");
  return el("button", {
    class: "pr-open",
    title: "このファイルを触っている PR を見る",
    onclick: (e) => {
      e.stopPropagation();
      closePanel();
      state.view = "files";
      state.file = bare;
      state.fileQuery = bare;
      state.filesSharedOnly = false;
      render();
    },
  }, path);
}

// --- ビュー: 干渉一覧 --------------------------------------------------

function viewConflicts() {
  const iv = DATA.interference[state.line];
  const root = el("div");
  const counts = iv.level_counts;

  root.append(el("p", { class: "hint" },
    `${iv.pairs_evaluated} ペアを評価。衝突は疎なので、行列ではなく一覧で示す。`
    + `L0（無干渉）は ${counts.L0 || 0} ペア。`));

  root.append(el("div", { class: "legend" },
    el("span", {}, levelChip(1), LEVEL_DESC[1]),
    el("span", {}, levelChip(2), LEVEL_DESC[2]),
    el("span", {}, levelChip(3), LEVEL_DESC[3]),
    el("span", {}, el("span", { class: "badge warn" }, "△"), "同一関数・依存ファイル"),
  ));

  if (counts.degraded) {
    root.append(el("div", { class: "action" },
      el("div", { class: "big" }, counts.degraded),
      el("div", {}, el("p", {}, el("strong", {}, "ペアは判定不能")),
        el("div", { class: "sub" },
          "片方がベースと衝突していて着地させられないため、同時マージ可能性を問えない。"
          + "該当PRを rebase すれば解析できるようになる。"))));
  }

  const rows = iv.pairs
    .filter((p) => p.level !== undefined && p.level >= 1)
    .filter((p) => !isHidden(p.a) && !isHidden(p.b))
    .filter((p) => !state.author || [p.a, p.b].some((x) => PR.get(x)?.author === state.author))
    // 実コードの衝突を先に。コメントだけのものは同じ等級でも後ろへ。
    .sort((a, b) =>
      (!!a.comment_only - !!b.comment_only)
      || b.level - a.level
      || (b.conflict_files || []).length - (a.conflict_files || []).length);

  if (!rows.length) return root.append(el("div", { class: "empty" }, "該当なし")), root;

  const t = el("table");
  t.append(el("thead", {}, el("tr", {},
    el("th", {}, "レベル"), el("th", {}, "PR A"), el("th", {}, "PR B"),
    el("th", {}, "衝突/重複ファイル"), el("th", {}, "警告"), el("th", {}, ""))));
  const tb = el("tbody");
  for (const p of rows) {
    const files = p.conflict_files && p.conflict_files.length
      ? p.conflict_files.map((f) =>
          el("div", {}, fileLink(f.path),
            f.structural ? el("span", { class: "badge warn", title: `ステージ ${f.stages.join(",")}` }, "構造") : null))
      : (p.overlap_files || []).slice(0, 4).map((f) => el("div", { class: "muted" }, fileLink(f)));
    const warns = (p.warnings || []).map((w) =>
      el("div", { class: "small" },
        el("span", { class: "badge warn" }, w.kind === "same_function_region" ? "同一関数" : "依存/設定"),
        " ", w.symbols ? el("code", {}, w.symbols.join(", ")) : el("code", {}, w.path)));
    const withHunks = (p.conflict_files || []).filter((f) => f.hunks && f.hunks.length);
    const actions = el("td", {});
    const row = el("tr", {},
      el("td", {}, levelChip(p.level), p.comment_only ? commentOnlyBadge() : null),
      el("td", {}, prLink(p.a)),
      el("td", {}, prLink(p.b)),
      el("td", { class: "small" }, files),
      el("td", {}, warns),
      actions);
    if (withHunks.length) {
      actions.append(expandableRow(tb, row, 6, `衝突箇所（${withHunks.length}ファイル）`, () =>
        el("div", {}, ...withHunks.map((f) => conflictHunks(f, p.a, p.b)))));
    } else {
      tb.append(row);
    }
  }
  t.append(tb);
  root.append(el("div", { class: "table-scroll" }, t));
  return root;
}

function prLink(id) {
  const pr = PR.get(id);
  if (!pr) return el("span", {}, id);
  return el("span", {},
    prOpenButton(id),
    pr.is_draft ? el("span", { class: "badge draft" }, "D") : null,
    pr.review_decision === "APPROVED" ? el("span", { class: "badge approved" }, "✓") : null,
    el("div", { class: "small muted" }, pr.title.slice(0, 46)),
    el("div", {}, authorChip(pr.author, pr.author_avatar_url)));
}

// --- ビュー: スタック --------------------------------------------------

function viewStacks() {
  const root = el("div");
  if (state.hideDraft) {
    root.append(el("p", { class: "hint" },
      "⚠ この画面では「Draft を隠す」を適用していません。"
      + "鎖の途中を省くと、存在しない直接依存があるように見えてしまうためです。"));
  }
  root.append(el("p", { class: "hint" },
    "スタックした PR は、親がマージされるまでマージできない（ハード制約）。"
    + "横軸がスタック深度、帯がリポジトリ。帯の境界を跨ぐ矢印は、"
    + "別リポジトリに続いていることを示す。"));

  // 子を持つ or 親を持つ PR だけを鎖として抽出
  const inLine = DATA.pull_requests.filter((p) => p.line === state.line);
  const tips = inLine.filter((p) => p.stack.depth > 0 && !inLine.some((q) => q.stack.ancestors.includes(p.id)));

  if (!tips.length) return root.append(el("div", { class: "empty" }, "スタックPRなし")), root;

  for (const tip of tips.sort((a, b) => b.stack.depth - a.stack.depth)) {
    const chainIds = [...tip.stack.ancestors, tip.id];
    const repos = [...new Set(chainIds.map((id) => PR.get(id)?.repo).filter(Boolean))];
    const box = el("div", { class: "swimlane" });
    box.append(el("div", { class: "repo-band" },
      "リポジトリ: " + repos.join("  ⇄  "),
      repos.length > 1 ? el("span", { class: "badge fork" }, "フォークを跨ぐ") : null));

    const chain = el("div", { class: "chain" });
    chain.append(el("div", { class: "chain-node line-root" }, state.line));
    let prevRepo = null;
    chainIds.forEach((id) => {
      const pr = PR.get(id);
      const crossing = prevRepo && pr && pr.repo !== prevRepo;
      chain.append(el("span", { class: "chain-arrow" + (crossing ? " cross-repo" : "") },
        crossing ? "⇒" : "→"));
      chain.append(el("button", {
        class: "chain-node" + (pr?.kind === "external_pr" ? " external" : ""),
        title: pr?.title || id,
        onclick: () => openPanel(id),
      }, pr ? `${pr.repo.split("/")[0]}#${pr.number}` : id));
      prevRepo = pr?.repo || prevRepo;
    });
    box.append(chain);

    if (repos.length > 1) {
      box.append(el("div", { class: "panel-body small muted" },
        el("strong", {}, "運用上の注意: "),
        "フォーク側リポジトリに対して開かれた PR は、上流リポジトリへ"
        + "直接マージできない。この鎖を通すには、上流から順にマージしたうえで、"
        + "フォーク側の PR を上流リポジトリに対して開き直す必要がある。"));
    }
    root.append(box);
  }

  // 掃除タスク
  const dup = DATA.warnings.filter((w) => w.kind === "duplicate_pr_head");
  const orphan = DATA.warnings.filter((w) => w.kind === "orphan_base_branch");
  if (dup.length || orphan.length) {
    const body = el("div", { class: "panel-body" });
    for (const w of [...dup, ...orphan]) {
      body.append(el("div", { class: "pr" },
        el("span", { class: "badge warn" },
          w.kind === "duplicate_pr_head" ? "⧉ 同一コミット" : "親PR不在"),
        el("span", { class: "pr-title" }, w.detail),
        el("span", { class: "mono small" }, w.subjects.map(shortId).join(" "))));
    }
    root.append(el("div", { class: "panel" },
      el("h3", {}, "確認したいもの",
        el("span", { class: "muted" }, "— 順序の問題ではなく、構成の確認")), body));
  }
  return root;
}

// --- ビュー: 自分視点 --------------------------------------------------

function viewMine() {
  const root = el("div");
  if (!state.author) {
    root.append(el("div", { class: "empty" },
      "上の「著者」で自分を選んでください。"));
    return root;
  }

  const mine = DATA.pull_requests.filter(
    (p) => p.author === state.author && p.line === state.line && !isHidden(p.id));
  const iv = DATA.interference[state.line];
  const o = DATA.orders[state.line];
  const preset = o.presets[state.preset] || o.presets.balanced;
  const rank = new Map(preset.order.map((id, i) => [id, i]));

  // 1 つの PR に理由が複数付くので、PR ごとに理由をまとめる。
  // 理由の数だけカードを積むと、同じ PR が何度も並んで読めなくなる。
  const ready = new Map(), waiting = new Map(), blocking = new Map(), todo = new Map();
  const add = (box, id, reason) => {
    if (!box.has(id)) box.set(id, []);
    if (reason && !box.get(id).includes(reason)) box.get(id).push(reason);
  };

  for (const p of mine) {
    const unmergedAncestors = p.stack.ancestors.filter((a) => PR.has(a));
    if (p.base_conflict) add(todo, p.id, "ベースと衝突している。rebase が必要");
    if (p.is_draft) add(todo, p.id, "Draft のまま");
    if (p.review_decision === "REVIEW_REQUIRED") add(todo, p.id, "レビュー未実施");
    if (p.review_decision === "CHANGES_REQUESTED") add(todo, p.id, "修正要求に対応が必要");
    if (p.duplicate_of) {
      add(todo, p.id, `${p.duplicate_of.map(shortId).join(", ")} と同一コミット。どちらかをクローズ`);
    }

    if (unmergedAncestors.length) {
      add(waiting, p.id, `${unmergedAncestors.map(shortId).join(" → ")} が先にマージされる必要がある`);
    } else if (!p.base_conflict) {
      add(ready, p.id, null);
    }
    if (p.blocks.length) {
      const who = [...new Set(p.blocks.map((b) => PR.get(b)?.author).filter(Boolean))];
      add(blocking, p.id, `${p.blocks.length}件（${who.join(", ")}）がこの PR を待っている`);
    }
  }

  // 衝突相手のうち、自分が先に推奨されているもの／後のもの
  for (const pair of iv.pairs) {
    if (pair.level === undefined || pair.level < 2) continue;
    if (isHidden(pair.a) || isHidden(pair.b)) continue;
    const [a, b] = [pair.a, pair.b];
    const mineSide = PR.get(a)?.author === state.author ? a : PR.get(b)?.author === state.author ? b : null;
    if (!mineSide) continue;
    const other = mineSide === a ? b : a;
    if (PR.get(other)?.author === state.author) continue;
    const label = `${shortId(other)}（${PR.get(other)?.author}）と L${pair.level} 衝突`;
    if ((rank.get(mineSide) ?? 0) < (rank.get(other) ?? 0)) {
      add(blocking, mineSide, label + " — あなたが先の推奨");
    } else {
      add(waiting, mineSide, label + " — 相手が先の推奨");
    }
  }

  // 待つ理由がある PR は「今すぐマージできる」ではない
  for (const id of waiting.keys()) ready.delete(id);

  const box = (title, map, hint) => {
    const entries = [...map.entries()].filter(([id]) => !isHidden(id)).sort(
      (x, y) => (rank.get(x[0]) ?? 1e9) - (rank.get(y[0]) ?? 1e9),
    );
    return el("div", { class: "panel" },
      el("h3", {}, title, el("span", { class: "muted" }, `— ${entries.length}件`)),
      el("div", { class: "panel-body" },
        hint ? el("p", { class: "hint" }, hint) : null,
        entries.length
          ? entries.map(([id, reasons]) => prCard(id, { reasons }))
          : el("div", { class: "empty" }, "なし")));
  };

  const grid = el("div", { class: "grid cols-2" });
  grid.append(box("今すぐマージできる", ready, "ベース衝突がなく、待つべき親も衝突相手もない"));
  grid.append(box("あなたが待たせている", blocking, "他の人の作業がここで止まっている"));
  grid.append(box("あなたが待っている", waiting, null));
  grid.append(box("あなたのTODO", todo, null));
  root.append(grid);
  return root;
}

// --- ビュー: PR一覧 ----------------------------------------------------

let sortKey = "number", sortDir = 1;

function viewTable() {
  const root = el("div");
  const rows = DATA.pull_requests
    .filter((p) => p.line === state.line)
    .filter((p) => !isHidden(p.id))
    .filter((p) => !state.author || p.author === state.author);

  const o = DATA.orders[state.line];
  const rank = new Map((o.presets[state.preset] || o.presets.balanced).order.map((id, i) => [id, i + 1]));
  const metrics = o.metrics || {};

  const cols = [
    ["順", (p) => rank.get(p.id) ?? 999, true],
    ["PR", (p) => p.number, false],
    ["タイトル", (p) => p.title, false],
    ["著者", (p) => p.author, false],
    ["状態", (p) => p.review_decision, false],
    ["規模", (p) => p.additions + p.deletions, true],
    ["blocks", (p) => (metrics[p.id]?.blocks ?? 0), true],
    ["regret", (p) => (metrics[p.id]?.regret ?? 0), true],
  ];

  rows.sort((a, b) => {
    const c = cols.find((c) => c[0] === sortKey) || cols[0];
    const va = c[1](a), vb = c[1](b);
    return (va > vb ? 1 : va < vb ? -1 : 0) * sortDir;
  });

  const t = el("table");
  t.append(el("thead", {}, el("tr", {},
    ...cols.map(([name, , isNum]) =>
      el("th", {
        class: isNum ? "num" : "",
        onclick: () => { sortDir = sortKey === name ? -sortDir : 1; sortKey = name; render(); },
      }, name + (sortKey === name ? (sortDir > 0 ? " ▲" : " ▼") : ""))))));
  const tb = el("tbody");
  for (const p of rows) {
    tb.append(el("tr", {},
      el("td", { class: "num" }, rank.get(p.id) ?? "—"),
      el("td", { class: "num" }, prOpenButton(p.id)),
      el("td", {}, p.title, " ", el("span", { class: "pr-badges" }, ...prBadges(p))),
      el("td", {}, authorChip(p.author, p.author_avatar_url)),
      el("td", { class: "small" }, p.review_decision),
      el("td", { class: "num" }, `+${p.additions}/-${p.deletions}`),
      el("td", { class: "num" }, metrics[p.id]?.blocks ?? 0),
      el("td", { class: "num" }, (metrics[p.id]?.regret ?? 0).toFixed(1))));
  }
  t.append(tb);
  root.append(el("div", { class: "table-scroll" }, t));
  return root;
}



// --- 干渉グラフ -------------------------------------------------------
//
// 弧ダイアグラム。**横軸が推奨マージ順**（左が先）で、そこに 2 種類の
// 関係を上下に分けて描く:
//
//   下側（有向・矢印）  スタック依存。親が入るまで子はマージできない
//                       ＝ 本当の意味で「ブロックしている」関係
//   上側（無向）        衝突。どちらの順で流しても誰かが解決する。
//                       向きを描かないのは、解析が測っているのが
//                       「同時マージ可能性」という対称な性質だから
//
// 全部描くと密になるので、レベル下限・著者・PR 選択で絞れるようにする。

const SVG_NS = "http://www.w3.org/2000/svg";
const svg = (tag, attrs = {}, ...kids) => {
  const n = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v === null || v === undefined) continue;
    if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    n.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return n;
};

function interferenceGraph(ids, { height = 300 } = {}) {
  const o = DATA.orders[state.line];
  const iv = DATA.interference[state.line];
  const preset = o.presets[state.preset] || o.presets.balanced;
  const rank = new Map(preset.order.map((id, i) => [id, i]));

  // 推奨順に並べる。横位置そのものが「いつ流すか」を表す。
  const nodes = visiblePrs(ids).sort((a, b) => (rank.get(a) ?? 1e9) - (rank.get(b) ?? 1e9));
  const idx = new Map(nodes.map((id, i) => [id, i]));

  const conflicts = iv.pairs.filter(
    (p) => p.level !== undefined && p.level >= state.minLevel
      && idx.has(p.a) && idx.has(p.b),
  );
  // スタックは作者が意図した依存で、このビューアが解決を助けたい
  // 「意図しない干渉」ではない。文脈として薄く描き、消せるようにする。
  //
  // `stack.ancestors` は推移的な祖先すべてを持つ。そのまま辺にすると
  // A→B→C→D の鎖で 6 本描かれてしまう（A→C や A→D まで引かれる）。
  // 必要なのは直接の親だけなので、鎖の中で最も近い祖先 1 本に絞る。
  const stacks = [];
  if (state.showStack !== false) {
    for (const id of nodes) {
      const ancestors = (PR.get(id) || {}).stack?.ancestors || [];
      // ancestors はルート側が先頭。表示されている中で最も近いものが直接の親。
      for (let i = ancestors.length - 1; i >= 0; i--) {
        if (idx.has(ancestors[i])) {
          stacks.push({ from: ancestors[i], to: id });
          break;
        }
      }
    }
  }

  const STEP = Math.max(46, Math.min(96, Math.floor(1120 / Math.max(nodes.length, 1))));
  const NW = Math.min(STEP - 8, 54), NH = 22;
  const W = Math.max(nodes.length * STEP + 40, 320);
  const midY = Math.round(height / 2);

  const x = (id) => 20 + idx.get(id) * STEP + STEP / 2;

  const g = svg("svg", {
    class: "graph", width: W, height,
    viewBox: `0 0 ${W} ${height}`, role: "img",
    "aria-label": "干渉グラフ",
  });

  g.append(svg("defs", {},
    svg("marker", {
      id: "arrow", viewBox: "0 0 8 8", refX: 7, refY: 4,
      markerWidth: 6, markerHeight: 6, orient: "auto-start-reverse",
    }, svg("path", { d: "M0,0 L8,4 L0,8 z", fill: "var(--ink-2)" }))));

  // 推奨順の軸
  g.append(svg("line", { class: "axis", x1: 10, y1: midY, x2: W - 10, y2: midY }));
  g.append(svg("text", { class: "axis-label", x: 12, y: midY - NH / 2 - 8 }, "← 先にマージ"));
  g.append(svg("text", { class: "axis-label", x: W - 12, y: midY - NH / 2 - 8, "text-anchor": "end" }, "後 →"));

  const focus = state.pr && idx.has(state.pr) ? state.pr : null;
  const touches = (a, b) => !focus || a === focus || b === focus;

  // 上: 衝突（無向）
  for (const c of conflicts) {
    const x1 = x(c.a), x2 = x(c.b);
    const span = Math.abs(x2 - x1);
    const r = Math.min(span / 2, midY - NH / 2 - 14);
    const dir = -1;
    g.append(svg("path", {
      class: `edge lv${c.level}` + (c.comment_only ? " comment-only" : "")
        + (touches(c.a, c.b) ? "" : " dim"),
      d: `M${x1},${midY + dir * (NH / 2)} A${span / 2},${r} 0 0,${x2 > x1 ? 1 : 0} ${x2},${midY + dir * (NH / 2)}`,
    }, svg("title", {}, `${shortId(c.a)} ↔ ${shortId(c.b)} — L${c.level} 衝突`
      + `（順序は「誰が rebase するか」を決めるだけ）`)));
  }

  // 下: スタック依存（有向）
  for (const s of stacks) {
    const x1 = x(s.from), x2 = x(s.to);
    const span = Math.abs(x2 - x1);
    const r = Math.min(span / 2, midY - NH / 2 - 14);
    g.append(svg("path", {
      class: "edge stack intentional" + (touches(s.from, s.to) ? "" : " dim"),
      "marker-end": "url(#arrow)",
      d: `M${x1},${midY + NH / 2} A${span / 2},${r} 0 0,${x2 > x1 ? 0 : 1} ${x2},${midY + NH / 2}`,
    }, svg("title", {}, `${shortId(s.from)} → ${shortId(s.to)} — ${shortId(s.from)} がマージされるまで ${shortId(s.to)} はマージできない`)));
  }

  // ノード
  for (const id of nodes) {
    const pr = PR.get(id);
    const cls = ["node",
      pr?.review_decision === "APPROVED" ? "approved" : "",
      pr?.is_draft ? "draft" : "",
      pr?.base_conflict ? "rebase" : "",
      focus === id ? "sel" : "",
      focus && focus !== id
        && !conflicts.some((c) => (c.a === id && c.b === focus) || (c.b === id && c.a === focus))
        && !stacks.some((s) => (s.from === id && s.to === focus) || (s.to === id && s.from === focus))
        ? "dim" : "",
    ].join(" ");
    const cx = x(id);
    g.append(svg("g", {
      class: cls, role: "button", tabindex: "0",
      onclick: () => openPanel(id),
      onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openPanel(id); } },
    },
      svg("rect", { x: cx - NW / 2, y: midY - NH / 2, width: NW, height: NH, rx: 4 }),
      svg("text", { x: cx, y: midY + 4, "text-anchor": "middle" }, shortId(id)),
      svg("title", {}, `${id}\n${pr?.title || ""}\n著者: ${pr?.author || "?"}`),
    ));
  }

  const wrap = el("div", {});
  wrap.append(el("div", { class: "graph-wrap" }, g));
  wrap.append(el("div", { class: "graph-legend" },
    el("span", {}, el("i", { style: "border-color: var(--l2)" }), "上側の弧 = 衝突（無向。順序は誰が払うかを決めるだけ）"),
    el("span", {}, el("i", { class: "legend-stack" }), "下側の矢印 = スタック依存（作者が意図した順序。干渉ではない）"),
    el("span", {}, el("i", { class: "legend-comment" }), "点線 = コメント・文書だけの衝突"),
    el("span", {}, "枠が緑 = Approved / 破線 = Draft / 赤 = 要rebase"),
  ));
  if (focus) {
    wrap.append(el("p", { class: "hint" },
      `${shortId(focus)} に関係する辺だけを強調しています。`,
      el("button", { onclick: closePanel }, "強調を解除")));
  }
  return wrap;
}

/** グラフの絞り込み。全部出すと密になるので既定は L2 以上。 */
function graphFilters() {
  const row = el("div", { class: "filters" }, el("span", { class: "small muted" }, "グラフに出す干渉:"));
  for (const [lv, label] of [[1, "L1以上（同一ファイル含む）"], [2, "L2以上（実際に衝突）"], [3, "L3のみ（構造衝突）"]]) {
    row.append(el("button", {
      "aria-pressed": state.minLevel === lv,
      onclick: () => { state.minLevel = lv; render(); },
      title: LEVEL_DESC[lv],
    }, label));
  }
  row.append(el("label", {},
    el("input", {
      type: "checkbox", checked: state.showStack !== false,
      onchange: (e) => { state.showStack = e.target.checked; render(); },
    }), "スタック依存も表示"));
  return row;
}

// --- サイドパネル -----------------------------------------------------
// PR を見るのにページ遷移させない。一覧の文脈を保ったまま詳細を開く。

function openPanel(id) {
  state.pr = id;
  writeHash();
  renderPanel();
}

function closePanel() {
  state.pr = null;
  writeHash();
  renderPanel();
}

/** その PR が関わる干渉ペアを、相手・レベル付きで集める。 */
function pairsFor(id) {
  const iv = DATA.interference[state.line];
  if (!iv) return [];
  return iv.pairs
    .filter((p) => (p.a === id || p.b === id) && p.level !== undefined && p.level >= 1)
    .map((p) => ({ ...p, other: p.a === id ? p.b : p.a }))
    .sort((x, y) => (!!x.comment_only - !!y.comment_only) || y.level - x.level);
}



/** 表の行の直下に、全幅の展開行を足す。
 *
 *  差分を列の中に押し込むと横幅が足りず、1 行が数語で折り返して読めない。
 *  `colspan` で表の幅いっぱいを使う行に逃がす。 */
function expandableRow(tbody, row, colCount, label, buildContent) {
  const holder = el("tr", { class: "expand-row", hidden: true });
  const cell = el("td", { colspan: String(colCount) });
  holder.append(cell);
  let built = false;
  const btn = el("button", {
    class: "pr-open expand-toggle",
    onclick: (e) => {
      e.stopPropagation();
      const open = holder.hasAttribute("hidden");
      if (open && !built) { cell.append(buildContent()); built = true; }
      if (open) holder.removeAttribute("hidden");
      else holder.setAttribute("hidden", "");
      btn.textContent = (open ? "▾ " : "▸ ") + label;
    },
  }, "▸ " + label);
  tbody.append(row, holder);
  return btn;
}

/** 2 つの行列の最長共通部分列。衝突ハンクは高々数十行なので素朴な DP でよい。 */
function lineDiff(a, b) {
  const n = a.length, m = b.length;
  const dp = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const out = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { out.push({ t: "same", s: a[i] }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push({ t: "a", s: a[i] }); i++; }
    else { out.push({ t: "b", s: b[j] }); j++; }
  }
  while (i < n) out.push({ t: "a", s: a[i++] });
  while (j < m) out.push({ t: "b", s: b[j++] });
  return out;
}

/** 衝突箇所を差分として見せる。
 *
 *  ここで並ぶ 2 つは「変更前と変更後」ではなく、**同じ場所に対する
 *  2 つの案**である。どちらが + でどちらが − かは本質的に任意なので、
 *  記号だけに頼らず、行頭に PR 番号を出して取り違えを防ぐ。 */
function conflictHunks(file, aId, bId) {
  if (!file.hunks || !file.hunks.length) return null;
  const box = el("div", { class: "hunks" });

  const legend = el("div", { class: "diff-legend" },
    el("span", { class: "diff-key a" }, "−"), prOpenButton(aId), el("span", { class: "muted" }, "側　"),
    el("span", { class: "diff-key b" }, "＋"), prOpenButton(bId), el("span", { class: "muted" }, "側"),
    el("span", { class: "small muted" }, "　どちらが正しいという意味ではなく、同じ場所に対する 2 つの案です"));
  box.append(legend);

  for (const h of file.hunks) {
    const rows = lineDiff(h.a || [], h.b || []);
    const pre = el("div", { class: "diff" });
    for (const r of rows) {
      const mark = r.t === "a" ? "−" : r.t === "b" ? "＋" : " ";
      pre.append(el("div", { class: "diff-line " + r.t },
        el("span", { class: "diff-mark" }, mark),
        el("span", { class: "diff-text" }, r.s === "" ? " " : r.s)));
    }
    if (h.a_truncated || h.b_truncated) {
      pre.append(el("div", { class: "diff-line trunc" },
        el("span", { class: "diff-mark" }, " "),
        el("span", { class: "diff-text" }, "…（以降省略）")));
    }
    box.append(el("div", { class: "hunk" },
      el("div", { class: "hunk-loc" },
        el("code", {}, `${file.path}:${h.line}`),
        el("span", { class: "small muted" },
          `　${(h.a || []).length} 行 ↔ ${(h.b || []).length} 行`)),
      pre));
  }
  return box;
}

/** PR 詳細の中身。**サイドパネルと単一ページで共有する**（表示場所が
 *  変わっても同じ情報・同じ並びになるようにするため）。 */
function prDetail(id) {
  const pr = PR.get(id);
  const o = DATA.orders[state.line] || {};
  const preset = (o.presets || {})[state.preset] || (o.presets || {}).balanced;
  const rank = preset ? preset.order.indexOf(pr.id) : -1;
  const metrics = (o.metrics || {})[pr.id];
  const cluster = (o.clusters || []).find((c) => c.members.includes(pr.id));
  const related = pairsFor(pr.id);

  const out = [];

  const stats = el("div", { class: "stat-row" });
  if (rank >= 0) stats.append(el("div", {}, el("strong", {}, rank + 1), `推奨順（${preset.order.length}件中）`));
  if (metrics) stats.append(el("div", {}, el("strong", {}, metrics.blocks), "スタックでブロック"));
  stats.append(el("div", {}, el("strong", {}, related.length), "干渉する相手"));
  stats.append(el("div", {}, el("strong", {}, `+${pr.additions}/-${pr.deletions}`), `${pr.changed_files_count} ファイル`));
  out.push(stats);

  // レビューでの指摘。要修正の PR は「何を直すか」が最優先の情報。
  const notes = pr.review_notes || [];
  if (notes.length) {
    const inline = notes.filter((n) => n.state === "INLINE");
    const overall = notes.filter((n) => n.state !== "INLINE");
    const box = el("div", {});
    for (const n of [...overall, ...inline]) {
      box.append(el("div", { class: "review-note" + (n.state === "CHANGES_REQUESTED" ? " changes" : "") },
        el("div", { class: "review-head" },
          authorChip(n.author, ""),
          n.state === "CHANGES_REQUESTED"
            ? el("span", { class: "badge warn" }, "要修正")
            : n.state === "INLINE"
              ? el("span", { class: "badge" }, "指摘")
              : el("span", { class: "badge" }, "コメント"),
          n.path ? fileLink(n.path + (n.line ? `:${n.line}` : "")) : null,
          n.outdated ? el("span", { class: "badge", title: "コメント後にこの箇所が変更された" }, "行がずれている") : null,
          n.url ? el("a", { class: "small", href: n.url, target: "_blank", rel: "noopener" }, "元コメント ↗") : null),
        el("p", { class: "review-body" }, n.body)));
    }
    out.push(el("section", {},
      el("h4", {}, pr.review_decision === "CHANGES_REQUESTED"
        ? `修正が必要な箇所（${notes.length}件）` : `レビューでの指摘（${notes.length}件）`),
      box));
  } else if (pr.review_decision === "CHANGES_REQUESTED") {
    out.push(el("section", {}, el("h4", {}, "修正が必要"),
      el("p", { class: "hint" },
        "修正要求が出ていますが、本文のあるコメントを取得できませんでした。"
        + "GitHub 側で確認してください。")));
  }

  // 干渉（干渉一覧ページと同じ形式）。この PR を見に来る一番の理由なので先頭に置く。
  if (related.length) {
    const tb = el("tbody");
    for (const r of related) {
      const other = PR.get(r.other);
      const rHunks = (r.conflict_files || []).filter((f) => f.hunks && f.hunks.length);
      const rActions = el("td", {});
      const rRow = el("tr", {},
        el("td", {}, levelChip(r.level), r.comment_only ? commentOnlyBadge() : null),
        el("td", {}, prOpenButton(r.other),
          el("div", { class: "small muted" }, other ? other.title.slice(0, 44) : ""),
          other ? el("div", {}, authorChip(other.author, other.author_avatar_url)) : null),
        el("td", { class: "small" },
          (r.conflict_files || []).map((f) =>
            el("div", {}, fileLink(f.path),
              f.structural ? el("span", { class: "badge warn" }, "構造") : null)),
          !r.conflict_files?.length && r.overlap_files
            ? el("div", { class: "muted" }, el("code", {}, r.overlap_files.slice(0, 3).join(", ")))
            : null),
        el("td", { class: "small" },
          (r.warnings || []).map((w) => el("div", {},
            el("span", { class: "badge warn" }, w.kind === "same_function_region" ? "同一関数" : "依存/設定"),
            " ", el("code", {}, (w.symbols || [w.path]).join(", "))))),
        rActions);
      if (rHunks.length) {
        rActions.append(expandableRow(tb, rRow, 5, `衝突箇所（${rHunks.length}）`, () =>
          el("div", {}, ...rHunks.map((f) => conflictHunks(f, r.a, r.b)))));
      } else {
        tb.append(rRow);
      }
    }
    out.push(el("section", {},
      el("h4", {}, `干渉する PR（${related.length}件）`),
      el("div", { class: "table-scroll" },
        el("table", {},
          el("thead", {}, el("tr", {},
            el("th", {}, "レベル"), el("th", {}, "相手"), el("th", {}, "ファイル"),
            el("th", {}, "警告"), el("th", {}, ""))),
          tb))));
  } else {
    out.push(el("section", {}, el("h4", {}, "干渉する PR"),
      el("p", { class: "hint" }, "この統合ラインの中では、どの PR とも干渉していません。")));
  }

  // ベース衝突
  if (pr.base_conflict && pr.base_conflict_files) {
    out.push(el("section", {},
      el("h4", {}, "ベースとの衝突（まず rebase が必要）"),
      el("ul", { class: "tight small" },
        ...pr.base_conflict_files.map((f) =>
          el("li", {}, fileLink(f.path), " ",
            el("span", { class: "muted" }, `ステージ ${f.stages.join(",")}`))))));
  }

  const kv = el("dl", { class: "kv" });
  const put = (k, v) => { kv.append(el("dt", {}, k), el("dd", {}, v)); };
  put("著者", pr.author);
  put("レビュー", pr.review_decision);
  put("ブランチ", el("code", {},
    `${pr.head.repo === DATA.source.repo ? "" : pr.head.repo.split("/")[0] + ":"}${pr.head.branch}`));
  put("マージ先", el("code", {}, pr.base.branch));
  put("クラスタ", cluster
    ? el("button", {
        class: "cluster-link",
        onclick: () => { closePanel(); state.view = "cluster"; state.cluster = cluster.id; render(); },
      }, `${cluster.id}（${cluster.members.length}件）`)
    : el("span", { class: "muted" }, "独立（単独でマージ可能）"));
  if (pr.stack.depth > 0) {
    put("スタック", el("span", {}, ...pr.stack.ancestors.map((a, i) =>
      el("span", {}, i ? " → " : "", prOpenButton(a))), " → ", el("strong", {}, shortId(pr.id))));
  }
  if (pr.blocks.length) {
    put("これを待つPR", el("span", {}, ...pr.blocks.map((b, i) => el("span", {}, i ? " " : "", prOpenButton(b)))));
  }
  if (pr.duplicate_of) {
    put("同一コミット", el("span", {},
      ...pr.duplicate_of.map((d) => prOpenButton(d)),
      el("div", { class: "small muted" },
        "head が同じ PR。マージ先が違う場合は、レビュー用と着地用を"
        + "分けていることがあるので、重複とは限らない")));
  }
  out.push(el("section", {}, el("h4", {}, "この PR について"), kv));

  if (pr.changed_files && pr.changed_files.length) {
    out.push(el("section", {},
      el("h4", {}, `変更ファイル（${pr.changed_files.length}件）`),
      el("details", {},
        el("summary", { class: "muted" }, "一覧を開く"),
        el("ul", { class: "tight small mono" },
          ...pr.changed_files.map((f) => el("li", {}, fileLink(f)))))));
  }
  return out;
}

function renderPanel() {
  const backdrop = $("#panel-backdrop");
  const panel = $("#side-panel");
  // 単一ページで見ているときはパネルを重ねない
  const show = state.pr && PR.has(state.pr) && state.view !== "pr";
  if (!show) {
    backdrop.removeAttribute("data-open");
    panel.removeAttribute("data-open");
    panel.setAttribute("aria-hidden", "true");
    return;
  }
  const pr = PR.get(state.pr);
  panel.replaceChildren(
    el("header", {},
      el("div", { class: "grow" },
        el("div", { class: "mono small muted" }, prRef(pr.id), " ", pr.repo),
        el("h2", {}, pr.title),
        el("div", { class: "pr-badges" },
          authorChip(pr.author, pr.author_avatar_url), ...prBadges(pr))),
      el("button", {
        title: "このPRだけのページを開く",
        onclick: () => { state.view = "pr"; render(); },
      }, "⤢ ページで開く"),
      el("a", { class: "btn", href: pr.url, target: "_blank", rel: "noopener" }, "GitHub ↗"),
      el("button", { onclick: closePanel, title: "閉じる（Esc）", "aria-label": "閉じる" }, "✕")),
    el("div", { class: "body" }, ...prDetail(state.pr)),
  );
  panel.setAttribute("data-open", "");
  panel.setAttribute("aria-hidden", "false");
  backdrop.setAttribute("data-open", "");
}

// --- ビュー: PR 単一ページ ---------------------------------------------

function viewPr() {
  const root = el("div");
  if (!state.pr || !PR.has(state.pr)) {
    root.append(el("div", { class: "empty" }, "PR を選んでください。"));
    return root;
  }
  const pr = PR.get(state.pr);
  root.append(el("p", { class: "crumb" },
    el("button", { onclick: () => { state.view = "board"; render(); } }, "← 推奨マージ順"),
    "　/　", el("span", { class: "mono" }, pr.id)));

  root.append(el("div", { class: "panel" },
    el("h3", {},
      el("span", { class: "mono muted" }, prRef(pr.id)), "　",
      el("strong", {}, pr.title),
      el("span", { class: "pr-badges" },
        authorChip(pr.author, pr.author_avatar_url), ...prBadges(pr)),
      el("span", { class: "head-spacer" }),
      el("a", { class: "btn", href: pr.url, target: "_blank", rel: "noopener" }, "GitHub ↗")),
    el("div", { class: "panel-body" }, ...prDetail(state.pr))));
  return root;
}

// --- 描画 --------------------------------------------------------------

const VIEWS = { board: viewBoard, cluster: viewCluster, pr: viewPr, files: viewFiles, conflicts: viewConflicts, stacks: viewStacks, mine: viewMine, table: viewTable };

/** 全ビューに効く絞り込み。
 *
 *  以前は著者フィルタを「自分視点」の中だけに置いていたが、その値は
 *  干渉一覧・ファイル・PR一覧にも効いていた。切り替えられる場所と
 *  効く場所がずれていると、なぜ件数が減っているのか分からなくなる。
 *  効く範囲と同じだけ見える位置に置く。 */
function renderGlobalFilters() {
  const host = $("#global-filters");
  if (!host || !DATA) return;
  const authors = [...new Set(DATA.pull_requests.map((p) => p.author))].sort();
  const me = state.author && DATA.pull_requests.find((p) => p.author === state.author);

  // replaceChildren は el() と違って null を落とさず文字列 "null" にする。
  // 条件付きの要素を渡すので、ここで確実に間引く。
  host.replaceChildren(...[
    el("label", {}, "著者",
      el("select", {
        "aria-label": "著者で絞り込む",
        onchange: (e) => { state.author = e.target.value; render(); },
      },
        el("option", { value: "" }, "すべて"),
        ...authors.map((a) => el("option", { value: a, selected: a === state.author }, a)))),
    me ? authorChip(state.author, me.author_avatar_url) : null,
    state.author
      ? el("button", { onclick: () => { state.author = ""; render(); } }, "絞り込みを解除")
      : null,
    el("label", {},
      el("input", {
        type: "checkbox", checked: state.hideDraft,
        onchange: (e) => { state.hideDraft = e.target.checked; render(); },
      }), "Draft を隠す"),
    state.author || state.hideDraft
      ? el("span", { class: "filter-note" },
          "この絞り込みはすべてのタブに効いています")
      : el("span", { class: "small muted" }, "すべてのタブに効きます"),
  ].filter(Boolean));
}

function render() {
  writeHash();
  renderGlobalFilters();
  for (const b of document.querySelectorAll("#view-tabs button")) {
    // クラスタ詳細は推奨マージ順の下位ページなので、タブはそちらを選択状態にする
    const active = state.view === "cluster" ? "board" : state.view;
    b.setAttribute("aria-selected", String(b.dataset.view === active));
  }
  for (const b of document.querySelectorAll("#line-tabs button")) {
    b.setAttribute("aria-pressed", String(b.dataset.line === state.line));
  }
  const host = $("#view");
  host.replaceChildren();
  try {
    host.append((VIEWS[state.view] || VIEWS.board)());
  } catch (err) {
    host.append(el("div", { class: "banner" }, "描画エラー: " + err.message));
    console.error(err);
  }
  renderPanel();
}

function setupChrome() {
  const sel = $("#repo-select");
  sel.replaceChildren(
    ...INDEX.analyses.map((a) =>
      el("option", { value: a.repo, selected: a.repo === state.repo },
        `${a.repo}（PR ${a.prs_total}）`)),
  );
  sel.addEventListener("change", async (e) => {
    state.repo = e.target.value;
    state.line = null;
    // 対象リポジトリが変われば著者の顔ぶれも変わるので、そこだけは解除する
    state.author = "";
    await loadAnalysis();
    setupLines();
    render();
  });
  const forks = DATA.source.forks_scanned || [];
  const nameEl = $("#repo-name");
  nameEl.textContent = forks.length
    ? `fork ${forks.length} 件を追跡`
    : "fork なし";
  nameEl.title = forks.join("\n");

  setupLines();

  for (const b of document.querySelectorAll("#view-tabs button")) {
    b.addEventListener("click", () => {
      state.view = b.dataset.view;
      state.cluster = null;
      if (b.dataset.view !== "files") { state.file = null; state.fileDir = null; }
      render();
    });
  }

  $("#panel-backdrop").addEventListener("click", closePanel);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && state.pr) closePanel();
  });

  $("#theme-toggle").addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme");
    const next = cur === "dark" ? "light" : cur === "light" ? "" : "dark";
    if (next) document.documentElement.setAttribute("data-theme", next);
    else document.documentElement.removeAttribute("data-theme");
    localStorage.setItem("theme", next);
  });
  const saved = localStorage.getItem("theme");
  if (saved) document.documentElement.setAttribute("data-theme", saved);
}

function setupLines() {
  const age = (Date.now() - new Date(DATA.generated_at).getTime()) / 3600000;
  const f = $("#freshness");
  f.textContent = "最終更新 " + relativeTime(DATA.generated_at);
  f.title = DATA.generated_at;
  f.className = age > 24 ? "freshness very-stale" : age > 12 ? "freshness stale" : "freshness";

  const lt = $("#line-tabs");
  lt.replaceChildren();
  for (const l of DATA.integration_lines) {
    lt.append(el("button", {
      "data-line": l.id,
      onclick: () => { state.line = l.id; render(); },
      title: l.diverged_from
        ? `${l.diverged_from.line} と分岐（先行 ${l.diverged_from.ahead} / 後続 ${l.diverged_from.behind} コミット）`
        : "",
    }, `${l.id}（${l.pr_count}）`));
  }
  if (!state.line || !DATA.interference[state.line]) {
    state.line = DATA.integration_lines[0]?.id ?? null;
  }
}

async function loadAnalysis() {
  const entry = INDEX.analyses.find((a) => a.repo === state.repo) || INDEX.analyses[0];
  state.repo = entry.repo;
  const res = await fetch("data/" + entry.file, { cache: "no-cache" });
  if (!res.ok) throw new Error(`${entry.file}: HTTP ${res.status}`);
  DATA = await res.json();
  PR = new Map(DATA.pull_requests.map((p) => [p.id, p]));
}

async function main() {
  readHash();
  try {
    const res = await fetch(INDEX_URL, { cache: "no-cache" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    INDEX = await res.json();
    if (!INDEX.analyses || !INDEX.analyses.length) throw new Error("解析結果が空です");
    await loadAnalysis();
  } catch (err) {
    $("#load-error").append(el("div", { class: "banner" },
      `解析データを読み込めませんでした（${err.message}）。`
      + `Actions のワークフローが成功しているか確認してください。`));
    return;
  }

  if (DATA.schema_version !== 1) {
    $("#load-error").append(el("div", { class: "banner" },
      `データ形式のバージョン（${DATA.schema_version}）がビューアと一致しません。`
      + `ページを再読み込みしてください。`));
    return;
  }

  setupChrome();
  render();
}

main();
