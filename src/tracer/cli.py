"""tracer diff <base> <target> — regression scope for what `target` introduced.

Exit codes: 0 success, 2 git/ref error (never a silent false "all clear").
"""

from __future__ import annotations

import argparse
import datetime
import json
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
from .jira_client import JiraClient
from .llm_config import get_llm_config
from .loom_client import LoomClient, Reach
from .predict_cfg import load_predict_config
from .report import Feature, jira_comment, render_html
from .screens import EndpointIndex, load_screen_map
from .semantic_matcher import enrich_layer3, enrich_layer4


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
    if db_path.exists() and not reindex:
        return db_path
    if db:  # loom analyze only writes the default location — can't honor a custom path
        raise FileNotFoundError(f"--db {db_path} not found; run `loom analyze .` in the repo yourself")
    why = "rebuilding graph (--reindex)" if db_path.exists() else \
        f"first run for {repo_path.name} — building the Loom code graph (one-time, ~1-2 min)"
    print(f"tracer: {why}…", file=sys.stderr)
    r = subprocess.run(["loom", "analyze", "."], cwd=repo_path, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise diffmod.GitError(f"loom analyze failed: {(r.stderr or r.stdout).strip()[-300:]}")
    print(f"tracer: graph ready at {db_path}", file=sys.stderr)
    return db_path


def analyze(repo: str, base: str, target: str, db: str | None, two_dot: bool, max_depth: int,
            reindex: bool = False):
    repo_path = Path(repo).resolve()
    scope = diffmod.changed_backend(str(repo_path), base, target, two_dot)
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

    return scope, changed_risks, list(features.values()), recs, coupled, tests, unmatched, blind_spots, lc


def _run_diff(args) -> int:
    try:
        scope, changed, feats, recs, coupled, tests, unmatched, blind_spots, lc = analyze(
            args.repo, args.base, args.target, args.db, args.two_dot, args.max_depth,
            args.reindex,
        )
    except (diffmod.GitError, FileNotFoundError) as e:
        print(f"tracer: {e}", file=sys.stderr)
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
        print(f"tracer: scope written to {args.scope}", file=sys.stderr)

    if args.no_ai:
        notes, status = None, "disabled with --no-ai"
    else:
        llm_cfg = get_llm_config(
            Path(args.repo) / ".env",
            Path(__file__).parents[2] / ".env",
            provider_override=args.llm_provider,
            model_override=args.llm_model,
        )
        print(
            f"tracer: investigating top {args.investigate} screen(s) via Loom + LLM agent"
            f" ({llm_cfg.provider}/{llm_cfg.model})…",
            file=sys.stderr,
        )
        notes, status = agentmod.try_investigate(
            agent_scope(scope, changed, feats, recs, top=args.investigate),
            lc, Path(args.repo).resolve(), llm_cfg,
        )
    if notes is None:
        print(f"tracer: {status}", file=sys.stderr)

    try:
        tickets = jira_mock.build_tickets(
            str(Path(args.repo).resolve()), args.base, args.target, changed, feats, args.two_dot
        )
    except diffmod.GitError:
        tickets = None  # ticket preview is a nice-to-have; never block the report

    # Cross-repo bridge: resolve backend endpoints to real Angular screens (opt-in).
    bridge_result = None
    endpoint_risk = {ep.node_id: f.level for f in feats for ep in f.endpoints}  # inherit server risk
    if args.client_repo:
        try:
            client_root = Path(args.client_repo).resolve()
            client_db = ensure_graph(client_root, args.client_db, args.reindex)
            endpoints = list({ep.node_id: ep for f in feats for ep in f.endpoints}.values())
            print(f"tracer: bridging {len(endpoints)} endpoint(s) to client screens…", file=sys.stderr)
            bridge_result = bridgemod.resolve_client_screens(endpoints, LoomClient(client_db), client_root)
            print(f"tracer: client match rate {bridge_result.match_rate:.0%} "
                  f"({len(bridge_result.mappings)} screen mapping(s), "
                  f"{len(bridge_result.unresolved)} unmapped)", file=sys.stderr)
        except (diffmod.GitError, FileNotFoundError) as e:
            print(f"tracer: client bridge skipped ({e}) — backend-only report", file=sys.stderr)

    Path(args.out).write_text(
        render_html(scope, changed, feats, recs, coupled, tests,
                    blind_spots=blind_spots, ai_notes=notes, tickets=tickets,
                    bridge=bridge_result, endpoint_risk=endpoint_risk),
        encoding="utf-8",
    )
    print(jira_comment(scope, feats, recs, notes))
    if notes:
        print(f"\nQA NOTES (AI-narrated from the deterministic scope above):\n{notes}")
    if unmatched:
        print(f"\n(note: {len(unmatched)} changed files had no symbol-level match: "
              f"{', '.join(p.rsplit('/', 1)[-1] for p in unmatched[:6])}…)")
    print(f"\nHTML report: {args.out}")
    return 0


def _run_predict(args) -> int:
    """Stage 2: read scope.json → gap detection → TCM test cases → run dir output."""
    import datetime
    import shutil
    from .mailer import SmtpConfig

    # Load scope
    scope_path = Path(args.scope)
    if not scope_path.exists():
        print(f"tracer: scope file not found: {scope_path}", file=sys.stderr)
        return 2
    scope_data = json.loads(scope_path.read_text(encoding="utf-8"))
    commits = scope_data.get("commits", [])

    # Create timestamped run dir
    ts = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    run_dir = Path("runs") / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(scope_path, run_dir / "scope.json")
    print(f"tracer predict: run dir → {run_dir}", file=sys.stderr)

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

    # LLM config (needed by semantic enrichment layers)
    llm_cfg = get_llm_config(
        *env_files,
        provider_override=args.llm_provider,
        model_override=args.llm_model,
    )

    # Gap detection
    jira_client = JiraClient(cfg.jira_base_url, cfg.jira_email, cfg.jira_api_token)
    try:
        coverage = gd.detect_gaps(commits, jira_client, jira_project)
    except Exception as e:
        print(f"tracer predict: Jira gap detection failed ({e}) — continuing without gap data",
              file=sys.stderr)
        coverage = []

    uncovered = [c for c in coverage if not c.covered]
    covered = [c for c in coverage if c.covered]

    # Gap alert
    gap_alert_sent = False
    if uncovered and not args.no_alert:
        smtp_cfg = SmtpConfig(
            host=cfg.smtp_host, port=cfg.smtp_port,
            user=cfg.smtp_user, password=cfg.smtp_password,
            from_addr=cfg.smtp_from,
        )
        author_emails = [c.author_email for c in uncovered if c.author_email]
        try:
            mailermod.send_gap_alert(
                uncovered, author_emails, cfg.alert_emails, smtp_cfg, jira_project,
                jira_client=jira_client,
                scope_data=scope_data,
                llm_cfg=llm_cfg,
                jira_base_url=cfg.jira_base_url,
            )
            print(f"tracer predict: gap alert sent for {len(uncovered)} uncovered commit(s)",
                  file=sys.stderr)
            gap_alert_sent = True
        except Exception as e:
            print(f"tracer predict: gap alert failed ({e})", file=sys.stderr)

    # Semantic Layer 3 — find stories for uncovered commits
    l3_included, l3_alerts = [], []
    if uncovered:
        try:
            l3_included, l3_alerts = enrich_layer3(
                uncovered,
                scope_data.get("changed_symbols", []),
                scope_data.get("affected_screens", []),
                jira_client, jira_project, llm_cfg,
            )
            if l3_included:
                print(
                    f"tracer predict: semantic L3 matched {len(l3_included)} story/stories "
                    f"for uncovered commits",
                    file=sys.stderr,
                )
        except Exception as e:
            print(f"tracer predict: semantic L3 failed ({e}) — skipping", file=sys.stderr)

    # Fetch raw Jira stories for covered commits
    all_jira_keys = list({k for c in covered for k in c.jira_keys})
    try:
        jira_stories_raw = jira_client.fetch_raw_by_keys(all_jira_keys) if all_jira_keys else []
        defects_raw = jira_client.fetch_defects_for_stories(all_jira_keys) if all_jira_keys else []
    except Exception as e:
        print(f"tracer predict: Jira story fetch failed ({e})", file=sys.stderr)
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
            print(f"tracer predict: semantic story fetch failed ({e})", file=sys.stderr)

    # Include L3 semantic story keys in TC selection
    if l3_included:
        l3_keys = [m.key for m in l3_included]
        all_jira_keys = list(dict.fromkeys(all_jira_keys + l3_keys))

    # Fetch TCM test cases (raw + parsed)
    try:
        tcm_raw, all_cases = tcm.fetch_all(
            tcm_vertical, tcm_project,
            cfg.tcm_session, cfg.tcm_project_session,
            refresh_token=cfg.tcm_refresh_token,
        ) if tcm_vertical else ([], [])
    except Exception as e:
        print(f"tracer predict: TCM fetch failed ({e}) — output will be empty", file=sys.stderr)
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
        try:
            l4_included, l4_alerts = enrich_layer4(
                unlinked_tcs,
                scope_data.get("affected_screens", []),
                scope_data.get("changed_symbols", []),
                story_summaries,
                llm_cfg,
            )
            if l4_included:
                print(
                    f"tracer predict: semantic L4 matched {len(l4_included)} unlinked TC(s)",
                    file=sys.stderr,
                )
        except Exception as e:
            print(f"tracer predict: semantic L4 failed ({e}) — skipping", file=sys.stderr)

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

    # Write raw JSON files
    (run_dir / "jira_stories.json").write_text(json.dumps(jira_stories_raw, indent=2), encoding="utf-8")
    (run_dir / "defects.json").write_text(json.dumps(defects_raw, indent=2), encoding="utf-8")
    (run_dir / "sdet360testcases.json").write_text(json.dumps(tcm_raw, indent=2), encoding="utf-8")

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

    # Generate HTML report (fails loudly if LLM call fails)
    try:
        reportermod.generate(
            str(run_dir), scope_data, coverage,
            jira_stories_raw, defects_raw, tcm_raw,
            llm_cfg,
            semantic_tcs=l4_included,
            semantic_alerts=l3_alerts + l4_alerts,
        )
        print(f"tracer predict: report.html written ({llm_cfg.provider}/{llm_cfg.model})", file=sys.stderr)
    except Exception as e:
        print(f"tracer predict: report generation failed — {e}", file=sys.stderr)
        return 2

    print(f"tracer predict: {len(selected)} test case(s) → {run_dir}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tracer", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    # ── diff subcommand ───────────────────────────────────────────────────
    d = sub.add_parser("diff", help="scope regression for target's changes relative to base")
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
                   help="write deterministic scope JSON for use by tracer predict")
    d.add_argument("--llm-provider", default=None, dest="llm_provider",
                   metavar="PROVIDER", help="LLM provider: groq or openai (overrides LLM_PROVIDER env)")
    d.add_argument("--llm-model", default=None, dest="llm_model",
                   metavar="MODEL", help="LLM model name (overrides LLM_MODEL env)")

    # ── predict subcommand ────────────────────────────────────────────────
    p = sub.add_parser("predict", help="select test cases from scope.json via TCM + Jira")
    p.add_argument("--scope", required=True, metavar="PATH",
                   help="scope.json from tracer diff --scope")
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
    p.add_argument("--no-predict-ai", action="store_true", dest="no_predict_ai",
                   help="reserved for Phase 2c LangGraph selector (currently always true)")
    p.add_argument("--llm-provider", default=None, dest="llm_provider",
                   metavar="PROVIDER", help="LLM provider: groq or openai (overrides LLM_PROVIDER env)")
    p.add_argument("--llm-model", default=None, dest="llm_model",
                   metavar="MODEL", help="LLM model name (overrides LLM_MODEL env)")

    args = ap.parse_args(argv)

    if args.cmd == "diff":
        return _run_diff(args)
    if args.cmd == "predict":
        return _run_predict(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
