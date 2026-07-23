"""tracer diff <base> <target> — regression scope for what `target` introduced.

Exit codes: 0 success, 2 git/ref error (never a silent false "all clear").
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from . import agent as agentmod
from . import bridge as bridgemod
from . import diff as diffmod
from . import history, jira_mock, llm, risk
from .loom_client import LoomClient, Reach
from .report import Feature, jira_comment, render_html
from .screens import EndpointIndex, load_screen_map


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
    r = subprocess.run(["loom", "analyze", "."], cwd=repo_path, capture_output=True, text=True)
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tracer", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
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
    args = ap.parse_args(argv)

    try:
        scope, changed, feats, recs, coupled, tests, unmatched, blind_spots, lc = analyze(
            args.repo, args.base, args.target, args.db, args.two_dot, args.max_depth,
            args.reindex,
        )
    except (diffmod.GitError, FileNotFoundError) as e:
        print(f"tracer: {e}", file=sys.stderr)
        return 2

    if args.no_ai:
        notes, status = None, "disabled with --no-ai"
    else:
        key = llm.load_key(Path(args.repo) / ".env", Path(__file__).parents[2] / ".env")
        print(f"tracer: investigating top {args.investigate} screen(s) via Loom + LLM agent…",
              file=sys.stderr)
        notes, status = agentmod.try_investigate(
            agent_scope(scope, changed, feats, recs, top=args.investigate),
            lc, Path(args.repo).resolve(), key,
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


if __name__ == "__main__":
    sys.exit(main())
