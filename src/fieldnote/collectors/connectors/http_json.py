"""HTTP JSON connector: a URL returning JSON plus a JSONPath-style (dotted) field mapping.

Example::

    - type: http_json
      url: https://example.org/api/sales.json
      records_path: data.rows          # where the list of records lives
      mapping: {entity: brand.name, region: state, period: month, value: metrics.units}

Only use endpoints whose terms permit automated access. robots.txt is honoured.
"""

from __future__ import annotations

import json
from typing import Any

from fieldnote.collectors.connectors.base import MetricConnector, MetricRecord, normalize_period, parse_number
from fieldnote.collectors.connectors.registry import register
from fieldnote.config import ConfigError


def get_path(obj: Any, path: str | None) -> Any:
    """Resolve a dotted path like ``data.items.0.name`` (``$.`` prefix allowed)."""
    if not path:
        return obj
    path = path.removeprefix("$.").removeprefix("$")
    cur = obj
    for part in [p for p in path.split(".") if p]:
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError) as exc:
                raise KeyError(path) from exc
        elif isinstance(cur, dict):
            if part not in cur:
                raise KeyError(path)
            cur = cur[part]
        else:
            raise KeyError(path)
    return cur


@register("http_json")
class HttpJsonConnector(MetricConnector):
    type_name = "http_json"

    def load(self) -> list[MetricRecord]:
        if not self.cfg.url:
            raise ConfigError(f"connector {self.describe()}: 'url' is required for http_json")
        if self.ctx.http is None:
            raise ConfigError("http_json connector needs network access (not available offline)")
        res = self.ctx.http.get(self.cfg.url, conditional=False)
        if not res.ok:
            raise ConfigError(f"connector {self.describe()}: HTTP {res.status}")
        data = json.loads(res.text)
        records = get_path(data, self.cfg.records_path)
        if not isinstance(records, list):
            raise ConfigError(f"connector {self.describe()}: records_path did not resolve to a list")
        m = self.cfg.mapping
        out = []
        for rec in records:
            try:
                out.append(
                    MetricRecord(
                        entity=str(get_path(rec, m["entity"])).strip(),
                        region=str(get_path(rec, m["region"])).strip() if "region" in m else "all",
                        period=normalize_period(str(get_path(rec, m["period"]))),
                        value=parse_number(get_path(rec, m["value"])),
                    )
                )
            except (KeyError, ValueError):
                continue
        return out
