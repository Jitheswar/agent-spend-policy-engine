"""Policy engine: the gate agents must pass through before any money moves.

POST /spend is the only way an agent gets paid access to a resource-server
API. It runs an ordered chain of checks -- known agent, known action,
cryptographic identity, kill switch, approved action, per-request limit,
request velocity, daily cap, human-approval threshold -- and only if every
one passes does it call the x402 payment loop (common.avm_client) against
the resource server. A denial never reaches the resource server at all;
there is no payment attempt to deny.

Every outcome, approved or denied, is written twice: to the mutable
operational log the dashboard reads, and to an append-only hash-chained
audit ledger whose head is periodically notarized on Algorand (see
policy_engine/storage.py and common/anchor.py). The second one is what lets
anybody -- including someone who doesn't trust whoever runs this process --
verify after the fact that the record wasn't edited.
"""

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from common import anchor, identity, policy_auth
from common.avm_client import agent_address, agent_secret_key_b64, build_paying_session, load_accounts
from policy_engine import policy_store, storage

RESOURCE_SERVER_URL = os.getenv("RESOURCE_SERVER_URL", "http://127.0.0.1:4021")
EXPLORER_TX_URL = "https://lora.algokit.io/testnet/transaction/{}"

VELOCITY_WINDOW_SECONDS = 60

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
    allow_methods=["GET", "POST", "PATCH"],
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
    # drives the per-request-limit, approval-threshold and daily-cap checks
    # -- letting a caller (the dashboard's amount input, in particular)
    # demonstrate denial and approval-hold paths that no fixed action price
    # in policy.json actually triggers on its own. The real x402 payment, if
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
    frozen: bool | None = None
    max_requests_per_minute: int | None = Field(default=None, ge=0, le=10000)
    require_approval_above_usd: float | None = Field(default=None, ge=0, le=1000)


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
    storage.append_event("policy.updated", {"agent_id": agent_id, "fields": fields})
    return {"agent_id": agent_id, **updated}


class FreezeRequest(BaseModel):
    frozen: bool


@app.post("/admin/agents/{agent_id}/freeze")
def freeze_agent(agent_id: str, req: FreezeRequest):
    """The kill switch: stop an agent spending anything, immediately.

    Every other control here is a budget -- it bounds how much damage a
    misbehaving agent does before someone notices. This is the control for
    after someone notices. It takes effect on the very next request, needs
    no restart, and persists to policy.json, and both directions land in
    the audit ledger, because "who unfroze it, and when" matters as much as
    the freeze.
    """
    try:
        updated = policy_store.set_frozen(agent_id, req.frozen)
    except KeyError:
        raise HTTPException(404, f"unknown agent '{agent_id}'")
    storage.append_event("agent.frozen" if req.frozen else "agent.unfrozen", {"agent_id": agent_id})
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
                "frozen": cfg.get("frozen", False),
                "max_requests_per_minute": cfg.get("max_requests_per_minute"),
                "require_approval_above_usd": cfg.get("require_approval_above_usd"),
                "recent_requests": storage.count_recent_requests(agent_id, VELOCITY_WINDOW_SECONDS),
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


@app.get("/approvals")
def list_approvals():
    """Spends parked for a human decision. These already hold budget
    against the daily cap (see storage.try_reserve) -- a request waiting on
    approval must not let a later one slip through on money it's already
    claimed."""
    return storage.get_pending_approvals()


@app.post("/admin/reset")
def reset_audit_log():
    """Wipes the operational request log only -- for demo rehearsals.

    No on-chain effect, and deliberately NO effect on the append-only audit
    ledger: the wipe itself is appended to that ledger as an event, and
    every anchor covering the cleared period still verifies. An audit trail
    that a "reset" button can quietly erase would not be one.
    """
    storage.reset()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Audit ledger + on-chain notarization
# ---------------------------------------------------------------------------


@app.get("/audit/events")
def audit_events(limit: int = 100):
    return storage.get_audit_events(limit=limit)


@app.get("/audit/verify")
def audit_verify(onchain: bool = True, anchors: int = 5):
    """Recompute the whole hash chain and check it against Algorand.

    `onchain=false` skips the public-indexer round trip entirely, and
    `anchors` bounds how many of them get checked. Both exist for the
    dashboard, which polls this often: verifying five anchors against a
    public indexer every few seconds is a lot of traffic aimed at someone
    else's free endpoint to re-learn something that can't have changed.
    The full check runs on demand and on a slower timer.
    """
    return anchor.integrity_report(check_onchain=onchain, anchor_limit=max(1, min(anchors, 50)))


@app.post("/audit/anchor")
def audit_anchor():
    """Notarize the current chain head on Algorand testnet, synchronously."""
    return anchor.anchor_now()


@app.get("/audit/anchors")
def audit_anchors(limit: int = 20):
    return storage.get_anchors(limit=limit)


class TamperRequest(BaseModel):
    seq: int | None = None
    field: str = Field(default="amount_usd", max_length=64)
    # Left unset by the dashboard so the server can pick something that is
    # actually different from what's already there. A fixed default here
    # (it used to be "0.01") silently no-ops whenever the record already
    # holds that value: the payload is unchanged, so the hash is unchanged,
    # so verification correctly reports no tampering -- and the demo looks
    # like the detection failed when in fact nothing was edited.
    value: str | None = Field(default=None, max_length=128)


@app.post("/admin/demo/tamper")
def demo_tamper(req: TamperRequest):
    """DEMO ONLY: doctor a historical audit event, the way a malicious
    operator with database access would.

    This exists to make the tamper-evidence claim checkable instead of just
    stated. It rewrites a past event's payload directly in SQLite AND
    recomputes that event's own hash to match -- i.e. it simulates someone
    competent, not someone careless. Verification still catches it, because
    the following event's prev_hash no longer lines up; and an attacker who
    goes further and recomputes the entire chain forward is still caught by
    the on-chain anchor, whose hash lives in a block they cannot edit.

    Call GET /audit/verify afterwards to see exactly which seq broke.
    """
    events = storage.get_audit_events(limit=50)
    seq = req.seq

    if seq is None:
        candidates = [e for e in events if e["event_type"] == "request.decided"]
        if not candidates:
            raise HTTPException(400, "no request events to tamper with yet -- fire a spend first")

        # Prefer a record that verification can actually catch. Editing the
        # chain's LAST event is undetectable by hashing alone (nothing
        # follows it to disagree, and its own hash gets recomputed -- see
        # storage.verify_chain), so an anchored record is the first choice
        # and any non-head record the second. Falling through to the head
        # only happens when it's the sole candidate, and then the honest
        # answer really is "not yet detectable, it isn't anchored".
        latest_anchor = storage.get_latest_anchor()
        anchored_seq = latest_anchor["seq"] if latest_anchor else 0
        head = storage.get_chain_head()
        head_seq = head["seq"] if head else 0

        pool = (
            [e for e in candidates if e["seq"] <= anchored_seq]
            or [e for e in candidates if e["seq"] < head_seq]
            or candidates
        )
        # Priciest of the pool: an operator hiding a spend hides the
        # expensive one, which also makes the demo legible.
        seq = max(pool, key=lambda e: json.loads(e["payload"]).get("amount_usd") or 0)["seq"]

    current = None
    for event in events:
        if event["seq"] == seq:
            current = json.loads(event["payload"]).get(req.field)
            break

    if req.value is not None:
        try:
            parsed_value: object = json.loads(req.value)
        except (ValueError, TypeError):
            parsed_value = req.value  # keep it as a plain string
    elif isinstance(current, (int, float)) and not isinstance(current, bool):
        # Shrink the amount, and guarantee the new value differs from the
        # old one -- writing back an identical value changes no bytes and
        # therefore no hashes, which would make a working tamper detector
        # look broken.
        parsed_value = 0.01 if abs(float(current) - 0.01) > 1e-9 else 0.001
    else:
        parsed_value = "approved" if current != "approved" else "denied"

    try:
        result = storage.tamper_with_event(seq, {req.field: parsed_value})
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"status": "tampered", **result, "next": "GET /audit/verify"}


@app.post("/admin/demo/restore")
def demo_restore():
    """Undo every demo tamper, so the break/detect/restore loop can be run
    again. Restores the exact original bytes, not a recomputation."""
    return {"status": "restored", **storage.restore_tampered()}


# ---------------------------------------------------------------------------
# Spend decisions
# ---------------------------------------------------------------------------


def _deny(agent_id: str, action: str, amount_usd: float, reason: str, code: str) -> dict:
    row = storage.log_request(agent_id, action, amount_usd, "denied", reason)
    anchor.maybe_auto_anchor()
    return {"decision": "denied", "reason": reason, "code": code, "log": row}


def _execute_payment(agent_id: str, action: str, action_cfg: dict, reservation_id: int) -> dict:
    """Runs the x402 payment loop for a reservation that has cleared policy.

    Shared by /spend (the straight-through path) and the approval-hold
    release path, which must behave identically once a human has said yes --
    same payment, same settlement parsing, same finalize semantics. Every
    exit path here MUST finalize the reservation: a row left 'pending'
    counts toward the daily cap forever (see CAP_COUNTING_DECISIONS), with
    no cleanup short of wiping the log. A broken agent would otherwise
    silently burn its entire cap through 500s that never show up as
    "denied" anywhere.
    """
    try:
        session, address, x402_client = _session_for(agent_id)
    except Exception as e:
        reason = f"failed to prepare payment session: {e}"
        row = storage.finalize(reservation_id, "denied", reason)
        return {"decision": "denied", "reason": reason, "code": "payment_setup_failed", "log": row}

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
        row = storage.finalize(reservation_id, "denied", reason)
        return {"decision": "denied", "reason": reason, "code": "payment_failed", "log": row}

    if not response.ok:
        reason = f"x402 payment did not settle (status {response.status_code}): {response.text[:200]}"
        row = storage.finalize(reservation_id, "denied", reason)
        return {"decision": "denied", "reason": reason, "code": "settlement_failed", "log": row}

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

    row = storage.finalize(reservation_id, "approved", reason, tx_id, explorer_url)

    try:
        resource_data = response.json()
    except Exception:
        resource_data = None

    return {
        "decision": "approved",
        "reason": reason,
        "code": "approved",
        "tx_id": tx_id,
        "explorer_url": explorer_url,
        "resource_data": resource_data,
        "log": row,
    }


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
        return _deny(agent_id, action, 0.0, f"unknown agent '{agent_id}'", "unknown_agent")

    # 2. Known action?
    action_cfg = policy["actions"].get(action)
    if action_cfg is None:
        return _deny(agent_id, action, 0.0, f"unknown action '{action}'", "unknown_action")

    amount_usd = req.amount_usd if req.amount_usd is not None else action_cfg["price_usd"]

    # 3. Cryptographic identity: does this request actually come from
    # someone who controls agent_id's own Algorand private key? Verified
    # against the exact (agent_id, action, amount_usd) triple being acted
    # on, so a validly-signed request for one action/amount can't be
    # replayed to authorize a different one. Runs before every policy check
    # below it: those checks are all statements about a specific agent, and
    # they mean nothing until the caller has proven to be that agent.
    try:
        claimed_address = agent_address(agent_id)
    except (KeyError, OSError, ValueError):
        return _deny(
            agent_id, action, amount_usd,
            f"no known signing key on file for '{agent_id}'", "no_signing_key",
        )

    sig_ok, sig_reason = identity.verify_request(
        claimed_address, agent_id, action, amount_usd, req.timestamp, req.nonce, req.signature
    )
    if not sig_ok:
        return _deny(
            agent_id, action, amount_usd,
            f"identity verification failed: {sig_reason}", "identity_failed",
        )

    # 4. Kill switch. Checked immediately after identity and before every
    # budget rule, because freezing an agent has to mean "stops spending
    # now", not "stops spending once its remaining budget runs out."
    if agent_cfg.get("frozen"):
        return _deny(
            agent_id, action, amount_usd,
            f"agent '{agent_id}' is frozen -- kill switch engaged by an operator", "frozen",
        )

    # 5. Action approved for this agent?
    if action not in agent_cfg["allowed_actions"]:
        return _deny(
            agent_id, action, amount_usd,
            f"agent '{agent_id}' is not approved for action '{action}'", "action_not_allowed",
        )

    # 6. Per-request spend limit
    if amount_usd > agent_cfg["per_request_limit_usd"]:
        return _deny(
            agent_id, action, amount_usd,
            f"${amount_usd:.2f} exceeds per-request limit "
            f"${agent_cfg['per_request_limit_usd']:.2f} for '{agent_id}'",
            "over_per_request_limit",
        )

    # 7. Velocity. A daily cap bounds total damage but says nothing about
    # rate: an agent stuck in a retry loop can burn a whole day's budget in
    # seconds, and every one of those calls is individually within policy.
    # This is the check that catches "correct but runaway", which is the
    # failure mode an autonomous agent actually has.
    rpm_limit = agent_cfg.get("max_requests_per_minute")
    if rpm_limit is not None:
        recent = storage.count_recent_requests(agent_id, VELOCITY_WINDOW_SECONDS)
        if recent >= rpm_limit:
            return _deny(
                agent_id, action, amount_usd,
                f"velocity limit hit: {recent} requests in the last {VELOCITY_WINDOW_SECONDS}s "
                f"(max {rpm_limit}/min for '{agent_id}')",
                "over_velocity_limit",
            )

    # 8. Human-in-the-loop threshold. Above this, policy alone isn't allowed
    # to authorize the spend -- it gets parked holding its budget until a
    # person releases or rejects it. Deliberately evaluated BEFORE the cap
    # reservation returns to the caller, so a parked request can't be
    # double-spent against by a later one arriving while it waits.
    approval_threshold = agent_cfg.get("require_approval_above_usd")
    needs_approval = approval_threshold is not None and amount_usd > approval_threshold

    # 9. Daily cap -- atomically checked and reserved in one transaction, so
    # two concurrent /spend calls for the same agent (double-click, two
    # tabs, autoplay overlapping a manual fire) can't both read the same
    # "spent so far" and both slip under the cap. See storage.try_reserve.
    reservation = storage.try_reserve(
        agent_id,
        action,
        amount_usd,
        agent_cfg["daily_cap_usd"],
        decision="awaiting_approval" if needs_approval else "pending",
    )
    if reservation is None:
        spent_today = storage.get_daily_spend(agent_id, _today())
        return _deny(
            agent_id, action, amount_usd,
            f"${spent_today:.2f} spent today + ${amount_usd:.2f} would exceed "
            f"daily cap ${agent_cfg['daily_cap_usd']:.2f} for '{agent_id}'",
            "over_daily_cap",
        )

    if needs_approval:
        reason = (
            f"${amount_usd:.2f} is above '{agent_id}'s ${approval_threshold:.2f} "
            "auto-approval threshold -- held for human review"
        )
        row = storage.finalize(reservation["id"], "awaiting_approval", reason)
        anchor.maybe_auto_anchor()
        return {
            "decision": "awaiting_approval",
            "reason": reason,
            "code": "awaiting_approval",
            "request_id": reservation["id"],
            "log": row,
        }

    result = _execute_payment(agent_id, action, action_cfg, reservation["id"])
    anchor.maybe_auto_anchor()
    return result


@app.post("/admin/holds/{request_id}/approve")
def approve_hold(request_id: int):
    """A human releases a parked spend. The payment runs only now."""
    row = storage.get_request(request_id)
    if row is None:
        raise HTTPException(404, f"no request {request_id}")
    if row["decision"] != "awaiting_approval":
        raise HTTPException(409, f"request {request_id} is '{row['decision']}', not awaiting approval")

    policy = policy_store.get_policy()
    action_cfg = policy["actions"].get(row["action"])
    if action_cfg is None:
        reason = f"action '{row['action']}' no longer exists in policy"
        return {"decision": "denied", "reason": reason, "log": storage.finalize(request_id, "denied", reason)}

    # Re-check the kill switch at release time, not just at request time. A
    # hold can sit for as long as the reviewer takes, and an agent frozen
    # in the meantime must not be able to spend through a request that was
    # already in the queue when the freeze landed.
    agent_cfg = policy["agents"].get(row["agent_id"], {})
    if agent_cfg.get("frozen"):
        reason = f"agent '{row['agent_id']}' was frozen while this request was awaiting approval"
        storage.append_event("approval.blocked", {"request_id": request_id, "reason": reason})
        return {"decision": "denied", "reason": reason, "log": storage.finalize(request_id, "denied", reason)}

    storage.append_event("approval.granted", {"request_id": request_id, "agent_id": row["agent_id"]})
    result = _execute_payment(row["agent_id"], row["action"], action_cfg, request_id)
    anchor.maybe_auto_anchor()
    return result


@app.post("/admin/holds/{request_id}/reject")
def reject_hold(request_id: int):
    row = storage.get_request(request_id)
    if row is None:
        raise HTTPException(404, f"no request {request_id}")
    if row["decision"] != "awaiting_approval":
        raise HTTPException(409, f"request {request_id} is '{row['decision']}', not awaiting approval")

    reason = "rejected by human reviewer"
    storage.append_event("approval.rejected", {"request_id": request_id, "agent_id": row["agent_id"]})
    # finalize to 'denied' also releases the budget this hold was reserving.
    result_row = storage.finalize(request_id, "denied", reason)
    anchor.maybe_auto_anchor()
    return {"decision": "denied", "reason": reason, "log": result_row}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=4022)
