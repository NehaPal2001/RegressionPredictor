# Tracer — session handoff (portable context)

Read this first to continue Tracer on a fresh machine / new Claude session. It captures the
whole build state, architecture, decisions, how to run, and what's next. Current as of
2026-07-19, after Phase 1.

## What Tracer is

Deterministic regression scoping for QA. Input: two git refs of a backend repo. Output: an HTML
report + a Jira-comment payload telling QA **which features/screens to test and why**.

**Core principle: deterministic where it can be, AI where it has to be.** The whole analysis
spine — diff parsing, call-graph blast radius, defect recurrence, risk scoring — is rules and
lookups. AI is confined to ONE seam: an investigator agent that reads the actual changed code to
write grounded QA test steps. **AI never decides risk or reachability.** Unplug the AI (`--no-ai`)
and the correct scope still exists deterministically.

Pipeline: `git diff (base…target) → changed symbols (Loom) → call-graph blast radius → controller
endpoints → screens → git defect-recurrence → rules risk → HTML report + Jira comment`.

## Dependency on Loom

Tracer consumes **Loom** read-only — a tree-sitter + SQLite code graph (`loom-tool` on PyPI, repo
github.com/ddevilz/loom). Loom is ONLY the static graph; Tracer owns
all git logic and never asks Loom about diffs.

- Per-repo DB at `~/.loom/projects/<repo-dir-name>.db`. Build with `loom analyze .` inside a repo.
- Tracer reads the SQLite directly (`loom_client.py`) — recursive CTE over `edges WHERE kind='CALLS'`.
- Edge kinds: `CALLS`, `CONTAINS`, `COUPLED_WITH` (git co-change), `TESTED_BY`. Node kinds
  lowercase (`method`, `function`, `class`, `file`). Node ids: `method:REPO:path:fqn`; file ids
  `file:path` (no repo segment). Bridge edges (DI interface→impl, gRPC proto) carry
  `confidence_tier='inferred'`.
- **Require loom-tool ≥ 0.7.1.** 0.7.0 fixed TS call resolution (frontend CALLS 13→1585) + DI/gRPC
  bridges; 0.7.1 fixed generic-name misbinding (`log.error` no longer binds 280 fake callers to
  `TokenResponseDto.error`). Loom edge metadata stores the call argument in `call_context` — the
  cross-repo bridge relies on this.

## Test fixtures (the real repos)

Two real work repos, used **read-only** as fixtures. **NEVER commit/push in them; never run git
commands that mutate them.** (User's standing rule.)
- `~/Downloads/sdet360ai/SDET360.ai-Server` — Spring Boot Java + Python (gRPC) backend.
- `~/Downloads/sdet360ai/SDET360.ai-Client` — Angular frontend.
- Demo pair: `ai-development → Notification`. Chosen because it re-touches a prior fix commit
  (`50b9ffca`, `TestCaseService.java:1655`), firing the defect-recurrence hero.
- The Loom DB must match the **target** checkout's line numbers. This session used a git worktree
  of the Server at `Notification` (in a session scratch dir) so the original checkout stays
  untouched. On a fresh machine: check out the target branch in a dir, `loom analyze .`, point
  `--repo` there. Scratch worktree paths from this session are gone — recreate.

## Module map (`src/tracer/`, ~1760 LOC)

| File | Job |
|------|-----|
| `cli.py` | `tracer diff <base> <target>` — orchestrates everything, exit code 2 on git error |
| `diff.py` | git diff (three-dot default) → changed files/hunks; `branch_commits`/`commit_files` for mock Jira |
| `loom_client.py` | read-only Loom queries: line→symbol, blast-radius CTE, fan-in, coupled/tested, agent nav (`search`/`callees`/`get_symbol`/`api_call_sites`) |
| `screens.py` | controller→endpoint→screen: parses `@RestController` + `@*Mapping`, module from package, roles from `@PreAuthorize`; `screens.yaml` url-prefix map |
| `history.py` | defect recurrence via `git log -L` on changed lines; keeps prior + fix-shaped commits, scrubs merges/reverts, 90-day recency |
| `risk.py` | deterministic HIGH/MED/LOW rules with cited reasons; recurrence, proto=HIGH, fan-in≥5=MED, hub-cap (deep+hub-mediated → LOW) |
| `bridge.py` | **Phase 1** cross-repo: backend endpoint → real Angular screen. Suffix-matches endpoint paths, walks client TS graph to component, names screen from `app.routes.ts` |
| `agent.py` | **the one AI seam** — LangGraph (`langchain.agents.create_agent`) investigator. Loom-backed tools (`loom_search`/`loom_callees`/`read_symbol`/`grep_repo`); reads actual code live, writes grounded QA steps. Groq `llama-3.3-70b-versatile` via `langchain-groq` |
| `llm.py` | Groq key loading (`load_key` from env/.env) — used by cli to feed the agent |
| `jira_mock.py` | mock ticket tree: parent Story + sub-tasks from real branch commits, mapped to modules/screens, FIX-flagged |
| `report.py` | HTML report + `jira_comment` payload (grounded agent notes + cited facts, template fallback with `--no-ai`) |

## How to run

```bash
cd tracer
uv sync                      # installs deps incl. loom-tool, langchain, langchain-groq, openai
uv run pytest tests/ -q      # 21 tests, all deterministic (synthetic sqlite, no network)

# backend-only (instant, no AI):
uv run tracer diff ai-development Notification --repo <server-repo> --out report.html --no-ai

# full: agent reads code + cross-repo bridge to Angular screens:
uv run tracer diff ai-development Notification \
  --repo <server-repo> \
  --client-repo ~/Downloads/sdet360ai/SDET360.ai-Client \
  --out report.html --investigate 1
open report.html
```

First run on a repo auto-runs `loom analyze` if no DB (see `ensure_graph` in cli.py).

### Flags
| flag | effect |
|------|--------|
| `--client-repo PATH` | Phase 1 bridge → real Angular screens (omit = backend-only) |
| `--investigate N` | top-N screens the agent reads code for (default 2; **Groq free tier = 12k tokens/min**, use 1–2) |
| `--no-ai` | skip the agent entirely (pure deterministic, instant, stable output) |
| `--two-dot` | raw tip-to-tip diff instead of merge-base |
| `--reindex` | rebuild the Loom graph(s) first |
| `--db` / `--client-db` | override Loom DB paths |

### Config
Groq key: `tracer/.env` → `GROQ_API_KEY=gsk_...` (gitignored). Without it, the agent is skipped and
the deterministic report still ships. Uses the **openai SDK / langchain-groq**, not raw urllib
(Groq 403s the default urllib User-Agent).

## Report sections (order)
1. **Jira comment preview** (top) — what gets posted: cited recurrence facts + grounded WHAT-TO-TEST
   from the agent (varies per run because it reads live code). `--no-ai` → deterministic template.
2. Defect recurrence hero · 3. Mock Jira tickets · 4. Frontend screens (cross-repo) · 5. Features ·
6. Changed symbols · 7. Co-change · 8. Linked tests · 9. Blind spots.

## Key decisions (and why)
- **AI = investigator agent, not cached summaries.** Loom `store_understanding` rejected — cached
  summaries rot on a fix branch. Agent reads live code every run.
- **Agent asks Loom "which code", then reads.** Loom-backed tools give locations; agent reads the
  real body. Bounded by the blast radius, not free repo crawl.
- **LangGraph `create_react_agent` is deprecated** → migrated to `langchain.agents.create_agent`
  (langchain ≥1.0). Same compiled graph.
- **Cross-repo mechanism A (pure Loom metadata) is enough.** Measured **90% match rate** on the
  real SDET pair — mechanism B (source-fallback for helper-built URLs) NOT needed. Endpoint strings
  come from Loom's `call_context` edge metadata; suffix-match absorbs the `/api` prefix the client
  omits.
- **Reachability stays deterministic; cross-repo hops labeled `inferred`.** LLM never decides it.
- **fan-in counts distinct caller files**, not raw edges (mitigates any residual Loom misbinding).
- **LOW = "defer to periodic full regression pass", never "skip"** (static selection ~3% unsafe).
- **Every screen that calls a changed endpoint is shown** (no dedup) — that's the QA worklist.

## Specs / docs
- `docs/superpowers/specs/2026-07-14-tracer-v0.2-design.md` — Phase 0/1/2 plan.
- `docs/superpowers/specs/2026-07-19-tracer-phase1-bridge-design.md` — Phase 1 (implemented, 90%).
- `docs/` is gitignored — copy it manually when moving machines, or un-ignore.
- `ci/tracer-regression.yml` — GitHub Action template (Phase 2, not yet live).
- Prior-art research (Google TAP/Meta PTS/Launchable/Pact/Sealights lessons) informed the risk
  rules — summarized in the v0.2 spec.

## Open / next (Phase 2, not built)
- **CI live** — harden `ci/tracer-regression.yml`, PR comment. No Jira posting yet (parked).
- **Backtest simulator** `tracer backtest --last M` — "would have flagged N of last M regressions."
- **Real Jira post** — needs `JIRA_URL/EMAIL/API_TOKEN`; the comment text already exists.
- **Seam 1** (`--tests` fuzzy test-description → symbol) — stubbed, not wired.
- Known limit: Groq free-tier 12k TPM caps `--investigate` to 1–2 screens. Paid tier lifts it.

## Loom feedback status (`~/Downloads/sdet360ai/LOOM-FEEDBACK.md`)
Fixed + validated this session: #1 (TS calls), #2 (DI edges), #8 (generic-name misbind). Open: #9
(selenium report bundles indexed as code — needs `.loomignore`).
