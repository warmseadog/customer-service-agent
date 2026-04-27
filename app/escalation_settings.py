"""
全局升级收件人：数据库覆盖层，优先于 .env（config）。

仪表盘保存的邮箱/姓名写入 SQLite；某字段为空则该字段仍使用环境变量。
"""

from __future__ import annotations

from app.config import config
from app.database import get_support_escalation_settings, upsert_support_escalation_settings


def effective_default_owner() -> tuple[str, str]:
    row = get_support_escalation_settings()
    de = (row.get("default_owner_email") or "").strip()
    dn = (row.get("default_owner_name") or "").strip()
    if de:
        return de, dn or (config.DEFAULT_SUPPORT_OWNER_NAME or "").strip() or "Support Owner"
    e = (config.DEFAULT_SUPPORT_OWNER_EMAIL or "").strip()
    n = (config.DEFAULT_SUPPORT_OWNER_NAME or "").strip() or "Support Owner"
    return e, n


def settings_api_dict() -> dict:
    row = get_support_escalation_settings()
    db, dn = effective_default_owner()
    return {
        "default_owner_email": (row.get("default_owner_email") or "") or "",
        "default_owner_name": (row.get("default_owner_name") or "") or "",
        "effective_default_email": db,
        "effective_default_name": dn,
        "env_default_email": (config.DEFAULT_SUPPORT_OWNER_EMAIL or "") or "",
        "updated_at": row.get("updated_at") or "",
    }


def save_settings_from_api(
    *,
    default_owner_email: str | None = None,
    default_owner_name: str | None = None,
) -> dict:
    upsert_support_escalation_settings(
        default_owner_email=default_owner_email,
        default_owner_name=default_owner_name,
    )
    return settings_api_dict()
