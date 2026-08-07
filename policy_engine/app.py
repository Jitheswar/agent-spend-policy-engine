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

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from common import identity, policy_auth
from common.avm_client import agent_address, agent_secret_key_b64, build_paying_session, load_accounts
from policy_engine import policy_store, storage

RESOURCE_SERVER_URL = os.getenv("RESOURCE_SERVER_URL", "http://127.0.0.1:4021")
EXPLORER_TX_URL = "https://lora.algokit.io/testnet/transaction/{}"

storage.init_db()

app = FastAPI(title="Agent Spend Policy Engine")
app.add_middleware(
    CORSMiddleware,
    # Only the dashboard's own origin needs cross-origin access. This is
    # local-only/testnet, not a hard security boundary, but there's no
    # reason to let an arbitrary third-party page POST /spend or
    # /admin/reset against a browser that happens to have this open.
    allow_origins=[
        "http://127.0.0.1:4023",
        "http://localhost:4023",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
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
    # Deliberately just a length cap, not a charset restriction -- unknown
    # agent_id/action values are a legitimate, exercised path (the "unknown
    # agent" denial). Keeping arbitrary strings out of storage.log_request
    # small is enough; rendering safety is the dashboard's job (escapeHtml).
    agent_id: str = Field(max_length=128)
    action: str = Field(max_length=128)
    # Optional: the amount the agent is requesting authorization for. When
    # omitted, falls back to the action's configured price (unchanged
    # behavior for agents/simulate.py and any other caller). When given, it
    # drives the per-request-limit and daily-cap checks -- letting a caller
    # (the dashboard's amount input, in particular) demonstrate the
    # per-request-limit denial path, which no fixed action price in
    # policy.json actually triggers on its own. The real x402 payment, if
    # approved, still settles at the resource server's real fixed price --
    # that's dictated by the protocol, not the client -- so this models
    # "authorize up to $X," not "pay exactly $X."
    amount_usd: float | None = Field(default=None, ge=0, le=1000)
    # Cryptographic proof this request actually came from someone who
    # controls agent_id's Algorand private key (see common/identity.py).
    # Required on every call -- there is no unsigned path to /spend.
    # Signed over the RESOLVED amount (amount_usd if given, else the
    # action's configured price), so the caller must know that value
    # up front -- see POST /admin/sign for how the dashboard gets it.
    timestamp: float
    nonce: str = Field(max_length=32)
    signature: str = Field(max_length=256)


class SignRequest(BaseModel):
    agent_id: str = Field(max_length=128)
    action: str = Field(max_length=128)
    amount_usd: float = Field(ge=0, le=1000)


@app.post("/admin/sign")
def admin_sign(req: SignRequest):
    """Convenience signing for the dashboard's human-operated console --
    explicitly NOT something a real external agent would ever call. A
    genuine agent process holds its own key and signs locally, exactly
    like agents/simulate.py does; this exists only because the dashboard
    runs in a browser, which correctly never has access to private key
    material, and this demo's keys are already held custodially by this
    same process for payment-signing purposes regardless (see
    common/avm_client.py). Signing here proves nothing about the browser
    operator -- it proves the SAME thing agents/simulate.py's local
    signing proves, just performed on the caller's behalf as a demo
    convenience.
    """
    accounts = load_accounts()
    if req.agent_id not in accounts:
        raise HTTPException(404, f"unknown agent '{req.agent_id}'")
    secret_key_b64 = agent_secret_key_b64(req.agent_id, accounts)
    return identity.sign_request(secret_key_b64, req.agent_id, req.action, req.amount_usd)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/policy")
def get_policy():
    return policy_store.get_policy()


class AgentPolicyUpdate(BaseModel):
    per_request_limit_usd: float | None = Field(default=None, ge=0, le=1000)
    daily_cap_usd: float | None = Field(default=None, ge=0, le=1000)
    allowed_actions: list[str] | None = None


@app.patch("/admin/agents/{agent_id}")
def update_agent_policy(agent_id: str, update: AgentPolicyUpdate):
    """Live policy edit -- takes effect immediately, no restart, and
    persists to policy.json so it survives one too."""
    fields = {k: v for k, v in update.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "no fields to update")
    try:
        updated = policy_store.update_agent(agent_id, **fields)
    except KeyError:
        raise HTTPException(404, f"unknown agent '{agent_id}'")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"agent_id": agent_id, **updated}


@app.get("/agents")
def list_agents():
    policy = policy_store.get_policy()
    out = []
    for agent_id, cfg in policy["agents"].items():
        summary = storage.get_agent_summary(agent_id)
        try:
            address = agent_address(agent_id)
        except (KeyError, OSError, ValueError):
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
    # One snapshot for the whole request -- policy.json can change between
    # requests (that's the point, see policy_store), but a single /spend
    # call should be evaluated against one consistent view of it rather
    # than possibly re-reading mid-request if a hot-reload lands in between.
    policy = policy_store.get_policy()

    # 1. Known agent?
    agent_cfg = policy["agents"].get(agent_id)
    if agent_cfg is None:
        return _deny(agent_id, action, 0.0, f"unknown agent '{agent_id}'")

    # 2. Known action?
    action_cfg = policy["actions"].get(action)
    if action_cfg is None:
        return _deny(agent_id, action, 0.0, f"unknown action '{action}'")

    amount_usd = req.amount_usd if req.amount_usd is not None else action_cfg["price_usd"]

    # 2.5 Cryptographic identity: does this request actually come from
    # someone who controls agent_id's own Algorand private key? Verified
    # against the exact (agent_id, action, amount_usd) triple being acted
    # on, so a validly-signed request for one action/amount can't be
    # replayed to authorize a different one.
    try:
        claimed_address = agent_address(agent_id)
    except (KeyError, OSError, ValueError):
        return _deny(agent_id, action, amount_usd, f"no known signing key on file for '{agent_id}'")

    sig_ok, sig_reason = identity.verify_request(
        claimed_address, agent_id, action, amount_usd, req.timestamp, req.nonce, req.signature
    )
    if not sig_ok:
        return _deny(agent_id, action, amount_usd, f"identity verification failed: {sig_reason}")

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

    # 5. Daily cap -- atomically checked and reserved in one transaction, so
    # two concurrent /spend calls for the same agent (double-click, two
    # tabs, autoplay overlapping a manual fire) can't both read the same
    # "spent so far" and both slip under the cap. See storage.try_reserve.
    reservation = storage.try_reserve(agent_id, action, amount_usd, agent_cfg["daily_cap_usd"], _today())
    if reservation is None:
        spent_today = storage.get_daily_spend(agent_id, _today())
        return _deny(
            agent_id, action, amount_usd,
            f"${spent_today:.2f} spent today + ${amount_usd:.2f} would exceed "
            f"daily cap ${agent_cfg['daily_cap_usd']:.2f} for '{agent_id}'",
        )

    # Policy cleared and budget reserved -- only now does any payment attempt
    # happen. From here down, every exit path MUST call storage.finalize on
    # this reservation: a 'pending' row that never resolves counts toward
    # the daily cap forever (see CAP_COUNTING_DECISIONS), with no cleanup
    # short of wiping the whole audit log. A broken agent (bad key, etc.)
    # would otherwise silently burn its entire cap through 500s that never
    # show up as "denied" anywhere.
    try:
        session, address, x402_client = _session_for(agent_id)
    except Exception as e:
        reason = f"failed to prepare payment session: {e}"
        row = storage.finalize(reservation["id"], "denied", reason)
        return {"decision": "denied", "reason": reason, "log": row}

    url = RESOURCE_SERVER_URL.rstrip("/") + action_cfg["resource_path"]
    # Proves to the resource server that THIS specific spend cleared policy
    # (see common/policy_auth.py) -- without it, the resource server has no
    # idea this engine exists and would sell the same API call to anyone
    # who pays for it directly. Passed per-call (not session.headers,
    # which would race: _sessions caches one session per agent and FastAPI
    # runs sync handlers in a threadpool, so two concurrent /spend calls
    # for the same agent could clobber each other's token on a shared
    # mutable session header).
    auth_headers = {policy_auth.HEADER_NAME: policy_auth.mint_token(agent_id, action)}

    try:
        # 60s, not 30s: the full round trip here is policy engine -> resource
        # server -> facilitator -> Algorand -> back, and the public testnet
        # facilitator/algod endpoints have been observed taking well over
        # 30s combined under real (if degraded) network conditions -- see
        # README "Known limitations" #3. This doesn't make that dependency
        # any less real, it just stops treating "slow" the same as "broken."
        response = session.get(url, headers=auth_headers, timeout=60)
    except Exception as e:
        reason = f"payment request failed: {e}"
        row = storage.finalize(reservation["id"], "denied", reason)
        return {"decision": "denied", "reason": reason, "log": row}

    if not response.ok:
        reason = f"x402 payment did not settle (status {response.status_code}): {response.text[:200]}"
        row = storage.finalize(reservation["id"], "denied", reason)
        return {"decision": "denied", "reason": reason, "log": row}

    # response.ok means the resource server already ran verify()+settle()
    # against the facilitator before returning 200 -- money has moved on
    # Algorand regardless of anything below. Nothing past this line may
    # finalize the reservation as "denied": that would misrepresent a real
    # spend as a no-op and incorrectly free its budget back onto the daily
    # cap. Worst case below is losing the tx_id/explorer_url for display;
    # the spend itself is never in question once we're here.
    from x402.http import x402HTTPClientSync

    tx_id = None
    explorer_url = None
    reason = f"approved: {action} within policy for '{agent_id}'"
    try:
        http_client = x402HTTPClientSync(x402_client)
        settle = http_client.get_payment_settle_response(lambda name: response.headers.get(name))
        settle_data = json.loads(settle.model_dump_json())
        tx_id = settle_data.get("transaction")
        explorer_url = EXPLORER_TX_URL.format(tx_id) if tx_id else None
    except Exception as e:
        reason = (
            f"approved: {action} within policy for '{agent_id}' "
            f"(settlement details unavailable: {e})"
        )

    row = storage.finalize(reservation["id"], "approved", reason, tx_id, explorer_url)

    try:
        resource_data = response.json()
    except Exception:
        resource_data = None

    return {
        "decision": "approved",
        "reason": reason,
        "tx_id": tx_id,
        "explorer_url": explorer_url,
        "resource_data": resource_data,
        "log": row,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=4022)
