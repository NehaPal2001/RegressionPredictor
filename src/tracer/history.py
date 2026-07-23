"""Defect recurrence: does this change re-touch lines that a past fix commit touched?

Pure git (`log -L`), no AI. A hit means: the exact line range being changed now was
changed before by a commit whose subject says it fixed something. That is the
strongest deterministic "test this hard" signal available.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import date as _date

from .diff import GitError

FIX_WORDS = ("fix", "bug", "hotfix", "defect", "patch", "issue")

RECENCY_DAYS = 90


def _is_recent(date_str: str) -> bool:
    try:
        return (_date.today() - _date.fromisoformat(date_str)).days < RECENCY_DAYS
    except ValueError:
        return True  # unknown format → conservative: treat as recent


@dataclass(frozen=True)
class Recurrence:
    path: str
    start: int
    end: int
    sha: str
    subject: str
    date: str


def _fixish(subject: str) -> bool:
    s = subject.lower()
    return any(w in s for w in FIX_WORDS)


def _is_revert(subject: str) -> bool:
    return subject.strip().lower().startswith("revert")


def _prior_commits(repo: str, base: str) -> set[str]:
    r = subprocess.run(
        ["git", "-C", repo, "rev-list", base], capture_output=True, text=True
    )
    if r.returncode != 0:
        # empty set would silently disable recurrence — a false "all clear"
        raise GitError(f"git rev-list {base} failed: {r.stderr.strip()}")
    return set(r.stdout.split())


def recurrence(
    repo: str, target: str, base: str, files: list[tuple[str, list[tuple[int, int]]]]
) -> list[Recurrence]:
    """For each changed range (line numbers valid at `target`), trace its line history
    and keep fix-like commits that predate the branch (reachable from `base`)."""
    prior = _prior_commits(repo, base)
    hits: list[Recurrence] = []
    seen: set[tuple[str, str]] = set()
    for path, ranges in files:
        for start, end in ranges:
            r = subprocess.run(
                [
                    "git", "-C", repo, "log",
                    f"-L{start},{end}:{path}",
                    "--format=%H%x09%P%x09%s%x09%cs",
                    target,
                ],
                capture_output=True,
                text=True,
            )
            if r.returncode != 0:
                continue  # range/file unknown to git at target (e.g. brand-new file)
            for line in r.stdout.splitlines():
                parts = line.split("\t", 3)
                if len(parts) != 4 or len(parts[0]) != 40:
                    continue  # diff payload, not a commit header
                sha, parents, subject, date = parts
                if " " in parents:  # two or more parent hashes → merge commit
                    continue
                if _is_revert(subject):
                    continue
                if sha in prior and _fixish(subject) and (sha, path) not in seen:
                    seen.add((sha, path))
                    hits.append(Recurrence(path, start, end, sha[:8], subject, date))
    return hits
