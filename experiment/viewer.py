"""Build an HTML page that shows each saved report with the judge's findings highlighted.

Usage (from this folder):
  .venv/bin/python viewer.py                      # Claude Opus 5.5 reports -> results/anthropic_reports.html
  .venv/bin/python viewer.py --provider openai    # GPT-6.1 Sol reports    -> results/openai_reports.html

Highlights in each report:
  red     a statement the judge flagged as unsupported or altered (fidelity), with the reason on hover
  amber   a placeholder left for the radiologist to complete ("[specify segment]")
The side panel lists every judge answer that was not the "good" answer, with its evidence quote,
including omissions (completeness) that cannot be highlighted because the text is missing.
"""

from __future__ import annotations

import argparse
import html
import json
import re
from collections import Counter

import experiment as e

FID_LABELS = {
    "fid_other_findings": "Finding outside the nodule (not supplied)",
    "fid_global_normal": "Rest-of-study-is-normal statement (not supplied)",
    "fid_nodule_details": "Nodule detail not supplied",
    "fid_technique": "Technique detail not supplied",
    "fid_clinical_history": "Clinical history stated as fact (not supplied)",
    "fid_altered_facts": "Supplied fact changed",
    "fid_wrong_guideline": "Wrong guideline",
    "fid_unsupported_recommendation": "Unsupported recommendation",
}

OTHER_LABELS = {
    "org_title": "No title", "org_clinical_information": "No clinical information section",
    "org_technique": "No technique section", "org_comparison": "No comparison section",
    "org_findings": "No findings section", "org_impression": "No impression section",
    "org_recommendation": "No recommendation section", "org_order": "Sections out of order",
    "comp_age_sex": "Age or sex missing", "comp_smoking": "Smoking history missing",
    "comp_indication": "Indication missing", "comp_comparison_status": "Prior-CT status missing",
    "comp_size": "Size missing", "comp_attenuation": "Attenuation missing", "comp_number": "Number missing",
    "comp_location": "Location missing", "comp_margins": "Margins missing",
    "comp_risk_category": "Risk category not stated", "comp_followup_correct": "Follow-up not per Fleischner 2017",
    "comp_guideline_cited": "Guideline not named",
}
LABELS = {**FID_LABELS, **OTHER_LABELS}

S_OPEN, S_MID, S_CLOSE = "\x01", "\x02", "\x03"  # sentinels survive escaping and markdown


def locate(text: str, quote: str) -> tuple[int, int] | None:
    """Find a judge quote in the report, tolerating markdown and whitespace differences."""
    quote = quote.strip().strip('"“”')
    if len(quote) < 3:
        return None
    i = text.find(quote)
    if i >= 0:
        return i, i + len(quote)
    # Allow **, *, _, # and any whitespace between the quote's words.
    words = [re.escape(w) for w in re.findall(r"\S+", quote.replace("**", ""))]
    if not words:
        return None
    pattern = r"[\s*_#]*".join(words)
    m = re.search(pattern, text, re.IGNORECASE)
    return (m.start(), m.end()) if m else None


def mark_spans(text: str, spans: list[tuple[int, int, str, str]]) -> str:
    """Insert sentinel markers for non-overlapping spans (earliest, longest first)."""
    spans = sorted(spans, key=lambda s: (s[0], -(s[1] - s[0])))
    kept, end = [], -1
    for s in spans:
        if s[0] >= end:
            kept.append(s)
            end = s[1]
    out, pos = [], 0
    for start, stop, cls, tip in kept:
        out.append(text[pos:start])
        # A highlight never crosses a line break, so it can't straddle paragraphs or list items.
        out.append("\n".join(f"{S_OPEN}{cls}|{tip}{S_MID}{seg}{S_CLOSE}" if seg.strip() else seg
                             for seg in text[start:stop].split("\n")))
        pos = stop
    out.append(text[pos:])
    return "".join(out)


def render_markdown(text: str) -> str:
    """Small markdown subset: headings, bullets, numbered items, bold, italics, rules."""
    lines_out, in_list = [], None
    for raw in text.splitlines():
        line = html.escape(raw, quote=False)
        line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
        line = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", line)
        stripped = line.strip()
        bullet = re.match(r"^\s*[-*•●]\s+(.*)", line)
        number = re.match(r"^\s*\d+[.)]\s+(.*)", line)
        kind = "ul" if bullet else "ol" if number else None
        if in_list and kind != in_list:
            lines_out.append(f"</{in_list}>")
            in_list = None
        if kind:
            if not in_list:
                lines_out.append(f"<{kind}>")
                in_list = kind
            lines_out.append(f"<li>{(bullet or number).group(1)}</li>")
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)", stripped)
        if heading:
            level = min(len(heading.group(1)) + 2, 6)
            lines_out.append(f"<h{level}>{heading.group(2)}</h{level}>")
        elif re.fullmatch(r"-{3,}|\*{3,}|_{3,}", stripped):
            lines_out.append("<hr>")
        elif stripped:
            lines_out.append(f"<p>{line}</p>")
    if in_list:
        lines_out.append(f"</{in_list}>")
    body = "\n".join(lines_out)

    def to_mark(m):
        cls, tip = m.group(1).split("|", 1)
        return f'<mark class="{cls}" title="{tip}">'
    body = re.sub(f"{S_OPEN}(.*?){S_MID}", to_mark, body)
    return body.replace(S_CLOSE, "</mark>")


def build(provider: str) -> str:
    label = e.MODELS[provider]["label"]
    reports = {r["run_index"]: r for r in e.read_jsonl(e.raw_path(provider)) if not r.get("error")}
    judged = {r["run_index"]: r for r in e.read_jsonl(e.judge_path(provider)) if not r.get("error")}
    ids = sorted(reports)

    flag_counts, cards, nav = Counter(), [], []
    totals = {"org": [], "comp": [], "fid": []}
    for i in ids:
        text = reports[i]["text"]
        v = judged.get(i, {}).get("verdicts")
        spans, issues = [], []
        if v:
            sc = e.judge_scores(v)
            totals["org"].append(sc["organization"])
            totals["comp"].append(sc["completeness"])
            totals["fid"].append(sc["fidelity"])
            for k, (metric, question, good) in e.JUDGE_CATEGORIES.items():
                ans, ev = v[k]["answer"], v[k]["evidence"]
                if metric == "info" or ans == good:
                    continue
                flag_counts[k] += 1
                found = locate(text, ev) if ev else None
                if metric == "fidelity" and found:
                    spans.append((found[0], found[1], "bad", FID_LABELS.get(k, k)))
                issues.append((metric, k, question, ev, bool(found)))
        for m in re.finditer(r"\[[^\]]{3,}\]", text):
            spans.append((m.start(), m.end(), "ph", "Placeholder for the radiologist to complete"))
        body = render_markdown(mark_spans(text, spans))

        if v:
            faithful = sc["fidelity"] == 1.0
            score_html = (f'<span class="pill">Organization {sc["organization"]:.2f}</span>'
                          f'<span class="pill">Completeness {sc["completeness"]:.2f}</span>'
                          f'<span class="pill {"ok" if faithful else "no"}">{"Faithful" if faithful else "Not faithful"}</span>')
        else:
            score_html = '<span class="pill">Not judged yet</span>'
        issue_rows = []
        for metric, k, q, ev, found in sorted(issues, key=lambda x: ["fidelity", "completeness", "organization"].index(x[0])):
            tag = {"fidelity": "Unsupported", "completeness": "Missing", "organization": "Structure"}[metric]
            evid = f'<q>{html.escape(ev)}</q>' if ev else '<span class="muted">(absent from report)</span>'
            loc = ' <span class="muted">· highlighted</span>' if found and metric == "fidelity" else ""
            issue_rows.append(f'<li class="{metric}"><span class="tag">{tag}</span> <b>{html.escape(LABELS.get(k, q))}</b>'
                              f'<div>{evid}{loc}</div></li>')
        issues_html = "<ul class='issues'>" + "".join(issue_rows) + "</ul>" if issue_rows else "<p class='muted'>No issues flagged.</p>"
        meta = f'{reports[i].get("output_tokens", "?")} output tokens · {len(text.split())} words'
        nav.append(f'<a href="#run-{i}" class="{"nav-no" if v and not faithful else ""}">{i}</a>')
        cards.append(f"""
<section class="card" id="run-{i}">
  <header><h2>Run {i}</h2><div class="pills">{score_html}</div><div class="meta">{meta}</div></header>
  <div class="cols">
    <article class="report">{body}</article>
    <aside><h3>Judge findings</h3>{issues_html}</aside>
  </div>
</section>""")

    n = len(totals["fid"])
    summary = ""
    if n:
        metrics = [
            ("organization", "Organization", f"{sum(totals['org']) / n:.2f}", "mean score"),
            ("completeness", "Completeness", f"{sum(totals['comp']) / n:.2f}", "mean score"),
            ("fidelity", "Fidelity", f"{sum(totals['fid']) / n:.0%}", "faithful reports"),
        ]
        panels = []
        for metric, title, value, unit in metrics:
            keys = [k for k, (m, _, _) in e.JUDGE_CATEGORIES.items() if m == metric]
            # Top 3 rubric items by how often they failed (ties keep rubric order).
            top3 = sorted(keys, key=lambda k: -flag_counts[k])[:3]
            rows = "".join(
                f'<li><span class="bar" style="--w:{flag_counts[k] / n * 100:.0f}%"></span>'
                f'<b>{flag_counts[k]}/{n}</b> {html.escape(LABELS.get(k, k))}</li>'
                for k in top3)
            panels.append(f"""
    <div class="panel {metric}">
      <div class="ph-head"><span class="ph-title">{title}</span><span class="ph-value">{value}</span><span class="ph-unit">{unit}</span></div>
      <div class="ph-sub">Top 3 of {len(keys)} rubric items, by how often they failed</div>
      <ul class="freq">{rows}</ul>
    </div>""")
        summary = f"""
<div class="summary">
  <h3>Judge results across {n} reports</h3>
  <div class="panels">{''.join(panels)}
  </div>
</div>"""

    return TEMPLATE.format(label=html.escape(label), prompt=html.escape(e.PROMPT), n=len(ids),
                           judge=html.escape(e.judge_settings()["model"]), summary=summary,
                           nav="".join(nav), cards="".join(cards))


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{label} Reports</title>
<style>
  :root {{
    --bg: #f6f7f9; --card: #fff; --ink: #1d2733; --muted: #66717d; --line: #dde2e7;
    --bad-bg: #fde4e4; --bad: #a3221a; --ph-bg: #fdf0d8; --ph: #8a4b00;
    --ok-bg: #e2f4ea; --ok: #0b6e44; --accent: #5b3fd1;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --bg: #14171b; --card: #1d2228; --ink: #e6e9ed; --muted: #9aa4ae; --line: #323a43;
      --bad-bg: #4a1f1f; --bad: #ffb4ab; --ph-bg: #45351a; --ph: #f5c77a;
      --ok-bg: #18382a; --ok: #8fe0b6; --accent: #b4a3ff;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.5 -apple-system, "Helvetica Neue", Arial, sans-serif; }}
  .wrap {{ max-width: 1180px; margin: 0 auto; padding: 28px 16px 60px; }}
  h1 {{ font-size: 26px; margin: 0 0 4px; }}
  .sub {{ color: var(--muted); margin: 0 0 16px; }}
  .prompt {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; font-style: italic; }}
  .legend {{ display: flex; flex-wrap: wrap; gap: 16px; margin: 14px 0; font-size: 13.5px; color: var(--muted); }}
  .summary {{ background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 16px 18px; margin: 16px 0; }}
  .stats {{ display: flex; flex-wrap: wrap; gap: 28px; margin-bottom: 8px; }}
  .stats div {{ color: var(--muted); font-size: 13px; }}
  .stats span {{ display: block; font-size: 28px; font-weight: 700; color: var(--ink); }}
  .summary h3 {{ font-size: 14px; margin: 0 0 10px; }}
  .panels {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }}
  .panel {{ border: 1px solid var(--line); border-radius: 10px; padding: 12px; }}
  .ph-head {{ display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }}
  .ph-title {{ font-weight: 700; font-size: 15px; }}
  .ph-value {{ font-size: 26px; font-weight: 700; margin-left: auto; }}
  .ph-unit {{ font-size: 12px; color: var(--muted); }}
  .ph-sub {{ font-size: 12px; color: var(--muted); margin: 2px 0 8px; }}
  @media (max-width: 760px) {{ .panels {{ grid-template-columns: 1fr; }} }}
  .freq {{ list-style: none; padding: 0; margin: 0; font-size: 14px; }}
  .freq li {{ position: relative; isolation: isolate; padding: 4px 8px; margin: 3px 0; border-radius: 6px; overflow: hidden; }}
  .freq .bar {{ position: absolute; inset: 0 auto 0 0; width: var(--w); background: var(--bad-bg); z-index: -1; }}
  .freq b {{ margin-right: 6px; }}
  nav {{ position: sticky; top: 0; background: var(--bg); padding: 8px 0; display: flex; flex-wrap: wrap; gap: 6px; z-index: 5; }}
  nav a {{ text-decoration: none; color: var(--ink); border: 1px solid var(--line); background: var(--card); border-radius: 6px; padding: 2px 9px; font-size: 13px; }}
  nav a.nav-no {{ border-color: var(--bad); color: var(--bad); }}
  .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 12px; margin: 16px 0; overflow: hidden; }}
  .card header {{ display: flex; flex-wrap: wrap; align-items: center; gap: 10px; padding: 12px 16px; border-bottom: 1px solid var(--line); }}
  .card h2 {{ font-size: 18px; margin: 0; }}
  .pills {{ display: flex; gap: 6px; flex-wrap: wrap; }}
  .pill {{ font-size: 12.5px; font-weight: 600; border: 1px solid var(--line); border-radius: 99px; padding: 1px 9px; }}
  .pill.ok {{ background: var(--ok-bg); color: var(--ok); border-color: transparent; }}
  .pill.no {{ background: var(--bad-bg); color: var(--bad); border-color: transparent; }}
  .meta {{ margin-left: auto; color: var(--muted); font-size: 12.5px; }}
  .cols {{ display: grid; grid-template-columns: minmax(0, 1.6fr) minmax(0, 1fr); }}
  .report {{ padding: 14px 18px; font-family: Georgia, "Times New Roman", serif; font-size: 15px; border-right: 1px solid var(--line); }}
  .report h3, .report h4, .report h5, .report h6 {{ font-family: -apple-system, "Helvetica Neue", Arial, sans-serif; margin: 12px 0 4px; font-size: 15px; }}
  .report p {{ margin: 4px 0; }}
  .report ul, .report ol {{ margin: 4px 0; padding-left: 22px; }}
  .report hr {{ border: none; border-top: 1px solid var(--line); margin: 10px 0; }}
  mark {{ border-radius: 3px; padding: 0 2px; cursor: help; }}
  mark.bad {{ background: var(--bad-bg); color: var(--bad); box-shadow: inset 0 -2px 0 var(--bad); }}
  mark.ph {{ background: var(--ph-bg); color: var(--ph); }}
  aside {{ padding: 14px 16px; font-size: 14px; }}
  aside h3 {{ font-size: 13px; text-transform: uppercase; letter-spacing: .6px; color: var(--muted); margin: 0 0 8px; }}
  .issues {{ list-style: none; padding: 0; margin: 0; }}
  .issues li {{ padding: 7px 0; border-top: 1px solid var(--line); }}
  .issues li:first-child {{ border-top: none; }}
  .issues q {{ font-family: Georgia, serif; color: var(--muted); }}
  .tag {{ font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .5px; padding: 1px 6px; border-radius: 4px; }}
  .fidelity .tag {{ background: var(--bad-bg); color: var(--bad); }}
  .completeness .tag {{ background: var(--ph-bg); color: var(--ph); }}
  .organization .tag {{ background: var(--line); color: var(--ink); }}
  .muted {{ color: var(--muted); }}
  .swatch {{ display: inline-block; width: 14px; height: 14px; border-radius: 3px; vertical-align: -2px; margin-right: 5px; }}
  @media (max-width: 760px) {{
    .cols {{ grid-template-columns: 1fr; }}
    .report {{ border-right: none; border-bottom: 1px solid var(--line); }}
    .meta {{ margin-left: 0; width: 100%; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <h1>{label}: base-prompt reports</h1>
  <p class="sub">{n} reports from the same prompt. Judged by {judge}, blinded to the source model.</p>
  <div class="prompt">“{prompt}”</div>
  <div class="legend">
    <span><span class="swatch" style="background:var(--bad-bg);box-shadow:inset 0 -2px 0 var(--bad)"></span>Flagged by the judge as not supplied or changed (hover for the reason)</span>
    <span><span class="swatch" style="background:var(--ph-bg)"></span>Placeholder for the radiologist</span>
  </div>
  {summary}
  <nav>{nav}</nav>
  {cards}
</div>
</body>
</html>
"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="anthropic", choices=list(e.MODELS))
    args = ap.parse_args()
    out = e.OUT_DIR / f"{args.provider}_reports.html"
    out.write_text(build(args.provider))
    print(f"Wrote {out.relative_to(e.HERE)}")
