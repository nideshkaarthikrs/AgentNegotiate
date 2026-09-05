"""
SQLite database layer for AgentNegotiate.

Uses aiosqlite for async operations.  Stores:
  • negotiations — high-level session metadata
  • rounds       — per-round offer / decision history
  • audit_log    — tamper-evident hash-chained event log
"""

from __future__ import annotations

import asyncio
import json
import os
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

from src.schemas import (
    AuditEntry,
    AuditEventType,
    NegotiationRound,
    NegotiationState,
    NegotiationStatus,
    Offer,
    PolicyDecision,
)

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DATABASE_PATH", "agent_negotiate.db")

# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS negotiations (
    negotiation_id   TEXT PRIMARY KEY,
    status           TEXT NOT NULL DEFAULT 'active',
    current_round    INTEGER NOT NULL DEFAULT 1,
    buyer_mandate    TEXT NOT NULL,        -- JSON
    seller_policy    TEXT NOT NULL,        -- JSON
    final_price      REAL,
    final_quantity   INTEGER,
    procurement_request TEXT DEFAULT '',
    razorpay_order_id   TEXT,
    razorpay_payment_link TEXT,
    payment_status   TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rounds (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    negotiation_id   TEXT NOT NULL,
    round_number     INTEGER NOT NULL,
    buyer_offer      TEXT,                -- JSON
    seller_offer     TEXT,                -- JSON
    buyer_decision   TEXT,                -- JSON
    seller_decision  TEXT,                -- JSON
    buyer_explanation  TEXT DEFAULT '',
    seller_explanation TEXT DEFAULT '',
    FOREIGN KEY (negotiation_id) REFERENCES negotiations(negotiation_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    negotiation_id   TEXT NOT NULL,
    event_type       TEXT NOT NULL,
    data             TEXT NOT NULL DEFAULT '{}',
    timestamp        TEXT NOT NULL,
    prev_hash        TEXT NOT NULL DEFAULT 'GENESIS',
    hash             TEXT NOT NULL,
    FOREIGN KEY (negotiation_id) REFERENCES negotiations(negotiation_id)
);

CREATE INDEX IF NOT EXISTS idx_audit_log_negotiation
    ON audit_log (negotiation_id, id);

CREATE INDEX IF NOT EXISTS idx_rounds_negotiation
    ON rounds (negotiation_id, round_number);
"""


# ---------------------------------------------------------------------------
# Connection management
#
# A fresh connection per call plus SQLite's default rollback journal produced
# "database is locked" as soon as two negotiations ran concurrently.  We keep
# one connection per (path, event loop) with WAL and a busy timeout instead.
# aiosqlite serialises all work on a connection through a single thread, so
# the shared connection also serialises writes for free.
# ---------------------------------------------------------------------------

_BUSY_TIMEOUT_MS = 5000

_connections: dict[tuple[str, int], aiosqlite.Connection] = {}
_conn_lock = asyncio.Lock()


async def _open(path: str) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.commit()
    return conn


@asynccontextmanager
async def _connect(db_path: str | None = None):
    """Yield the shared connection for *db_path* on the running event loop."""
    path = db_path or DB_PATH
    key = (path, id(asyncio.get_running_loop()))

    conn = _connections.get(key)
    if conn is None:
        async with _conn_lock:
            conn = _connections.get(key)
            if conn is None:
                conn = await _open(path)
                _connections[key] = conn
    yield conn


async def close_connections() -> None:
    """Close every pooled connection (call from the FastAPI lifespan shutdown)."""
    for key, conn in list(_connections.items()):
        try:
            await conn.close()
        except Exception:  # pragma: no cover - best effort on shutdown
            logger.warning("Failed to close DB connection for %s", key[0])
        _connections.pop(key, None)


async def init_db(db_path: str | None = None) -> None:
    """Create tables if they don't exist."""
    path = db_path or DB_PATH
    async with _connect(path) as db:
        await db.executescript(_SCHEMA_SQL)
        await db.commit()
    logger.info("Database initialised at %s", path)


# ---------------------------------------------------------------------------
# Negotiation CRUD
# ---------------------------------------------------------------------------

async def create_negotiation(state: NegotiationState, db_path: str | None = None) -> None:
    """Insert a new negotiation row."""
    path = db_path or DB_PATH
    async with _connect(path) as db:
        await db.execute(
            """INSERT INTO negotiations
               (negotiation_id, status, current_round, buyer_mandate, seller_policy,
                procurement_request, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                state.negotiation_id,
                state.status.value,
                state.current_round,
                state.buyer_mandate.model_dump_json(),
                state.seller_policy.model_dump_json(),
                state.procurement_request,
                state.created_at.isoformat(),
                state.updated_at.isoformat(),
            ),
        )
        await db.commit()


async def update_negotiation(state: NegotiationState, db_path: str | None = None) -> None:
    """Update an existing negotiation row."""
    path = db_path or DB_PATH
    async with _connect(path) as db:
        await db.execute(
            """UPDATE negotiations
               SET status = ?, current_round = ?, final_price = ?, final_quantity = ?,
                   razorpay_order_id = ?, razorpay_payment_link = ?, payment_status = ?,
                   updated_at = ?
               WHERE negotiation_id = ?""",
            (
                state.status.value,
                state.current_round,
                state.final_price,
                state.final_quantity,
                state.razorpay_order_id,
                state.razorpay_payment_link,
                state.payment_status,
                datetime.now(timezone.utc).isoformat(),
                state.negotiation_id,
            ),
        )
        await db.commit()


async def set_payment_info(
    negotiation_id: str,
    razorpay_order_id: str,
    razorpay_payment_link: str,
    payment_status: str,
    db_path: str | None = None,
) -> None:
    """
    Write *only* the payment columns.

    The payment paths used to rebuild a whole NegotiationState and push it
    through ``update_negotiation``, which reset ``current_round`` to 1 and
    blanked any column the rebuilt state did not carry.
    """
    path = db_path or DB_PATH
    async with _connect(path) as db:
        await db.execute(
            """UPDATE negotiations
               SET razorpay_order_id = ?, razorpay_payment_link = ?,
                   payment_status = ?, updated_at = ?
               WHERE negotiation_id = ?""",
            (
                razorpay_order_id,
                razorpay_payment_link,
                payment_status,
                datetime.now(timezone.utc).isoformat(),
                negotiation_id,
            ),
        )
        await db.commit()


async def set_payment_status(
    negotiation_id: str,
    payment_status: str,
    razorpay_order_id: str | None = None,
    db_path: str | None = None,
) -> None:
    """Write only ``payment_status`` (and the order id when supplied)."""
    path = db_path or DB_PATH
    async with _connect(path) as db:
        await db.execute(
            """UPDATE negotiations
               SET payment_status = ?,
                   razorpay_order_id = COALESCE(?, razorpay_order_id),
                   updated_at = ?
               WHERE negotiation_id = ?""",
            (
                payment_status,
                razorpay_order_id or None,
                datetime.now(timezone.utc).isoformat(),
                negotiation_id,
            ),
        )
        await db.commit()


async def set_status(
    negotiation_id: str,
    status: NegotiationStatus | str,
    db_path: str | None = None,
) -> None:
    """Write only the lifecycle status — used to close out a crashed run."""
    path = db_path or DB_PATH
    value = status.value if isinstance(status, NegotiationStatus) else str(status)
    async with _connect(path) as db:
        await db.execute(
            "UPDATE negotiations SET status = ?, updated_at = ? WHERE negotiation_id = ?",
            (value, datetime.now(timezone.utc).isoformat(), negotiation_id),
        )
        await db.commit()


async def get_negotiation(negotiation_id: str, db_path: str | None = None) -> Optional[dict]:
    """Fetch a negotiation by ID. Returns raw dict or None."""
    path = db_path or DB_PATH
    async with _connect(path) as db:
        cursor = await db.execute(
            "SELECT * FROM negotiations WHERE negotiation_id = ?",
            (negotiation_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)


async def list_negotiations(db_path: str | None = None) -> list[dict]:
    """Return all negotiations, most recent first."""
    path = db_path or DB_PATH
    async with _connect(path) as db:
        cursor = await db.execute(
            "SELECT * FROM negotiations ORDER BY created_at DESC"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Rounds CRUD
# ---------------------------------------------------------------------------

async def save_round(
    negotiation_id: str,
    round_number: int,
    buyer_offer: Offer | None = None,
    seller_offer: Offer | None = None,
    buyer_decision: PolicyDecision | None = None,
    seller_decision: PolicyDecision | None = None,
    buyer_explanation: str = "",
    seller_explanation: str = "",
    db_path: str | None = None,
) -> None:
    """Insert or update a round row."""
    path = db_path or DB_PATH
    async with _connect(path) as db:
        # Check if round already exists
        cursor = await db.execute(
            "SELECT id FROM rounds WHERE negotiation_id = ? AND round_number = ?",
            (negotiation_id, round_number),
        )
        existing = await cursor.fetchone()

        if existing:
            await db.execute(
                """UPDATE rounds SET
                   buyer_offer = COALESCE(?, buyer_offer),
                   seller_offer = COALESCE(?, seller_offer),
                   buyer_decision = COALESCE(?, buyer_decision),
                   seller_decision = COALESCE(?, seller_decision),
                   buyer_explanation = CASE WHEN ? != '' THEN ? ELSE buyer_explanation END,
                   seller_explanation = CASE WHEN ? != '' THEN ? ELSE seller_explanation END
                   WHERE negotiation_id = ? AND round_number = ?""",
                (
                    buyer_offer.model_dump_json() if buyer_offer else None,
                    seller_offer.model_dump_json() if seller_offer else None,
                    buyer_decision.model_dump_json() if buyer_decision else None,
                    seller_decision.model_dump_json() if seller_decision else None,
                    buyer_explanation, buyer_explanation,
                    seller_explanation, seller_explanation,
                    negotiation_id, round_number,
                ),
            )
        else:
            await db.execute(
                """INSERT INTO rounds
                   (negotiation_id, round_number, buyer_offer, seller_offer,
                    buyer_decision, seller_decision, buyer_explanation, seller_explanation)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    negotiation_id, round_number,
                    buyer_offer.model_dump_json() if buyer_offer else None,
                    seller_offer.model_dump_json() if seller_offer else None,
                    buyer_decision.model_dump_json() if buyer_decision else None,
                    seller_decision.model_dump_json() if seller_decision else None,
                    buyer_explanation,
                    seller_explanation,
                ),
            )
        await db.commit()


async def get_rounds(negotiation_id: str, db_path: str | None = None) -> list[dict]:
    """Fetch all rounds for a negotiation, ordered by round number."""
    path = db_path or DB_PATH
    async with _connect(path) as db:
        cursor = await db.execute(
            "SELECT * FROM rounds WHERE negotiation_id = ? ORDER BY round_number",
            (negotiation_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Audit log CRUD
# ---------------------------------------------------------------------------

async def save_audit_entry(entry: AuditEntry, db_path: str | None = None) -> int:
    """Insert an audit entry and return its row id."""
    path = db_path or DB_PATH
    async with _connect(path) as db:
        cursor = await db.execute(
            """INSERT INTO audit_log
               (negotiation_id, event_type, data, timestamp, prev_hash, hash)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                entry.negotiation_id,
                entry.event_type.value,
                entry.data,
                entry.timestamp.isoformat(),
                entry.prev_hash,
                entry.hash,
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def get_last_audit_entry(negotiation_id: str, db_path: str | None = None) -> Optional[dict]:
    """
    Fetch only the most recent audit row.

    ``append_audit_entry`` needs just the previous hash; re-reading the whole
    chain on every append made logging O(n²) in the number of events.
    """
    path = db_path or DB_PATH
    async with _connect(path) as db:
        cursor = await db.execute(
            "SELECT * FROM audit_log WHERE negotiation_id = ? ORDER BY id DESC LIMIT 1",
            (negotiation_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_audit_log(negotiation_id: str, db_path: str | None = None) -> list[dict]:
    """Fetch the full audit log for a negotiation, ordered by id."""
    path = db_path or DB_PATH
    async with _connect(path) as db:
        cursor = await db.execute(
            "SELECT * FROM audit_log WHERE negotiation_id = ? ORDER BY id",
            (negotiation_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
