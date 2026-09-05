#!/usr/bin/env python3
"""
Batch Evaluation Script for AgentNegotiate.

Simulates 50 diverse procurement scenarios through the **production**
settlement loop (``negotiation_runner.settle``) with the bare policy-engine
evaluators substituted for the LangGraph/Groq ones.  Same turn order, same
termination rules, same final-offer settlement — so these metrics describe
what the API actually does.

(The previous version simulated a *seller-first* loop of its own, while the
runner is buyer-first.  It structurally could not observe the two convergence
bugs this suite now guards against.)

This proves the core constraints:
  • 0 % budget overruns on closed deals
  • 0 % floor violations on any seller offer
  • 0 deals missed when a ZOPA exists (floor_price ≤ max_budget/quantity)

Outputs a metrics table to stdout.

Usage:
  cd AgentNegotiate/backend
  uv run python scripts/batch_eval.py
"""

from __future__ import annotations

import asyncio
import math
import random
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.schemas import BuyerMandate, DecisionType, NegotiationStatus, SellerPolicy
from src.negotiation_runner import Settlement, effective_max_rounds, settle


# ---------------------------------------------------------------------------
# Scenario generator
# ---------------------------------------------------------------------------

def generate_scenarios(n: int = 50, seed: int = 42) -> list[dict]:
    """
    Generate n diverse procurement scenarios with a realistic
    distribution of outcomes.

    Every scenario is a *valid* configuration: ``target_price`` never exceeds
    the per-unit budget cap and ``floor_price`` never exceeds ``list_price``
    (both are now rejected by the schema validators).
    """
    rng = random.Random(seed)
    scenarios = []

    for i in range(n):
        base_price = rng.uniform(50, 500)
        list_price = round(base_price * rng.uniform(1.2, 1.6), 2)
        floor_pct = rng.uniform(0.70, 0.95)
        floor_price = round(list_price * floor_pct, 2)
        quantity = rng.randint(10, 500)
        buyer_rounds = rng.randint(2, 5)
        seller_rounds = rng.randint(2, 5)

        # Target and budget strategy
        r = rng.random()
        if r < 0.40:
            # CLOSEABLE: target near/above floor, generous budget
            desired_target = round(floor_price * rng.uniform(0.90, 1.05), 2)
            max_budget = round(list_price * quantity * 1.1, 2)
        elif r < 0.70:
            # CHALLENGING: target below floor, but budget allows
            desired_target = round(floor_price * rng.uniform(0.70, 0.90), 2)
            max_budget = round(list_price * quantity * rng.uniform(0.9, 1.1), 2)
        else:
            # IMPOSSIBLE: budget too low for a deal (max unit price < floor)
            desired_target = round(floor_price * rng.uniform(0.40, 0.70), 2)
            max_budget = round(floor_price * quantity * rng.uniform(0.5, 0.85), 2)

        # A target above the per-unit budget cap is an incoherent mandate.
        max_unit = max_budget / quantity
        # Floor (not round) to 2 dp so the target can never round up past the cap.
        target_price = math.floor(min(desired_target, max_unit) * 100) / 100

        scenarios.append({
            "id": i + 1,
            "buyer_mandate": BuyerMandate(
                target_price=target_price,
                max_budget=max_budget,
                quantity=quantity,
                max_rounds=buyer_rounds,
            ),
            "seller_policy": SellerPolicy(
                list_price=list_price,
                floor_price=floor_price,
                quantity=quantity,
                max_rounds=seller_rounds,
            ),
        })

    return scenarios


# ---------------------------------------------------------------------------
# Run a single negotiation through the production settlement loop
# ---------------------------------------------------------------------------

async def run_scenario(mandate: BuyerMandate, policy: SellerPolicy) -> dict:
    """Drive the production ``settle()`` loop with policy-engine evaluators."""
    # A ZOPA exists when the seller's floor is reachable within the buyer's
    # per-unit budget cap.  Any such scenario *must* close.
    zopa = policy.floor_price <= mandate.max_unit_price + 1e-9

    result = Settlement()
    floor_violation = False

    async for step in settle(mandate, policy, result=result):
        if (
            step.role == "seller"
            and step.decision.decision == DecisionType.COUNTER
            and step.price is not None
            and step.price < policy.floor_price - 0.01
        ):
            floor_violation = True

    deal_closed = result.status == NegotiationStatus.ACCEPTED
    budget_overrun = bool(
        deal_closed
        and result.final_price is not None
        and result.final_price * mandate.quantity > mandate.max_budget + 0.01
    )

    return {
        "deal_closed": deal_closed,
        "rounds": result.rounds_used,
        "final_price": result.final_price,
        "budget_overrun": budget_overrun,
        "floor_violation": floor_violation,
        "zopa": zopa,
        "missed_deal": bool(zopa and not deal_closed),
        "outcome": f"{result.status.value}:{result.reason}",
        "horizon": effective_max_rounds(mandate, policy),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _main() -> int:
    scenarios = generate_scenarios(50)

    print("=" * 122)
    print("  AgentNegotiate — Batch Evaluation (50 Scenarios, production settle() loop)")
    print("=" * 122)
    print()

    results = []
    for s in scenarios:
        res = await run_scenario(s["buyer_mandate"], s["seller_policy"])
        res["id"] = s["id"]
        res["target"] = s["buyer_mandate"].target_price
        res["list"] = s["seller_policy"].list_price
        res["floor"] = s["seller_policy"].floor_price
        res["max_unit"] = round(s["buyer_mandate"].max_unit_price, 2)
        results.append(res)

    # Table
    print(f"{'#':>3}  {'Target':>8}  {'List':>8}  {'Floor':>8}  {'MaxUnit':>8}  "
          f"{'Rnds':>4}  {'Final':>10}  {'Deal':>4}  {'ZOPA':>4}  {'Miss':>4}  "
          f"{'BdgOK':>5}  {'FlrOK':>5}  Outcome")
    print("-" * 122)
    for r in results:
        final = f"₹{r['final_price']:.2f}" if r["final_price"] else "—"
        print(f"{r['id']:>3}  ₹{r['target']:>7.2f}  ₹{r['list']:>7.2f}  ₹{r['floor']:>7.2f}  "
              f"₹{r['max_unit']:>7.2f}  {r['rounds']:>4}  {final:>10}  "
              f"{'✅' if r['deal_closed'] else '❌':>4}  "
              f"{'✅' if r['zopa'] else '—':>4}  "
              f"{'❌' if r['missed_deal'] else '✅':>4}  "
              f"{'✅' if not r['budget_overrun'] else '❌':>5}  "
              f"{'✅' if not r['floor_violation'] else '❌':>5}  {r['outcome']}")

    # Summary
    total = len(results)
    closed = [r for r in results if r["deal_closed"]]
    n_closed = len(closed)
    n_zopa = sum(1 for r in results if r["zopa"])
    overruns = sum(1 for r in results if r["budget_overrun"])
    floor_viol = sum(1 for r in results if r["floor_violation"])
    missed = sum(1 for r in results if r["missed_deal"])
    avg_rnds = sum(r["rounds"] for r in closed) / n_closed if closed else 0
    ratios = [(r["final_price"] / r["floor"]) for r in closed if r["floor"] > 0 and r["final_price"]]
    avg_ratio = sum(ratios) / len(ratios) if ratios else 0

    print()
    print("=" * 122)
    print("  AGGREGATE METRICS")
    print("=" * 122)
    print(f"  Total scenarios:             {total}")
    print(f"  Scenarios with a ZOPA:       {n_zopa}/{total}")
    print(f"  Deal close rate:             {n_closed}/{total} ({n_closed/total*100:.1f}%)")
    print(f"  Close rate given a ZOPA:     {n_closed}/{n_zopa} "
          f"({(n_closed/n_zopa*100) if n_zopa else 0:.1f}%)")
    print(f"  Average rounds to close:     {avg_rnds:.2f}")
    print(f"  Avg final price / floor:     {avg_ratio:.2%}")
    print(f"  Missed deals despite ZOPA:   {missed}  {'✅ PASS' if missed == 0 else '❌ FAIL'}")
    print(f"  Budget overruns (closed):    {overruns}  {'✅ PASS' if overruns == 0 else '❌ FAIL'}")
    print(f"  Floor violations (any):      {floor_viol}  {'✅ PASS' if floor_viol == 0 else '❌ FAIL'}")
    print()

    if overruns > 0 or floor_viol > 0 or missed > 0:
        print("  ❌ CONSTRAINTS VIOLATED")
        return 1
    print("  ✅ ALL CONSTRAINTS SATISFIED")
    return 0


def main():
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
