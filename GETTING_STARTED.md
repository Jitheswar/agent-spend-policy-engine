# Getting started

From `git clone` to an autonomous agent paying for a real API call on
Algorand, with a policy engine deciding whether it's allowed to.

**Time:** about 15 minutes, most of it waiting on a faucet.
**Cost:** nothing. Everything runs on Algorand *testnet* — the ALGO and USDC
are free and worthless, and the system refuses to start on mainnet unless
you explicitly opt in.

If you'd rather read *why* any of this exists before running it, start with
[README.md](README.md). If you want to demo it to someone, [DEMO.md](DEMO.md)
is a scripted five-minute walkthrough.

---

## What you're about to run

Three local services and a fleet of software agents that spend money:

| | Port | What it is |
|---|---|---|
| **Dashboard** | 4023 | Where you watch and control everything. Start here. |
| **Policy engine** | 4022 | Decides whether each spend is allowed, *before* any payment |
| **Resource server** | 4021 | Sells two real APIs behind an x402 paywall |

The agents call paid APIs. Every call goes through the policy engine first,
which checks nine rules and only then lets a real USDC payment settle on
Algorand. Every decision — allowed or blocked — lands in a hash-chained
audit ledger that gets notarized on-chain, so nobody, including you, can
quietly rewrite what happened.

---

## Step 1 · Prerequisites

- **Python 3.10 or newer** (`python3 --version`)
- **git**
- A **browser** — for the dashboard, and for two faucets that need a human
- An internet connection: payments settle on a public network, and the APIs
  behind the paywall are real

No Docker, no database to install, no API keys.

```bash
python3 --version    # must be 3.10+
```

<details>
<summary>If your Python is older than 3.10</summary>

The codebase uses `X | None` type syntax and the `x402-avm` SDK requires
3.10+. Install a newer Python (`pyenv install 3.12`, your package manager,
or python.org) and make sure `python3` points at it before continuing.
</details>

---

## Step 2 · Clone and start

```bash
git clone <your-fork-url> blockhack
cd blockhack
./start.sh
```

`start.sh` is safe to re-run as often as you like — it only creates what's
missing. On the first run it will:

1. Create a virtualenv in `.venv/` and install dependencies
2. Print the configuration it resolved (see Step 3)
3. Generate four Algorand accounts into `data/accounts.json`
4. Tell you they're empty and need funding
5. Start all three services anyway

You'll see something like:

```
==> Configuration
    network      : testnet  (test funds)
    algod        : https://testnet-api.algonode.cloud
    indexer      : https://testnet-idx.algonode.cloud
    paying asset : USDC (ASA 10458941)
    facilitator  : https://facilitator.goplausible.xyz
    accounts     : /path/to/blockhack/data/accounts.json
    config from  : (no .env -- using defaults)

==> Generating Algorand accounts...
Accounts stored at /path/to/blockhack/data/accounts.json

[server]           F4QA6Q3G...
[agent_weather]    7I26GWUA...
[agent_enrichment] MK7CI6KP...
[agent_rogue]      6WV74ZEL...
```

**Keep those addresses visible — Step 4 needs them.**

At the end it prints the three URLs and waits. `Ctrl+C` stops all three
services.

> **`data/accounts.json` holds real private keys** (testnet ones). It's
> gitignored and must stay that way. Don't paste its contents anywhere, and
> don't reuse those keys for anything that matters.

---

## Step 3 · Configuration (optional — skip it the first time)

There is nothing you *have* to configure. Every setting has a working
default and the defaults run against testnet.

When you do want to change something:

```bash
cp .env.example .env
```

`.env.example` lists every knob with its default and an explanation.
The one that matters is `NETWORK` (`testnet` or `mainnet`) — it derives the
node, the indexer, the USDC asset, the chain id and the explorer links
*together*, so they can't end up pointing at different chains.

Check what you're actually pointed at, any time:

```bash
python3 -m common.config
```

Real environment variables beat the file, so you can override for one run
without editing anything:

```bash
NETWORK=testnet AUTO_ANCHOR_THRESHOLD=3 ./start.sh
```

**One setting worth doing properly if you'll use `/enrich` much:** SEC asks
API callers to identify themselves and throttles the ones that don't. In
`.env`:

```
SEC_USER_AGENT=your-project/1.0 (you@example.com)
```

---

## Step 4 · Fund the accounts

**This is the one step that can't be automated**, because both faucets use a
captcha. It's also where people get stuck, so here it is in full.

### What each account needs, and why

| Account | ALGO | USDC | Why |
|---|---|---|---|
| `server` | yes | yes | Receives every payment. Pays anchoring fees in ALGO, and funds newly onboarded agents out of its own USDC. |
| `agent_weather` | yes | yes | Pays for its API calls |
| `agent_enrichment` | yes | yes | Pays for its API calls |
| `agent_rogue` | yes | yes | Pays for its API calls (until its 2¢ cap stops it, on purpose) |

Every account needs **both**. The ALGO isn't the payment — it covers
transaction fees and Algorand's minimum-balance requirement. The USDC is
what actually moves when an agent buys an API call.

Rough minimums: **~0.3 ALGO** per account (0.2 is locked as minimum balance
once it holds USDC, the rest is fees at 0.001 each), and any amount of USDC
— the whole demo spends a few cents.

### 4a · Get ALGO

Go to **https://lora.algokit.io/testnet/fund** and request ALGO for each of
the four addresses. (The old `bank.testnet.algorand.network` URL still works
— it redirects here.)

### 4b · Get USDC

Go to **https://faucet.circle.com**, choose **Algorand testnet** in the
network dropdown, and request for each of the four addresses. You get 20
USDC per address every 2 hours, which is enormously more than you need.

### 4c · Opt in to USDC

Algorand accounts must explicitly opt in before they can *receive* an asset.
Once each address has ALGO:

```bash
python3 scripts/setup_accounts.py optin
```

```
[server]           opted in to USDC-testnet, txid=...
[agent_weather]    opted in to USDC-testnet, txid=...
```

If you requested USDC before opting in, the faucet request may have failed —
just request it again after opting in.

### 4d · Confirm

```bash
python3 scripts/setup_accounts.py balances
```

```
[server              ] F4QA6Q3G...  |     2.968 ALGO  |  2.15 USDC
[agent_weather       ] 7I26GWUA...  |     1.999 ALGO  |  19.26 USDC
[agent_enrichment    ] MK7CI6KP...  |     1.999 ALGO  |  18.20 USDC
[agent_rogue         ] 6WV74ZEL...  |     1.999 ALGO  |  19.42 USDC
```

**You're done when every row shows both a non-zero ALGO balance and a USDC
figure** (not `NOT opted in`).

Restart with `./start.sh` and it'll record that setup is verified, skipping
these network checks on later runs. Use `./start.sh --recheck` to force them
again — e.g. after re-funding.

---

## Step 5 · Your first paid call

Open **http://127.0.0.1:4023/index.html**.

You'll see a card per agent. On **Weather Bot**, click **Fire: weather**.

Two seconds later:

- A row appears in the feed: agent, action, the city it fetched, the amount,
  `approved`, and a transaction id
- **Click the transaction id.** It opens a real Algorand block explorer
  showing a real USDC transfer

That request was signed with the agent's own Algorand key, checked against
nine policy rules, paid for on-chain, and served with live weather data
from Open-Meteo. Nothing in that path is simulated.

### The same thing from the command line

```bash
python3 agents/simulate.py once
```

This fires a curated sequence designed to show both outcomes:

```
[APPROVED] agent_weather    -> weather  (city=Reykjavik)  approved: weather within policy
           tx: https://lora.algokit.io/testnet/transaction/OVAIW2GS...
[APPROVED] agent_enrichment -> enrich   (company=NVDA)     approved: enrich within policy
           tx: https://lora.algokit.io/testnet/transaction/EVJIZ2QX...
[DENIED ] agent_weather    -> enrich   (company=Apple)    agent 'agent_weather' is not approved for action 'enrich'
[APPROVED] agent_rogue      -> weather  (city=Lagos)       approved: weather within policy
[APPROVED] agent_rogue      -> weather  (city=Osaka)       approved: weather within policy
[DENIED ] agent_rogue      -> weather  (city=Quito)       $0.02 spent today + $0.01 would exceed daily cap $0.02
[DENIED ] agent_rogue      -> enrich   (company=Tesla)     agent 'agent_rogue' is not approved for action 'enrich'
[DENIED ] agent_weather    -> weather  (planet=Mars)       invalid call parameters: unknown parameter(s): planet
[DENIED ] agent_ghost      -> weather  (city=Oslo)         unknown agent 'agent_ghost'
```

Every denial happened *before* any payment was attempted. There's nothing to
refund because nothing was ever charged.

---

## Step 6 · Check that it's real

Don't take the dashboard's word for it. Two independent checks:

**A single payment**, against a public indexer that has nothing to do with
this codebase:

```bash
curl -s "https://testnet-idx.algonode.cloud/v2/transactions/<txid>" | python3 -m json.tool
```

**The entire audit ledger**, including whether it's been tampered with:

```bash
python3 scripts/verify_audit.py
```

This reads the database directly (services don't need to be running),
recomputes every hash in the chain, then fetches each on-chain anchor's note
back off the public indexer and compares. Exit code 0 only if the ledger is
intact *and* confirmed on-chain.

```
  VERDICT: VERIFIED
  seq 219 matches the hash recorded in Algorand block transaction DDBM5S6MVB...
```

Then break it on purpose: click **Tamper with a record** in the dashboard,
run `verify_audit.py` again, and watch it name the exact record that was
edited. **Restore** puts it back.

---

## Step 7 · Make it yours

### Add an agent while it's running

Click **+ Onboard an agent** in the dashboard, set its limits, and click
**Onboard & fund**. About fifteen seconds later it has its own Algorand
keypair, is funded out of the treasury, is opted into USDC, and is paying
for API calls. No restart, no config file.

This draws 0.5 ALGO and 0.5 USDC from `server` each time — it fails visibly
if the treasury is empty.

### Change what an agent may do

Edit `policy_engine/policy.json` and save. It hot-reloads; the next request
uses the new rules. Or from the dashboard, or:

```bash
curl -X PATCH http://127.0.0.1:4022/admin/agents/agent_rogue \
  -H "Authorization: Bearer $(python3 -m common.config --admin-token)" \
  -H 'Content-Type: application/json' -d '{"daily_cap_usd": 0.10}'
```

Every `/admin` route needs that header — they are all authority over money,
so none of them is reachable by anything that can merely open a socket to
the port. The token is generated on first run into `data/admin_token.txt`
and `start.sh` hands it to the dashboard, so the buttons in the UI just
work; you only need it for `curl`. Set `ADMIN_TOKEN` in `.env` to use your
own, or `ADMIN_TOKEN=` (empty) to turn authentication off on a throwaway
box. A 401 from one of these routes means the header is missing or stale —
re-run `start.sh` if the dashboard is the one getting it.

### Add a new paid API

Three places:

1. **`resource_server/upstreams.py`** — a `fetch_*` function for the real
   upstream. Raise `UpstreamError(status, code, message)` on failure; do
   *not* return a 200 with an error body, or the agent gets charged for a
   call that didn't deliver.
2. **`resource_server/main.py`** — a route calling it through `_serve()`,
   plus its price in the `routes` dict and its path in `PROTECTED_ACTIONS`.
3. **`policy_engine/policy.json`** — an entry under `actions` with
   `resource_path`, `price_usd`, and the `params` it accepts.

### Run the tests

```bash
python3 -m pytest tests/ -v
```

183 tests, no network needed — upstreams are mocked and anchoring is
disabled, so a test run never spends anything or calls a vendor.

---

## Troubleshooting

Keyed to the message you'll actually see.

<details>
<summary><code>balance too low to opt in, fund with ALGO first</code></summary>

That account has under 0.2 ALGO. Opting into an asset raises Algorand's
minimum-balance requirement, so it needs ALGO *before* it can hold USDC.
Fund it at https://lora.algokit.io/testnet/fund and re-run
`python3 scripts/setup_accounts.py optin`.
</details>

<details>
<summary><code>NOT opted in</code> in the balances output</summary>

Run `python3 scripts/setup_accounts.py optin`. If it skips the account, it
needs ALGO first (see above).
</details>

<details>
<summary><code>couldn't reach the network to check</code></summary>

The public AlgoNode endpoint is slow or briefly down — this is about
reaching the node, **not** about your account being unfunded. Wait a moment
and re-run. Don't re-fund an account over this.
</details>

<details>
<summary><code>x402 payment did not settle</code> / <code>settlement_failed</code></summary>

The payment leg broke. In order of likelihood:

1. **The agent is out of USDC or ALGO.** Check
   `python3 scripts/setup_accounts.py balances`.
2. **The agent isn't opted into USDC.** Same command; look for `NOT opted in`.
3. **The facilitator is down.** `https://facilitator.goplausible.xyz` is a
   public service and a genuine single point of failure — if it's
   unreachable, every payment fails. Check `curl -sI https://facilitator.goplausible.xyz`.
</details>

<details>
<summary><code>upstream_not_found</code> / <code>upstream_unavailable</code></summary>

**Not a payment problem, and it cost you nothing.** The payment rail was
fine; the API behind the paywall either had no record of what you asked for
(a city or company that doesn't exist) or is down. Payment settles only
after the upstream returns successfully, so no USDC moved. Verify that
yourself with `balances` before and after.

For `/enrich`, the company must be an SEC filer — "Acme Corp" will 404,
"Apple" or "NVDA" won't.
</details>

<details>
<summary><code>invalid call parameters: unknown parameter(s)</code></summary>

Each action declares the arguments it accepts in `policy_engine/policy.json`
under `params`. Anything undeclared is denied rather than forwarded. Add the
parameter to the action's schema, or fix the caller.
</details>

<details>
<summary><code>identity verification failed</code></summary>

The request's signature doesn't match the agent's key. Usually one of:

- **Params changed after signing.** The signature covers them; sign the
  exact params you send.
- **Clock skew.** Signatures expire after 60 seconds. Check your system time.
- **Replay.** Each nonce works once. Generate a fresh signature per request.
</details>

<details>
<summary><code>velocity limit hit</code></summary>

Working as intended — the agent exceeded its requests-per-minute. Wait 60
seconds, or raise `max_requests_per_minute` for that agent in policy.json.
This is easy to trip while testing in a loop.
</details>

<details>
<summary><code>agent 'X' is frozen</code></summary>

Someone hit the kill switch. Unfreeze from the dashboard, or set
`"frozen": false` for that agent in `policy_engine/policy.json`.
</details>

<details>
<summary><code>missing policy authorization</code> (403 from :4021)</summary>

You called the resource server directly instead of going through the policy
engine. That's the gate working — the resource server only serves requests
carrying a token the engine mints after a spend clears. Send spends to
`POST :4022/spend` instead.
</details>

<details>
<summary>The dashboard is blank or says it can't reach the engine</summary>

1. Is the engine up? `curl http://127.0.0.1:4022/health`
2. If you changed `DASHBOARD_PORT`, the browser origin must match what the
   engine allows. It follows the port automatically — but if you set
   `DASHBOARD_ORIGINS` by hand, make sure it lists the origin you're
   actually loading. A CORS preflight failure makes buttons silently do
   nothing while `curl` keeps working.
3. Hard-reload the page (Ctrl+Shift+R).
</details>

<details>
<summary><code>address already in use</code></summary>

Something is still on 4021/4022/4023 — probably a previous run.

```bash
pkill -f "uvicorn resource_server.main"
pkill -f "uvicorn policy_engine.app"
pkill -f "dashboard/serve.py"
```

Or change the ports in `.env` (`RESOURCE_SERVER_PORT`, etc.).
</details>

<details>
<summary><code>NETWORK=mainnet moves real funds</code> — it refuses to start</summary>

Deliberate — real funds shouldn't be reachable by a typo. Set
`ALLOW_MAINNET=true` alongside it.

Two other things change on mainnet, and both will stop you if they aren't
right. `ADMIN_TOKEN` may not be explicitly empty: an unauthenticated kill
switch next to money that's worth something is refused rather than warned
about. And `ALLOW_DEMO_ENDPOINTS` defaults to off, which disables
`/admin/sign` — so the dashboard's Fire buttons stop working, because they
depend on the engine signing on an agent's behalf. That's correct: on
mainnet a real agent signs for itself. Read limitations #2 and #8 in the
README before you set any of it.
</details>

<details>
<summary>SEC EDGAR requests failing or rate-limited</summary>

Set `SEC_USER_AGENT` in `.env` to something identifying you with a contact
address. SEC's fair-access policy throttles generic user agents.
</details>

<details>
<summary>Everything is slow</summary>

Normal, and mostly not this code. A single approved spend is: policy engine
→ resource server → upstream API → facilitator → Algorand → back. The public
testnet endpoints have been observed taking 30+ seconds under load. The
outbound timeout is 60s deliberately, to avoid turning slow-but-working
payments into false denials.
</details>

---

## Stopping and resetting

**Stop the services:** `Ctrl+C` in the terminal running `start.sh`.

**Clear the request feed** for a fresh demo: the **Reset log** button, or
`curl -X POST http://127.0.0.1:4022/admin/reset -H "Authorization: Bearer
$(python3 -m common.config --admin-token)"`. This deliberately does
*not* clear the audit ledger — the reset itself is recorded as an event in
it. An audit trail a button can erase wouldn't be one.

**Start completely over:**

```bash
rm -rf data/ .venv/
./start.sh
```

This destroys your accounts' private keys along with everything else. Any
testnet funds in them become unrecoverable — which costs nothing, but means
re-funding from the faucets.

---

## Where to go next

- **[README.md](README.md)** — why this needs a blockchain, the full control
  list, architecture, and an honest account of what's still missing
- **[DEMO.md](DEMO.md)** — a scripted five-minute walkthrough for showing
  someone else
- **[.env.example](.env.example)** — every configuration knob, documented
