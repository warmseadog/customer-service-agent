"""
邮箱级 RBAC：组长 / 组员可见范围；与 admin、operator、viewer（不按邮箱过滤读）区分。
"""

from __future__ import annotations

from fastapi import HTTPException

from app.auth_deps import CurrentUser
from app.database import get_thread_state, rbac_mailbox_ids_for_user
from app.thread_scope import parse_scoped_thread_id

_MSG_403 = "当前账号权限不足。"


def mailbox_scope(user: CurrentUser) -> frozenset[int] | None:
    """None 表示不按邮箱限制；否则仅可访问集合内 mailbox_id。"""
    return rbac_mailbox_ids_for_user(user.id, user.role)


def ensure_mailbox_in_scope(user: CurrentUser, mailbox_id: int | None) -> None:
    if mailbox_id is None:
        return
    allowed = mailbox_scope(user)
    if allowed is None:
        return
    if int(mailbox_id) not in allowed:
        raise HTTPException(status_code=403, detail=_MSG_403)


def thread_mailbox_id(thread_id: str) -> int:
    st = get_thread_state(thread_id)
    if st and st.get("mailbox_id") is not None:
        return int(st["mailbox_id"])
    mid, _ = parse_scoped_thread_id(thread_id)
    return int(mid)


def ensure_thread_in_scope(user: CurrentUser, thread_id: str) -> None:
    ensure_mailbox_in_scope(user, thread_mailbox_id(thread_id))


def filter_products_by_scope(rows: list[dict], allowed: frozenset[int] | None) -> list[dict]:
    if allowed is None:
        return rows
    out: list[dict] = []
    for p in rows:
        mbs = set(int(x) for x in (p.get("mailbox_ids") or []) if x is not None)
        if mbs & allowed:
            out.append(p)
    return out


def filter_mailboxes_by_scope(rows: list[dict], allowed: frozenset[int] | None) -> list[dict]:
    if allowed is None:
        return rows
    return [m for m in rows if int(m["id"]) in allowed]


def summary_for_mailboxes(allowed: frozenset[int] | None) -> dict:
    """仪表盘状态卡片用：scoped 时只统计可见邮箱相关产品等。"""
    from app.database import (
        count_escalation_events,
        list_escalation_events_for_mailboxes,
        list_intent_results_for_mailboxes,
        list_processed_messages,
        list_processed_messages_for_mailboxes,
        list_support_staff,
    )
    from app.services.lead_service import list_intent_rows
    from app.services.product_service import list_product_rows

    if allowed is None:
        products = list_product_rows()
        intents = list_intent_rows(limit=500)
        escalations = count_escalation_events()
        processed = list_processed_messages(limit=500)
        staff = list_support_staff()
        return {
            "products": len(products),
            "intents": len(intents),
            "escalations": escalations,
            "processed": len(processed),
            "support_staff": len(staff),
        }
    mids = sorted(allowed)
    if not mids:
        return {
            "products": 0,
            "intents": 0,
            "escalations": 0,
            "processed": 0,
            "support_staff": len(list_support_staff()),
        }
    prows = filter_products_by_scope(list_product_rows(), allowed)
    intents = list_intent_results_for_mailboxes(500, mids)
    esc = list_escalation_events_for_mailboxes(5000, mids)
    proc = list_processed_messages_for_mailboxes(500, mids)
    return {
        "products": len(prows),
        "intents": len(intents),
        "escalations": len(esc),
        "processed": len(proc),
        "support_staff": len(list_support_staff()),
    }
