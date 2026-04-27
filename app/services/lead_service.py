"""
lead_service.py — 意图/情绪流水读写服务

注意：合作工单（leads）、CSV 导出等外呼相关功能已随重构移除。
本文件仅保留 intent_results 相关操作，供 main.py 路由使用。
"""

from __future__ import annotations

from app.database import (
    delete_all_intent_results,
    delete_intent_result,
    list_intent_results,
)


def list_intent_rows(limit: int = 100) -> list[dict]:
    return list_intent_results(limit=limit)


def remove_intent(intent_id: int) -> bool:
    return delete_intent_result(intent_id)


def remove_all_intents() -> int:
    return delete_all_intent_results()
