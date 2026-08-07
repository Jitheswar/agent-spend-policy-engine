"""Mock agents that fire spend requests at the policy engine.

Three fixed identities, matching policy_engine/policy.json:
  - agent_weather:     calls the paid weather API, well within its limits.
  - agent_enrichment:  calls the paid data enrichment API, well within its limits.
  - agent_rogue:       tightly capped on purpose -- burns through its daily
                        cap in two calls, then keeps trying (denied), and
                        also tries an action it was never approved for.

The scenario list below is a fixed, curated sequence rather than random
firing, so a live demo always shows the same clean mix of approvals and
denials instead of relying on luck.

Usage:
    python agents/simulate.py once       # fire the whole sequence once, then exit
    python agents/simulate.py loop 4     # fire the sequence on repeat, 4s between calls
"""

import os
import sys
import time

import requests

POLICY_ENGINE_URL = os.getenv("POLICY_ENGINE_URL", "http://127.0.0.1:4022")

# (agent_id, action) -- ordered for a good demo narrative.
SCENARIOS = [
    ("agent_weather", "weather"),      # approved: normal spend
    ("agent_enrichment", "enrich"),    # approved: normal spend
    ("agent_weather", "enrich"),       # denied: action not approved for this agent
    ("agent_rogue", "weather"),        # approved: 1st call, $0.01 of $0.02 cap
    ("agent_rogue", "weather"),        # approved: 2nd call, hits $0.02 cap exactly
    ("agent_rogue", "weather"),        # denied: would exceed daily cap
    ("agent_rogue", "enrich"),         # denied: action not approved for this agent
    ("agent_ghost", "weather"),        # denied: unknown / unregistered agent
]


def fire(agent_id: str, action: str) -> dict:
    resp = requests.post(
        f"{POLICY_ENGINE_URL}/spend",
        json={"agent_id": agent_id, "action": action},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def run_once():
    for agent_id, action in SCENARIOS:
        result = fire(agent_id, action)
        decision = result["decision"].upper()
        line = f"[{decision:7s}] {agent_id:16s} -> {action:8s}  {result['reason']}"
        if result.get("tx_id"):
            line += f"\n           tx: {result['explorer_url']}"
        print(line)


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
    else:
        print(__doc__)
        sys.exit(1)
