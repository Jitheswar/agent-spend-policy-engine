"""Phase 1: prove the raw x402 -> Algorand payment loop works, end to end.

No policy engine, no UI. Just: GET /weather -> 402 -> sign + settle an
Algorand testnet USDC payment -> retry -> 200. Run resource_server/main.py
first, then run this against it.

One thing it cannot skip, though it would like to: the resource server's
PolicyAuthMiddleware rejects anything without a token minted by the policy
engine, so this mints its own. That is not cheating -- it runs on the same
machine and reads the same local secret the engine does, which is exactly
the trust boundary common/policy_auth.py documents. The alternative is a
script that 403s before it ever sees a 402, which is what this was doing
after that middleware landed: still described in the README as the proof of
the payment loop, and no longer able to prove anything.
"""

import json
import os
import sys
from urllib.parse import urlencode

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from x402.http import x402HTTPClientSync
from common import config, policy_auth
from common.avm_client import build_paying_session

RESOURCE_SERVER_URL = config.RESOURCE_SERVER_URL
ENDPOINT_PATH = config.env("ENDPOINT_PATH", "/weather")
AGENT_NAME = config.env("AGENT_NAME", "agent_weather")
# Which action the minted token is for. Has to match the path: the token
# commits to the action AND to the call arguments below.
ACTION = config.env("ACTION", ENDPOINT_PATH.lstrip("/"))
CITY = config.env("CITY", "San Francisco")


def main():
    session, address, x402 = build_paying_session(AGENT_NAME)
    print(f"Paying as agent '{AGENT_NAME}': {address}")

    params = {"city": CITY} if ACTION == "weather" else {"company": config.env("COMPANY", "Apple Inc.")}
    url = RESOURCE_SERVER_URL.rstrip("/") + ENDPOINT_PATH + "?" + urlencode(params)
    headers = {policy_auth.HEADER_NAME: policy_auth.mint_token(AGENT_NAME, ACTION, params)}

    with session:
        print(f"\n--> GET {url} (no payment yet)")
        response = session.get(url, headers=headers)

        print(f"<-- final status: {response.status_code}")
        print(f"<-- body: {response.text}")

        if response.ok:
            http_client = x402HTTPClientSync(x402)
            try:
                settle = http_client.get_payment_settle_response(
                    lambda name: response.headers.get(name)
                )
                data = json.loads(settle.model_dump_json())
                print("\n=== SETTLEMENT ===")
                print(json.dumps(data, indent=2))
                txid = data.get("transaction")
                if txid:
                    print(f"\nAlgorand explorer: {config.explorer_url(txid)}")
            except ValueError:
                print("No payment response header found (was this route actually paid?)")
        else:
            print("\nRequest did not succeed -- payment loop failed.")
            sys.exit(1)


if __name__ == "__main__":
    main()
