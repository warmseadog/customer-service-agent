from __future__ import annotations

import csv
import io

from app.database import list_collaboration_leads, list_intent_results, update_ticket_status


def list_lead_rows() -> list[dict]:
    return list_collaboration_leads()


def list_intent_rows(limit: int = 100) -> list[dict]:
    return list_intent_results(limit=limit)


def patch_ticket_status(ticket_id: int, status: str) -> dict | None:
    return update_ticket_status(ticket_id, status)


def export_leads_csv() -> bytes:
    rows = list_collaboration_leads()
    buffer = io.StringIO()
    fieldnames = [
        "creator_name",
        "creator_email",
        "platform",
        "campaign_name",
        "product_name",
        "asin",
        "commission_rate",
        "status",
        "intent",
        "intent_summary",
        "latest_message",
        "notes",
        "updated_at",
    ]
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow({name: row.get(name, "") for name in fieldnames})
    # UTF-8 BOM 让 Excel 直接双击打开时正确识别中文编码
    return "\ufeff" + buffer.getvalue()
