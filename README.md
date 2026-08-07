# Agent Spend Policy Engine

Autonomous agents try to spend money on paid APIs. A policy engine checks
spend limits, approval rules, and daily caps *before* any payment is
attempted. Every approved payment settles for real on **Algorand testnet**
through the **x402** protocol (HTTP 402 -> pay -> retry -> 200).

## x402 + Algorand: what exists, what we built

x402 was built around EVM/USDC. Before writing any code we checked whether
Algorand support already existed. It does:

- **GoPlausible** operates a public, live x402 facilitator with first-class
  Algorand support: `https://facilitator.goplausible.xyz`. It verifies and
  settles payments on Algorand testnet/mainnet (and Base, Solana).
- There's an official Python SDK on PyPI, **`x402-avm`**, with FastAPI
  server middleware and requests/httpx client integration. Algorand's x402
  spec extension is merged into Coinbase's x402 repo
  ([scheme_exact_algo.md](https://github.com/coinbase/x402/blob/main/specs/schemes/exact/scheme_exact_algo.md)).

So we did **not** build a custom facilitator. We used the real one. The
"exact" scheme pays in testnet USDC (an ASA, id `10458941`), not raw ALGO --
that's the standard x402 pattern (stable pricing per call).

## Architecture

```
Mock agents / Dashboard
        |
        v  POST /spend {agent_id, action}
Policy Engine (:4022)  --- checks policy.json, logs to SQLite ---
        |
        v  (only if approved) GET <resource>, pays via x402 if 402'd
Resource Server (:4021) --- x402 middleware, gated routes ---
        |
        v  verify() / settle()
GoPlausible Facilitator (public, hosted)
        |
        v
Algorand Testnet (real, verifiable transactions)
```

- **`resource_server/`** -- FastAPI app selling `/weather` ($0.01) and
  `/enrich` ($0.05) behind x402 payment middleware. Knows nothing about
  policy; it just sells API calls for USDC on Algorand testnet.
- **`policy_engine/`** -- the enforcement layer. `policy.json` defines
  per-agent allowed actions, per-request limits, and daily caps.
  `POST /spend` checks policy first; a denial never reaches the resource
  server, so no payment is ever attempted for a blocked request. Every
  outcome (approved or denied) is written to `data/policy_engine.db`
  (SQLite) as the audit trail.
- **`common/avm_client.py`** -- shared Algorand signer + x402 paying
  session, used by both the raw Phase 1 proof script and the policy
  engine's payment step.
- **`agents/simulate.py`** -- fires a curated, repeatable sequence of
  requests (mix of approvals and denials) at the policy engine.
- **`dashboard/`** -- static HTML/JS. Live agent cards (spend, caps, fire
  buttons), live request feed with clickable Algorand testnet explorer
  links, autoplay toggle, and a demo-log reset button.

## Setup + run

```bash
./start.sh
```

One command: creates the venv, installs dependencies, generates the four
Algorand testnet accounts (`data/accounts.json`, gitignored, testnet-only)
if they don't exist yet, checks/opts them into testnet USDC, then starts
all three services -- resource server (`:4021`), policy engine (`:4022`),
dashboard (`:4023`). Safe to re-run any time; it only creates what's
missing and always (re)starts the services fresh. Open
http://127.0.0.1:4023/index.html.

### Funding the accounts (one-time, needs a human)

The first `./start.sh` run generates the accounts but they start with zero
balance -- payments will fail until funded:

1. ALGO (small amounts, just for opt-in fees/min-balance): the official
   Algorand TestNet Dispenser at https://bank.testnet.algorand.network/
   (Google login + captcha -- a human has to do this step).
2. Testnet USDC (what the payments actually happen in): **faucet.circle.com**
   -> select Algorand testnet -> request for each `agent_*` address. The
   `server` address never needs USDC, it only receives it.

Re-run `./start.sh` after funding -- it'll pick up the balances and opt
every account into USDC automatically. Or check manually:

```bash
python3 scripts/setup_accounts.py balances
```

Or fire scenarios from the CLI instead of/alongside the dashboard:

```bash
python3 agents/simulate.py once       # one pass through the curated scenario list
python3 agents/simulate.py loop 4     # repeat every 4s
```

To prove the raw payment loop in isolation (no policy engine):

```bash
python3 scripts/phase1_client.py
```

## Verifying it's real

Every approved request returns an Algorand testnet transaction ID and an
explorer link (`https://lora.algokit.io/testnet/transaction/<txid>`). You
can also check any transaction independently against the public indexer:

```bash
curl -s "https://testnet-idx.algonode.cloud/v2/transactions/<txid>" | python3 -m json.tool
```
