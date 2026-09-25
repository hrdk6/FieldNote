"""CSV connector: a file you downloaded manually from a public dashboard or industry release."""

from __future__ import annotations

import csv

from fieldnote.collectors.connectors.base import (
    MetricConnector,
    MetricRecord,
    normalize_period,
    parse_number,
    resolve_data_path,
)
from fieldnote.collectors.connectors.registry import register
from fieldnote.config import ConfigError
from fieldnote.logging_setup import get_logger

log = get_logger(__name__)


@register("csv_file")
class CsvFileConnector(MetricConnector):
    type_name = "csv_file"

    def load(self) -> list[MetricRecord]:
        if not self.cfg.path:
            raise ConfigError(f"connector {self.describe()}: 'path' is required for csv_file")
        path = resolve_data_path(self.cfg.path, self.ctx.workspace)
        mapping = self.cfg.mapping
        out: list[MetricRecord] = []
        skipped = 0
        with path.open(newline="", encoding="utf-8-sig") as fh:
            # Lines starting with '#' are comments (e.g. provenance notes at the top of the file).
            reader = csv.DictReader(line for line in fh if not line.lstrip().startswith("#"))
            fields = set(reader.fieldnames or [])
            missing = {
                v for k, v in mapping.items() if k in ("entity", "period", "value", "region") and v not in fields
            }
            if missing:
                raise ConfigError(f"connector {self.describe()}: CSV {path.name} lacks columns {sorted(missing)}")
            for row in reader:
                try:
                    out.append(
                        MetricRecord(
                            entity=row[mapping["entity"]].strip(),
                            region=(row.get(mapping["region"], "") if "region" in mapping else "all").strip() or "all",
                            period=normalize_period(row[mapping["period"]]),
                            value=parse_number(row[mapping["value"]]),
                        )
                    )
                except (ValueError, KeyError):
                    skipped += 1
        if skipped:
            log.warning("connector %s: skipped %d unparseable rows", self.describe(), skipped)
        return out
