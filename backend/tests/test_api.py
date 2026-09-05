"""
Integration tests for the ``/api/negotiations/{id}/verify-payment`` route.

This route exists because ``POST /api/webhooks/razorpay`` — the only other
place ``payment_status`` ever becomes "captured" — requires Razorpay's
servers to reach ours over the public internet. That's unavailable against
``localhost`` in local development, so the webhook never fires there and the
UI is stuck showing "Pay Now" forever even after a real test-mode payment
completes. ``verify-payment`` lets the browser's own post-payment redirect
(which carries a Razorpay-signed payload regardless of network reachability)
confirm the payment instead.

Uses a throwaway SQLite file per test (via ``database.DB_PATH`` monkeypatch)
so these tests never touch the real ``agent_negotiate.db``.
"""

import asyncio
import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient

from src import database as db
from src import payments
from src.schemas import BuyerMandate, NegotiationState, NegotiationStatus, SellerPolicy


def _sign(payment_link_id: str, reference_id: str, status: str, payment_id: str, secret: str) -> str:
    msg = f"{payment_link_id}|{reference_id}|{status}|{payment_id}"
    return hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "test_secret")
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_dummy")
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    asyncio.run(db.init_db())

    from src import api as api_module
    with TestClient(api_module.app) as c:
        yield c

    # Every asyncio.run() call above (and every direct db.* call the test
    # body makes) opens its own aiosqlite connection, keyed by
    # (path, id(event loop)) — a fresh loop per asyncio.run() means a fresh
    # connection/background thread each time. None of those are the app's
    # own lifespan connection (already closed by the `with` above), so they
    # must be closed here or the worker threads keep the process alive after
    # the test session finishes.
    asyncio.run(db.close_connections())


async def _seed_accepted_negotiation(negotiation_id: str, *, payment_status: str | None, payment_link_id: str = "plink_test123"):
    state = NegotiationState(
        negotiation_id=negotiation_id,
        status=NegotiationStatus.ACCEPTED,
        buyer_mandate=BuyerMandate(target_price=80, max_budget=10000, quantity=100, max_rounds=3),
        seller_policy=SellerPolicy(list_price=95, floor_price=90, quantity=100, max_rounds=3),
        final_price=90.0,
        final_quantity=100,
    )
    await db.create_negotiation(state)
    if payment_status is not None:
        await db.set_payment_info(
            negotiation_id,
            razorpay_order_id="order_test123",
            razorpay_payment_link=f"https://rzp.io/{payment_link_id}",
            payment_status=payment_status,
        )
    return payment_link_id


class TestVerifyPaymentEndpoint:
    def test_valid_paid_signature_marks_captured(self, client):
        neg_id = "neg-valid-paid"
        link_id = asyncio.run(_seed_accepted_negotiation(neg_id, payment_status="created"))
        sig = _sign(link_id, "", "paid", "pay_abc", "test_secret")

        resp = client.post(
            f"/api/negotiations/{neg_id}/verify-payment",
            json={
                "razorpay_payment_id": "pay_abc",
                "razorpay_payment_link_id": link_id,
                "razorpay_payment_link_status": "paid",
                "razorpay_signature": sig,
            },
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body == {"verified": True, "payment_status": "captured"}

        neg = asyncio.run(db.get_negotiation(neg_id))
        assert neg["payment_status"] == "captured"

    def test_invalid_signature_is_rejected_and_does_not_mutate_state(self, client):
        neg_id = "neg-bad-sig"
        link_id = asyncio.run(_seed_accepted_negotiation(neg_id, payment_status="created"))

        resp = client.post(
            f"/api/negotiations/{neg_id}/verify-payment",
            json={
                "razorpay_payment_id": "pay_abc",
                "razorpay_payment_link_id": link_id,
                "razorpay_payment_link_status": "paid",
                "razorpay_signature": "not_the_real_signature",
            },
        )

        assert resp.status_code == 400
        neg = asyncio.run(db.get_negotiation(neg_id))
        assert neg["payment_status"] == "created"

    def test_unpaid_status_does_not_mark_captured(self, client):
        # A cancelled/expired payment link redirect must not flip the UI to
        # "paid" just because the signature itself is valid.
        neg_id = "neg-cancelled"
        link_id = asyncio.run(_seed_accepted_negotiation(neg_id, payment_status="created"))
        sig = _sign(link_id, "", "cancelled", "pay_abc", "test_secret")

        resp = client.post(
            f"/api/negotiations/{neg_id}/verify-payment",
            json={
                "razorpay_payment_id": "pay_abc",
                "razorpay_payment_link_id": link_id,
                "razorpay_payment_link_status": "cancelled",
                "razorpay_signature": sig,
            },
        )

        assert resp.status_code == 200
        assert resp.json() == {"verified": True, "payment_status": "created"}

    def test_already_captured_is_idempotent(self, client):
        neg_id = "neg-already-captured"
        link_id = asyncio.run(_seed_accepted_negotiation(neg_id, payment_status="captured"))
        sig = _sign(link_id, "", "paid", "pay_abc", "test_secret")

        resp = client.post(
            f"/api/negotiations/{neg_id}/verify-payment",
            json={
                "razorpay_payment_id": "pay_abc",
                "razorpay_payment_link_id": link_id,
                "razorpay_payment_link_status": "paid",
                "razorpay_signature": sig,
            },
        )

        assert resp.status_code == 200
        assert resp.json() == {"verified": True, "payment_status": "captured"}

    def test_unknown_negotiation_is_404(self, client):
        sig = _sign("plink_x", "", "paid", "pay_abc", "test_secret")
        resp = client.post(
            "/api/negotiations/does-not-exist/verify-payment",
            json={
                "razorpay_payment_id": "pay_abc",
                "razorpay_payment_link_id": "plink_x",
                "razorpay_payment_link_status": "paid",
                "razorpay_signature": sig,
            },
        )
        assert resp.status_code == 404

    def test_missing_reference_id_field_defaults_to_empty_string(self, client):
        # The frontend won't always have razorpay_payment_link_reference_id
        # in the URL (Razorpay omits it when no reference_id was set at link
        # creation) — the request body must tolerate the field being absent.
        neg_id = "neg-no-reference-id"
        link_id = asyncio.run(_seed_accepted_negotiation(neg_id, payment_status="created"))
        sig = _sign(link_id, "", "paid", "pay_abc", "test_secret")

        resp = client.post(
            f"/api/negotiations/{neg_id}/verify-payment",
            json={
                "razorpay_payment_id": "pay_abc",
                "razorpay_payment_link_id": link_id,
                "razorpay_payment_link_status": "paid",
                "razorpay_signature": sig,
                # razorpay_payment_link_reference_id intentionally omitted
            },
        )

        assert resp.status_code == 200
        assert resp.json() == {"verified": True, "payment_status": "captured"}
