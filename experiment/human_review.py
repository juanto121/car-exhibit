"""Build the radiologist scoring app (one self-contained HTML page) from the saved reports.

Usage (from this folder):
  .venv/bin/python human_review.py

Writes:
  human_review/index.html          the app, with every saved report embedded (published as an Artifact)
  results/human_review_key.csv     which model wrote each blinded report ID. Keep this file to yourself:
                                   the app and its CSV show only the blinded IDs (R01, R02, ...).

Report order is shuffled with a fixed seed, so rebuilding with the same reports gives the same IDs.
Adding reports later (the remaining 90 per model) changes the shuffle, so finish one review round
per build, or keep the earlier key file with its CSV.
"""

from __future__ import annotations

import csv
import json
import random

import experiment as e
from viewer import render_markdown

SEED = 2027
APP_DIR = e.HERE / "human_review"

# The rubric shown to the radiologist: the same 28 items the judge answers, written so that
# 5 always means "fully meets" (fidelity items are phrased as "free of ...").
RUBRIC = [
    ("Organization", "Required sections, in order", [
        ("org_title", "Title naming the examination"),
        ("org_clinical_information", "Clinical information / indication section"),
        ("org_technique", "Technique section"),
        ("org_comparison", "Comparison section (prior studies available or not)"),
        ("org_findings", "Findings section"),
        ("org_impression", "Impression section"),
        ("org_recommendation", "Recommendation section"),
        ("org_order", "Sections in conventional order"),
    ]),
    ("Completeness", "No omitted findings or descriptors", [
        ("comp_age_sex", "States age (70) and sex (male)"),
        ("comp_smoking", "States never-smoker status"),
        ("comp_indication", "States the indication (chronic cough)"),
        ("comp_comparison_status", "States that there is no prior CT"),
        ("comp_size", "Nodule size: 7 mm"),
        ("comp_attenuation", "Attenuation: solid"),
        ("comp_number", "Number: single"),
        ("comp_location", "Location: right lower lobe"),
        ("comp_margins", "Margins: smooth"),
        ("comp_risk_category", "States the patient's risk category"),
        ("comp_followup_correct", "Follow-up matches Fleischner 2017 (CT at 6–12 months, then consider 18–24 months)"),
        ("comp_guideline_cited", "Names the guideline"),
    ]),
    ("Fidelity", "No fabricated or altered content", [
        ("fid_other_findings", "Free of findings about other structures (pleura, nodes, airways, heart…)"),
        ("fid_global_normal", "Free of “rest of the study is normal” statements"),
        ("fid_nodule_details", "Free of nodule details not supplied (calcification, fat, segment…)"),
        ("fid_technique", "Free of technique details not supplied (contrast, reconstructions…)"),
        ("fid_clinical_history", "Free of clinical history stated as fact"),
        ("fid_altered_facts", "Free of changes to supplied facts"),
        ("fid_wrong_guideline", "Uses the right guideline (not Lung-RADS)"),
        ("fid_unsupported_recommendation", "Free of unsupported recommendations"),
    ]),
]


def main() -> None:
    reports = []
    for p in e.MODELS:
        for r in e.read_jsonl(e.raw_path(p)):
            if not r.get("error") and r.get("text"):
                reports.append({"provider": p, "run_index": r["run_index"], "text": r["text"]})
    reports.sort(key=lambda r: (r["provider"], r["run_index"]))
    random.Random(SEED).shuffle(reports)

    APP_DIR.mkdir(exist_ok=True)
    key_rows, data = [], []
    for n, r in enumerate(reports, 1):
        rid = f"R{n:02d}" if len(reports) < 100 else f"R{n:03d}"
        key_rows.append({"report_id": rid, "provider": r["provider"], "model": e.MODELS[r["provider"]]["label"],
                         "run_index": r["run_index"]})
        data.append({"id": rid, "html": render_markdown(r["text"]), "words": len(r["text"].split())})

    with (e.OUT_DIR / "human_review_key.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(key_rows[0]))
        w.writeheader()
        w.writerows(key_rows)

    rubric = [{"name": c, "definition": d, "items": [{"id": i, "label": l} for i, l in items]} for c, d, items in RUBRIC]
    page = TEMPLATE.replace("__DATA__", json.dumps({"prompt": e.PROMPT, "reports": data, "rubric": rubric}))
    (APP_DIR / "index.html").write_text(page)
    # Standalone copy for a regular web host (GitHub Pages): same page inside a full HTML document.
    site = APP_DIR / "site"
    site.mkdir(exist_ok=True)
    (site / "index.html").write_text(
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        '<meta name="robots" content="noindex">\n'
        "<style>:root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}"
        "body{margin:0}[hidden]{display:none!important}</style>\n</head>\n<body>\n" + page + "\n</body>\n</html>\n")
    print(f"Wrote human_review/index.html ({len(data)} reports, {sum(len(c['items']) for c in rubric)} items each)")
    print("Wrote results/human_review_key.csv (blinded ID -> model). Do not send this file to the reviewer.")


TEMPLATE = r"""<title>Nodule Report Scoring</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Serif:ital,wght@0,400;0,600;1,400&family=IBM+Plex+Mono:wght@500;600&display=swap">
<style>
/* Layout: a reading desk. The report stays in view (sticky) while the scorecard beside it
   shows only what is left to score; scored items fold into compact chips. One column on a phone,
   with the report pinned in a short scrollable strip at the top. */
:root {
  --bg: #f3f5f7; --surface: #ffffff; --ink: #18222d; --muted: #5d6b79; --line: #d9e0e6;
  --accent: #1f5f8b; --accent-soft: #e3eef6; --ok: #1c7a4f; --ok-soft: #e1f3e9; --warn: #9a5b00;
  --sel: #1f5f8b; --sel-ink: #ffffff;
  --font-ui: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
  --font-report: "IBM Plex Serif", Georgia, "Times New Roman", serif;
  --font-mono: "IBM Plex Mono", ui-monospace, Menlo, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #11161b; --surface: #1a2027; --ink: #e4e9ee; --muted: #9aa7b3; --line: #2e3740;
    --accent: #7fb6dc; --accent-soft: #1d3142; --ok: #7fd1a6; --ok-soft: #173326; --warn: #f0b660;
    --sel: #7fb6dc; --sel-ink: #0f1a24; color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #11161b; --surface: #1a2027; --ink: #e4e9ee; --muted: #9aa7b3; --line: #2e3740;
  --accent: #7fb6dc; --accent-soft: #1d3142; --ok: #7fd1a6; --ok-soft: #173326; --warn: #f0b660;
  --sel: #7fb6dc; --sel-ink: #0f1a24; color-scheme: dark;
}
* { box-sizing: border-box; }
body { background: var(--bg); color: var(--ink); font: 15px/1.5 var(--font-ui); }
.app { max-width: 1320px; margin: 0 auto; padding-block: 20px 48px;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); }
h1 { font-size: 22px; margin: 0; text-wrap: balance; }
.top { display: flex; flex-wrap: wrap; gap: 12px 20px; align-items: flex-end; justify-content: space-between; }
.top p { margin: 4px 0 0; color: var(--muted); max-width: 70ch; }
.who { display: flex; gap: 8px; align-items: center; }
.who label { font-size: 13px; color: var(--muted); }
input[type=text], textarea { font: inherit; color: var(--ink); background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 7px 10px; }
input[type=text] { width: 180px; }
textarea { width: 100%; min-height: 60px; resize: vertical; }
button { font: inherit; cursor: pointer; }
.btn { border: 1px solid var(--line); background: var(--surface); color: var(--ink); border-radius: 8px; padding: 7px 12px; font-weight: 500; }
.btn.primary { background: var(--accent); border-color: var(--accent); color: var(--sel-ink); }
.btn:disabled { opacity: .45; cursor: default; }
.btn:focus-visible, .score button:focus-visible, .rlist button:focus-visible, .chip:focus-visible, .linkbtn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
kbd { font-family: var(--font-mono); font-size: 12px; border: 1px solid var(--line); border-bottom-width: 2px; border-radius: 4px; padding: 0 5px; background: var(--surface); }
.supplied { margin: 16px 0; background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; }
.supplied b { font-size: 12px; letter-spacing: .6px; text-transform: uppercase; color: var(--muted); display: block; margin-bottom: 2px; }
.supplied q { font-family: var(--font-report); }
details.help { margin: 0 0 16px; background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 10px 14px; }
details.help summary { cursor: pointer; font-weight: 600; }
details.help ul { margin: 8px 0 2px; padding-left: 20px; }
.keys { display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 13px; color: var(--muted); margin: 0 0 12px; }
.progress { display: flex; flex-wrap: wrap; gap: 10px 18px; align-items: center; margin-bottom: 12px; font-size: 14px; color: var(--muted); }
.bar { flex: 1 1 200px; height: 8px; background: var(--line); border-radius: 99px; overflow: hidden; min-width: 120px; }
.bar span { display: block; height: 100%; background: var(--ok); width: 0; transition: width .2s; }
.layout { display: grid; grid-template-columns: 150px minmax(0, 1fr); gap: 16px; align-items: start; }
.rlist { position: sticky; top: calc(env(safe-area-inset-top, 0px) + 12px); display: flex; flex-direction: column; gap: 3px; max-height: calc(100vh - 24px); overflow: auto; }
.rlist button { display: flex; justify-content: space-between; align-items: center; gap: 6px; text-align: left; border: 1px solid transparent; background: transparent; color: var(--ink); border-radius: 7px; padding: 5px 9px; font-family: var(--font-mono); font-size: 13px; }
.rlist button.on { background: var(--surface); border-color: var(--line); font-weight: 600; }
.rlist .st { font-family: var(--font-ui); font-size: 11.5px; color: var(--muted); }
.rlist .st.done { color: var(--ok); font-weight: 600; }
.desk { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px; align-items: start; }
.panel { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; min-width: 0; }
.panel header { padding: 11px 16px; border-bottom: 1px solid var(--line); display: flex; justify-content: space-between; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.panel h2 { font-size: 15px; margin: 0; }
.panel header small { color: var(--muted); font-weight: 400; font-size: 13px; }
/* The report stays in view while scoring. */
.reportPanel { position: sticky; top: calc(env(safe-area-inset-top, 0px) + 12px); max-height: calc(100vh - 24px); display: flex; flex-direction: column; }
.report { padding: 14px 18px; font-family: var(--font-report); font-size: 15px; line-height: 1.55; overflow: auto; min-height: 0; }
.report h3, .report h4, .report h5, .report h6 { font-family: var(--font-ui); font-size: 14.5px; margin: 12px 0 4px; }
.report p { margin: 4px 0; }
.report ul, .report ol { margin: 4px 0; padding-left: 22px; }
.report hr { border: none; border-top: 1px solid var(--line); margin: 10px 0; }
.card { padding: 4px 16px 16px; }
.linkbtn { border: none; background: none; color: var(--accent); padding: 0; font-size: 13px; font-weight: 500; }
.cat { margin-top: 14px; }
.cat h3 { font-size: 13.5px; margin: 0 0 4px; display: flex; justify-content: space-between; gap: 8px; }
.cat h3 span { font-weight: 400; color: var(--muted); font-size: 12.5px; }
.item { padding: 9px 10px; margin: 0 -10px; border: 1.5px solid transparent; border-radius: 9px; }
.item + .item { margin-top: 2px; }
.item.active { border-color: var(--accent); background: var(--accent-soft); }
.item .lab { font-size: 14px; margin-bottom: 6px; display: flex; justify-content: space-between; gap: 10px; cursor: pointer; }
.item .hint { font-size: 12px; color: var(--accent); white-space: nowrap; font-weight: 600; }
.score { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 4px; }
.score button { border: 1px solid var(--line); background: var(--surface); color: var(--ink); border-radius: 7px; padding: 6px 0; font-family: var(--font-mono); font-weight: 600; font-size: 14px; min-width: 0; }
.score button:hover { border-color: var(--accent); }
.score button.on { background: var(--sel); border-color: var(--sel); color: var(--sel-ink); }
.ends { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); font-size: 11px; color: var(--muted); margin-top: 3px; text-align: center; }
.chips { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 4px; }
.chip { border: 1px solid var(--line); background: var(--bg); color: var(--ink); border-radius: 99px; padding: 2px 9px; font-size: 12.5px; max-width: 100%; text-align: left; }
.chip b { font-family: var(--font-mono); color: var(--accent); margin-left: 4px; }
.alldone { margin-top: 14px; padding: 12px; border-radius: 10px; background: var(--ok-soft); color: var(--ok); font-weight: 500; }
.notes { margin-top: 14px; }
.notes label { font-size: 13px; color: var(--muted); display: block; margin-bottom: 4px; }
.nav { display: flex; gap: 8px; justify-content: space-between; margin-top: 14px; flex-wrap: wrap; }
.export { margin-top: 20px; background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; }
.export h2 { font-size: 15px; margin: 0 0 4px; }
.export p { margin: 2px 0 10px; color: var(--muted); font-size: 14px; }
.row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.msg { font-size: 13.5px; color: var(--muted); }
.msg.ok { color: var(--ok); }
.msg.warn { color: var(--warn); }
.csvbox { margin-top: 10px; font-family: var(--font-mono); font-size: 12px; min-height: 140px; }
.confirm { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 10px; padding: 8px 10px; border: 1px solid var(--warn); border-radius: 8px; }
@media (max-width: 1000px) {
  .desk { grid-template-columns: 1fr; }
  .reportPanel { max-height: 40vh; z-index: 2; box-shadow: 0 6px 14px -10px rgba(0,0,0,.4); }
}
@media (max-width: 700px) {
  .layout { grid-template-columns: 1fr; }
  .rlist { position: static; flex-direction: row; flex-wrap: wrap; max-height: none; }
  .rlist button { padding: 4px 8px; }
  .keys { display: none; }
}
@media (prefers-reduced-motion: reduce) { .bar span { transition: none; } }
</style>

<div class="app">
  <div class="top">
    <div>
      <h1>Nodule Report Scoring</h1>
      <p>Score each AI-written report against the supplied information only. Reports are blinded: the model that wrote each one is not shown.</p>
    </div>
    <div class="who">
      <label for="reviewer">Reviewer</label>
      <input type="text" id="reviewer" placeholder="Name or initials" autocomplete="off">
    </div>
  </div>

  <div class="supplied"><b>Supplied information (identical for every report)</b><q id="prompt"></q></div>

  <details class="help">
    <summary>How to score</summary>
    <ul>
      <li>Score every item from <b>1</b> (fails) to <b>5</b> (fully meets); <b>3</b> is partly met, for example a section that exists but is incomplete.</li>
      <li>Judge only against the supplied information above. The AI had no images: anything it states that is not in the supplied information, including normal findings such as “no pleural effusion”, counts as unsupported.</li>
      <li>Placeholders left for the radiologist (for example “[specify segment]”) and questions to the reader are not statements.</li>
      <li>Fidelity items are phrased so that 5 means the report is free of the problem.</li>
      <li>Scored items fold into chips; click a chip to change its score. Everything saves in this browser as you go. When you finish, download or copy the CSV and send it back.</li>
    </ul>
  </details>

  <div class="keys" aria-label="Keyboard shortcuts">
    <span><kbd>1</kbd>–<kbd>5</kbd> score the highlighted item</span>
    <span><kbd>↑</kbd><kbd>↓</kbd> move between items</span>
    <span><kbd>←</kbd><kbd>→</kbd> previous / next report</span>
    <span><kbd>Enter</kbd> next report when done</span>
    <span><kbd>Backspace</kbd> clear a score</span>
  </div>

  <div class="progress">
    <span id="progText"></span>
    <div class="bar" aria-hidden="true"><span id="progBar"></span></div>
  </div>

  <div class="layout">
    <nav class="rlist" id="rlist" aria-label="Reports"></nav>
    <div class="desk">
      <section class="panel reportPanel" aria-labelledby="rTitle">
        <header><h2 id="rTitle"></h2><small id="rWords"></small></header>
        <div class="report" id="report"></div>
      </section>
      <section class="panel" aria-label="Scorecard">
        <header><h2>Scorecard <small id="cardCount"></small></h2><button class="linkbtn" id="toggleAll" type="button"></button></header>
        <div class="card">
          <div id="card"></div>
          <div class="notes">
            <label for="note">Notes on this report (optional)</label>
            <textarea id="note" placeholder="Anything that the scores don't capture"></textarea>
          </div>
          <div class="nav">
            <button class="btn" id="prev" type="button">← Previous report</button>
            <button class="btn primary" id="next" type="button">Next report →</button>
          </div>
        </div>
      </section>
    </div>
  </div>

  <section class="export">
    <h2>Send your scores</h2>
    <p>One row per scored item (scale 1–5), with your reviewer name, the blinded report ID and your notes.</p>
    <div class="row">
      <button class="btn primary" id="dl" type="button">Download CSV</button>
      <button class="btn" id="copy" type="button">Copy CSV</button>
      <button class="btn" id="show" type="button">Show CSV</button>
      <button class="btn" id="clear" type="button">Clear all scores</button>
      <span class="msg" id="msg" role="status"></span>
    </div>
    <div class="confirm" id="confirm" hidden>
      <span>Delete every score and note saved in this browser? This can't be undone.</span>
      <button class="btn" id="clearYes" type="button">Delete all</button>
      <button class="btn" id="clearNo" type="button">Keep them</button>
    </div>
    <textarea class="csvbox" id="csvbox" readonly hidden aria-label="CSV"></textarea>
  </section>
</div>

<script>
const DATA = __DATA__;
const KEY = "nodule-human-review-v2";   // v2: 1–5 scale (v1 used 1–10)
const SCALE = 5;
const ANCHORS = ["fails", "", "partly", "", "fully meets"];
const ITEMS = DATA.rubric.flatMap(c => c.items.map(i => ({...i, category: c.name})));
const $ = id => document.getElementById(id);

let state = { reviewer: "", current: 0, scores: {}, notes: {}, times: {} };
try { const s = JSON.parse(localStorage.getItem(KEY) || "null"); if (s) state = {...state, ...s}; } catch (e) {}
function save() { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {} }

let active = null;        // item id that keys 1–5 score
let showAll = false;      // show scored items in full instead of chips
const rid = () => DATA.reports[state.current].id;
const scoresOf = id => state.scores[id] || {};
const scored = id => Object.keys(scoresOf(id)).length;
const firstUnscored = (from = -1) => {
  const s = scoresOf(rid());
  for (let k = 1; k <= ITEMS.length; k++) {
    const it = ITEMS[(from + k + ITEMS.length) % ITEMS.length];
    if (s[it.id] == null) return it.id;
  }
  return null;
};

function renderList() {
  const nav = $("rlist");
  nav.innerHTML = "";
  DATA.reports.forEach((r, i) => {
    const b = document.createElement("button");
    b.type = "button";
    const n = scored(r.id), done = n === ITEMS.length;
    b.className = i === state.current ? "on" : "";
    b.innerHTML = `<span>${r.id}</span><span class="st ${done ? "done" : ""}">${done ? "done" : n ? n + "/" + ITEMS.length : ""}</span>`;
    if (i === state.current) b.setAttribute("aria-current", "true");
    b.onclick = () => go(i);
    nav.appendChild(b);
  });
}

function renderProgress() {
  const total = DATA.reports.length * ITEMS.length;
  const n = DATA.reports.reduce((a, r) => a + scored(r.id), 0);
  const done = DATA.reports.filter(r => scored(r.id) === ITEMS.length).length;
  $("progText").textContent = `${done} of ${DATA.reports.length} reports complete · ${n} of ${total} items scored`;
  $("progBar").style.width = (total ? n / total * 100 : 0) + "%";
}

function renderReport() {
  const r = DATA.reports[state.current];
  $("rTitle").textContent = `Report ${r.id}`;
  $("rWords").textContent = `${r.words} words`;
  $("report").innerHTML = r.html;
  $("report").scrollTop = 0;
  $("note").value = state.notes[r.id] || "";
  $("prev").disabled = state.current === 0;
  $("next").disabled = state.current === DATA.reports.length - 1;
  active = firstUnscored();
  renderCard();
}

function itemRow(it, value) {
  const row = document.createElement("div");
  row.className = "item" + (it.id === active ? " active" : "");
  row.id = "it-" + it.id;
  const lab = document.createElement("div");
  lab.className = "lab";
  lab.innerHTML = `<span id="l-${it.id}"></span>${it.id === active ? '<span class="hint">press 1–5</span>' : ""}`;
  lab.firstChild.textContent = it.label;
  lab.onclick = () => { active = it.id; renderCard(); };
  row.appendChild(lab);
  const g = document.createElement("div");
  g.className = "score";
  g.setAttribute("role", "radiogroup");
  g.setAttribute("aria-labelledby", `l-${it.id}`);
  for (let k = 1; k <= SCALE; k++) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = k;
    b.className = value === k ? "on" : "";
    b.setAttribute("role", "radio");
    b.setAttribute("aria-checked", value === k ? "true" : "false");
    b.onclick = () => setScore(it.id, k);
    g.appendChild(b);
  }
  row.appendChild(g);
  row.insertAdjacentHTML("beforeend", `<div class="ends">${ANCHORS.map(a => `<span>${a}</span>`).join("")}</div>`);
  return row;
}

function renderCard() {
  const s = scoresOf(rid());
  const n = scored(rid());
  $("cardCount").textContent = `${n} of ${ITEMS.length} scored`;
  $("toggleAll").textContent = showAll ? "Fold scored items" : `Show scored items (${n})`;
  $("toggleAll").hidden = n === 0;
  const card = $("card");
  card.innerHTML = "";
  DATA.rubric.forEach(c => {
    const open = c.items.filter(it => showAll || s[it.id] == null || it.id === active);
    const done = c.items.filter(it => !showAll && s[it.id] != null && it.id !== active);
    const sec = document.createElement("div");
    sec.className = "cat";
    const left = c.items.filter(it => s[it.id] == null).length;
    sec.innerHTML = `<h3>${c.name}<span>${left ? left + " left · " : ""}${c.definition}</span></h3>`;
    open.forEach(it => sec.appendChild(itemRow(it, s[it.id])));
    if (done.length) {
      const chips = document.createElement("div");
      chips.className = "chips";
      done.forEach(it => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "chip";
        b.title = "Change this score";
        b.textContent = it.label;
        b.insertAdjacentHTML("beforeend", `<b>${s[it.id]}</b>`);
        b.onclick = () => { active = it.id; renderCard(); };
        chips.appendChild(b);
      });
      sec.appendChild(chips);
    }
    card.appendChild(sec);
  });
  if (n === ITEMS.length && !active) {
    card.insertAdjacentHTML("beforeend", `<div class="alldone">All ${ITEMS.length} items scored.${state.current < DATA.reports.length - 1 ? " Press Enter or Next report to continue." : " This was the last report: send your scores below."}</div>`);
  }
  const a = active && $("it-" + active);
  if (a) a.scrollIntoView({ block: "nearest" });
}

function setScore(item, k) {
  const id = rid();
  state.scores[id] = state.scores[id] || {};
  state.times[id] = state.times[id] || {};
  state.scores[id][item] = k;
  state.times[id][item] = new Date().toISOString();
  save();
  // Move to the next item that still needs a score.
  active = firstUnscored(ITEMS.findIndex(it => it.id === item));
  renderCard(); renderList(); renderProgress();
}

function clearScore(item) {
  const id = rid();
  if (state.scores[id]) delete state.scores[id][item];
  save(); renderCard(); renderList(); renderProgress();
}

function moveActive(step) {
  // Move among the items that are shown in full (unscored ones, or all when expanded).
  const s = scoresOf(rid());
  const shown = ITEMS.filter(it => showAll || s[it.id] == null || it.id === active).map(it => it.id);
  if (!shown.length) return;
  const i = shown.indexOf(active);
  active = shown[(i < 0 ? 0 : i + step + shown.length) % shown.length];
  renderCard();
}

function go(i) {
  if (i < 0 || i >= DATA.reports.length) return;
  state.current = i;
  showAll = false;
  save(); renderList(); renderReport(); renderProgress();
}

document.addEventListener("keydown", ev => {
  const t = ev.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA")) return;  // typing a name or a note
  if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
  if (/^[1-5]$/.test(ev.key) && active) { setScore(active, Number(ev.key)); ev.preventDefault(); }
  else if (ev.key === "ArrowDown") { moveActive(1); ev.preventDefault(); }
  else if (ev.key === "ArrowUp") { moveActive(-1); ev.preventDefault(); }
  else if (ev.key === "ArrowRight") { go(state.current + 1); ev.preventDefault(); }
  else if (ev.key === "ArrowLeft") { go(state.current - 1); ev.preventDefault(); }
  else if (ev.key === "Enter" && scored(rid()) === ITEMS.length && !(t && t.tagName === "BUTTON")) { go(state.current + 1); ev.preventDefault(); }
  else if (ev.key === "Backspace" && active) { clearScore(active); ev.preventDefault(); }
});

function csv() {
  const esc = v => { const s = String(v ?? ""); return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; };
  const rows = [["reviewer", "report_id", "category", "item_id", "item", "score", "scale_max", "scored_at", "report_note"]];
  DATA.reports.forEach(r => {
    const s = scoresOf(r.id), t = state.times[r.id] || {};
    ITEMS.forEach(it => {
      if (s[it.id] != null) rows.push([state.reviewer, r.id, it.category, it.id, it.label, s[it.id], SCALE, t[it.id] || "", state.notes[r.id] || ""]);
    });
  });
  return rows.map(r => r.map(esc).join(",")).join("\n") + "\n";
}

function msg(t, cls) { $("msg").textContent = t; $("msg").className = "msg " + (cls || ""); }
function fname() {
  const who = (state.reviewer || "reviewer").replace(/[^A-Za-z0-9_-]+/g, "_");
  return `nodule_scores_${who}_${new Date().toISOString().slice(0, 10)}.csv`;
}

// Inside a Claude artifact the download goes through the viewer's `downloads` capability;
// on a regular website (GitHub Pages) a plain browser download is used instead.
let downloads = null;
const inArtifact = !!window.claude?.use;
(async () => {
  if (!inArtifact) return;
  try { downloads = await window.claude.use("downloads"); } catch (e) { downloads = null; }
  if (!downloads) { $("dl").hidden = true; msg("Use Copy CSV to send your scores.", ""); }
})();

function browserDownload() {
  const url = URL.createObjectURL(new Blob([csv()], { type: "text/csv;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url; a.download = fname();
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  msg("CSV downloaded.", "ok");
}

$("dl").onclick = async () => {
  if (!inArtifact) { browserDownload(); return; }
  if (!downloads) return;
  try {
    const r = await downloads.save({ filename: fname(), data: csv() });
    msg(r.status === "saved" ? "CSV saved." : "CSV sent.", "ok");
  } catch (e) {
    if (e && e.code === "declined") msg("Download cancelled.", "");
    else { msg("Download isn't available here. Use Copy CSV instead.", "warn"); if (["unavailable", "not_granted", "capability_disabled", "capability_removed"].includes(e?.code)) $("dl").hidden = true; }
  }
};
$("copy").onclick = async () => {
  const text = csv();
  try { await navigator.clipboard.writeText(text); msg("CSV copied. Paste it into an email or a .csv file.", "ok"); }
  catch (e) { $("csvbox").hidden = false; $("csvbox").value = text; $("csvbox").select(); msg("Copy isn't allowed here: the CSV is selected below, press Ctrl/Cmd+C.", "warn"); }
};
$("show").onclick = () => { const box = $("csvbox"); box.hidden = !box.hidden; box.value = csv(); $("show").textContent = box.hidden ? "Show CSV" : "Hide CSV"; };
$("clear").onclick = () => { $("confirm").hidden = false; };
$("clearNo").onclick = () => { $("confirm").hidden = true; };
$("clearYes").onclick = () => {
  state = { reviewer: state.reviewer, current: 0, scores: {}, notes: {}, times: {} };
  save(); $("confirm").hidden = true; msg("All scores deleted.", ""); go(0);
};
$("toggleAll").onclick = () => { showAll = !showAll; renderCard(); };
$("reviewer").value = state.reviewer;
$("reviewer").oninput = e => { state.reviewer = e.target.value.trim(); save(); };
$("note").oninput = e => { state.notes[rid()] = e.target.value; save(); };
$("prev").onclick = () => go(state.current - 1);
$("next").onclick = () => go(state.current + 1);
$("prompt").textContent = DATA.prompt;

if (state.current >= DATA.reports.length) state.current = 0;
renderList(); renderReport(); renderProgress();
</script>
"""

if __name__ == "__main__":
    main()
