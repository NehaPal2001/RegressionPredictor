"""Central logging configuration for tracer.

Two sinks with deliberately different jobs:

* **console (stderr)** — the bare message, no timestamp and no traceback, so the
  CLI reads exactly as it did before logging existed. INFO and above.
* **run-dir file** — everything at DEBUG and above, as structured columns::

      [HH:MM:SS.mmm] [LEVEL] [Workflow] [Agent] [Step] Message (Duration)

  plus full tracebacks for any record logged with ``exc_info=True``.

Only the time-of-day appears, not the date: the run folder
(``runs/2026-07-31T11-47-12/``) already carries the date uniquely.

The three context columns are filled in automatically, so ordinary
``log.debug("...")`` calls need no changes:

* **Workflow** — the pipeline stage, from a contextvar set by the
  :func:`workflow` context manager/decorator. Because it is a contextvar, every
  call made underneath a ``with workflow(...)`` block inherits the value without
  passing anything down.
* **Agent** — derived from the logger name (``tracer.tcm_client`` → ``TCMClient``).
* **Step** — the calling function's name by default. Generic HTTP helpers that
  serve several operations take an explicit ``extra={"step": ...}`` from their
  caller, since ``funcName`` alone cannot say which operation ran.
* **Duration** — appended only when the caller measured one and passed
  ``extra={"duration": seconds}``. No placeholder when absent.
"""

from __future__ import annotations

import contextvars
import logging
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

PKG = "tracer"

LOG_FILENAME = "tracer.log"

_FILE_FMT = ("[%(asctime)s.%(msecs)03d] [%(levelname)s] [%(workflow)s] "
             "[%(agent)s] [%(step)s] %(message)s%(duration_suffix)s")
_DATE_FMT = "%H:%M:%S"

# ── workflow (pipeline stage) ────────────────────────────────────────────────
DIFF = "Diff"
PREDICT = "Predict"
GAP_ANALYSIS = "GapAnalysis"
EMAIL_ALERT = "EmailAlert"
SEMANTIC_MATCHING = "SemanticMatching"
JIRA_FETCH = "JiraFetch"
TCM_FETCH = "TCMFetch"
REPORT_GENERATION = "ReportGeneration"
RELEASE = "Release"

_workflow: contextvars.ContextVar[str] = contextvars.ContextVar(
    "tracer_workflow", default=PREDICT
)


@contextmanager
def workflow(name: str) -> Generator[None]:
    """Tag every record logged inside this block with a pipeline stage.

    Usable as a context manager or as a decorator (``@workflow(DIFF)``). Nested
    blocks restore the outer stage on exit, and anything called underneath
    inherits the value through the contextvar — no plumbing through call sites.
    """
    token = _workflow.set(name)
    try:
        yield
    finally:
        _workflow.reset(token)


def current_workflow() -> str:
    """The stage in effect right now — mostly useful in tests."""
    return _workflow.get()


# ── agent (which component logged) ───────────────────────────────────────────
_AGENTS = {
    "tracer": "Core",
    "tracer.cli": "CLI",
    "tracer.jira_client": "JiraClient",
    "tracer.gap_detector": "GapDetector",
    "tracer.tcm_client": "TCMClient",
    "tracer.mailer": "Mailer",
    "tracer.reporter": "Reporter",
    "tracer.predict_cfg": "Config",
    "tracer.llm_config": "LLMConfig",
    "tracer.llm": "LLM",
    "tracer.agent": "Agent",
    "tracer.bridge": "Bridge",
    "tracer.diff": "Diff",
    "tracer.history": "History",
    "tracer.loom_client": "LoomClient",
    "tracer.screens": "Screens",
    "tracer.semantic_matcher": "SemanticMatcher",
}


def _agent_for(logger_name: str) -> str:
    """Map a logger name to a display name, CamelCasing anything unmapped."""
    if logger_name in _AGENTS:
        return _AGENTS[logger_name]
    tail = logger_name.rsplit(".", 1)[-1]
    return "".join(part.capitalize() for part in tail.split("_")) or "Core"


def _format_duration(seconds: float) -> str:
    """Sub-100ms reads better in milliseconds; everything else in seconds."""
    if seconds < 0.1:
        return f"{seconds * 1000:.0f}ms"
    return f"{seconds:.2f}s"


class _ContextFilter(logging.Filter):
    """Stamp workflow/agent/step/duration onto every record.

    Attached to each *handler*, not to the package logger: a logger's filters run
    only for records logged through that exact logger, so a filter on ``tracer``
    would never see anything from ``tracer.cli`` or ``tracer.tcm_client``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if hasattr(record, "workflow"):        # already stamped at emit time
            return True
        record.workflow = _workflow.get()
        record.agent = _agent_for(record.name)
        if not hasattr(record, "step"):        # explicit extra={"step": ...} wins
            record.step = record.funcName
        duration = getattr(record, "duration", None)
        record.duration_suffix = (
            f" ({_format_duration(duration)})" if duration is not None else ""
        )
        return True


class _ConsoleFormatter(logging.Formatter):
    """Message only — no timestamp, no level, no columns, no traceback.

    Tracebacks are deliberately dropped here: a record logged with
    ``exc_info=True`` shows one clean line on the console while the file sink
    still records the full stack.
    """

    def format(self, record: logging.LogRecord) -> str:
        return record.getMessage()


class _PendingHandler(logging.Handler):
    """Holds records until a file handler exists, then replays them into it."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


_pending: _PendingHandler | None = None
_file_handler: logging.FileHandler | None = None


def setup_logging(console_level: str = "INFO") -> logging.Logger:
    """Configure the ``tracer`` package logger. Idempotent — safe to call twice.

    Returns the package logger. Console output goes to stderr so stdout stays
    reserved for the command's actual product (the Jira comment, report paths).
    """
    global _pending

    log = logging.getLogger(PKG)
    log.setLevel(logging.DEBUG)      # handlers do the filtering, not the logger
    log.propagate = False            # never double-emit through the root logger

    if log.handlers:                 # already configured
        return log

    level = getattr(logging, str(console_level).upper(), logging.INFO)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(level)
    console.setFormatter(_ConsoleFormatter())
    console.addFilter(_ContextFilter())
    log.addHandler(console)

    _pending = _PendingHandler()
    _pending.addFilter(_ContextFilter())
    log.addHandler(_pending)

    return log


def attach_run_log(run_dir: Path) -> Path | None:
    """Open ``<run_dir>/tracer.log`` and replay everything logged so far into it.

    Returns the log path, or None if the file could not be opened — logging must
    never be the reason a run fails, so failure here is reported on the console
    and otherwise ignored.
    """
    global _file_handler

    log = logging.getLogger(PKG)
    path = Path(run_dir) / LOG_FILENAME
    try:
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError as e:
        log.warning("RegressIQ: could not open log file %s (%s) — console only", path, e)
        return None

    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(_FILE_FMT, datefmt=_DATE_FMT))
    # Catches records the console handler never saw (DEBUG, when the console is
    # at INFO); replayed records arrive already stamped and pass through.
    handler.addFilter(_ContextFilter())

    if _pending is not None:
        for record in _pending.records:
            handler.handle(record)
        _pending.records.clear()
        log.removeHandler(_pending)

    log.addHandler(handler)
    _file_handler = handler
    log.debug("RegressIQ: log file opened at %s", path)
    return path


def close_run_log() -> None:
    """Flush and detach the file handler. Safe to call when none is attached."""
    global _file_handler
    if _file_handler is None:
        return
    log = logging.getLogger(PKG)
    _file_handler.flush()
    _file_handler.close()
    log.removeHandler(_file_handler)
    _file_handler = None
