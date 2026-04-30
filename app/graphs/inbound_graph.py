"""
inbound_graph.py — 被动入站客服邮件 LangGraph 状态机

节点流程：
  bind_product → analyze_sentiment_tone → evaluate_escalation → generate_reply → [generate_escalation_summary] → END

- bind_product:               从线程已绑定产品 or 关键词匹配兜底
- analyze_sentiment_tone:     LLM 分析情绪/语气/是否建议升级
- evaluate_escalation:        混合策略决定是否升级（LLM 推荐 + 规则强制）
- generate_reply:             非安抚类生成普通回复；安抚类返回空正文，由 agent 先发内部通知再生成
- generate_escalation_summary: 在 should_escalate 或安抚类时生成内部摘要
"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from app.config import config
from app.database import check_repeat_dissatisfaction, list_products_for_mailbox
from app.llm_service import (
    detect_customer_satisfaction_and_tone,
    generate_escalation_summary,
    generate_normal_reply,
)

# 触发强制升级的关键词
_FORCE_ESCALATION_KEYWORDS = [
    "律师", "起诉", "法院", "投诉", "lawyer", "sue", "court", "legal action",
    "媒体", "曝光", "media", "press", "journalist",
    "complaint", "fraud", "scam", "骗子", "诈骗",
]


class InboundState(TypedDict, total=False):
    contact: dict
    product: dict | None
    mailbox_id: int
    thread_id: str
    latest_message: str
    thread_history: list[dict]
    # 情绪分析结果
    sentiment: str
    tone: str
    escalate_recommended: bool
    analysis_reason: str
    # 升级决策
    should_escalate: bool
    escalation_reason: str
    # 输出
    suggested_reply: str
    escalation_summary: str


def _bind_product(state: InboundState) -> InboundState:
    """若 state 中 product 已绑定则直接用；否则仅在本邮箱关联产品集中做关键词匹配。"""
    if state.get("product"):
        return {}

    latest = (state.get("latest_message") or "").lower()
    thread_hist = state.get("thread_history") or []
    context_text = latest + " ".join(
        m.get("body", "")[:200].lower() for m in thread_hist[-3:]
    )

    try:
        mid = int(state.get("mailbox_id") or 0)
        products = list_products_for_mailbox(mid, active_only=True) if mid > 0 else []
    except Exception:
        products = []

    best_product = None
    best_score = 0
    for p in products:
        score = sum(
            1 for kw in (p.get("keywords") or [])
            if str(kw).strip().lower() in context_text
        )
        if score > best_score:
            best_score = score
            best_product = p

    return {"product": best_product if best_score > 0 else None}


def _analyze_sentiment_tone(state: InboundState) -> InboundState:
    """调用 LLM 检测用户情绪、语气，并获取升级建议。"""
    result = detect_customer_satisfaction_and_tone(
        contact=state.get("contact", {}),
        latest_message=state.get("latest_message", ""),
        thread_history=state.get("thread_history", []),
    )
    return {
        "sentiment": result["sentiment"],
        "tone": result["tone"],
        "escalate_recommended": result["escalate_recommended"],
        "analysis_reason": result["reason_short"],
    }


def _needs_calm(state: InboundState) -> bool:
    s = state.get("sentiment", "neutral")
    t = state.get("tone", "cooperative")
    return s == "dissatisfied" or t in ("firm", "hostile")


def _evaluate_escalation(state: InboundState) -> InboundState:
    """
    混合策略决定是否真正升级：
      - LLM 的 escalate_recommended 为主
      - 规则强制兜底：强硬关键词 | 同线程第二次不满意
    """
    latest = (state.get("latest_message") or "").lower()
    sentiment = state.get("sentiment", "neutral")
    escalate_recommended = state.get("escalate_recommended", False)
    thread_id = state.get("thread_id", "")
    reason = state.get("analysis_reason", "")

    keyword_hit = any(kw in latest for kw in _FORCE_ESCALATION_KEYWORDS)
    repeat_dissatisfied = False
    if sentiment == "dissatisfied" and thread_id:
        try:
            repeat_dissatisfied = check_repeat_dissatisfaction(
                thread_id, hours=config.REPEAT_DISSATISFACTION_HOURS
            )
        except Exception:
            pass

    should_escalate = escalate_recommended or keyword_hit or repeat_dissatisfied

    escalation_reason = reason
    if keyword_hit:
        escalation_reason = "命中强制升级关键词（律师/投诉/媒体等）"
    elif repeat_dissatisfied:
        escalation_reason = f"同线程在 {config.REPEAT_DISSATISFACTION_HOURS}h 内第二次不满意，强制升级"

    return {
        "should_escalate": should_escalate,
        "escalation_reason": escalation_reason,
    }


def _generate_reply(state: InboundState) -> InboundState:
    """普通场景直接生成回复；不满/安抚类延后到 agent：先发内部通知再生成正文，避免话术与事实不一致。"""
    if _needs_calm(state):
        return {"suggested_reply": ""}

    reply = generate_normal_reply(
        contact=state.get("contact", {}),
        product=state.get("product"),
        latest_message=state.get("latest_message", ""),
        thread_history=state.get("thread_history", []),
        sentiment=state.get("sentiment", "neutral"),
    )
    return {"suggested_reply": reply}


def _generate_escalation_summary(state: InboundState) -> InboundState:
    """在应该升级或安抚类时生成内部升级摘要。结合上下文避免客户单回订单号时摘要信息缺失。"""
    if not state.get("should_escalate") and not _needs_calm(state):
        return {"escalation_summary": ""}

    hist = state.get("thread_history", [])
    latest = state.get("latest_message", "")
    context_msgs = [m.get("body", "") for m in hist[-2:] if not m.get("is_mine")]
    context_msgs.append(latest)
    combined_latest = "\n\n".join(context_msgs)

    summary = generate_escalation_summary(
        thread_id=state.get("thread_id", ""),
        contact=state.get("contact", {}),
        product=state.get("product"),
        latest_message=combined_latest,
        sentiment=state.get("sentiment", ""),
        tone=state.get("tone", ""),
        reason=state.get("escalation_reason", ""),
    )
    return {"escalation_summary": summary}


def _should_generate_escalation_summary(state: InboundState) -> str:
    if state.get("should_escalate") or _needs_calm(state):
        return "generate_escalation_summary"
    return END


def _build_graph() -> Any:
    graph = StateGraph(InboundState)
    graph.add_node("bind_product", _bind_product)
    graph.add_node("analyze_sentiment_tone", _analyze_sentiment_tone)
    graph.add_node("evaluate_escalation", _evaluate_escalation)
    graph.add_node("generate_reply", _generate_reply)
    graph.add_node("generate_escalation_summary", _generate_escalation_summary)

    graph.set_entry_point("bind_product")
    graph.add_edge("bind_product", "analyze_sentiment_tone")
    graph.add_edge("analyze_sentiment_tone", "evaluate_escalation")
    graph.add_edge("evaluate_escalation", "generate_reply")
    graph.add_conditional_edges(
        "generate_reply",
        _should_generate_escalation_summary,
        {
            "generate_escalation_summary": "generate_escalation_summary",
            END: END,
        },
    )
    graph.add_edge("generate_escalation_summary", END)
    return graph.compile()


_GRAPH = _build_graph()


def run_inbound_graph(
    *,
    contact: dict,
    product: dict | None,
    mailbox_id: int,
    thread_id: str,
    latest_message: str,
    thread_history: list[dict],
) -> dict:
    result = _GRAPH.invoke(
        {
            "contact": contact,
            "product": product,
            "mailbox_id": int(mailbox_id),
            "thread_id": thread_id,
            "latest_message": latest_message,
            "thread_history": thread_history,
        }
    )
    return {
        "product": result.get("product"),
        "sentiment": result.get("sentiment", "neutral"),
        "tone": result.get("tone", "cooperative"),
        "escalate_recommended": result.get("escalate_recommended", False),
        "should_escalate": result.get("should_escalate", False),
        "escalation_reason": result.get("escalation_reason", ""),
        "suggested_reply": result.get("suggested_reply", ""),
        "escalation_summary": result.get("escalation_summary", ""),
    }
