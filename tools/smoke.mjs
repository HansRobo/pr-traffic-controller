// 最小の DOM シムで、全ビューが実データに対して例外を出さないか確認する。
import fs from "node:fs";

class N {
  constructor(tag){this.tag=tag;this.children=[];this.attrs={};this.text="";}
  append(...k){for(const x of k.flat()){if(x==null||x===false)continue;this.children.push(x);}}
  replaceChildren(){this.children=[];}
  setAttribute(k,v){this.attrs[k]=v;}
  getAttribute(k){return this.attrs[k]??null;}
  addEventListener(){}
  querySelector(){return new N("div");}
  querySelectorAll(){return [];}
  get textContent(){return this.text;}
  set textContent(v){this.text=v;}
  set className(v){this.attrs.class=v;}
  set innerHTML(v){this.text=v;}
  count(){return 1+this.children.reduce((a,c)=>a+(c.count?c.count():0),0);}
}
const doc = {
  createElement:(t)=>new N(t),
  createElementNS:(ns,t)=>new N(t),
  createTextNode:(t)=>{const n=new N("#text");n.text=t;return n;},
  querySelector:()=>new N("div"),
  querySelectorAll:()=>[],
  documentElement:new N("html"),
  getElementById:()=>null,
  addEventListener(){},
};
globalThis.document = doc;
globalThis.Node = N;
globalThis.location = { hash: "" };
globalThis.history = { replaceState(){} };
globalThis.localStorage = { getItem:()=>null, setItem(){} };
globalThis.fetch = async () => ({ ok:true, json: async()=>JSON.parse(fs.readFileSync("docs/data/latest.json","utf8")) });

let src = fs.readFileSync("docs/app.js","utf8").replace(/^main\(\);$/m,"");
src += `
export { state, VIEWS, PR, main as _main };
export function _init(d){ DATA = d; PR = new Map(d.pull_requests.map(p=>[p.id,p])); }
`;
const tmp="/tmp/_pr_traffic_controller_app.mjs";
fs.writeFileSync(tmp, src);
const m = await import(tmp);

const idx = "docs/data/index.json";
if (!fs.existsSync(idx)) { console.log("解析結果が無いのでスキップ"); process.exit(0); }
const index = JSON.parse(fs.readFileSync(idx,"utf8"));
if (!index.analyses?.length) { console.log("解析結果が空なのでスキップ"); process.exit(0); }

let fail=0;
let data;
for (const entry of index.analyses) {
data = JSON.parse(fs.readFileSync("docs/data/" + entry.file,"utf8"));
m._init(data);
for (const line of Object.keys(data.interference)) {
  m.state.line = line;
  for (const preset of Object.keys(data.orders[line].presets)) {
    m.state.preset = preset;
    for (const [name, fn] of Object.entries(m.VIEWS)) {
      for (const author of ["", ...[...new Set(data.pull_requests.map(p=>p.author))].slice(0,2)]) {
      for (const hideDraft of [false, true]) {
        m.state.author = author;
        m.state.hideDraft = hideDraft;
        // クラスタ詳細は各クラスタを、他は 1 回ずつ
        const clusters = name === "cluster"
          ? [...(data.orders[line].clusters||[]).map(c=>c.id), "存在しないID"]
          : [null];
        for (const cid of clusters) {
          m.state.cluster = cid;
          for (const lv of (name === "cluster" ? [1,2,3] : [2])) {
            m.state.minLevel = lv;
            // PR 選択あり/なしの両方（グラフの強調とパネル）
            for (const sel of [null, data.pull_requests.find(p=>p.line===line)?.id ?? null]) {
              m.state.pr = sel;
              // グラフの場所絞り込みも一度は通す
              const anyFile = (data.interference[line].pairs.find(p=>p.files?.length)?.files||[])[0];
              m.state.graphScope = (cid && anyFile) ? { kind:"file", value:anyFile.path } : null;
              try { const n = fn(); if(!n) throw new Error("null 返却"); }
              catch(e){ fail++; console.log(`  ✗ ${line}/${preset}/${name}/c=${cid}/lv=${lv}: ${e.message}`); }
            }
          }
        }
      }
      }
    }
    m.state.cluster = null; m.state.pr = null; m.state.minLevel = 2; m.state.hideDraft = false; m.state.graphScope = null;
  }
}
console.log(`  ${entry.repo}: ${Object.keys(data.interference).length} ライン × ${Object.keys(data.orders[Object.keys(data.orders)[0]].presets).length} プリセット を検証`);
}
console.log(fail ? `${fail} 件失敗` : `全 ${index.analyses.length} 解析 × 全ビュー × 全ライン × 全プリセット × 著者フィルタ: 例外なし`);
// ノード数のサンプル
m.state.line=Object.keys(data.interference)[0]; m.state.preset="balanced"; m.state.author="";
for (const [n,fn] of Object.entries(m.VIEWS)) console.log(`  ${n}: ${fn().count()} ノード`);
