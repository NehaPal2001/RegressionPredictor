"""Read-only Jira Cloud client (sdettech-tea.atlassian.net).

Auth: Basic auth — JIRA_EMAIL + JIRA_API_TOKEN.
Uses POST /rest/api/3/search/jql (GET /search is 410 Gone on Jira v3).
No writes to Jira under any circumstances.
"""

from __future__ import annotations

import base64
import json
import urllib.request
import urllib.error
from dataclasses import dataclass

_FIELDS = ["key", "issuetype", "status", "priority", "summary", "description", "customfield_10014", "issuelinks"]


@dataclass(frozen=True)
class JiraIssue:
    key: str
    type: str          # "Story", "Bug", "Epic"
    status: str
    priority: str
    summary: str
    description: str   # plain text extracted from ADF
    epic_key: str | None


def _adf_to_text(node: dict | None) -> str:
    """Recursively extract plain text from Atlassian Document Format."""
    if not node:
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    parts = [_adf_to_text(child) for child in (node.get("content") or [])]
    joined = " ".join(p for p in parts if p)
    if node.get("type") in ("paragraph", "listItem", "heading", "bulletList", "orderedList"):
        return joined + "\n" if joined else ""
    return joined


def _parse_issue(raw: dict) -> JiraIssue:
    f = raw.get("fields", {})
    desc_adf = f.get("description")
    desc = _adf_to_text(desc_adf).strip() if isinstance(desc_adf, dict) else (desc_adf or "")
    epic_field = f.get("customfield_10014")
    epic_key = epic_field.get("key") if isinstance(epic_field, dict) else None
    return JiraIssue(
        key=raw["key"],
        type=(f.get("issuetype") or {}).get("name", ""),
        status=(f.get("status") or {}).get("name", ""),
        priority=(f.get("priority") or {}).get("name", ""),
        summary=f.get("summary", ""),
        description=desc,
        epic_key=epic_key,
    )


class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str):
        self._base = base_url.rstrip("/")
        raw = f"{email}:{api_token}"
        self._auth = "Basic " + base64.b64encode(raw.encode()).decode()

    def _post_search(self, jql: str, max_results: int = 100) -> list[dict]:
        url = f"{self._base}/rest/api/3/search/jql"
        body = json.dumps({"jql": jql, "maxResults": max_results, "fields": _FIELDS}).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Authorization", self._auth)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read()).get("issues", [])

    def fetch_stories(self, project_key: str, max_results: int = 100) -> list[JiraIssue]:
        """Fetch Story issues in a project."""
        jql = f'project="{project_key}" AND issuetype=Story ORDER BY created DESC'
        return [_parse_issue(r) for r in self._post_search(jql, max_results)]

    def fetch_defects(self, project_key: str, max_results: int = 100) -> list[JiraIssue]:
        """Fetch Bug/Defect issues in a project."""
        jql = f'project="{project_key}" AND issuetype in (Bug, Defect) ORDER BY created DESC'
        return [_parse_issue(r) for r in self._post_search(jql, max_results)]

    def fetch_by_keys(self, keys: list[str]) -> list[JiraIssue]:
        """Fetch specific issues by key list."""
        if not keys:
            return []
        key_list = ", ".join(f'"{k}"' for k in keys)
        return [_parse_issue(r) for r in self._post_search(f"issue in ({key_list})", len(keys))]

    def fetch_raw_by_keys(self, keys: list[str]) -> list[dict]:
        """Return raw Jira issue dicts (not parsed JiraIssue) for the given keys."""
        if not keys:
            return []
        key_list = ", ".join(f'"{k}"' for k in keys)
        return self._post_search(f"issue in ({key_list})", len(keys))

    def fetch_defects_for_stories(self, story_keys: list[str]) -> list[dict]:
        """Find defect issues linked via issuelinks on the given stories.

        Fetches story raw dicts, scans issuelinks for outwardIssue/inwardIssue
        where issuetype.name == 'Defects', then batch-fetches those defects.
        Returns raw Jira issue dicts. Empty list if no defects linked.
        """
        stories_raw = self.fetch_raw_by_keys(story_keys)
        defect_keys: set[str] = set()
        for raw in stories_raw:
            for link in raw.get("fields", {}).get("issuelinks", []):
                for direction in ("inwardIssue", "outwardIssue"):
                    linked = link.get(direction)
                    if linked and linked.get("fields", {}).get("issuetype", {}).get("name") in ("Bug", "Defect", "Defects"):
                        defect_keys.add(linked["key"])
        if not defect_keys:
            return []
        return self.fetch_raw_by_keys(sorted(defect_keys))

    def fetch_open_stories(self, project_key: str, max_results: int = 100) -> list[dict]:
        """Fetch all To Do + In Progress stories for semantic L3 scoring.

        Returns raw Jira issue dicts (same shape as fetch_raw_by_keys).
        """
        jql = (
            f'project="{project_key}" AND status in ("To Do","In Progress")'
            f' AND issuetype=Story ORDER BY priority DESC'
        )
        return self._post_search(jql, max_results=max_results)

    def search_commit(self, commit_hash: str, project_key: str) -> list[JiraIssue]:
        """Find Jira stories that mention a commit hash in description or any comment."""
        jql = f'project="{project_key}" AND text ~ "{commit_hash}"'
        return [_parse_issue(r) for r in self._post_search(jql, 20)]

    def search_commit_message(self, message: str, project_key: str) -> list[JiraIssue]:
        """Fallback: search by commit message keywords across description and all comments.

        Strips special JQL characters, takes first 60 chars so the query stays stable.
        Used when the hash alone is not found in any story.
        """
        # Remove chars that break JQL text~ queries
        clean = message.replace('"', ' ').replace("'", ' ').replace('\\', ' ').strip()
        # Take first 60 chars — enough to be distinctive, short enough to avoid JQL limits
        keyword = clean[:60].rsplit(' ', 1)[0]
        if len(keyword) < 6:
            return []
        try:
            jql = f'project="{project_key}" AND text ~ "{keyword}"'
            return [_parse_issue(r) for r in self._post_search(jql, 20)]
        except Exception:
            return []
