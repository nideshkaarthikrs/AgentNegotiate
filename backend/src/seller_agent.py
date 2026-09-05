"""
Seller Agent — LangGraph StateGraph that orchestrates the seller side
of a negotiation.

Mirror of the buyer agent, but uses SellerPolicy and evaluate_seller.
"""

from __future__ import annotations

import logging
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from src.schemas import (
    DecisionType,
    Offer,
    PolicyDecision,
    SellerPolicy,
)
from src.policy_engine import evaluate_seller
from src.llm_parser import explain_decision

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State schema for the seller graph
# ---------------------------------------------------------------------------

class SellerGraphState(TypedDict):
    """State flowing through the seller agent graph."""
    # Inputs
    buyer_offer: dict              # latest buyer offer (dict for serialisation)
    seller_last_price: float | None
    current_round: int
    max_rounds: int                # shared negotiation-level horizon
    policy: dict                   # SellerPolicy as dict

    # Outputs (set by nodes)
    decision: dict | None          # PolicyDecision as dict
    explanation: str
    is_terminal: bool


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------

def evaluate_node(state: SellerGraphState) -> dict[str, Any]:
    """Run the deterministic policy engine for the seller."""
    policy = SellerPolicy(**state["policy"])
    buyer_offer = Offer(**state["buyer_offer"])

    decision = evaluate_seller(
        buyer_offer=buyer_offer,
        policy=policy,
        current_round=state["current_round"],
        seller_last_price=state.get("seller_last_price"),
        max_rounds=state.get("max_rounds") or policy.max_rounds,
    )

    is_terminal = decision.decision in (DecisionType.ACCEPT, DecisionType.REJECT)
    return {
        "decision": decision.model_dump(),
        "is_terminal": is_terminal,
    }


async def explain_node(state: SellerGraphState) -> dict[str, Any]:
    """Generate a human-readable explanation of the decision."""
    decision_dict = state["decision"]
    decision = PolicyDecision(**decision_dict)

    context = {
        "round": state["current_round"],
        "seller_last_price": state.get("seller_last_price"),
        "buyer_offer_price": state["buyer_offer"]["price"],
    }

    explanation = await explain_decision(decision, role="seller", context=context)
    return {"explanation": explanation}


# ---------------------------------------------------------------------------
# Build the graph
# ---------------------------------------------------------------------------

def build_seller_graph() -> StateGraph:
    """Construct and compile the seller agent graph."""
    graph = StateGraph(SellerGraphState)

    graph.add_node("evaluate", evaluate_node)
    graph.add_node("explain", explain_node)

    graph.set_entry_point("evaluate")
    graph.add_edge("evaluate", "explain")
    graph.add_edge("explain", END)

    return graph.compile()


# Singleton compiled graph
seller_graph = build_seller_graph()
