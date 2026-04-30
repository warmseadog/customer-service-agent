"""
agent.py — 被动入站客服邮件处理器

核心流程：
  1. 拉取未读邮件
  2. 识别所属 thread / 联系人 / 产品
  3. 用 LangGraph 分析情绪；安抚类在发对外回复前先尝试发内部通知，再按「是否已发内部」生成正文（话术与行为一致）
  4. 安抚内部路由：语气 **hostile**（激烈）→ 内部通知**优先**全局备用联系人；其它安抚类（配合/强硬的常见不满）→ **产品 owner 链**（与数据库一致后再备用/默认）
  5. 自动 send_reply 给用户（不抄送内部）
  6. 升级：写 escalation_events + 发内部通知（含冷却；安抚类可配置忽略冷却）
  7. 记录 intent_result（含 cs_sentiment / cs_tone / escalated）
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from app.thread_scope import scope_message_stub, scope_thread_id
from app.config import config
from app.database import (
    count_escalation_events_for_thread,
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
    list_enabled_mailboxes,
    update_mailbox_check_status,
)
from app.escalation_settings import effective_default_owner
from app.graphs import run_inbound_graph
from app.llm_service import (
    generate_calm_reply,
    generate_escalation_summary,
    translate_to_chinese_for_support,
)
from app.mail_service import fetch_unread_emails, send_internal_escalation, send_reply

logger = logging.getLogger(__name__)


def _thread_has_prior_our_message(thread_history: list[dict]) -> bool:
    """本线程中是否已有我方（客服）发出过的历史。"""
    return any(m.get("is_mine") for m in (thread_history or []))


def _count_customer_messages(thread_history: list[dict]) -> int:
    """客户（非我方）来信条数；当前处理来信已写入 thread_history 后计数。"""
    return sum(1 for m in (thread_history or []) if not m.get("is_mine"))


def _count_our_messages(thread_history: list[dict]) -> int:
    """本线程我方已发出回信条数（与 save 后拉取的 thread_history 一致）。"""
    return sum(1 for m in (thread_history or []) if m.get("is_mine"))


def _has_order_reference(text: str) -> bool:
    if not (text and text.strip()):
        return False
    t = text.strip()
    if re.search(
        r"(?:订单|单号|order\s*#?|order\s*no\.?|purchase)[\s:：#]*[A-Za-z0-9#\-]{4,64}",
        t,
        re.I,
    ):
        return True
    if re.search(
        r"(?:[A-Z]{2,}\d[\dA-Z\-/]*|\d{8,16}(?![.\d/])|#[A-Za-z0-9#\-/]{3,}|[#＃(（]\s*[\dA-Z\-#]{4,}|[#＃]?\d{4,}[-/]\d+)",
        t,
    ):
        return True
    if re.search(r"(?<![.\d])\d{8,16}(?![.\d])", t):
        return True
    return False


def _has_product_mention_in_text(text: str, product: dict | None) -> bool:
    if not (text and text.strip()):
        return False
    t = text.strip()
    if product:
        name = (product.get("name") or "").strip()
        if name and name in t:
            return True
        pb = (product.get("brand") or "").strip()
        if pb and len(pb) >= 2 and pb in t:
            return True
        for kw in (product.get("keywords") or [])[:12]:
            ks = str(kw).strip()
            if len(ks) >= 2 and ks in t:
                return True
    if re.search(
        r"(产品|品名|型号|款|item|product|sku|套装|系列)"
        r"|(筋膜枪|瑜伽垫|跳绳|哑铃|弹力|阻力|按摩|器材)",
        t,
        re.I,
    ):
        return True
    return bool(re.search(r"[\u4e00-\u9fff]{2,}", t) and len(t) >= 6)


def _looks_order_and_product_only_followup(
    thread_history: list[dict],
    latest: str,
    product: dict | None,
) -> bool:
    """
    我方已回过信后：若本封能识别出订单/单号类信息，且能对应到品名/产品指称，篇幅不长，
    则视为客户已按上封要求补全「单号+品名」、**不再追问题描述**，进入收尾感谢+致歉+已转售后。
    """
    if not _thread_has_prior_our_message(thread_history):
        return False
    if not _has_order_reference(latest):
        return False
    if not _has_product_mention_in_text(latest, product):
        return False
    if len((latest or "").strip()) > 800:
        return False
    return True


def _calm_reply_mode(
    thread_history: list[dict],
    latest_message: str,
    product: dict | None,
) -> str:
    """
    initial_three: 本线程中我方将发出**第一封**正式客服回复
    empathy_pure: 本方已回信 ≥ N 封后仍以安抚为主的场景：对外**不重复**售后时间线空话，短共情
    close_ack: 客户已补订单+品名、收尾致谢与致歉
    soothe_focus: 客户来信≥3 封：少索要订单，侧重多角度安抚与承接
    default: 其它多轮安抚
    """
    if not _thread_has_prior_our_message(thread_history):
        return "initial_three"
    if _count_our_messages(thread_history) >= config.CALM_EMPATHY_ONLY_MIN_PRIOR_OUTBOUND:
        return "empathy_pure"
    if _looks_order_and_product_only_followup(thread_history, latest_message, product):
        return "close_ack"
    if _count_customer_messages(thread_history) >= 3:
        return "soothe_focus"
    return "default"


def _product_display_for_mail(product: dict | None) -> str:
    """邮件/摘要中的产品展示：品牌 · 名称（若有品牌）。"""
    if not product:
        return "未绑定产品"
    name = (product.get("name") or "").strip()
    brand = (product.get("brand") or "").strip()
    if brand and name:
        return f"{brand} · {name}"
    return name or "未绑定产品"


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
    确定内部升级收件人：
      1. 产品的 owner_email
      2. 产品的 fallback_owner_email
      3. 全局兜底（仪表盘或 .env 的 DEFAULT）
    """
    if product:
        email = (product.get("owner_email") or "").strip()
        name = (product.get("owner_name") or "").strip()
        if email:
            return email, name
        fallback = (product.get("fallback_owner_email") or "").strip()
        if fallback:
            return fallback, name or (product.get("owner_name") or "").strip()
    default_email, default_name = effective_default_owner()
    return default_email, default_name


def _get_calm_internal_recipient(
    product: dict | None, *, intense: bool
) -> tuple[str, str]:
    """
    安抚类内部通知收件人。
    
    - intense（语气 hostile / 情绪激烈）或非激烈：均沿用 _get_owner_email_and_name，
      即优先产品负责人链，无则走全局兜底。
    """
    if not intense:
        return _get_owner_email_and_name(product)
    default_email, default_name = effective_default_owner()
    if default_email:
        return default_email, default_name
    return _get_owner_email_and_name(product)


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


def _handle_one_email(msg: dict, mailbox_row: dict) -> dict:
    t0 = time.perf_counter()
    mailbox_id = int(mailbox_row["id"])
    raw_key = _thread_key(msg)
    thread_id = scope_thread_id(mailbox_id, raw_key)
    dedup_raw = msg["message_id"] or msg["uid"]
    message_id = scope_message_stub(mailbox_id, dedup_raw)
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
    t_after_prep = time.perf_counter()

    graph_result = run_inbound_graph(
        contact=contact,
        product=product,
        mailbox_id=mailbox_id,
        thread_id=thread_id,
        latest_message=msg["body"],
        thread_history=thread_history,
    )
    t_after_graph = time.perf_counter()
    sec_prep = t_after_prep - t0
    sec_graph = t_after_graph - t_after_prep

    # 图内关键词绑定的产品（若原来为 None，图内可能已匹配到）
    resolved_product = graph_result.get("product") or product
    sentiment = graph_result["sentiment"]
    tone = graph_result["tone"]
    should_escalate = graph_result["should_escalate"]
    escalation_reason = graph_result["escalation_reason"]
    suggested_reply = graph_result["suggested_reply"]
    escalation_summary = (graph_result.get("escalation_summary") or "").strip()

    needs_calm = sentiment == "dissatisfied" or tone in ("firm", "hostile")
    # 语气 hostile 视为需要「跪舔级」安抚 + 内部通知**优先**备用联系人；其余安抚类走产品/默认链
    calm_intense = needs_calm and tone == "hostile"

    logger.info(
        f"🧠 情绪: {sentiment} | 语气: {tone} | 升级: {should_escalate} | 安抚类: {needs_calm} | 激烈(备用): {calm_intense}"
    )

    owner_email, owner_name = _get_calm_internal_recipient(
        resolved_product, intense=calm_intense
    )
    escalated_flag = False
    after_sales_notified = False
    sec_internal = 0.0
    sec_calm = 0.0

    def _do_send_internal(reason: str, for_calm: bool) -> bool:
        nonlocal sec_internal
        _t_int0 = time.perf_counter()
        try:
            if not owner_email:
                logger.warning("⚠️ 升级无收件人：产品未绑定 owner，DEFAULT_SUPPORT_OWNER_EMAIL 亦为空")
                return False
            # 避免将「客服工单」误发到客户邮箱（产品负责人 / 默认邮箱若误填为 KOL 邮箱）
            _owner_l = owner_email.strip().lower()
            _from_l = (msg.get("from_email") or "").strip().lower()
            _contact_l = (contact.get("email") or "").strip().lower()
            if _owner_l and (_owner_l == _from_l or (_contact_l and _owner_l == _contact_l)):
                logger.error(
                    "⚠️ 内部升级收件人与客户邮箱相同，已跳过发送工单，请检查产品 owner / DEFAULT_SUPPORT_OWNER_EMAIL 配置"
                )
                return False
            if not _can_send_escalation_now(thread_id, for_calm_path=for_calm):
                logger.info(f"⏳ 升级冷却中，跳过内部通知（cooldown={config.ESCALATION_EMAIL_COOLDOWN_MINUTES}min）")
                return False
            # 同线程已成功写入的升级条数；本封即将发送的为第 (prior+1) 次推送
            prior_count = count_escalation_events_for_thread(thread_id)
            push_sequence = prior_count + 1
            contact_name = contact.get("name") or contact.get("email") or "未知联系人"
            contact_email = contact.get("email") or ""
            product_name = _product_display_for_mail(resolved_product)
            priority = "high" if tone == "hostile" else "medium"
            # 为了让产品负责人看到完整的上下文（尤其是第二封信补齐信息的场景），将历史拼接
            history_lines = []
            for item in thread_history[-3:]:
                role = "客服" if item.get("is_mine") else (contact.get("name") or "客户")
                history_lines.append(f"[{role}] {item.get('body', '')[:300]}")
            history_lines.append(f"[{contact.get('name') or '客户'}] {msg.get('body', '')[:1000]}")
            orig_for_escalation = "\n\n".join(history_lines)[:2000]

            original_message_zh = translate_to_chinese_for_support(orig_for_escalation)

            summary = escalation_summary
            if not summary:
                summary = generate_escalation_summary(
                    thread_id=thread_id,
                    contact=contact,
                    product=resolved_product,
                    latest_message=orig_for_escalation,
                    sentiment=sentiment,
                    tone=tone,
                    reason=reason,
                )

            sent_at = datetime.now().isoformat()
            success = send_internal_escalation(
                mailbox_row=mailbox_row,
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
                push_sequence=push_sequence,
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
        finally:
            sec_internal += time.perf_counter() - _t_int0

    # ── 决定是否立即发内部通知（基于严重度与状态机） ──
    is_red_alert = calm_intense or should_escalate
    thread_text_for_order = msg["body"] + " " + " ".join(m.get("body", "") for m in thread_history)
    has_order = _has_order_reference(thread_text_for_order)
    info_complete = (resolved_product is not None) and has_order

    should_send_internal_now = False
    reason_internal = escalation_reason or "客户表达不满，需售后同步"

    if is_red_alert:
        should_send_internal_now = True
        de, _ = effective_default_owner()
        if calm_intense and de and owner_email and owner_email.strip().lower() == de.strip().lower():
            reason_internal = f"{reason_internal} [路由:情绪激烈→全局收件人]"
        elif calm_intense and not de:
            reason_internal = f"{reason_internal} [路由:情绪激烈但无全局邮箱→产品负责人链]"
        else:
            reason_internal = f"{reason_internal} [路由:触发强制升级/警报→强推内部]"
    elif needs_calm:
        if info_complete:
            should_send_internal_now = True
            reason_internal = f"{reason_internal} [路由:一般反馈且信息齐备→推送产品负责人]"
        else:
            should_send_internal_now = False
            logger.info("⏳ 一般不满但信息未齐（缺订单号或产品名），暂挂起不发内部通知，等待客户补齐。")

    if needs_calm:
        if should_send_internal_now:
            if owner_email:
                if _do_send_internal(reason_internal, for_calm=True):
                    escalated_flag = True
                    after_sales_notified = True
            else:
                logger.warning("⚠️ 需要通知内部但无负责人邮箱：对外不声称已联系售后")
        
        calm_mode = _calm_reply_mode(thread_history, msg["body"], resolved_product)
        logger.info(
            f"   安抚回复模式: {calm_mode} (首封索三项=initial_three / "
            f"多轮后不重复售后话术=empathy_pure / 收尾=close_ack / "
            f"≥3封侧重安抚=soothe_focus / 其他=default)"
        )
        t_calm0 = time.perf_counter()
        suggested_reply = generate_calm_reply(
            contact=contact,
            product=resolved_product,
            sentiment=sentiment,
            tone=tone,
            latest_message=msg["body"],
            thread_history=thread_history,
            after_sales_notified=after_sales_notified,
            product_resolved=resolved_product is not None,
            intense_appeasement=calm_intense,
            calm_mode=calm_mode,
        )
        sec_calm = time.perf_counter() - t_calm0
    elif should_escalate:
        # 非安抚但需升级（如仅关键词命中）
        if should_send_internal_now and owner_email:
            if _do_send_internal(reason_internal, for_calm=False):
                escalated_flag = True

    # ── 发送用户回复 ────────────────────────────────────────────────────────────
    sec_outbound = 0.0
    if suggested_reply:
        t_out0 = time.perf_counter()
        sent_ok = send_reply(mailbox_row, original=msg, reply_body=suggested_reply)
        if sent_ok:
            save_thread_message(
                thread_id=thread_id,
                message_id=scope_message_stub(mailbox_id, f"our-reply-to-{dedup_raw}"),
                role="our",
                subject=f"Re: {msg['subject']}",
                body=suggested_reply[:config.BODY_EXCERPT_LENGTH],
                creator_id=contact.get("id"),
            )
            logger.info("✅ 已发送客服回复")
        else:
            logger.error("❌ 客服回复发送失败")
        sec_outbound = time.perf_counter() - t_out0

    # ── 记录意图结果 ─────────────────────────────────────────────────────────────
    t_persist0 = time.perf_counter()
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

    sec_persist = time.perf_counter() - t_persist0
    sec_total = time.perf_counter() - t0
    sec_accounted = sec_prep + sec_graph + sec_internal + sec_calm + sec_outbound + sec_persist
    sec_other = max(0.0, sec_total - sec_accounted)
    logger.info(
        "⏱ 本封耗时 合计%.2fs | 准备+入库来信%.2fs | 入站分析图%.2fs | 内部升级%.2fs | 安抚/兜底正文%.2fs | 回复客户%.2fs | 意图落库%.2fs | 其它%.2fs",
        sec_total,
        sec_prep,
        sec_graph,
        sec_internal,
        sec_calm,
        sec_outbound,
        sec_persist,
        sec_other,
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


def _msg_sort_key(msg: dict) -> tuple[str, int]:
    """同 thread 内多封未读时的稳定顺序：日期字符串升序，其次 IMAP uid。"""
    uid_raw = str(msg.get("uid") or "")
    try:
        uid_i = int(uid_raw)
    except ValueError:
        uid_i = 0
    return (str(msg.get("date") or ""), uid_i)


def _process_thread_batch(tasks: list[dict]) -> dict:
    """
    同一 thread_scoped 内顺序处理多封来信；供线程池调用。
    每项含 msg, mailbox_row, scoped_mid, thread_scoped, mid。
    """
    sentiment_counters: dict[str, int] = {"satisfied": 0, "neutral": 0, "dissatisfied": 0}
    processed = success = escalated = 0
    for t in tasks:
        msg = t["msg"]
        mailbox_row = t["mailbox_row"]
        scoped_mid = t["scoped_mid"]
        thread_scoped = t["thread_scoped"]
        mid = t["mid"]
        processed += 1
        try:
            result = _handle_one_email(msg, mailbox_row)
            k = result.get("sentiment", "neutral")
            sentiment_counters[k] = sentiment_counters.get(k, 0) + 1
            if result.get("escalated"):
                escalated += 1
            success += 1
        except Exception as exc:
            logger.error(f"❌ 来信处理失败: {exc}", exc_info=True)
        finally:
            mark_message_processed(scoped_mid, thread_scoped, mid)
    return {
        "processed": processed,
        "success": success,
        "escalated": escalated,
        "sentiment_counters": sentiment_counters,
    }


def _imap_fetch_for_mailbox(
    mailbox_row: dict,
) -> tuple[int, dict, list[dict], str | None]:
    """单邮箱 IMAP 拉取；供线程池调用。返回 (id, row, emails, fetch_error)。"""
    mid = int(mailbox_row["id"])
    try:
        emails = fetch_unread_emails(mailbox_row, limit=config.MAX_EMAILS_PER_CYCLE)
        return mid, mailbox_row, emails, None
    except Exception as exc:
        logger.error("邮箱 %s IMAP 失败: %s", mid, exc, exc_info=True)
        return mid, mailbox_row, [], str(exc)


def run_check_cycle() -> dict:
    logger.info("=" * 50)
    logger.info("🔄 开始新一轮来信检查")
    mailboxes = list_enabled_mailboxes()
    if not mailboxes:
        logger.warning("无已启用邮箱，跳过")
        return {
            "total": 0,
            "processed": 0,
            "success": 0,
            "escalated": 0,
            "dissatisfied": 0,
            "satisfied": 0,
            "neutral": 0,
            "mailbox_count": 0,
        }

    t_cycle0 = time.perf_counter()
    n_mb = len(mailboxes)
    workers = max(1, min(config.MAILBOX_FETCH_MAX_WORKERS, n_mb))
    if workers == 1:
        snapshots = [_imap_fetch_for_mailbox(mb) for mb in mailboxes]
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            snapshots = list(ex.map(_imap_fetch_for_mailbox, mailboxes))
    t_after_imap = time.perf_counter()

    processed = success = escalated = 0
    total_in = 0
    sentiment_counters: dict[str, int] = {"satisfied": 0, "neutral": 0, "dissatisfied": 0}
    tasks_by_thread: defaultdict[str, list[dict]] = defaultdict(list)

    for mid, mailbox_row, emails, fetch_err in snapshots:
        if fetch_err:
            update_mailbox_check_status(mid, last_error=fetch_err)
            continue
        chk = datetime.now().isoformat()
        update_mailbox_check_status(mid, last_error=None, last_checked_at=chk)
        total_in += len(emails)
        for msg in emails:
            msg["mailbox_id"] = mid
            raw_dedup = msg["message_id"] or msg["uid"]
            thread_scoped = scope_thread_id(mid, _thread_key(msg))
            scoped_mid = scope_message_stub(mid, raw_dedup)
            if is_message_processed(scoped_mid, mid):
                continue
            mb_addr = (mailbox_row.get("email_address") or "").strip().lower()
            if msg["from_email"].strip().lower() == mb_addr:
                mark_message_processed(scoped_mid, thread_scoped, mid)
                continue
            tasks_by_thread[thread_scoped].append(
                {
                    "msg": msg,
                    "mailbox_row": mailbox_row,
                    "scoped_mid": scoped_mid,
                    "thread_scoped": thread_scoped,
                    "mid": mid,
                }
            )

    batches = []
    for _tid, tlist in tasks_by_thread.items():
        tlist.sort(key=lambda x: _msg_sort_key(x["msg"]))
        batches.append(tlist)

    t_after_queue = time.perf_counter()

    proc_workers = max(1, config.EMAIL_PROCESS_MAX_WORKERS)
    n_batches = len(batches)
    effective_proc_workers = 1 if n_batches <= 1 else min(proc_workers, n_batches)
    if n_batches == 0:
        pass
    elif effective_proc_workers == 1:
        for batch in batches:
            part = _process_thread_batch(batch)
            processed += part["processed"]
            success += part["success"]
            escalated += part["escalated"]
            for skey, sv in part["sentiment_counters"].items():
                sentiment_counters[skey] = sentiment_counters.get(skey, 0) + sv
    else:
        with ThreadPoolExecutor(max_workers=effective_proc_workers) as ex:
            for part in ex.map(_process_thread_batch, batches):
                processed += part["processed"]
                success += part["success"]
                escalated += part["escalated"]
                for skey, sv in part["sentiment_counters"].items():
                    sentiment_counters[skey] = sentiment_counters.get(skey, 0) + sv

    t_after_process = time.perf_counter()

    elapsed = time.perf_counter() - t_cycle0
    sec_imap = t_after_imap - t_cycle0
    sec_queue = t_after_queue - t_after_imap
    sec_process = t_after_process - t_after_queue
    logger.info(
        "🎉 本轮完成: 拉取 %s 封 | 处理 %s | 升级=%s | satisfied=%s neutral=%s dissatisfied=%s",
        total_in,
        processed,
        escalated,
        sentiment_counters["satisfied"],
        sentiment_counters["neutral"],
        sentiment_counters["dissatisfied"],
    )
    logger.info(
        "⏱ 本轮阶段耗时 合计%.2fs | IMAP拉取%.2fs | 分拣组批%.2fs | 处理来信%.2fs "
        "（IMAP并行worker=%s，会话batch=%s，处理并行≤%s）",
        elapsed,
        sec_imap,
        sec_queue,
        sec_process,
        workers,
        n_batches,
        effective_proc_workers if n_batches else 0,
    )
    logger.info("=" * 50)
    return {
        "total": total_in,
        "processed": processed,
        "success": success,
        "escalated": escalated,
        **sentiment_counters,
        "mailbox_count": len(mailboxes),
    }
