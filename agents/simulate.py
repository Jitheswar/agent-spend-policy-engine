"""Mock agents that fire spend requests at the policy engine.

Three fixed identities, matching policy_engine/policy.json:
  - agent_weather:     calls the paid weather API, well within its limits.
  - agent_enrichment:  calls the paid data enrichment API, well within its limits.
  - agent_rogue:       tightly capped on purpose -- burns through its daily
                        cap in two calls, then keeps trying (denied), and
                        also tries an action it was never approved for.

Every request is signed with the agent's own Algorand key (common.identity)
-- this script has legitimate local access to that key material (see
data/accounts.json), the same way a real autonomous agent process would
hold its own key. That's what makes it a fair stand-in for "an agent," as
opposed to the dashboard's Fire buttons, which represent a human operator
and go through POST /admin/sign instead (see policy_engine/app.py).

The scenario list below is a fixed, curated sequence rather than random
firing, so a live demo always shows the same clean mix of approvals and
denials instead of relying on luck.

Usage:
    python agents/simulate.py once            # fire the whole sequence once, then exit
    python agents/simulate.py loop 4          # fire the sequence on repeat, 4s between calls
    python agents/simulate.py burst 25        # a runaway agent: 25 requests as fast as possible
"""

import os
import sys
import time

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common.avm_client import agent_secret_key_b64, load_accounts  # noqa: E402
from common.identity import sign_request  # noqa: E402

from common import config  # noqa: E402

POLICY_ENGINE_URL = config.POLICY_ENGINE_URL

# (agent_id, action, params) -- ordered for a good demo narrative.
#
# The params are real arguments hitting real upstreams: the cities are
# geocoded and their live conditions fetched from Open-Meteo, the companies
# are resolved to a CIK and pulled from SEC EDGAR. They vary per scenario on
# purpose -- identical arguments every run would be served from the resource
# server's cache, which is fine for a paid API but would stop the run from
# actually exercising the upstreams.
SCENARIOS = [
    ("agent_weather", "weather", {"city": "Reykjavik"}),      # approved: normal spend
    ("agent_enrichment", "enrich", {"company": "NVDA"}),      # approved: normal spend
    ("agent_weather", "enrich", {"company": "Apple"}),        # denied: action not approved for this agent
    ("agent_rogue", "weather", {"city": "Lagos"}),            # approved: 1st call, $0.01 of $0.02 cap
    ("agent_rogue", "weather", {"city": "Osaka"}),            # approved: 2nd call, hits $0.02 cap exactly
    ("agent_rogue", "weather", {"city": "Quito"}),            # denied: would exceed daily cap
    ("agent_rogue", "enrich", {"company": "Tesla"}),          # denied: action not approved for this agent
    ("agent_weather", "weather", {"planet": "Mars"}),         # denied: parameter no policy declares
    ("agent_ghost", "weather", {"city": "Oslo"}),             # denied: unknown / unregistered agent
]


def fire(agent_id: str, action: str, accounts: dict, action_prices: dict,
         params: dict | None = None) -> dict:
    amount_usd = action_prices.get(action, 0.0)
    body = {"agent_id": agent_id, "action": action, "amount_usd": amount_usd, "params": params or {}}

    if agent_id in accounts:
        # A real agent identity: sign for real with its own key. The
        # signature covers params too, so the arguments can't be swapped in
        # flight for ones this agent never authorized.
        sk_b64 = agent_secret_key_b64(agent_id, accounts)
        body.update(sign_request(sk_b64, agent_id, action, amount_usd, params or {}))
    else:
        # agent_ghost has no key on file by design (it's here to exercise
        # the "unknown agent" denial). The policy engine rejects unknown
        # agents before it ever looks at the signature, so these values
        # just need to satisfy the request schema, not be valid.
        body.update({"timestamp": time.time(), "nonce": "n/a", "signature": "n/a"})

    resp = requests.post(f"{POLICY_ENGINE_URL}/spend", json=body, timeout=90)
    resp.raise_for_status()
    return resp.json()


def run_once():
    accounts = load_accounts()
    policy = requests.get(f"{POLICY_ENGINE_URL}/policy", timeout=10).json()
    action_prices = {name: cfg["price_usd"] for name, cfg in policy["actions"].items()}

    for agent_id, action, params in SCENARIOS:
        result = fire(agent_id, action, accounts, action_prices, params)
        decision = result["decision"].upper()
        arg = ", ".join(f"{k}={v}" for k, v in params.items())
        line = f"[{decision:7s}] {agent_id:16s} -> {action:8s} ({arg})  {result['reason']}"
        if result.get("tx_id"):
            line += f"\n           tx: {result['explorer_url']}"
        print(line)


def run_burst(agent_id: str, action: str, count: int):
    """A runaway agent: the same request, as fast as the loop can issue it.

    This is the failure mode an autonomous agent actually has -- not "spends
    too much on one call" but "gets stuck and issues a correct call ten
    thousand times". Every request here is individually within the
    per-request limit, so nothing but the velocity limiter stops it, which
    is exactly the point of demonstrating it separately.

    Fires with no params, so every call resolves to the action's policy
    default and lands on the resource server's cache. That's deliberate:
    the thing under test is the rate limiter, and 25 distinct live lookups
    would be an unkind way to treat a free public API to prove it.
    """
    accounts = load_accounts()
    policy = requests.get(f"{POLICY_ENGINE_URL}/policy", timeout=10).json()
    action_prices = {name: cfg["price_usd"] for name, cfg in policy["actions"].items()}

    tally: dict[str, int] = {}
    for i in range(count):
        result = fire(agent_id, action, accounts, action_prices)
        decision = result["decision"]
        tally[decision] = tally.get(decision, 0) + 1
        print(f"  [{i + 1:3d}/{count}] {decision.upper():18s} {result['reason'][:88]}")

    print("\n" + "-" * 60)
    print(f"  {count} requests fired: " + ", ".join(f"{n} {d}" for d, n in sorted(tally.items())))
    print("  Nothing here exceeded a per-request limit -- the limiter stopped the RATE.")


def run_loop(interval: float):
    print(f"Looping scenarios every {interval}s. Ctrl+C to stop.")
    try:
        while True:
            run_once()
            print("-" * 60)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "once"
    if mode == "once":
        run_once()
    elif mode == "loop":
        interval = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
        run_loop(interval)
    elif mode == "burst":
        count = int(sys.argv[2]) if len(sys.argv) > 2 else 25
        # agent_weather by default: its daily cap ($1.00) is deliberately
        # roomy, so the velocity limiter is unambiguously the thing that
        # stops the burst rather than the budget quietly running out.
        agent_id = sys.argv[3] if len(sys.argv) > 3 else "agent_weather"
        action = sys.argv[4] if len(sys.argv) > 4 else "weather"
        run_burst(agent_id, action, count)
    else:
        print(__doc__)
        sys.exit(1)
