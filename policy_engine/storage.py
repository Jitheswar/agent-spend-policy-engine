"""SQLite-backed audit trail for the policy engine.

One table, one job: every spend request an agent makes gets a row, approved
or denied, before or after the payment attempt. This is the audit trail the
dashboard reads from.
"""

import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "policy_engine.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    action TEXT NOT NULL,
    amount_usd REAL NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    tx_id TEXT,
    explorer_url TEXT
);
"""


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(SCHEMA)


def log_request(
    agent_id: str,
    action: str,
    amount_usd: float,
    decision: str,
    reason: str,
    tx_id: str | None = None,
    explorer_url: str | None = None,
) -> dict:
    ts = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO requests (timestamp, agent_id, action, amount_usd, decision, reason, tx_id, explorer_url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, agent_id, action, amount_usd, decision, reason, tx_id, explorer_url),
        )
        row = conn.execute("SELECT * FROM requests WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)


def get_requests(limit: int = 200) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM requests ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_daily_spend(agent_id: str, day: str) -> float:
    """day is an ISO date string, e.g. '2026-08-07' (UTC)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount_usd), 0) AS total FROM requests "
            "WHERE agent_id = ? AND decision = 'approved' AND substr(timestamp, 1, 10) = ?",
            (agent_id, day),
        ).fetchone()
        return float(row["total"])


def reset() -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM requests")


def get_agent_summary(agent_id: str) -> dict:
    with _connect() as conn:
        row = conn.execute(
            "SELECT "
            "  COALESCE(SUM(CASE WHEN decision = 'approved' THEN amount_usd ELSE 0 END), 0) AS total_spend_usd, "
            "  COUNT(CASE WHEN decision = 'approved' THEN 1 END) AS approved_count, "
            "  COUNT(CASE WHEN decision = 'denied' THEN 1 END) AS denied_count "
            "FROM requests WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        return dict(row)
