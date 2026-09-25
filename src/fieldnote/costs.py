"""LLM cost accounting and per-run budget enforcement."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from fieldnote.logging_setup import get_logger

log = get_logger(__name__)

# USD per million tokens (input, output). Prefix match, longest prefix wins.
PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "mock": (0.0, 0.0),
}
_FALLBACK_PRICE = (5.0, 25.0)
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25


class BudgetExceededError(Exception):
    """Raised before an LLM call that would start after the run budget is spent."""


def price_for(model: str) -> tuple[float, float]:
    best: tuple[int, tuple[float, float]] | None = None
    for prefix, price in PRICING_PER_MTOK.items():
        if model.startswith(prefix) and (best is None or len(prefix) > best[0]):
            best = (len(prefix), price)
    if best is None:
        log.warning("no price table entry for model %s; using conservative fallback", model)
        return _FALLBACK_PRICE
    return best[1]


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    pin, pout = price_for(model)
    cost = (
        input_tokens * pin
        + output_tokens * pout
        + cache_read_tokens * pin * CACHE_READ_MULTIPLIER
        + cache_write_tokens * pin * CACHE_WRITE_MULTIPLIER
    ) / 1_000_000
    return round(cost, 6)


@dataclass
class UsageEvent:
    task: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    cached: bool


@dataclass
class CostTracker:
    """Accumulates usage for a run and enforces ``budget_usd`` (0 disables the limit)."""

    budget_usd: float = 0.0
    events: list[UsageEvent] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def total_cost(self) -> float:
        return round(sum(e.cost_usd for e in self.events), 6)

    @property
    def tokens_in(self) -> int:
        return sum(e.input_tokens for e in self.events)

    @property
    def tokens_out(self) -> int:
        return sum(e.output_tokens for e in self.events)

    @property
    def calls(self) -> int:
        return len(self.events)

    @property
    def cached_calls(self) -> int:
        return sum(1 for e in self.events if e.cached)

    def remaining(self) -> float:
        if self.budget_usd <= 0:
            return float("inf")
        return self.budget_usd - self.total_cost

    def check(self) -> None:
        if self.budget_usd > 0 and self.total_cost >= self.budget_usd:
            raise BudgetExceededError(
                f"LLM budget of ${self.budget_usd:.2f} reached (spent ${self.total_cost:.4f}); "
                "stopping further LLM calls for this run"
            )

    def record(self, event: UsageEvent) -> None:
        with self._lock:
            self.events.append(event)

    def snapshot(self) -> tuple[int, int, int, float]:
        """(n_events, tokens_in, tokens_out, cost) - used by the tracer to compute per-step deltas."""
        return (len(self.events), self.tokens_in, self.tokens_out, self.total_cost)

    def summary(self) -> dict[str, float | int]:
        return {
            "calls": self.calls,
            "cached_calls": self.cached_calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": self.total_cost,
            "budget_usd": self.budget_usd,
        }
