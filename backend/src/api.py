"""
FastAPI application for AgentNegotiate.

Endpoints:
  POST   /api/negotiate              — Start a new negotiation
  GET    /api/negotiations            — List all negotiations
  GET    /api/negotiations/{id}       — Get negotiation state + rounds
  GET    /api/negotiations/{id}/audit — Get audit trail
  POST   /api/negotiations/{id}/pay   — Create Razorpay payment link
  POST   /api/webhooks/razorpay      — Razorpay payment webhook (signed)
  WS     /ws/negotiate/{id}          — Live negotiation events
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ValidationError, model_validator

from src.schemas import (
    AuditEventType,
    BuyerMandate,
    NegotiationStatus,
    SellerPolicy,
)
from src import audit
from src import database as db
from src import payments
from src.negotiation_runner import run_negotiation

# Load .env
load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# How many recent events to replay to a WebSocket client that connects late.
_EVENT_BUFFER_SIZE = 200


def _cors_origins() -> list[str]:
    """
    Allowed browser origins.

    ``allow_origins=["*"]`` together with ``allow_credentials=True`` is a
    combination browsers reject outright, so the origins must be explicit.
    """
    raw = os.environ.get(
        "CORS_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000",
    )
    return [o.strip() for o in raw.split(",") if o.strip()]


# ---------------------------------------------------------------------------
# Lifespan — init DB on startup, close pooled connections on shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    try:
        yield
    finally:
        await db.close_connections()


app = FastAPI(
    title="AgentNegotiate API",
    description="Two-agent B2B procurement negotiation with LangGraph, Groq, and Razorpay",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class NegotiateRequest(BaseModel):
    """Request body to start a new negotiation."""
    procurement_request: str = Field(
        default="",
        description="Free-text procurement request (optional — parsed by LLM)",
    )
    buyer_target_price: float = Field(..., gt=0)
    buyer_max_budget: float = Field(..., gt=0)
    buyer_quantity: int = Field(..., gt=0)
    buyer_max_rounds: int = Field(default=3, ge=1, le=10)
    buyer_expiry: Optional[datetime] = Field(
        default=None,
        description="UTC deadline after which the buyer's mandate is void",
    )

    seller_list_price: float = Field(..., gt=0)
    seller_floor_price: float = Field(..., gt=0)
    seller_quantity: int = Field(..., gt=0)
    seller_max_rounds: int = Field(default=3, ge=1, le=10)

    @model_validator(mode="after")
    def _check_coherent(self) -> "NegotiateRequest":
        """Reject configurations that make the hard constraints unsatisfiable."""
        max_unit = self.buyer_max_budget / self.buyer_quantity
        if self.buyer_target_price > max_unit + 1e-9:
            raise ValueError(
                f"buyer_target_price ({self.buyer_target_price}) exceeds the per-unit "
                f"budget cap buyer_max_budget/buyer_quantity ({max_unit:.4f})"
            )
        if self.seller_floor_price > self.seller_list_price + 1e-9:
            raise ValueError(
                f"seller_floor_price ({self.seller_floor_price}) exceeds "
                f"seller_list_price ({self.seller_list_price})"
            )
        if self.seller_quantity < self.buyer_quantity:
            raise ValueError(
                f"seller_quantity ({self.seller_quantity}) is less than "
                f"buyer_quantity ({self.buyer_quantity})"
            )
        return self


class PaymentRequest(BaseModel):
    """Request body to create a payment link."""
    callback_url: str = Field(default="", description="Redirect URL after payment")


class VerifyPaymentRequest(BaseModel):
    """
    The ``razorpay_*`` query params Razorpay appends to ``callback_url``
    when it redirects the browser back after a Payment Link checkout.
    """
    razorpay_payment_id: str
    razorpay_payment_link_id: str
    razorpay_payment_link_status: str
    razorpay_signature: str
    razorpay_payment_link_reference_id: str = Field(
        default="", description="Empty when no reference_id was set at link creation"
    )


# ---------------------------------------------------------------------------
# WebSocket connection manager
# ---------------------------------------------------------------------------

class ConnectionManager:
    """
    Tracks active WebSocket connections per negotiation.

    Also keeps a short replay buffer per negotiation: the client can only
    learn its ``negotiation_id`` from the POST response, but the run starts
    before that response is sent, so the earliest events (often all of them)
    land before anyone is listening.  Replaying the buffer on connect closes
    that race without any protocol change on the frontend.
    """

    def __init__(self):
        self.connections: dict[str, list[WebSocket]] = {}
        self.buffers: dict[str, list[dict]] = {}

    async def connect(self, negotiation_id: str, ws: WebSocket):
        await ws.accept()
        self.connections.setdefault(negotiation_id, []).append(ws)
        for event in list(self.buffers.get(negotiation_id, [])):
            try:
                await ws.send_json(event)
            except Exception:
                self.disconnect(negotiation_id, ws)
                return

    def disconnect(self, negotiation_id: str, ws: WebSocket):
        conns = self.connections.get(negotiation_id, [])
        if ws in conns:
            conns.remove(ws)
        if not conns:
            self.connections.pop(negotiation_id, None)

    async def broadcast(self, negotiation_id: str, data: dict):
        buf = self.buffers.setdefault(negotiation_id, [])
        buf.append(data)
        if len(buf) > _EVENT_BUFFER_SIZE:
            del buf[: len(buf) - _EVENT_BUFFER_SIZE]

        dead: list[WebSocket] = []
        for ws in list(self.connections.get(negotiation_id, [])):
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            logger.info("Evicting dead WebSocket for negotiation %s", negotiation_id)
            self.disconnect(negotiation_id, ws)

    def clear_buffer(self, negotiation_id: str) -> None:
        self.buffers.pop(negotiation_id, None)


manager = ConnectionManager()

# Track running negotiations to avoid duplicates, and hold a strong reference
# to each background task — a bare asyncio.create_task() result is only weakly
# referenced by the loop and can be garbage-collected mid-run.
_running_negotiations: set[str] = set()
_background_tasks: set[asyncio.Task] = set()


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.post("/api/negotiate")
async def start_negotiation(req: NegotiateRequest):
    """Start a new negotiation and run it in the background."""
    neg_id = str(uuid.uuid4())

    try:
        buyer_mandate = BuyerMandate(
            target_price=req.buyer_target_price,
            max_budget=req.buyer_max_budget,
            quantity=req.buyer_quantity,
            max_rounds=req.buyer_max_rounds,
            expiry=req.buyer_expiry,
        )
        seller_policy = SellerPolicy(
            list_price=req.seller_list_price,
            floor_price=req.seller_floor_price,
            quantity=req.seller_quantity,
            max_rounds=req.seller_max_rounds,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors())

    if neg_id in _running_negotiations:
        raise HTTPException(status_code=409, detail="Negotiation already running")

    async def _run():
        _running_negotiations.add(neg_id)
        try:
            async for event in run_negotiation(
                buyer_mandate=buyer_mandate,
                seller_policy=seller_policy,
                procurement_request=req.procurement_request,
                negotiation_id=neg_id,
            ):
                await manager.broadcast(neg_id, event.to_dict())
                # Small delay for animation on the frontend
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            await _fail_negotiation(neg_id, "cancelled")
            raise
        except Exception as exc:
            logger.exception("Negotiation %s failed: %s", neg_id, exc)
            # Never leave a row stuck in `active` with the UI spinning.
            await _fail_negotiation(neg_id, f"internal_error: {exc}")
        finally:
            _running_negotiations.discard(neg_id)

    task = asyncio.create_task(_run())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return {
        "negotiation_id": neg_id,
        "status": "started",
        "ws_url": f"/ws/negotiate/{neg_id}",
    }


async def _fail_negotiation(neg_id: str, reason: str) -> None:
    """Write a terminal status, audit the failure, and tell any listeners."""
    try:
        await db.set_status(neg_id, NegotiationStatus.REJECTED)
    except Exception:
        logger.exception("Could not persist failure status for %s", neg_id)
    try:
        await audit.append_audit_entry(
            neg_id, AuditEventType.BUYER_REJECT, data={"reason": reason},
        )
    except Exception:
        logger.exception("Could not append failure audit entry for %s", neg_id)

    await manager.broadcast(neg_id, {
        "event_type": "negotiation_completed",
        "round_number": 0,
        "role": "system",
        "decision": "rejected",
        "price": None,
        "quantity": None,
        "explanation": f"The negotiation stopped unexpectedly: {reason}",
        "status": "rejected",
    })


@app.get("/api/negotiations")
async def list_negotiations():
    """List all negotiations."""
    rows = await db.list_negotiations()
    return {"negotiations": rows}


@app.get("/api/negotiations/{negotiation_id}")
async def get_negotiation(negotiation_id: str):
    """Get a single negotiation with its rounds."""
    neg = await db.get_negotiation(negotiation_id)
    if neg is None:
        raise HTTPException(status_code=404, detail="Negotiation not found")

    rounds = await db.get_rounds(negotiation_id)

    # Parse JSON fields in rounds
    for r in rounds:
        for field in ("buyer_offer", "seller_offer", "buyer_decision", "seller_decision"):
            if r.get(field) and isinstance(r[field], str):
                try:
                    r[field] = json.loads(r[field])
                except json.JSONDecodeError:
                    pass

    # Parse JSON fields in negotiation
    for field in ("buyer_mandate", "seller_policy"):
        if neg.get(field) and isinstance(neg[field], str):
            try:
                neg[field] = json.loads(neg[field])
            except json.JSONDecodeError:
                pass

    return {"negotiation": neg, "rounds": rounds}


@app.get("/api/negotiations/{negotiation_id}/audit")
async def get_audit_trail(negotiation_id: str):
    """Get the audit trail and verify its integrity."""
    neg = await db.get_negotiation(negotiation_id)
    if neg is None:
        raise HTTPException(status_code=404, detail="Negotiation not found")

    is_valid, entries = await audit.verify_chain(negotiation_id)
    return {
        "negotiation_id": negotiation_id,
        "chain_valid": is_valid,
        "entries": entries,
    }


@app.post("/api/negotiations/{negotiation_id}/pay")
async def create_payment(negotiation_id: str, req: PaymentRequest):
    """Create a Razorpay payment link for an accepted negotiation."""
    neg = await db.get_negotiation(negotiation_id)
    if neg is None:
        raise HTTPException(status_code=404, detail="Negotiation not found")

    if neg["status"] != NegotiationStatus.ACCEPTED.value:
        raise HTTPException(status_code=400, detail="Negotiation is not in ACCEPTED state")

    final_price = neg.get("final_price")
    final_quantity = neg.get("final_quantity")
    if not final_price or not final_quantity:
        raise HTTPException(status_code=400, detail="Final price/quantity not set")

    total_amount = final_price * final_quantity

    try:
        order = payments.create_order(
            amount=total_amount,
            negotiation_id=negotiation_id,
        )
        link = payments.create_payment_link(
            amount=total_amount,
            description=f"AgentNegotiate - {negotiation_id[:8]}",
            negotiation_id=negotiation_id,
            callback_url=req.callback_url,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    # Touch only the payment columns — see database.set_payment_info.
    await db.set_payment_info(
        negotiation_id,
        razorpay_order_id=order["id"],
        razorpay_payment_link=link.get("short_url", ""),
        payment_status="created",
    )

    await audit.append_audit_entry(
        negotiation_id, AuditEventType.PAYMENT_CREATED,
        data={"order_id": order["id"], "amount": total_amount, "link": link.get("short_url", "")},
    )

    return {
        "order_id": order["id"],
        "payment_link": link.get("short_url", ""),
        "amount": total_amount,
    }


@app.post("/api/negotiations/{negotiation_id}/verify-payment")
async def verify_payment(negotiation_id: str, req: VerifyPaymentRequest):
    """
    Verify the redirect-callback signature Razorpay appends to the payment
    link's ``callback_url`` and, if valid and paid, mark the payment
    captured.

    This exists alongside (not instead of) ``POST /api/webhooks/razorpay``:
    the webhook requires Razorpay's servers to reach ours over the public
    internet, which they cannot do against ``localhost`` without a tunnel,
    so in local development it never fires and ``payment_status`` never
    becomes "captured". This redirect signature arrives via the user's own
    browser instead and is independently verifiable, so it works regardless
    of whether the webhook is reachable. Safe to call more than once — a
    negotiation already marked "captured" is left untouched.
    """
    neg = await db.get_negotiation(negotiation_id)
    if neg is None:
        raise HTTPException(status_code=404, detail="Negotiation not found")

    if not payments.verify_payment_link_signature(
        payment_link_id=req.razorpay_payment_link_id,
        payment_link_reference_id=req.razorpay_payment_link_reference_id,
        payment_link_status=req.razorpay_payment_link_status,
        payment_id=req.razorpay_payment_id,
        signature=req.razorpay_signature,
    ):
        raise HTTPException(status_code=400, detail="Invalid payment link signature")

    if req.razorpay_payment_link_status != "paid":
        return {"verified": True, "payment_status": neg.get("payment_status")}

    if neg.get("payment_status") == "captured":
        return {"verified": True, "payment_status": "captured"}

    await db.set_payment_status(negotiation_id, payment_status="captured")
    await audit.append_audit_entry(
        negotiation_id, AuditEventType.PAYMENT_SUCCESS,
        data={"payment_id": req.razorpay_payment_id, "via": "redirect_signature"},
    )
    await manager.broadcast(negotiation_id, {
        "event_type": "payment_success",
        "payment_id": req.razorpay_payment_id,
        "order_id": neg.get("razorpay_order_id", ""),
    })

    return {"verified": True, "payment_status": "captured"}


@app.post("/api/webhooks/razorpay")
async def razorpay_webhook(request: Request):
    """
    Handle Razorpay webhook events.

    The signature is verified against the **raw** request body with
    ``RAZORPAY_WEBHOOK_SECRET`` before anything is read from the payload;
    an unsigned or mis-signed request mutates nothing.
    """
    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    if not payments.verify_webhook_signature(raw_body, signature):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Malformed webhook body")

    event = body.get("event", "")
    payload = body.get("payload", {}) or {}
    payment_entity = (payload.get("payment", {}) or {}).get("entity", {}) or {}
    notes = payment_entity.get("notes", {}) or {}
    negotiation_id = notes.get("negotiation_id", "")

    if event == "payment.captured" and negotiation_id:
        order_id = payment_entity.get("order_id", "")
        payment_id = payment_entity.get("id", "")

        neg = await db.get_negotiation(negotiation_id)
        if neg:
            await db.set_payment_status(
                negotiation_id,
                payment_status="captured",
                razorpay_order_id=order_id or None,
            )
            await audit.append_audit_entry(
                negotiation_id, AuditEventType.PAYMENT_SUCCESS,
                data={"payment_id": payment_id, "order_id": order_id},
            )
            await manager.broadcast(negotiation_id, {
                "event_type": "payment_success",
                "payment_id": payment_id,
                "order_id": order_id,
            })

    elif event == "payment.failed" and negotiation_id:
        await db.set_payment_status(negotiation_id, payment_status="failed")
        await audit.append_audit_entry(
            negotiation_id, AuditEventType.PAYMENT_FAILED,
            data={"reason": payment_entity.get("error_description", "unknown")},
        )

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

@app.websocket("/ws/negotiate/{negotiation_id}")
async def websocket_negotiate(websocket: WebSocket, negotiation_id: str):
    """
    WebSocket endpoint for live negotiation events.

    On connect, any events already emitted for this negotiation are replayed
    so a client that connects after the run started still sees the full
    transcript.
    """
    await manager.connect(negotiation_id, websocket)
    try:
        # Keep the connection alive — client can send pings
        while True:
            data = await websocket.receive_text()
            # Echo back for keepalive
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(negotiation_id, websocket)
    except Exception:
        manager.disconnect(negotiation_id, websocket)
