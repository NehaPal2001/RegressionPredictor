"""Mock Jira ticket tree — no real API.

Builds a parent regression Story for the branch under test and one Sub-task per
commit that lives on the branch, each mapped deterministically to the modules and
screens it touches (via the changed symbols in that commit's files). Fix-shaped
commits are flagged — those are where regressions hide.

This is a stand-in for the real Jira post: same shape (parent + sub-tickets + a
mapping to what QA must regression-test), rendered as a preview instead of posted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .diff import GitError, branch_commits, commit_files
from .history import _fixish
from .screens import module_of

_ORDER = {"HIGH": 2, "MEDIUM": 1, "LOW": 0}


@dataclass
class Ticket:
    key: str
    kind: str            # "Story" | "Sub-task"
    summary: str
    parent: str | None
    modules: list[str] = field(default_factory=list)
    screens: list[str] = field(default_factory=list)
    risk: str = "LOW"
    commit: str | None = None
    is_fix: bool = False


def build_tickets(
    repo: str, base: str, target: str, changed_risks, features,
    two_dot: bool = False, project: str = "SDET", start_num: int = 1000,
) -> tuple[Ticket, list[Ticket]]:
    """(parent Story, [sub-tickets]). Sub-tickets map commits → modules/screens/risk."""
    commits = branch_commits(repo, base, target, two_dot)

    # index changed symbols by file so a commit's files → the symbols (and risk) it changed
    syms_by_file: dict[str, list] = {}
    for r in changed_risks:
        syms_by_file.setdefault(r.symbol.path, []).append(r)
    # short-name → screens it reaches (features record reaching symbols in `via`)
    screens_by_symbol: dict[str, set[str]] = {}
    for f in features:
        for name in f.via:
            screens_by_symbol.setdefault(name, set()).add(f.screen)

    parent = Ticket(
        key=f"{project}-{start_num}", kind="Story", parent=None,
        summary=f"Regression sign-off: {target} (vs {base})",
        risk=max((r.level for r in changed_risks), key=lambda l: _ORDER[l], default="LOW"),
    )
    subs: list[Ticket] = []
    for i, c in enumerate(commits, start=1):
        try:
            files = commit_files(repo, c.sha)
        except GitError:
            files = []
        modules = sorted({module_of(fp) or "Other" for fp in files}) if files else []
        syms_here = [r for fp in files for r in syms_by_file.get(fp, [])]
        screens = sorted({
            s for r in syms_here
            for s in screens_by_symbol.get(r.symbol.name.rsplit(".", 1)[-1], set())
        })
        risk = max((r.level for r in syms_here), key=lambda l: _ORDER[l], default="LOW")
        # a merge commit's subject often carries a branch name like 'fix/unique-id' — that is
        # not itself a fix, so don't flag it (matches the recurrence merge-scrubbing rule)
        is_fix = _fixish(c.subject) and not c.subject.startswith("Merge ")
        subs.append(Ticket(
            key=f"{project}-{start_num + i}", kind="Sub-task", parent=parent.key,
            summary=c.subject, modules=modules, screens=screens, risk=risk,
            commit=c.sha, is_fix=is_fix,
        ))
    # roll the union of sub-ticket screens/modules up onto the parent
    parent.screens = sorted({s for t in subs for s in t.screens})
    parent.modules = sorted({m for t in subs for m in t.modules})
    return parent, subs
