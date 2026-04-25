from __future__ import annotations

import csv
import io
from typing import Any

from app.database import bulk_upsert_creators, list_creators, update_creator, upsert_creator

FIELD_ALIASES = {
    "email": "email",
    "邮箱": "email",
    "mail": "email",
    "name": "name",
    "达人名称": "name",
    "姓名": "name",
    "creator_name": "name",
    "platform": "platform",
    "平台": "platform",
    "profile_url": "profile_url",
    "账号链接": "profile_url",
    "链接": "profile_url",
    "country": "country",
    "国家": "country",
    "language": "language",
    "语言": "language",
    "tags": "tags",
    "标签": "tags",
    "identity_summary": "identity_summary",
    "达人画像": "identity_summary",
    "bio": "identity_summary",
    "notes": "notes",
    "备注": "notes",
    "status": "collaboration_status",
    "合作状态": "collaboration_status",
}


def _normalize_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    for sep in ("|", "，", ";", "；"):
        text = text.replace(sep, ",")
    return [part.strip() for part in text.split(",") if part.strip()]


def _normalize_creator_row(row: dict) -> dict:
    normalized: dict[str, Any] = {}
    for key, value in row.items():
        mapped = FIELD_ALIASES.get(str(key).strip(), str(key).strip())
        normalized[mapped] = value.strip() if isinstance(value, str) else value
    normalized["tags"] = _normalize_tags(normalized.get("tags"))
    return normalized


def import_creators_from_csv_text(csv_text: str) -> dict:
    if not csv_text.strip():
        raise ValueError("CSV 内容不能为空")

    buffer = io.StringIO(csv_text)
    reader = csv.DictReader(buffer)
    rows = [_normalize_creator_row(row) for row in reader if any((value or "").strip() for value in row.values())]
    if not rows:
        raise ValueError("CSV 中没有可导入的数据")
    return bulk_upsert_creators(rows)


def save_creator(payload: dict) -> dict:
    payload = _normalize_creator_row(payload)
    return upsert_creator(payload)


def patch_creator(creator_id: int, payload: dict) -> dict | None:
    payload = _normalize_creator_row(payload)
    return update_creator(creator_id, payload)


def list_creator_rows() -> list[dict]:
    return list_creators()
