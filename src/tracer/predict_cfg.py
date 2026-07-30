"""Load Jira, TCM, and SMTP configuration from .env files.

Follows the same pattern as llm.py load_key: checks os.environ first, then
reads each .env file in order (first value wins). Never raises on missing keys
— callers get empty strings and use --no-alert / --no-predict-ai to degrade.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PredictConfig:
    # Jira — read-only
    jira_base_url: str = "https://sdettech-tea.atlassian.net"
    jira_email: str = ""
    jira_api_token: str = ""
    # TCM — cookie auth
    tcm_session: str = ""
    tcm_project_session: str = ""
    tcm_refresh_token: str = ""
    tcm_vertical_id: str = ""
    tcm_project_key: str = "AS360"
    tcm_project_id: str = ""
    # SMTP
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    alert_emails: list[str] = field(default_factory=list)
    # LLM
    groq_api_key: str = ""


def _read_env(*env_files: str | Path) -> dict[str, str]:
    """Build env dict: os.environ first, then .env files (first value wins)."""
    env: dict[str, str] = dict(os.environ)
    for f in env_files:
        p = Path(f)
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip("\"'")
            if k and k not in env:
                env[k] = v
    return env


def load_predict_config(*env_files: str | Path) -> PredictConfig:
    """Load PredictConfig from environment and .env files."""
    e = _read_env(*env_files)
    emails_raw = e.get("ALERT_EMAILS", "")
    alert_emails = [a.strip() for a in emails_raw.split(",") if a.strip()]
    return PredictConfig(
        jira_base_url=e.get("JIRA_BASE_URL", "https://sdettech-tea.atlassian.net"),
        jira_email=e.get("JIRA_EMAIL", ""),
        jira_api_token=e.get("JIRA_API_TOKEN", ""),
        tcm_session=e.get("TCM_SESSION", ""),
        tcm_project_session=e.get("TCM_PROJECT_SESSION", ""),
        tcm_refresh_token=e.get("TCM_REFRESH_TOKEN", ""),
        tcm_vertical_id=e.get("TCM_VERTICAL_ID", ""),
        tcm_project_key=e.get("TCM_PROJECT_KEY", "AS360"),
        tcm_project_id=e.get("TCM_PROJECT_ID", ""),
        smtp_host=e.get("SMTP_HOST", ""),
        smtp_port=int(e.get("SMTP_PORT", "587")),
        smtp_user=e.get("SMTP_USER", ""),
        smtp_password=e.get("SMTP_PASSWORD", ""),
        smtp_from=e.get("SMTP_FROM", ""),
        alert_emails=alert_emails,
        groq_api_key=e.get("GROQ_API_KEY", ""),
    )
