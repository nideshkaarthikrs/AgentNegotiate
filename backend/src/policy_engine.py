"""
Deterministic Policy Engine for AgentNegotiate.

Every financial decision — accept, counter, or reject — is made here
with pure arithmetic.  The LLM is *never* consulted for price decisions.

Concession strategy (per round), where `max_rounds` is the negotiation-level
horizon shared by both sides:

    concession fraction = current_round / max_rounds

So a 4-round negotiation concedes 25 % → 50 % → 75 % → 100 % of the
remaining gap.  The final round always concedes the whole gap, which is
what lets the two sides meet (see "crossing detection" below).

Hard constraints:
  • Buyer's counter-offer is clamped so total ≤ max_budget.
  • Seller's counter-offer is clamped so price ≥ floor_price.
  • The hard constraint is always applied *last*, so a soft "sanity"
    bound can never override it.
  • Negotiation terminates after max_rounds.

Crossing detection:
  If our own concession target has reached (or passed) the counterparty's
  standing price, we accept their price instead of restating it.  Without
  this, both sides can land on the identical number and still record a
  "no deal".
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from src.schemas import (
    BuyerMandate,
    DecisionType,
    Offer,
    PolicyDecision,
    SellerPolicy,
)

# Tolerance for float comparisons (prices are rounded to 2 dp).
_EPS = 1e-6


# ---------------------------------------------------------------------------
# Concession schedule
# ---------------------------------------------------------------------------

def _concession_rate(current_round: int, max_rounds: int) -> float:
    """Calculate concession fraction based on how close we are to the final round."""
    if max_rounds <= 0 or current_round >= max_rounds:
        return 1.0  # Final round: concede 100% of the gap (clamped by budget/floor later)

    # E.g. for 4 rounds:
    # Round 1: 0.25 (1/4)
    # Round 2: 0.50 (2/4)
    # Round 3: 0.75 (3/4)
    return current_round / max_rounds


def _reason_for_rate(rate: float) -> str:
    """Machine-readable reason string for a concession."""
    return f"conceding_{int(rate * 100)}pct_of_gap"


# ---------------------------------------------------------------------------
# Buyer evaluation
# ---------------------------------------------------------------------------

def evaluate_buyer(
    seller_offer: Offer,
    mandate: BuyerMandate,
    current_round: int,
    buyer_last_price: float | None = None,
    max_rounds: int | None = None,
) -> PolicyDecision:
    """
    Decide the buyer's next move given the latest seller offer.

    Parameters
    ----------
    seller_offer : Offer
        The most recent offer from the seller.
    mandate : BuyerMandate
        The buyer's configuration / constraints.
    current_round : int
        Which round we are currently in.
    buyer_last_price : float | None
        The buyer's last offered price (None on the first round).
    max_rounds : int | None
        Negotiation-level horizon.  Defaults to the mandate's own
        ``max_rounds``; the runner passes the *shared* horizon so both
        sides concede on the same schedule and walk away at the same time.

    Returns
    -------
    PolicyDecision
        ACCEPT, COUNTER (with a new Offer), or REJECT.
    """
    horizon = max_rounds if max_rounds is not None else mandate.max_rounds

    # --- Check mandate expiry ---
    if mandate.expiry and datetime.now(timezone.utc) > mandate.expiry:
        return PolicyDecision(
            decision=DecisionType.REJECT,
            reason="mandate_expired",
        )

    affordable = seller_offer.price <= mandate.max_unit_price + _EPS

    # --- Accept if seller price is at or below target OR at/below our last offer ---
    if affordable and (
        seller_offer.price <= mandate.target_price + _EPS
        or (buyer_last_price is not None and seller_offer.price <= buyer_last_price + _EPS)
    ):
        return PolicyDecision(
            decision=DecisionType.ACCEPT,
            reason="seller_price_met_our_requirements",
        )

    # --- Round cap reached → reject ---
    if current_round > horizon:
        return PolicyDecision(
            decision=DecisionType.REJECT,
            reason="max_rounds_exceeded",
        )

    # --- Calculate counter-offer ---
    reference_price = buyer_last_price if buyer_last_price is not None else mandate.target_price
    rate = _concession_rate(current_round, horizon)
    gap = seller_offer.price - reference_price
    counter_price = reference_price + gap * rate

    # Sanity: counter should be at least the target price
    counter_price = max(counter_price, mandate.target_price)

    # Hard constraint LAST: never exceed budget
    counter_price = min(counter_price, mandate.max_unit_price)

    # Round to 2 decimal places — never round *up* past the budget cap
    counter_price = round(counter_price, 2)
    if counter_price > mandate.max_unit_price:
        counter_price = math.floor(round(mandate.max_unit_price * 100, 6)) / 100

    # --- Crossing detection: we were willing to move to their number anyway ---
    if affordable and counter_price >= seller_offer.price - _EPS:
        return PolicyDecision(
            decision=DecisionType.ACCEPT,
            reason="concession_target_met_counterparty_price",
        )

    counter_offer = Offer(
        price=counter_price,
        quantity=mandate.quantity,
        terms="",
        round_number=current_round,
    )

    return PolicyDecision(
        decision=DecisionType.COUNTER,
        offer=counter_offer,
        reason=_reason_for_rate(rate),
    )


# ---------------------------------------------------------------------------
# Seller evaluation
# ---------------------------------------------------------------------------

def evaluate_seller(
    buyer_offer: Offer,
    policy: SellerPolicy,
    current_round: int,
    seller_last_price: float | None = None,
    max_rounds: int | None = None,
) -> PolicyDecision:
    """
    Decide the seller's next move given the latest buyer offer.

    Parameters
    ----------
    buyer_offer : Offer
        The most recent offer from the buyer.
    policy : SellerPolicy
        The seller's configuration / constraints.
    current_round : int
        Which round we are currently in.
    seller_last_price : float | None
        The seller's last offered price (None on the first round).
    max_rounds : int | None
        Negotiation-level horizon (see :func:`evaluate_buyer`).

    Returns
    -------
    PolicyDecision
        ACCEPT, COUNTER (with a new Offer), or REJECT.
    """
    horizon = max_rounds if max_rounds is not None else policy.max_rounds

    above_floor = buyer_offer.price >= policy.floor_price - _EPS

    # --- Accept if buyer price meets or exceeds list price OR at/above our last offer ---
    if above_floor and (
        buyer_offer.price >= policy.list_price - _EPS
        or (seller_last_price is not None and buyer_offer.price >= seller_last_price - _EPS)
    ):
        return PolicyDecision(
            decision=DecisionType.ACCEPT,
            reason="buyer_price_met_our_requirements",
        )

    # --- Round cap reached → reject ---
    if current_round > horizon:
        return PolicyDecision(
            decision=DecisionType.REJECT,
            reason="max_rounds_exceeded",
        )

    # --- Calculate counter-offer ---
    reference_price = seller_last_price if seller_last_price is not None else policy.list_price
    rate = _concession_rate(current_round, horizon)
    gap = reference_price - buyer_offer.price
    counter_price = reference_price - gap * rate

    # Sanity: counter should be at most the list price
    counter_price = min(counter_price, policy.list_price)

    # Hard constraint LAST: never go below floor
    counter_price = max(counter_price, policy.floor_price)

    # Round to 2 decimal places — never round *down* past the floor
    counter_price = round(counter_price, 2)
    if counter_price < policy.floor_price:
        counter_price = math.ceil(round(policy.floor_price * 100, 6)) / 100

    # --- Crossing detection: we were willing to move to their number anyway ---
    if above_floor and counter_price <= buyer_offer.price + _EPS:
        return PolicyDecision(
            decision=DecisionType.ACCEPT,
            reason="concession_target_met_counterparty_price",
        )

    counter_offer = Offer(
        price=counter_price,
        quantity=policy.quantity,
        terms="",
        round_number=current_round,
    )

    return PolicyDecision(
        decision=DecisionType.COUNTER,
        offer=counter_offer,
        reason=_reason_for_rate(rate),
    )


# ---------------------------------------------------------------------------
# Mandate expiry check (standalone utility)
# ---------------------------------------------------------------------------

def check_mandate_expiry(mandate: BuyerMandate) -> bool:
    """Return True if the mandate has expired."""
    if mandate.expiry is None:
        return False
    return datetime.now(timezone.utc) > mandate.expiry
