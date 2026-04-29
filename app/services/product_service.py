from __future__ import annotations

from typing import Any

from app.database import delete_all_products, delete_product, list_products, upsert_product


def _normalize_keywords(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    for sep in ("|", "，", ";", "；", "\n"):
        text = text.replace(sep, ",")
    return [part.strip() for part in text.split(",") if part.strip()]


def normalize_product_payload(payload: dict) -> dict:
    normalized = dict(payload)
    normalized["keywords"] = _normalize_keywords(payload.get("keywords"))
    normalized["commission_rate"] = float(payload.get("commission_rate") or 0)
    normalized["is_active"] = bool(payload.get("is_active", True))
    normalized["owner_name"] = (payload.get("owner_name") or "").strip()
    normalized["owner_email"] = (payload.get("owner_email") or "").strip()
    normalized["fallback_owner_email"] = (payload.get("fallback_owner_email") or "").strip()
    normalized["brand"] = (payload.get("brand") or "").strip()
    normalized["asin"] = (payload.get("asin") or "").strip()
    # 仪表盘已移除卖点/场景描述；未传则清空，避免旧数据在反复保存时残留
    normalized["tagline"] = (payload.get("tagline") or "").strip()
    normalized["scene"] = (payload.get("scene") or "").strip()
    normalized["intro"] = (payload.get("intro") or "").strip()
    normalized["description"] = (payload.get("description") or "").strip()
    return normalized


def save_product(payload: dict) -> dict:
    return upsert_product(normalize_product_payload(payload))


def remove_product(product_id: str) -> bool:
    return delete_product(product_id)


def remove_all_products() -> int:
    return delete_all_products()


def list_product_rows(active_only: bool = False) -> list[dict]:
    return list_products(active_only=active_only)
