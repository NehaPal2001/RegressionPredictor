# Tracer

**Deterministic regression scoping + QA test predictor.** Give Tracer a git diff; it tells your QA
team *which features to test and why* — grounded in the call graph, git history, and past bug fixes.
Phase 2a extends this into a two-stage pipeline that selects test cases from SDET360 TCM using
Jira requirement coverage.

> **The principle:** *Deterministic where it can be, AI where it has to be.*
> The entire analysis spine — parsing the diff, walking the call graph, mining git history,
> detecting defect recurrence, gap detection, test case selection — is rules and lookups.
> AI is confined to **two seams**: matching fuzzy test descriptions to code, and writing
> the final human summary. AI never decides risk.

---

## What it does

### Stage 1 — `tracer diff` (Regression Scope)

When a developer changes code, QA has to answer: *what could this break, and what should we test?*

1. **Reads the diff** → finds the exact functions that changed (including deletions).
2. **Walks the call graph** (via [Loom](https://github.com/ddevilz/loom)) → finds every feature/screen that transitively depends on the changed code.
3. **Mines git history** → detects defect recurrence (re-touching previously-fixed lines).
4. **Scores risk** deterministically → HIGH / MEDIUM / LOW with a specific, cite-able reason.
5. **Writes `scope.json`** — a versioned contract passed to Stage 2.
6. **Renders HTML report** with full blast radius and AI-narrated QA notes.

### Stage 2 — `tracer predict` (QA Regression Predictor)

Takes `scope.json` from Stage 1 and produces `test_cases.json` for SDET360 release cycle creation.

1. **Gap detection** — for each commit in scope, searches Jira (`text ~ "commit_hash"`) to find linked stories. Commits with no story → **uncovered** → SMTP email alert to commit authors + team.
2. **Requirement linking** — fetches full Jira story details and open defects for covered commits.
3. **Test case selection** — pulls test cases from SDET360 TCM filtered by `jiraStoryKey`. Falls back to all ACTIVE cases if none are linked.

---

## Install

```bash
git clone git@github.com:ddevilz/tracer.git
cd tracer
uv venv && uv pip install -e .
```

Requires Python 3.10+. Depends on [`loom-tool`](https://github.com/ddevilz/loom) for the code graph.

---

## Configure

Copy `.env.example` to `.env` and fill in:

```env
# AI (Stage 1 investigator agent)
GROQ_API_KEY=gsk_...

# Jira — read-only, Basic auth
JIRA_BASE_URL=https://sdettech-tea.atlassian.net
JIRA_EMAIL=you@company.com
JIRA_API_TOKEN=your_jira_api_token        # https://id.atlassian.com/manage-profile/security/api-tokens

# TCM (SDET360 qa.sdet360.ai) — cookie auth
# Get from browser: F12 → Application → Cookies on qa.sdet360.ai
TCM_SESSION=<session cookie value>
TCM_PROJECT_SESSION=<project_session cookie value>
TCM_REFRESH_TOKEN=<refresh token from localStorage>
TCM_VERTICAL_ID=<UUID from TCM API URL>
TCM_PROJECT_KEY=AS360                     # your TCM project key

# SMTP gap alert (optional — use --no-alert to skip)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@company.com
SMTP_PASSWORD=your_app_password
SMTP_FROM=tracer@yourorg.com
ALERT_EMAILS=qa@yourorg.com,lead@yourorg.com
```

`.env` is gitignored — never commit it.

> **How to get TCM cookies:**
> 1. Log into [qa.sdet360.ai](https://qa.sdet360.ai) in Chrome
> 2. Press **F12** → **Application** tab → **Cookies**
> 3. Copy `session` → `TCM_SESSION`, `project_session` → `TCM_PROJECT_SESSION`
> 4. Check **Local Storage** for `refreshToken` → `TCM_REFRESH_TOKEN`
> 5. Open any TCM API call in **Network** tab → copy the UUID from the URL → `TCM_VERTICAL_ID`

> **How to link commits to Jira stories (required for gap detection):**
> In each Jira story's description or a comment, include the 7-character git commit hash,
> e.g. `Commit: 8a0c5b4`. Tracer will find the story via `text ~ "8a0c5b4"` JQL.

---

## Usage

### Stage 1 — Generate scope from a branch diff

```bash
uv run tracer diff <base> <target> \
  --repo "C:\path\to\your\repo" \
  --scope scope.json \
  --no-ai
```

**With full AI investigation:**
```bash
uv run tracer diff main feature/my-branch \
  --repo "C:\path\to\your\repo" \
  --scope scope.json \
  --out tracer-report.html
```

**With cross-repo Angular bridge:**
```bash
uv run tracer diff main feature/my-branch \
  --repo "C:\path\to\SDET360.ai-Server" \
  --client-repo "C:\path\to\SDET360.ai-Client" \
  --scope scope.json \
  --out tracer-report.html
```

### Stage 2 — Predict test cases from scope

```bash
uv run tracer predict \
  --scope scope.json \
  --out test_cases.json \
  --env .env \
  --jira-project REG \
  --no-alert
```

**With SMTP gap alert enabled:**
```bash
uv run tracer predict \
  --scope scope.json \
  --out test_cases.json \
  --env .env \
  --jira-project REG
```

**Override TCM project:**
```bash
uv run tracer predict \
  --scope scope.json \
  --out test_cases.json \
  --env .env \
  --jira-project REG \
  --tcm-project AS360 \
  --no-alert
```

---

## CLI Reference

### `tracer diff`

| Flag | Default | Meaning |
|------|---------|---------|
| `base` | *(required)* | Baseline git ref (e.g. `main`) |
| `target` | *(required)* | Branch under test |
| `--repo` | `.` | Path to the git repo |
| `--scope PATH` | — | Write `scope.json` for use by `tracer predict` |
| `--out PATH` | `tracer-report.html` | HTML report output path |
| `--no-ai` | — | Skip AI investigation (faster, offline) |
| `--investigate N` | `2` | How many top-risk screens the AI reads code for |
| `--max-depth N` | `6` | Blast-radius depth cap |
| `--two-dot` | — | Raw tip-to-tip diff (skip merge-base) |
| `--reindex` | — | Rebuild the Loom graph before analyzing |
| `--db PATH` | `~/.loom/projects/<repo>.db` | Override the Loom DB path |
| `--client-repo PATH` | — | Angular client repo for cross-repo bridge |
| `--client-db PATH` | — | Client Loom DB path |

### `tracer predict`

| Flag | Default | Meaning |
|------|---------|---------|
| `--scope PATH` | *(required)* | `scope.json` from `tracer diff --scope` |
| `--out PATH` | `test_cases.json` | Output path for SDET360 release cycle |
| `--env PATH` | — | Path to `.env` file |
| `--jira-project KEY` | `REG` | Jira project key for gap detection |
| `--tcm-project KEY` | from `.env` | TCM project key override |
| `--tcm-vertical UUID` | from `.env` | TCM vertical UUID override |
| `--no-alert` | — | Suppress SMTP gap alert email |
| `--no-predict-ai` | — | Reserved for Phase 2c LangGraph selector |

Exit codes: `0` success, `2` error (bad ref / missing scope file).

---

## Output files

### `scope.json` (Stage 1 output)

```json
{
  "version": 1,
  "base": "main",
  "target": "feature/my-branch",
  "commits": [
    { "hash": "8a0c5b4d", "author_email": "dev@co.com", "message": "..." }
  ],
  "changed_symbols": [
    { "id": "method:...", "name": "validateToken", "path": "src/Auth.java", "risk": "HIGH", "fan_in": 7 }
  ],
  "affected_screens": ["Login", "Dashboard"]
}
```

### `test_cases.json` (Stage 2 output)

```json
{
  "version": 1,
  "covered_commits": [{ "hash": "8a0c5b4d", "jira_keys": ["REG-20", "REG-21"] }],
  "uncovered_commits": [{ "hash": "d1125cb9", "author_email": "dev@co.com", "message": "..." }],
  "jira_stories": [{ "key": "REG-20", "summary": "...", "status": "To Do", "priority": "High" }],
  "jira_defects": [{ "key": "REG-28", "summary": "...", "status": "To Do" }],
  "test_cases": [
    {
      "unique_id": "TC-0042",
      "title": "Verify suite execution with runtime chaining",
      "priority": "High",
      "jira_story_key": "REG-20",
      "selection_reason": "linked",
      "confidence": "high"
    }
  ]
}
```

`selection_reason` is `"linked"` when selected via Jira story → TCM link, or `"all"` when falling back to all ACTIVE cases.

---

## Workflow: linking commits to Jira for gap detection

Tracer searches Jira for commit hashes using `text ~ "commit_hash"` JQL. To make a commit
"covered", paste its 7-character hash in any Jira story comment:

```
branch: feature/api-test-suite
Commit: 8a0c5b4
url: https://github.com/your-org/repo/commit/8a0c5b4d...
message: "implement API test suite execution with runtime chaining"
```

Uncovered commits trigger a gap alert email listing the commit hash, author email, and message.

---

## Development

```bash
uv pip install -e ".[dev]"

# Run all tests (45 total — no network, no AI)
uv run pytest tests/ -v

# Run just the Phase 2a predict tests
uv run pytest tests/test_predict.py -v

# Lint
ruff check src/ tests/
```

### Test coverage

| Test file | What it covers | Count |
|-----------|----------------|-------|
| `tests/test_spine.py` | diff parsing, risk scoring, blast radius, screens, bridge | 21 |
| `tests/test_predict.py` | TCM client, Jira client, gap detector, mailer, predict CLI | 24 |

---

## Architecture

```
tracer diff                          tracer predict
────────────────────────────────     ──────────────────────────────────────
git diff + Loom call graph           scope.json
  → changed symbols                    → gap_detector.py  (Jira text~)
  → blast radius (screens)             → mailer.py        (SMTP alert)
  → defect recurrence                  → jira_client.py   (fetch stories)
  → risk scoring                       → tcm_client.py    (fetch test cases)
  → AI investigation (opt)             → test_cases.json
  → HTML report
  → scope.json  ──────────────────────►
```

See [`docs/superpowers/specs/2026-07-24-tracer-phase2-predict-design.md`](docs/superpowers/specs/2026-07-24-tracer-phase2-predict-design.md) for the full Phase 2 design spec.

---

## License

MIT
