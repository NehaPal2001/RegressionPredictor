"""Test Plan email — the single consolidated message sent per run.

Formerly one of two emails (the other being the standalone requirement-gap
alert); the gap findings are now folded in here as a "Requirement Gap Analysis"
section so the run dispatches exactly one email. The LLM gap narrative is passed
in via ``gap_narrative`` (see ``mailer.build_gap_narrative``); everything else in
this email is computed here, in plain Python, from data the run already produced:

===========================  ============================================
Section                      Source
===========================  ============================================
Release Summary              scope.json (base/target refs, files, screens, commits)
Features To Be Tested        jira_stories.json
Defect Retest Scope          defects.json
Test Approach / Automation   selected test cases, counted by AUTOMATION_STATUS
Coverage Risks & Gaps        uncovered commits (+ open stories, when supplied)
Test Environment             loaded config (TCM project key, TCM/Jira base URLs)
Entry / Exit Criteria        fixed policy text — config, never generated
Test Case Summary            selected test case count + priority breakdown
===========================  ============================================

Every number and list in the sections above is a direct count over the real data
structures, so those sections cannot state anything the run did not actually
produce. The one exception is the Requirement Gap Analysis section, whose prose
comes from the LLM gap narrative — it is omitted entirely when no narrative is
supplied, and the factual Coverage Risks table still lists the uncovered commits
either way.

The markup follows transactional-email rules rather than web-page rules —
table-based layout, inline styles on everything load-bearing, web-safe fonts,
explicit cell background colours, max-width 640px single column. Outlook does not
implement flexbox or grid, and several clients drop or invert unstyled areas
under dark mode.

The narrative is produced by ``mailer.build_gap_narrative`` and passed in by the
caller; ``mailer.send_gap_alert`` (the old standalone gap email) is no longer
invoked by the predict flow.
"""

from __future__ import annotations

import html
import logging
import smtplib
from collections import Counter
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from .tcm_client import BASE_URL as TCM_BASE_URL

log = logging.getLogger(__name__)

# Organisational policy, not something a model can know about this team. Override
# per-project with TEST_PLAN_ENTRY_CRITERIA / TEST_PLAN_EXIT_CRITERIA in .env
# (pipe-separated, one criterion per segment).
DEFAULT_ENTRY_CRITERIA: tuple[str, ...] = (
    "All in-scope Jira stories are development-complete and moved to Ready for QA.",
    "Build is deployed to the QA environment and the smoke suite passes.",
    "Test data and environment access are available to the QA team.",
    "No open blocker defects against the screens in scope.",
)

DEFAULT_EXIT_CRITERIA: tuple[str, ...] = (
    "All planned test cases executed, with results recorded in TCM.",
    "No open Critical or High severity defects in the areas covered by this plan.",
    "Medium and Low defects triaged, and either fixed or deferred with sign-off.",
    "Regression pass green for every affected screen listed above.",
    "Test summary shared with the release stakeholders.",
)

# ── palette (inlined per element; no external stylesheet survives email) ──────
_INK = "#1a2332"
_MUTED = "#5b6b7f"
_LINE = "#dfe4ea"
_CARD_BG = "#ffffff"
_PAGE_BG = "#eef1f5"
_HEADER_BG = "#1f3a5f"
_ACCENT = "#2f6fb0"
_WARN = "#b0480d"

_FONT = "Arial, Helvetica, sans-serif"


def _esc(value) -> str:
    """Escape any value for safe inclusion in the email body."""
    return html.escape(str(value if value is not None else ""))


# ── data extraction — every figure below is a count over real structures ─────


def _issue_fields(issue: dict) -> tuple[str, str, str, str]:
    """(key, summary, status, priority) from a raw Jira issue dict.

    Also accepts the flattened ``{"key","summary","status","priority"}`` shape so
    the caller can hand over either jira_stories.json's raw dicts or the
    summarised copies inside test_cases.json.
    """
    key = issue.get("key", "")
    fields = issue.get("fields")
    if isinstance(fields, dict):
        return (
            key,
            fields.get("summary", "") or "",
            (fields.get("status") or {}).get("name", "") or "",
            (fields.get("priority") or {}).get("name", "") or "",
        )
    return (key, issue.get("summary", "") or "",
            issue.get("status", "") or "", issue.get("priority", "") or "")


def _automation_split(test_cases: list[dict]) -> list[tuple[str, int]]:
    """Count selected test cases by automation status, busiest bucket first."""
    counts = Counter(
        (tc.get("automation_status") or "Unspecified").strip() or "Unspecified"
        for tc in test_cases
    )
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def _priority_split(test_cases: list[dict]) -> list[tuple[str, int]]:
    """Count by priority — empty when no test case carries a priority value."""
    counts = Counter(
        (tc.get("priority") or "").strip()
        for tc in test_cases
        if (tc.get("priority") or "").strip()
    )
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def _manual_vs_automated(split: list[tuple[str, int]]) -> tuple[int, int]:
    """(manual, automated) using the same rule as the Stage 3 release command."""
    manual = sum(n for status, n in split if status.strip().lower() == "can not be automated")
    return manual, sum(n for _, n in split) - manual


def _has_automation_data(split: list[tuple[str, int]]) -> bool:
    """False when no selected test case carries an automation status at all.

    Everything landing in the Unspecified bucket means the field never made it
    into the selected data, so reporting "N automated, 0 manual" would be a
    fabricated split rather than a measured one.
    """
    return any(status != "Unspecified" for status, _ in split)


# ── markup helpers ───────────────────────────────────────────────────────────


def _card(title: str, inner: str, *, note: str = "") -> str:
    """One titled section, as a full-width table with explicit cell colours."""
    note_html = (
        f'<div style="margin:0 0 12px 0; font-size:13px; line-height:19px; '
        f'color:{_MUTED};">{note}</div>' if note else ""
    )
    return f"""
      <tr>
        <td align="left" bgcolor="{_PAGE_BG}" style="padding:0 24px 16px 24px; background-color:{_PAGE_BG};">
          <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
                 style="border-collapse:collapse; background-color:{_CARD_BG}; border:1px solid {_LINE};">
            <tr>
              <td align="left" bgcolor="{_CARD_BG}"
                  style="padding:18px 20px 20px 20px; background-color:{_CARD_BG};
                         font-family:{_FONT}; color:{_INK};">
                <div style="margin:0 0 14px 0; font-size:15px; font-weight:bold;
                            letter-spacing:0.3px; color:{_INK};">{title}</div>
                {note_html}
                {inner}
              </td>
            </tr>
          </table>
        </td>
      </tr>"""


def _kv_table(rows: list[tuple[str, str]]) -> str:
    """Two-column label/value table."""
    if not rows:
        return f'<div style="font-size:13px; color:{_MUTED};">Not available for this run.</div>'
    cells = "".join(
        f"""
        <tr>
          <td align="left" valign="top" bgcolor="{_CARD_BG}"
              style="padding:5px 12px 5px 0; background-color:{_CARD_BG}; font-family:{_FONT};
                     font-size:13px; line-height:19px; color:{_MUTED}; white-space:nowrap;">{label}</td>
          <td align="left" valign="top" bgcolor="{_CARD_BG}"
              style="padding:5px 0; background-color:{_CARD_BG}; font-family:{_FONT};
                     font-size:13px; line-height:19px; color:{_INK};">{value}</td>
        </tr>"""
        for label, value in rows
    )
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
            f'style="border-collapse:collapse;">{cells}</table>')


def _list_table(headers: list[str], rows: list[list[str]], empty: str) -> str:
    """Bordered data table; renders `empty` when there are no rows."""
    if not rows:
        return (f'<div style="font-family:{_FONT}; font-size:13px; line-height:19px; '
                f'color:{_MUTED};">{empty}</div>')
    head = "".join(
        f"""<th align="left" bgcolor="#f4f6f9"
                style="padding:8px 10px; background-color:#f4f6f9; border-bottom:1px solid {_LINE};
                       font-family:{_FONT}; font-size:12px; font-weight:bold; color:{_MUTED};
                       text-transform:uppercase; letter-spacing:0.4px;">{h}</th>"""
        for h in headers
    )
    body = "".join(
        "<tr>" + "".join(
            f"""<td align="left" valign="top" bgcolor="{_CARD_BG}"
                    style="padding:8px 10px; background-color:{_CARD_BG};
                           border-bottom:1px solid {_LINE}; font-family:{_FONT};
                           font-size:13px; line-height:19px; color:{_INK};">{cell}</td>"""
            for cell in row
        ) + "</tr>"
        for row in rows
    )
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
            f'style="border-collapse:collapse;"><tr>{head}</tr>{body}</table>')


def _bullets(items) -> str:
    if not items:
        return f'<div style="font-size:13px; color:{_MUTED};">Not configured.</div>'
    lis = "".join(
        f'<li style="margin:0 0 6px 0; font-family:{_FONT}; font-size:13px; '
        f'line-height:19px; color:{_INK};">{_esc(item)}</li>'
        for item in items
    )
    return f'<ul style="margin:0; padding:0 0 0 18px;">{lis}</ul>'


def _stat(label: str, value: str, colour: str) -> str:
    """One cell of the key-stats row — a table cell, not a flex child."""
    return f"""
      <td align="center" valign="top" bgcolor="{_CARD_BG}" width="25%"
          style="padding:14px 8px; background-color:{_CARD_BG}; border:1px solid {_LINE};
                 font-family:{_FONT};">
        <div style="font-size:24px; line-height:28px; font-weight:bold; color:{colour};">{value}</div>
        <div style="margin-top:4px; font-size:11px; line-height:15px; color:{_MUTED};
                    text-transform:uppercase; letter-spacing:0.5px;">{label}</div>
      </td>"""


def _gap_analysis_card(narrative: dict | None) -> str:
    """Business-level requirement-gap narrative, folded in from the old gap alert.

    ``narrative`` is ``mailer.build_gap_narrative``'s output —
    ``{"summary": str|None, "unplanned_areas": [{name, detail, business_impact}]}``.
    Returns "" when no narrative or summary is available, so the section simply
    does not appear; the factual Coverage Risks card still lists the uncovered
    commits regardless.
    """
    if not narrative:
        return ""
    summary = narrative.get("summary")
    if not summary:
        return ""
    areas = narrative.get("unplanned_areas") or []

    summary_html = (
        f'<div style="margin:0 0 4px 0; font-family:{_FONT}; font-size:14px; '
        f'line-height:21px; color:{_INK}; border-left:3px solid {_WARN}; '
        f'padding-left:14px;">{_esc(summary)}</div>'
    )
    areas_html = ""
    if areas:
        areas_html = (
            '<div style="height:14px; line-height:14px; font-size:1px;">&nbsp;</div>'
            + _list_table(
                ["Unplanned area", "What changed", "Business impact"],
                [[_esc(a.get("name", "")), _esc(a.get("detail", "")),
                  _esc(a.get("business_impact", ""))]
                 for a in areas],
                "Related commits could not be grouped into named areas.",
            )
        )
    return _card(
        "Requirement Gap Analysis",
        summary_html + areas_html,
        note=("Changes not linked to any approved Jira story — no planned "
              "requirement covers them, so no test coverage is assigned. "
              "The commit-level breakdown is in Coverage Risks below."),
    )


# ── public API ───────────────────────────────────────────────────────────────


def build_test_plan_html(
    scope: dict,
    jira_stories: list[dict],
    defects: list[dict],
    test_cases: list[dict],
    uncovered: list,
    open_stories: list[dict] | None,
    cfg,
    gap_narrative: dict | None = None,
) -> str:
    """Return the complete standalone Test Plan email document.

    ``scope`` is scope.json, ``jira_stories``/``defects`` are the raw Jira dicts
    written to jira_stories.json / defects.json, ``test_cases`` is the selected
    list from test_cases.json, ``uncovered`` is the CommitCoverage list, and
    ``cfg`` is the loaded PredictConfig. ``gap_narrative`` is
    ``mailer.build_gap_narrative``'s output; when present its summary and
    unplanned-area breakdown render as a Requirement Gap Analysis section, and
    when absent that section is simply omitted.
    """
    base = scope.get("base", "") or "unknown"
    target = scope.get("target", "") or "unknown"
    commits = scope.get("commits", []) or []
    changed_symbols = scope.get("changed_symbols", []) or []
    screens = scope.get("affected_screens", []) or []
    files_touched = len({s.get("path", "") for s in changed_symbols if s.get("path")})

    auto_split = _automation_split(test_cases)
    prio_split = _priority_split(test_cases)
    manual_n, automated_n = _manual_vs_automated(auto_split)
    automation_known = _has_automation_data(auto_split)
    open_stories = open_stories or []

    entry = list(getattr(cfg, "test_plan_entry_criteria", None) or DEFAULT_ENTRY_CRITERIA)
    exit_ = list(getattr(cfg, "test_plan_exit_criteria", None) or DEFAULT_EXIT_CRITERIA)

    release = _esc(target)
    preheader = (f"Test plan and coverage risk summary for {target} — "
                 f"{len(jira_stories)} feature(s), {len(test_cases)} test case(s), "
                 f"{len(uncovered)} coverage risk(s).")

    # ── stats row ────────────────────────────────────────────────────────────
    stats = (
        _stat("Features", str(len(jira_stories)), _ACCENT)
        + _stat("Defects Retested", str(len(defects)), _ACCENT)
        + _stat("Coverage Risks", str(len(uncovered)), _WARN if uncovered else _ACCENT)
        + _stat("Auto / Manual",
                f"{automated_n}/{manual_n}" if automation_known else "n/a", _ACCENT)
    )

    # ── sections ─────────────────────────────────────────────────────────────
    summary = _card("Release Summary", _kv_table([
        ("Release", release),
        ("Baseline", _esc(base)),
        ("Commits", f"{len(commits)}"),
        ("Files changed", f"{files_touched}"),
        ("Changed symbols", f"{len(changed_symbols)}"),
        ("Affected screens", _esc(", ".join(screens)) if screens else "None identified"),
    ]))

    features = _card(
        "Features To Be Tested",
        _list_table(
            ["Story", "Summary", "Status"],
            [[_esc(k), _esc(s), _esc(st)]
             for k, s, st, _ in (_issue_fields(i) for i in jira_stories)],
            "No Jira stories were linked to the commits in this release.",
        ),
        note=f"{len(jira_stories)} story/stories in scope for this release.",
    )

    defect_scope = _card(
        "Defect Retest Scope",
        _list_table(
            ["Defect", "Summary", "Status"],
            [[_esc(k), _esc(s), _esc(st)]
             for k, s, st, _ in (_issue_fields(d) for d in defects)],
            "No defects are linked to the stories in this release.",
        ),
        note=f"{len(defects)} defect(s) to retest.",
    )

    approach = _card(
        "Test Approach — Automation Split",
        _list_table(
            ["Automation status", "Test cases"],
            [[_esc(status), str(n)] for status, n in auto_split],
            "No test cases were selected for this release.",
        ),
        note=(f"{automated_n} case(s) run through automation, {manual_n} executed manually "
              f'("Can Not Be Automated").' if automation_known else
              f"{len(test_cases)} selected case(s) carry no automation status in this run's "
              f"data, so the automation split could not be measured."),
    )

    risk_rows = [
        [_esc(getattr(c, "hash", "")),
         _esc(getattr(c, "author_email", "") or "unknown"),
         _esc((getattr(c, "message", "") or "")[:120])]
        for c in uncovered
    ]
    gap_card = _gap_analysis_card(gap_narrative)

    risks = _card(
        "Coverage Risks &amp; Gaps",
        _list_table(
            ["Commit", "Author", "Change"],
            risk_rows,
            "Every commit in this release maps to a planned Jira story.",
        ),
        note=(f"{len(uncovered)} commit(s) are not referenced by any Jira story, so no "
              f"planned requirement covers them."
              + (f" {len(open_stories)} open story/stories exist in the project as possible "
                 f"homes for this work." if open_stories else "")),
    )

    environment = _card("Test Environment", _kv_table([
        ("TCM project", _esc(getattr(cfg, "tcm_project_key", "") or "not set")),
        ("TCM instance", _esc(TCM_BASE_URL)),
        ("Jira instance", _esc(getattr(cfg, "jira_base_url", "") or "not set")),
    ]))

    criteria = _card(
        "Entry &amp; Exit Criteria",
        f'<div style="margin:0 0 8px 0; font-family:{_FONT}; font-size:13px; '
        f'font-weight:bold; color:{_INK};">Entry</div>{_bullets(entry)}'
        f'<div style="margin:16px 0 8px 0; font-family:{_FONT}; font-size:13px; '
        f'font-weight:bold; color:{_INK};">Exit</div>{_bullets(exit_)}',
    )

    tc_rows = [["Total selected", str(len(test_cases))]]
    tc_rows += [[f"Priority: {_esc(p)}", str(n)] for p, n in prio_split]
    tc_summary = _card(
        "Test Case Summary",
        _list_table(["Bucket", "Count"], tc_rows, "No test cases selected."),
        note=("Priority breakdown omitted — no test case in this release carries a "
              "priority value." if not prio_split else ""),
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Test Plan — {release}</title>
<style type="text/css">
  /* Responsive polish only — every load-bearing style is inline above. */
  @media only screen and (max-width:620px) {{
    .rq-stat {{ display:block !important; width:100% !important; }}
    .rq-pad {{ padding-left:12px !important; padding-right:12px !important; }}
  }}
</style>
</head>
<body style="margin:0; padding:0; background-color:{_PAGE_BG}; -webkit-text-size-adjust:100%;">
<div style="display:none; max-height:0; overflow:hidden; mso-hide:all;
            font-size:1px; line-height:1px; color:{_PAGE_BG};">{_esc(preheader)}</div>

<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
       bgcolor="{_PAGE_BG}" style="background-color:{_PAGE_BG}; border-collapse:collapse;">
  <tr>
    <td align="center" bgcolor="{_PAGE_BG}" style="background-color:{_PAGE_BG}; padding:24px 8px;">

      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="640"
             style="width:640px; max-width:640px; border-collapse:collapse;">

        <tr>
          <td align="left" bgcolor="{_HEADER_BG}" class="rq-pad"
              style="background-color:{_HEADER_BG}; padding:22px 24px; font-family:{_FONT};">
            <div style="font-size:12px; line-height:16px; color:#a9c2dd;
                        text-transform:uppercase; letter-spacing:1px;">RegressIQ — Test Plan</div>
            <div style="margin-top:6px; font-size:21px; line-height:27px; font-weight:bold;
                        color:#ffffff;">{release}</div>
            <div style="margin-top:4px; font-size:13px; line-height:19px; color:#c9dcee;">
              Baseline {_esc(base)} &middot; {len(commits)} commit(s) &middot; {len(screens)} screen(s) affected
            </div>
          </td>
        </tr>

        <tr>
          <td align="center" bgcolor="{_PAGE_BG}" class="rq-pad"
              style="background-color:{_PAGE_BG}; padding:16px 24px;">
            <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
                   style="border-collapse:collapse;">
              <tr>{stats}</tr>
            </table>
          </td>
        </tr>

        {summary}
        {features}
        {defect_scope}
        {approach}
        {tc_summary}
        {gap_card}
        {risks}
        {environment}
        {criteria}

        <tr>
          <td align="left" bgcolor="{_PAGE_BG}" class="rq-pad"
              style="background-color:{_PAGE_BG}; padding:4px 24px 24px 24px;
                     font-family:{_FONT}; font-size:12px; line-height:18px; color:{_MUTED};">
            The full regression report is attached as <strong>report.html</strong>.
            Every figure above is computed from this run's scope, Jira, and TCM data —
            generated automatically by RegressIQ.
          </td>
        </tr>

      </table>
    </td>
  </tr>
</table>
</body>
</html>"""


def send_test_plan_email(
    html_body: str,
    report_path: str | Path | None,
    recipients: list[str],
    smtp_cfg,
    subject: str = "[RegressIQ] Test Plan",
) -> None:
    """Send the Test Plan email with report.html attached.

    A brand-new multipart/mixed message — deliberately not routed through
    ``mailer.send_gap_alert``, which sends its own separate email.
    """
    if not recipients:
        log.warning("test plan: no recipients — not sending")
        return

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = smtp_cfg.from_addr
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    attached = False
    if report_path:
        p = Path(report_path)
        if p.is_file():
            # octet-stream rather than text/html: guarantees every client treats
            # it as a download instead of trying to inline-render the report.
            part = MIMEApplication(p.read_bytes(), _subtype="octet-stream")
            part.add_header("Content-Disposition", "attachment", filename="report.html")
            msg.attach(part)
            attached = True
        else:
            log.warning("test plan: report not found at %s — sending without attachment", p)

    log.debug("test plan: sending to %d recipient(s) %s via %s:%s (attachment=%s)",
              len(recipients), recipients, smtp_cfg.host, smtp_cfg.port, attached)
    try:
        with smtplib.SMTP(smtp_cfg.host, smtp_cfg.port) as server:
            server.starttls()
            server.login(smtp_cfg.user, smtp_cfg.password)
            server.sendmail(smtp_cfg.from_addr, recipients, msg.as_string())
    except smtplib.SMTPException as e:
        log.debug("test plan: SMTP send failed (%s)", e, exc_info=True)
        raise
    log.debug("test plan: sent")
