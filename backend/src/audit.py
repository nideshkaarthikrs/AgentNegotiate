"""
Hash-chained audit trail for tamper-evident negotiation logging.

Every event is recorded with:
  {event_type, data, timestamp, prev_hash, hash}

where hash = SHA-256(prev_hash ‖ event_type ‖ data ‖ timestamp)

This guarantees that if any entry is modified or deleted, all
subsequent hashes become invalid — detectable by verify_chain().
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

# pyrefly: ignore [missing-import]
from src.schemas import AuditEntry, AuditEventType
# pyrefly: ignore [missing-import]
from src import database as db

logger = logging.getLogger(__name__)

GENESIS_HASH = "GENESIS"


# ---------------------------------------------------------------------------
# Hash computation
# ---------------------------------------------------------------------------

def compute_hash(prev_hash: str, event_type: str, data: str, timestamp: str) -> str:
    """Compute SHA-256 hash of the concatenated fields."""
    payload = f"{prev_hash}|{event_type}|{data}|{timestamp}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Append an audit entry
# ---------------------------------------------------------------------------

async def append_audit_entry(
    negotiation_id: str,
    event_type: AuditEventType,
    data: dict | str = "",
    db_path: str | None = None,
) -> AuditEntry:
    """
    Create and persist a new audit entry, chaining it to the previous one.

    Parameters
    ----------
    negotiation_id : str
    event_type : AuditEventType
    data : dict | str
        Event payload (will be JSON-serialised if dict).
    db_path : str | None
        Override database path (for testing).

    Returns
    -------
    AuditEntry
    """
    # Serialise data
    if isinstance(data, dict):
        data_str = json.dumps(data, default=str, sort_keys=True)
    else:
        data_str = str(data)

    # Fetch only the last entry to get prev_hash (O(1) instead of O(n))
    last = await db.get_last_audit_entry(negotiation_id, db_path=db_path)
    prev_hash = last["hash"] if last else GENESIS_HASH

    timestamp = datetime.now(timezone.utc)
    entry_hash = compute_hash(
        prev_hash=prev_hash,
        event_type=event_type.value,
        data=data_str,
        timestamp=timestamp.isoformat(),
    )

    entry = AuditEntry(
        negotiation_id=negotiation_id,
        event_type=event_type,
        data=data_str,
        timestamp=timestamp,
        prev_hash=prev_hash,
        hash=entry_hash,
    )

    row_id = await db.save_audit_entry(entry, db_path=db_path)
    entry.id = row_id
    logger.info(
        "Audit [%s] %s  hash=%s…",
        negotiation_id[:8], event_type.value, entry_hash[:12],
    )
    return entry


# ---------------------------------------------------------------------------
# Verify the chain
# ---------------------------------------------------------------------------

async def verify_chain(negotiation_id: str, db_path: str | None = None) -> tuple[bool, list[dict]]:
    """
    Validate the integrity of the audit chain for a negotiation.

    Returns
    -------
    (is_valid, entries)
        is_valid : bool — True if every hash is correct.
        entries  : list[dict] — the raw audit rows, each augmented with
                   a ``"valid"`` boolean field.
    """
    rows = await db.get_audit_log(negotiation_id, db_path=db_path)
    if not rows:
        return True, []

    all_valid = True
    prev_hash = GENESIS_HASH

    for row in rows:
        expected = compute_hash(
            prev_hash=row["prev_hash"],
            event_type=row["event_type"],
            data=row["data"],
            timestamp=row["timestamp"],
        )
        row["valid"] = (row["hash"] == expected and row["prev_hash"] == prev_hash)
        if not row["valid"]:
            all_valid = False
            logger.warning(
                "Audit chain broken at id=%s for negotiation %s",
                row["id"], negotiation_id,
            )
        prev_hash = row["hash"]

    return all_valid, rows
