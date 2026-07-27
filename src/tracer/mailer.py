"""SMTP requirement gap alert.

Sends a plain-text email when commits in the diff are not found in any Jira
story. Recipients = union of commit author emails + configured team list.
Never sends if there are no uncovered commits.
"""

from __future__ import annotations

import smtplib
from dataclasses import dataclass
from email.mime.text import MIMEText

from .gap_detector import CommitCoverage


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str
    from_addr: str


def send_gap_alert(
    uncovered: list[CommitCoverage],
    author_emails: list[str],
    team_emails: list[str],
    smtp_cfg: SmtpConfig,
    project_key: str,
) -> None:
    """Send gap alert email. No-op if uncovered is empty."""
    if not uncovered:
        return
    recipients = sorted(set(author_emails) | set(team_emails))
    if not recipients:
        return

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

    with smtplib.SMTP(smtp_cfg.host, smtp_cfg.port) as server:
        server.starttls()
        server.login(smtp_cfg.user, smtp_cfg.password)
        server.sendmail(smtp_cfg.from_addr, recipients, msg.as_string())
