"""LLM-scored semantic fallback for commit→story (L3) and TC→scope (L4) matching.

Two public functions:
  enrich_layer3 — scores open/active Jira stories against uncovered commits
  enrich_layer4 — scores unlinked TCM test cases against regression scope

Both make one batch LLM call and return (auto_included, alerts).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .gap_detector import CommitCoverage
from .jira_client import JiraClient
from .llm_config import LLMConfig, call_llm_api

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SemanticMatch:
    key: str          # Jira story key (L3) or TC unique_id (L4)
    score: int        # LLM-assigned 1-10
    reason: str       # one-sentence LLM explanation
    confidence: str   # "semantic" (score >=7) or "suggested" (score 4-6)


def _split_by_score(scores: list[dict]) -> tuple[list[SemanticMatch], list[SemanticMatch]]:
    included, alerts = [], []
    for item in scores:
        try:
            score = int(item.get("score", 0))
        except (TypeError, ValueError):
            score = 0
        key = str(item.get("key", ""))
        reason = str(item.get("reason", ""))
        if not key:
            continue
        if score >= 7:
            included.append(SemanticMatch(key=key, score=score, reason=reason, confidence="semantic"))
        elif score >= 4:
            alerts.append(SemanticMatch(key=key, score=score, reason=reason, confidence="suggested"))
    log.debug("scored %d candidate(s): %d included (>=7), %d alert(s) (4-6), %d dropped (<4)",
              len(scores), len(included), len(alerts), len(scores) - len(included) - len(alerts))
    for m in included + alerts:
        log.debug("  %s score=%d (%s) — %s", m.key, m.score, m.confidence, m.reason)
    return included, alerts


def _tc_title(tc: dict) -> str:
    for f in (tc.get("customFieldValues") or []):
        if f.get("fieldDefinition", {}).get("name") == "TEST_CASE_TITLE":
            return f.get("value") or ""
    return tc.get("title", "")


def enrich_layer3(
    uncovered_commits: list[CommitCoverage],
    changed_symbols: list[dict],
    affected_screens: list[str],
    jira: JiraClient,
    project_key: str,
    llm_cfg: LLMConfig,
) -> tuple[list[SemanticMatch], list[SemanticMatch]]:
    """Score open/active Jira stories against uncovered commits.

    Returns (auto_included, alerts).
    auto_included: score >=7, confidence="semantic"
    alerts:        score 4-6, confidence="suggested"
    """
    if not uncovered_commits:
        log.debug("semantic L3: no uncovered commits — nothing to score")
        return [], []

    open_stories = jira.fetch_open_stories(project_key)
    if not open_stories:
        log.debug("semantic L3: no open stories in %s — nothing to score against", project_key)
        return [], []
    log.debug("semantic L3: scoring %d open story/stories against %d uncovered commit(s)",
              len(open_stories), len(uncovered_commits))

    symbol_names = [s.get("name", "") for s in changed_symbols[:15]]
    commit_lines = "\n".join(
        f"  - {c.hash[:7]}: {c.message} | changed: {', '.join(symbol_names[:5])} | screens: {', '.join(affected_screens)}"
        for c in uncovered_commits
    )
    story_lines = "\n".join(
        f"  - {s['key']}: {s.get('fields', {}).get('summary', '')} "
        f"(status: {(s.get('fields', {}).get('status') or {}).get('name', 'Unknown')})"
        for s in open_stories
    )

    prompt = f"""You are a QA analyst. For each candidate Jira story, score its relevance to the uncovered commits on a scale of 1-10.

UNCOVERED COMMITS (not found in any Jira story):
{commit_lines}

CANDIDATE JIRA STORIES (open/active):
{story_lines}

Scoring guide:
  10 = story directly names the changed feature or screen
   7 = story is clearly about the same area as the changed code
   5 = story is plausibly related but uncertain
   3 = story is in the same project but unrelated area
   1 = no relationship

Return JSON with exactly this structure:
{{
  "scores": [
    {{"key": "REG-X", "score": 8, "reason": "one sentence explanation"}}
  ]
}}

Score every candidate story listed above. Do not omit any."""

    result = call_llm_api(prompt, llm_cfg)
    return _split_by_score(result.get("scores", []))


def enrich_layer4(
    unlinked_tcs: list[dict],
    affected_screens: list[str],
    changed_symbols: list[dict],
    story_summaries: list[str],
    llm_cfg: LLMConfig,
) -> tuple[list[SemanticMatch], list[SemanticMatch]]:
    """Score unlinked TCM test cases against the regression scope.

    Returns (auto_included, alerts).
    auto_included: score >=7, confidence="semantic"
    alerts:        score 4-6, confidence="suggested"
    """
    if not unlinked_tcs:
        log.debug("semantic L4: no unlinked test cases — nothing to score")
        return [], []
    log.debug("semantic L4: scoring %d unlinked TC(s) against %d screen(s) and %d symbol(s)",
              len(unlinked_tcs), len(affected_screens), len(changed_symbols))

    symbol_names = [s.get("name", "") for s in changed_symbols[:15]]

    tc_lines = "\n".join(
        f"  - {tc.get('uniqueTestcaseId', '')}: {_tc_title(tc)}"
        for tc in unlinked_tcs
    )

    prompt = f"""You are a QA analyst. For each unlinked test case, score its relevance to this regression on a scale of 1-10.

REGRESSION SCOPE:
- Affected screens: {', '.join(affected_screens) or 'none'}
- Changed methods: {', '.join(symbol_names) or 'none'}
- Jira stories in scope: {'; '.join(story_summaries) or 'none'}

UNLINKED TEST CASES (no Jira story assigned):
{tc_lines}

Scoring guide:
  10 = TC directly tests a changed screen or method
   7 = TC clearly covers the area being changed
   5 = TC might catch regressions in this area
   3 = TC is in same domain but unlikely to be affected
   1 = TC is unrelated

Return JSON with exactly this structure:
{{
  "scores": [
    {{"key": "REGTC-X", "score": 7, "reason": "one sentence explanation"}}
  ]
}}

Score every candidate test case listed above. Do not omit any."""

    result = call_llm_api(prompt, llm_cfg)
    return _split_by_score(result.get("scores", []))
