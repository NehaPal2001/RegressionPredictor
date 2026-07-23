"""Cross-repo bridge: backend REST endpoint → real Angular client screen.

Deterministic, no LLM, no network. Joins backend endpoints (screens.py) to Angular
`ApiService` call sites stored in the client Loom graph, walks the client CALLS graph
up to the routed component, and names the screen from `app.routes.ts`. Every bridged
screen is labeled `inferred` — the LLM never decides reachability.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .loom_client import LoomClient, Symbol
from .screens import Endpoint

WILDCARD = "{*}"

# ${clientInterp} and {backendPathVar} both collapse to one opaque segment.
_INTERP = re.compile(r"\$?\{[^{}]*\}")
# first string/template-literal arg of an apiService.VERB(...) call
_CALL_ARG = re.compile(r"""apiService\.\w+\s*\(\s*(['"`])(.*?)\1""", re.DOTALL)
# route parsing
_PATH = re.compile(r"""path\s*:\s*['"]([^'"]*)['"]""")
_COMP = re.compile(r"""component\s*:\s*(\w+)""")
_CHILDREN = re.compile(r"\bchildren\s*:")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


@dataclass(frozen=True)
class ScreenMapping:
    endpoint: Endpoint
    screen_name: str
    client_component: str
    confidence: str = "inferred"


@dataclass
class BridgeResult:
    mappings: list[ScreenMapping]
    unresolved: list[Endpoint]
    match_rate: float  # resolved / total endpoints


# -- normalization (the join key) -------------------------------------------


def normalize_path(raw: str) -> list[str]:
    """URL template → segment list: drop query, strip leading /, ${..}/{var} → {*}, lower."""
    raw = raw.split("?", 1)[0].strip().lstrip("/")
    raw = _INTERP.sub(WILDCARD, raw).lower()
    return [s for s in raw.split("/") if s]


def suffix_match(backend_segs: list[str], client_segs: list[str]) -> bool:
    """Client segments are a suffix of backend segments (absorbs the backend's /api,/v1
    prefix the client omits). Literals must be equal in aligned trailing positions; {*}
    matches any one segment. Require >=2 literal matches to reject 1-segment coincidence."""
    if not client_segs or len(client_segs) > len(backend_segs):
        return False
    literal_matches = 0
    for c, b in zip(reversed(client_segs), reversed(backend_segs)):
        if c != WILDCARD and b != WILDCARD:
            if c != b:
                return False
            literal_matches += 1
    return literal_matches >= 2


# -- client call-site extraction --------------------------------------------


def extract_endpoint(call_context: str) -> str | None:
    """First literal endpoint arg of an apiService.VERB(...) call. None if the string is
    fully indirected (no literal segments, e.g. `${this.base()}?page=0`) — unmatchable by A."""
    m = _CALL_ARG.search(call_context)
    if not m:
        return None
    raw = m.group(2)
    if all(seg == WILDCARD for seg in normalize_path(raw)):  # no literal to join on
        return None
    return raw


def extract_verb(method_name: str) -> str:
    """apiService verb method name → HTTP verb (get → GET)."""
    return method_name.rsplit(".", 1)[-1].upper()


# -- component → screen ------------------------------------------------------


def _humanize_class(component: str) -> str:
    """NewDashboardComponent → 'New Dashboard'."""
    return _CAMEL.sub(" ", component.removesuffix("Component"))


def _humanize_path(route_path: str) -> str:
    """dashboard/testcase-vs-assignee → 'Dashboard Testcase Vs Assignee'."""
    return " ".join(w.capitalize() for w in re.split(r"[/\-_]", route_path) if w)


def _top_level_objects(region: str):
    """Yield each top-level {...} object substring in an array's inner text (brace depth)."""
    depth = 0
    start = -1
    for i, ch in enumerate(region):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                yield region[start : i + 1]
                start = -1


def _children_region(obj: str) -> str | None:
    """Inner text of this object's `children: [ ... ]` array, or None."""
    m = _CHILDREN.search(obj)
    if not m:
        return None
    lb = obj.find("[", m.end())
    if lb < 0:
        return None
    depth = 0
    for i in range(lb, len(obj)):
        if obj[i] == "[":
            depth += 1
        elif obj[i] == "]":
            depth -= 1
            if depth == 0:
                return obj[lb + 1 : i]
    return None


def _walk_routes(region: str, prefix: str, out: dict[str, str]) -> None:
    for obj in _top_level_objects(region):
        child = _CHILDREN.search(obj)
        own = obj[: child.start()] if child else obj  # this object's own keys, not children'
        pm = _PATH.search(own)
        cm = _COMP.search(own)
        full = "/".join(p for p in (prefix, pm.group(1) if pm else "") if p)
        if cm:
            out[cm.group(1)] = full
        inner = _children_region(obj)
        if inner is not None:
            _walk_routes(inner, full, out)


def parse_routes(routes_ts_text: str) -> dict[str, str]:
    """Component class name → route path, composing nested `children` arrays. Tolerant."""
    out: dict[str, str] = {}
    _walk_routes(routes_ts_text, "", out)
    return out


def component_screen(component: str, routes: dict[str, str]) -> str:
    """Route path (humanized) if routed, else humanized class name."""
    if component in routes:
        return _humanize_path(routes[component]) or _humanize_class(component)
    return _humanize_class(component)


# -- orchestrator ------------------------------------------------------------


def _component_of(sym: Symbol) -> str | None:
    """Component class name if this symbol belongs to an Angular component, else None."""
    for seg in sym.name.split("."):
        if seg.endswith("Component"):
            return seg
    if sym.path.endswith(".component.ts"):
        stem = Path(sym.path).name[: -len(".component.ts")]
        return "".join(w.capitalize() for w in re.split(r"[-_]", stem) if w) + "Component"
    return None


def _find_component(client_lc: LoomClient, call_site_id: str) -> str | None:
    """Walk UP the client CALLS graph from a call-site method to the nearest component."""
    seed = client_lc.get_symbol(call_site_id)
    if seed is not None and (c := _component_of(seed)):
        return c  # component calls apiService directly
    best: tuple[int, str] | None = None
    for r in client_lc.blast_radius(call_site_id):
        c = _component_of(r.symbol)
        if c and (best is None or r.depth < best[0]):
            best = (r.depth, c)
    return best[1] if best else None


def _load_routes(client_repo: str | Path | None) -> dict[str, str]:
    if not client_repo:
        return {}
    root = Path(client_repo)
    out: dict[str, str] = {}
    for pat in ("**/*.routes.ts", "**/*-routing.module.ts"):
        for f in root.glob(pat):
            try:
                out.update(parse_routes(f.read_text(errors="replace")))
            except OSError:
                pass
    return out


def resolve_client_screens(
    endpoints: list[Endpoint], client_lc: LoomClient, client_repo: str | Path | None
) -> BridgeResult:
    """Join backend endpoints to client ApiService call sites → real screens. Unmatched
    endpoints (incl. those only reached by fully-indirected client calls) go to `unresolved`."""
    routes = _load_routes(client_repo)
    backend = [(e, normalize_path(e.url)) for e in endpoints]
    mappings: list[ScreenMapping] = []
    matched: set[str] = set()
    seen: set[tuple[str, str, str]] = set()

    for from_id, verb_name, call_context in client_lc.api_call_sites():
        raw = extract_endpoint(call_context)
        if raw is None:  # fully indirected — cannot match, leaves endpoints unresolved
            continue
        verb = extract_verb(verb_name)
        client_segs = normalize_path(raw)
        comp: str | None = None
        walked = False
        for ep, be_segs in backend:
            if ep.verb not in (verb, "ANY"):
                continue
            if not suffix_match(be_segs, client_segs):
                continue
            if not walked:  # only walk the graph once we have a match
                comp, walked = _find_component(client_lc, from_id), True
            screen = component_screen(comp, routes) if comp else ep.screen
            key = (ep.node_id, screen, comp or "")
            if key not in seen:
                seen.add(key)
                mappings.append(ScreenMapping(ep, screen, comp or "", "inferred"))
            matched.add(ep.node_id)

    unresolved = [e for e in endpoints if e.node_id not in matched]
    match_rate = len(matched) / len(endpoints) if endpoints else 0.0
    return BridgeResult(mappings, unresolved, match_rate)
