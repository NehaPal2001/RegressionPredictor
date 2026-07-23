"""Investigator agent (LangGraph): reads the ACTUAL code live to write grounded
regression steps — never a cached summary.

The deterministic spine decides WHAT is in scope. This agent then navigates Loom
(ask Loom "which code do I need" → callees/search → node + location) and reads the
real bodies live, following validation/error paths, before writing QA test steps
grounded in what the code does right now. Bounded to the repo, read-only.

Structure is a LangChain agent (langchain.agents.create_agent) — a compiled
LangGraph graph with a model node and a tools node joined by conditional edges.
Unplug it → the deterministic scope still stands; only the rich prose disappears.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from langchain.agents import create_agent
from langchain_core.tools import tool
from langchain_groq import ChatGroq

from .loom_client import LoomClient

MODEL = "llama-3.3-70b-versatile"
# Groq free tier is 12k tokens/MINUTE and the react agent re-sends full history each step,
# so total accumulation must stay under that. Bound tool rounds low + keep each read small:
# ~4 reads * ~1.4k + scope ~1.5k + system ~0.6k stays comfortably under 12k.
RECURSION_LIMIT = 10    # ~4 tool round-trips then forced answer
MAX_READ_LINES = 110
MAX_GREP_HITS = 15
MAX_TOOL_CHARS = 3200   # cap a single tool result fed back to the model

SYSTEM = """You are a QA regression analyst investigating a code change for MANUAL testers.
You are given a deterministic scope (screens, changed symbols with Loom node ids, prior fix
history). The scope is GROUND TRUTH — never change a risk level, invent an endpoint, or claim
a screen not listed.

Loom is your map. To understand a symbol: call loom_callees to see what it calls, or
loom_search to locate a symbol by name; both return Loom node ids. Then call read_symbol on a
node id to read its ACTUAL current source. Read before you write — never guess what a function
does when you can read it. Follow the real paths: read the controller method, see what it
validates or calls, look those up in Loom, read them, then write the error cases from what you
actually saw. Use grep_repo only when Loom has no node for something you need.

When you have read enough, output ONLY the final regression notes as plain text. For each
affected screen, highest risk first:
- MODULE TOUCHED: the feature area
- NAVIGATE TO: the screen/page a tester opens
- LOGIN AS: the role required (from the scope; if none, "any authenticated user")
- HAPPY PATH: the exact steps and the expected visible result
- ERROR CASES: concrete failure inputs you found in the code (bad payload, missing field,
  wrong role, nonexistent id) and the expected handling
- IF PRIOR FIX: prefix a "Likely scenario (inferred from the fix commit, not the original bug
  report):" line reconstructing the user-facing bug to re-test hard
- If reachability is 'inferred', say the path is not fully confirmed — QA should verify the
  screen is actually affected first.
No commit hashes, no file paths, no markdown headers in the final answer."""


def _fmt(syms) -> str:
    return "\n".join(f"{s.id}  ({s.path}:{s.start_line}-{s.end_line})" for s in syms) or "(none)"


def _build_tools(lc: LoomClient, root: Path):
    root = root.resolve()

    @tool
    def loom_search(query: str) -> str:
        """Find Loom code-symbol node ids by name substring (ask Loom where a symbol is)."""
        return _fmt(lc.search(query))

    @tool
    def loom_callees(node_id: str) -> str:
        """List the Loom node ids this symbol calls — follow the graph downward."""
        return _fmt(lc.callees(node_id))

    @tool
    def read_symbol(node_id: str) -> str:
        """Read the actual current source of a Loom node id (Loom gives the location)."""
        sym = lc.get_symbol(node_id)
        if sym is None:
            return f"(no Loom node {node_id})"
        fp = (root / sym.path).resolve()
        if not str(fp).startswith(str(root)) or not fp.is_file():
            return f"(cannot read {sym.path})"
        lines = fp.read_text(errors="replace").splitlines()
        start = max(1, sym.start_line)
        end = min(len(lines), max(start, sym.end_line))
        if end - start > MAX_READ_LINES:
            end = start + MAX_READ_LINES
        body = "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))
        return f"{sym.name} @ {sym.path}:{start}-{end}\n{body}"[:MAX_TOOL_CHARS] or "(empty)"

    @tool
    def grep_repo(pattern: str) -> str:
        """Search java/py source for a regex — only when Loom has no node for what you need."""
        import re
        try:
            re.compile(pattern)
        except re.error:
            return "(invalid regex)"
        r = subprocess.run(
            ["grep", "-rniE", pattern, "--include=*.java", "--include=*.py", "."],
            cwd=root, capture_output=True, text=True, timeout=30,
        )
        return "\n".join(r.stdout.splitlines()[:MAX_GREP_HITS]) or "(no matches)"

    return [loom_search, loom_callees, read_symbol, grep_repo]


def investigate(scope: dict, lc: LoomClient, repo_root: str | Path, key: str) -> str:
    """Run the LangGraph react agent; return grounded regression notes."""
    llm = ChatGroq(model=MODEL, api_key=key, temperature=0.2)
    agent = create_agent(llm, _build_tools(lc, Path(repo_root)), system_prompt=SYSTEM)
    result = agent.invoke(
        {"messages": [("user", "Deterministic scope to investigate:\n" + json.dumps(scope, indent=1))]},
        config={"recursion_limit": RECURSION_LIMIT},
    )
    return (result["messages"][-1].content or "").strip()


def try_investigate(scope: dict, lc: LoomClient, repo_root: str | Path, key: str | None) -> tuple[str | None, str]:
    """(notes, status). Never raises — the deterministic report must always ship."""
    if not key:
        return None, "no GROQ_API_KEY — skipped AI investigation (deterministic scope is complete)"
    try:
        return investigate(scope, lc, repo_root, key), "ok"
    except Exception as e:  # LangGraph/Groq/recursion — never sink the deterministic report
        return None, f"AI investigation failed ({type(e).__name__}: {e}) — deterministic scope unaffected"