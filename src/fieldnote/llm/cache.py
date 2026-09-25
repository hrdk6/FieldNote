"""Response cache keyed by hash(model + prompt + schema). Makes reruns and tests cheap and deterministic."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fieldnote.db.engine import Database
from fieldnote.db.models import LLMCache


def cache_key(**parts: Any) -> str:
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class LLMCacheStore:
    """Two-level cache: in-memory, plus the ``llm_cache`` table.

    Writes are buffered and persisted by :meth:`flush` (called by the pipeline between stages) so
    cache inserts never contend with an open write transaction on SQLite.
    """

    def __init__(self, db: Database | None, workspace: str = "*") -> None:
        self.db = db
        self.workspace = workspace
        self._memory: dict[str, str] = {}
        self._pending: dict[str, dict[str, object]] = {}

    def get(self, key: str) -> str | None:
        if key in self._memory:
            return self._memory[key]
        if self.db is None:
            return None
        with self.db.session() as s:
            row = s.get(LLMCache, key)
            if row is None:
                return None
            self._memory[key] = row.response
            return row.response

    def put(self, key: str, response: str, *, model: str, task: str, input_tokens: int, output_tokens: int) -> None:
        self._memory[key] = response
        if self.db is not None:
            self._pending[key] = {
                "response": response,
                "model": model,
                "task": task,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }

    def flush(self) -> int:
        if self.db is None or not self._pending:
            return 0
        pending, self._pending = self._pending, {}
        with self.db.session() as s:
            for key, v in pending.items():
                row = s.get(LLMCache, key)
                if row is None:
                    s.add(LLMCache(key=key, workspace=self.workspace, **v))  # type: ignore[arg-type]
                else:
                    row.response = str(v["response"])
        return len(pending)
