"""Read-only queries against a Loom SQLite graph (~/.loom/projects/<repo>.db).

Loom is ONLY the static graph. Tracer never writes to it and never asks it
about diffs — Tracer owns all git logic.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

CODE_KINDS = ("method", "function")


@dataclass(frozen=True)
class Symbol:
    id: str
    kind: str
    name: str
    path: str
    start_line: int
    end_line: int
    language: str | None


@dataclass(frozen=True)
class Reach:
    """A node reached by walking callers from a changed symbol."""

    symbol: Symbol
    depth: int
    inferred: bool  # True if any hop on the best path was an inferred edge (DI/gRPC bridge)


def _row_symbol(r: sqlite3.Row) -> Symbol:
    return Symbol(r["id"], r["kind"], r["name"], r["path"], r["start_line"] or 0, r["end_line"] or 0, r["language"])


class LoomClient:
    def __init__(self, db_path: str | Path):
        p = Path(db_path).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"Loom DB not found: {p} — run `loom analyze .` inside the repo first")
        # check_same_thread=False: the LangGraph agent runs read-only tool calls in a
        # worker thread. Safe here — the DB is opened mode=ro and tool calls are sequential.
        self.con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, check_same_thread=False)
        self.con.row_factory = sqlite3.Row

    # -- symbol lookup ------------------------------------------------------

    def symbols_at(self, path: str, start: int, end: int) -> list[Symbol]:
        """Smallest code symbols overlapping [start, end] in a file (methods/functions)."""
        rows = self.con.execute(
            f"""SELECT id, kind, name, path, start_line, end_line, language FROM nodes
                WHERE path = ? AND deleted_at IS NULL AND kind IN ({",".join("?" * len(CODE_KINDS))})
                  AND start_line <= ? AND end_line >= ?
                ORDER BY (end_line - start_line) ASC""",
            (path, *CODE_KINDS, end, start),
        ).fetchall()
        return [_row_symbol(r) for r in rows]

    # -- graph walks --------------------------------------------------------

    def blast_radius(self, seed_id: str, max_depth: int = 6) -> list[Reach]:
        """Transitive callers of seed: everything that can break if seed changes.

        Walks CALLS edges in reverse (from_id calls to_id). Bridge edges that Loom
        marks confidence_tier='inferred' (interface->impl dispatch, gRPC proto hop)
        taint the path as inferred — the report labels those "inferred", never
        "confirmed".
        """
        rows = self.con.execute(
            """WITH RECURSIVE walk(id, depth, inferred) AS (
                   SELECT ?, 0, 0
                   UNION
                   SELECT e.from_id, w.depth + 1,
                          CASE WHEN w.inferred = 1 OR e.confidence_tier = 'inferred' THEN 1 ELSE 0 END
                   FROM edges e JOIN walk w ON e.to_id = w.id
                   WHERE e.kind = 'CALLS' AND w.depth < ?
               )
               SELECT n.id, n.kind, n.name, n.path, n.start_line, n.end_line, n.language,
                      MIN(w.depth) AS depth, MIN(w.inferred) AS inferred
               FROM walk w JOIN nodes n ON n.id = w.id
               WHERE w.id != ? AND n.deleted_at IS NULL
               GROUP BY n.id""",
            (seed_id, max_depth, seed_id),
        ).fetchall()
        return [Reach(_row_symbol(r), r["depth"], bool(r["inferred"])) for r in rows]

    def fan_in(self, node_id: str) -> int:
        return self.con.execute(
            """SELECT COUNT(DISTINCT n.path) FROM edges e
               JOIN nodes n ON n.id = e.from_id
               WHERE e.kind='CALLS' AND e.to_id = ? AND n.deleted_at IS NULL""",
            (node_id,),
        ).fetchone()[0]

    # -- history & test signals (precomputed by Loom) -----------------------

    def coupled_files(self, path: str, exclude: set[str], limit: int = 5) -> list[tuple[str, float]]:
        """Files that historically co-change with `path` (Loom COUPLED_WITH), strongest first."""
        rows = self.con.execute(
            """SELECT from_id, to_id, metadata FROM edges
               WHERE kind='COUPLED_WITH' AND (from_id = ? OR to_id = ?)""",
            (f"file:{path}", f"file:{path}"),
        ).fetchall()
        out = []
        for r in rows:
            other = (r["to_id"] if r["from_id"] == f"file:{path}" else r["from_id"]).removeprefix("file:")
            if other in exclude:
                continue
            freq = json.loads(r["metadata"]).get("coupling_frequency", 0.0)
            out.append((other, freq))
        out.sort(key=lambda t: -t[1])
        return out[:limit]

    def tests_for(self, node_id: str) -> list[Symbol]:
        """Test symbols linked to a production symbol (Loom TESTED_BY: from=test, to=prod)."""
        rows = self.con.execute(
            """SELECT n.id, n.kind, n.name, n.path, n.start_line, n.end_line, n.language
               FROM edges e JOIN nodes n ON n.id = e.from_id
               WHERE e.kind='TESTED_BY' AND e.to_id = ? AND n.deleted_at IS NULL""",
            (node_id,),
        ).fetchall()
        return [_row_symbol(r) for r in rows]

    # -- navigation for the investigator agent (Loom is the map) -------------

    _SYM_COLS = "id, kind, name, path, start_line, end_line, language"

    def get_symbol(self, node_id: str) -> Symbol | None:
        r = self.con.execute(
            f"SELECT {self._SYM_COLS} FROM nodes WHERE id=? AND deleted_at IS NULL", (node_id,)
        ).fetchone()
        return _row_symbol(r) if r else None

    def search(self, query: str, limit: int = 10) -> list[Symbol]:
        """Find code symbols by name substring — the agent asks Loom 'where is X'."""
        rows = self.con.execute(
            f"""SELECT {self._SYM_COLS} FROM nodes
                WHERE name LIKE ? AND deleted_at IS NULL
                  AND kind IN ({",".join("?" * len(CODE_KINDS))})
                ORDER BY LENGTH(name) ASC LIMIT ?""",
            (f"%{query}%", *CODE_KINDS, limit),
        ).fetchall()
        return [_row_symbol(r) for r in rows]

    def callees(self, node_id: str) -> list[Symbol]:
        """What this symbol calls — the agent follows the graph downward."""
        rows = self.con.execute(
            f"""SELECT n.{self._SYM_COLS.replace(', ', ', n.')} FROM edges e
                JOIN nodes n ON n.id = e.to_id
                WHERE e.kind='CALLS' AND e.from_id=? AND n.deleted_at IS NULL""",
            (node_id,),
        ).fetchall()
        return [_row_symbol(r) for r in rows]

    def api_call_sites(self, service_path_like: str = "%api.service%") -> list[tuple[str, str, str]]:
        """Client ApiService callers: (from_id, verb_method_name, call_context) for each CALLS
        edge into a get/post/put/delete/patch method whose file matches service_path_like.
        call_context (the endpoint-string argument) is read from the edge's metadata JSON."""
        rows = self.con.execute(
            """SELECT e.from_id, n.name, e.metadata FROM edges e
               JOIN nodes n ON n.id = e.to_id
               WHERE e.kind='CALLS' AND n.deleted_at IS NULL
                 AND n.name IN ('get','post','put','delete','patch')
                 AND n.path LIKE ?""",
            (service_path_like,),
        ).fetchall()
        out = []
        for r in rows:
            try:
                ctx = json.loads(r["metadata"] or "{}").get("call_context", "")
            except (ValueError, TypeError):
                ctx = ""
            if ctx:
                out.append((r["from_id"], r["name"], ctx))
        return out
