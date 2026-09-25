"""Agent tracing to ``agent_traces`` (the data source for the dashboard's Run Inspector)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from fieldnote.costs import CostTracker
from fieldnote.db.engine import Database
from fieldnote.db.models import AgentTrace
from fieldnote.logging_setup import get_logger, redact

log = get_logger(__name__)


@dataclass
class TraceStep:
    agent: str
    step: str
    input_summary: str = ""
    output_summary: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    status: str = "ok"
    error: str = ""


class Tracer:
    def __init__(self, db: Database | None, workspace: str, run_id: int | None, cost: CostTracker) -> None:
        self.db = db
        self.workspace = workspace
        self.run_id = run_id
        self.cost = cost
        self.steps: list[dict[str, Any]] = []
        self._pending: list[dict[str, Any]] = []

    @contextmanager
    def step(self, agent: str, step: str, input_summary: str = "") -> Iterator[TraceStep]:
        ts = TraceStep(agent=agent, step=step, input_summary=input_summary)
        _, tin0, tout0, cost0 = self.cost.snapshot()
        start = time.perf_counter()
        try:
            yield ts
        except Exception as exc:
            ts.status = "failed"
            ts.error = redact(f"{type(exc).__name__}: {exc}")[:2000]
            raise
        finally:
            _, tin1, tout1, cost1 = self.cost.snapshot()
            row = {
                "agent": agent,
                "step": step,
                "status": ts.status,
                "input_summary": redact(ts.input_summary)[:4000],
                "output_summary": redact(ts.output_summary)[:4000],
                "tool_calls": ts.tool_calls[:100],
                "error": ts.error,
                "tokens_in": tin1 - tin0,
                "tokens_out": tout1 - tout0,
                "cost": round(cost1 - cost0, 6),
                "latency_ms": int((time.perf_counter() - start) * 1000),
            }
            self.steps.append(row)
            self._pending.append(row)

    def flush(self) -> int:
        """Persist buffered trace rows (buffered so tracing never contends with open write transactions)."""
        if self.db is None or not self._pending:
            return 0
        rows, self._pending = self._pending, []
        try:
            with self.db.session() as s:
                for row in rows:
                    s.add(AgentTrace(workspace=self.workspace, run_id=self.run_id, **row))
        except Exception:  # tracing must never break a run
            log.exception("failed to persist agent traces")
            return 0
        return len(rows)
