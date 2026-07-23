"""Tracer owns the diff: git refs -> changed backend files + line ranges.

Three-dot semantics by default: scope = what `target` introduced since it forked
from `base` (diff from merge-base to target). --two-dot compares raw tips.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field

BACKEND_GLOBS = ("*.java", "*.py", "*.proto")
# Generated code is never a change *source* (the .proto itself is the source of truth).
GENERATED = re.compile(r"(_pb2\w*\.py$|/generated/|/grpc/generated/)")

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class GitError(RuntimeError):
    pass


def _git(repo: str, *args: str) -> str:
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if r.returncode != 0:
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


def branch_commits(repo: str, base: str, target: str, two_dot: bool = False) -> list[Commit]:
    """Commits unique to target since it diverged from base — the branch's own work items."""
    resolved_base = base if two_dot else merge_base(repo, base, target)
    out = _git(repo, "log", f"{resolved_base}..{target}", "--format=%H%x09%s%x09%cs")
    commits = []
    for line in out.strip().splitlines():
        sha, subject, date = line.split("\t", 2)
        commits.append(Commit(sha[:8], subject, date))
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
    all_changed = _git(repo, "diff", "--name-only", resolved_base, target).splitlines()
    raw = _git(repo, "diff", "-U0", "--no-color", resolved_base, target, "--", *BACKEND_GLOBS)
    files = [f for f in parse_unified(raw) if not GENERATED.search(f.path)]
    proto = [f.path for f in files if f.path.endswith(".proto")]
    return DiffScope(resolved_base, target, files, proto, len(all_changed))
