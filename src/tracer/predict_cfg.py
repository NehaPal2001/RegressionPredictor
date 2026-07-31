"""Load Jira, TCM, and SMTP configuration from .env files.

Follows the same pattern as llm.py load_key: checks os.environ first, then
reads each .env file in order (first value wins). Never raises on missing keys
— callers get empty strings and use --no-alert / --no-predict-ai to degrade.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


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
    # Test Plan email — fixed policy text, empty means "use the built-in default"
    test_plan_entry_criteria: list[str] = field(default_factory=list)
    test_plan_exit_criteria: list[str] = field(default_factory=list)
    # LLM
    groq_api_key: str = ""


def _read_env(*env_files: str | Path) -> dict[str, str]:
    """Build env dict: os.environ first, then .env files (first value wins)."""
    env: dict[str, str] = dict(os.environ)
    found = 0
    for f in env_files:
        p = Path(f)
        if not p.exists():
            log.debug("env file not found: %s", p)
            continue
        found += 1
        log.debug("env file loaded: %s", p)
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip("\"'")
            if k and k not in env:
                env[k] = v
    if env_files and not found:
        log.warning("no .env file found (tried: %s) — Jira, TCM, SMTP and LLM "
                    "credentials will be empty",
                    ", ".join(str(Path(f)) for f in env_files))
    return env


def _criteria(raw: str) -> list[str]:
    """Split a criteria env var into individual lines.

    .env files are line-based, so a multi-line block cannot be expressed
    directly: separate the criteria with ``|`` (a literal ``\\n`` also works).
    Empty means the caller falls back to its own default policy text.
    """
    return [part.strip() for part in raw.replace("\\n", "|").split("|") if part.strip()]


def _presence(e: dict[str, str], *keys: str) -> str:
    """'KEY=set KEY2=MISSING' — presence only, never the value itself."""
    return " ".join(f"{k}={'set' if e.get(k) else 'MISSING'}" for k in keys)


def load_predict_config(*env_files: str | Path) -> PredictConfig:
    """Load PredictConfig from environment and .env files."""
    e = _read_env(*env_files)
    emails_raw = e.get("ALERT_EMAILS", "")
    alert_emails = [a.strip() for a in emails_raw.split(",") if a.strip()]
    # Secrets are never written to the log — only whether each one was found.
    log.debug("predict config — jira: %s",
              _presence(e, "JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"))
    log.debug("predict config — tcm: %s",
              _presence(e, "TCM_SESSION", "TCM_PROJECT_SESSION", "TCM_REFRESH_TOKEN",
                        "TCM_VERTICAL_ID", "TCM_PROJECT_KEY", "TCM_PROJECT_ID"))
    log.debug("predict config — smtp: %s, %d alert recipient(s)",
              _presence(e, "SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"),
              len(alert_emails))
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
        test_plan_entry_criteria=_criteria(e.get("TEST_PLAN_ENTRY_CRITERIA", "")),
        test_plan_exit_criteria=_criteria(e.get("TEST_PLAN_EXIT_CRITERIA", "")),
        groq_api_key=e.get("GROQ_API_KEY", ""),
    )
