"""
Negotiation Runner — orchestrates a full multi-round negotiation
between the Buyer Agent and Seller Agent.

This module manages:
  • Turn-taking between buyer and seller
  • State persistence to SQLite via database.py
  • Audit logging via audit.py
  • Termination conditions (accept / reject / expiry / max rounds)

The turn-taking itself lives in :func:`settle`, a pure async generator with
no I/O.  Production (:func:`run_negotiation`) and the offline evaluator
(``scripts/batch_eval.py``) both drive that *same* generator, so the batch
metrics describe real behaviour.  ``settle`` takes the two evaluators as
callables: production passes LangGraph-backed ones (policy engine + LLM
explanation), the evaluator passes bare policy-engine ones.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncGenerator, Awaitable, Callable, Optional

# pyrefly: ignore [missing-import]
from src.schemas import (
    AuditEventType,
    BuyerMandate,
    DecisionType,
    NegotiationState,
    NegotiationStatus,
    Offer,
    PolicyDecision,
    SellerPolicy,
)
# pyrefly: ignore [missing-import]
from src.buyer_agent import buyer_graph
# pyrefly: ignore [missing-import]
from src.seller_agent import seller_graph
# pyrefly: ignore [missing-import]
from src.policy_engine import check_mandate_expiry, evaluate_buyer, evaluate_seller
# pyrefly: ignore [missing-import]
from src import audit
# pyrefly: ignore [missing-import]
from src import database as db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event type for WebSocket broadcasting
# ---------------------------------------------------------------------------

class NegotiationEvent:
    """A single event emitted during a negotiation (for WebSocket streaming)."""

    def __init__(
        self,
        event_type: str,
        round_number: int,
        role: str,
        decision: str,
        price: float | None = None,
        quantity: int | None = None,
        explanation: str = "",
        status: str = "active",
    ):
        self.event_type = event_type
        self.round_number = round_number
        self.role = role
        self.decision = decision
        self.price = price
        self.quantity = quantity
        self.explanation = explanation
        self.status = status

    def to_dict(self) -> dict:
        return {
            "event_type": self.event_type,
            "round_number": self.round_number,
            "role": self.role,
            "decision": self.decision,
            "price": self.price,
            "quantity": self.quantity,
            "explanation": self.explanation,
            "status": self.status,
        }


# ---------------------------------------------------------------------------
# Pure settlement core — shared by the API runner and scripts/batch_eval.py
# ---------------------------------------------------------------------------

@dataclass
class NegotiationStep:
    """One side's move in a negotiation."""
    round_number: int
    role: str                       # "buyer" | "seller"
    decision: PolicyDecision
    price: float | None             # the price this side now stands behind
    quantity: int
    explanation: str = ""
    offer: Optional[Offer] = None   # set only when decision == COUNTER


@dataclass
class Settlement:
    """Terminal outcome of a negotiation, filled in by :func:`settle`."""
    status: NegotiationStatus = NegotiationStatus.ACTIVE
    final_price: Optional[float] = None
    final_quantity: Optional[int] = None
    rounds_used: int = 0
    reason: str = ""
    steps: list[NegotiationStep] = field(default_factory=list)


# (decision, explanation)
BuyerEvaluator = Callable[..., Awaitable[tuple[PolicyDecision, str]]]
SellerEvaluator = Callable[..., Awaitable[tuple[PolicyDecision, str]]]


def effective_max_rounds(mandate: BuyerMandate, policy: SellerPolicy) -> int:
    """
    The single negotiation-level horizon.

    Both sides must share a horizon: if they don't, the side with the shorter
    limit walks away while the other is still conceding on a longer schedule.
    The shorter of the two is the only value neither party has authorised
    exceeding.
    """
    return min(mandate.max_rounds, policy.max_rounds)


def _opening_offer(mandate: BuyerMandate, quantity: int) -> PolicyDecision:
    """
    The buyer's round-1 opening bid.

    Deterministic by construction: always the mandate's target price.  No
    LLM-parsed number is allowed anywhere near it.
    """
    return PolicyDecision(
        decision=DecisionType.COUNTER,
        offer=Offer(
            price=mandate.target_price,
            quantity=quantity,
            terms="",
            round_number=1,
        ),
        reason="opening_offer",
    )


async def policy_buyer_evaluator(
    *,
    seller_offer: Offer | None,
    mandate: BuyerMandate,
    current_round: int,
    buyer_last_price: float | None,
    max_rounds: int,
    quantity: int,
    procurement_request: str = "",
) -> tuple[PolicyDecision, str]:
    """Bare policy-engine buyer evaluator (no LLM).  Used by batch_eval/tests."""
    if seller_offer is None:
        return _opening_offer(mandate, quantity), ""
    decision = evaluate_buyer(
        seller_offer=seller_offer,
        mandate=mandate,
        current_round=current_round,
        buyer_last_price=buyer_last_price,
        max_rounds=max_rounds,
    )
    return decision, ""


async def policy_seller_evaluator(
    *,
    buyer_offer: Offer,
    policy: SellerPolicy,
    current_round: int,
    seller_last_price: float | None,
    max_rounds: int,
    quantity: int,
) -> tuple[PolicyDecision, str]:
    """Bare policy-engine seller evaluator (no LLM).  Used by batch_eval/tests."""
    decision = evaluate_seller(
        buyer_offer=buyer_offer,
        policy=policy,
        current_round=current_round,
        seller_last_price=seller_last_price,
        max_rounds=max_rounds,
    )
    return decision, ""


async def settle(
    mandate: BuyerMandate,
    policy: SellerPolicy,
    *,
    result: Settlement,
    procurement_request: str = "",
    buyer_evaluator: BuyerEvaluator | None = None,
    seller_evaluator: SellerEvaluator | None = None,
) -> AsyncGenerator[NegotiationStep, None]:
    """
    Run the turn-taking loop, yielding one :class:`NegotiationStep` per move.

    The terminal outcome is written into *result* (an out-parameter, because
    async generators cannot return a value to an ``async for``).

    Termination order:
      1. Mandate expiry — checked *before* the round-1 opening bid.
      2. Accept / reject from either side, at any point.
      3. ``max_rounds`` exhausted — but only after the buyer has been given a
         chance to evaluate the seller's *closing* offer.  Without that final
         look, both sides can converge on an identical price and still be
         recorded as "no deal".
    """
    buyer_eval = buyer_evaluator or policy_buyer_evaluator
    seller_eval = seller_evaluator or policy_seller_evaluator

    horizon = effective_max_rounds(mandate, policy)
    # The seller cannot ship more than it holds; the buyer will not take more
    # than it asked for.
    quantity = min(mandate.quantity, policy.quantity)

    # --- Mandate expiry, checked before any offer is made ---
    if check_mandate_expiry(mandate):
        result.status = NegotiationStatus.EXPIRED
        result.reason = "mandate_expired"
        result.rounds_used = 0
        return

    buyer_last_price: float | None = None
    seller_last_price: float | None = None
    current_round = 1

    while current_round <= horizon:
        logger.info("=== Round %d/%d ===", current_round, horizon)
        result.rounds_used = current_round

        # ------------------ Buyer's turn ------------------
        seller_offer = (
            None if seller_last_price is None
            else Offer(
                price=seller_last_price,
                quantity=quantity,
                terms="",
                round_number=current_round,
            )
        )

        decision, explanation = await buyer_eval(
            seller_offer=seller_offer,
            mandate=mandate,
            current_round=current_round,
            buyer_last_price=buyer_last_price,
            max_rounds=horizon,
            quantity=quantity,
            procurement_request=procurement_request if current_round == 1 else "",
        )

        step = _make_step("buyer", current_round, decision, quantity, explanation,
                          fallback_price=seller_last_price if decision.decision == DecisionType.ACCEPT
                          else buyer_last_price)
        result.steps.append(step)
        yield step

        if decision.decision == DecisionType.ACCEPT:
            result.status = NegotiationStatus.ACCEPTED
            result.final_price = seller_last_price
            result.final_quantity = quantity
            result.reason = decision.reason
            return
        if decision.decision == DecisionType.REJECT:
            result.status = NegotiationStatus.REJECTED
            result.reason = decision.reason
            return

        buyer_last_price = step.price

        # ------------------ Seller's turn ------------------
        buyer_offer = Offer(
            price=buyer_last_price,
            quantity=quantity,
            terms="",
            round_number=current_round,
        )

        decision, explanation = await seller_eval(
            buyer_offer=buyer_offer,
            policy=policy,
            current_round=current_round,
            seller_last_price=seller_last_price,
            max_rounds=horizon,
            quantity=quantity,
        )

        step = _make_step("seller", current_round, decision, quantity, explanation,
                          fallback_price=buyer_last_price if decision.decision == DecisionType.ACCEPT
                          else seller_last_price)
        result.steps.append(step)
        yield step

        if decision.decision == DecisionType.ACCEPT:
            result.status = NegotiationStatus.ACCEPTED
            result.final_price = buyer_last_price
            result.final_quantity = quantity
            result.reason = decision.reason
            return
        if decision.decision == DecisionType.REJECT:
            result.status = NegotiationStatus.REJECTED
            result.reason = decision.reason
            return

        seller_last_price = step.price
        current_round += 1

    # ---------------- Final-offer settlement ----------------
    # The loop above exits right after the seller's closing counter, which the
    # buyer has never seen.  Give the buyer that one last evaluation before
    # declaring "no deal" — at the final round, where it concedes the whole
    # remaining gap.
    if seller_last_price is not None:
        closing_offer = Offer(
            price=seller_last_price,
            quantity=quantity,
            terms="",
            round_number=horizon,
        )
        decision, explanation = await buyer_eval(
            seller_offer=closing_offer,
            mandate=mandate,
            current_round=horizon,
            buyer_last_price=buyer_last_price,
            max_rounds=horizon,
            quantity=quantity,
            procurement_request="",
        )
        if decision.decision == DecisionType.ACCEPT:
            step = _make_step("buyer", horizon, decision, quantity, explanation,
                              fallback_price=seller_last_price)
            result.steps.append(step)
            yield step

            result.status = NegotiationStatus.ACCEPTED
            result.final_price = seller_last_price
            result.final_quantity = quantity
            result.reason = decision.reason
            result.rounds_used = horizon
            return

    result.status = NegotiationStatus.REJECTED
    result.reason = "max_rounds_exhausted"
    result.rounds_used = horizon


def _make_step(
    role: str,
    round_number: int,
    decision: PolicyDecision,
    quantity: int,
    explanation: str,
    fallback_price: float | None,
) -> NegotiationStep:
    """Normalise a PolicyDecision into a NegotiationStep."""
    if decision.decision == DecisionType.COUNTER and decision.offer is not None:
        offer = decision.offer.model_copy(update={"quantity": quantity})
        return NegotiationStep(
            round_number=round_number,
            role=role,
            decision=decision,
            price=offer.price,
            quantity=quantity,
            explanation=explanation,
            offer=offer,
        )
    return NegotiationStep(
        round_number=round_number,
        role=role,
        decision=decision,
        price=fallback_price,
        quantity=quantity,
        explanation=explanation,
        offer=None,
    )


# ---------------------------------------------------------------------------
# LangGraph-backed evaluators (policy engine + LLM explanations)
# ---------------------------------------------------------------------------

async def _graph_buyer_evaluator(
    *,
    seller_offer: Offer | None,
    mandate: BuyerMandate,
    current_round: int,
    buyer_last_price: float | None,
    max_rounds: int,
    quantity: int,
    procurement_request: str = "",
) -> tuple[PolicyDecision, str]:
    """Drive the buyer LangGraph for one turn."""
    graph_state = {
        "procurement_request": procurement_request,
        "seller_offer": seller_offer.model_dump() if seller_offer else None,
        "buyer_last_price": buyer_last_price,
        "current_round": current_round,
        "max_rounds": max_rounds,
        "mandate": mandate.model_dump(),
        "parsed_offer": None,
        "decision": None,
        "explanation": "",
        "is_terminal": False,
    }
    out = await buyer_graph.ainvoke(graph_state)
    return PolicyDecision(**out["decision"]), out.get("explanation", "")


async def _graph_seller_evaluator(
    *,
    buyer_offer: Offer,
    policy: SellerPolicy,
    current_round: int,
    seller_last_price: float | None,
    max_rounds: int,
    quantity: int,
) -> tuple[PolicyDecision, str]:
    """Drive the seller LangGraph for one turn."""
    graph_state = {
        "buyer_offer": buyer_offer.model_dump(),
        "seller_last_price": seller_last_price,
        "current_round": current_round,
        "max_rounds": max_rounds,
        "policy": policy.model_dump(),
        "decision": None,
        "explanation": "",
        "is_terminal": False,
    }
    out = await seller_graph.ainvoke(graph_state)
    return PolicyDecision(**out["decision"]), out.get("explanation", "")


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

_BUYER_EVENT_TYPES = {
    DecisionType.ACCEPT: AuditEventType.BUYER_ACCEPT,
    DecisionType.COUNTER: AuditEventType.BUYER_OFFER,
    DecisionType.REJECT: AuditEventType.BUYER_REJECT,
}

_SELLER_EVENT_TYPES = {
    DecisionType.ACCEPT: AuditEventType.SELLER_ACCEPT,
    DecisionType.COUNTER: AuditEventType.SELLER_OFFER,
    DecisionType.REJECT: AuditEventType.SELLER_REJECT,
}


async def run_negotiation(
    buyer_mandate: BuyerMandate,
    seller_policy: SellerPolicy,
    procurement_request: str = "",
    negotiation_id: str | None = None,
    db_path: str | None = None,
) -> AsyncGenerator[NegotiationEvent, None]:
    """
    Run a full negotiation, yielding events as they happen.

    This is an async generator so the FastAPI WebSocket handler can
    stream events in real time.

    Parameters
    ----------
    buyer_mandate : BuyerMandate
    seller_policy : SellerPolicy
    procurement_request : str
        Free-text procurement request (parsed by the buyer agent on turn 1).
    negotiation_id : str | None
        Override the auto-generated ID (useful for testing).
    db_path : str | None
        Override database path.

    Yields
    ------
    NegotiationEvent
    """
    neg_id = negotiation_id or str(uuid.uuid4())

    state = NegotiationState(
        negotiation_id=neg_id,
        buyer_mandate=buyer_mandate,
        seller_policy=seller_policy,
        procurement_request=procurement_request,
    )

    # Persist initial state
    await db.init_db(db_path)
    await db.create_negotiation(state, db_path=db_path)
    await audit.append_audit_entry(
        neg_id, AuditEventType.NEGOTIATION_STARTED,
        data={"buyer_mandate": buyer_mandate.model_dump(), "seller_policy": seller_policy.model_dump()},
        db_path=db_path,
    )

    yield NegotiationEvent(
        event_type="negotiation_started",
        round_number=0,
        role="system",
        decision="started",
        explanation="Negotiation has started.",
        status="active",
    )

    result = Settlement()

    async for step in settle(
        buyer_mandate,
        seller_policy,
        result=result,
        procurement_request=procurement_request,
        buyer_evaluator=_graph_buyer_evaluator,
        seller_evaluator=_graph_seller_evaluator,
    ):
        is_buyer = step.role == "buyer"
        event_type = (
            _BUYER_EVENT_TYPES if is_buyer else _SELLER_EVENT_TYPES
        )[step.decision.decision]

        await audit.append_audit_entry(
            neg_id, event_type,
            data={"price": step.price, "round": step.round_number, "reason": step.decision.reason},
            db_path=db_path,
        )

        yield NegotiationEvent(
            event_type=event_type.value,
            round_number=step.round_number,
            role=step.role,
            decision=step.decision.decision.value,
            price=step.price,
            quantity=step.quantity,
            explanation=step.explanation,
            status="active",
        )

        await db.save_round(
            neg_id, step.round_number,
            buyer_offer=step.offer if is_buyer else None,
            seller_offer=None if is_buyer else step.offer,
            buyer_decision=step.decision if is_buyer else None,
            seller_decision=None if is_buyer else step.decision,
            buyer_explanation=step.explanation if is_buyer else "",
            seller_explanation="" if is_buyer else step.explanation,
            db_path=db_path,
        )

        state.current_round = max(step.round_number, 1)
        await db.update_negotiation(state, db_path=db_path)

    # ---------------- Terminal state ----------------
    state.status = result.status
    state.current_round = max(result.rounds_used, 1)
    state.final_price = result.final_price
    state.final_quantity = result.final_quantity
    state.updated_at = datetime.now(timezone.utc)
    await db.update_negotiation(state, db_path=db_path)

    if result.status == NegotiationStatus.EXPIRED:
        await audit.append_audit_entry(
            neg_id, AuditEventType.MANDATE_EXPIRED,
            data={"reason": result.reason},
            db_path=db_path,
        )
        yield NegotiationEvent(
            event_type="negotiation_completed",
            round_number=0,
            role="system",
            decision="expired",
            explanation="The buyer's mandate expired before the negotiation could start.",
            status="expired",
        )
        return

    if result.status == NegotiationStatus.ACCEPTED:
        yield NegotiationEvent(
            event_type="negotiation_completed",
            round_number=result.rounds_used,
            role="system",
            decision="accepted",
            price=result.final_price,
            quantity=result.final_quantity,
            explanation="Deal agreed by both agents!",
            status="accepted",
        )
        return

    # Rejected.  A max-rounds walk-away has no per-side audit entry yet.
    if result.reason == "max_rounds_exhausted":
        await audit.append_audit_entry(
            neg_id, AuditEventType.BUYER_REJECT,
            data={"reason": result.reason, "round": result.rounds_used},
            db_path=db_path,
        )
        explanation = "Maximum rounds exhausted. No deal reached."
    else:
        explanation = f"Negotiation ended without a deal: {result.reason}"

    yield NegotiationEvent(
        event_type="negotiation_completed",
        round_number=result.rounds_used,
        role="system",
        decision="rejected",
        explanation=explanation,
        status="rejected",
    )
