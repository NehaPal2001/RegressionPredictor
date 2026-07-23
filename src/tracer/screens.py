"""Controller -> endpoint -> screen resolver (deterministic, runs on Loom's graph).

A blast-radius node is an endpoint if its file is a Spring @RestController/@Controller
and the method carries a mapping annotation. Screen names come from screens.yaml
(flat `url-prefix: Screen Name` map, longest prefix wins) with a humanized
controller-class-name fallback so the tool works before anyone curates YAML.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

MAPPING = re.compile(r"@(Get|Post|Put|Delete|Patch|Request)Mapping\s*(?:\(([^)]*)\))?")
STRING_LIT = re.compile(r'"([^"]*)"')
REQ_METHOD = re.compile(r"RequestMethod\.(\w+)")
CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
PREAUTH = re.compile(r"@PreAuthorize\s*\(\s*\"(.*?)\"\s*\)")
ROLE_TOKEN = re.compile(r"ROLE_([A-Z]+)")
# Java package segment right after tenant/ or master/ is the feature module.
MODULE_SEG = re.compile(r"/(?:tenant|master)/([a-z][a-z0-9]*)/")


@dataclass(frozen=True)
class Endpoint:
    verb: str
    url: str
    screen: str
    controller: str
    method_name: str
    node_id: str
    module: str = ""
    roles: tuple[str, ...] = ()  # human role names, () = any authenticated user


def humanize(controller_class: str) -> str:
    return CAMEL.sub(" ", controller_class.removesuffix("Controller"))


def module_of(path: str) -> str:
    """Feature module from a Java package path (…/tenant/<module>/… → 'Module')."""
    m = MODULE_SEG.search(path)
    return _humanize_module(m.group(1)) if m else ""


_MODULE_NAMES = {
    "testcasemanagement": "Test Case Management",
    "testcaseexecution": "Test Case Execution",
    "businessrequirement": "Business Requirement",
    "automationapitesting": "API Testing",
    "testplanuniseries": "Test Plan",
}


def _humanize_module(seg: str) -> str:
    return _MODULE_NAMES.get(seg, seg.capitalize())


def roles_from(annotation_lines: list[str]) -> tuple[str, ...]:
    """Role names from a @PreAuthorize expression. Empty tuple = any authenticated user."""
    for line in annotation_lines:
        m = PREAUTH.search(line)
        if not m:
            continue
        roles = tuple(dict.fromkeys(t.capitalize() for t in ROLE_TOKEN.findall(m.group(1))))
        return roles  # ROLE_ADMIN → 'Admin'; isAuthenticated() → ()
    return ()


def load_screen_map(path: str | Path) -> dict[str, str]:
    """Flat YAML subset: one `prefix: Screen Name` per line. Missing file -> empty map."""
    p = Path(path)
    if not p.exists():
        return {}
    out = {}
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip().strip("\"'")] = v.strip().strip("\"'")
    return out


def screen_for(url: str, fallback: str, screen_map: dict[str, str]) -> str:
    best = ""
    for prefix in screen_map:
        if url.startswith(prefix) and len(prefix) > len(best):
            best = prefix
    return screen_map[best] if best else fallback


def _mapping_of(text_lines: list[str]) -> tuple[str, str] | None:
    """(verb, path) from the first mapping annotation in the given lines."""
    for line in text_lines:
        m = MAPPING.search(line)
        if not m:
            continue
        kind, args = m.group(1), m.group(2) or ""
        lit = STRING_LIT.search(args)
        url = lit.group(1) if lit else ""
        if kind == "Request":
            rm = REQ_METHOD.search(args)
            verb = rm.group(1) if rm else "ANY"
        else:
            verb = kind.upper()
        return verb, url
    return None


class EndpointIndex:
    """Parses controller files once; answers `endpoint_for(symbol)` for graph nodes."""

    def __init__(self, repo_root: str | Path, screen_map: dict[str, str]):
        self.root = Path(repo_root)
        self.screen_map = screen_map
        self._files: dict[str, list[str] | None] = {}  # path -> lines (None = not a controller)

    def _controller_lines(self, rel_path: str) -> list[str] | None:
        if rel_path not in self._files:
            p = self.root / rel_path
            lines: list[str] | None = None
            if p.suffix == ".java" and p.exists():
                text = p.read_text(errors="replace")
                if "@RestController" in text or "@Controller" in text:
                    lines = text.splitlines()
            self._files[rel_path] = lines
        return self._files[rel_path]

    def endpoint_for(self, node_id: str, name: str, path: str, start_line: int) -> Endpoint | None:
        lines = self._controller_lines(path)
        if lines is None or start_line <= 0:
            return None
        # class-level @RequestMapping prefix: annotations above the class declaration
        header = []
        class_decl_idx = 0
        for i, line in enumerate(lines):
            header.append(line)
            if re.search(r"\bclass\s+\w+", line):
                class_decl_idx = i
                break
        class_map = _mapping_of(header)
        prefix = class_map[1] if class_map else ""
        # method-level annotation: Loom's start_line points at the method; annotations
        # sit just above it (or are included when tree-sitter counts modifiers).
        # Window must not reach back into the class header, or the class-level
        # @RequestMapping would masquerade as the first method's mapping.
        # start_line is 1-based; slice indices 0-based: 1-based lines
        # [start_line-6 .. start_line+3] == slice [start_line-7 : start_line+3].
        lo = max(class_decl_idx + 1, start_line - 7)
        window = lines[lo : start_line + 3]
        method_map = _mapping_of(window)
        if method_map is None:
            return None
        verb, mpath = method_map
        url = (prefix.rstrip("/") + "/" + mpath.lstrip("/")).rstrip("/") or "/"
        controller = Path(path).stem
        # roles: method-level @PreAuthorize wins; fall back to class-level
        roles = roles_from(window) or roles_from(header)
        return Endpoint(
            verb=verb,
            url=url,
            screen=screen_for(url, humanize(controller), self.screen_map),
            controller=controller,
            method_name=name.rsplit(".", 1)[-1],
            node_id=node_id,
            module=module_of(path),
            roles=roles,
        )
