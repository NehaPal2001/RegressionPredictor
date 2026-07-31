"""Tracer owns the diff: git refs -> changed backend files + line ranges.

Three-dot semantics by default: scope = what `target` introduced since it forked
from `base` (diff from merge-base to target). --two-dot compares raw tips.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

BACKEND_GLOBS = ("*.java", "*.py", "*.proto")
# Generated code is never a change *source* (the .proto itself is the source of truth).
GENERATED = re.compile(r"(_pb2\w*\.py$|/generated/|/grpc/generated/)")

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class GitError(RuntimeError):
    pass


def _git(repo: str, *args: str) -> str:
    log.debug("git -C %s %s", repo, " ".join(args))
    r = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        log.debug("git %s exited %d: %s", " ".join(args), r.returncode, r.stderr.strip()[:300])
        raise GitError(r.stderr.strip() or f"git {' '.join(args)} failed")
    return r.stdout


@dataclass
class FileChange:
    path: str
    ranges: list[tuple[int, int]] = field(default_factory=list)  # target-side line ranges
    deletions: int = 0  # count of pure-deletion hunks (old code removed)


@dataclass
class DiffScope:
    base_ref: str  # resolved base (merge-base unless --two-dot)
    target_ref: str
    files: list[FileChange]
    proto_changed: list[str]
    total_files_changed: int  # all files, not just backend


def merge_base(repo: str, base: str, target: str) -> str:
    return _git(repo, "merge-base", base, target).strip()


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str
    date: str
    author_email: str = ""


def branch_commits(repo: str, base: str, target: str, two_dot: bool = False) -> list[Commit]:
    """Commits unique to target since it diverged from base — the branch's own work items."""
    resolved_base = base if two_dot else merge_base(repo, base, target)
    out = _git(repo, "log", f"{resolved_base}..{target}", "--format=%H%x09%ae%x09%s%x09%cs")
    commits = []
    for line in out.strip().splitlines():
        if not line.strip():
            continue
        sha, author_email, subject, date = line.split("\t", 3)
        commits.append(Commit(sha[:8], subject, date, author_email))
    log.debug("branch_commits: %d commit(s) in %s..%s", len(commits), resolved_base, target)
    return commits


def commit_files(repo: str, sha: str) -> list[str]:
    """Backend files a single commit touched (for mapping a commit → modules/screens)."""
    out = _git(repo, "show", "--name-only", "--format=", sha, "--", *BACKEND_GLOBS)
    return [f for f in out.splitlines() if f.strip() and not GENERATED.search(f)]


def parse_unified(text: str) -> list[FileChange]:
    """Parse `git diff -U0` output into per-file target-side line ranges."""
    files: list[FileChange] = []
    cur: FileChange | None = None
    for line in text.splitlines():
        if line.startswith("diff --git "):
            # `b/` side is the target path
            path = line.split(" b/", 1)[1]
            cur = FileChange(path)
            files.append(cur)
        elif cur is not None and (m := HUNK.match(line)):
            new_start, new_count = int(m.group(3)), int(m.group(4) or "1")
            if new_count == 0:
                cur.deletions += 1
            else:
                cur.ranges.append((new_start, new_start + new_count - 1))
    return [f for f in files if f.ranges or f.deletions]


def changed_backend(repo: str, base: str, target: str, two_dot: bool = False) -> DiffScope:
    resolved_base = base if two_dot else merge_base(repo, base, target)
    log.debug("changed_backend: %s..%s (%s), base resolved to %s",
              base, target, "two-dot" if two_dot else "three-dot", resolved_base)
    all_changed = _git(repo, "diff", "--name-only", resolved_base, target).splitlines()
    raw = _git(repo, "diff", "-U0", "--no-color", resolved_base, target, "--", *BACKEND_GLOBS)
    parsed = parse_unified(raw)
    files = [f for f in parsed if not GENERATED.search(f.path)]
    proto = [f.path for f in files if f.path.endswith(".proto")]
    log.debug("changed_backend: %d file(s) changed overall, %d backend file(s) "
              "(%d generated skipped), %d proto file(s)",
              len(all_changed), len(files), len(parsed) - len(files), len(proto))
    if not files:
        log.warning("no backend files changed between %s and %s — scope will be empty",
                    resolved_base, target)
    return DiffScope(resolved_base, target, files, proto, len(all_changed))
