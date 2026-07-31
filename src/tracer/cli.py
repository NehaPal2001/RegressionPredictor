"""RegressIQ diff <base> <target> — regression scope for what `target` introduced.

Exit codes: 0 success, 2 git/ref error (never a silent false "all clear").
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import subprocess
import sys
from pathlib import Path

from . import agent as agentmod
from . import bridge as bridgemod
from . import diff as diffmod
from . import gap_detector as gd
from . import history, jira_mock, risk
from . import mailer as mailermod
from . import reporter as reportermod
from . import tcm_client as tcm
from . import test_plan
from .jira_client import JiraClient
from .llm_config import get_llm_config
from . import logging_setup as logmod
from .logging_setup import attach_run_log, close_run_log, setup_logging, workflow
from .loom_client import LoomClient, Reach
from .predict_cfg import load_predict_config
from .report import Feature, jira_comment, render_html
from .screens import EndpointIndex, load_screen_map
from .semantic_matcher import enrich_layer3, enrich_layer4

log = logging.getLogger(__name__)


def scope_json(scope, changed, feats, recs) -> dict:
    """The complete deterministic scope as data — what the investigator agent reads.

    Includes Loom node ids so the agent has entry points to navigate from (loom_callees /
    read_symbol) rather than guessing symbol locations.
    """
    return {
        "base": scope.base_ref,
        "target": scope.target_ref,
        "features": [
            {
                "screen": f.screen,
                "risk": f.level,
                "why": f.reasons,
                "reachability": f.confidence,
                "hops_from_change": f.min_depth,
                "module": next((e.module for e in f.endpoints if e.module), ""),
                "roles": sorted({r for e in f.endpoints for r in e.roles}),
                "endpoints": sorted({f"{e.verb} {e.url}" for e in f.endpoints}),
                "endpoint_node_ids": sorted({e.node_id for e in f.endpoints}),
                "reached_via_changed_symbols": f.via,
            }
            for f in feats
        ],
        "changed_symbols": [
            {"name": r.symbol.name.rsplit(".", 1)[-1], "node_id": r.symbol.id, "file": r.symbol.path,
             "lines": [r.symbol.start_line, r.symbol.end_line], "risk": r.level, "why": r.reasons}
            for r in changed
        ],
        "defect_recurrence": [
            {"file": r.path, "lines": [r.start, r.end], "fixed_by_commit": r.sha,
             "fix_subject": r.subject, "fix_date": r.date}
            for r in recs
        ],
    }

ORDER = {"HIGH": 2, "MEDIUM": 1, "LOW": 0}


def agent_scope(scope, changed, feats, recs, top: int = 2) -> dict:
    """Lean scope for the investigator agent — only the top-risk screens, minimal fields.

    The agent reads real code (token-heavy) and Groq's free tier caps tokens/minute, so we
    hand it only the highest-risk screens to investigate and drop verbose reason strings
    (those are already in the deterministic report). Node ids stay — the agent needs entry
    points to navigate Loom from.
    """
    ranked = sorted(feats, key=lambda f: (-ORDER[f.level], f.min_depth))[:top]
    return {
        "base": scope.base_ref,
        "target": scope.target_ref,
        "screens_to_investigate": [
            {
                "screen": f.screen,
                "risk": f.level,
                "reachability": f.confidence,
                "module": next((e.module for e in f.endpoints if e.module), ""),
                "roles": sorted({r for e in f.endpoints for r in e.roles}) or ["any authenticated user"],
                "endpoints": sorted({f"{e.verb} {e.url}" for e in f.endpoints})[:4],
                "endpoint_node_ids": sorted({e.node_id for e in f.endpoints})[:3],
            }
            for f in ranked
        ],
        "changed_symbols": [
            {"name": r.symbol.name.rsplit(".", 1)[-1], "node_id": r.symbol.id, "risk": r.level}
            for r in changed[:8]
        ],
        "defect_recurrence": [
            {"symbol_area": r.path.rsplit("/", 1)[-1], "fix_subject": r.subject, "fix_date": r.date}
            for r in recs
        ],
    }


def compute_blind_spots(lc, candidates: dict, known_endpoint_ids: set, max_depth: int) -> list:
    """A blind spot is a genuine dead end: not itself an endpoint, no test, AND walking
    FURTHER from it never reaches an endpoint either. A service method sitting between a
    changed symbol and its controller is a `candidate` too, but its own upward walk leads
    straight to that controller — checking "is this exact node an endpoint" alone flags
    nearly every intermediate method as unreachable, which is wrong."""
    blind_spots = []
    for node_id, sym in candidates.items():
        if node_id in known_endpoint_ids or lc.tests_for(node_id):
            continue
        if any(up.symbol.id in known_endpoint_ids for up in lc.blast_radius(node_id, max_depth)):
            continue
        blind_spots.append(sym)
    return blind_spots


def ensure_graph(repo_path: Path, db: str | None, reindex: bool) -> Path:
    """First run (no Loom DB) or --reindex: build the graph with `loom analyze`."""
    db_path = Path(db) if db else Path.home() / ".loom" / "projects" / f"{repo_path.name}.db"
    log.debug("loom graph: repo=%s db=%s exists=%s reindex=%s",
              repo_path, db_path, db_path.exists(), reindex)
    if db_path.exists() and not reindex:
        return db_path
    if db:  # loom analyze only writes the default location — can't honor a custom path
        raise FileNotFoundError(f"--db {db_path} not found; run `loom analyze .` in the repo yourself")
    why = "rebuilding graph (--reindex)" if db_path.exists() else \
        f"first run for {repo_path.name} — building the Loom code graph (one-time, ~1-2 min)"
    log.info("RegressIQ: %s…", why)
    r = subprocess.run(["loom", "analyze", "."], cwd=repo_path, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        log.debug("loom analyze exited %s: %s", r.returncode, (r.stderr or r.stdout).strip()[-300:])
        raise diffmod.GitError(f"loom analyze failed: {(r.stderr or r.stdout).strip()[-300:]}")
    log.info("RegressIQ: graph ready at %s", db_path)
    return db_path


def analyze(repo: str, base: str, target: str, db: str | None, two_dot: bool, max_depth: int,
            reindex: bool = False):
    repo_path = Path(repo).resolve()
    log.debug("analyze: repo=%s base=%s target=%s two_dot=%s max_depth=%s",
              repo_path, base, target, two_dot, max_depth)
    scope = diffmod.changed_backend(str(repo_path), base, target, two_dot)
    log.debug("analyze: %d changed backend file(s), merge-base ref=%s, proto_changed=%s",
              len(scope.files), scope.base_ref, bool(scope.proto_changed))
    lc = LoomClient(ensure_graph(repo_path, db, reindex))

    # changed hunks -> changed symbols (dedup: several hunks can hit one method)
    changed_syms = {}
    unmatched: list[str] = []
    for fc in scope.files:
        matched = False
        for start, end in fc.ranges:
            for sym in lc.symbols_at(fc.path, start, end):
                changed_syms.setdefault(sym.id, sym)
                matched = True
        if not matched:
            unmatched.append(fc.path)  # imports/fields/deletions only — file-level change
    log.debug("analyze: %d changed symbol(s), %d file(s) with no symbol-level match",
              len(changed_syms), len(unmatched))

    recs = history.recurrence(
        str(repo_path), target, scope.base_ref, [(fc.path, fc.ranges) for fc in scope.files]
    )
    proto_changed = bool(scope.proto_changed)

    changed_risks = [
        risk.assess_symbol(sym, lc.fan_in(sym.id), recs, proto_changed)
        for sym in changed_syms.values()
    ]
    changed_risks.sort(key=lambda r: -ORDER[r.level])

    # blast radius -> endpoints -> features
    ep_index = EndpointIndex(repo_path, load_screen_map(repo_path / "screens.yaml")
                             or load_screen_map(Path(__file__).parents[2] / "screens.yaml"))
    features: dict[str, Feature] = {}

    def add_endpoint(reach: Reach, via_risk: risk.SymbolRisk):
        ep = ep_index.endpoint_for(reach.symbol.id, reach.symbol.name, reach.symbol.path, reach.symbol.start_line)
        if ep is None:
            return
        f = features.get(ep.screen)
        if f is None:
            f = features[ep.screen] = Feature(ep.screen, [], "LOW", [], reach.depth, reach.inferred)
        f.endpoints.append(ep)
        f.min_depth = min(f.min_depth, reach.depth)
        # confirmed if ANY path to this screen is bridge-free; inferred only when all paths are
        f.inferred = f.inferred and reach.inferred
        seed_is_proto = via_risk.is_proto
        capped_level, cap_reason = risk.cap_reach_risk(
            via_risk.level, reach.depth, via_risk.fan_in, seed_is_proto
        )
        if cap_reason:
            effective_risk = risk.SymbolRisk(via_risk.symbol, capped_level, [cap_reason] + via_risk.reasons, via_risk.fan_in)
        else:
            effective_risk = via_risk
        lvl, reasons = risk.combine([effective_risk])
        if ORDER[lvl] > ORDER[f.level]:
            f.level, f.reasons = lvl, reasons  # higher level supersedes old citations
        elif ORDER[lvl] == ORDER[f.level]:
            f.reasons = list(dict.fromkeys(f.reasons + reasons))
        name = via_risk.symbol.name.rsplit(".", 1)[-1]
        if name not in f.via:
            f.via.append(name)

    candidates: dict[str, object] = {}  # node_id -> Symbol, every seed + reach seen

    for r in changed_risks:
        candidates.setdefault(r.symbol.id, r.symbol)
        # depth=0: seed is the endpoint; cap_reach_risk never fires at depth 0 (0 < DEEP_HOP), which is correct
        add_endpoint(Reach(r.symbol, 0, False), r)
        for reach in lc.blast_radius(r.symbol.id, max_depth):
            add_endpoint(reach, r)
            candidates.setdefault(reach.symbol.id, reach.symbol)

    known_endpoint_ids = {ep.node_id for f in features.values() for ep in f.endpoints}
    blind_spots = compute_blind_spots(lc, candidates, known_endpoint_ids, max_depth)

    coupled = {
        fc.path: lc.coupled_files(fc.path, exclude={f.path for f in scope.files})
        for fc in scope.files
    }
    tests = list({t.id: t for r in changed_risks for t in lc.tests_for(r.symbol.id)}.values())
    log.debug("analyze: %d affected screen(s), %d blind spot(s), %d existing test(s), "
              "%d defect recurrence(s)",
              len(features), len(blind_spots), len(tests), len(recs))

    return scope, changed_risks, list(features.values()), recs, coupled, tests, unmatched, blind_spots, lc


@workflow(logmod.DIFF)
def _run_diff(args) -> int:
    ts = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    run_dir = Path("runs") / f"{ts}-diff"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = attach_run_log(run_dir)
    log.debug("RegressIQ diff: base=%s target=%s repo=%s out=%s scope=%s",
              args.base, args.target, args.repo, args.out, args.scope)
    if log_path:
        log.info("RegressIQ: log → %s", log_path)

    try:
        scope, changed, feats, recs, coupled, tests, unmatched, blind_spots, lc = analyze(
            args.repo, args.base, args.target, args.db, args.two_dot, args.max_depth,
            args.reindex,
        )
    except (diffmod.GitError, FileNotFoundError) as e:
        log.error("RegressIQ: %s", e, exc_info=True)
        return 2

    if args.scope:
        commits = diffmod.branch_commits(args.repo, args.base, args.target, args.two_dot)
        scope_data = {
            "version": 1,
            "base": scope.base_ref,
            "target": scope.target_ref,
            "repo": str(Path(args.repo).resolve()),
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            "commits": [
                {"hash": c.sha, "author_email": c.author_email, "message": c.subject}
                for c in commits
            ],
            "changed_symbols": [
                {"id": r.symbol.id, "name": r.symbol.name.rsplit(".", 1)[-1],
                 "path": r.symbol.path, "risk": r.level, "fan_in": r.fan_in}
                for r in changed
            ],
            "affected_screens": [f.screen for f in feats],
        }
        Path(args.scope).write_text(json.dumps(scope_data, indent=2), encoding="utf-8")
        log.info("RegressIQ: scope written to %s", args.scope)
        log.debug("scope contents: %d commit(s), %d changed symbol(s), %d affected screen(s)",
                  len(scope_data["commits"]), len(scope_data["changed_symbols"]),
                  len(scope_data["affected_screens"]))

    if args.no_ai:
        notes, status = None, "disabled with --no-ai"
    else:
        llm_cfg = get_llm_config(
            Path(args.repo) / ".env",
            Path(__file__).parents[2] / ".env",
            provider_override=args.llm_provider,
            model_override=args.llm_model,
        )
        log.info(
            "RegressIQ: investigating top %s screen(s) via Loom + LLM agent (%s/%s)…",
            args.investigate, llm_cfg.provider, llm_cfg.model,
        )
        notes, status = agentmod.try_investigate(
            agent_scope(scope, changed, feats, recs, top=args.investigate),
            lc, Path(args.repo).resolve(), llm_cfg,
        )
    if notes is None:
        log.warning("RegressIQ: %s", status)

    try:
        tickets = jira_mock.build_tickets(
            str(Path(args.repo).resolve()), args.base, args.target, changed, feats, args.two_dot
        )
    except diffmod.GitError as e:
        log.warning("ticket preview skipped: %s", e, exc_info=True)
        tickets = None  # ticket preview is a nice-to-have; never block the report

    # Cross-repo bridge: resolve backend endpoints to real Angular screens (opt-in).
    bridge_result = None
    endpoint_risk = {ep.node_id: f.level for f in feats for ep in f.endpoints}  # inherit server risk
    if args.client_repo:
        try:
            client_root = Path(args.client_repo).resolve()
            client_db = ensure_graph(client_root, args.client_db, args.reindex)
            endpoints = list({ep.node_id: ep for f in feats for ep in f.endpoints}.values())
            log.info("RegressIQ: bridging %d endpoint(s) to client screens…", len(endpoints))
            bridge_result = bridgemod.resolve_client_screens(endpoints, LoomClient(client_db), client_root)
            log.info("RegressIQ: client match rate %.0f%% (%d screen mapping(s), %d unmapped)",
                     bridge_result.match_rate * 100, len(bridge_result.mappings),
                     len(bridge_result.unresolved))
        except (diffmod.GitError, FileNotFoundError) as e:
            log.warning("RegressIQ: client bridge skipped (%s) — backend-only report", e, exc_info=True)

    Path(args.out).write_text(
        render_html(scope, changed, feats, recs, coupled, tests,
                    blind_spots=blind_spots, ai_notes=notes, tickets=tickets,
                    bridge=bridge_result, endpoint_risk=endpoint_risk),
        encoding="utf-8",
    )
    log.debug("HTML report written to %s", args.out)
    # stdout below is the command's product (Jira comment) — deliberately not logged
    print(jira_comment(scope, feats, recs, notes))
    if notes:
        print(f"\nQA NOTES (AI-narrated from the deterministic scope above):\n{notes}")
    if unmatched:
        print(f"\n(note: {len(unmatched)} changed files had no symbol-level match: "
              f"{', '.join(p.rsplit('/', 1)[-1] for p in unmatched[:6])}…)")
    print(f"\nHTML report: {args.out}")
    log.debug("RegressIQ diff: completed successfully")
    return 0


def _automation_status(raw_tc: dict) -> str:
    """Read AUTOMATION_STATUS out of a raw TCM test case's customFieldValues."""
    for f in raw_tc.get("customFieldValues") or []:
        if f.get("fieldKey") == "AUTOMATION_STATUS":
            return f.get("fieldValue") or ""
    return ""


@workflow(logmod.RELEASE)
def _do_create_release(run_dir: Path, ts: str, tcm_raw: list[dict], cfg) -> int:
    """Stage 3: create a TCM release + test cycle + manual/automation execution cycles.

    Test cases are split by AUTOMATION_STATUS: "Can Not Be Automated" goes to the
    manual cycle, everything else to the automation cycle.
    """
    if not tcm_raw:
        log.warning("RegressIQ release: no test cases fetched — skipping release creation")
        return 0
    if not cfg.tcm_project_id:
        log.error("RegressIQ release: TCM_PROJECT_ID not set in .env")
        return 2

    log.info("RegressIQ release: classifying %d test cases by Automation Status...", len(tcm_raw))
    manual_ids, automation_ids = [], []
    for tc in tcm_raw:
        uid = tc.get("uniqueTestcaseId")
        if not uid:
            continue
        if _automation_status(tc).strip().lower() == "can not be automated":
            manual_ids.append(uid)
        else:
            automation_ids.append(uid)
    log.info("RegressIQ release: %d manual (Can Not Be Automated), %d automation",
             len(manual_ids), len(automation_ids))
    log.debug("manual ids: %s", manual_ids or "none")
    log.debug("automation ids: %s", automation_ids or "none")

    if not manual_ids and not automation_ids:
        log.warning("RegressIQ release: no test cases to assign — nothing created")
        return 0

    vid, pid = cfg.tcm_vertical_id, cfg.tcm_project_id
    sess, proj_sess, refresh = cfg.tcm_session, cfg.tcm_project_session, cfg.tcm_refresh_token

    release_name = f"Regression"
    try:
        log.info('RegressIQ release: creating release "%s"...', release_name)
        release = tcm.create_release(vid, pid, {
            "releaseName": release_name,
            "releaseDescription": "Automatically generated regression release",
            "releaseVersion": "1.0",
            "status": "PLANNED",
        }, sess, proj_sess, refresh)
        release_id = release.get("id")
        if not release_id:
            log.error("RegressIQ release: create release returned no id — %s", release)
            return 2
        log.info("RegressIQ release: ✓ release created (id: %s)", release_id)
    except Exception as e:
        log.error("RegressIQ release: create release failed — %s", e, exc_info=True)
        return 2

    cycle_name = f"Test Cycle"
    try:
        log.info('RegressIQ release: creating test cycle "%s"...', cycle_name)
        cycle = tcm.create_test_cycle(vid, pid, release_id, {
            "releaseId": release_id,
            "versionName": cycle_name,
            "versionNumber": "1.0",   # API rejects the cycle without it: "Version number is required"
            "versionDescription": "Automatically generated test cycle",
            "status": "PLANNED",
        }, sess, proj_sess, refresh)
        test_cycle_id = cycle.get("id")
        if not test_cycle_id:
            log.error("RegressIQ release: create test cycle returned no id — %s", cycle)
            return 2
        log.info("RegressIQ release: ✓ test cycle created (id: %s)", test_cycle_id)
    except Exception as e:
        log.error("RegressIQ release: create test cycle failed — %s", e, exc_info=True)
        return 2

    exec_cycles = [
        ("Manual", "Manual test cases - Can Not Be Automated", "MANUAL", manual_ids),
        ("Automation",
         "Automation test cases - Planned/Automated/In Progress/Not Automated",
         "AUTOMATION", automation_ids),
    ]
    created: list[tuple[str, str, list[str]]] = []
    for name, desc, ctype, ids in exec_cycles:
        try:
            log.info('RegressIQ release: creating execution cycle "%s"...', name)
            ec = tcm.create_execution_cycle(vid, pid, test_cycle_id, {
                "cycleName": name,
                "cycleDescription": desc,
                "cycleType": ctype,
                "status": "NOT_STARTED",
            }, sess, proj_sess, refresh)
            ec_id = ec.get("id")
            if not ec_id:
                log.error("RegressIQ release: create execution cycle %s returned no id — %s", name, ec)
                return 2
            log.info("RegressIQ release: ✓ execution cycle created (id: %s)", ec_id)
            created.append((name, ec_id, ids))
        except Exception as e:
            log.error("RegressIQ release: create execution cycle %s failed — %s", name, e, exc_info=True)
            return 2

    for name, ec_id, ids in created:
        if not ids:
            log.info("RegressIQ release: skipping %s assignment (0 test cases)",
                     name.split()[0].lower())
            continue
        try:
            log.info("RegressIQ release: assigning %d test case(s) to %s...", len(ids), name)
            tcm.assign_test_cases_bulk(vid, pid, ec_id, {
                "cycleId": ec_id,
                "uniqueTestcaseIds": ids,
            }, sess, proj_sess, refresh)
            log.info("RegressIQ release: ✓ %d test case(s) assigned", len(ids))
        except Exception as e:
            log.error("RegressIQ release: assigning test cases to %s failed — %s", name, e, exc_info=True)
            return 2

    log.info("RegressIQ release: done ✓")
    return 0


def _run_predict(args) -> int:
    """Stage 2: read scope.json → gap detection → TCM test cases → run dir output."""
    import datetime
    import shutil
    from .mailer import SmtpConfig

    # Load scope
    scope_path = Path(args.scope)
    if not scope_path.exists():
        log.error("RegressIQ: scope file not found: %s", scope_path)
        return 2
    scope_data = json.loads(scope_path.read_text(encoding="utf-8"))
    commits = scope_data.get("commits", [])
    log.debug("RegressIQ predict: scope=%s (%d commit(s), %d changed symbol(s), %d screen(s))",
              scope_path, len(commits), len(scope_data.get("changed_symbols", [])),
              len(scope_data.get("affected_screens", [])))

    # Create timestamped run dir
    ts = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    run_dir = Path("runs") / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(scope_path, run_dir / "scope.json")
    attach_run_log(run_dir)
    log.info("RegressIQ predict: run dir → %s", run_dir)

    # Load config
    env_candidates = [
        Path(args.env) if args.env else None,
        Path(__file__).parents[2] / ".env",
    ]
    env_files = [p for p in env_candidates if p is not None]
    cfg = load_predict_config(*env_files)

    # CLI overrides
    tcm_vertical = args.tcm_vertical or cfg.tcm_vertical_id
    tcm_project = args.tcm_project or cfg.tcm_project_key
    jira_project = args.jira_project
    log.debug("RegressIQ predict: tcm_project=%s jira_project=%s create_release=%s no_alert=%s",
              tcm_project, jira_project,
              getattr(args, "create_release", False), args.no_alert)

    # LLM config (needed by semantic enrichment layers)
    llm_cfg = get_llm_config(
        *env_files,
        provider_override=args.llm_provider,
        model_override=args.llm_model,
    )

    # Gap detection
    jira_client = JiraClient(cfg.jira_base_url, cfg.jira_email, cfg.jira_api_token)
    with workflow(logmod.GAP_ANALYSIS):
        try:
            coverage = gd.detect_gaps(commits, jira_client, jira_project)
        except Exception as e:
            log.warning("RegressIQ predict: Jira gap detection failed (%s) — continuing without gap data",
                        e, exc_info=True)
            coverage = []

        uncovered = [c for c in coverage if not c.covered]
        covered = [c for c in coverage if c.covered]
        log.debug("gap detection: %d covered, %d uncovered commit(s)", len(covered), len(uncovered))

    # Gap alert
    gap_alert_sent = False
    smtp_cfg = None
    alert_recipients: list[str] = []
    if uncovered and not args.no_alert:
        with workflow(logmod.EMAIL_ALERT):
            smtp_cfg = SmtpConfig(
                host=cfg.smtp_host, port=cfg.smtp_port,
                user=cfg.smtp_user, password=cfg.smtp_password,
                from_addr=cfg.smtp_from,
            )
            author_emails = [c.author_email for c in uncovered if c.author_email]
            alert_recipients = sorted(set(author_emails) | set(cfg.alert_emails))
            try:
                mailermod.send_gap_alert(
                    uncovered, author_emails, cfg.alert_emails, smtp_cfg, jira_project,
                    jira_client=jira_client,
                    scope_data=scope_data,
                    llm_cfg=llm_cfg,
                    jira_base_url=cfg.jira_base_url,
                )
                log.info("RegressIQ predict: gap alert sent for %d uncovered commit(s)", len(uncovered))
                gap_alert_sent = True
            except Exception as e:
                log.error("RegressIQ predict: gap alert failed (%s)", e, exc_info=True)

    # Semantic Layer 3 — find stories for uncovered commits
    l3_included, l3_alerts = [], []
    if uncovered:
        with workflow(logmod.SEMANTIC_MATCHING):
            try:
                l3_included, l3_alerts = enrich_layer3(
                    uncovered,
                    scope_data.get("changed_symbols", []),
                    scope_data.get("affected_screens", []),
                    jira_client, jira_project, llm_cfg,
                )
                if l3_included:
                    log.info(
                        "RegressIQ predict: semantic L3 matched %d story/stories for uncovered commits",
                        len(l3_included),
                    )
                log.debug("semantic L3: %d match(es), %d alert(s)", len(l3_included), len(l3_alerts))
            except Exception as e:
                log.warning("RegressIQ predict: semantic L3 failed (%s) — skipping", e, exc_info=True)

    # Fetch raw Jira stories for covered commits
    with workflow(logmod.JIRA_FETCH):
        all_jira_keys = list({k for c in covered for k in c.jira_keys})
        log.debug("jira keys from covered commits: %s", all_jira_keys or "none")
        try:
            jira_stories_raw = jira_client.fetch_raw_by_keys(all_jira_keys) if all_jira_keys else []
            defects_raw = jira_client.fetch_defects_for_stories(all_jira_keys) if all_jira_keys else []
        except Exception as e:
            log.error("RegressIQ predict: Jira story fetch failed (%s)", e, exc_info=True)
            jira_stories_raw, defects_raw = [], []

        # Merge semantically matched stories into jira_stories_raw
        if l3_included:
            semantic_story_keys = [m.key for m in l3_included]
            try:
                semantic_stories_raw = jira_client.fetch_raw_by_keys(semantic_story_keys)
                existing_keys = {s["key"] for s in jira_stories_raw}
                jira_stories_raw = jira_stories_raw + [
                    s for s in semantic_stories_raw if s["key"] not in existing_keys
                ]
            except Exception as e:
                log.warning("RegressIQ predict: semantic story fetch failed (%s)", e, exc_info=True)

    # Include L3 semantic story keys in TC selection
    if l3_included:
        l3_keys = [m.key for m in l3_included]
        all_jira_keys = list(dict.fromkeys(all_jira_keys + l3_keys))

    # Fetch TCM test cases (raw + parsed)
    with workflow(logmod.TCM_FETCH):
        try:
            if not tcm_vertical:
                log.warning("RegressIQ predict: TCM_VERTICAL_ID not set — skipping TCM fetch, "
                            "output will be empty")
                tcm_raw, all_cases = [], []
            else:
                tcm_raw, all_cases = tcm.fetch_all(
                    tcm_vertical, tcm_project,
                    cfg.tcm_session, cfg.tcm_project_session,
                    refresh_token=cfg.tcm_refresh_token,
                )
        except Exception as e:
            log.error("RegressIQ predict: TCM fetch failed (%s) — output will be empty", e, exc_info=True)
            tcm_raw, all_cases = [], []

    # Semantic Layer 4 — find relevant TCs among unlinked cases
    l4_included, l4_alerts = [], []
    unlinked_tcs = [
        tc for tc in tcm_raw
        if tc.get("jiraStoryKey") in (None, "NA", "")
    ]
    if unlinked_tcs:
        story_summaries = [
            f"{s['key']}: {(s.get('fields') or {}).get('summary', '')}"
            for s in jira_stories_raw
        ]
        with workflow(logmod.SEMANTIC_MATCHING):
            try:
                l4_included, l4_alerts = enrich_layer4(
                    unlinked_tcs,
                    scope_data.get("affected_screens", []),
                    scope_data.get("changed_symbols", []),
                    story_summaries,
                    llm_cfg,
                )
                if l4_included:
                    log.info("RegressIQ predict: semantic L4 matched %d unlinked TC(s)", len(l4_included))
                log.debug("semantic L4: %d unlinked TC(s) considered, %d match(es), %d alert(s)",
                          len(unlinked_tcs), len(l4_included), len(l4_alerts))
            except Exception as e:
                log.warning("RegressIQ predict: semantic L4 failed (%s) — skipping", e, exc_info=True)

    # Test case selection — hard link chain via jiraStoryKey
    selected = []
    for jira_key in all_jira_keys:
        story_raw = next((s for s in jira_stories_raw if s["key"] == jira_key), None)
        story_summary = (story_raw or {}).get("fields", {}).get("summary") if story_raw else None
        for tc in tcm.by_jira_key(all_cases, jira_key):
            selected.append({
                "unique_id": tc.unique_id,
                "title": tc.title,
                "priority": tc.priority,
                "category": tc.category,
                "automation_status": tc.automation_status,
                "jira_story_key": tc.jira_story_key,
                "jira_story_summary": story_summary,
                "selection_reason": "linked",
                "confidence": "high",
                "steps": [{"order": s.order, "action": s.action, "expected": s.expected}
                          for s in tc.steps],
            })

    # Fallback: all active cases
    if not selected and all_cases:
        for tc in all_cases:
            if tc.status == "ACTIVE":
                selected.append({
                    "unique_id": tc.unique_id,
                    "title": tc.title,
                    "priority": tc.priority,
                    "category": tc.category,
                    "automation_status": tc.automation_status,
                    "jira_story_key": tc.jira_story_key,
                    "jira_story_summary": None,
                    "selection_reason": "all",
                    "confidence": "medium",
                    "steps": [{"order": s.order, "action": s.action, "expected": s.expected}
                              for s in tc.steps],
                })

    log.debug("selection: %d test case(s) selected (%s)", len(selected),
              "linked" if any(s["selection_reason"] == "linked" for s in selected)
              else "fallback: all active" if selected else "none")

    # Write raw JSON files
    (run_dir / "jira_stories.json").write_text(json.dumps(jira_stories_raw, indent=2), encoding="utf-8")
    (run_dir / "defects.json").write_text(json.dumps(defects_raw, indent=2), encoding="utf-8")
    (run_dir / "sdet360testcases.json").write_text(json.dumps(tcm_raw, indent=2), encoding="utf-8")
    log.debug("wrote jira_stories.json (%d), defects.json (%d), sdet360testcases.json (%d)",
              len(jira_stories_raw), len(defects_raw), len(tcm_raw))

    # Write test_cases.json
    output = {
        "version": 1,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "run_dir": str(run_dir),
        "base": scope_data.get("base", ""),
        "target": scope_data.get("target", ""),
        "gap_alert_sent": gap_alert_sent,
        "uncovered_commits": [
            {"hash": c.hash, "author_email": c.author_email, "message": c.message}
            for c in uncovered
        ],
        "covered_commits": [
            {"hash": c.hash, "jira_keys": list(c.jira_keys)}
            for c in covered
        ],
        "jira_stories": [
            {"key": s["key"], "summary": s.get("fields", {}).get("summary", ""),
             "status": (s.get("fields", {}).get("status") or {}).get("name", ""),
             "priority": (s.get("fields", {}).get("priority") or {}).get("name", "")}
            for s in jira_stories_raw
        ],
        "jira_defects": [
            {"key": d["key"], "summary": d.get("fields", {}).get("summary", ""),
             "status": (d.get("fields", {}).get("status") or {}).get("name", ""),
             "priority": (d.get("fields", {}).get("priority") or {}).get("name", "")}
            for d in defects_raw
        ],
        "test_cases": selected,
        "semantic_test_cases": [
            {
                "unique_id": m.key,
                "confidence": m.confidence,
                "score": m.score,
                "reason": m.reason,
            }
            for m in l4_included
        ],
        "semantic_alerts": [
            {
                "type": "layer3",
                "key": m.key,
                "score": m.score,
                "reason": m.reason,
            }
            for m in l3_alerts
        ] + [
            {
                "type": "layer4",
                "key": m.key,
                "score": m.score,
                "reason": m.reason,
            }
            for m in l4_alerts
        ],
    }
    out_path = run_dir / "test_cases.json"
    out_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    log.debug("wrote %s", out_path)

    # Generate HTML report (fails loudly if LLM call fails)
    with workflow(logmod.REPORT_GENERATION):
        try:
            reportermod.generate(
                str(run_dir), scope_data, coverage,
                jira_stories_raw, defects_raw, tcm_raw,
                llm_cfg,
                semantic_tcs=l4_included,
                semantic_alerts=l3_alerts + l4_alerts,
            )
            log.info("RegressIQ predict: report.html written (%s/%s)", llm_cfg.provider, llm_cfg.model)
        except Exception as e:
            log.error("RegressIQ predict: report generation failed — %s", e, exc_info=True)
            return 2

    # Test Plan email 
    if uncovered and not args.no_alert:
        with workflow(logmod.EMAIL_ALERT):
            try:
                try:
                    open_stories = jira_client.fetch_open_stories(jira_project)
                except Exception as e:
                    log.debug("test plan: open story fetch failed (%s) — risk section "
                              "will list uncovered commits only", e)
                    open_stories = []
                html_body = test_plan.build_test_plan_html(
                    scope_data, jira_stories_raw, defects_raw, selected,
                    uncovered, open_stories, cfg,
                )
                test_plan.send_test_plan_email(
                    html_body,
                    run_dir / "report.html",
                    alert_recipients,
                    smtp_cfg,
                    subject=(f"[RegressIQ] Test Plan — {scope_data.get('target', '')} "
                             f"({len(jira_stories_raw)} feature(s), "
                             f"{len(selected)} test case(s))"),
                )
                log.info("RegressIQ predict: test plan email sent to %d recipient(s)",
                         len(alert_recipients))
            except Exception as e:
                log.error("RegressIQ predict: test plan email failed (%s)", e, exc_info=True)

    log.info("RegressIQ predict: %d test case(s) → %s", len(selected), run_dir)

    if getattr(args, "create_release", False):
        rc = _do_create_release(run_dir, ts, tcm_raw, cfg)
        if rc != 0:
            return rc
    log.debug("RegressIQ predict: completed successfully")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="RegressIQ", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--log-level", default="INFO", dest="log_level",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="console log level (default INFO); the run log file is always DEBUG")

    # ── diff subcommand ───────────────────────────────────────────────────
    d = sub.add_parser("diff", parents=[common],
                       help="scope regression for target's changes relative to base")
    d.add_argument("base", help="baseline ref (e.g. main)")
    d.add_argument("target", help="branch under test")
    d.add_argument("--two-dot", action="store_true", help="raw tip-to-tip diff (skip merge-base)")
    d.add_argument("--repo", default=".", help="path to the git repo (default: cwd)")
    d.add_argument("--db", default=None, help="Loom DB path (default ~/.loom/projects/<repo>.db)")
    d.add_argument("--out", default="tracer-report.html", help="HTML report path")
    d.add_argument("--max-depth", type=int, default=6, help="blast-radius depth cap")
    d.add_argument("--no-ai", action="store_true", help="skip AI investigation (seam 2)")
    d.add_argument("--investigate", type=int, default=2,
                   help="how many top-risk screens the agent reads code for (default 2; free-tier TPM bound)")
    d.add_argument("--reindex", action="store_true", help="rebuild the Loom graph first")
    d.add_argument("--client-repo", default=None,
                   help="Angular client repo — resolves backend endpoints to real frontend screens")
    d.add_argument("--client-db", default=None, help="client Loom DB path (default ~/.loom/projects/<name>.db)")
    d.add_argument("--scope", default=None, metavar="PATH",
                   help="write deterministic scope JSON for use by RegressIQ predict")
    d.add_argument("--llm-provider", default=None, dest="llm_provider",
                   metavar="PROVIDER", help="LLM provider: groq or openai (overrides LLM_PROVIDER env)")
    d.add_argument("--llm-model", default=None, dest="llm_model",
                   metavar="MODEL", help="LLM model name (overrides LLM_MODEL env)")

    # ── predict subcommand ────────────────────────────────────────────────
    p = sub.add_parser("predict", parents=[common],
                       help="select test cases from scope.json via TCM + Jira")
    p.add_argument("--scope", required=True, metavar="PATH",
                   help="scope.json from RegressIQ diff --scope")
    p.add_argument("--out", default="test_cases.json",
                   help="output path for test_cases.json")
    p.add_argument("--env", default=None, metavar="PATH",
                   help="path to .env file")
    p.add_argument("--tcm-vertical", default=None, dest="tcm_vertical",
                   help="TCM vertical UUID (overrides TCM_VERTICAL_ID from env)")
    p.add_argument("--tcm-project", default=None, dest="tcm_project",
                   help="TCM project key (overrides TCM_PROJECT_KEY, default AS360)")
    p.add_argument("--jira-project", default="REG", dest="jira_project",
                   help="Jira project key (default REG)")
    p.add_argument("--no-alert", action="store_true", dest="no_alert",
                   help="suppress gap alert email")
    p.add_argument("--create-release", action="store_true", dest="create_release",
                   help="create release, test cycle, and execution cycles in TCM after prediction")
    p.add_argument("--no-predict-ai", action="store_true", dest="no_predict_ai",
                   help="reserved for Phase 2c LangGraph selector (currently always true)")
    p.add_argument("--llm-provider", default=None, dest="llm_provider",
                   metavar="PROVIDER", help="LLM provider: groq or openai (overrides LLM_PROVIDER env)")
    p.add_argument("--llm-model", default=None, dest="llm_model",
                   metavar="MODEL", help="LLM model name (overrides LLM_MODEL env)")

    args = ap.parse_args(argv)

    setup_logging(args.log_level)
    # Tag the argv line with the stage it belongs to — _run_diff/_run_predict
    # set their own stage, but this line is logged before either is entered.
    with workflow(logmod.DIFF if args.cmd == "diff" else logmod.PREDICT):
        log.debug("RegressIQ %s — argv: %s", args.cmd,
                  " ".join(argv if argv is not None else sys.argv[1:]))

    try:
        if args.cmd == "diff":
            return _run_diff(args)
        if args.cmd == "predict":
            return _run_predict(args)
        return 1
    except Exception:
        # Console still gets Python's own traceback on re-raise; this puts the
        # full stack in the run log too, where it can be read after the fact.
        log.critical("RegressIQ: unhandled error", exc_info=True)
        raise
    finally:
        close_run_log()


if __name__ == "__main__":
    sys.exit(main())
