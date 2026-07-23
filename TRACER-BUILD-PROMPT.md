# Claude Code kickoff prompt — build Tracer v0.1

Copy everything in the block below into Claude Code, run from the root that contains
`SDET360.ai-Server/` and `SDET360.ai-Client/` (the same folder as `CLAUDE.md`).

---

You are helping me build **Tracer**, a deterministic regression-scoping tool for QA. Before writing any
code, read these files in this repo — they are the result of a long validation session and contain the
ground truth for every decision:

- `CLAUDE.md` — project memory: repo layout, the Loom-vs-Graphify findings, and the Tracer responsibility boundary.
- `loom-vs-graphify-report.md` — coverage comparison (files/nodes by language).
- `loom-vs-graphify-validation-report.md` — call-graph correctness, ground-truthed vs grep.
- `LOOM-FEEDBACK.md` — known Loom gaps are fixed

## What Tracer does
Given two git refs, it tells QA which features/screens to test and why. Flow:
`git diff (base…target) → changed symbols → call-graph blast-radius → screens → git defect-recurrence →
risk score → a readable report`.

## Non-negotiable principle
**Deterministic where it can be, AI where it has to be.** The whole analysis spine is rules and lookups.
AI is confined to TWO seams only: (1) matching a fuzzy test description to a code symbol, (2) writing the
final human-readable summary. **AI never decides risk or reachability.** For v0.1 you may stub both seams.

## Responsibility boundary (critical — do not violate)
- **Tracer owns ALL git logic**: parsing the diff, mapping hunks → changed symbols, and git-history /
  defect-recurrence (`git log -L`, blame). Tracer does the diffing.
- **Loom is ONLY the static graph**, consumed read-only. It's a queryable SQLite symbol index (with file +
  line ranges) + call edges (`CALLS`), plus `COUPLED_WITH` (precomputed git co-change) and `TESTED_BY`.
  Loom answers "given a symbol → callers / blast-radius." It never sees a diff.
- Loom DB path: `~/.loom/projects/SDET360.ai-Server.db`. Rebuild with `loom analyze .` inside the repo.
  Query it directly with SQL (recursive CTE over `edges WHERE kind='CALLS'`); do NOT shell out to
  `loom callers <name>` — its bare-name CLI lookup is unreliable (see LOOM-FEEDBACK.md #3).

## Validated facts you must build around
- Loom's **Java and Python** call graphs are CORRECT (verified 100% precision/recall on real methods).
  Backend scoping is trustworthy.
- Loom's **TypeScript/Angular** call graph is BROKEN (13 edges for the whole frontend). **v0.1 is
  backend-only (Java + Python). Do not attempt frontend scoping.**
- Blast-radius stops at the service layer because THREE edges are missing from every static tool. Tracer
  must supply them as deterministic resolvers that run ON Loom's graph:
  1. **interface → impl** (Spring DI): a call to an interface method also reaches the `implements` class's
     matching method. Needed to get from controller to service impl.
  2. **proto-bridge** (gRPC Java↔Python): parse `SDET360.ai-Server/proto/ai_service.proto`, join by RPC
     name — Java stub `generateResponse` == proto/Python `GenerateResponse`. Synthesize edges:
     Java stub-call-site → [RPC node] → Python `AiServiceServicer.<Rpc>` (in `ai-service/app/grpc_server.py`).
     Also map wrapper `FastApiGrpcClient` methods. Treat any `ai_service.proto` change as HIGH risk.
     Ignore generated files (`*_pb2*.py`, `grpc/generated/*.java`) as change sources.
  3. **controller → endpoint/screen**: walk up to the `@RestController` method, read its
     `@GetMapping`/`@PostMapping` path, map path → feature/screen name via a hand-maintained YAML.

## v0.1 goal — a DEMO
CLI: `tracer diff <base> <target> [--two-dot]`
- First arg = baseline, second arg = branch under test. Scope = **target's** changes relative to base.
- Default to **three-dot** semantics (`git merge-base base target`, then diff that base..target) — this
  scopes only what the target branch introduced. `--two-dot` = raw tip-to-tip for non-descendant pairs
  (e.g. staging vs release).
- Works on ANY branch pair, not just main. Repo has `main` plus feature branches (JiraFilters, Notification,
  ai-development, etc.).
- Output: an **HTML regression report** (not a real Jira post for the demo — render a "Jira comment preview"
  panel). Include: changed symbols, affected features/screens, per-feature risk (HIGH/MED/LOW) with a
  cited reason, also make sure we share how it should be tested in regression that feature what needs to be tested how its happy path and bad bath, and a **defect-recurrence** callout ("re-touches code fixed in commit X for bug Y").
- The defect-recurrence line is the hero moment — make it prominent. Every risk score must cite its reason;
  nothing is a black box.
- When a path can't be completed (interface/proto hop), label the reachability **"inferred," not
  "confirmed."** Honest uncertainty over confident wrong answers.

## Build order (depth-first, de-risk the scary hop first)
1. `tracer/loom_client.py` — read-only graph queries (resolve hunk line-range → symbol; recursive
   blast-radius; fetch COUPLED_WITH + TESTED_BY for a symbol).
2. `tracer/diff.py` — `git diff` (three-dot default) → changed files/hunks → changed symbols via loom_client.
3. `tracer/resolvers/` — interface→impl, proto_bridge, controller_screen. **Start here for real value.**
4. `tracer/history.py` — `git log -L` defect recurrence on changed lines.
5. `tracer/risk.py` — rules table → HIGH/MED/LOW with reasons.
6. `tracer/report.py` — HTML report + Jira-comment-preview panel.
7. Seams (stub-then-fill): `search_code` FTS for test-desc→symbol; a single templated LLM call for prose.

Language: **Python** (so it reads Loom's SQLite directly, no IPC).

First milestone to prove the concept: `tracer diff main <feature-branch>` prints a correct list of affected
backend features with honest confidence labels for one real branch pair in `SDET360.ai-Server`. Get that
path working end to end before polishing the HTML or wiring the AI seams.

Ask me before picking the demo branch pair — we want one whose changed lines overlap a prior `fix:` commit
so the defect-recurrence hero moment fires.
