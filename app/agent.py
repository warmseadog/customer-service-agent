"""
agent.py — 主动达人开发场景下的回信处理器

核心流程：
  1. 拉取未读邮件
  2. 识别所属 thread / creator / campaign / product
  3. 用 LangGraph 做意图识别
  4. 有合作意向则沉淀为 lead
  5. 无合作意向则自动发送感谢回复
"""

from __future__ import annotations

import logging

from app.config import config
from app.database import (
    create_intent_result,
    get_creator,
    get_creator_by_email,
    get_outreach_message_by_thread_id,
    get_product,
    get_thread_messages,
    get_thread_state,
    is_message_processed,
    mark_message_processed,
    save_thread_message,
    update_creator,
    upsert_collaboration_lead,
    upsert_creator,
    upsert_thread_state,
)
from app.graphs import run_inbound_graph
from app.mail_service import fetch_unread_emails, send_reply

logger = logging.getLogger(__name__)


def _thread_key(msg: dict) -> str:
    refs = (msg.get("references") or "").strip()
    if refs:
        return refs.split()[0]
    return msg.get("message_id", msg["uid"])


def _build_thread_history(thread_id: str) -> list[dict]:
    rows = get_thread_messages(thread_id, limit=config.MAX_THREAD_MESSAGES)
    return [
        {
            "subject": row["subject"] or "",
            "body": row["body"] or "",
            "is_mine": row["role"] == "our",
        }
        for row in rows
    ]


def _resolve_context(msg: dict, thread_id: str) -> dict:
    state = get_thread_state(thread_id)
    outreach = get_outreach_message_by_thread_id(thread_id)
    creator = None
    product = None
    campaign_id = None
    outreach_id = None

    if state and state.get("creator_id"):
        creator = get_creator(state["creator_id"])
    if not creator and outreach:
        creator = get_creator(outreach["creator_id"])
    if not creator:
        creator = get_creator_by_email(msg["from_email"])
    if not creator:
        creator = upsert_creator(
            {
                "email": msg["from_email"],
                "name": msg.get("from_name", ""),
                "collaboration_status": "new",
                "notes": "由回信自动入库",
            }
        )

    if state and state.get("product_id"):
        product = get_product(state["product_id"])
    if not product and outreach and outreach.get("product_id"):
        product = get_product(outreach["product_id"])

    if state and state.get("campaign_id"):
        campaign_id = state["campaign_id"]
    elif outreach:
        campaign_id = outreach.get("campaign_id")

    if outreach:
        outreach_id = outreach.get("id")

    return {
        "state": state,
        "creator": creator,
        "product": product,
        "campaign_id": campaign_id,
        "outreach_id": outreach_id,
    }


def _handle_one_email(msg: dict) -> dict:
    thread_id = _thread_key(msg)
    message_id = msg["message_id"] or msg["uid"]
    logger.info("─" * 50)
    logger.info(f"📩 回信来自: {msg['from_name']} <{msg['from_email']}>")
    logger.info(f"   主题: {msg['subject']}")
    logger.info(f"   Thread key: {thread_id[:80]}")

    context = _resolve_context(msg, thread_id)
    creator = context["creator"]
    product = context["product"]
    campaign_id = context["campaign_id"]
    outreach_id = context["outreach_id"]

    save_thread_message(
        thread_id=thread_id,
        message_id=message_id,
        role="kol",
        subject=msg["subject"],
        body=msg["body"][:config.BODY_EXCERPT_LENGTH],
        creator_id=creator.get("id"),
        campaign_id=campaign_id,
        outreach_id=outreach_id,
    )
    thread_history = _build_thread_history(thread_id)
    graph_result = run_inbound_graph(
        creator=creator,
        product=product,
        latest_message=msg["body"],
        thread_history=thread_history,
    )
    intent_result = graph_result["intent_result"]
    suggested_reply = graph_result.get("suggested_reply", "")
    intent = intent_result.get("intent", "manual_review")

    create_intent_result(
        {
            "thread_id": thread_id,
            "creator_id": creator.get("id"),
            "campaign_id": campaign_id,
            "product_id": (product or {}).get("id"),
            "message_id": message_id,
            "intent": intent,
            "confidence": intent_result.get("confidence", 0),
            "summary": intent_result.get("summary", ""),
            "suggested_reply": suggested_reply,
            "raw_json": intent_result.get("raw", ""),
        }
    )

    note = intent_result.get("summary", "")
    if intent in ("interested", "need_followup"):
        commission_rate = float((product or {}).get("commission_rate") or 0)
        upsert_collaboration_lead(
            {
                "creator_id": creator["id"],
                "campaign_id": campaign_id,
                "product_id": (product or {}).get("id"),
                "thread_id": thread_id,
                "status": "new",
                "commission_rate": commission_rate,
                "intent": intent,
                "intent_summary": note,
                "latest_message": msg["body"][:500],
                "notes": note,
            }
        )
        collab_status = "interested" if intent == "interested" else "followup_needed"
        update_creator(creator["id"], {"collaboration_status": collab_status})
        logger.info("📋 已生成合作工单，等待人工对接")
    elif intent == "not_interested":
        update_creator(creator["id"], {"collaboration_status": "not_interested"})
        if suggested_reply:
            logger.info("🙏 检测到拒绝意图，发送礼貌感谢回复")
            if send_reply(original=msg, reply_body=suggested_reply):
                save_thread_message(
                    thread_id=thread_id,
                    message_id=f"our-reply-to-{message_id}",
                    role="our",
                    subject=f"Re: {msg['subject']}",
                    body=suggested_reply[:config.BODY_EXCERPT_LENGTH],
                    creator_id=creator.get("id"),
                    campaign_id=campaign_id,
                    outreach_id=outreach_id,
                )
    else:
        logger.info("🔍 意图不明确（manual_review），仅记录，无自动操作")

    upsert_thread_state(
        thread_id=thread_id,
        kol_email=msg["from_email"],
        kol_name=msg.get("from_name", ""),
        stage=1,
        last_message_id=message_id,
        notes=note,
        creator_id=creator.get("id"),
        campaign_id=campaign_id,
        product_id=(product or {}).get("id"),
        intent_label=intent,
    )
    logger.info(f"🎯 回信意图: {intent} | {note}")
    return {
        "intent": intent,
        "creator_email": creator.get("email"),
        "creator_name": creator.get("name"),
        "thread_id": thread_id,
    }


def run_check_cycle() -> dict:
    logger.info("=" * 50)
    logger.info("🔄 开始新一轮回信检查")
    emails = fetch_unread_emails(limit=config.MAX_EMAILS_PER_CYCLE)
    if not emails:
        logger.info("😴 暂无未读邮件")
        return {
            "total": 0,
            "processed": 0,
            "success": 0,
            "interested": 0,
            "not_interested": 0,
            "need_followup": 0,
            "manual_review": 0,
        }

    processed = success = 0
    counters = {
        "interested": 0,
        "not_interested": 0,
        "need_followup": 0,
        "manual_review": 0,
    }

    for msg in emails:
        dedup_key = msg["message_id"] or msg["uid"]
        thread_id = _thread_key(msg)

        if is_message_processed(dedup_key):
            continue

        if msg["from_email"].lower() == config.EMAIL_ADDRESS.lower():
            mark_message_processed(dedup_key, thread_id)
            continue

        processed += 1
        try:
            result = _handle_one_email(msg)
            counters[result["intent"]] += 1
            success += 1
        except Exception as exc:
            logger.error(f"❌ 回信处理失败: {exc}", exc_info=True)
        finally:
            mark_message_processed(dedup_key, thread_id)

    tickets_created = counters["interested"] + counters["need_followup"]
    logger.info(
        "🎉 本轮完成: 处理 %s 封 | 工单=%s（interested=%s, need_followup=%s）| not_interested=%s | manual_review=%s",
        processed,
        tickets_created,
        counters["interested"],
        counters["need_followup"],
        counters["not_interested"],
        counters["manual_review"],
    )
    logger.info("=" * 50)
    return {
        "total": len(emails),
        "processed": processed,
        "success": success,
        **counters,
    }
