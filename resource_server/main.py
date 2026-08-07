"""x402-protected resource server.

Hosts the paid APIs that agents want to call. Every route below is gated by
TWO layers, in this order (see PolicyAuthMiddleware docstring for why the
order matters):

  1. PolicyAuthMiddleware -- rejects outright (403) any request that
     doesn't carry a valid, unexpired token minted by the policy engine.
     Without this, this process would happily sell API calls to anyone who
     pays for them directly, completely bypassing policy_engine/app.py's
     spend limits and daily caps. It has no idea that engine exists unless
     something tells it to check.
  2. The x402 payment middleware -- unpaid request gets HTTP 402 with
     payment requirements, a request bearing a valid X-PAYMENT header (a
     signed Algorand testnet transaction group) gets verified + settled
     through the public GoPlausible facilitator, then served.
"""

import json
import os
import sys

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from x402.http import FacilitatorConfig, HTTPFacilitatorClient, PaymentOption
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import RouteConfig
from x402.mechanisms.avm import ALGORAND_TESTNET_CAIP2, USDC_TESTNET_ASA_ID
from x402.mechanisms.avm.exact import ExactAvmServerScheme
from x402.schemas import AssetAmount
from x402.server import x402ResourceServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.policy_auth import HEADER_NAME, verify_token  # noqa: E402

ACCOUNTS_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "accounts.json")
FACILITATOR_URL = os.getenv("FACILITATOR_URL", "https://facilitator.goplausible.xyz")

# Maps protected paths to the action name policy tokens are minted for.
PROTECTED_ACTIONS = {"/weather": "weather", "/enrich": "enrich"}


def _server_address() -> str:
    override = os.getenv("AVM_ADDRESS")
    if override:
        return override
    with open(ACCOUNTS_PATH) as f:
        accounts = json.load(f)
    return accounts["server"]["address"]


AVM_ADDRESS = _server_address()

app = FastAPI(title="Agent Spend Policy Engine -- Resource Server")

facilitator = HTTPFacilitatorClient(FacilitatorConfig(url=FACILITATOR_URL))
server = x402ResourceServer(facilitator)
server.register(ALGORAND_TESTNET_CAIP2, ExactAvmServerScheme())

# Two paid APIs, matching the two mock agent scenarios in the build spec.
routes = {
    "GET /weather": RouteConfig(
        accepts=PaymentOption(
            scheme="exact",
            pay_to=AVM_ADDRESS,
            price=AssetAmount(
                amount="10000",  # 0.01 USDC
                asset=str(USDC_TESTNET_ASA_ID),
                extra={"name": "USDC", "decimals": 6},
            ),
            network=ALGORAND_TESTNET_CAIP2,
        ),
        mime_type="application/json",
        description="Paid weather report API",
    ),
    "GET /enrich": RouteConfig(
        accepts=PaymentOption(
            scheme="exact",
            pay_to=AVM_ADDRESS,
            price=AssetAmount(
                amount="50000",  # 0.05 USDC
                asset=str(USDC_TESTNET_ASA_ID),
                extra={"name": "USDC", "decimals": 6},
            ),
            network=ALGORAND_TESTNET_CAIP2,
        ),
        mime_type="application/json",
        description="Paid data enrichment API",
    ),
}

app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=server)


class PolicyAuthMiddleware(BaseHTTPMiddleware):
    """Rejects any request to a protected route that doesn't carry a valid
    token from the policy engine, before the x402 middleware ever runs.

    Starlette runs middleware added via add_middleware() in LAST-added,
    first-executed order on incoming requests (verified empirically against
    this installed version -- see scratch/middleware_order_test.py from the
    session that added this). Registering this middleware AFTER
    PaymentMiddlewareASGI above is what makes it the outer layer: an
    unauthorized caller gets a 403 here and never even sees a 402, let
    alone gets a chance to pay.
    """

    async def dispatch(self, request, call_next):
        action = PROTECTED_ACTIONS.get(request.url.path)
        if action is None:
            return await call_next(request)

        token = request.headers.get(HEADER_NAME)
        if not token:
            return JSONResponse(
                {
                    "error": "missing policy authorization",
                    "detail": "this endpoint only serves requests that already cleared the policy engine",
                },
                status_code=403,
            )

        ok, reason, _claimed_agent_id = verify_token(token, action)
        if not ok:
            return JSONResponse({"error": "invalid policy authorization", "detail": reason}, status_code=403)

        return await call_next(request)


app.add_middleware(PolicyAuthMiddleware)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "pay_to": AVM_ADDRESS, "facilitator": FACILITATOR_URL}


@app.get("/weather")
async def get_weather(request: Request) -> dict:
    return {"weather": "sunny", "temperature": 72, "unit": "F"}


@app.get("/enrich")
async def get_enrichment(request: Request) -> dict:
    return {
        "company": "Acme Corp",
        "employees": 4200,
        "industry": "Logistics",
        "risk_score": 0.12,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=4021)
