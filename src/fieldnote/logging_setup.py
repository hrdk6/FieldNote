"""Structured logging with run ids and secret redaction."""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
from datetime import UTC, datetime

from fieldnote.config import SECRET_ENV_VARS

run_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("fieldnote_run_id", default="-")
workspace_var: contextvars.ContextVar[str] = contextvars.ContextVar("fieldnote_workspace", default="-")

_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\b\d{6,}:[A-Za-z0-9_\-]{30,}\b"),  # Telegram bot token shape
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),  # Google API key shape
    re.compile(r"(?i)(password|secret|token|api[_-]?key)=([^\s&]+)"),
]


def redact(text: str) -> str:
    """Remove known secret values and secret-shaped strings from ``text``."""
    for name in SECRET_ENV_VARS:
        value = os.environ.get(name)
        if value and len(value) >= 6 and value in text:
            text = text.replace(value, f"<{name}:redacted>")
    for pat in _SECRET_PATTERNS:
        text = pat.sub(lambda m: f"{m.group(1)}=<redacted>", text) if pat.groups >= 2 else pat.sub("<redacted>", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = run_id_var.get()
        record.workspace = workspace_var.get()
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover - malformed log call
            return True
        clean = redact(msg)
        if clean != msg:
            record.msg = clean
            record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "run_id": getattr(record, "run_id", "-"),
            "workspace": getattr(record, "workspace", "-"),
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


_CONFIGURED = False


def setup_logging(level: str = "INFO", fmt: str = "text") -> None:
    global _CONFIGURED
    root = logging.getLogger("fieldnote")
    root.setLevel(level.upper())
    if _CONFIGURED:
        for h in root.handlers:
            h.setLevel(level.upper())
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(RedactingFilter())
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s [%(workspace)s run=%(run_id)s] %(name)s: %(message)s")
        )
    root.addHandler(handler)
    root.propagate = False
    # Keep noisy third-party loggers quiet.
    for noisy in ("urllib3", "trafilatura", "httpx", "httpx2", "anthropic", "matplotlib", "praw", "prawcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    if not name.startswith("fieldnote"):
        name = f"fieldnote.{name}"
    return logging.getLogger(name)
