"""
Buyer Agent — LangGraph StateGraph that orchestrates the buyer side
of a negotiation.

Graph nodes:
  parse_request  → LLM parses natural-language request into an Offer
  evaluate       → Deterministic policy engine decides Accept/Counter/Reject
  explain        → LLM generates a human-readable explanation

The graph is invoked once per *turn* (i.e. after the seller responds).
"""

from __future__ import annotations

import logging
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from src.schemas import (
    BuyerMandate,
    DecisionType,
    Offer,
    PolicyDecision,
)
from src.policy_engine import evaluate_buyer
from src.llm_parser import explain_decision, parse_procurement_request

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State schema for the buyer graph
# ---------------------------------------------------------------------------

class BuyerGraphState(TypedDict):
    """State flowing through the buyer agent graph."""
    # Inputs
    procurement_request: str       # natural-language request (first turn only)
    seller_offer: dict | None      # latest seller offer (dict for serialisation)
    buyer_last_price: float | None # buyer's last offered price
    current_round: int
    max_rounds: int                # shared negotiation-level horizon
    mandate: dict                  # BuyerMandate as dict

    # Outputs (set by nodes)
    parsed_offer: dict | None
    decision: dict | None          # PolicyDecision as dict
    explanation: str
    is_terminal: bool              # True → negotiation should stop after this turn


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------

async def parse_request_node(state: BuyerGraphState) -> dict[str, Any]:
    """Parse the initial procurement request into a structured Offer."""
    text = state.get("procurement_request", "")
    if not text:
        # Not the first turn — skip parsing
        return {"parsed_offer": None}

    offer = await parse_procurement_request(text, round_number=state["current_round"])
    return {"parsed_offer": offer.model_dump()}


def build_opening_offer(mandate: BuyerMandate, parsed: dict | None) -> Offer:
    """
    Build the buyer's round-1 opening bid.

    The price is *always* ``mandate.target_price``.  The LLM-parsed request
    may contribute free-text terms, and a quantity only when the mandate does
    not carry one — it can never influence the price.  (An earlier version
    took ``min(parsed_price, target_price)``, which let a hallucinated ₹0.01
    become the opening bid and anchored the whole negotiation below the
    seller's floor.)
    """
    opening_price = mandate.target_price

    quantity = mandate.quantity
    if not quantity and parsed:
        quantity = max(int(parsed.get("quantity") or 1), 1)

    # Guard: the opening bid must sit inside the mandate's own bounds.
    if not (mandate.target_price <= opening_price <= mandate.max_unit_price + 1e-9):
        raise ValueError(
            f"opening bid {opening_price} outside mandate bounds "
            f"[{mandate.target_price}, {mandate.max_unit_price}]"
        )

    return Offer(
        price=opening_price,
        quantity=quantity,
        terms=str(parsed.get("terms", "")) if parsed else "",
        round_number=1,
    )


def evaluate_node(state: BuyerGraphState) -> dict[str, Any]:
    """Run the deterministic policy engine."""
    mandate = BuyerMandate(**state["mandate"])
    max_rounds = state.get("max_rounds") or mandate.max_rounds

    seller_offer_dict = state.get("seller_offer")
    if seller_offer_dict is None:
        # First turn: buyer opens with its target price
        decision = PolicyDecision(
            decision=DecisionType.COUNTER,
            offer=build_opening_offer(mandate, state.get("parsed_offer")),
            reason="opening_offer",
        )
        return {
            "decision": decision.model_dump(),
            "is_terminal": False,
        }

    seller_offer = Offer(**seller_offer_dict)
    decision = evaluate_buyer(
        seller_offer=seller_offer,
        mandate=mandate,
        current_round=state["current_round"],
        buyer_last_price=state.get("buyer_last_price"),
        max_rounds=max_rounds,
    )

    is_terminal = decision.decision in (DecisionType.ACCEPT, DecisionType.REJECT)
    return {
        "decision": decision.model_dump(),
        "is_terminal": is_terminal,
    }


async def explain_node(state: BuyerGraphState) -> dict[str, Any]:
    """Generate a human-readable explanation of the decision."""
    decision_dict = state["decision"]
    decision = PolicyDecision(**decision_dict)

    context = {
        "round": state["current_round"],
        "buyer_last_price": state.get("buyer_last_price"),
    }
    if state.get("seller_offer"):
        context["seller_offer_price"] = state["seller_offer"]["price"]

    explanation = await explain_decision(decision, role="buyer", context=context)
    return {"explanation": explanation}


# ---------------------------------------------------------------------------
# Build the graph
# ---------------------------------------------------------------------------

def build_buyer_graph() -> StateGraph:
    """Construct and compile the buyer agent graph."""
    graph = StateGraph(BuyerGraphState)

    graph.add_node("parse_request", parse_request_node)
    graph.add_node("evaluate", evaluate_node)
    graph.add_node("explain", explain_node)

    graph.set_entry_point("parse_request")
    graph.add_edge("parse_request", "evaluate")
    graph.add_edge("evaluate", "explain")
    graph.add_edge("explain", END)

    return graph.compile()


# Singleton compiled graph
buyer_graph = build_buyer_graph()
