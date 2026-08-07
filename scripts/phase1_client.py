"""Phase 1: prove the raw x402 -> Algorand payment loop works, end to end.

No policy engine, no UI. Just: GET /weather -> 402 -> sign + settle an
Algorand testnet USDC payment -> retry -> 200. Run resource_server/main.py
first, then run this against it.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from x402.http import x402HTTPClientSync
from common.avm_client import build_paying_session

RESOURCE_SERVER_URL = os.getenv("RESOURCE_SERVER_URL", "http://127.0.0.1:4021")
ENDPOINT_PATH = os.getenv("ENDPOINT_PATH", "/weather")
AGENT_NAME = os.getenv("AGENT_NAME", "agent_weather")


def main():
    session, address, x402 = build_paying_session(AGENT_NAME)
    print(f"Paying as agent '{AGENT_NAME}': {address}")

    url = RESOURCE_SERVER_URL.rstrip("/") + ENDPOINT_PATH

    with session:
        print(f"\n--> GET {url} (no payment yet)")
        response = session.get(url)

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
                    print(f"\nAlgorand testnet explorer: https://lora.algokit.io/testnet/transaction/{txid}")
            except ValueError:
                print("No payment response header found (was this route actually paid?)")
        else:
            print("\nRequest did not succeed -- payment loop failed.")
            sys.exit(1)


if __name__ == "__main__":
    main()
