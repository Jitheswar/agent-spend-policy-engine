"""Policy engine: the gate agents must pass through before any money moves.

POST /spend is the only way an agent gets paid access to a resource-server
API. It checks policy (known agent, approved action, per-request limit,
daily cap) and only if every check passes does it call the x402 payment
loop (common.avm_client) against the resource server. A denial never
reaches the resource server at all -- there is no payment attempt to deny.

Every outcome, approved or denied, is written to the audit log
(policy_engine/storage.py) before the response goes out.
"""

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from common.avm_client import agent_address, build_paying_session
from policy_engine import storage

POLICY_PATH = os.path.join(os.path.dirname(__file__), "policy.json")
RESOURCE_SERVER_URL = os.getenv("RESOURCE_SERVER_URL", "http://127.0.0.1:4021")
EXPLORER_TX_URL = "https://lora.algokit.io/testnet/transaction/{}"

with open(POLICY_PATH) as f:
    POLICY = json.load(f)

storage.init_db()

app = FastAPI(title="Agent Spend Policy Engine")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# One paying session per agent, built lazily and reused (each holds a
# persistent requests.Session + registered signer for that agent's key).
_sessions: dict[str, tuple] = {}


def _session_for(agent_id: str):
    if agent_id not in _sessions:
        _sessions[agent_id] = build_paying_session(agent_id)
    return _sessions[agent_id]


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class SpendRequest(BaseModel):
    agent_id: str
    action: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/policy")
def get_policy():
    return POLICY


@app.get("/agents")
def list_agents():
    out = []
    for agent_id, cfg in POLICY["agents"].items():
        summary = storage.get_agent_summary(agent_id)
        try:
            address = agent_address(agent_id)
        except Exception:
            address = None
        out.append(
            {
                "agent_id": agent_id,
                "display_name": cfg["display_name"],
                "address": address,
                "allowed_actions": cfg["allowed_actions"],
                "per_request_limit_usd": cfg["per_request_limit_usd"],
                "daily_cap_usd": cfg["daily_cap_usd"],
                "daily_spend_usd": storage.get_daily_spend(agent_id, _today()),
                "total_spend_usd": summary["total_spend_usd"],
                "approved_count": summary["approved_count"],
                "denied_count": summary["denied_count"],
            }
        )
    return out


@app.get("/requests")
def list_requests(limit: int = 200):
    return storage.get_requests(limit=limit)


@app.post("/admin/reset")
def reset_audit_log():
    """Wipes the audit log only -- for demo rehearsals. No on-chain effect."""
    storage.reset()
    return {"status": "ok"}


def _deny(agent_id: str, action: str, amount_usd: float, reason: str) -> dict:
    row = storage.log_request(agent_id, action, amount_usd, "denied", reason)
    return {"decision": "denied", "reason": reason, "log": row}


@app.post("/spend")
def spend(req: SpendRequest):
    agent_id, action = req.agent_id, req.action

    # 1. Known agent?
    agent_cfg = POLICY["agents"].get(agent_id)
    if agent_cfg is None:
        return _deny(agent_id, action, 0.0, f"unknown agent '{agent_id}'")

    # 2. Known action?
    action_cfg = POLICY["actions"].get(action)
    if action_cfg is None:
        return _deny(agent_id, action, 0.0, f"unknown action '{action}'")

    amount_usd = action_cfg["price_usd"]

    # 3. Action approved for this agent?
    if action not in agent_cfg["allowed_actions"]:
        return _deny(
            agent_id, action, amount_usd,
            f"agent '{agent_id}' is not approved for action '{action}'",
        )

    # 4. Per-request spend limit
    if amount_usd > agent_cfg["per_request_limit_usd"]:
        return _deny(
            agent_id, action, amount_usd,
            f"${amount_usd:.2f} exceeds per-request limit "
            f"${agent_cfg['per_request_limit_usd']:.2f} for '{agent_id}'",
        )

    # 5. Daily cap
    spent_today = storage.get_daily_spend(agent_id, _today())
    if spent_today + amount_usd > agent_cfg["daily_cap_usd"]:
        return _deny(
            agent_id, action, amount_usd,
            f"${spent_today:.2f} spent today + ${amount_usd:.2f} would exceed "
            f"daily cap ${agent_cfg['daily_cap_usd']:.2f} for '{agent_id}'",
        )

    # Policy cleared -- only now does any payment attempt happen.
    session, address, x402_client = _session_for(agent_id)
    url = RESOURCE_SERVER_URL.rstrip("/") + action_cfg["resource_path"]

    try:
        response = session.get(url, timeout=30)
    except Exception as e:
        return _deny(agent_id, action, amount_usd, f"payment request failed: {e}")

    if not response.ok:
        reason = f"x402 payment did not settle (status {response.status_code}): {response.text[:200]}"
        row = storage.log_request(agent_id, action, amount_usd, "denied", reason)
        return {"decision": "denied", "reason": reason, "log": row}

    from x402.http import x402HTTPClientSync

    tx_id = None
    try:
        http_client = x402HTTPClientSync(x402_client)
        settle = http_client.get_payment_settle_response(lambda name: response.headers.get(name))
        settle_data = json.loads(settle.model_dump_json())
        tx_id = settle_data.get("transaction")
    except ValueError:
        pass

    explorer_url = EXPLORER_TX_URL.format(tx_id) if tx_id else None
    reason = f"approved: {action} within policy for '{agent_id}'"
    row = storage.log_request(agent_id, action, amount_usd, "approved", reason, tx_id, explorer_url)
    return {
        "decision": "approved",
        "reason": reason,
        "tx_id": tx_id,
        "explorer_url": explorer_url,
        "resource_data": response.json(),
        "log": row,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=4022)
