"""Deterministic risk rules. Every level cites its reason — nothing is a black box.

AI never runs here. The rules are a fixed, auditable table:
  HIGH   - change re-touches lines a past fix commit touched (defect recurrence)
  HIGH   - .proto contract changed (hits Java client + Python server + generated code)
  MEDIUM - changed symbol is widely called (fan-in >= FAN_IN_MEDIUM)
  LOW    - reachable from the change, no aggravating signal
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date

from .history import Recurrence
from .loom_client import Symbol

FAN_IN_MEDIUM = 5
HUB_FAN_IN = 25
DEEP_HOP = 5
RECENCY_DAYS = 90
LEVELS = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


@dataclass
class SymbolRisk:
    symbol: Symbol
    level: str
    reasons: list[str] = field(default_factory=list)
    fan_in: int = 0
    is_proto: bool = False


def _overlaps(sym: Symbol, rec: Recurrence) -> bool:
    return sym.path == rec.path and sym.start_line <= rec.end and sym.end_line >= rec.start


def assess_symbol(
    sym: Symbol, fan_in: int, recurrences: list[Recurrence], proto_changed: bool
) -> SymbolRisk:
    level, reasons = "LOW", []
    for rec in recurrences:
        if _overlaps(sym, rec):
            rec_is_recent = (_date.today() - _date.fromisoformat(rec.date)).days < RECENCY_DAYS
            if rec_is_recent:
                level = "HIGH"
                reasons.append(
                    f"re-touches lines fixed by commit {rec.sha} on {rec.date} (\"{rec.subject}\") — "
                    "the same class of bug can return; test that prior bug's scenario hard"
                )
            else:
                if level != "HIGH":
                    level = "MEDIUM"
                reasons.append(
                    f"previously-fixed lines (commit {rec.sha}, {rec.date}, >=90 days ago — "
                    "risk decayed; still worth a targeted check)"
                )
    is_proto = proto_changed and sym.path.endswith(".proto")
    if is_proto:
        level = "HIGH"
        reasons.append(
            "gRPC contract change: affects the Java client, the Python server, and all generated stubs"
        )
    if fan_in >= FAN_IN_MEDIUM and level != "HIGH":
        level = "MEDIUM"
    if fan_in >= FAN_IN_MEDIUM:
        reasons.append(f"widely used: {fan_in} distinct caller file{'s' if fan_in != 1 else ''}")
    if not reasons:
        reasons.append("directly changed in this diff")
    return SymbolRisk(sym, level, reasons, fan_in, is_proto)


def cap_reach_risk(
    level: str, depth: int, seed_fan_in: int, seed_is_proto: bool = False
) -> tuple[str, str | None]:
    """Cap risk for blast-radius nodes reached at great depth through a hub.

    Returns (final_level, cap_reason_or_None).
    Proto seed changes are always global escalation and are never capped.
    """
    if seed_is_proto:
        return level, None
    if depth >= DEEP_HOP and seed_fan_in >= HUB_FAN_IN and LEVELS[level] > LEVELS["LOW"]:
        return "LOW", "distant, hub-mediated path"
    return level, None


def combine(feature_risks: list[SymbolRisk]) -> tuple[str, list[str]]:
    """Feature risk = max over the changed symbols that reach it; cites the winners."""
    if not feature_risks:
        return "LOW", ["reachable from this change"]
    top = max(LEVELS[r.level] for r in feature_risks)
    level = next(k for k, v in LEVELS.items() if v == top)
    reasons = [x for r in feature_risks if LEVELS[r.level] == top for x in r.reasons]
    # dedup, keep order
    return level, list(dict.fromkeys(reasons))
