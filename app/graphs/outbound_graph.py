from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from app.llm_service import generate_outreach_email, recommend_products_for_creator


class OutboundState(TypedDict, total=False):
    creator: dict
    products: list[dict]
    forced_product: dict | None
    campaign_name: str
    commission_rate_override: float | None
    selected_product: dict
    draft_subject: str
    draft_body: str


def _select_product(state: OutboundState) -> OutboundState:
    forced_product = state.get("forced_product")
    if forced_product:
        return {"selected_product": forced_product}

    products = state.get("products", [])
    creator = state.get("creator", {})
    selected = recommend_products_for_creator(creator, products, top_n=1)
    return {"selected_product": selected[0] if selected else {}}


def _draft_email(state: OutboundState) -> OutboundState:
    product = state.get("selected_product", {})
    commission_rate = (
        state.get("commission_rate_override")
        if state.get("commission_rate_override") is not None
        else product.get("commission_rate", 0)
    )
    draft = generate_outreach_email(
        creator=state.get("creator", {}),
        product=product,
        commission_rate=float(commission_rate or 0),
        campaign_name=state.get("campaign_name"),
    )
    return {
        "draft_subject": draft["subject"],
        "draft_body": draft["body"],
    }


def _build_graph() -> Any:
    graph = StateGraph(OutboundState)
    graph.add_node("select_product", _select_product)
    graph.add_node("draft_email", _draft_email)
    graph.set_entry_point("select_product")
    graph.add_edge("select_product", "draft_email")
    graph.add_edge("draft_email", END)
    return graph.compile()


_GRAPH = _build_graph()


def run_outbound_graph(
    *,
    creator: dict,
    products: list[dict],
    forced_product: dict | None = None,
    campaign_name: str = "",
    commission_rate_override: float | None = None,
) -> dict:
    result = _GRAPH.invoke(
        {
            "creator": creator,
            "products": products,
            "forced_product": forced_product,
            "campaign_name": campaign_name,
            "commission_rate_override": commission_rate_override,
        }
    )
    return {
        "product": result.get("selected_product", {}),
        "subject": result.get("draft_subject", ""),
        "body": result.get("draft_body", ""),
    }
