"""Tests for Razorpay Payment Link redirect-signature verification.

This is the signature Razorpay appends to a Payment Link's ``callback_url``
after redirecting the browser back post-payment — distinct from the Checkout
``order_id|payment_id`` formula and the webhook's raw-body HMAC.  See
https://razorpay.com/docs/payments/payment-links/apis/ :

    signature = HMAC-SHA256(
        payment_link_id + "|" + payment_link_reference_id + "|" +
        payment_link_status + "|" + payment_id,
        key_secret,
    )

Confirmed against both Razorpay's docs and the installed ``razorpay`` SDK's
own ``Utility.verify_payment_link_signature`` source, which builds the same
``"{}|{}|{}|{}".format(payment_link_id, payment_link_reference_id,
payment_link_status, payment_id)`` payload.
"""

import hashlib
import hmac

from src import payments


def _sign(payment_link_id: str, reference_id: str, status: str, payment_id: str, secret: str) -> str:
    msg = f"{payment_link_id}|{reference_id}|{status}|{payment_id}"
    return hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()


class TestVerifyPaymentLinkSignature:
    def test_valid_signature_is_accepted(self, monkeypatch):
        monkeypatch.setenv("RAZORPAY_KEY_SECRET", "test_secret")
        sig = _sign("plink_123", "neg_abc", "paid", "pay_456", "test_secret")
        assert payments.verify_payment_link_signature(
            payment_link_id="plink_123",
            payment_link_reference_id="neg_abc",
            payment_link_status="paid",
            payment_id="pay_456",
            signature=sig,
        ) is True

    def test_missing_reference_id_defaults_to_empty_string(self, monkeypatch):
        # Razorpay omits razorpay_payment_link_reference_id entirely from the
        # callback when no reference_id was set at link-creation time — our
        # create_payment_link() never sets one, so this is the normal case.
        monkeypatch.setenv("RAZORPAY_KEY_SECRET", "test_secret")
        sig = _sign("plink_123", "", "paid", "pay_456", "test_secret")
        assert payments.verify_payment_link_signature(
            payment_link_id="plink_123",
            payment_link_reference_id="",
            payment_link_status="paid",
            payment_id="pay_456",
            signature=sig,
        ) is True

    def test_tampered_payment_id_is_rejected(self, monkeypatch):
        monkeypatch.setenv("RAZORPAY_KEY_SECRET", "test_secret")
        sig = _sign("plink_123", "", "paid", "pay_456", "test_secret")
        assert payments.verify_payment_link_signature(
            payment_link_id="plink_123",
            payment_link_reference_id="",
            payment_link_status="paid",
            payment_id="pay_999",
            signature=sig,
        ) is False

    def test_tampered_status_is_rejected(self, monkeypatch):
        # A signature minted for "created" must not verify against "paid" —
        # otherwise an unpaid redirect could be replayed as a paid one.
        monkeypatch.setenv("RAZORPAY_KEY_SECRET", "test_secret")
        sig = _sign("plink_123", "", "created", "pay_456", "test_secret")
        assert payments.verify_payment_link_signature(
            payment_link_id="plink_123",
            payment_link_reference_id="",
            payment_link_status="paid",
            payment_id="pay_456",
            signature=sig,
        ) is False

    def test_missing_key_secret_is_rejected(self, monkeypatch):
        monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
        assert payments.verify_payment_link_signature(
            payment_link_id="plink_123",
            payment_link_reference_id="",
            payment_link_status="paid",
            payment_id="pay_456",
            signature="anything",
        ) is False

    def test_empty_signature_is_rejected(self, monkeypatch):
        monkeypatch.setenv("RAZORPAY_KEY_SECRET", "test_secret")
        assert payments.verify_payment_link_signature(
            payment_link_id="plink_123",
            payment_link_reference_id="",
            payment_link_status="paid",
            payment_id="pay_456",
            signature="",
        ) is False
