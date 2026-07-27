"""LLM-assisted HTML QA report generator.

Builds the deterministic skeleton (summary cards, feature cards, TC tables,
defect rows) from the run's JSON data, then calls Groq to fill in per-screen
QA Notes, a release summary, and blind spots. Raises RuntimeError if the
Groq call fails — the caller (cli.py) exits non-zero.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_MODEL = "llama-3.3-70b-versatile"

_RISK_ORDER = {"HIGH": 2, "MEDIUM": 1, "MED": 1, "LOW": 0}
_PRIORITY_TO_RISK = {"High": "HIGH", "Medium": "MED", "Low": "LOW"}


def _call_groq(prompt: str, api_key: str) -> dict:
    """Call Groq chat completions in JSON mode. Raises RuntimeError on any failure."""
    body = json.dumps({
        "model": _MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0.3,
        "max_tokens": 2048,
    }).encode()
    req = urllib.request.Request(_GROQ_URL, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "python-groq/0.9.0")
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as e:
        raise RuntimeError(f"Groq LLM call failed: {e}") from e


def _build_prompt(scope: dict, jira_stories_raw: list[dict], defects_raw: list[dict], tcm_raw: list[dict]) -> str:
    stories_summary = [
        f"  - {s['key']}: {s['fields'].get('summary', '')} (priority: {(s['fields'].get('priority') or {}).get('name', 'Unknown')})"
        for s in jira_stories_raw
    ]
    defect_summary = [
        f"  - {d['key']}: {d['fields'].get('summary', '')} (status: {(d['fields'].get('status') or {}).get('name', 'Unknown')})"
        for d in defects_raw
    ]
    tc_summary = [
        f"  - {tc.get('uniqueTestcaseId', '')}: {_cv(tc.get('customFieldValues') or [], 'TEST_CASE_TITLE')} (story: {tc.get('jiraStoryKey', 'NA')})"
        for tc in tcm_raw[:30]
    ]
    symbols = scope.get("changed_symbols", [])
    high_syms = [s["name"] for s in symbols if s.get("risk") == "HIGH"]
    med_syms = [s["name"] for s in symbols if s.get("risk") in ("MEDIUM", "MED")]
    screens = scope.get("affected_screens", [])

    return f"""You are a QA lead writing a regression test report. Analyze this change set and return a JSON object.

CHANGE SET:
- Base branch: {scope.get('base', 'unknown')}
- Target branch: {scope.get('target', 'unknown')}
- Commits: {len(scope.get('commits', []))}
- Affected screens: {', '.join(screens) or 'none'}
- HIGH risk symbols ({len(high_syms)}): {', '.join(high_syms[:10]) or 'none'}
- MEDIUM risk symbols ({len(med_syms)}): {', '.join(med_syms[:10]) or 'none'}
- Total changed symbols: {len(symbols)}

JIRA STORIES IN SCOPE:
{chr(10).join(stories_summary) or '  (none)'}

OPEN DEFECTS LINKED TO STORIES:
{chr(10).join(defect_summary) or '  (none)'}

TEST CASES TO RUN ({len(tcm_raw)} total):
{chr(10).join(tc_summary) or '  (none)'}

Return a JSON object with exactly these keys:
{{
  "release_summary": "<2-3 sentence overview of what QA needs to focus on for this release>",
  "qa_notes": [
    {{
      "screen": "<screen name from the affected screens list above>",
      "notes": "<what QA should focus on for this screen, 1-2 sentences>",
      "risks": "<specific risks or edge cases to watch for, 1 sentence>"
    }}
  ],
  "blind_spots": [
    "<area or scenario not covered by the listed test cases, 1 sentence each>"
  ]
}}

Include one qa_notes entry per affected screen. Include 2-4 blind_spots."""


def _cv(custom_fields: list[dict], name: str) -> str:
    for f in custom_fields:
        if f.get("fieldDefinition", {}).get("name") == name:
            return f.get("value") or ""
    return ""


def _tc_title(tc: dict) -> str:
    return _cv(tc.get("customFieldValues") or [], "TEST_CASE_TITLE") or tc.get("title", "")


def _story_risk(story: dict) -> str:
    priority = (story.get("fields", {}).get("priority") or {}).get("name", "")
    return _PRIORITY_TO_RISK.get(priority, "LOW")


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _render_html(
    scope: dict,
    jira_stories_raw: list[dict],
    defects_raw: list[dict],
    tcm_raw: list[dict],
    llm: dict,
) -> str:
    import datetime
    generated_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    base = _esc(scope.get("base", ""))
    target = _esc(scope.get("target", ""))
    commit_count = len(scope.get("commits", []))
    screens = scope.get("affected_screens", [])
    symbols = scope.get("changed_symbols", [])

    # Stats
    high_count = sum(1 for s in jira_stories_raw if _story_risk(s) == "HIGH")
    med_count = sum(1 for s in jira_stories_raw if _story_risk(s) == "MED")
    low_count = sum(1 for s in jira_stories_raw if _story_risk(s) == "LOW")
    if not jira_stories_raw:
        max_sym_risk = max((_RISK_ORDER.get(s.get("risk", "LOW"), 0) for s in symbols), default=0)
        if max_sym_risk >= 2:
            high_count = len(screens)
        elif max_sym_risk >= 1:
            med_count = len(screens)
        else:
            low_count = len(screens)

    tc_count = len(tcm_raw)
    defect_count = len(defects_raw)

    # Defect lookup by story key (via issuelinks on defect)
    defects_by_story: dict[str, list[dict]] = {}
    for d in defects_raw:
        for link in (d.get("fields", {}) or {}).get("issuelinks", []):
            for direction in ("inwardIssue", "outwardIssue"):
                linked = link.get(direction)
                if linked and linked.get("fields", {}).get("issuetype", {}).get("name") == "Story":
                    defects_by_story.setdefault(linked["key"], []).append(d)

    # TCs by story key
    tcs_by_story: dict[str, list[dict]] = {}
    for tc in tcm_raw:
        key = tc.get("jiraStoryKey", "NA")
        if key and key != "NA":
            tcs_by_story.setdefault(key, []).append(tc)

    # QA notes lookup by screen name
    qa_notes_by_screen: dict[str, dict] = {
        n["screen"]: n for n in (llm.get("qa_notes") or [])
    }

    # Sort stories by risk desc
    sorted_stories = sorted(
        jira_stories_raw,
        key=lambda s: -_RISK_ORDER.get(_story_risk(s), 0),
    )

    # Build feature cards
    feature_cards = ""
    for story in sorted_stories:
        key = story["key"]
        fields = story.get("fields", {})
        raw_summary = fields.get("summary", key)
        summary = _esc(raw_summary)
        risk = _story_risk(story)
        risk_css = {"HIGH": "high", "MED": "med", "LOW": "low"}.get(risk, "low")
        risk_label = "MED" if risk == "MED" else risk

        qa_note = qa_notes_by_screen.get(raw_summary, {})
        notes_html = _esc(qa_note.get("notes", "")) if qa_note else ""
        risks_html = _esc(qa_note.get("risks", "")) if qa_note else ""

        story_defects = defects_by_story.get(key, [])
        if story_defects:
            defect_rows = "".join(
                f'<div class="bug-row">'
                f'<span class="bug-status open">OPEN</span>'
                f'<span class="jira-link">{_esc(d["key"])}</span> — {_esc((d.get("fields", {}).get("summary") or ""))}'
                f'</div>'
                for d in story_defects
            )
        else:
            defect_rows = '<div class="no-bugs">No open defects linked</div>'

        story_tcs = tcs_by_story.get(key, [])
        if story_tcs:
            tc_rows = "".join(
                f'<tr>'
                f'<td><span class="tc-id">{_esc(tc.get("uniqueTestcaseId", ""))}</span></td>'
                f'<td class="tc-name">{_esc(_tc_title(tc))}</td>'
                f'<td><span class="exec-badge unexec">{_esc(tc.get("testcaseStatus", ""))}</span></td>'
                f'</tr>'
                for tc in story_tcs
            )
            tc_section = f'''<div class="tc-section">
        <div class="info-label" style="margin-bottom:.4rem">Test Cases — SDET360.ai TCM</div>
        <table class="tc-table">
          <tr><th>TC ID</th><th>Test Case Name</th><th>Status</th></tr>
          {tc_rows}
        </table>
      </div>'''
        else:
            tc_section = '<div class="tc-section"><div class="no-bugs">No test cases linked to this story</div></div>'

        why_html = ""
        if notes_html:
            why_html = f'<div class="info-row"><div class="info-label">QA Notes</div><div class="info-val">{notes_html}</div></div>'
            if risks_html:
                why_html += f'<div class="info-row"><div class="info-label">Risks</div><div class="info-val">{risks_html}</div></div>'

        feature_cards += f'''
  <div class="feature-card {risk_css}">
    <div class="fc-head">
      <span class="risk-badge {risk_label}">{risk_label}</span>
      <span class="screen-name">{summary}</span>
      <span class="conf" style="margin-left:auto">{_esc(key)}</span>
    </div>
    <div class="fc-body">
      <div>
        {why_html}
        <div class="info-row">
          <div class="info-label">Jira Story</div>
          <div class="info-val"><span class="jira-link">{_esc(key)}</span> — {summary}</div>
        </div>
      </div>
      <div>
        <div class="info-row">
          <div class="info-label">Open Defects</div>
          {defect_rows}
        </div>
      </div>
      {tc_section}
    </div>
  </div>'''

    all_tc_pills = "".join(
        f'<span class="tc-pill">{_esc(tc.get("uniqueTestcaseId", ""))}</span>'
        for tc in tcm_raw
    )

    blind_items = "".join(
        f'<div class="blind-card"><div class="bc-name">&#9888; Blind Spot</div>'
        f'<div class="bc-detail">{_esc(b)}</div></div>'
        for b in (llm.get("blind_spots") or [])
    )

    story_pills = "".join(
        f'<span class="story-pill"><span class="sp-key">{_esc(s["key"])}</span>'
        f'<span class="sp-title">{_esc((s.get("fields", {}).get("summary") or "")[:60])}</span></span>'
        for s in jira_stories_raw
    )

    defect_alert = ""
    if defects_raw:
        defect_alert = f'''<div class="alert">
    <div class="icon">&#9888;</div>
    <div>
      <div class="title">Open Defects — {defect_count} defect(s) linked to stories in scope</div>
      <div class="detail">Re-run affected scenarios before marking regression complete.</div>
    </div>
  </div>'''

    release_summary = _esc(llm.get("release_summary", ""))

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>QA Regression Report — SDET360.ai</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font: 13px/1.5 -apple-system, 'Segoe UI', sans-serif; background: #f1f5f9; color: #1e293b; }}
  .topbar {{ background: #0f172a; color: #f8fafc; display: flex; align-items: center; justify-content: space-between; padding: .6rem 1.5rem; }}
  .topbar .logo {{ font-weight: 700; font-size: 1rem; letter-spacing: .03em; color: #38bdf8; }}
  .topbar .meta {{ font-size: .75rem; color: #94a3b8; }}
  .page {{ max-width: 1100px; margin: 0 auto; padding: 1.2rem 1rem 3rem; }}
  .summary-row {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: .7rem; margin-bottom: 1.2rem; }}
  .scard {{ background: #fff; border-radius: 8px; padding: .8rem 1rem; border: 1px solid #e2e8f0; }}
  .scard .val {{ font-size: 1.6rem; font-weight: 700; line-height: 1; }}
  .scard .lbl {{ font-size: .72rem; color: #64748b; margin-top: .2rem; }}
  .scard.high .val {{ color: #dc2626; }} .scard.med .val {{ color: #ea580c; }} .scard.low .val {{ color: #16a34a; }}
  .scard.tc .val {{ color: #2563eb; }} .scard.bug .val {{ color: #7c3aed; }}
  .alert {{ background: #fef2f2; border: 2px solid #dc2626; border-radius: 8px; padding: .7rem 1rem; margin-bottom: 1rem; display: flex; align-items: flex-start; gap: .6rem; }}
  .alert .icon {{ font-size: 1.1rem; flex-shrink: 0; margin-top: .05rem; }}
  .alert .title {{ font-weight: 700; color: #dc2626; font-size: .85rem; }}
  .alert .detail {{ font-size: .78rem; color: #7f1d1d; margin-top: .1rem; }}
  .release-summary {{ background: #f0f9ff; border: 1px solid #bae6fd; border-radius: 8px; padding: .8rem 1rem; margin-bottom: 1rem; font-size: .82rem; color: #0c4a6e; }}
  .sec-header {{ display: flex; align-items: center; gap: .6rem; margin: 1.2rem 0 .6rem; }}
  .sec-header h2 {{ font-size: .9rem; font-weight: 700; color: #334155; }}
  .sec-header .count {{ background: #e2e8f0; color: #475569; font-size: .72rem; padding: .1rem .45rem; border-radius: 9px; font-weight: 600; }}
  .feature-card {{ background: #fff; border-radius: 8px; border: 1px solid #e2e8f0; margin-bottom: .8rem; overflow: hidden; }}
  .feature-card.high {{ border-left: 4px solid #dc2626; }} .feature-card.med {{ border-left: 4px solid #ea580c; }} .feature-card.low {{ border-left: 4px solid #16a34a; }}
  .fc-head {{ display: flex; align-items: center; gap: .6rem; padding: .7rem 1rem; border-bottom: 1px solid #f1f5f9; background: #fafafa; }}
  .risk-badge {{ font-size: .7rem; font-weight: 700; padding: .15rem .5rem; border-radius: 4px; letter-spacing: .04em; }}
  .risk-badge.HIGH {{ background: #fee2e2; color: #991b1b; }} .risk-badge.MED {{ background: #ffedd5; color: #9a3412; }} .risk-badge.LOW {{ background: #dcfce7; color: #166534; }}
  .fc-head .screen-name {{ font-weight: 700; font-size: .9rem; }}
  .fc-head .conf {{ font-size: .72rem; color: #94a3b8; margin-left: auto; }}
  .fc-body {{ padding: .7rem 1rem; display: grid; grid-template-columns: 1fr 1fr; gap: .8rem; }}
  .info-row {{ margin-bottom: .5rem; }}
  .info-label {{ font-size: .68rem; font-weight: 700; text-transform: uppercase; letter-spacing: .06em; color: #94a3b8; margin-bottom: .2rem; }}
  .info-val {{ font-size: .78rem; color: #334155; }}
  .jira-link {{ color: #2563eb; font-size: .78rem; font-weight: 500; }}
  .bug-row {{ display: flex; align-items: center; gap: .4rem; font-size: .78rem; margin-bottom: .25rem; }}
  .bug-status {{ font-size: .68rem; font-weight: 600; padding: .1rem .4rem; border-radius: 3px; }}
  .bug-status.open {{ background: #fee2e2; color: #991b1b; }}
  .no-bugs {{ font-size: .78rem; color: #16a34a; }}
  .tc-section {{ grid-column: 1 / -1; border-top: 1px dashed #e2e8f0; padding-top: .6rem; margin-top: .2rem; }}
  .tc-table {{ width: 100%; border-collapse: collapse; font-size: .78rem; }}
  .tc-table th {{ font-size: .68rem; font-weight: 700; text-transform: uppercase; letter-spacing: .05em; color: #94a3b8; text-align: left; padding: .25rem .4rem; border-bottom: 1px solid #f1f5f9; }}
  .tc-table td {{ padding: .3rem .4rem; border-bottom: 1px solid #f8fafc; }}
  .tc-id {{ font-family: monospace; font-weight: 700; color: #2563eb; font-size: .8rem; }}
  .exec-badge {{ font-size: .68rem; font-weight: 600; padding: .1rem .4rem; border-radius: 3px; }}
  .exec-badge.unexec {{ background: #f1f5f9; color: #475569; }}
  .tc-name {{ color: #334155; }}
  .checklist-grid {{ display: flex; flex-wrap: wrap; gap: .4rem; margin-top: .4rem; }}
  .tc-pill {{ background: #eff6ff; color: #1d4ed8; font-family: monospace; font-size: .78rem; font-weight: 600; padding: .25rem .55rem; border-radius: 5px; border: 1px solid #bfdbfe; }}
  .blind-card {{ background: #fff7ed; border: 1px solid #fed7aa; border-radius: 8px; padding: .8rem 1rem; margin-bottom: .5rem; }}
  .blind-card .bc-name {{ font-weight: 600; color: #9a3412; font-size: .85rem; }}
  .blind-card .bc-detail {{ font-size: .78rem; color: #7c2d12; margin-top: .15rem; }}
  .story-pill {{ display: inline-flex; align-items: center; gap: .4rem; background: #fff; border: 1px solid #dbeafe; border-radius: 6px; padding: .3rem .6rem; margin: .25rem; font-size: .78rem; }}
  .story-pill .sp-key {{ font-weight: 700; color: #1d4ed8; }}
  .story-pill .sp-title {{ color: #475569; }}
  .report-footer {{ margin-top: 2rem; padding-top: 1rem; border-top: 1px solid #e2e8f0; font-size: .72rem; color: #94a3b8; display: flex; justify-content: space-between; }}
</style>
</head>
<body>

<div class="topbar">
  <div class="logo">SDET360.ai &nbsp;&middot;&nbsp; <span style="color:#94a3b8;font-weight:400">QA Regression Report</span></div>
  <div style="display:flex;align-items:center;gap:1rem">
    <span style="background:#1e40af;color:#bfdbfe;font-size:.72rem;padding:.15rem .5rem;border-radius:9px">{target}</span>
    <span class="meta">Base: {base} &nbsp;&middot;&nbsp; {commit_count} commit(s) &nbsp;&middot;&nbsp; Generated: {generated_at}</span>
  </div>
</div>

<div class="page">

  <div class="summary-row">
    <div class="scard high"><div class="val">{high_count}</div><div class="lbl">HIGH risk stories</div></div>
    <div class="scard med"><div class="val">{med_count}</div><div class="lbl">MEDIUM risk stories</div></div>
    <div class="scard low"><div class="val">{low_count}</div><div class="lbl">LOW risk stories</div></div>
    <div class="scard tc"><div class="val">{tc_count}</div><div class="lbl">Test cases to run</div></div>
    <div class="scard bug"><div class="val">{defect_count}</div><div class="lbl">Open defects in scope</div></div>
  </div>

  {f'<div class="release-summary">{release_summary}</div>' if release_summary else ''}

  {defect_alert}

  <div class="sec-header">
    <h2>Regression Scope</h2>
    <span class="count">{len(jira_stories_raw)} stories &middot; ordered by risk</span>
  </div>

  {feature_cards}

  <div class="sec-header"><h2>Master Test Case Checklist</h2><span class="count">{tc_count} total</span></div>
  <div class="checklist-grid">
    {all_tc_pills}
  </div>

  <div class="sec-header"><h2>Blind Spots</h2></div>
  {blind_items if blind_items else '<div class="blind-card"><div class="bc-detail">No blind spots identified.</div></div>'}

  <div class="sec-header"><h2>Jira Stories in Scope</h2></div>
  <div>{story_pills}</div>

  <div class="report-footer">
    <span>Generated by Tracer &middot; {generated_at}</span>
    <span>{base} &rarr; {target} &middot; {len(symbols)} changed symbols &middot; {commit_count} commits</span>
  </div>
</div>
</body>
</html>'''


def generate(
    run_dir: str,
    scope: dict,
    coverages: list,
    jira_stories_raw: list[dict],
    defects_raw: list[dict],
    tcm_raw: list[dict],
    groq_api_key: str,
) -> None:
    """Generate report.html in run_dir. Raises RuntimeError if Groq call fails."""
    prompt = _build_prompt(scope, jira_stories_raw, defects_raw, tcm_raw)
    llm_data = _call_groq(prompt, groq_api_key)
    html = _render_html(scope, jira_stories_raw, defects_raw, tcm_raw, llm_data)
    Path(run_dir, "report.html").write_text(html, encoding="utf-8")
