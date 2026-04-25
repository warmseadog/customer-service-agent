from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from app.llm_service import detect_creator_reply_intent, generate_polite_decline_reply


class InboundState(TypedDict, total=False):
    creator: dict
    product: dict | None
    latest_message: str
    thread_history: list[dict]
    intent_result: dict
    suggested_reply: str


def _classify_intent(state: InboundState) -> InboundState:
    result = detect_creator_reply_intent(
        creator=state.get("creator", {}),
        latest_message=state.get("latest_message", ""),
        thread_history=state.get("thread_history", []),
        product=state.get("product"),
    )
    return {"intent_result": result}


def _suggest_reply(state: InboundState) -> InboundState:
    intent = (state.get("intent_result") or {}).get("intent")
    if intent != "not_interested":
        return {"suggested_reply": ""}
    reply = generate_polite_decline_reply(
        creator=state.get("creator", {}),
        product=state.get("product"),
    )
    return {"suggested_reply": reply}


def _build_graph() -> Any:
    graph = StateGraph(InboundState)
    graph.add_node("classify_intent", _classify_intent)
    graph.add_node("suggest_reply", _suggest_reply)
    graph.set_entry_point("classify_intent")
    graph.add_edge("classify_intent", "suggest_reply")
    graph.add_edge("suggest_reply", END)
    return graph.compile()


_GRAPH = _build_graph()


def run_inbound_graph(
    *,
    creator: dict,
    product: dict | None,
    latest_message: str,
    thread_history: list[dict],
) -> dict:
    result = _GRAPH.invoke(
        {
            "creator": creator,
            "product": product,
            "latest_message": latest_message,
            "thread_history": thread_history,
        }
    )
    intent_result = result.get("intent_result", {})
    return {
        "intent_result": intent_result,
        "suggested_reply": result.get("suggested_reply", ""),
    }
