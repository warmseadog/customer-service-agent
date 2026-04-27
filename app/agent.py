"""
agent.py — 被动入站客服邮件处理器

核心流程：
  1. 拉取未读邮件
  2. 识别所属 thread / 联系人 / 产品
  3. 用 LangGraph 分析情绪；安抚类在发对外回复前先尝试发内部通知，再按「是否已发内部」生成正文（话术与行为一致）
  4. 自动 send_reply 给用户；安抚类在配置存在负责人时抄送其邮箱
  5. 升级：写 escalation_events + 发内部通知（含冷却；安抚类可配置忽略冷却）
  6. 记录 intent_result（含 cs_sentiment / cs_tone / escalated）
"""

from __future__ import annotations

import logging
from datetime import datetime

from app.config import config
from app.database import (
    create_escalation_event,
    create_intent_result,
    get_creator,
    get_creator_by_email,
    get_last_escalation_time,
    get_product,
    get_thread_messages,
    get_thread_state,
    is_message_processed,
    list_products,
    mark_message_processed,
    save_thread_message,
    upsert_creator,
    upsert_thread_state,
)
from app.graphs import run_inbound_graph
from app.llm_service import (
    generate_calm_reply,
    generate_escalation_summary,
    translate_to_chinese_for_support,
)
from app.mail_service import fetch_unread_emails, send_internal_escalation, send_reply

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


def _resolve_contact(msg: dict, thread_id: str) -> dict:
    """
    解析联系人（创建者）。优先级：
      1. 线程状态中已有的 creator_id
      2. 通过邮箱在 creators 表查找
      3. 自动新建联系人记录
    """
    state = get_thread_state(thread_id)
    contact = None

    if state and state.get("creator_id"):
        contact = get_creator(state["creator_id"])

    if not contact:
        contact = get_creator_by_email(msg["from_email"])

    if not contact:
        contact = upsert_creator(
            {
                "email": msg["from_email"],
                "name": msg.get("from_name", ""),
                "collaboration_status": "new",
                "notes": "由来信自动入库",
            }
        )

    return contact


def _resolve_product(thread_id: str) -> dict | None:
    """从线程状态获取已绑定产品；若无，返回 None（图内关键词匹配兜底）。"""
    state = get_thread_state(thread_id)
    if state and state.get("product_id"):
        return get_product(state["product_id"])
    return None


def _get_owner_email_and_name(product: dict | None) -> tuple[str, str]:
    """
    按 1A 规则确定内部升级收件人：
      1. 产品的 owner_email
      2. 产品的 fallback_owner_email
      3. 全局 DEFAULT_SUPPORT_OWNER_EMAIL
    """
    if product:
        email = (product.get("owner_email") or "").strip()
        name = (product.get("owner_name") or "").strip()
        if email:
            return email, name
        fallback = (product.get("fallback_owner_email") or "").strip()
        if fallback:
            return fallback, name
    return config.DEFAULT_SUPPORT_OWNER_EMAIL, config.DEFAULT_SUPPORT_OWNER_NAME


def _cooldown_ok(thread_id: str) -> bool:
    """检查同线程升级邮件冷却时间是否已过。"""
    last_sent_iso = get_last_escalation_time(thread_id)
    if not last_sent_iso:
        return True
    try:
        last_sent = datetime.fromisoformat(last_sent_iso)
        elapsed_minutes = (datetime.now() - last_sent).total_seconds() / 60
        return elapsed_minutes >= config.ESCALATION_EMAIL_COOLDOWN_MINUTES
    except Exception:
        return True


def _can_send_escalation_now(thread_id: str, *, for_calm_path: bool) -> bool:
    """安抚类可配置忽略冷却，确保「承诺已联系售后」与系统发信一致。"""
    if for_calm_path and config.CALM_BYPASS_ESCALATION_COOLDOWN:
        return True
    return _cooldown_ok(thread_id)


def _handle_one_email(msg: dict) -> dict:
    thread_id = _thread_key(msg)
    message_id = msg["message_id"] or msg["uid"]
    logger.info("─" * 50)
    logger.info(f"📩 收到来信: {msg['from_name']} <{msg['from_email']}>")
    logger.info(f"   主题: {msg['subject']}")
    logger.info(f"   Thread key: {thread_id[:80]}")

    contact = _resolve_contact(msg, thread_id)
    product = _resolve_product(thread_id)

    save_thread_message(
        thread_id=thread_id,
        message_id=message_id,
        role="kol",
        subject=msg["subject"],
        body=msg["body"][:config.BODY_EXCERPT_LENGTH],
        creator_id=contact.get("id"),
    )

    thread_history = _build_thread_history(thread_id)

    graph_result = run_inbound_graph(
        contact=contact,
        product=product,
        thread_id=thread_id,
        latest_message=msg["body"],
        thread_history=thread_history,
    )

    # 图内关键词绑定的产品（若原来为 None，图内可能已匹配到）
    resolved_product = graph_result.get("product") or product
    sentiment = graph_result["sentiment"]
    tone = graph_result["tone"]
    should_escalate = graph_result["should_escalate"]
    escalation_reason = graph_result["escalation_reason"]
    suggested_reply = graph_result["suggested_reply"]
    escalation_summary = (graph_result.get("escalation_summary") or "").strip()

    needs_calm = sentiment == "dissatisfied" or tone in ("firm", "hostile")

    logger.info(f"🧠 情绪: {sentiment} | 语气: {tone} | 升级: {should_escalate} | 安抚类: {needs_calm}")

    owner_email, owner_name = _get_owner_email_and_name(resolved_product)
    escalated_flag = False
    after_sales_notified = False

    def _do_send_internal(reason: str, for_calm: bool) -> bool:
        if not owner_email:
            logger.warning("⚠️ 升级无收件人：产品未绑定 owner，DEFAULT_SUPPORT_OWNER_EMAIL 亦为空")
            return False
        if not _can_send_escalation_now(thread_id, for_calm_path=for_calm):
            logger.info(f"⏳ 升级冷却中，跳过内部通知（cooldown={config.ESCALATION_EMAIL_COOLDOWN_MINUTES}min）")
            return False
        contact_name = contact.get("name") or contact.get("email") or "未知联系人"
        contact_email = contact.get("email") or ""
        product_name = (resolved_product or {}).get("name") or "未绑定产品"
        priority = "high" if tone == "hostile" else "medium"
        orig_for_escalation = (msg.get("body") or "")[:2000]
        original_message_zh = translate_to_chinese_for_support(orig_for_escalation)

        summary = escalation_summary
        if not summary:
            summary = generate_escalation_summary(
                thread_id=thread_id,
                contact=contact,
                product=resolved_product,
                latest_message=msg["body"],
                sentiment=sentiment,
                tone=tone,
                reason=reason,
            )

        sent_at = datetime.now().isoformat()
        success = send_internal_escalation(
            to_email=owner_email,
            to_name=owner_name,
            thread_id=thread_id,
            contact_name=contact_name,
            contact_email=contact_email,
            product_name=product_name,
            priority=priority,
            escalation_summary=summary,
            original_message=orig_for_escalation,
            original_message_zh=original_message_zh,
        )
        if success:
            create_escalation_event(
                {
                    "thread_id": thread_id,
                    "creator_id": contact.get("id"),
                    "product_id": (resolved_product or {}).get("id"),
                    "reason": reason,
                    "internal_email_to": owner_email,
                    "sent_at": sent_at,
                }
            )
            logger.info(f"🚨 已发送升级通知 → {owner_email}")
        return success

    # ── 安抚类：先发内部通知（成功后才在对外回复中写「已联系售后」），再生成正文并抄送负责人 ──
    if needs_calm:
        if owner_email:
            if _do_send_internal(escalation_reason or "客户表达不满，需售后同步", for_calm=True):
                escalated_flag = True
                after_sales_notified = True
        else:
            logger.warning("⚠️ 安抚类来信但无负责人邮箱：对外不声称已联系售后，亦不抄送")

        suggested_reply = generate_calm_reply(
            contact=contact,
            product=resolved_product,
            sentiment=sentiment,
            tone=tone,
            latest_message=msg["body"],
            thread_history=thread_history,
            after_sales_notified=after_sales_notified,
        )
    elif should_escalate:
        # 非安抚但需升级（罕见，如关键词命中而情绪识别未标为不满）
        if _do_send_internal(escalation_reason, for_calm=False):
            escalated_flag = True

    # ── 发送用户回复 ────────────────────────────────────────────────────────────
    if suggested_reply:
        cc_list = [owner_email] if (needs_calm and owner_email) else None
        sent_ok = send_reply(original=msg, reply_body=suggested_reply, cc_emails=cc_list)
        if sent_ok:
            save_thread_message(
                thread_id=thread_id,
                message_id=f"our-reply-to-{message_id}",
                role="our",
                subject=f"Re: {msg['subject']}",
                body=suggested_reply[:config.BODY_EXCERPT_LENGTH],
                creator_id=contact.get("id"),
            )
            logger.info("✅ 已发送客服回复")
        else:
            logger.error("❌ 客服回复发送失败")

    # ── 记录意图结果 ─────────────────────────────────────────────────────────────
    create_intent_result(
        {
            "thread_id": thread_id,
            "creator_id": contact.get("id"),
            "product_id": (resolved_product or {}).get("id"),
            "message_id": message_id,
            "intent": "cs_reply",
            "confidence": 1.0,
            "summary": escalation_reason if should_escalate else f"情绪:{sentiment} 语气:{tone}",
            "suggested_reply": suggested_reply,
            "cs_sentiment": sentiment,
            "cs_tone": tone,
            "escalated": escalated_flag,
        }
    )

    upsert_thread_state(
        thread_id=thread_id,
        kol_email=msg["from_email"],
        kol_name=msg.get("from_name", ""),
        stage=1,
        last_message_id=message_id,
        notes=f"情绪:{sentiment} 语气:{tone}" + (" [已升级]" if escalated_flag else ""),
        creator_id=contact.get("id"),
        product_id=(resolved_product or {}).get("id"),
        intent_label=f"{sentiment}/{tone}",
    )

    logger.info(f"🎯 处理完成 | sentiment={sentiment} tone={tone} escalated={escalated_flag}")
    return {
        "sentiment": sentiment,
        "tone": tone,
        "escalated": escalated_flag,
        "contact_email": contact.get("email"),
        "contact_name": contact.get("name"),
        "thread_id": thread_id,
    }


def run_check_cycle() -> dict:
    logger.info("=" * 50)
    logger.info("🔄 开始新一轮来信检查")
    emails = fetch_unread_emails(limit=config.MAX_EMAILS_PER_CYCLE)
    if not emails:
        logger.info("😴 暂无未读邮件")
        return {
            "total": 0,
            "processed": 0,
            "success": 0,
            "escalated": 0,
            "dissatisfied": 0,
            "satisfied": 0,
            "neutral": 0,
        }

    processed = success = escalated = 0
    sentiment_counters: dict[str, int] = {"satisfied": 0, "neutral": 0, "dissatisfied": 0}

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
            sentiment_counters[result.get("sentiment", "neutral")] = (
                sentiment_counters.get(result.get("sentiment", "neutral"), 0) + 1
            )
            if result.get("escalated"):
                escalated += 1
            success += 1
        except Exception as exc:
            logger.error(f"❌ 来信处理失败: {exc}", exc_info=True)
        finally:
            mark_message_processed(dedup_key, thread_id)

    logger.info(
        "🎉 本轮完成: 处理 %s 封 | 升级=%s | satisfied=%s neutral=%s dissatisfied=%s",
        processed,
        escalated,
        sentiment_counters["satisfied"],
        sentiment_counters["neutral"],
        sentiment_counters["dissatisfied"],
    )
    logger.info("=" * 50)
    return {
        "total": len(emails),
        "processed": processed,
        "success": success,
        "escalated": escalated,
        **sentiment_counters,
    }
