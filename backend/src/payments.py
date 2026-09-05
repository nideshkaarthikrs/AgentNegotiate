"""
Razorpay payment integration for AgentNegotiate.

Creates test-mode Orders and Payment Links when a deal is accepted.
Verifies webhook signatures (raw-body HMAC) and checkout signatures.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os

import razorpay

logger = logging.getLogger(__name__)

_client: razorpay.Client | None = None


def _get_client() -> razorpay.Client:
    """Lazy-initialise the Razorpay client."""
    global _client
    if _client is None:
        key_id = os.environ.get("RAZORPAY_KEY_ID", "")
        key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "")
        if not key_id or not key_secret:
            raise RuntimeError(
                "RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET must be set. "
                "Get test keys from https://dashboard.razorpay.com/app/keys"
            )
        _client = razorpay.Client(auth=(key_id, key_secret))
    return _client


def create_order(
    amount: float,
    currency: str = "INR",
    negotiation_id: str = "",
) -> dict:
    """
    Create a Razorpay Order.

    Parameters
    ----------
    amount : float
        Total order value in major currency unit (e.g. ₹100.50).
    currency : str
        ISO currency code (default: INR).
    negotiation_id : str
        Attached as a note for traceability.

    Returns
    -------
    dict
        Razorpay order object with 'id', 'amount', etc.
    """
    client = _get_client()
    # Razorpay expects amount in paise (smallest currency unit)
    amount_paise = int(round(amount * 100))

    order_data = {
        "amount": amount_paise,
        "currency": currency,
        "notes": {
            "negotiation_id": negotiation_id,
            "source": "agent_negotiate",
        },
    }

    order = client.order.create(data=order_data)
    logger.info(
        "Created Razorpay order %s for ₹%.2f (negotiation %s)",
        order["id"], amount, negotiation_id,
    )
    return order


def create_payment_link(
    amount: float,
    description: str = "AgentNegotiate Payment",
    negotiation_id: str = "",
    currency: str = "INR",
    callback_url: str = "",
) -> dict:
    """
    Create a Razorpay Payment Link.

    Parameters
    ----------
    amount : float
        Total in major currency unit.
    description : str
    negotiation_id : str
    currency : str
    callback_url : str
        URL Razorpay redirects to after payment.

    Returns
    -------
    dict
        Payment link object with 'short_url', etc.
    """
    client = _get_client()
    amount_paise = int(round(amount * 100))

    link_data = {
        "amount": amount_paise,
        "currency": currency,
        "description": description,
        "notes": {
            "negotiation_id": negotiation_id,
            "source": "agent_negotiate",
        },
    }
    if callback_url:
        link_data["callback_url"] = callback_url
        link_data["callback_method"] = "get"

    link = client.payment_link.create(data=link_data)
    logger.info(
        "Created payment link %s for ₹%.2f (negotiation %s)",
        link.get("short_url", "N/A"), amount, negotiation_id,
    )
    return link


def verify_checkout_signature(
    order_id: str,
    payment_id: str,
    signature: str,
) -> bool:
    """
    Verify a Razorpay **Checkout** callback signature.

    This is the ``order_id|payment_id`` formula, keyed with
    ``RAZORPAY_KEY_SECRET``.  It is *not* the webhook formula — see
    :func:`verify_webhook_signature`, which is what
    ``POST /api/webhooks/razorpay`` must use.

    Parameters
    ----------
    order_id : str
    payment_id : str
    signature : str

    Returns
    -------
    bool
        True if the signature is valid.
    """
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "")
    if not key_secret:
        logger.error("Cannot verify signature: RAZORPAY_KEY_SECRET not set")
        return False

    message = f"{order_id}|{payment_id}"
    expected = hmac.new(
        key_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    is_valid = hmac.compare_digest(expected, signature or "")
    if not is_valid:
        logger.warning("Invalid Razorpay checkout signature for order %s", order_id)
    return is_valid


def verify_payment_link_signature(
    payment_link_id: str,
    payment_link_reference_id: str,
    payment_link_status: str,
    payment_id: str,
    signature: str,
) -> bool:
    """
    Verify a Razorpay **Payment Link** redirect-callback signature.

    This is the signature Razorpay appends to ``callback_url`` as
    ``razorpay_signature`` when it redirects the browser back after a
    Payment Link checkout — a *different* formula from both
    :func:`verify_checkout_signature` (Checkout's ``order_id|payment_id``)
    and :func:`verify_webhook_signature` (server-to-server webhooks).

    It matters because the webhook path requires Razorpay's servers to be
    able to reach ours over the public internet — which they cannot in local
    development (``localhost``) without a tunnel. This redirect signature
    arrives via the user's own browser instead, so it is the only
    payment-confirmation signal available in that environment, and is worth
    trusting on its own merits (it's cryptographically signed) rather than
    treating the webhook as the sole source of truth.

    Formula, keyed with ``RAZORPAY_KEY_SECRET``
    (https://razorpay.com/docs/payments/payment-links/apis/, confirmed
    against the ``razorpay`` SDK's own ``Utility.verify_payment_link_signature``):

        payment_link_id + "|" + payment_link_reference_id + "|" +
        payment_link_status + "|" + payment_id

    ``payment_link_reference_id`` is Razorpay's empty string when no
    ``reference_id`` was set at link-creation time (we never set one in
    :func:`create_payment_link`) — pass ``""`` for it in that case.

    Parameters
    ----------
    payment_link_id : str
    payment_link_reference_id : str
    payment_link_status : str
    payment_id : str
    signature : str

    Returns
    -------
    bool
        True if the signature is valid.
    """
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "")
    if not key_secret:
        logger.error("Cannot verify payment link signature: RAZORPAY_KEY_SECRET not set")
        return False
    if not signature:
        return False

    message = f"{payment_link_id}|{payment_link_reference_id}|{payment_link_status}|{payment_id}"
    expected = hmac.new(
        key_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    is_valid = hmac.compare_digest(expected, signature)
    if not is_valid:
        logger.warning("Invalid Razorpay payment link signature for link %s", payment_link_id)
    return is_valid


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    """
    Verify a Razorpay **webhook** signature.

    Razorpay signs the *raw request body* with the webhook secret
    (``RAZORPAY_WEBHOOK_SECRET``, configured alongside the webhook URL in the
    dashboard) and sends the digest in the ``X-Razorpay-Signature`` header.
    The body must be hashed exactly as received — re-serialising the parsed
    JSON changes the bytes and the digest with them.

    Parameters
    ----------
    raw_body : bytes
        The unmodified request body.
    signature : str
        Value of the ``X-Razorpay-Signature`` header.

    Returns
    -------
    bool
        True only if the secret is configured and the digest matches.
    """
    secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
    if not secret:
        logger.error(
            "Cannot verify webhook: RAZORPAY_WEBHOOK_SECRET not set. "
            "Rejecting the request rather than trusting it."
        )
        return False
    if not signature:
        logger.warning("Webhook rejected: missing X-Razorpay-Signature header")
        return False

    expected = hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    is_valid = hmac.compare_digest(expected, signature)
    if not is_valid:
        logger.warning("Invalid Razorpay webhook signature")
    return is_valid
