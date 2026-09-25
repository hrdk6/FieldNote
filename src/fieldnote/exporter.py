"""`fieldnote export`: dump a workspace's findings, claims, documents (metadata), changes and actions."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from fieldnote.config import WorkspaceConfig, workspace_out_dir
from fieldnote.coordination.tracker import item_dict
from fieldnote.db import repo
from fieldnote.db.engine import Database


def _rows(db: Database, cfg: WorkspaceConfig) -> dict[str, list[dict[str, Any]]]:
    ws = cfg.workspace
    with db.session() as s:
        findings = [
            {
                "id": f.id,
                "title": f.title,
                "category": f.category,
                "status": f.status,
                "total_score": round(f.total_score, 4),
                "scores": json.dumps({k: v for k, v in (f.scores or {}).items() if k != "evidence_breakdown"}),
                "evidence_claim_ids": json.dumps(f.evidence_claim_ids),
                "first_seen": f.first_seen.isoformat(),
                "last_seen": f.last_seen.isoformat(),
                "what_happened": f.what_happened,
                "why_it_matters": f.why_it_matters,
                "recommended_action": f.recommended_action,
                "how_to_execute": f.how_to_execute,
            }
            for f in repo.findings(s, ws, statuses=("active", "demoted", "stale"))
        ]
        claims = [
            {
                "id": c.id,
                "run_id": c.run_id,
                "agent": c.agent,
                "type": c.type,
                "status": c.status,
                "text": c.text,
                "source_document_ids": json.dumps(c.source_document_ids),
                "excerpt": c.excerpt,
                "critic_notes": c.critic_notes,
            }
            for c in repo.all_claims(s, ws)
        ]
        documents = [
            {
                "id": d.id,
                "source_type": d.source_type,
                "source_name": d.source_name,
                "url": d.url,
                "title": d.title,
                "published_at": d.published_at.isoformat() if d.published_at else "",
                "entities": json.dumps(d.entities),
            }
            for d in repo.all_documents(s, ws)
        ]
        changes = [
            {
                "id": c.id,
                "competitor": c.competitor,
                "kind": c.kind,
                "significance": c.significance,
                "summary": c.summary,
                "url": c.url,
                "detected_at": c.detected_at.isoformat(),
            }
            for c in repo.changes_between(s, ws, None)
        ]
        actions = [item_dict(a) for a in repo.action_items(s, ws, None)]
    return {
        "findings": findings,
        "claims": claims,
        "documents": documents,
        "change_events": changes,
        "action_items": actions,
    }


def export_workspace(db: Database, cfg: WorkspaceConfig, fmt: str = "csv") -> list[Path]:
    folder = workspace_out_dir(cfg.workspace) / "export"
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for name, rows in _rows(db, cfg).items():
        if fmt == "json":
            path = folder / f"{name}.json"
            path.write_text(json.dumps(rows, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
        else:
            path = folder / f"{name}.csv"
            with path.open("w", newline="", encoding="utf-8") as fh:
                if rows:
                    w = csv.DictWriter(fh, fieldnames=list(rows[0]))
                    w.writeheader()
                    w.writerows(rows)
        out.append(path)
    return out
