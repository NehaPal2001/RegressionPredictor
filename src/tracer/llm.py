"""AI seam 2: narrate the deterministic scope as QA regression notes (Groq).

The LLM NARRATES, never decides. Risk, reachability, features, and bug history
were all computed upstream by rules; the model only turns that JSON into
readable QA prose. Unplug it (no GROQ_API_KEY / network error) and the report
still carries the complete deterministic scope.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from openai import OpenAI, OpenAIError

MODEL = "llama-3.3-70b-versatile"
BASE_URL = "https://api.groq.com/openai/v1"

SYSTEM = (
    "You write regression-testing notes for MANUAL QA testers — not developers. Use ONLY facts "
    "present in the input JSON: never invent features, endpoints, code, or bug history, and "
    "never change a risk level or reachability label.\n"
    "\n"
    "The reader cannot act on a commit hash or a raw commit subject. Never print one. A fix "
    "commit's subject (e.g. 'fix merging issue X, added missed endpoints, update Y') is developer "
    "shorthand for several bundled changes, not a bug report — do not quote it verbatim. Instead, "
    "reconstruct the most plausible USER-FACING scenario that subject implies: what the tester "
    "would click, type, or submit, and what visibly went wrong. Prefix that reconstruction with "
    "'Likely scenario (inferred from the fix commit, not the original bug report):' so it is never "
    "mistaken for a confirmed repro step — you are guessing at user-facing behavior from developer "
    "text, and must say so.\n"
    "\n"
    "For each feature, highest risk first, in plain QA language:\n"
    "- what to test: the happy path, then the failure paths\n"
    "- if the data cites a prior fix, the reconstructed likely scenario to re-test hard\n"
    "- reachability: if 'inferred' (crosses a DI/gRPC bridge), say the path is not fully confirmed "
    "so QA should double check the feature is actually affected before spending time on it\n"
    "\n"
    "Short bullet lines, plain text, no markdown headers, no commit hashes, no file paths."
)


def load_key(*env_files: str | Path) -> str | None:
    if key := os.environ.get("GROQ_API_KEY"):
        return key
    for f in env_files:
        p = Path(f)
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            k, _, v = line.strip().partition("=")
            if k == "GROQ_API_KEY" and v:
                return v.strip().strip("\"'")
    return None


def qa_notes(scope: dict, key: str, timeout: int = 60) -> str:
    client = OpenAI(api_key=key, base_url=BASE_URL, timeout=timeout)
    r = client.chat.completions.create(
        model=MODEL,
        temperature=0.2,
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(scope, indent=1)},
        ],
    )
    return (r.choices[0].message.content or "").strip()


def try_qa_notes(scope: dict, *env_files: str | Path) -> tuple[str | None, str]:
    """(notes, status). Never raises — the deterministic report must always ship."""
    key = load_key(*env_files)
    if not key:
        return None, "no GROQ_API_KEY — skipped AI narration (deterministic scope is complete)"
    try:
        return qa_notes(scope, key), "ok"
    except OpenAIError as e:
        return None, f"AI narration failed ({e}) — deterministic scope unaffected"
