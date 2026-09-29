# Agent Spend Policy Engine

[![tests](https://github.com/Jitheswar/agent-spend-policy-engine/actions/workflows/tests.yml/badge.svg)](https://github.com/Jitheswar/agent-spend-policy-engine/actions/workflows/tests.yml)

AI agents sometimes need to pay for APIs. This decides whether each payment is allowed before any money moves. Approved payments are settled for real on Algorand testnet using the x402 protocol (request, get a 402, pay, retry, get a 200).

Every decision, allowed or blocked, goes into a tamper-evident log. Each entry contains the hash of the one before it, and the latest hash is periodically written to Algorand. So anyone can check that the record wasn't edited, without having to trust whoever runs the system.

The APIs behind the paywall are real. `/weather` returns live conditions from Open-Meteo. `/enrich` looks up a company or ticker on SEC EDGAR and returns its public profile.

```bash
./start.sh     # then open http://127.0.0.1:4023/index.html
```

New here? [GETTING_STARTED.md](GETTING_STARTED.md) takes you from `git clone` to a paid API call in about fifteen minutes. There's also a five-minute demo script in [DEMO.md](DEMO.md).

## Why a blockchain?

Two things use it, and only one of them really needs it.

**Payments.** Agents pay per call in testnet USDC. Small payments between software with no account, invoice or history with each other are awkward on card rails. This is useful, but plenty of projects stop here.

**The audit log.** This is the part that needs a chain. A spending-control system is only as good as its record. If the record sits in the operator's own database, the operator can rewrite it. With the latest hash sitting in an Algorand block, a rewritten history no longer matches, and anyone can check that against a public indexer.

You can check it in about ten seconds:

```bash
python3 scripts/verify_audit.py
```

It reads the database directly, recomputes every hash, then fetches each anchor from the public AlgoNode indexer and compares. It exits 0 only if the chain is intact and confirmed on-chain.

The dashboard also has a *Tamper with a record* button. It edits a past entry the way a dishonest operator would, and verification still catches it and names the exact record.

## Built on existing x402 support

Algorand already had x402 support, so we used it instead of writing our own:

- **GoPlausible** runs a public x402 facilitator with Algorand support at `https://facilitator.goplausible.xyz`.
- **`x402-avm`** is an official Python SDK with FastAPI and requests/httpx integration. Algorand's x402 spec is part of Coinbase's x402 repo ([scheme_exact_algo.md](https://github.com/coinbase/x402/blob/main/specs/schemes/exact/scheme_exact_algo.md)).

Payments are in testnet USDC (asset id `10458941`), not raw ALGO, so prices per call stay stable.

## How it fits together

```
Agents (signed requests)          Dashboard (human operator)
        | POST /spend                      | approve / reject / freeze / anchor
        v                                  v
   +---------------------------------------------------+
   | Policy Engine :4022                               |
   | identity -> kill switch -> allowed action ->      |
   | per-request limit -> velocity -> daily cap ->     |
   | human approval threshold                          |
   | every outcome -> requests (working view)          |
   |               -> audit_events (append-only chain) |
   +-------+----------------------------+--------------+
           | only if approved           | chain head, periodically
           v                            v
   Resource Server :4021         Algorand testnet
   policy gate + x402            note: ASPE1|seq|hash
           |
           v
   GoPlausible facilitator -> Algorand
```

- `policy_engine/` runs the checks on `POST /spend`, and only then starts the payment. A denied request never reaches the resource server. Admin routes are protected by a middleware, so forgetting to protect a new route isn't possible.
- `policy_engine/storage.py` has two tables. `requests` is the working view the dashboard reads. `audit_events` is the append-only chain. Nothing updates or deletes its rows, even `/admin/reset`, which adds a reset event instead.
- `common/anchor.py` writes the chain head to Algorand as a 0-ALGO self-payment with `ASPE1|<seq>|<hash>` in the note. It runs on a background thread once enough events pile up, so no `/spend` ever waits on it.
- `resource_server/` sells `/weather` ($0.01) and `/enrich` ($0.05) behind two layers: a token from the policy engine, then x402 payment. It knows nothing about spending policy. It just refuses anyone the engine hasn't cleared.
- `resource_server/upstreams.py` holds the real vendors, Open-Meteo and SEC EDGAR. Both are free, so the money here demonstrates pay-per-call settlement, not a markup.
- `common/identity.py` signs and verifies `/spend` requests with the agent's own Algorand key. Requests can't be replayed (nonce plus a 60-second window).
- `common/policy_auth.py` makes the per-spend HMAC token the resource server checks. It covers the agent and the call arguments, so it authorizes one specific call, not a category.
- `common/config.py` is the only place that reads settings. One `NETWORK` value picks the node, indexer, USDC asset, chain id and explorer links together, so anchoring and payments can't end up on different chains.
- `common/provisioning.py` onboards a new agent while running: keypair, ALGO for fees, USDC opt-in and working capital.
- `agents/simulate.py` fires repeatable sequences of signed requests.
- `dashboard/` is a static HTML/JS control page.

## The checks

Every `/spend` goes through these in order. The order matters and is tested.

| Check | Denial code | Why |
|---|---|---|
| Known agent and action | `unknown_agent`, `unknown_action` | Nothing unregistered spends |
| Cryptographic identity | `identity_failed` | The caller must hold that agent's key |
| Kill switch | `frozen` | Stop an agent immediately, whatever budget is left |
| Allowed action | `action_not_allowed` | This agent may call this API |
| Call arguments | `invalid_params` | Limit what it may ask for, not just what it may spend |
| Per-request limit | `over_per_request_limit` | Cap any single call |
| Velocity | `over_velocity_limit` | Cap the rate, so a retry loop can't burn the daily cap in seconds |
| Daily cap | `over_daily_cap` | Cap the day |
| Human approval | `awaiting_approval` | Above a threshold, a person must release it |

Velocity and the daily cap are decided in one transaction, so two simultaneous calls can't both slip under a limit.

A request waiting for approval keeps its budget reserved. When someone approves it, every authority check runs again, so an agent that was frozen, removed, or stripped of the action while it waited can't spend through the queue. The approved spend also uses the arguments the reviewer saw, even if the policy changed while it sat in the queue.

Limits live in `policy_engine/policy.json`. Edit it live with `PATCH /admin/agents/{id}`, or edit the file, which reloads by itself.

### Arguments are part of the policy

Each action declares what arguments it accepts:

```json
"weather": {
  "resource_path": "/weather",
  "price_usd": 0.01,
  "params": { "city": { "required": true, "max_length": 64, "default": "San Francisco" } }
}
```

An argument that isn't declared is denied, not passed along. Defaults come from policy, never from the caller. The arguments are covered by the request signature, so nobody can change them in flight, and they're written to the log even when a request is denied early.

### A failed API call costs nothing

The x402 middleware only takes payment if the route returns a status below 400. So if the vendor is down or the city doesn't exist, the handler returns a real error and no money moves. Checked on testnet: weather for a city that doesn't exist returned `upstream_not_found` and moved exactly 0.000000 USDC. `tests/test_upstreams.py` checks this against the real middleware.

## Setup

```bash
./start.sh
```

This creates the virtualenv, installs dependencies, prints the settings it will use, creates Algorand accounts in `data/accounts.json` (gitignored), opts them into USDC, and starts the resource server (`:4021`), the policy engine (`:4022`) and the dashboard (`:4023`). It's safe to run again. No `.env` is needed to start.

After every account is funded, a `data/.setup_verified` marker is written and later runs skip the slow balance checks. Run `./start.sh --recheck` to force them again.

### Funding the accounts (needs a person, once)

New accounts start empty and payments fail until they're funded:

1. **ALGO** for fees: the Algorand TestNet Dispenser at https://lora.algokit.io/testnet/fund (needs sign-in and a captcha).
2. **Testnet USDC:** https://faucet.circle.com, pick Algorand testnet, and request for each address. You get 20 USDC per address every 2 hours, far more than the demo uses.

Every address needs both, including `server`, which pays anchoring fees in ALGO and funds new agents in USDC. Then run `./start.sh` again, or check with:

```bash
python3 scripts/setup_accounts.py balances
```

### Settings

```bash
cp .env.example .env               # optional, defaults work
python3 -m common.config           # shows what this process is pointed at
```

Every setting and its default is in [`.env.example`](.env.example). Environment variables beat `.env`, so `NETWORK=mainnet ./start.sh` works without editing anything. `NETWORK=mainnet` refuses to start unless `ALLOW_MAINNET=true` is also set, refuses an empty `ADMIN_TOKEN`, and turns `ALLOW_DEMO_ENDPOINTS` off by default.

Before real traffic, set a contact string for SEC EDGAR, which asks callers to identify themselves:

```
SEC_USER_AGENT=your-project/1.0 (you@example.com)
```

### Admin access

Every `/admin/*` route (freeze, unfreeze, onboard, approve, edit a cap) needs a bearer token:

```bash
curl -H "Authorization: Bearer $(python3 -m common.config --admin-token)" \
  -X POST http://127.0.0.1:4022/admin/agents/agent_rogue/freeze \
  -H 'Content-Type: application/json' -d '{"frozen":true}'
```

A token is generated on first run in `data/admin_token.txt` (gitignored) and passed to the dashboard by `start.sh`. Set `ADMIN_TOKEN` to use your own. An empty value turns auth off, which is fine on a throwaway testnet box and refused on mainnet.

`/spend` is deliberately not behind this token. Anyone can try to spend. The signature decides.

### Running scenarios from the command line

```bash
python3 agents/simulate.py once        # one pass through the scenarios
python3 agents/simulate.py loop 4      # repeat every 4 seconds
python3 agents/simulate.py burst 25    # a runaway agent that trips the velocity limit
```

## Adding an agent while it's running

Use **+ Onboard an agent** in the dashboard. About fifteen seconds later the new agent is paying for API calls, with no restart.

The order is deliberate:

1. It gets an Algorand keypair. Without its own key it can't sign, so it can't spend.
2. It's registered in policy with limits. This happens before funding, so there's never a moment where an agent can pay without a policy covering it.
3. Its account is funded and opted into USDC on-chain, in the background: ALGO for fees, the USDC opt-in (signed by the agent), then working capital, all from the server's treasury.

While step 3 runs, the agent is governed but can't pay, and its Fire buttons stay disabled.

From the command line:

```bash
curl -X POST http://127.0.0.1:4022/admin/agents \
  -H "Authorization: Bearer $(python3 -m common.config --admin-token)" \
  -H 'Content-Type: application/json' \
  -d '{"agent_id":"agent_research","display_name":"Research Agent",
       "allowed_actions":["weather"],"per_request_limit_usd":0.02,"daily_cap_usd":0.10}'
```

`DELETE /admin/agents/{id}` removes an agent. Its key stays in `data/accounts.json` and its history stays in the log, so deleting an agent can't erase what it spent. Adding it back reuses the same account. Adding, funding and removing agents are all logged and anchored too.

Each onboarding takes 0.5 ALGO and 0.5 USDC from the `server` account (`PROVISION_ALGO` and `PROVISION_USDC` change that). It fails with a clear message if the account is empty.

## Tests

```bash
pytest tests/ -v
```

No network is needed. Upstreams are mocked, and anchoring is turned off in `tests/conftest.py`, so a test run never sends a transaction or calls a vendor. Admin auth is off in the suite so it can call `/admin` routes, and `test_admin_auth.py` turns it on and tests it for real.

The tests cover policy decisions and race conditions (12 simultaneous requests against a cap with room for 2, and against a rate limit of 3, with exactly 2 and exactly 3 getting through), admin auth on every route, the hash chain including a tamper where the attacker recomputes the edited entry's hash, governance (kill switch, velocity, approval holds), request signing, resource-server tokens, config, the upstreams, argument rules, and agent onboarding.

They don't cover the approval path end to end, which needs a live facilitator and funded accounts. `scripts/phase1_client.py` does that check.

## Known limitations

1. **The resource server could be called directly.** Fixed. It rejects any request without a valid, unexpired token from the policy engine. The token is a shared secret on local disk, not network isolation, so production should put the resource server on a network only the engine can reach, or use mTLS. Also, a token isn't single-use, because x402 sends the request twice. Its lifetime is the limit.
2. **Agent identity used to be unchecked.** Mostly fixed. `/spend` needs a signature over agent, action, amount, arguments, timestamp and nonce. In this demo the engine holds every agent's key (that's how it signs their payments), and the dashboard's Fire buttons go through `POST /admin/sign`, which is a signing oracle. It's protected by the admin token and by `ALLOW_DEMO_ENDPOINTS`, which is off on mainnet. With it off, nobody can spend without holding an agent's private key.
3. **A hash chain can't detect an edit to its own last entry.** This is inherent. Only an anchor closes it, which is why anchoring is automatic. The exposure window is the events since the last anchor, capped by `AUTO_ANCHOR_THRESHOLD` (default 8). The demo's tamper button avoids the last entry so the demo stays honest.
4. **The x402 facilitator is a single point of failure.** Not fixed, on purpose. A local copy would just move the failure to the same machine. If `facilitator.goplausible.xyz` is down, every payment fails. It and AlgoNode sometimes take 30+ seconds. The outbound timeout is 60 seconds to avoid false denials.
5. **`policy.json` needed a restart.** Fixed. It reloads when the file changes, and `PATCH` writes changes back safely.
6. **Anchoring costs a fee.** 0.001 ALGO per anchor, about one transaction per 8 decisions. Production would batch more, ideally with a Merkle root.
7. **The upstreams are free.** The $0.01 and $0.05 prices demonstrate settlement, not a markup. A cache hit still counts as a sale. The upstream call happens inside the payment window, so its timeout is capped at 10 seconds against the engine's 60.
8. **The admin plane had no auth.** Fixed, with a bearer token. It isn't per-operator identity and doesn't tell two operators apart.
9. **A held request could outlive the authority behind it.** Fixed. Releasing a hold checks the agent still exists, isn't frozen, and still has the action.
10. **Reservations could leak on a crash.** Fixed. On startup the engine releases reservations older than the outbound timeout and logs it.

## Checking a payment yourself

Every approved request returns an Algorand testnet transaction ID and an explorer link (`https://lora.algokit.io/testnet/transaction/<txid>`). Check it against the public indexer:

```bash
curl -s "https://testnet-idx.algonode.cloud/v2/transactions/<txid>" | python3 -m json.tool
```

To read an anchor's hash straight off the chain:

```bash
curl -s "https://testnet-idx.algonode.cloud/v2/transactions/<anchor_txid>" \
  | python3 -c "import sys,json,base64; print(base64.b64decode(json.load(sys.stdin)['transaction']['note']).decode())"
```

That prints `ASPE1|<seq>|<hash>`, the hash the log had at that point, sitting in a block anyone can read.
