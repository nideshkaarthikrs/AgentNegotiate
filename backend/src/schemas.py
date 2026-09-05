"""
Pydantic schemas for the AgentNegotiate system.

Defines all data models for offers, buyer mandates, seller policies,
and negotiation state. These are the core data structures that flow
through the LangGraph agents and the deterministic policy engine.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class NegotiationStatus(str, enum.Enum):
    """Lifecycle status of a negotiation session."""
    ACTIVE = "active"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"


class DecisionType(str, enum.Enum):
    """The three possible outcomes of a policy-engine evaluation."""
    ACCEPT = "accept"
    COUNTER = "counter"
    REJECT = "reject"


class AuditEventType(str, enum.Enum):
    """Types of events recorded in the tamper-evident audit log."""
    NEGOTIATION_STARTED = "negotiation_started"
    BUYER_OFFER = "buyer_offer"
    SELLER_OFFER = "seller_offer"
    BUYER_ACCEPT = "buyer_accept"
    SELLER_ACCEPT = "seller_accept"
    BUYER_REJECT = "buyer_reject"
    SELLER_REJECT = "seller_reject"
    MANDATE_EXPIRED = "mandate_expired"
    PAYMENT_CREATED = "payment_created"
    PAYMENT_SUCCESS = "payment_success"
    PAYMENT_FAILED = "payment_failed"


# ---------------------------------------------------------------------------
# Core domain models
# ---------------------------------------------------------------------------

class Offer(BaseModel):
    """A single price offer exchanged between buyer and seller."""
    price: float = Field(..., ge=0, description="Offered unit price")
    quantity: int = Field(..., gt=0, description="Number of units")
    terms: str = Field(default="", description="Free-text delivery / payment terms")
    round_number: int = Field(..., ge=1, description="Which negotiation round this offer belongs to")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def total_value(self) -> float:
        """Total order value = price × quantity."""
        return self.price * self.quantity


class BuyerMandate(BaseModel):
    """
    Configuration that governs the buyer agent's behaviour.

    The policy engine uses this to decide whether to accept, counter,
    or reject a seller's offer — and to clamp any counter-offer so that
    the total never exceeds `max_budget`.
    """
    target_price: float = Field(..., gt=0, description="Ideal unit price the buyer wants")
    max_budget: float = Field(..., gt=0, description="Absolute ceiling on total order value")
    quantity: int = Field(..., gt=0, description="Number of units to procure")
    max_rounds: int = Field(default=3, ge=1, le=10, description="Maximum negotiation rounds before walking away")
    expiry: Optional[datetime] = Field(default=None, description="UTC deadline after which the mandate is void")

    @property
    def max_unit_price(self) -> float:
        """Derived hard cap: max_budget / quantity."""
        return self.max_budget / self.quantity

    @model_validator(mode="after")
    def _check_coherent(self) -> "BuyerMandate":
        """
        The target price must be reachable within the budget.

        If ``target_price > max_budget / quantity`` the buyer's opening bid
        would already breach its own hard budget cap, which makes the
        budget clamp impossible to honour.
        """
        if self.target_price > self.max_unit_price + 1e-9:
            raise ValueError(
                f"target_price ({self.target_price}) exceeds the per-unit budget cap "
                f"max_budget/quantity ({self.max_unit_price:.4f})"
            )
        return self


class SellerPolicy(BaseModel):
    """
    Configuration that governs the seller agent's behaviour.

    The policy engine uses this to decide whether to accept, counter,
    or reject a buyer's offer — and to clamp any counter-offer so that
    the price never drops below `floor_price`.
    """
    list_price: float = Field(..., gt=0, description="Published / opening unit price")
    floor_price: float = Field(..., gt=0, description="Absolute minimum unit price (never go below)")
    quantity: int = Field(..., gt=0, description="Number of units available")
    max_rounds: int = Field(default=3, ge=1, le=10, description="Maximum negotiation rounds before walking away")

    @model_validator(mode="after")
    def _check_coherent(self) -> "SellerPolicy":
        """
        The floor must not sit above the list price.

        Otherwise the seller's opening price already breaches its own floor
        and the floor clamp cannot be honoured.
        """
        if self.floor_price > self.list_price + 1e-9:
            raise ValueError(
                f"floor_price ({self.floor_price}) exceeds list_price ({self.list_price})"
            )
        return self


# ---------------------------------------------------------------------------
# Decision result returned by the policy engine
# ---------------------------------------------------------------------------

class PolicyDecision(BaseModel):
    """Output of a single policy-engine evaluation step."""
    decision: DecisionType
    offer: Optional[Offer] = Field(default=None, description="Counter-offer (only when decision == COUNTER)")
    reason: str = Field(default="", description="Machine-readable reason for the decision")


# ---------------------------------------------------------------------------
# Negotiation state (persisted in SQLite, flows through LangGraph)
# ---------------------------------------------------------------------------

class NegotiationRound(BaseModel):
    """One complete exchange: a buyer offer and the seller's response (or vice-versa)."""
    round_number: int
    buyer_offer: Optional[Offer] = None
    seller_offer: Optional[Offer] = None
    buyer_decision: Optional[PolicyDecision] = None
    seller_decision: Optional[PolicyDecision] = None


class NegotiationState(BaseModel):
    """
    Full state of a negotiation session.

    This is the LangGraph state object that gets passed between nodes.
    """
    negotiation_id: str = Field(..., description="Unique identifier for this negotiation")
    status: NegotiationStatus = NegotiationStatus.ACTIVE
    current_round: int = Field(default=1, ge=1)
    buyer_mandate: BuyerMandate
    seller_policy: SellerPolicy
    rounds: list[NegotiationRound] = Field(default_factory=list)
    final_price: Optional[float] = None
    final_quantity: Optional[int] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # -- Procurement request text (input from user) --
    procurement_request: str = Field(default="", description="Original natural-language procurement request")

    # -- Payment --
    razorpay_order_id: Optional[str] = None
    razorpay_payment_link: Optional[str] = None
    payment_status: Optional[str] = None


# ---------------------------------------------------------------------------
# Audit log entry
# ---------------------------------------------------------------------------

class AuditEntry(BaseModel):
    """A single row in the hash-chained audit log."""
    id: Optional[int] = None
    negotiation_id: str
    event_type: AuditEventType
    data: str = Field(default="{}", description="JSON-serialised event payload")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    prev_hash: str = Field(default="GENESIS", description="SHA-256 hash of the previous entry")
    hash: str = Field(default="", description="SHA-256 hash of this entry")
