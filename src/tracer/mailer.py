"""SMTP requirement gap alert.

Sends a plain-text email when commits in the diff are not found in any Jira
story. Recipients = union of commit author emails + configured team list.
Never sends if there are no uncovered commits.
"""

from __future__ import annotations

import datetime
import logging
import smtplib
from dataclasses import dataclass
from email.mime.text import MIMEText

from .gap_detector import CommitCoverage
from .llm_config import LLMConfig, call_llm_api

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str
    from_addr: str


def build_gap_narrative(
    uncovered: list[CommitCoverage],
    open_stories: list[dict],
    scope_data: dict,
    project_key: str,
    llm_cfg: LLMConfig,
) -> dict:
    """Call LLM once; return {summary, unplanned_areas:[{name,detail,business_impact}]}.
    Falls back to {summary: None, unplanned_areas: []} on any error.
    """
    commit_block = "\n".join(f"- {c.message}" for c in uncovered)
    story_block = "\n".join(
        f"- {s['key']}: {(s.get('fields') or {}).get('summary', '')} "
        f"(status: {((s.get('fields') or {}).get('status') or {}).get('name', 'Unknown')})"
        for s in open_stories
    ) or "  (none)"
    screens = ", ".join(scope_data.get("affected_screens", [])) or "not specified"

    prompt = f"""You are writing a business-level gap alert for a QA manager and product owner.
Use plain English — no commit hashes, no file names, no technical jargon.

PLANNED WORK (Jira open stories — what the team approved for this sprint):
{story_block}

UNPLANNED CODE CHANGES (commits not linked to any Jira story):
{commit_block}

AFFECTED PRODUCT AREAS (screens or modules changed):
{screens}

Write a short summary (2-3 sentences) explaining how many changes were not tied to a planned
requirement, which product areas were affected, and why QA needs to pay attention.

Then identify the unplanned work areas (group related commits into logical areas, max 5 areas).
For each area:
- name: short label like "API Test Suite — Execution Engine" (not a file name)
- detail: one sentence on what changed in plain English
- business_impact: one sentence on what QA or end users might notice

Return JSON only:
{{
  "summary": "...",
  "unplanned_areas": [
    {{"name": "...", "detail": "...", "business_impact": "..."}}
  ]
}}"""
    log.debug("gap narrative: asking %s/%s about %d uncovered commit(s), %d open story/stories",
              llm_cfg.provider, llm_cfg.model, len(uncovered), len(open_stories))
    try:
        result = call_llm_api(prompt, llm_cfg)
        narrative = {
            "summary": result.get("summary"),
            "unplanned_areas": result.get("unplanned_areas") or [],
        }
        log.debug("gap narrative: %d unplanned area(s), summary=%s",
                  len(narrative["unplanned_areas"]), bool(narrative["summary"]))
        return narrative
    except Exception as e:
        # Falls back to the plain-text email body — worth knowing why.
        log.warning("gap narrative: LLM call failed (%s) — falling back to plain-text alert",
                    e, exc_info=True)
        return {"summary": None, "unplanned_areas": []}


def _render_gap_html(
    narrative: dict,
    uncovered: list[CommitCoverage],
    project_key: str,
    jira_base_url: str,
) -> str | None:
    """Render the professional HTML gap alert. Returns None if summary is absent."""
    summary = narrative.get("summary")
    if not summary:
        return None
    areas = narrative.get("unplanned_areas") or []

    try:
        today = datetime.date.today().strftime("%d %B %Y")
    except Exception:
        today = str(datetime.date.today())

    def esc(s: str) -> str:
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    area_rows = ""
    for a in areas:
        area_rows += f"""
        <tr>
          <td>
            <div class="area-name">{esc(a.get("name", ""))}</div>
            <div class="area-detail">{esc(a.get("detail", ""))}</div>
          </td>
          <td><div class="area-detail">{esc(a.get("business_impact", ""))}</div></td>
          <td style="text-align:right"><span class="status-badge">Not Planned</span></td>
        </tr>"""

    if not area_rows:
        area_rows = f"""
        <tr>
          <td colspan="3"><div class="area-detail">{len(uncovered)} commit(s) not linked to any approved story.</div></td>
        </tr>"""

    boards_url = f"{jira_base_url.rstrip('/')}/jira/software/projects/{project_key}/boards"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tracer — Gap Alert</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #e8edf2; padding: 32px 16px; font-family: -apple-system, 'Segoe UI', Arial, sans-serif; color: #1a2332; }}
  .email-outer {{ max-width: 600px; margin: 0 auto; }}
  .header {{ background: #1a2332; padding: 20px 32px; display: flex; align-items: center; justify-content: space-between; border-radius: 6px 6px 0 0; }}
  .header .brand {{ font-size: 15px; font-weight: 700; color: #ffffff; letter-spacing: .12em; text-transform: uppercase; }}
  .header .alert-type {{ font-size: 11px; font-weight: 600; color: #94a3b8; letter-spacing: .08em; text-transform: uppercase; }}
  .alert-strip {{ background: #c0392b; padding: 11px 32px; }}
  .alert-strip p {{ font-size: 12px; font-weight: 600; color: #ffffff; letter-spacing: .04em; }}
  .body {{ background: #ffffff; padding: 36px 32px; }}
  .greeting {{ font-size: 13px; color: #64748b; margin-bottom: 20px; }}
  .summary {{ font-size: 15px; line-height: 1.7; color: #1a2332; border-left: 3px solid #1a2332; padding-left: 16px; margin-bottom: 32px; }}
  .section-label {{ font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: .12em; color: #94a3b8; margin-bottom: 12px; padding-bottom: 8px; border-bottom: 1px solid #e8edf2; }}
  .gap-table {{ width: 100%; border-collapse: collapse; margin-bottom: 32px; }}
  .gap-table th {{ font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: .1em; color: #94a3b8; text-align: left; padding: 0 0 8px; }}
  .gap-table td {{ padding: 14px 0; border-bottom: 1px solid #f1f5f9; vertical-align: top; }}
  .gap-table tr:last-child td {{ border-bottom: none; }}
  .area-name {{ font-size: 13px; font-weight: 700; color: #1a2332; margin-bottom: 4px; }}
  .area-detail {{ font-size: 12px; color: #64748b; line-height: 1.6; }}
  .status-badge {{ display: inline-block; font-size: 10px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; padding: 3px 8px; border-radius: 3px; background: #fef2f2; color: #b91c1c; white-space: nowrap; }}
  .implication {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 4px; padding: 16px 18px; margin-bottom: 32px; }}
  .implication p {{ font-size: 13px; line-height: 1.7; color: #334155; }}
  .action-row {{ display: flex; align-items: center; justify-content: space-between; gap: 20px; padding-top: 4px; }}
  .action-text {{ font-size: 13px; color: #475569; line-height: 1.6; }}
  .action-text strong {{ color: #1a2332; display: block; margin-bottom: 3px; font-size: 13px; }}
  .cta {{ display: inline-block; background: #1a2332; color: #ffffff; font-size: 12px; font-weight: 700; letter-spacing: .04em; padding: 10px 22px; border-radius: 4px; text-decoration: none; white-space: nowrap; }}
  .divider {{ border: none; border-top: 1px solid #e8edf2; margin: 28px 0; }}
  .footer {{ background: #f8fafc; border-top: 1px solid #e2e8f0; border-radius: 0 0 6px 6px; padding: 16px 32px; }}
  .footer p {{ font-size: 11px; color: #94a3b8; line-height: 1.8; }}
  .footer strong {{ color: #64748b; font-weight: 600; }}
</style>
</head>
<body>
<div class="email-outer">
  <div class="header">
    <div class="brand">Tracer</div>
    <div class="alert-type">Requirement Gap Alert</div>
  </div>
  <div class="alert-strip">
    <p>{len(uncovered)} unplanned change(s) detected in {esc(project_key)} — review required before release</p>
  </div>
  <div class="body">
    <p class="greeting">Hi Team,</p>
    <div class="summary">{esc(summary)}</div>
    <div class="section-label">Unplanned Changes Detected</div>
    <table class="gap-table">
      <thead>
        <tr>
          <th style="width:55%">Area</th>
          <th style="width:30%">Business Impact</th>
          <th style="width:15%; text-align:right">Status</th>
        </tr>
      </thead>
      <tbody>{area_rows}</tbody>
    </table>
    <div class="section-label">Why This Requires Attention</div>
    <div class="implication">
      <p>Changes that are not linked to an approved requirement have no test cases assigned in the
      test management system. If these areas go untested in the current regression cycle, defects
      in these modules may reach production undetected. The team should confirm whether these
      changes were intentional and either link them to an existing story or raise a new requirement
      so QA can assign test coverage before the cycle closes.</p>
    </div>
    <hr class="divider">
    <div class="action-row">
      <div class="action-text">
        <strong>Recommended Action</strong>
        Open the Jira project and link each unplanned change to an approved story, or raise a new requirement card for QA to pick up.
      </div>
      <a class="cta" href="{boards_url}">Open Jira</a>
    </div>
  </div>
  <div class="footer">
    <p>
      Generated by <strong>Tracer</strong> &nbsp;&middot;&nbsp;
      Project: <strong>{esc(project_key)}</strong> &nbsp;&middot;&nbsp; {today}<br>
      This alert was sent to the QA manager and product owner. To suppress alerts, use <strong>--no-alert</strong>.
    </p>
  </div>
</div>
</body>
</html>"""


def send_gap_alert(
    uncovered: list[CommitCoverage],
    author_emails: list[str],
    team_emails: list[str],
    smtp_cfg: SmtpConfig,
    project_key: str,
    jira_client=None,
    scope_data: dict | None = None,
    llm_cfg: LLMConfig | None = None,
    jira_base_url: str | None = None,
) -> None:
    """Send gap alert email. No-op if uncovered is empty."""
    if not uncovered:
        log.debug("gap alert: nothing uncovered — not sending")
        return
    recipients = sorted(set(author_emails) | set(team_emails))
    if not recipients:
        log.warning("gap alert: %d uncovered commit(s) but no recipients "
                    "(no commit authors and ALERT_EMAILS empty) — not sending", len(uncovered))
        return

    html_body = None
    if jira_client is not None and llm_cfg is not None:
        try:
            open_stories = jira_client.fetch_open_stories(project_key)
        except Exception as e:
            log.warning("gap alert: could not fetch open stories (%s) — "
                        "narrative will have no story candidates", e, exc_info=True)
            open_stories = []
        narrative = build_gap_narrative(
            uncovered, open_stories, scope_data or {}, project_key, llm_cfg
        )
        html_body = _render_gap_html(
            narrative, uncovered, project_key, jira_base_url or ""
        )

    if html_body:
        msg = MIMEText(html_body, "html")
    else:
        lines = [
            f"Tracer detected {len(uncovered)} commit(s) not referenced in any "
            f"Jira story in project {project_key}.",
            "",
            "These commits were not covered by any planned requirement:",
            "",
        ]
        for c in uncovered:
            lines.append(f"  {c.hash}  {c.author_email}")
            lines.append(f'  "{c.message}"')
            lines.append("")
        lines.append("Please link these commits to Jira stories or raise a new requirement.")
        msg = MIMEText("\n".join(lines), "plain")

    msg["Subject"] = (
        f"[Tracer] Requirement gap — {len(uncovered)} unplanned commit(s) "
        f"detected in project {project_key}"
    )
    msg["From"] = smtp_cfg.from_addr
    msg["To"] = ", ".join(recipients)

    # Recipients and host are logged; the SMTP password never is.
    log.debug("gap alert: sending %s body to %d recipient(s) %s via %s:%s as %s",
              "html" if html_body else "plain", len(recipients), recipients,
              smtp_cfg.host, smtp_cfg.port, smtp_cfg.user or "<no user>")
    try:
        with smtplib.SMTP(smtp_cfg.host, smtp_cfg.port) as server:
            server.starttls()
            server.login(smtp_cfg.user, smtp_cfg.password)
            server.sendmail(smtp_cfg.from_addr, recipients, msg.as_string())
    except smtplib.SMTPException as e:
        log.debug("gap alert: SMTP send failed (%s)", e, exc_info=True)
        raise
    log.debug("gap alert: sent")
