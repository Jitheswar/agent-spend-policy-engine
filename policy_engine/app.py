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
import secrets
import sys
from datetime import datetime, timezone
from urllib.parse import urlencode

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from common import anchor, config, identity, policy_auth, provisioning
from common.avm_client import agent_address, agent_secret_key_b64, build_paying_session, load_accounts
from policy_engine import policy_store, storage

RESOURCE_SERVER_URL = config.RESOURCE_SERVER_URL

VELOCITY_WINDOW_SECONDS = config.VELOCITY_WINDOW_SECONDS

storage.init_db()

app = FastAPI(title="Agent Spend Policy Engine")

# Endpoints that exist only to make the system demonstrable, and that hand
# out capabilities no production deployment should: /admin/sign will sign a
# /spend request as any agent (see admin_sign), and /admin/demo/* doctors
# audit records. Gated together by ALLOW_DEMO_ENDPOINTS.
DEMO_PREFIXES = ("/admin/sign", "/admin/demo/")

AUTH_HEADER = "Authorization"


class AdminAuthMiddleware(BaseHTTPMiddleware):
    """Requires a bearer token on every /admin route.

    This is the control the whole product rests on. Everything under
    /admin is an authority over money -- freeze, unfreeze, onboard, release
    a held spend, edit a cap -- and for a system whose pitch is "a person
    decides what the agents may do", leaving that reachable by anything
    that can open a socket to this port made the pitch untrue.

    Deliberately a middleware rather than a per-route dependency: routes get
    added, and a dependency you forget to attach fails open. A prefix match
    here covers every route that exists now and every one added later.

    The token is resolved per request (config.admin_token()) rather than
    captured at import, so a test can flip it without reloading the app --
    same reasoning as anchor.anchoring_disabled().
    """

    async def dispatch(self, request, call_next):
        path = request.url.path
        if not path.startswith("/admin"):
            return await call_next(request)

        if any(path.startswith(prefix) for prefix in DEMO_PREFIXES) and not config.ALLOW_DEMO_ENDPOINTS:
            # 404 rather than 403: an endpoint that is switched off should
            # look absent, not merely locked.
            return JSONResponse(
                {
                    "error": "not enabled",
                    "detail": (
                        "demo-only endpoints are disabled on this deployment "
                        "(set ALLOW_DEMO_ENDPOINTS=true to enable them)"
                    ),
                },
                status_code=404,
            )

        # CORS preflight carries no Authorization header by definition; it
        # has to reach the CORS layer to be answered at all.
        if request.method == "OPTIONS":
            return await call_next(request)

        expected = config.admin_token()
        if not expected:
            return await call_next(request)

        supplied = request.headers.get(AUTH_HEADER, "")
        scheme, _, presented = supplied.partition(" ")
        # Compared as BYTES. secrets.compare_digest refuses non-ASCII str and
        # raises TypeError, and Starlette decodes header bytes as latin-1 --
        # so `Authorization: Bearer tökén` over a raw socket turned an
        # unauthenticated request into a 500 with a traceback instead of a
        # 401. A wrong credential is a wrong credential; it is never a bug
        # report. (httpx can't even send that header, so it takes a raw
        # socket to see it -- which is exactly the caller who would try.)
        if scheme.lower() != "bearer" or not secrets.compare_digest(
            presented.strip().encode("utf-8", "surrogateescape"), expected.encode("utf-8")
        ):
            return JSONResponse(
                {
                    "error": "admin authorization required",
                    "detail": (
                        "every /admin route needs 'Authorization: Bearer <ADMIN_TOKEN>'. "
                        f"The token for this deployment is in {config.ADMIN_TOKEN_PATH} "
                        "unless ADMIN_TOKEN was set in the environment."
                    ),
                },
                status_code=401,
            )
        return await call_next(request)


app.add_middleware(AdminAuthMiddleware)

# Added AFTER the auth middleware, which makes it the OUTER layer (Starlette
# runs add_middleware() registrations last-added-first). That ordering is
# load-bearing twice over: a preflight OPTIONS is answered here instead of
# being rejected by auth for carrying no credentials, and a 401 still comes
# back with CORS headers, so the browser reports "unauthorized" rather than
# an opaque network error that sends you looking in the wrong place.
app.add_middleware(
    CORSMiddleware,
    # Only the dashboard's own origin needs cross-origin access. This is
    # local-only/testnet, not a hard security boundary, but there's no
    # reason to let an arbitrary third-party page POST /spend or
    # /admin/reset against a browser that happens to have this open.
    allow_origins=config.DASHBOARD_ORIGINS,
    # DELETE is here for deregistering an agent. It's easy to add an endpoint
    # and forget this list -- the browser then fails the preflight and the
    # button does nothing, while curl against the same route works fine,
    # which sends you looking in entirely the wrong place.
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", AUTH_HEADER],
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
    # The arguments the call is actually for -- which city, which company.
    # Validated against the action's declared param schema in policy.json
    # (see _resolve_params) and forwarded to the resource server as the
    # query string. Omitted keys fall back to the schema's default, which
    # is what lets the dashboard's one-click Fire buttons work without
    # asking an operator to type a city every time.
    params: dict[str, str] | None = None
    # Cryptographic proof this request actually came from someone who
    # controls agent_id's Algorand private key (see common/identity.py).
    # Required on every call -- there is no unsigned path to /spend.
    # Signed over the RESOLVED amount (amount_usd if given, else the
    # action's configured price) and over params as supplied, so the caller
    # must know both up front -- see POST /admin/sign for how the dashboard
    # gets them.
    timestamp: float
    nonce: str = Field(max_length=32)
    signature: str = Field(max_length=256)

    @field_validator("params")
    @classmethod
    def _bounded_params(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return _check_param_bounds(value)


def _check_param_bounds(value: dict[str, str] | None) -> dict[str, str] | None:
    """Hard ceiling on call arguments, distinct from the policy check.

    policy.json's per-action `max_length` is the governance control -- an
    operator deciding what a sanctioned call may look like. This is the
    anti-abuse floor underneath it, and it exists because of where rejected
    requests go rather than what they mean.

    /spend is deliberately open to unauthenticated callers: anyone may
    *attempt* a spend, and the signature decides whether it succeeds. Every
    attempt, including a denial raised before validation, is written into an
    append-only ledger with no delete path -- that is the point of the
    ledger. Without a ceiling here, a single unauthenticated request could
    commit an arbitrary number of attacker-chosen bytes to it permanently,
    and drive auto-anchoring (and its transaction fees) while doing so.
    """
    if value is None:
        return None
    if len(value) > config.MAX_PARAM_KEYS:
        raise ValueError(f"at most {config.MAX_PARAM_KEYS} parameters are accepted")
    for key, item in value.items():
        if len(key) > config.MAX_PARAM_KEY_LENGTH:
            raise ValueError(f"parameter names are limited to {config.MAX_PARAM_KEY_LENGTH} characters")
        if len(item) > config.MAX_PARAM_VALUE_LENGTH:
            raise ValueError(
                f"parameter '{key[:32]}' exceeds the {config.MAX_PARAM_VALUE_LENGTH}-character ceiling"
            )
    return value


class SignRequest(BaseModel):
    agent_id: str = Field(max_length=128)
    action: str = Field(max_length=128)
    amount_usd: float = Field(ge=0, le=1000)
    params: dict[str, str] | None = None

    @field_validator("params")
    @classmethod
    def _bounded_params(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return _check_param_bounds(value)


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

    Be precise about what this route is, because it is the hinge the
    identity guarantee turns on: it is a signing oracle. It will sign for
    ANY agent. While it is reachable, "only a caller who holds an agent's
    private key can spend as that agent" is true only of callers who also
    lack the admin token -- which is exactly why it sits behind both
    AdminAuthMiddleware and ALLOW_DEMO_ENDPOINTS, and why the second of
    those defaults off on mainnet.
    """
    accounts = load_accounts()
    if req.agent_id not in accounts:
        raise HTTPException(404, f"unknown agent '{req.agent_id}'")
    secret_key_b64 = agent_secret_key_b64(req.agent_id, accounts)
    return identity.sign_request(
        secret_key_b64, req.agent_id, req.action, req.amount_usd, req.params
    )


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


def _reject_unknown_actions(actions: list[str] | None) -> None:
    """Both routes that accept an allowed_actions list run this.

    POST /admin/agents validated it and PATCH didn't, which is the kind of
    asymmetry that looks harmless until you notice what it lets through: an
    arbitrary attacker-chosen string written straight into policy.json, and
    from there into the dashboard's markup. An action nothing can serve is
    also just wrong on its own terms -- it grants an authority that cannot
    be exercised and reads, in the audit log, as if it could.
    """
    if not actions:
        return
    known = policy_store.get_policy()["actions"]
    unknown = [a for a in actions if a not in known]
    if unknown:
        raise HTTPException(400, f"unknown action(s): {', '.join(unknown)}")


@app.patch("/admin/agents/{agent_id}")
def update_agent_policy(agent_id: str, update: AgentPolicyUpdate):
    """Live policy edit -- takes effect immediately, no restart, and
    persists to policy.json so it survives one too."""
    fields = {k: v for k, v in update.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "no fields to update")
    _reject_unknown_actions(fields.get("allowed_actions"))
    try:
        updated = policy_store.update_agent(agent_id, **fields)
    except KeyError:
        raise HTTPException(404, f"unknown agent '{agent_id}'")
    except ValueError as e:
        raise HTTPException(400, str(e))
    storage.append_event("policy.updated", {"agent_id": agent_id, "fields": fields})
    return {"agent_id": agent_id, **updated}


class NewAgentRequest(BaseModel):
    agent_id: str = Field(max_length=64, pattern=r"^[a-z0-9_]+$")
    display_name: str = Field(max_length=64)
    allowed_actions: list[str] = Field(default_factory=list)
    per_request_limit_usd: float = Field(default=0.05, ge=0, le=1000)
    daily_cap_usd: float = Field(default=0.50, ge=0, le=1000)
    max_requests_per_minute: int | None = Field(default=15, ge=0, le=10000)
    require_approval_above_usd: float | None = Field(default=None, ge=0, le=1000)
    fund: bool = True


@app.post("/admin/agents")
def create_agent(req: NewAgentRequest):
    """Onboard a new agent while the system is running -- no restart, no
    config file editing, no redeploy.

    Three things have to happen for a new agent to be real, and they happen
    in this order for a reason:

      1. It gets an Algorand keypair (common/provisioning.ensure_account).
         Without its own key it cannot sign a /spend request, so the
         identity check at the top of /spend would reject it -- there is no
         path by which a keyless agent spends anything.
      2. It's registered in policy with caps. This is what makes it
         *governed*; it happens before any funding, so there is never a
         window where an agent can pay for something no policy covers.
      3. Its account is funded and opted into USDC on-chain, in the
         background, so the response is immediate and the dashboard can
         show it arriving.

    Note what step 3 means for honesty about state: between the response and
    the funding landing, the agent is registered and policy-governed but
    cannot yet settle a payment. That's reported as
    `provisioning.state == "funding"` on GET /agents rather than papered
    over -- an agent that looks ready and then fails to pay is a worse
    demo, and a worse system, than one that says what it's doing.
    """
    _reject_unknown_actions(req.allowed_actions)

    try:
        address, created = provisioning.ensure_account(req.agent_id)
    except OSError as e:
        raise HTTPException(500, f"could not create account: {e}")

    try:
        cfg = policy_store.add_agent(
            req.agent_id,
            req.display_name,
            allowed_actions=req.allowed_actions,
            per_request_limit_usd=req.per_request_limit_usd,
            daily_cap_usd=req.daily_cap_usd,
            max_requests_per_minute=req.max_requests_per_minute,
            require_approval_above_usd=req.require_approval_above_usd,
        )
    except ValueError as e:
        raise HTTPException(409, str(e))

    storage.append_event(
        "agent.registered",
        {
            "agent_id": req.agent_id,
            "display_name": req.display_name,
            "address": address,
            "new_account": created,
            "allowed_actions": req.allowed_actions,
            "per_request_limit_usd": req.per_request_limit_usd,
            "daily_cap_usd": req.daily_cap_usd,
        },
    )

    if req.fund:
        provisioning.provision_async(
            req.agent_id,
            on_done=lambda agent_id, result, err: storage.append_event(
                "agent.provisioned" if err is None else "agent.provision_failed",
                {"agent_id": agent_id, **({"error": str(err)} if err else result)},
            ),
        )

    anchor.maybe_auto_anchor()
    return {
        "agent_id": req.agent_id,
        "address": address,
        "new_account": created,
        "provisioning": provisioning.get_status(req.agent_id),
        **cfg,
    }


@app.delete("/admin/agents/{agent_id}")
def delete_agent(agent_id: str):
    """Deregister an agent. Keeps its key and its audit history -- see
    policy_store.remove_agent for why neither is deleted."""
    try:
        policy_store.remove_agent(agent_id)
    except KeyError:
        raise HTTPException(404, f"unknown agent '{agent_id}'")
    storage.append_event("agent.deregistered", {"agent_id": agent_id})
    return {"agent_id": agent_id, "removed": True}


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
                # Present only for agents onboarded during this process's
                # lifetime -- an agent still being funded is registered and
                # governed but cannot settle yet, and the dashboard says so
                # rather than letting an operator fire a doomed spend.
                "provisioning": provisioning.get_status(agent_id),
                "recent_requests": storage.count_recent_requests(agent_id, VELOCITY_WINDOW_SECONDS),
                "daily_spend_usd": storage.get_daily_spend(agent_id, _today()),
                "total_spend_usd": summary["total_spend_usd"],
                "approved_count": summary["approved_count"],
                "denied_count": summary["denied_count"],
            }
        )
    return out


@app.get("/requests")
def list_requests(limit: int = Query(200, ge=1, le=1000)):
    # Bounded, because SQLite reads a negative LIMIT as "no limit": ?limit=-1
    # used to return the entire table, and an unbounded one serialises it to
    # JSON in memory. Same reasoning as the clamp on /audit/verify's anchors.
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
def audit_events(limit: int = Query(100, ge=1, le=1000)):
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
def audit_anchors(limit: int = Query(20, ge=1, le=1000)):
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


def _deny(agent_id: str, action: str, amount_usd: float, reason: str, code: str,
          params: dict | None = None) -> dict:
    """Record a denial.

    `params` is the arguments the call was for. Denials raised before the
    schema is resolved (unknown agent, bad signature, frozen, action not
    allowed) pass the caller's raw params rather than nothing: what an agent
    *attempted* is part of the record, and "agent_rogue tried to enrich
    Tesla and was refused" is the sentence an operator needs, not
    "agent_rogue was refused."
    """
    row = storage.log_request(agent_id, action, amount_usd, "denied", reason, params=params)
    anchor.maybe_auto_anchor()
    return {"decision": "denied", "reason": reason, "code": code, "log": row}


def _resolve_params(action_cfg: dict, supplied: dict | None) -> tuple[dict, str | None]:
    """Validate call arguments against the action's declared schema and fill
    in defaults. Returns (resolved, error) -- error is None when valid.

    This is a governance control, not input sanitation. Now that the paid
    APIs take arguments, "how much may this agent spend" stops being the
    whole question: an agent with a perfectly ordinary budget can still call
    an action with arguments nobody sanctioned. Declaring the accepted
    params per action in policy.json means the operator decides the shape of
    the call, not just its price, and an unknown key is a denial rather than
    something quietly forwarded to the upstream.

    Defaults come from policy (operator-controlled), never from the caller,
    so filling one in after signature verification can't be used to smuggle
    a value past the signature -- the agent has no influence over it.
    """
    schema = action_cfg.get("params") or {}
    supplied = supplied or {}

    unknown = set(supplied) - set(schema)
    if unknown:
        return {}, f"unknown parameter(s) for this action: {', '.join(sorted(unknown))}"

    resolved: dict[str, str] = {}
    for name, spec in schema.items():
        value = supplied.get(name)
        if value is None or value == "":
            value = spec.get("default")
        if value is None or value == "":
            if spec.get("required"):
                return {}, f"missing required parameter '{name}'"
            continue
        value = str(value)
        max_length = spec.get("max_length")
        if max_length is not None and len(value) > max_length:
            return {}, f"parameter '{name}' exceeds {max_length} characters"
        resolved[name] = value

    return resolved, None


def _classify_failure(status_code: int, body: str) -> tuple[str, str]:
    """Turn a non-2xx from the resource server into (code, description).

    Worth separating because these have completely different operators and
    completely different fixes. A 402 means the payment leg broke -- the
    facilitator, the chain, the agent's balance. A 5xx carrying an upstream
    code means the vendor behind the paywall is down, which is nothing to do
    with payments at all. Reporting both as "settlement failed" (which this
    did before the APIs were real, when the only route to a non-2xx *was* a
    payment problem) sends whoever is on call to the wrong system.

    None of these cost the agent anything: the x402 middleware settles only
    after a handler returns under 400, so every status here means no
    payment was taken.
    """
    detail = ""
    upstream_code = ""
    try:
        parsed = json.loads(body).get("detail")
        if isinstance(parsed, dict):
            upstream_code = parsed.get("code", "")
            detail = parsed.get("message", "")
        elif isinstance(parsed, str):
            detail = parsed
    except (ValueError, AttributeError):
        detail = body[:200]

    if status_code == 402:
        return "settlement_failed", f"x402 payment did not settle: {detail or body[:200]}"
    if status_code == 403:
        return "policy_auth_failed", f"resource server rejected this engine's authorization: {detail}"
    if status_code == 404:
        return "upstream_not_found", detail or "the upstream has no record matching that request"
    if 400 <= status_code < 500:
        return "bad_request", detail or f"resource server rejected the request ({status_code})"
    return (
        upstream_code or "upstream_unavailable",
        detail or f"the paid API's upstream is unavailable ({status_code})",
    )


def _execute_payment(agent_id: str, action: str, action_cfg: dict, reservation_id: int,
                     params: dict | None = None) -> dict:
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
    # Query string baked into the URL rather than passed as requests'
    # `params=`. The x402 client re-issues this request after paying the
    # 402, and building the URL here means the arguments are part of the URL
    # being retried instead of depending on the retry path to carry a
    # separate params dict through. A paid call that silently lost its
    # arguments would charge the agent and return the wrong city.
    if params:
        url += "?" + urlencode(params)
    # Proves to the resource server that THIS specific spend cleared policy
    # (see common/policy_auth.py) -- without it, the resource server has no
    # idea this engine exists and would sell the same API call to anyone
    # who pays for it directly. Passed per-call (not session.headers,
    # which would race: _sessions caches one session per agent and FastAPI
    # runs sync handlers in a threadpool, so two concurrent /spend calls
    # for the same agent could clobber each other's token on a shared
    # mutable session header).
    auth_headers = {policy_auth.HEADER_NAME: policy_auth.mint_token(agent_id, action, params)}

    try:
        # 60s, not 30s: the full round trip here is policy engine -> resource
        # server -> facilitator -> Algorand -> back, and the public testnet
        # facilitator/algod endpoints have been observed taking well over
        # 30s combined under real (if degraded) network conditions -- see
        # README "Known limitations" #4. This doesn't make that dependency
        # any less real, it just stops treating "slow" the same as "broken."
        response = session.get(url, headers=auth_headers, timeout=config.OUTBOUND_TIMEOUT_SECONDS)
    except Exception as e:
        reason = f"payment request failed: {e}"
        row = storage.finalize(reservation_id, "denied", reason)
        return {"decision": "denied", "reason": reason, "code": "payment_failed", "log": row}

    if not response.ok:
        code, reason = _classify_failure(response.status_code, response.text)
        row = storage.finalize(reservation_id, "denied", reason)
        return {"decision": "denied", "reason": reason, "code": code, "log": row}

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
        explorer_url = config.explorer_url(tx_id)
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
        return _deny(agent_id, action, 0.0, f"unknown agent '{agent_id}'", "unknown_agent", req.params)

    # 2. Known action?
    action_cfg = policy["actions"].get(action)
    if action_cfg is None:
        return _deny(agent_id, action, 0.0, f"unknown action '{action}'", "unknown_action", req.params)

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
            f"no known signing key on file for '{agent_id}'", "no_signing_key", req.params,
        )

    # Verified against params EXACTLY as supplied, before any default is
    # filled in -- the signature has to cover what the caller actually sent,
    # not what policy later turned it into.
    sig_ok, sig_reason = identity.verify_request(
        claimed_address, agent_id, action, amount_usd, req.timestamp, req.nonce, req.signature,
        req.params,
    )
    if not sig_ok:
        return _deny(
            agent_id, action, amount_usd,
            f"identity verification failed: {sig_reason}", "identity_failed", req.params,
        )

    # 4. Kill switch. Checked immediately after identity and before every
    # budget rule, because freezing an agent has to mean "stops spending
    # now", not "stops spending once its remaining budget runs out."
    if agent_cfg.get("frozen"):
        return _deny(
            agent_id, action, amount_usd,
            f"agent '{agent_id}' is frozen -- kill switch engaged by an operator", "frozen",
            req.params,
        )

    # 5. Action approved for this agent?
    if action not in agent_cfg["allowed_actions"]:
        return _deny(
            agent_id, action, amount_usd,
            f"agent '{agent_id}' is not approved for action '{action}'", "action_not_allowed",
            req.params,
        )

    # 6. Call arguments valid for this action? Evaluated once the agent is
    # known to be allowed the action at all, and before any budget is
    # touched -- a malformed call shouldn't reserve money it can't spend.
    params, param_error = _resolve_params(action_cfg, req.params)
    if param_error:
        return _deny(
            agent_id, action, amount_usd,
            f"invalid call parameters: {param_error}", "invalid_params", req.params,
        )

    # 7. Per-request spend limit
    if amount_usd > agent_cfg["per_request_limit_usd"]:
        return _deny(
            agent_id, action, amount_usd,
            f"${amount_usd:.2f} exceeds per-request limit "
            f"${agent_cfg['per_request_limit_usd']:.2f} for '{agent_id}'",
            "over_per_request_limit", params,
        )

    # 8. Human-in-the-loop threshold. Above this, policy alone isn't allowed
    # to authorize the spend -- it gets parked holding its budget until a
    # person releases or rejects it. Deliberately evaluated BEFORE the cap
    # reservation returns to the caller, so a parked request can't be
    # double-spent against by a later one arriving while it waits.
    approval_threshold = agent_cfg.get("require_approval_above_usd")
    needs_approval = approval_threshold is not None and amount_usd > approval_threshold

    # 9 + 10. Velocity and the daily cap, decided together inside one
    # BEGIN IMMEDIATE transaction (see storage.try_reserve).
    #
    # Velocity bounds the RATE: a daily cap bounds total damage but says
    # nothing about how fast it arrives, and an agent stuck in a retry loop
    # can burn a whole day's budget in seconds with every call individually
    # within policy. That's the failure mode an autonomous agent actually
    # has. The cap then bounds the day, atomically, so two concurrent calls
    # (double-click, two tabs, autopilot overlapping a manual fire) can't
    # both read the same "spent so far" and both slip under it.
    #
    # Both live in the same transaction because a rate limiter evaluated
    # outside the write lock is a check-then-act -- and concurrency is
    # precisely the condition it exists to survive.
    reservation, rejected = storage.try_reserve(
        agent_id,
        action,
        amount_usd,
        agent_cfg["daily_cap_usd"],
        decision="awaiting_approval" if needs_approval else "pending",
        params=params,
        max_requests_per_minute=agent_cfg.get("max_requests_per_minute"),
        velocity_window_seconds=VELOCITY_WINDOW_SECONDS,
    )
    if rejected is not None:
        if rejected["code"] == "over_velocity_limit":
            return _deny(
                agent_id, action, amount_usd,
                f"velocity limit hit: {rejected['recent']} requests in the last "
                f"{rejected['window_seconds']}s (max {rejected['limit']}/min for '{agent_id}')",
                "over_velocity_limit", params,
            )
        return _deny(
            agent_id, action, amount_usd,
            f"${rejected['spent_today']:.2f} spent today + ${amount_usd:.2f} would exceed "
            f"daily cap ${rejected['cap']:.2f} for '{agent_id}'",
            "over_daily_cap", params,
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

    result = _execute_payment(agent_id, action, action_cfg, reservation["id"], params)
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

    # Re-run the authority checks at RELEASE time, not just at request time.
    # A hold sits for as long as the reviewer takes, and everything policy
    # says about this agent can change while it waits. The reviewer is
    # approving an amount; they are not re-granting the permission to make
    # the call at all -- so a hold must not become a hole through which a
    # revocation that landed in the meantime is escaped.
    #
    # Each of these is a way an operator can say "this agent may no longer
    # do this", and every one of them has to reach a queued request:
    reason = None
    agent_cfg = policy["agents"].get(row["agent_id"])
    action_cfg = policy["actions"].get(row["action"])

    if action_cfg is None:
        reason = f"action '{row['action']}' no longer exists in policy"
    elif agent_cfg is None:
        # Deregistration. The kill switch stops an agent; deregistering it
        # removes it outright, which is strictly the stronger statement --
        # it would be absurd for the weaker one to block a queued spend and
        # the stronger one to let it through.
        reason = (
            f"agent '{row['agent_id']}' was deregistered while this request was awaiting approval"
        )
    elif agent_cfg.get("frozen"):
        reason = f"agent '{row['agent_id']}' was frozen while this request was awaiting approval"
    elif row["action"] not in agent_cfg["allowed_actions"]:
        reason = (
            f"agent '{row['agent_id']}' is no longer approved for '{row['action']}' "
            "-- the action was revoked while this request was awaiting approval"
        )

    if reason is not None:
        storage.append_event("approval.blocked", {"request_id": request_id, "reason": reason})
        return {
            "decision": "denied",
            "reason": reason,
            "code": "approval_blocked",
            "log": storage.finalize(request_id, "denied", reason),
        }

    # The arguments this hold was created with, not a fresh resolution. A
    # reviewer approved a specific call -- "enrich Apple Inc." -- and if
    # policy's default changed while it sat in the queue, re-deriving here
    # would spend their approval on a different request than the one they
    # actually saw.
    params = json.loads(row.get("params") or "{}")

    storage.append_event(
        "approval.granted",
        {"request_id": request_id, "agent_id": row["agent_id"], "params": params},
    )
    result = _execute_payment(row["agent_id"], row["action"], action_cfg, request_id, params)
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

    uvicorn.run(app, host=config.BIND_HOST, port=config.POLICY_ENGINE_PORT)
