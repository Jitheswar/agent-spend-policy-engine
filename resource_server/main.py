"""x402-protected resource server.

Hosts the paid APIs that agents want to call. Every route below is gated by
the x402 payment middleware: an unpaid request gets HTTP 402 with payment
requirements, a request bearing a valid X-PAYMENT header (a signed Algorand
testnet transaction group) gets verified + settled through the public
GoPlausible facilitator, then served.

This process knows nothing about policy -- it just sells API calls for USDC
on Algorand testnet. The policy engine (policy_engine/app.py) sits in front
of it and decides whether a request is even allowed to reach here.
"""

import json
import os

from fastapi import FastAPI, Request

from x402.http import FacilitatorConfig, HTTPFacilitatorClient, PaymentOption
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import RouteConfig
from x402.mechanisms.avm import ALGORAND_TESTNET_CAIP2, USDC_TESTNET_ASA_ID
from x402.mechanisms.avm.exact import ExactAvmServerScheme
from x402.schemas import AssetAmount
from x402.server import x402ResourceServer

ACCOUNTS_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "accounts.json")
FACILITATOR_URL = os.getenv("FACILITATOR_URL", "https://facilitator.goplausible.xyz")


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
