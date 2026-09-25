"""Connector registry. Drop a module into this package with a ``@register("type")`` class to add one."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable
from typing import TYPE_CHECKING

from fieldnote.config import ConfigError, ConnectorConfig

if TYPE_CHECKING:
    from fieldnote.collectors.connectors.base import ConnectorContext, MetricConnector

CONNECTORS: dict[str, type[MetricConnector]] = {}
_DISCOVERED = False


def register(type_name: str) -> Callable[[type[MetricConnector]], type[MetricConnector]]:
    def deco(cls: type[MetricConnector]) -> type[MetricConnector]:
        CONNECTORS[type_name] = cls
        cls.type_name = type_name
        return cls

    return deco


def discover() -> dict[str, type[MetricConnector]]:
    global _DISCOVERED
    if not _DISCOVERED:
        import fieldnote.collectors.connectors as pkg

        for mod in pkgutil.iter_modules(pkg.__path__):
            if mod.name in ("base", "registry"):
                continue
            importlib.import_module(f"{pkg.__name__}.{mod.name}")
        _DISCOVERED = True
    return CONNECTORS


def build_connector(cfg: ConnectorConfig, ctx: ConnectorContext) -> MetricConnector:
    registry = discover()
    cls = registry.get(cfg.type)
    if cls is None:
        raise ConfigError(f"unknown connector type '{cfg.type}'. Available: {', '.join(sorted(registry))}")
    return cls(cfg, ctx)
