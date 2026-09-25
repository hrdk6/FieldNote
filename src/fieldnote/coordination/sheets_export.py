"""Optional Google Sheets sync for the action tracker (gspread + service account).

Off unless GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_SHEETS_SPREADSHEET_ID are set and the ``sheets``
extra is installed. The service-account JSON may be a file path or the JSON itself.
"""

from __future__ import annotations

import json
from pathlib import Path

from fieldnote.config import Settings, WorkspaceConfig
from fieldnote.coordination.tracker import CSV_FIELDS, list_items
from fieldnote.db.engine import Database


class SheetsUnavailableError(Exception):
    pass


def sheets_status(settings: Settings) -> tuple[bool, str]:
    if not settings.has_sheets:
        return False, "Google Sheets sync is off (set GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_SHEETS_SPREADSHEET_ID)"
    try:
        import gspread  # noqa: F401
    except ImportError:
        return False, "gspread is not installed (pip install 'fieldnote[sheets]')"
    return True, "ready"


def sync_to_sheets(db: Database, cfg: WorkspaceConfig, settings: Settings) -> int:
    ok, why = sheets_status(settings)
    if not ok:
        raise SheetsUnavailableError(why)
    import gspread

    raw = settings.google_service_account_json or ""
    info = json.loads(Path(raw).read_text(encoding="utf-8")) if Path(raw).exists() else json.loads(raw)
    client = gspread.service_account_from_dict(info)
    sheet = client.open_by_key(settings.google_sheets_spreadsheet_id)
    title = f"fieldnote_{cfg.workspace}"[:99]
    try:
        ws = sheet.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title=title, rows=1000, cols=len(CSV_FIELDS))
    rows = list_items(db, cfg, statuses=None)
    values = [CSV_FIELDS] + [[str(r.get(k, "")) for k in CSV_FIELDS] for r in rows]
    ws.clear()
    ws.update(values, "A1")
    return len(rows)
