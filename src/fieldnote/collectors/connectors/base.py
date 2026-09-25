"""Pluggable metrics connector interface.

A connector turns an external, *sanctioned* data source (a CSV you downloaded manually from a
public dashboard, or a JSON endpoint you are permitted to query) into ``MetricRecord`` rows.
Add a connector by dropping a module in this package that subclasses :class:`MetricConnector` and
decorates it with ``@register("your_type")``.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dateutil import parser as dtparser

from fieldnote.collectors.base import PoliteHttpClient
from fieldnote.config import REPO_ROOT, ConfigError, ConnectorConfig, WorkspaceConfig, data_dir, fixtures_dir


@dataclass
class MetricRecord:
    entity: str
    region: str
    period: str  # YYYY-MM
    value: float


@dataclass
class ConnectorContext:
    workspace: WorkspaceConfig
    http: PoliteHttpClient | None = None


class MetricConnector(ABC):
    type_name: str = "base"

    def __init__(self, cfg: ConnectorConfig, ctx: ConnectorContext) -> None:
        self.cfg = cfg
        self.ctx = ctx

    @abstractmethod
    def load(self) -> list[MetricRecord]: ...

    def describe(self) -> str:
        return self.cfg.display_name


_PERIOD_PATTERNS = [
    (re.compile(r"^(\d{4})-(\d{1,2})$"), lambda m: f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"),
    (re.compile(r"^(\d{4})/(\d{1,2})$"), lambda m: f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"),
    (re.compile(r"^(\d{1,2})[/-](\d{4})$"), lambda m: f"{int(m.group(2)):04d}-{int(m.group(1)):02d}"),
    (re.compile(r"^(\d{4})(\d{2})$"), lambda m: f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"),
]


def normalize_period(raw: str) -> str:
    """Normalise a month-ish value (2026-08, 2026-08-01, Aug 2026, 08/2026) to YYYY-MM."""
    s = str(raw).strip()
    for pat, fmt in _PERIOD_PATTERNS:
        m = pat.match(s)
        if m:
            period = fmt(m)
            month = int(period[5:])
            if not 1 <= month <= 12:
                raise ValueError(f"invalid month in period {raw!r}")
            return period
    try:
        dt = dtparser.parse(s, default=datetime(2000, 1, 1))
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"unrecognised period {raw!r}") from exc
    return f"{dt.year:04d}-{dt.month:02d}"


def parse_number(raw: object) -> float:
    if isinstance(raw, int | float):
        return float(raw)
    s = str(raw).strip().replace(",", "").replace("₹", "").replace("$", "")
    if s in ("", "-", "NA", "N/A", "null", "None"):
        raise ValueError("empty value")
    return float(s)


def resolve_data_path(path: str, cfg: WorkspaceConfig) -> Path:
    """Resolve a connector file path and refuse anything outside the allowed data roots."""
    allowed = [Path.cwd().resolve(), REPO_ROOT.resolve(), fixtures_dir().resolve(), data_dir().resolve()]
    candidates: list[Path] = []
    p = Path(path).expanduser()
    if p.is_absolute():
        candidates.append(p)
    else:
        candidates.append(Path.cwd() / p)
        candidates.append(REPO_ROOT / p)
        if cfg.source_path and not cfg.source_path.startswith("<"):
            candidates.append(Path(cfg.source_path.split(" (+")[0]).parent / p)
    for c in candidates:
        rc = c.resolve()
        if not any(rc == a or a in rc.parents for a in allowed):
            continue
        if rc.exists():
            return rc
    raise ConfigError(
        f"connector file {path!r} not found (or outside allowed directories {', '.join(str(a) for a in allowed)})"
    )
