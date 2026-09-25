"""Action-item extraction eval: precision/recall on hand-labeled meeting notes (fixtures/meeting_notes)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from fieldnote.config import WorkspaceConfig, fixtures_dir
from fieldnote.coordination.extractor import UNASSIGNED, extract_action_items
from fieldnote.llm.base import LLMClient

MATCH_THRESHOLD = 70


def load_labels(root: Path | None = None) -> list[dict[str, Any]]:
    root = root or fixtures_dir() / "meeting_notes"
    data = json.loads((root / "labels.json").read_text(encoding="utf-8"))
    out = []
    for n in data["notes"]:
        out.append({**n, "body": (root / n["file"]).read_text(encoding="utf-8")})
    return out


def evaluate_extraction(cfg: WorkspaceConfig, llm: LLMClient | None, root: Path | None = None) -> dict[str, Any]:
    notes = load_labels(root)
    tp = fp = fn = 0
    owner_ok = due_ok = matched = 0
    per_note = []
    errors: list[dict[str, Any]] = []
    for n in notes:
        note_date = date.fromisoformat(n["note_date"])
        predicted, via = extract_action_items(cfg, llm, n["body"], note_date)
        gold = list(n["items"])
        used: set[int] = set()
        note_tp = 0
        for p in predicted:
            best, best_score = None, 0.0
            for gi, g in enumerate(gold):
                if gi in used:
                    continue
                sc = fuzz.token_set_ratio(p.description.lower(), g["description"].lower())
                if sc > best_score:
                    best, best_score = gi, sc
            if best is not None and best_score >= MATCH_THRESHOLD:
                used.add(best)
                note_tp += 1
                g = gold[best]
                matched += 1
                gold_owner = g.get("owner") or UNASSIGNED
                if p.owner == gold_owner:
                    owner_ok += 1
                else:
                    errors.append(
                        {"note": n["file"], "item": g["description"], "owner": p.owner, "expected_owner": gold_owner}
                    )
                gold_due = g.get("due_date")
                pred_due = p.due_date.isoformat() if p.due_date else None
                if pred_due == gold_due:
                    due_ok += 1
                else:
                    errors.append(
                        {"note": n["file"], "item": g["description"], "due": pred_due, "expected_due": gold_due}
                    )
            else:
                fp += 1
                errors.append({"note": n["file"], "unexpected": p.description})
        missed = [g["description"] for gi, g in enumerate(gold) if gi not in used]
        fn += len(missed)
        tp += note_tp
        for mdesc in missed:
            errors.append({"note": n["file"], "missed": mdesc})
        per_note.append(
            {"file": n["file"], "gold": len(gold), "predicted": len(predicted), "matched": note_tp, "via": via}
        )
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "notes": len(notes),
        "gold_items": tp + fn,
        "predicted_items": tp + fp,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "owner_accuracy": round(owner_ok / matched, 4) if matched else 0.0,
        "due_date_accuracy": round(due_ok / matched, 4) if matched else 0.0,
        "per_note": per_note,
        "errors": errors[:25],
    }
