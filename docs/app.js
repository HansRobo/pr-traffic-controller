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
  pr: null,        // サイドパネルで開いている PR
  minLevel: 2,     // グラフに出す干渉レベルの下限
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
  for (const k of ["view", "repo", "line", "author", "preset", "cluster", "pr"]) {
    if (p.get(k)) state[k] = p.get(k);
  }
  state.hideDraft = p.get("hideDraft") === "1";
  if (p.get("minLevel")) state.minLevel = Number(p.get("minLevel"));
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
  if (state.pr) p.set("pr", state.pr);
  if (state.minLevel !== 2) p.set("minLevel", String(state.minLevel));
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

  return el(
    "div",
    { class: cls },
    step !== null ? el("span", { class: "step-no" }, step) : null,
    el("span", { class: "num" }, prOpenButton(id)),
    body,
    ...prBadges(pr),
    el("span", { class: "author" }, pr.author),
  );
}

/** PR 番号のボタン。ページ遷移させず、サイドパネルで開く。 */
function prOpenButton(id, label = null) {
  return el("button", {
    class: "pr-open",
    title: "詳細をパネルで開く",
    onclick: (e) => { e.stopPropagation(); openPanel(id); },
  }, label ?? shortId(id));
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
        el("summary", {},
          `${c.id}: ${c.members.length}件 / 内部衝突 ${c.internal_pairs}ペア　`,
          el("span", {
            class: "cluster-link",
            onclick: (e) => { e.preventDefault(); e.stopPropagation(); state.view = "cluster"; state.cluster = c.id; render(); },
          }, "詳細を見る →")),
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
  const members = [...c.members].sort((a, b) => (rank.get(a) ?? 1e9) - (rank.get(b) ?? 1e9));
  const memberSet = new Set(members);
  const pairs = iv.pairs.filter(
    (x) => memberSet.has(x.a) && memberSet.has(x.b) && x.level !== undefined && x.level >= 1,
  );
  const authors = [...new Set(members.map((m) => PR.get(m)?.author).filter(Boolean))];
  const blocked = members.filter((m) => PR.get(m)?.base_conflict);

  root.append(el("div", { class: "stat-row" },
    el("div", {}, el("strong", {}, members.length), "PR"),
    el("div", {}, el("strong", {}, pairs.filter((x) => x.level >= 2).length), "衝突ペア"),
    el("div", {}, el("strong", {}, authors.length), "人の作業"),
    el("div", {}, el("strong", {}, blocked.length), "要rebase"),
  ));

  root.append(el("p", { class: "hint" },
    "このクラスタの中でだけ順序が問題になります。他のクラスタや独立PRとは"
    + "並行に流して構いません。"
    + (authors.length > 1 ? `　関係者: ${authors.join(", ")}` : "")));

  // グラフ
  root.append(el("div", { class: "panel" },
    el("h3", {}, "干渉グラフ", el("span", { class: "muted" }, "— ノードをクリックすると詳細が開き、その PR の辺だけが強調されます")),
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
    el("h3", {}, "このクラスタの推奨順", el("span", { class: "muted" }, `— ${state.preset}`)),
    el("div", { class: "panel-body" }, list)));

  // 衝突ペア一覧
  if (pairs.length) {
    const tb = el("tbody");
    for (const x of pairs.sort((m, n) => n.level - m.level)) {
      tb.append(el("tr", {},
        el("td", {}, levelChip(x.level)),
        el("td", {}, prOpenButton(x.a)),
        el("td", {}, prOpenButton(x.b)),
        el("td", { class: "small" },
          (x.conflict_files || []).map((f) => el("div", {}, el("code", {}, f.path))),
          !x.conflict_files?.length && x.overlap_files
            ? el("div", { class: "muted" }, el("code", {}, x.overlap_files.slice(0, 2).join(", ")))
            : null),
        el("td", { class: "small" },
          (x.warnings || []).map((w) => el("div", {},
            el("span", { class: "badge warn" }, w.kind === "same_function_region" ? "同一関数" : "依存/設定"),
            " ", el("code", {}, (w.symbols || [w.path]).join(", ")))))));
    }
    root.append(el("div", { class: "panel" },
      el("h3", {}, `クラスタ内の干渉（${pairs.length}ペア）`),
      el("div", { class: "table-scroll" },
        el("table", {},
          el("thead", {}, el("tr", {},
            el("th", {}, "レベル"), el("th", {}, "PR A"), el("th", {}, "PR B"),
            el("th", {}, "ファイル"), el("th", {}, "警告"))),
          tb))));
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
    prOpenButton(id),
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

  // 待つ理由がある PR は「今すぐ流せる」ではない
  for (const id of waiting.keys()) ready.delete(id);

  const box = (title, map, hint) => {
    const entries = [...map.entries()].sort(
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
  grid.append(box("今すぐ流せる", ready, "ベース衝突がなく、待つべき親も衝突相手もない"));
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
      el("td", { class: "num" }, prOpenButton(p.id)),
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
  const nodes = [...ids].sort((a, b) => (rank.get(a) ?? 1e9) - (rank.get(b) ?? 1e9));
  const idx = new Map(nodes.map((id, i) => [id, i]));

  const conflicts = iv.pairs.filter(
    (p) => p.level !== undefined && p.level >= state.minLevel
      && idx.has(p.a) && idx.has(p.b),
  );
  const stacks = [];
  for (const id of nodes) {
    for (const anc of (PR.get(id)?.stack.ancestors) || []) {
      if (idx.has(anc)) stacks.push({ from: anc, to: id });
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
      class: `edge lv${c.level}` + (touches(c.a, c.b) ? "" : " dim"),
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
      class: "edge stack" + (touches(s.from, s.to) ? "" : " dim"),
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
    el("span", {}, el("i", { style: "border-color: var(--ink-2)" }), "下側の矢印 = スタック依存（親が先。真のブロック）"),
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
    .sort((x, y) => y.level - x.level);
}

function renderPanel() {
  const backdrop = $("#panel-backdrop");
  const panel = $("#side-panel");
  if (!state.pr || !PR.has(state.pr)) {
    backdrop.removeAttribute("data-open");
    panel.removeAttribute("data-open");
    panel.setAttribute("aria-hidden", "true");
    return;
  }
  const pr = PR.get(state.pr);
  const o = DATA.orders[state.line] || {};
  const preset = (o.presets || {})[state.preset] || (o.presets || {}).balanced;
  const rank = preset ? preset.order.indexOf(pr.id) : -1;
  const metrics = (o.metrics || {})[pr.id];
  const cluster = (o.clusters || []).find((c) => c.members.includes(pr.id));
  const related = pairsFor(pr.id);

  const body = el("div", { class: "body" });

  // 位置づけ
  const stats = el("div", { class: "stat-row" });
  if (rank >= 0) stats.append(el("div", {}, el("strong", {}, rank + 1), `推奨順（${preset.order.length}件中）`));
  if (metrics) {
    stats.append(el("div", {}, el("strong", {}, metrics.blocks), "スタックでブロック"));
    stats.append(el("div", {}, el("strong", {}, related.length), "干渉する相手"));
  }
  stats.append(el("div", {}, el("strong", {}, `+${pr.additions}/-${pr.deletions}`), `${pr.changed_files_count} ファイル`));
  body.append(stats);

  // 属性
  const kv = el("dl", { class: "kv" });
  const put = (k, v) => { kv.append(el("dt", {}, k), el("dd", {}, v)); };
  put("著者", pr.author);
  put("レビュー", pr.review_decision);
  put("ブランチ", el("code", {}, `${pr.head.repo === DATA.source.repo ? "" : pr.head.repo.split("/")[0] + ":"}${pr.head.branch}`));
  put("マージ先", el("code", {}, pr.base.branch));
  if (cluster) {
    put("クラスタ", el("button", {
      class: "cluster-link",
      onclick: () => { closePanel(); state.view = "cluster"; state.cluster = cluster.id; render(); },
    }, `${cluster.id}（${cluster.members.length}件）`));
  } else {
    put("クラスタ", el("span", { class: "muted" }, "独立（並行に流せる）"));
  }
  if (pr.stack.depth > 0) {
    put("スタック", el("span", {}, ...pr.stack.ancestors.map((a, i) =>
      el("span", {}, i ? " → " : "", prOpenButton(a))), " → ", el("strong", {}, shortId(pr.id))));
  }
  if (pr.blocks.length) {
    put("これを待つPR", el("span", {}, ...pr.blocks.map((b, i) => el("span", {}, i ? " " : "", prOpenButton(b)))));
  }
  if (pr.duplicate_of) {
    put("重複", el("span", {}, ...pr.duplicate_of.map((d) => prOpenButton(d)), " と同一コミット"));
  }
  body.append(el("section", {}, el("h4", {}, "この PR について"), kv));

  // ベース衝突
  if (pr.base_conflict && pr.base_conflict_files) {
    body.append(el("section", {},
      el("h4", {}, "ベースとの衝突（まず rebase が必要）"),
      el("ul", { class: "tight small" },
        ...pr.base_conflict_files.map((f) =>
          el("li", {}, el("code", {}, f.path), " ", el("span", { class: "muted" }, `ステージ ${f.stages.join(",")}`))))));
  }

  // 干渉相手
  if (related.length) {
    const rows = el("tbody");
    for (const r of related) {
      const other = PR.get(r.other);
      rows.append(el("tr", {},
        el("td", {}, levelChip(r.level)),
        el("td", {}, prOpenButton(r.other), el("div", { class: "small muted" }, other ? other.title.slice(0, 40) : "")),
        el("td", { class: "small" },
          (r.conflict_files || []).slice(0, 3).map((f) => el("div", {}, el("code", {}, f.path.split("/").pop()))),
          (r.warnings || []).map((w) => el("div", { class: "small" },
            el("span", { class: "badge warn" }, w.kind === "same_function_region" ? "同一関数" : "依存/設定"),
            " ", el("code", {}, (w.symbols || [w.path]).join(", ")))))));
    }
    body.append(el("section", {},
      el("h4", {}, `干渉する PR（${related.length}件)`),
      el("div", { class: "table-scroll" },
        el("table", {},
          el("thead", {}, el("tr", {}, el("th", {}, "レベル"), el("th", {}, "相手"), el("th", {}, "内容"))),
          rows))));
  }

  // 変更ファイル
  if (pr.changed_files && pr.changed_files.length) {
    body.append(el("section", {},
      el("h4", {}, `変更ファイル（${pr.changed_files.length}件）`),
      el("details", {},
        el("summary", { class: "muted" }, "一覧を開く"),
        el("ul", { class: "tight small mono" },
          ...pr.changed_files.map((f) => el("li", {}, f))))));
  }

  panel.replaceChildren(
    el("header", {},
      el("div", { class: "grow" },
        el("div", { class: "mono small muted" }, pr.id),
        el("h2", {}, pr.title),
        el("div", {}, ...prBadges(pr))),
      el("a", { class: "btn", href: pr.url, target: "_blank", rel: "noopener", title: "GitHub で開く" }, "GitHub ↗"),
      el("button", { onclick: closePanel, title: "閉じる（Esc）", "aria-label": "閉じる" }, "✕")),
    body,
  );
  panel.setAttribute("data-open", "");
  panel.setAttribute("aria-hidden", "false");
  backdrop.setAttribute("data-open", "");
}

// --- 描画 --------------------------------------------------------------

const VIEWS = { board: viewBoard, cluster: viewCluster, conflicts: viewConflicts, stacks: viewStacks, mine: viewMine, table: viewTable };

function render() {
  writeHash();
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
    state.author = "";
    await loadAnalysis();
    setupLines();
    render();
  });
  $("#repo-name").textContent =
    `fork: ${DATA.source.forks_scanned.join(", ") || "なし"}`;

  setupLines();

  for (const b of document.querySelectorAll("#view-tabs button")) {
    b.addEventListener("click", () => {
      state.view = b.dataset.view;
      state.cluster = null;
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
