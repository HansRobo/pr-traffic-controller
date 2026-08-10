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
  for (const k of ["view", "repo", "line", "author", "preset"]) {
    if (p.get(k)) state[k] = p.get(k);
  }
  state.hideDraft = p.get("hideDraft") === "1";
}

function writeHash() {
  const p = new URLSearchParams();
  p.set("view", state.view);
  if (state.repo) p.set("repo", state.repo);
  if (state.line) p.set("line", state.line);
  if (state.author) p.set("author", state.author);
  if (state.preset !== "balanced") p.set("preset", state.preset);
  if (state.hideDraft) p.set("hideDraft", "1");
  history.replaceState(null, "", "#" + p.toString());
}

// --- 部品 --------------------------------------------------------------

function prBadges(pr) {
  const out = [];
  if (pr.review_decision === "APPROVED") out.push(el("span", { class: "badge approved" }, "✓ Approved"));
  if (pr.review_decision === "CHANGES_REQUESTED") out.push(el("span", { class: "badge warn" }, "要修正"));
  if (pr.is_draft) out.push(el("span", { class: "badge draft" }, "Draft"));
  if (pr.base_conflict) out.push(el("span", { class: "badge rebase" }, "⚠ 要rebase"));
  if (pr.kind === "external_pr") out.push(el("span", { class: "badge fork" }, "外部fork"));
  if (pr.duplicate_of) out.push(el("span", { class: "badge warn" }, "⧉ 重複"));
  if (pr.blocks && pr.blocks.length) {
    out.push(el("span", { class: "badge blocks", title: pr.blocks.join(", ") }, `${pr.blocks.length}件をブロック`));
  }
  if (pr.stack && pr.stack.depth > 0) {
    out.push(el("span", { class: "badge", title: pr.stack.ancestors.join(" → ") }, `スタック深さ${pr.stack.depth}`));
  }
  return out;
}

function prCard(id, { step = null, note = null } = {}) {
  const pr = PR.get(id);
  if (!pr) return el("div", { class: "pr" }, id);
  const cls = ["pr", pr.is_draft ? "draft" : "", pr.base_conflict ? "base-conflict" : ""].join(" ");
  return el(
    "div",
    { class: cls },
    step !== null ? el("span", { class: "step-no" }, step) : null,
    el("span", { class: "num" }, el("a", { href: pr.url, target: "_blank", rel: "noopener" }, shortId(id))),
    el("span", { class: "pr-title" }, pr.title, note ? el("span", { class: "small muted" }, " — " + note) : null),
    ...prBadges(pr),
    el("span", { class: "author" }, pr.author),
  );
}

function levelChip(level) {
  return el("span", { class: `lv lv-${level}`, title: LEVEL_DESC[level] }, "L" + level);
}

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
  const actions = DATA.actions.filter((a) => a.line === line);
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
            ),
          ),
        );
      } else if (a.kind === "order_does_not_change_throughput") {
        box.append(
          el("div", { class: "action info" },
            el("div", { class: "big" }, a.merged),
            el("div", {},
              el("p", {}, el("strong", {}, "件はどの順序でも変わらない")),
              el("div", { class: "sub" },
                `${a.trials} 通りの順序を実際にマージして確認した。`
                + `順序が決めるのは「誰が rebase するか」であって、何件流せるかではない。`),
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
          el("p", {}, el("strong", {}, "件 — 順序によって landing 数が変わる")),
          el("div", { class: "sub" },
            `${sens.trials} 通り試行。max-landing プリセットはこの上限を狙う。`),
        ),
      ),
    );
  }

  // プリセット選択
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
      }, `${name}（${merged}件landing）`),
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
            ? "クラスタごとに厳密最適だが、これは「負担が最小」という意味であって"
              + "「landing 件数が最大」ではない（件数を優先するなら max-landing）。"
            : "")));

  // 並行に流せるもの / クラスタ
  const cols = el("div", { class: "grid cols-2" });

  const indep = o.independent.filter((id) => !state.hideDraft || !PR.get(id)?.is_draft);
  const indepPanel = el("panel", {});
  cols.append(
    el("div", { class: "panel" },
      el("h3", {}, "並行に流してよい ", el("strong", {}, String(indep.length)), " 件",
        el("span", { class: "muted" }, "— 互いに干渉しないので順不同")),
      el("div", { class: "panel-body" },
        indep.length
          ? indep.map((id) => prCard(id))
          : el("div", { class: "empty" }, "なし")),
    ),
  );

  const clusterWrap = el("div", { class: "panel" },
    el("h3", {}, "順序を議論すべき ", el("strong", {}, String(o.clusters.length)), " クラスタ",
      el("span", { class: "muted" }, "— この中だけ順序が意味を持つ")),
  );
  const cbody = el("div", { class: "panel-body" });
  if (!o.clusters.length) cbody.append(el("div", { class: "empty" }, "衝突クラスタなし"));

  const stepByPr = new Map();
  if (preset.simulation) for (const s of preset.simulation.steps) stepByPr.set(s.pr, s);

  // クラスタ内の並びは、プリセットの全体順序から絞り込んで導く。
  // preset.cluster_orders に頼ると、それを持たないプリセット
  // （max-landing は実マージで順序を作るため持たない）で並びが崩れる。
  const globalRank = new Map(preset.order.map((id, i) => [id, i]));
  for (const c of o.clusters) {
    const order = [...c.members].sort(
      (a, b) => (globalRank.get(a) ?? 1e9) - (globalRank.get(b) ?? 1e9),
    );
    const list = el("div", {});
    order.forEach((id, i) => {
      const s = stepByPr.get(id);
      let note = null;
      if (s && s.result === "conflict") {
        const files = (s.conflict_files || []).map((f) => f.path.split("/").pop());
        note = `逐次マージで衝突: ${files.slice(0, 3).join(", ")}`;
      } else if (s && s.result === "skipped") {
        note = "先に rebase が必要";
      }
      list.append(prCard(id, { step: i + 1, note }));
    });
    cbody.append(
      el("details", { open: true },
        el("summary", {}, `${c.id}: ${c.members.length}件 / 内部衝突 ${c.internal_pairs}ペア`),
        list),
    );
  }
  clusterWrap.append(cbody);
  cols.append(clusterWrap);
  root.append(cols);

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
          el("td", {}, el("a", { href: PR.get(s.pr)?.url || "#", target: "_blank" }, shortId(s.pr)),
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
    .filter((p) => !state.author || [p.a, p.b].some((x) => PR.get(x)?.author === state.author))
    .sort((a, b) => b.level - a.level || (b.conflict_files || []).length - (a.conflict_files || []).length);

  if (!rows.length) return root.append(el("div", { class: "empty" }, "該当なし")), root;

  const t = el("table");
  t.append(el("thead", {}, el("tr", {},
    el("th", {}, "レベル"), el("th", {}, "PR A"), el("th", {}, "PR B"),
    el("th", {}, "衝突/重複ファイル"), el("th", {}, "警告"))));
  const tb = el("tbody");
  for (const p of rows) {
    const files = p.conflict_files && p.conflict_files.length
      ? p.conflict_files.map((f) =>
          el("div", {}, el("code", {}, f.path),
            f.structural ? el("span", { class: "badge warn", title: `ステージ ${f.stages.join(",")}` }, "構造") : null))
      : (p.overlap_files || []).slice(0, 4).map((f) => el("div", { class: "muted" }, el("code", {}, f)));
    const warns = (p.warnings || []).map((w) =>
      el("div", { class: "small" },
        el("span", { class: "badge warn" }, w.kind === "same_function_region" ? "同一関数" : "依存/設定"),
        " ", w.symbols ? el("code", {}, w.symbols.join(", ")) : el("code", {}, w.path)));
    tb.append(el("tr", {},
      el("td", {}, levelChip(p.level)),
      el("td", {}, prLink(p.a)),
      el("td", {}, prLink(p.b)),
      el("td", { class: "small" }, files),
      el("td", {}, warns)));
  }
  t.append(tb);
  root.append(el("div", { class: "table-scroll" }, t));
  return root;
}

function prLink(id) {
  const pr = PR.get(id);
  if (!pr) return el("span", {}, id);
  return el("span", {},
    el("a", { href: pr.url, target: "_blank", rel: "noopener", class: "mono" }, shortId(id)),
    pr.is_draft ? el("span", { class: "badge draft" }, "D") : null,
    pr.review_decision === "APPROVED" ? el("span", { class: "badge approved" }, "✓") : null,
    el("div", { class: "small muted" }, pr.title.slice(0, 46)));
}

// --- ビュー: スタック --------------------------------------------------

function viewStacks() {
  const root = el("div");
  root.append(el("p", { class: "hint" },
    "スタックした PR は、親がマージされるまで流せない（ハード制約）。"
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
      chain.append(el("a", {
        class: "chain-node" + (pr?.kind === "external_pr" ? " external" : ""),
        href: pr?.url || "#", target: "_blank", rel: "noopener",
        title: pr?.title || id,
      }, pr ? `${pr.repo.split("/")[0]}#${pr.number}` : id));
      prevRepo = pr?.repo || prevRepo;
    });
    box.append(chain);

    if (repos.length > 1) {
      box.append(el("div", { class: "panel-body small muted" },
        el("strong", {}, "運用上の注意: "),
        "フォーク側リポジトリに対して開かれた PR は、上流リポジトリへ"
        + "直接マージできない。この鎖を流すには、上流から順にマージしたうえで、"
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
        el("span", { class: "badge warn" }, w.kind === "duplicate_pr_head" ? "⧉ 重複" : "親PR不在"),
        el("span", { class: "pr-title" }, w.detail),
        el("span", { class: "mono small" }, w.subjects.map(shortId).join(" "))));
    }
    root.append(el("div", { class: "panel" },
      el("h3", {}, "片付け候補", el("span", { class: "muted" }, "— 順序問題ではなく掃除タスク")), body));
  }
  return root;
}

// --- ビュー: 自分視点 --------------------------------------------------

function viewMine() {
  const root = el("div");
  const authors = [...new Set(DATA.pull_requests.map((p) => p.author))].sort();

  const sel = el("select", { onchange: (e) => { state.author = e.target.value; render(); } },
    el("option", { value: "" }, "— 著者を選択 —"),
    ...authors.map((a) => el("option", { value: a, selected: a === state.author }, a)));
  root.append(el("div", { class: "filters" }, el("label", {}, "著者", sel)));

  if (!state.author) {
    root.append(el("div", { class: "empty" }, "著者を選ぶと、自分に関係する部分だけが表示されます。"));
    return root;
  }

  const mine = DATA.pull_requests.filter((p) => p.author === state.author && p.line === state.line);
  const iv = DATA.interference[state.line];
  const o = DATA.orders[state.line];
  const preset = o.presets[state.preset] || o.presets.balanced;
  const rank = new Map(preset.order.map((id, i) => [id, i]));

  const ready = [], waiting = [], blocking = [], todo = [];

  for (const p of mine) {
    const unmergedAncestors = p.stack.ancestors.filter((a) => PR.has(a));
    if (p.base_conflict) todo.push([p.id, "ベースと衝突している。rebase が必要"]);
    if (p.is_draft) todo.push([p.id, "Draft のまま"]);
    if (p.review_decision === "REVIEW_REQUIRED") todo.push([p.id, "レビュー未実施"]);
    if (p.review_decision === "CHANGES_REQUESTED") todo.push([p.id, "修正要求に対応が必要"]);
    if (p.duplicate_of) todo.push([p.id, `${p.duplicate_of.map(shortId).join(",")} と同一コミット。どちらかをクローズ`]);

    if (unmergedAncestors.length) {
      waiting.push([p.id, `${unmergedAncestors.map(shortId).join(" → ")} が先にマージされる必要がある`]);
    } else if (!p.base_conflict) {
      ready.push([p.id, null]);
    }
    if (p.blocks.length) {
      const who = [...new Set(p.blocks.map((b) => PR.get(b)?.author).filter(Boolean))];
      blocking.push([p.id, `${p.blocks.length}件（${who.join(", ")}）がこの PR を待っている`]);
    }
  }

  // 衝突相手のうち、自分が先に推奨されているもの
  for (const pair of iv.pairs) {
    if (pair.level === undefined || pair.level < 2) continue;
    const [a, b] = [pair.a, pair.b];
    const mineSide = PR.get(a)?.author === state.author ? a : PR.get(b)?.author === state.author ? b : null;
    if (!mineSide) continue;
    const other = mineSide === a ? b : a;
    if (PR.get(other)?.author === state.author) continue;
    const label = `${shortId(other)}（${PR.get(other)?.author}）と L${pair.level} 衝突`;
    if ((rank.get(mineSide) ?? 0) < (rank.get(other) ?? 0)) blocking.push([mineSide, label + " — あなたが先の推奨"]);
    else waiting.push([mineSide, label + " — 相手が先の推奨"]);
  }

  const box = (title, items, hint) =>
    el("div", { class: "panel" },
      el("h3", {}, title, el("span", { class: "muted" }, `— ${items.length}件`)),
      el("div", { class: "panel-body" },
        hint ? el("p", { class: "hint" }, hint) : null,
        items.length
          ? items.map(([id, note]) => prCard(id, { note }))
          : el("div", { class: "empty" }, "なし")));

  const grid = el("div", { class: "grid cols-2" });
  grid.append(box("今すぐ流せる", ready, "ベース衝突がなく、待つべき親もない"));
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
    .filter((p) => !state.hideDraft || !p.is_draft)
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
      el("td", { class: "num" }, el("a", { href: p.url, target: "_blank", rel: "noopener" }, shortId(p.id))),
      el("td", {}, p.title, " ", ...prBadges(p)),
      el("td", {}, p.author),
      el("td", { class: "small" }, p.review_decision),
      el("td", { class: "num" }, `+${p.additions}/-${p.deletions}`),
      el("td", { class: "num" }, metrics[p.id]?.blocks ?? 0),
      el("td", { class: "num" }, (metrics[p.id]?.regret ?? 0).toFixed(1))));
  }
  t.append(tb);
  root.append(el("div", { class: "table-scroll" }, t));
  return root;
}

// --- 描画 --------------------------------------------------------------

const VIEWS = { board: viewBoard, conflicts: viewConflicts, stacks: viewStacks, mine: viewMine, table: viewTable };

function render() {
  writeHash();
  for (const b of document.querySelectorAll("#view-tabs button")) {
    b.setAttribute("aria-selected", String(b.dataset.view === state.view));
  }
  for (const b of document.querySelectorAll("#line-tabs button")) {
    b.setAttribute("aria-pressed", String(b.dataset.line === state.line));
  }
  const host = $("#view");
  host.replaceChildren();
  try {
    host.append(VIEWS[state.view]());
  } catch (err) {
    host.append(el("div", { class: "banner" }, "描画エラー: " + err.message));
    console.error(err);
  }
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
    state.author = "";
    await loadAnalysis();
    setupLines();
    render();
  });
  $("#repo-name").textContent =
    `fork: ${DATA.source.forks_scanned.join(", ") || "なし"}`;

  setupLines();

  for (const b of document.querySelectorAll("#view-tabs button")) {
    b.addEventListener("click", () => { state.view = b.dataset.view; render(); });
  }

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
