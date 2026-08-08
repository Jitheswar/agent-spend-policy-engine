# Agent Spend Policy Engine

Autonomous agents try to spend money on paid APIs. A policy engine decides
whether each spend is allowed *before* any payment is attempted. Approved
spends settle for real on **Algorand testnet** via the **x402** protocol
(HTTP 402 → pay → retry → 200). Every decision, allowed or blocked, is
written to an **append-only hash-chained audit ledger whose head is
notarized on Algorand** — so the record of what your agents did can be
proven unedited by someone who doesn't trust you.

```bash
./start.sh          # then open http://127.0.0.1:4023/index.html
```

## Why this needs a blockchain

Worth answering directly, because "we used a chain" is not a reason.

Two independent things here are on-chain, and only one of them is about
payments:

**1. Settlement.** Agents pay per API call in testnet USDC through x402.
Micropayments between software that has no prior relationship, no invoice,
and no account is a genuinely awkward fit for card rails. That part is
real, but it isn't the interesting part — plenty of projects stop here.

**2. The audit trail itself.** This is the part that actually needs a
chain. A spend-governance system's whole product is its record: *this
agent was allowed to spend this, this one was stopped.* If that record
lives only in the operator's database, then the operator can rewrite it,
and every guarantee the system offers is worth exactly as much as your
trust in whoever runs it. That's the failure mode governance software has
to not have.

So every decision is appended to a hash-chained ledger, and the chain head
is periodically written into the note field of an Algorand transaction.
After that, rewriting local history produces a ledger that disagrees with a
hash sitting in a block nobody involved can edit — and anyone can check
that themselves, against a public indexer, without trusting this codebase
or the machine it runs on.

That claim is checkable in about ten seconds:

```bash
python3 scripts/verify_audit.py
```

It reads the database directly (the services don't need to be running),
recomputes every hash, then fetches each anchor's note back off the public
AlgoNode indexer and compares. Exit code 0 only if the chain is intact
**and** confirmed on-chain.

There's also a button in the dashboard — *Tamper with a record* — that
doctors a past audit entry the way a malicious operator would, recomputing
that entry's own hash so the edit is internally consistent. Verification
still catches it, and names the exact record.

## x402 + Algorand: what already existed

Before writing any code we checked whether Algorand support existed. It
does, so we used it rather than rebuilding it:

- **GoPlausible** runs a public, live x402 facilitator with first-class
  Algorand support: `https://facilitator.goplausible.xyz`. It verifies and
  settles on Algorand testnet/mainnet (and Base, Solana).
- There's an official Python SDK on PyPI, **`x402-avm`**, with FastAPI
  server middleware and requests/httpx client integration. Algorand's x402
  spec extension is merged into Coinbase's x402 repo
  ([scheme_exact_algo.md](https://github.com/coinbase/x402/blob/main/specs/schemes/exact/scheme_exact_algo.md)).

We did **not** build a custom facilitator. The "exact" scheme pays in
testnet USDC (an ASA, id `10458941`), not raw ALGO — that's the standard
x402 pattern (stable pricing per call).

## Architecture

```
 Mock agents (signed)        Dashboard (human operator)
         |                            |
         |  POST /spend               |  approve / reject / freeze / notarize
         v                            v
 ┌─────────────────────────────────────────────────┐
 │  Policy Engine  :4022                           │
 │                                                 │
 │  identity → kill switch → allowed action →      │
 │  per-request limit → velocity → daily cap →     │
 │  human-approval threshold                       │
 │                                                 │
 │  every outcome ─┬─> requests    (operational)   │
 │                 └─> audit_events (append-only,  │
 │                      hash-chained)              │
 └───────┬─────────────────────────────┬───────────┘
         │ only if approved            │ chain head, periodically
         v                             v
 ┌──────────────────────┐      ┌──────────────────────┐
 │ Resource Server :4021│      │  Algorand testnet    │
 │  policy-auth gate    │      │  note: ASPE1|seq|hash│
 │  x402 middleware     │      └──────────▲───────────┘
 └───────┬──────────────┘                 │
         │ verify() / settle()            │ independently readable
         v                                │
   GoPlausible facilitator ───> Algorand ─┘
```

- **`policy_engine/`** — the enforcement layer. `POST /spend` runs the
  check chain above and only then calls the x402 payment loop. A denial
  never reaches the resource server, so no payment is ever attempted for a
  blocked request. `policy_store.py` hot-reloads `policy.json` on mtime
  change and persists live edits back to it atomically.
- **`policy_engine/storage.py`** — two tables, two jobs. `requests` is the
  mutable operational view the dashboard reads. `audit_events` is the
  append-only hash-chained ledger; nothing in the codebase updates or
  deletes a row in it, including `/admin/reset`, which appends a
  `audit.reset` event instead of truncating.
- **`common/anchor.py`** — notarizes the chain head onto Algorand as a
  0-ALGO self-payment carrying `ASPE1|<seq>|<hash>` in its note. Runs
  automatically once enough events accumulate, on a background thread so no
  `/spend` call ever waits on it.
- **`resource_server/`** — FastAPI app selling `/weather` ($0.01) and
  `/enrich` ($0.05) behind two layers: `PolicyAuthMiddleware` (rejects
  anything without a valid token from the policy engine) and the x402
  payment middleware. It knows nothing about spend policy; it just refuses
  to serve anyone the engine hasn't cleared.
- **`common/identity.py`** — signs/verifies `/spend` requests with the
  agent's own Algorand key (`algosdk.util.sign_bytes`/`verify_bytes`),
  domain-separated from transaction signing. Replay-guarded (nonce + 60s
  window).
- **`common/policy_auth.py`** — the shared-secret HMAC token the engine
  mints per approved spend and the resource server verifies.
- **`agents/simulate.py`** — fires curated, repeatable sequences of
  *signed* requests, using key material it has legitimate local access to.
- **`agents/llm_agent.py`** — a real LLM (DeepSeek) doing tool calls, where
  every tool is a governed spend. See below.
- **`common/provisioning.py`** — onboards a new agent at runtime: keypair,
  ALGO for fees, USDC opt-in, working capital, all on-chain.
- **`dashboard/`** — static HTML/JS mission control.

## The controls

Every check below runs on every `/spend`, in this order. Order matters and
is tested: identity is verified before any policy statement about an agent
is evaluated, and the kill switch is checked before every budget rule.

| Check | Denial code | What it's for |
|---|---|---|
| Known agent / known action | `unknown_agent`, `unknown_action` | Nothing unregistered spends |
| **Cryptographic identity** | `identity_failed` | Caller controls that agent's Algorand key. Replay-guarded, 60s TTL |
| **Kill switch** | `frozen` | Stop an agent *now*, regardless of remaining budget |
| Allowed action | `action_not_allowed` | This agent may call this API at all |
| Per-request limit | `over_per_request_limit` | Bound any single call |
| **Velocity** | `over_velocity_limit` | Bound the *rate*. A daily cap doesn't stop a retry loop burning it in seconds |
| Daily cap | `over_daily_cap` | Bound the day. Atomically reserved, so concurrent calls can't both slip under |
| **Human approval threshold** | `awaiting_approval` | Above this, policy alone can't authorize — a person must release it |

The last one parks the request holding its budget, so a queued approval
can't be double-spent against while it waits. Freezing an agent while a
hold is queued blocks the release too — otherwise the kill switch would
have a queue-shaped hole in it.

Per-agent config lives in `policy_engine/policy.json` and is editable live
(`PATCH /admin/agents/{id}`, or just edit the file — it hot-reloads).

## Setup + run

```bash
./start.sh
```

One command: creates the venv, installs dependencies, generates the four
Algorand testnet accounts (`data/accounts.json`, gitignored, testnet-only)
if missing, checks/opts them into testnet USDC, then starts all three
services — resource server (`:4021`), policy engine (`:4022`), dashboard
(`:4023`). Safe to re-run; it only creates what's missing.

Once every account is confirmed funded and opted in, a `data/.setup_verified`
marker is written and later runs skip the on-chain balance/opt-in checks —
those are real network round-trips against public AlgoNode, occasionally
multiple seconds each. Run `./start.sh --recheck` (or delete
`data/.setup_verified`) to force them again — e.g. after re-funding an
account.

### Funding the accounts (one-time, needs a human)

The first run generates accounts with zero balance; payments fail until
funded:

1. **ALGO** (for fees/min-balance, and for anchoring): the official
   Algorand TestNet Dispenser at https://bank.testnet.algorand.network/
   (Google login + captcha, so a human has to do this).
2. **Testnet USDC** (what payments actually happen in): **faucet.circle.com**
   → Algorand testnet → request for each `agent_*` address. The `server`
   address needs ALGO (it pays anchoring fees) but never needs USDC.

Re-run `./start.sh` after funding. Or check manually:

```bash
python3 scripts/setup_accounts.py balances
```

Fire scenarios from the CLI instead of the dashboard:

```bash
python3 agents/simulate.py once       # one pass through the curated scenarios
python3 agents/simulate.py loop 4     # repeat every 4s
python3 agents/simulate.py burst 25   # runaway agent -- trips the velocity limiter
```

See **[DEMO.md](DEMO.md)** for a five-minute walkthrough script.

## Adding an agent while it's running

The fleet isn't a fixed list compiled into the demo. **+ Onboard an agent**
in the dashboard adds one live — no restart, no config file, no redeploy —
and about fifteen seconds later it is paying for API calls on Algorand.

Three things have to be true before a new agent is real, and they happen in
this order for a reason:

1. **It gets an Algorand keypair.** Without its own key it cannot sign a
   `/spend` request, so the identity check rejects it. There is no path by
   which a keyless agent spends anything.
2. **It's registered in policy with caps** — per-call limit, daily cap,
   rate limit, allowed actions. This happens *before* any funding, so there
   is never a window in which an agent can pay for something no policy
   covers.
3. **Its account is funded and opted into USDC on-chain**, in the
   background: ALGO for fees, then a USDC opt-in signed by the new agent
   itself (Algorand requires an explicit opt-in before an account can
   receive an asset), then working capital. Three real transactions, funded
   from the server treasury — no faucet, no captcha, no human in the loop.

During step 3 the agent is registered and governed but genuinely cannot
settle a payment, so its card says which step it's on and its Fire buttons
stay disabled. An agent that looks ready and then fails to pay is a worse
demo, and a worse system, than one that says what it's doing.

Same thing from the CLI:

```bash
curl -X POST http://127.0.0.1:4022/admin/agents -H 'Content-Type: application/json' \
  -d '{"agent_id":"agent_deepseek","display_name":"DeepSeek Agent",
       "allowed_actions":["weather"],"per_request_limit_usd":0.02,"daily_cap_usd":0.10}'
```

`DELETE /admin/agents/{id}` deregisters one. Its key stays in
`data/accounts.json` and its history stays in the audit ledger, deliberately:
the account may hold testnet funds, and deleting an agent must not be a way
to make what it already spent disappear from the record. Re-onboarding the
same id reuses the same account rather than stranding it.

Both operations are audit events (`agent.registered`, `agent.provisioned`,
`agent.deregistered`), so they're inside the hash chain and get anchored on
Algorand like everything else — *who added an agent, with what limits, and
when* is exactly the kind of thing an operator would otherwise be able to
rewrite.

**One thing to check before demoing this:** each onboard draws 0.5 ALGO and
0.5 USDC out of the `server` account (tune with `PROVISION_ALGO` /
`PROVISION_USDC`). Onboarding fails, visibly, if the treasury is empty.

```bash
python3 scripts/setup_accounts.py balances
```

## A real LLM agent under the policy

`agents/simulate.py` fires requests a human picked in advance. That proves
the engine works; it doesn't prove an *autonomous* agent is actually
constrained by it. `agents/llm_agent.py` closes that gap: a real DeepSeek
model, given ordinary tools, deciding for itself what to call.

```bash
export DEEPSEEK_API_KEY=sk-...
python3 agents/llm_agent.py "Get me an enrichment profile on Acme Corp, and the weather."
```

The model gets two tools — `get_weather`, `enrich_company` — and nothing
else. It has no Algorand key, never sees the resource server, and doesn't
know x402 exists. Each tool call is routed through a signed `POST /spend`,
and the value returned to the model is whatever the policy engine decided:

| Decision | What the model receives |
|---|---|
| approved | the real API response, paid for on-chain, with the tx id |
| denied | the denial reason, verbatim |
| awaiting_approval | nothing — the agent **blocks** until a human clicks approve or reject in the dashboard |

Both tools are always offered, whatever identity you run as. That's
deliberate: the model is never prevented from *trying*, so anything that
stops it is the policy engine and not this script. Running the command
above as the default `agent_weather` — which is approved for weather only —
is the demo worth watching: the enrichment call comes back
`not approved for action 'enrich'`, that denial lands in the conversation
as a tool result, and the model rewrites its plan and reports what it
couldn't get. The governance layer is inside the agent's loop, not wrapped
around it.

```bash
python3 agents/llm_agent.py --agent agent_rogue "Check the weather a few times."
```

`agent_rogue` has a $0.02 daily cap, so the model spends twice, gets cut
off mid-task, and has to say so. Every spend and denial above is in the
same audit ledger, anchored on Algorand, and `scripts/verify_audit.py`
covers them exactly like any other.

DeepSeek is used because its API is OpenAI-compatible, so this is plain
`requests` against the standard chat-completions shape and adds no
dependency. Point `DEEPSEEK_BASE_URL`/`DEEPSEEK_MODEL` at any compatible
endpoint to swap the model out.

## Tests

```bash
pytest tests/ -v
```

75 tests, no network required (anchoring is disabled process-wide in
`tests/conftest.py`, so a unit-test run never submits a transaction).

- `test_policy_engine.py` — policy decisions, the daily-cap concurrency fix
  (12 simultaneous reservations against a cap with room for 2, asserts
  exactly 2 succeed), reservation-leak handling, and signed-identity denial
  paths at the HTTP layer.
- `test_audit_ledger.py` — the hash chain accepts an honest ledger and
  rejects a doctored one, including the *competent* tamper where the
  attacker recomputes the edited entry's own hash. Also covers the case
  that justifies anchoring at all: a fully-rewritten chain that verifies
  locally and still fails against its anchor.
- `test_governance.py` — kill switch, velocity limiting, approval holds
  (budget held while queued, freed on reject, blocked if the agent is
  frozen mid-queue, not approvable twice).
- `test_identity.py` — request signing/verification: wrong-key
  impersonation, tampered amount/action, expired timestamp, replay,
  malformed signature.
- `test_policy_auth.py` — resource-server token: wrong action, tampered
  signature, expired, malformed.
- `test_policy_store.py` — hot-reload on file edit, live `PATCH` persists.

Doesn't cover the approval path end-to-end (that needs a live facilitator +
funded testnet accounts — see `scripts/phase1_client.py` for that proof).

## Known limitations

Honest accounting. Some are fixed, some are deliberate tradeoffs, and one
is inherent.

**1. Resource server could be called directly, bypassing policy.** Fixed.
`PolicyAuthMiddleware` rejects (403) any request to `/weather` or `/enrich`
without a valid, unexpired HMAC token minted by the policy engine after a
spend clears. Verified live: a direct `curl` with no token gets 403 before
ever seeing a 402; a tampered or wrong-action token gets 403; the real flow
still works (the header survives the x402 client's internal 402-retry).
**Scope:** this is a shared static secret on local disk, not network
isolation. Production would put the resource server on a network only the
engine can reach, or use mTLS. It closes "pay the resource server directly
with your own funded wallet", not every bypass available to an attacker
with filesystem access.

**2. Agent identity wasn't cryptographically verified.** Mostly fixed.
`/spend` requires a signature over `(agent_id, action, amount_usd,
timestamp, nonce)`, verified with `algosdk.util.verify_bytes` against the
agent's known address. Replay-guarded and TTL-bounded.
**Scope:** in this demo the engine already holds every agent's key
custodially (that's how it signs their payments), so there's no separate
process holding a key nobody else touches. `agents/simulate.py` signs for
real with key material it legitimately has, like a genuine agent would. The
dashboard's Fire buttons are a human operator, not a cryptographic
identity, so they go through `POST /admin/sign` — a clearly-labeled
convenience endpoint, explicitly not something a real agent would call.
What this proves: `/spend` rejects any caller who doesn't possess an
agent's private key. What it doesn't: that the dashboard operator *is* the
agent — it isn't, and doesn't claim to be.

**3. A hash chain cannot detect an edit to its own last entry.** Inherent,
not a bug, and worth stating rather than glossing. Nothing follows the head
entry to disagree with it, and a tamperer who recomputes its hash leaves
something wholly self-consistent — `verify_chain()` will pass. Only an
anchor closes this, and only once one exists. That's precisely why
anchoring is automatic rather than manual: the exposure window is "events
appended since the last anchor", bounded by `AUTO_ANCHOR_THRESHOLD`
(default 8). The demo's tamper button deliberately avoids picking the head
entry when anything else is available — triggering the one case the
detector genuinely can't see would be a dishonest demo.

**4. The x402 facilitator is a single point of failure.** Not fixed,
deliberately. `x402-avm` does support self-hosting a facilitator, so a
local one with GoPlausible as fallback was a real option. We decided
against it: it doesn't eliminate the SPOF, it moves it to a process on the
same machine as everything else, for the cost of a fifth funded account and
meaningfully more moving parts. Genuine redundancy needs independently
hosted infrastructure, which is out of scope here. If
`facilitator.goplausible.xyz` is down, every payment fails.

Observed while testing: the public facilitator and AlgoNode endpoints
occasionally show multi-second, sometimes 30+ second latency (confirmed
independently with plain `curl` against AlgoNode's `/v2/status` — not an
artifact of this codebase). One `/spend` timed out mid-payment during
testing and correctly resolved to `denied` rather than getting stuck, but
it's a concrete example of what this SPOF costs: slow-but-alive looks
identical to a real denial from the caller's side. The outbound timeout is
60s to give genuinely slow round trips room instead of manufacturing false
denials — a mitigation, not a fix.

**5. `policy.json` needed a restart to take effect.** Fixed.
`policy_store.py` checks mtime on every read and reloads if changed.
`PATCH /admin/agents/{agent_id}` edits live and persists back atomically
(write-temp-then-rename), so changes survive a restart too.

**6. Anchoring costs a real transaction fee.** 0.001 ALGO per anchor, paid
by the `server` account. At the default threshold that's roughly one
transaction per 8 decisions. A production deployment would batch harder
(anchor every N minutes rather than every N events) and probably commit a
Merkle root over a batch rather than a running chain head.

## Verifying it's real

Every approved request returns an Algorand testnet transaction ID and an
explorer link (`https://lora.algokit.io/testnet/transaction/<txid>`). Check
any of them independently against the public indexer:

```bash
curl -s "https://testnet-idx.algonode.cloud/v2/transactions/<txid>" | python3 -m json.tool
```

And to read an audit anchor's committed hash straight off the chain:

```bash
curl -s "https://testnet-idx.algonode.cloud/v2/transactions/<anchor_txid>" \
  | python3 -c "import sys,json,base64; print(base64.b64decode(json.load(sys.stdin)['transaction']['note']).decode())"
```

That prints `ASPE1|<seq>|<hash>` — the hash the ledger reported at that
point, recorded in a block, readable by anyone.
