"""
Unit tests for the deterministic policy engine and the settlement loop.

These tests prove that every financial constraint is enforced
without any LLM involvement:
  • Budget clamping (buyer never exceeds max_budget)
  • Floor enforcement (seller never goes below floor_price)
  • Concession schedule (current_round / max_rounds of the remaining gap)
  • Round cap (negotiation ends after the shared max_rounds horizon)
  • Accept / reject / crossing conditions
  • Mandate expiry
  • Config coherence validation

Plus regression tests seeded from the three real negotiations that failed in
production (`e5c7b40d`, `8d97b8fc`, `dc4ea883`).
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from src.schemas import (
    BuyerMandate,
    DecisionType,
    NegotiationStatus,
    Offer,
    SellerPolicy,
)
from src.policy_engine import (
    _concession_rate,
    check_mandate_expiry,
    evaluate_buyer,
    evaluate_seller,
)
from src.negotiation_runner import (
    Settlement,
    effective_max_rounds,
    settle,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def buyer_mandate() -> BuyerMandate:
    """Standard buyer mandate: target ₹80, budget ₹10 000, qty 100."""
    return BuyerMandate(
        target_price=80.0,
        max_budget=10_000.0,
        quantity=100,
        max_rounds=3,
    )


@pytest.fixture
def seller_policy() -> SellerPolicy:
    """Standard seller policy: list ₹120, floor ₹90, qty 100."""
    return SellerPolicy(
        list_price=120.0,
        floor_price=90.0,
        quantity=100,
        max_rounds=3,
    )


def _make_offer(price: float, round_number: int = 1, quantity: int = 100) -> Offer:
    """Helper to build an Offer quickly."""
    return Offer(price=price, quantity=quantity, round_number=round_number)


async def _run(mandate: BuyerMandate, policy: SellerPolicy) -> Settlement:
    """Drive the production settlement loop with bare policy-engine evaluators."""
    result = Settlement()
    async for _step in settle(mandate, policy, result=result):
        pass
    return result


# ===================================================================
# Concession schedule
# ===================================================================

class TestConcessionSchedule:
    """The implemented schedule is `current_round / max_rounds` of the gap."""

    @pytest.mark.parametrize("max_rounds", list(range(1, 11)))
    def test_schedule_is_linear_and_ends_at_full_concession(self, max_rounds: int):
        for current_round in range(1, max_rounds + 1):
            rate = _concession_rate(current_round, max_rounds)
            expected = 1.0 if current_round >= max_rounds else current_round / max_rounds
            assert rate == pytest.approx(expected)
            assert 0.0 < rate <= 1.0

        # The final round always concedes the whole remaining gap — this is
        # what makes the two sides meet.
        assert _concession_rate(max_rounds, max_rounds) == 1.0
        assert _concession_rate(max_rounds + 5, max_rounds) == 1.0

    def test_rate_is_monotonically_increasing(self):
        rates = [_concession_rate(r, 5) for r in range(1, 6)]
        assert rates == sorted(rates)
        assert rates == [0.2, 0.4, 0.6, 0.8, 1.0]


# ===================================================================
# Buyer evaluation tests
# ===================================================================

class TestBuyerEvaluation:
    """Tests for evaluate_buyer()."""

    def test_accept_when_seller_at_target(self, buyer_mandate: BuyerMandate):
        """Buyer should ACCEPT if seller's price ≤ target price."""
        offer = _make_offer(price=80.0)
        decision = evaluate_buyer(offer, buyer_mandate, current_round=1)
        assert decision.decision == DecisionType.ACCEPT
        assert decision.reason == "seller_price_met_our_requirements"

    def test_accept_when_seller_below_target(self, buyer_mandate: BuyerMandate):
        """Buyer should ACCEPT if seller's price is below target."""
        offer = _make_offer(price=70.0)
        decision = evaluate_buyer(offer, buyer_mandate, current_round=1)
        assert decision.decision == DecisionType.ACCEPT

    def test_counter_round1_of_3(self, buyer_mandate: BuyerMandate):
        """Round 1 of 3: buyer concedes 1/3 of the gap from target."""
        seller_offer = _make_offer(price=120.0, round_number=1)
        decision = evaluate_buyer(seller_offer, buyer_mandate, current_round=1)

        assert decision.decision == DecisionType.COUNTER
        assert decision.offer is not None
        # gap = 120 - 80 = 40;  concession = 40 * (1/3) = 13.33;  counter = 93.33
        assert decision.offer.price == pytest.approx(93.33)

    def test_counter_round2_of_3(self):
        """Round 2 of 3: buyer concedes 2/3 of the gap from its last price."""
        # Use a higher budget so clamping doesn't interfere with concession math
        mandate = BuyerMandate(
            target_price=80.0,
            max_budget=15_000.0,  # max unit = 150
            quantity=100,
            max_rounds=3,
        )
        seller_offer = _make_offer(price=110.0, round_number=2)
        decision = evaluate_buyer(
            seller_offer, mandate, current_round=2, buyer_last_price=100.0
        )

        assert decision.decision == DecisionType.COUNTER
        assert decision.offer is not None
        # gap = 110 - 100 = 10;  concession = 10 * (2/3) = 6.67;  counter = 106.67
        assert decision.offer.price == pytest.approx(106.67)

    def test_counter_round3_of_4(self):
        """Round 3 of 4: buyer concedes 3/4 of the gap from its last price."""
        mandate = BuyerMandate(
            target_price=80.0,
            max_budget=15_000.0,  # max unit = 150
            quantity=100,
            max_rounds=4,
        )
        seller_offer = _make_offer(price=105.0, round_number=3)
        decision = evaluate_buyer(
            seller_offer, mandate, current_round=3, buyer_last_price=103.0
        )

        assert decision.decision == DecisionType.COUNTER
        assert decision.offer is not None
        # gap = 105 - 103 = 2;  concession = 2 * 0.75 = 1.5;  counter = 104.5
        assert decision.offer.price == pytest.approx(104.5)

    def test_final_round_crossing_accepts_instead_of_restating(self):
        """
        On the final round the buyer concedes the *whole* gap, so its counter
        would equal the seller's price.  It must accept rather than restate
        the same number — the bug behind `e5c7b40d`.
        """
        mandate = BuyerMandate(
            target_price=80.0,
            max_budget=15_000.0,
            quantity=100,
            max_rounds=3,
        )
        seller_offer = _make_offer(price=105.0, round_number=3)
        decision = evaluate_buyer(
            seller_offer, mandate, current_round=3, buyer_last_price=103.0
        )
        assert decision.decision == DecisionType.ACCEPT
        assert decision.reason == "concession_target_met_counterparty_price"

    def test_no_crossing_accept_above_budget(self):
        """Crossing must never accept a price above the per-unit budget cap."""
        mandate = BuyerMandate(
            target_price=80.0,
            max_budget=9_000.0,  # max unit = 90
            quantity=100,
            max_rounds=3,
        )
        seller_offer = _make_offer(price=105.0, round_number=3)
        decision = evaluate_buyer(
            seller_offer, mandate, current_round=3, buyer_last_price=88.0
        )
        assert decision.decision == DecisionType.COUNTER
        assert decision.offer is not None
        assert decision.offer.price <= mandate.max_unit_price

    def test_budget_clamping(self):
        """Counter-offer price is clamped so total ≤ max_budget."""
        mandate = BuyerMandate(
            target_price=80.0,
            max_budget=8_500.0,  # max unit price = 85
            quantity=100,
            max_rounds=3,
        )
        seller_offer = _make_offer(price=200.0, round_number=1)
        decision = evaluate_buyer(seller_offer, mandate, current_round=1)

        assert decision.decision == DecisionType.COUNTER
        assert decision.offer is not None
        # Without clamping: 80 + (200-80)/3 = 120 → exceeds 85
        assert decision.offer.price <= mandate.max_unit_price
        assert decision.offer.price == 85.0  # clamped to max

    def test_reject_after_max_rounds(self, buyer_mandate: BuyerMandate):
        """Buyer rejects when current_round exceeds max_rounds."""
        seller_offer = _make_offer(price=110.0, round_number=4)
        decision = evaluate_buyer(
            seller_offer, buyer_mandate, current_round=4, buyer_last_price=103.0
        )
        assert decision.decision == DecisionType.REJECT
        assert decision.reason == "max_rounds_exceeded"

    def test_shared_horizon_overrides_mandate_max_rounds(self, buyer_mandate: BuyerMandate):
        """
        The runner passes a single negotiation-level horizon.  With a horizon
        of 5 the buyer must NOT walk away at its own limit of 3.
        """
        seller_offer = _make_offer(price=95.0, round_number=4)
        decision = evaluate_buyer(
            seller_offer, buyer_mandate, current_round=4,
            buyer_last_price=85.0, max_rounds=5,
        )
        assert decision.decision != DecisionType.REJECT

    def test_reject_on_expired_mandate(self):
        """Buyer rejects if mandate has expired."""
        mandate = BuyerMandate(
            target_price=80.0,
            max_budget=10_000.0,
            quantity=100,
            max_rounds=3,
            expiry=datetime.now(timezone.utc) - timedelta(hours=1),  # already expired
        )
        seller_offer = _make_offer(price=90.0)
        decision = evaluate_buyer(seller_offer, mandate, current_round=1)
        assert decision.decision == DecisionType.REJECT
        assert decision.reason == "mandate_expired"

    def test_counter_preserves_quantity(self, buyer_mandate: BuyerMandate):
        """Counter-offer quantity matches the mandate quantity."""
        seller_offer = _make_offer(price=120.0)
        decision = evaluate_buyer(seller_offer, buyer_mandate, current_round=1)
        assert decision.offer is not None
        assert decision.offer.quantity == buyer_mandate.quantity

    def test_counter_price_never_below_target(self, buyer_mandate: BuyerMandate):
        """Even with math rounding, counter should be ≥ target price."""
        # A seller price very close to target
        seller_offer = _make_offer(price=81.0)
        decision = evaluate_buyer(seller_offer, buyer_mandate, current_round=1)
        # 81 is above target 80 but the round-1 concession lands below 81,
        # so this is a counter, not a crossing accept.
        assert decision.offer is not None
        assert decision.offer.price >= buyer_mandate.target_price


# ===================================================================
# Seller evaluation tests
# ===================================================================

class TestSellerEvaluation:
    """Tests for evaluate_seller()."""

    def test_accept_when_buyer_at_list(self, seller_policy: SellerPolicy):
        """Seller should ACCEPT if buyer's price ≥ list price."""
        offer = _make_offer(price=120.0)
        decision = evaluate_seller(offer, seller_policy, current_round=1)
        assert decision.decision == DecisionType.ACCEPT
        assert decision.reason == "buyer_price_met_our_requirements"

    def test_accept_when_buyer_above_list(self, seller_policy: SellerPolicy):
        """Seller should ACCEPT if buyer exceeds list price."""
        offer = _make_offer(price=130.0)
        decision = evaluate_seller(offer, seller_policy, current_round=1)
        assert decision.decision == DecisionType.ACCEPT

    def test_counter_round1_of_3(self, seller_policy: SellerPolicy):
        """Round 1 of 3: seller concedes 1/3 of the gap from list price."""
        buyer_offer = _make_offer(price=80.0, round_number=1)
        decision = evaluate_seller(buyer_offer, seller_policy, current_round=1)

        assert decision.decision == DecisionType.COUNTER
        assert decision.offer is not None
        # gap = 120 - 80 = 40;  concession = 40 * (1/3) = 13.33;  counter = 106.67
        assert decision.offer.price == pytest.approx(106.67)

    def test_counter_round2_of_3(self, seller_policy: SellerPolicy):
        """Round 2 of 3: seller concedes 2/3 of the gap from its last price."""
        buyer_offer = _make_offer(price=95.0, round_number=2)
        decision = evaluate_seller(
            buyer_offer, seller_policy, current_round=2, seller_last_price=100.0
        )

        assert decision.decision == DecisionType.COUNTER
        assert decision.offer is not None
        # gap = 100 - 95 = 5;  concession = 5 * (2/3) = 3.33;  counter = 96.67
        assert decision.offer.price == pytest.approx(96.67)

    def test_counter_round3_of_4(self, seller_policy: SellerPolicy):
        """Round 3 of 4: seller concedes 3/4 of the gap from its last price."""
        policy = SellerPolicy(
            list_price=120.0, floor_price=90.0, quantity=100, max_rounds=4,
        )
        buyer_offer = _make_offer(price=96.5, round_number=3)
        decision = evaluate_seller(
            buyer_offer, policy, current_round=3, seller_last_price=98.5
        )

        assert decision.decision == DecisionType.COUNTER
        assert decision.offer is not None
        # gap = 98.5 - 96.5 = 2;  concession = 2 * 0.75 = 1.5;  counter = 97.0
        assert decision.offer.price == pytest.approx(97.0)

    def test_final_round_crossing_accepts(self, seller_policy: SellerPolicy):
        """On the final round the seller accepts rather than restating the price."""
        buyer_offer = _make_offer(price=96.0, round_number=3)
        decision = evaluate_seller(
            buyer_offer, seller_policy, current_round=3, seller_last_price=98.5
        )
        assert decision.decision == DecisionType.ACCEPT
        assert decision.reason == "concession_target_met_counterparty_price"

    def test_no_crossing_accept_below_floor(self, seller_policy: SellerPolicy):
        """Crossing must never accept a price below the floor."""
        buyer_offer = _make_offer(price=70.0, round_number=3)
        decision = evaluate_seller(
            buyer_offer, seller_policy, current_round=3, seller_last_price=98.5
        )
        assert decision.decision == DecisionType.COUNTER
        assert decision.offer is not None
        assert decision.offer.price >= seller_policy.floor_price

    def test_floor_enforcement(self):
        """Counter-offer price is clamped so it never goes below floor."""
        policy = SellerPolicy(
            list_price=100.0,
            floor_price=95.0,
            quantity=100,
            max_rounds=3,
        )
        buyer_offer = _make_offer(price=50.0, round_number=1)
        decision = evaluate_seller(buyer_offer, policy, current_round=1)

        assert decision.decision == DecisionType.COUNTER
        assert decision.offer is not None
        # Without clamping: 100 - (100-50)/3 = 83.33 → below floor 95
        assert decision.offer.price >= policy.floor_price
        assert decision.offer.price == 95.0  # clamped to floor

    def test_reject_after_max_rounds(self, seller_policy: SellerPolicy):
        """Seller rejects when current_round exceeds max_rounds."""
        buyer_offer = _make_offer(price=80.0, round_number=4)
        decision = evaluate_seller(
            buyer_offer, seller_policy, current_round=4, seller_last_price=98.0
        )
        assert decision.decision == DecisionType.REJECT
        assert decision.reason == "max_rounds_exceeded"

    def test_counter_preserves_quantity(self, seller_policy: SellerPolicy):
        """Counter-offer quantity matches the policy quantity."""
        buyer_offer = _make_offer(price=80.0)
        decision = evaluate_seller(buyer_offer, seller_policy, current_round=1)
        assert decision.offer is not None
        assert decision.offer.quantity == seller_policy.quantity

    def test_counter_price_never_above_list(self, seller_policy: SellerPolicy):
        """Counter should be ≤ list price."""
        buyer_offer = _make_offer(price=85.0)
        decision = evaluate_seller(buyer_offer, seller_policy, current_round=1)
        assert decision.offer is not None
        assert decision.offer.price <= seller_policy.list_price


# ===================================================================
# Clamp ordering — the two hard guarantees (bugs 2.1 / 2.2)
# ===================================================================

class TestClampOrdering:
    """
    The hard constraint must be applied *last*.

    These use ``model_construct`` to build the incoherent configs that the
    validators now reject, proving the engine holds the line even if such a
    config reaches it some other way.
    """

    def test_budget_clamp_survives_target_sanity_bound(self):
        """target ₹100, budget ₹800, qty 10 → counter must not exceed ₹80/unit."""
        mandate = BuyerMandate.model_construct(
            target_price=100.0,
            max_budget=800.0,
            quantity=10,
            max_rounds=3,
            expiry=None,
        )
        assert mandate.max_unit_price == 80.0

        decision = evaluate_buyer(
            _make_offer(price=200.0, quantity=10), mandate, current_round=1
        )
        assert decision.decision == DecisionType.COUNTER
        assert decision.offer is not None
        assert decision.offer.price <= mandate.max_unit_price
        assert decision.offer.price * mandate.quantity <= mandate.max_budget

    def test_floor_clamp_survives_list_sanity_bound(self):
        """floor ₹150, list ₹100 → counter must not fall below ₹150."""
        policy = SellerPolicy.model_construct(
            list_price=100.0,
            floor_price=150.0,
            quantity=10,
            max_rounds=3,
        )
        decision = evaluate_seller(
            _make_offer(price=50.0, quantity=10), policy, current_round=1
        )
        assert decision.decision == DecisionType.COUNTER
        assert decision.offer is not None
        assert decision.offer.price >= policy.floor_price


# ===================================================================
# Config coherence validation (bug 2.3)
# ===================================================================

class TestConfigValidation:
    """Incoherent configs are rejected at construction time."""

    def test_target_above_per_unit_budget_is_rejected(self):
        with pytest.raises(ValueError, match="per-unit budget cap"):
            BuyerMandate(target_price=100.0, max_budget=800.0, quantity=10)

    def test_target_equal_to_per_unit_budget_is_allowed(self):
        mandate = BuyerMandate(target_price=80.0, max_budget=800.0, quantity=10)
        assert mandate.max_unit_price == 80.0

    def test_floor_above_list_is_rejected(self):
        with pytest.raises(ValueError, match="exceeds list_price"):
            SellerPolicy(list_price=100.0, floor_price=150.0, quantity=10)

    def test_floor_equal_to_list_is_allowed(self):
        policy = SellerPolicy(list_price=100.0, floor_price=100.0, quantity=10)
        assert policy.floor_price == policy.list_price


# ===================================================================
# Mandate expiry utility
# ===================================================================

class TestMandateExpiry:
    """Tests for check_mandate_expiry()."""

    def test_not_expired_when_no_expiry(self, buyer_mandate: BuyerMandate):
        """No expiry set → not expired."""
        assert check_mandate_expiry(buyer_mandate) is False

    def test_expired_mandate(self):
        """Past expiry → expired."""
        mandate = BuyerMandate(
            target_price=80.0,
            max_budget=10_000.0,
            quantity=100,
            expiry=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        assert check_mandate_expiry(mandate) is True

    def test_future_mandate(self):
        """Future expiry → not expired."""
        mandate = BuyerMandate(
            target_price=80.0,
            max_budget=10_000.0,
            quantity=100,
            expiry=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        assert check_mandate_expiry(mandate) is False


# ===================================================================
# Full negotiation via the production settlement loop
# ===================================================================

class TestSettlement:
    """End-to-end runs of `settle()` — the same loop the API drives."""

    async def test_convergence_to_deal(self):
        """Both sides converge and the deal closes inside the round budget."""
        mandate = BuyerMandate(
            target_price=80.0, max_budget=12_000.0, quantity=100, max_rounds=3,
        )
        policy = SellerPolicy(
            list_price=120.0, floor_price=90.0, quantity=100, max_rounds=3,
        )

        result = await _run(mandate, policy)

        assert result.status == NegotiationStatus.ACCEPTED
        assert result.final_price is not None
        # Both hard constraints hold on the agreed price.
        assert result.final_price * mandate.quantity <= mandate.max_budget
        assert result.final_price >= policy.floor_price
        assert result.rounds_used <= effective_max_rounds(mandate, policy)

        # No seller counter ever dipped below the floor, and no buyer counter
        # ever breached the budget.
        for step in result.steps:
            if step.price is None:
                continue
            if step.role == "seller" and step.offer is not None:
                assert step.offer.price >= policy.floor_price
            if step.role == "buyer" and step.offer is not None:
                assert step.offer.price * mandate.quantity <= mandate.max_budget

    async def test_regression_e5c7b40d_closes_instead_of_rejecting(self):
        """
        Real negotiation e5c7b40d: ZOPA ₹6000–7000.  Both sides landed on
        ₹6582.03 and it was still recorded as "no deal", because the loop
        exited right after the seller's closing counter.
        """
        mandate = BuyerMandate(
            target_price=5000.0, max_budget=350_000.0, quantity=50, max_rounds=4,
        )
        policy = SellerPolicy(
            list_price=8000.0, floor_price=6000.0, quantity=50, max_rounds=4,
        )

        result = await _run(mandate, policy)

        assert result.status == NegotiationStatus.ACCEPTED
        assert result.final_price == pytest.approx(6582.04, abs=1.0)
        assert result.final_price >= policy.floor_price
        assert result.final_price * mandate.quantity <= mandate.max_budget

    async def test_regression_8d97b8fc_opening_bid_is_the_target(self):
        """
        Real negotiation 8d97b8fc: the buyer opened at ₹0.01 because the LLM
        parse fed `min(parsed_price, target_price)`.  The opening bid must be
        the mandate's target price, full stop.
        """
        mandate = BuyerMandate(
            target_price=80.0, max_budget=12_000.0, quantity=100, max_rounds=3,
        )
        policy = SellerPolicy(
            list_price=120.0, floor_price=90.0, quantity=100, max_rounds=3,
        )

        result = await _run(mandate, policy)

        opening = result.steps[0]
        assert opening.role == "buyer"
        assert opening.decision.reason == "opening_offer"
        assert opening.price == mandate.target_price

        assert result.status == NegotiationStatus.ACCEPTED
        assert result.final_price >= policy.floor_price
        assert result.final_price * mandate.quantity <= mandate.max_budget

    async def test_hostile_llm_price_cannot_reach_the_opening_bid(self):
        """A hallucinated ₹0.01 parse must not influence the opening offer."""
        from src.buyer_agent import build_opening_offer

        mandate = BuyerMandate(
            target_price=80.0, max_budget=12_000.0, quantity=100, max_rounds=3,
        )
        offer = build_opening_offer(
            mandate,
            {"price": 0.01, "quantity": 7, "terms": "net 30"},
        )
        assert offer.price == 80.0
        assert offer.quantity == mandate.quantity  # mandate wins
        assert offer.terms == "net 30"             # terms may still come from the LLM

    async def test_regression_dc4ea883_reaches_a_terminal_state(self):
        """Real negotiation dc4ea883 was stuck `active` forever."""
        mandate = BuyerMandate(
            target_price=80.0, max_budget=12_000.0, quantity=100, max_rounds=3,
        )
        policy = SellerPolicy(
            list_price=120.0, floor_price=90.0, quantity=100, max_rounds=3,
        )
        result = await _run(mandate, policy)
        assert result.status != NegotiationStatus.ACTIVE

    async def test_asymmetric_max_rounds_no_premature_walkaway(self):
        """
        buyer(2 rounds) / seller(5 rounds), ZOPA ₹90–105.

        The loop used to run to max(2, 5) = 5 while the buyer rejected past
        its own limit of 2.  With a single shared horizon of min(2, 5) = 2,
        both sides concede on the same schedule and the deal closes.
        """
        mandate = BuyerMandate(
            target_price=80.0, max_budget=10_500.0, quantity=100, max_rounds=2,
        )
        policy = SellerPolicy(
            list_price=120.0, floor_price=90.0, quantity=100, max_rounds=5,
        )

        assert effective_max_rounds(mandate, policy) == 2

        result = await _run(mandate, policy)

        assert result.status == NegotiationStatus.ACCEPTED
        assert result.reason != "max_rounds_exceeded"
        assert 90.0 <= result.final_price <= 105.0

    async def test_expired_mandate_ends_expired_before_any_offer(self):
        """An expired mandate produces EXPIRED, not REJECTED, and makes no offer."""
        mandate = BuyerMandate(
            target_price=80.0, max_budget=12_000.0, quantity=100, max_rounds=3,
            expiry=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        policy = SellerPolicy(
            list_price=120.0, floor_price=90.0, quantity=100, max_rounds=3,
        )

        result = await _run(mandate, policy)

        assert result.status == NegotiationStatus.EXPIRED
        assert result.reason == "mandate_expired"
        assert result.steps == []

    async def test_settles_at_the_smaller_quantity(self):
        """The settled quantity never exceeds what the seller holds."""
        mandate = BuyerMandate(
            target_price=80.0, max_budget=12_000.0, quantity=100, max_rounds=3,
        )
        policy = SellerPolicy(
            list_price=120.0, floor_price=90.0, quantity=60, max_rounds=3,
        )

        result = await _run(mandate, policy)

        assert result.status == NegotiationStatus.ACCEPTED
        assert result.final_quantity == 60
        for step in result.steps:
            assert step.quantity == 60

    async def test_no_deal_when_budget_below_floor(self):
        """If buyer's max unit price < seller's floor, no deal is possible."""
        mandate = BuyerMandate(
            target_price=50.0,
            max_budget=7_000.0,  # max unit = 70
            quantity=100,
            max_rounds=3,
        )
        policy = SellerPolicy(
            list_price=120.0, floor_price=90.0, quantity=100, max_rounds=3,
        )

        result = await _run(mandate, policy)

        assert result.status == NegotiationStatus.REJECTED
        assert result.final_price is None
        for step in result.steps:
            if step.role == "seller" and step.offer is not None:
                assert step.offer.price >= policy.floor_price
            if step.role == "buyer" and step.offer is not None:
                assert step.offer.price <= mandate.max_unit_price

    async def test_immediate_accept_when_target_above_list(self):
        """If the buyer's target ≥ the seller's list price, the deal closes at once."""
        mandate = BuyerMandate(
            target_price=150.0, max_budget=20_000.0, quantity=100, max_rounds=3,
        )
        policy = SellerPolicy(
            list_price=120.0, floor_price=90.0, quantity=100, max_rounds=3,
        )

        result = await _run(mandate, policy)

        assert result.status == NegotiationStatus.ACCEPTED
        assert result.rounds_used == 1
        assert result.final_price == 150.0


# ===================================================================
# Property test: a ZOPA always closes
# ===================================================================

class TestZopaProperty:
    """
    Whenever ``floor_price <= max_budget / quantity`` there is a price both
    sides can accept, so the negotiation MUST close — with both hard
    constraints intact.
    """

    @pytest.mark.parametrize("seed", list(range(12)))
    async def test_any_zopa_closes(self, seed: int):
        rng = random.Random(seed)

        for _ in range(25):
            list_price = round(rng.uniform(10, 5_000), 2)
            floor_price = round(list_price * rng.uniform(0.3, 1.0), 2)
            quantity = rng.randint(1, 500)
            # Budget guarantees a ZOPA: max unit price ≥ floor.
            max_unit = floor_price * rng.uniform(1.0, 2.0)
            max_budget = round(max_unit * quantity, 2)
            target_price = round(
                min(rng.uniform(0.1, 1.0) * floor_price, max_budget / quantity), 2
            )
            if target_price <= 0:
                continue

            mandate = BuyerMandate(
                target_price=target_price,
                max_budget=max_budget,
                quantity=quantity,
                max_rounds=rng.randint(1, 10),
            )
            policy = SellerPolicy(
                list_price=list_price,
                floor_price=floor_price,
                quantity=quantity,
                max_rounds=rng.randint(1, 10),
            )
            assert policy.floor_price <= mandate.max_unit_price + 1e-9

            result = await _run(mandate, policy)

            assert result.status == NegotiationStatus.ACCEPTED, (
                f"missed a deal despite a ZOPA: target={target_price} "
                f"budget={max_budget} qty={quantity} list={list_price} "
                f"floor={floor_price} horizon={effective_max_rounds(mandate, policy)} "
                f"reason={result.reason}"
            )
            assert result.final_price >= policy.floor_price - 1e-9
            assert result.final_price * quantity <= mandate.max_budget + 0.01


# ===================================================================
# Razorpay webhook signature verification (bug 4.1)
# ===================================================================

class TestWebhookSignature:
    """The webhook must authenticate the raw body, not the checkout formula."""

    def test_valid_signature_accepted(self, monkeypatch):
        import hashlib
        import hmac

        from src import payments

        secret = "whsec_test_123"
        monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", secret)

        body = b'{"event":"payment.captured"}'
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

        assert payments.verify_webhook_signature(body, sig) is True

    def test_wrong_signature_rejected(self, monkeypatch):
        from src import payments

        monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "whsec_test_123")
        assert payments.verify_webhook_signature(b"{}", "deadbeef") is False

    def test_missing_signature_rejected(self, monkeypatch):
        from src import payments

        monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "whsec_test_123")
        assert payments.verify_webhook_signature(b"{}", "") is False

    def test_unset_secret_rejects_rather_than_trusts(self, monkeypatch):
        from src import payments

        monkeypatch.delenv("RAZORPAY_WEBHOOK_SECRET", raising=False)
        assert payments.verify_webhook_signature(b"{}", "anything") is False

    def test_body_tampering_is_detected(self, monkeypatch):
        import hashlib
        import hmac

        from src import payments

        secret = "whsec_test_123"
        monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", secret)

        body = b'{"event":"payment.captured","amount":100}'
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        tampered = b'{"event":"payment.captured","amount":999999}'

        assert payments.verify_webhook_signature(tampered, sig) is False
