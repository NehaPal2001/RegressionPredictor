"""Read-only client for SDET360 TCM (testify.sdet360.ai).

Auth: cookie-based — caller supplies session and project_session JWT strings.
Paginates all test cases for a given vertical + project automatically.
On 401, attempts one refresh using refresh_token before raising.
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error
from dataclasses import dataclass, field

BASE_URL = "https://testify.sdet360.ai"
_REFRESH_URL = f"{BASE_URL}/api/auth/refresh"


@dataclass(frozen=True)
class TestStep:
    order: int
    action: str
    expected: str


@dataclass(frozen=True)
class TestCase:
    id: str
    unique_id: str           # e.g. "TC-0001"
    jira_story_key: str      # "NA" when not linked to a Jira story
    jira_story_id: str | None
    status: str              # e.g. "ACTIVE"
    approval_status: str     # e.g. "APPROVED"
    title: str
    description: str
    category: str
    priority: str
    automation_status: str
    test_type: str
    steps: tuple[TestStep, ...] = field(default_factory=tuple)


def _cv(custom_fields: list[dict], name: str) -> str:
    """Extract a custom field value by its definition name."""
    for f in custom_fields:
        if f.get("fieldDefinition", {}).get("name") == name:
            return f.get("value") or ""
    return ""


def _parse_step(raw: dict) -> TestStep:
    return TestStep(
        order=raw.get("order", 0),
        action=raw.get("action") or "",
        expected=raw.get("expected") or "",
    )


def _parse_case(raw: dict) -> TestCase:
    cf = raw.get("customFieldValues") or []
    steps = tuple(_parse_step(s) for s in (raw.get("testSteps") or []))
    return TestCase(
        id=raw["id"],
        unique_id=raw.get("uniqueTestcaseId") or "",
        jira_story_key=raw.get("jiraStoryKey") or "NA",
        jira_story_id=raw.get("jiraStoryId"),
        status=raw.get("testcaseStatus") or "",
        approval_status=raw.get("approvalStatus") or "",
        title=_cv(cf, "TEST_CASE_TITLE"),
        description=_cv(cf, "TEST_CASE_DESCRIPTION"),
        category=_cv(cf, "TEST_CATEGORY"),
        priority=_cv(cf, "TEST_CASE_PRIORITY"),
        automation_status=_cv(cf, "AUTOMATION_STATUS"),
        test_type=_cv(cf, "TEST_CASE_TYPE"),
        steps=steps,
    )


def _page_url(vertical_id: str, project_key: str, page: int, size: int) -> str:
    return (
        f"{BASE_URL}/api/testcases/{vertical_id}/stories/testcases"
        f"?projectKey={project_key}&page={page}&size={size}&sort=uniqueTestcaseId,asc"
    )


def _do_request(url: str, session: str, project_session: str) -> dict:
    body = b"{}"
    req = urllib.request.Request(url, method="POST", data=body)
    req.add_header("Cookie", f"session={session}; project_session={project_session}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Content-Length", str(len(body)))
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def _try_refresh(refresh_token: str) -> tuple[str, str] | None:
    """Call refresh endpoint; return (new_session, new_project_session) or None on failure."""
    if not refresh_token:
        return None
    try:
        body = json.dumps({"refreshToken": refresh_token}).encode()
        req = urllib.request.Request(_REFRESH_URL, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
        return data.get("session", ""), data.get("projectSession", "")
    except Exception:
        return None


def fetch_all(
    vertical_id: str,
    project_key: str,
    session: str,
    project_session: str,
    page_size: int = 10,
    refresh_token: str = "",
) -> tuple[list[dict], list[TestCase]]:
    """Fetch every test case in a TCM project, paginating automatically.

    Returns (raw_content_list, parsed_cases_list). raw_content_list is the
    concatenated content[] arrays from all pages — suitable for saving as
    sdet360testcases.json. On 401, attempts one token refresh if refresh_token
    is provided.
    """
    def _get_page(page: int, sess: str, proj_sess: str) -> dict:
        url = _page_url(vertical_id, project_key, page, page_size)
        try:
            return _do_request(url, sess, proj_sess)
        except urllib.error.HTTPError as e:
            if e.code == 401 and refresh_token:
                refreshed = _try_refresh(refresh_token)
                if refreshed:
                    return _do_request(url, refreshed[0], refreshed[1])
            raise

    first = _get_page(0, session, project_session)
    raw_all: list[dict] = list(first.get("content", []))
    cases: list[TestCase] = [_parse_case(r) for r in first.get("content", [])]
    total_pages = first.get("totalPages", 1)
    for page in range(1, total_pages):
        data = _get_page(page, session, project_session)
        raw_all.extend(data.get("content", []))
        cases.extend(_parse_case(r) for r in data.get("content", []))
    return raw_all, cases


def by_jira_key(cases: list[TestCase], jira_key: str) -> list[TestCase]:
    """Return only the test cases linked to a specific Jira story key."""
    return [c for c in cases if c.jira_story_key == jira_key]


def linked_jira_keys(cases: list[TestCase]) -> set[str]:
    """Return all non-NA jiraStoryKey values across all test cases."""
    return {c.jira_story_key for c in cases if c.jira_story_key and c.jira_story_key != "NA"}
