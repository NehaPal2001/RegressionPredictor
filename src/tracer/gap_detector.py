"""Deterministic commit coverage classifier. No AI.

Classifies each commit in the scope as covered (found in at least one Jira
story description or comment) or uncovered (no story mentions it).
Covered commits drive test case selection; uncovered ones trigger the gap alert.
"""

from __future__ import annotations

from dataclasses import dataclass

from .jira_client import JiraClient


@dataclass(frozen=True)
class CommitCoverage:
    hash: str
    author_email: str
    message: str
    covered: bool
    jira_keys: tuple[str, ...]   # empty when covered=False


def detect_gaps(
    commits: list[dict],     # each has "hash", "author_email", "message"
    jira: JiraClient,
    project_key: str,
) -> list[CommitCoverage]:
    """Return coverage status for every commit in the scope.

    Search strategy (two passes per uncovered commit):
    1. Hash search  — text ~ "d1125cb"  (searches description + ALL comments)
    2. Message search — text ~ "api impl of history..." (fallback when hash not found)

    Merge commit hashes are skipped for message search to avoid false positives.
    Results from both passes are merged and deduplicated by story key.
    """
    result = []
    for c in commits:
        message = c.get("message", "")
        is_merge = message.lower().startswith("merge ")

        # Pass 1: search by 7-char commit hash (covers hash in description or any comment)
        by_hash = jira.search_commit(c["hash"][:7], project_key)

        # Pass 2: search by commit message keywords — only if hash search found nothing
        # and it is not a merge commit (merge messages are too generic to be useful)
        by_msg: list = []
        if not by_hash and not is_merge:
            by_msg = jira.search_commit_message(message, project_key)

        # Deduplicate by story key — hash results take priority
        seen: dict[str, object] = {i.key: i for i in by_hash}
        for i in by_msg:
            seen.setdefault(i.key, i)
        matching = list(seen.values())

        result.append(CommitCoverage(
            hash=c["hash"],
            author_email=c.get("author_email", ""),
            message=message,
            covered=bool(matching),
            jira_keys=tuple(i.key for i in matching),
        ))
    return result
