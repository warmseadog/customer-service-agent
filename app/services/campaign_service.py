from __future__ import annotations

from typing import Any

from app.database import (
    create_campaign,
    create_outreach_message,
    delete_campaign,
    delete_outreach_message,
    get_campaign,
    get_creator,
    get_outreach_message,
    get_product,
    list_campaigns,
    list_creators,
    list_outreach_messages,
    list_products,
    save_thread_message,
    update_campaign,
    update_creator,
    update_outreach_message,
    upsert_thread_state,
)
from app.graphs import run_outbound_graph
from app.mail_service import send_outreach_email


def _select_creators(creator_ids: list[int] | None) -> list[dict]:
    rows = list_creators()
    if not creator_ids:
        return rows
    wanted = set(int(item) for item in creator_ids)
    return [row for row in rows if row.get("id") in wanted]


def create_campaign_drafts(payload: dict) -> dict:
    name = (payload.get("name") or "").strip() or "Untitled Campaign"
    forced_product = None
    product_id = (payload.get("product_id") or "").strip()
    if product_id:
        forced_product = get_product(product_id)
        if not forced_product:
            raise ValueError("指定产品不存在")

    products = list_products(active_only=True)
    selected_creators = _select_creators(payload.get("creator_ids"))
    if not selected_creators:
        raise ValueError("没有可用于创建草稿的达人")

    commission_override = payload.get("commission_rate")
    commission_rate = (
        float(commission_override)
        if commission_override is not None and commission_override != ""
        else float((forced_product or {}).get("commission_rate") or 0)
    )
    campaign = create_campaign(
        name=name,
        product_id=product_id or "",
        commission_rate=commission_rate,
        notes=(payload.get("notes") or "").strip(),
    )

    drafts = []
    for creator in selected_creators:
        graph_result = run_outbound_graph(
            creator=creator,
            products=products,
            forced_product=forced_product,
            campaign_name=name,
            commission_rate_override=commission_override if commission_override not in (None, "") else None,
        )
        selected_product = graph_result.get("product") or forced_product or {}
        draft = create_outreach_message(
            {
                "creator_id": creator["id"],
                "campaign_id": campaign["id"],
                "product_id": selected_product.get("id"),
                "thread_id": f"draft-{campaign['id']}-{creator['id']}",
                "subject": graph_result.get("subject", ""),
                "body": graph_result.get("body", ""),
                "status": "draft",
            }
        )
        drafts.append(draft)

    update_campaign(campaign["id"], creator_count=len(drafts))
    return {
        "campaign": get_campaign(campaign["id"]),
        "drafts": list_campaign_outreach(campaign["id"]),
    }


def list_campaign_rows() -> list[dict]:
    campaigns = list_campaigns()
    return [
        campaign | {"messages": list_campaign_outreach(campaign["id"])}
        for campaign in campaigns
    ]


def remove_campaign(campaign_id: int) -> bool:
    return delete_campaign(campaign_id)


def list_campaign_outreach(campaign_id: int) -> list[dict]:
    rows = list_outreach_messages(campaign_id=campaign_id)
    enriched = []
    for row in rows:
        creator = get_creator(row["creator_id"])
        product = get_product(row["product_id"]) if row.get("product_id") else None
        enriched.append(
            row
            | {
                "creator_name": (creator or {}).get("name", ""),
                "creator_email": (creator or {}).get("email", ""),
                "product_name": (product or {}).get("name", ""),
                "commission_rate": (
                    get_campaign(campaign_id).get("commission_rate")
                    if get_campaign(campaign_id)
                    else None
                ),
            }
        )
    return enriched


def update_outreach_draft(outreach_id: int, payload: dict) -> dict | None:
    fields = {k: v for k, v in payload.items() if k in {"subject", "body", "status"}}
    return update_outreach_message(outreach_id, **fields)


def remove_outreach_message(outreach_id: int) -> bool:
    return delete_outreach_message(outreach_id)


def send_outreach_by_id(outreach_id: int) -> dict:
    draft = get_outreach_message(outreach_id)
    if not draft:
        raise ValueError("外呼草稿不存在")
    creator = get_creator(draft["creator_id"])
    if not creator:
        raise ValueError("草稿对应达人不存在")
    if draft.get("status") == "sent":
        return draft

    result = send_outreach_email(
        to_email=creator["email"],
        to_name=creator.get("name", ""),
        subject=draft["subject"],
        body=draft["body"],
    )
    if not result["success"]:
        update_outreach_message(outreach_id, status="failed")
        raise RuntimeError("SMTP 发送失败")

    updated = update_outreach_message(
        outreach_id,
        status="sent",
        message_id=result["message_id"],
        sent_at=result["sent_at"],
        thread_id=result["thread_id"],
    )
    campaign = get_campaign(draft["campaign_id"]) if draft.get("campaign_id") else None
    product = get_product(draft["product_id"]) if draft.get("product_id") else None
    save_thread_message(
        thread_id=result["thread_id"],
        message_id=result["message_id"],
        role="our",
        subject=draft["subject"],
        body=draft["body"],
        creator_id=creator["id"],
        campaign_id=draft.get("campaign_id"),
        outreach_id=outreach_id,
    )
    upsert_thread_state(
        thread_id=result["thread_id"],
        kol_email=creator["email"],
        kol_name=creator.get("name", ""),
        stage=1,
        last_message_id=result["message_id"],
        notes=f"主动开发邮件已发送 | campaign={campaign['name'] if campaign else ''}",
        creator_id=creator["id"],
        campaign_id=draft.get("campaign_id"),
        product_id=(product or {}).get("id"),
    )
    update_creator(creator["id"], {"last_outreach_at": result["sent_at"]})
    if draft.get("campaign_id"):
        update_campaign(draft["campaign_id"], status="sending")
    return updated or {}


def send_campaign(campaign_id: int) -> dict:
    messages = list_outreach_messages(campaign_id=campaign_id)
    sent = 0
    failed = 0
    for row in messages:
        try:
            send_outreach_by_id(row["id"])
            sent += 1
        except Exception:
            failed += 1
    update_campaign(campaign_id, status="sent" if failed == 0 else "partial")
    return {
        "campaign": get_campaign(campaign_id),
        "sent": sent,
        "failed": failed,
    }
