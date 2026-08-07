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
  `/enrich` ($0.05) behind two layers: `PolicyAuthMiddleware` (rejects any
  request without a valid token from the policy engine -- see
  [Known limitations](#known-limitations) #1) and the x402 payment
  middleware. Knows nothing about spend policy; it just refuses to serve
  anyone the policy engine hasn't already cleared.
- **`policy_engine/`** -- the enforcement layer. `policy_store.py` reads
  `policy.json` with mtime-based hot-reload (no restart needed -- see
  limitation #4) and exposes `PATCH /admin/agents/{id}` for live edits that
  persist back to the file. `POST /spend` checks policy, verifies the
  caller's cryptographic signature (limitation #2), and only then calls the
  resource server -- a denial never reaches it, so no payment is ever
  attempted for a blocked request. Every outcome is written to
  `data/policy_engine.db` (SQLite) as the audit trail.
- **`common/avm_client.py`** -- shared Algorand signer + x402 paying
  session, used by both the raw Phase 1 proof script and the policy
  engine's payment step.
- **`common/identity.py`** -- signs/verifies `/spend` requests with the
  agent's own Algorand key (`algosdk.util.sign_bytes`/`verify_bytes`),
  domain-separated from transaction signing. Replay-guarded (nonce + 60s
  window).
- **`common/policy_auth.py`** -- the shared-secret HMAC token the policy
  engine mints per approved spend and the resource server verifies before
  serving anything.
- **`agents/simulate.py`** -- fires a curated, repeatable sequence of
  *signed* requests (mix of approvals and denials) at the policy engine,
  using key material it has legitimate local access to.
- **`dashboard/`** -- static HTML/JS. Live agent cards (spend, caps, fire
  buttons with editable amounts), live request feed with clickable Algorand
  testnet explorer links, autoplay toggle, and a demo-log reset button.

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

Once every account is confirmed funded and opted in, a `data/.setup_verified`
marker is written and every later `./start.sh` skips the on-chain
balance/opt-in checks entirely -- those are real network round-trips
against the public AlgoNode testnet API (occasionally multiple seconds per
call), and re-runing them on every startup when nothing's changed is what
made `./start.sh` feel slow. Run `./start.sh --recheck` (or delete
`data/.setup_verified`) to force the checks again -- e.g. after re-funding
an account.

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

## Tests

```bash
pytest tests/ -v
```

- `test_policy_engine.py` -- policy decisions (unknown agent/action,
  per-request limit, daily cap), the daily-cap concurrency fix (12
  simultaneous reservations against a cap with room for 2, asserts exactly
  2 succeed), and the signed-identity denial paths (impersonation, replay)
  at the HTTP layer.
- `test_identity.py` -- unit tests for request signing/verification:
  wrong-key impersonation, tampered amount/action, expired timestamp,
  replay, malformed signature.
- `test_policy_auth.py` -- unit tests for the resource-server auth token:
  wrong action, tampered signature, expired, malformed.
- `test_policy_store.py` -- hot-reload on file edit, live `PATCH` persists
  to disk and is immediately visible.

Doesn't cover the approval path end-to-end (that needs a live facilitator +
funded testnet accounts -- see `scripts/phase1_client.py` for that proof
instead).

To prove the raw payment loop in isolation (no policy engine):

```bash
python3 scripts/phase1_client.py
```

## Known limitations

Four gaps were raised during review. Three are fixed; one is a documented,
deliberate tradeoff.

**1. Resource server could be called directly, bypassing policy.** Fixed.
`resource_server`'s `PolicyAuthMiddleware` rejects (403) any request to
`/weather` or `/enrich` that doesn't carry a valid, unexpired HMAC token
(`common/policy_auth.py`) minted by the policy engine after a spend clears.
Verified live: a direct `curl` with no token gets 403 before ever seeing a
402; a tampered or wrong-action token gets 403; the real policy-engine flow
still works (the header survives the x402 client's internal 402-retry).
**Honest scope**: this is a shared static secret on local disk, not network
isolation. A production deployment would put the resource server on a
network only the policy engine can reach (or use mTLS) rather than trust a
secret both processes happen to read off the same disk. It closes "pay the
resource server directly with your own funded wallet," not every possible
bypass of a determined attacker with filesystem access.

**2. Agent identity wasn't cryptographically verified.** Mostly fixed.
`POST /spend` now requires a signature over `(agent_id, action, amount_usd,
timestamp, nonce)`, verified with `algosdk.util.verify_bytes` against the
agent's known Algorand address (`common/identity.py`). Replay-guarded (a
signature can't be reused) and TTL-bounded (60s). Verified live: signing
with the wrong agent's key, tampering the amount after signing, and
replaying a valid signature are all rejected with distinct, correct
reasons; the real approval flow still settles real payments.
**Honest scope**: in this demo the policy engine already holds every
agent's private key custodially (that's how it signs their payments) --
there's no separate process where each agent independently holds a key
nobody else touches. `agents/simulate.py` signs for real with key material
it legitimately has local access to, the same way a genuine autonomous
agent process would. The dashboard's Fire buttons are a human operator, not
a separate cryptographic identity, so they go through `POST /admin/sign` --
a clearly-labeled convenience endpoint using the same custodial keys,
explicitly not something a real external agent would ever call. What this
proves: `/spend` rejects any caller outside this system who doesn't possess
an agent's private key. What it doesn't prove: that the dashboard operator
*is* the agent -- it isn't, and isn't claiming to be.

**3. The x402 facilitator is a single point of failure.** Not fixed --
deliberately. The `x402-avm` SDK does support self-hosting a facilitator
(`x402.mechanisms.avm.exact.ExactAvmFacilitatorScheme`), so a local
facilitator with GoPlausible's hosted one as fallback was a real option.
Decided against it: it doesn't actually eliminate the SPOF, it just moves
it to a process running on the same machine as everything else here, for
the cost of a 5th funded Algorand account and meaningfully more moving
parts. Genuine redundancy needs independently-hosted infrastructure, which
is out of scope for a hackathon demo. If `facilitator.goplausible.xyz` is
down, every payment in this system fails -- there is no failover.

Observed directly while testing this: the public facilitator and AlgoNode
testnet endpoints occasionally show multi-second, sometimes 30+ second,
latency (confirmed independently with plain `curl` against AlgoNode's
`/v2/status` -- not an artifact of this codebase). One `/spend` call timed
out mid-payment during testing; it correctly resolved to `denied` rather
than getting stuck (the reservation-leak fix earlier in this session
holding up under a real failure, not just a synthetic one) but it's a
concrete example of what this SPOF costs: a slow-but-not-actually-broken
external dependency looks identical to a real denial from the caller's
side. The policy engine's outbound timeout is 60s to give genuinely slow
(rather than dead) round trips room to complete instead of manufacturing
false denials, but that's a mitigation, not a fix -- it just moves where
the line is.

**4. `policy.json` needed a restart to take effect.** Fixed.
`policy_engine/policy_store.py` checks the file's mtime on every read and
reloads if it changed -- hand-edit the file and it takes effect on the next
request. `PATCH /admin/agents/{agent_id}` (body:
`{"per_request_limit_usd"?, "daily_cap_usd"?, "allowed_actions"?}`) edits
live and persists back to `policy.json` atomically (write-temp-then-rename),
so changes survive a restart too. Verified: a direct file edit and an API
edit are both picked up without touching either running process.

## Verifying it's real

Every approved request returns an Algorand testnet transaction ID and an
explorer link (`https://lora.algokit.io/testnet/transaction/<txid>`). You
can also check any transaction independently against the public indexer:

```bash
curl -s "https://testnet-idx.algonode.cloud/v2/transactions/<txid>" | python3 -m json.tool
```
