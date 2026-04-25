"""LangGraph 编排入口。"""

from app.graphs.inbound_graph import run_inbound_graph
from app.graphs.outbound_graph import run_outbound_graph

__all__ = ["run_outbound_graph", "run_inbound_graph"]
