"""Load Jira, TCM, and SMTP configuration from .env files.

An explicitly-passed .env file wins over any same-named shell environment
variable (first .env file wins if the key appears in more than one); os.environ
is only a fallback for keys absent from every .env file. This is the opposite
of llm.py's load_key precedence — deliberately, because TCM_SESSION /
TCM_PROJECT_SESSION / TCM_REFRESH_TOKEN are short-lived cookies the user
refreshes by editing .env. If a stale copy of one of these ever gets exported
into a shell (e.g. from a previous `export` or debugging session), os.environ
winning would silently shadow every future .env edit with an expired token —
which is exactly what happened before this was fixed: TCM kept 401ing even
after the .env cookie was refreshed, because a leftover exported TCM_SESSION
several days stale was shadowing it. Never raises on missing keys — callers
get empty strings and use --no-alert / --no-predict-ai to degrade.
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
    """Build env dict: .env files first (first file wins), os.environ fills gaps.

    A key already set from an earlier .env file is never overwritten by a later
    one or by os.environ. When a key exists in os.environ AND in some .env file
    with a different value, the .env value wins but the shadowing is logged —
    otherwise a stale shell-exported credential silently overrides every future
    .env edit with no visible symptom beyond the downstream API call failing.
    """
    env: dict[str, str] = {}
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
    for k, v in os.environ.items():
        if k in env and env[k] != v:
            log.warning("%s is set in the shell environment but differs from the "
                        ".env value — using .env (unset it in your shell if this "
                        "is unexpected: the shell value is being ignored)", k)
        elif k not in env:
            env[k] = v
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
