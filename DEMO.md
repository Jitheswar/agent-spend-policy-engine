# Five-minute demo script

Run `./start.sh`, open http://127.0.0.1:4023/index.html, and hit
**Reset log** so you start clean. Keep a terminal visible — one beat of
this demo happens there, on purpose.

Total: about five minutes. The last section is the one that matters; budget
time for it.

---

## 0 · The setup (20 seconds)

> "Three autonomous agents. Each one wants to call paid APIs and pay for
> them itself, per call, in USDC on Algorand. The question isn't whether
> they *can* pay — it's who decides whether they're allowed to, and how
> anyone knows afterwards what actually happened."

Point at the agent fleet. Each card is one agent with its own Algorand
address, its own budget, and its own rules.

The APIs behind the paywall are real, and it's worth saying so early: the
weather comes from Open-Meteo, the company data from SEC EDGAR. Nothing in
the path is stubbed, which is why the feed shows a real city and a real
filer next to each spend.

---

## 0b · Add a fourth agent, live (60 seconds)

Click **+ Onboard an agent**. The form is pre-filled; change nothing and
click **Onboard & fund**.

The card appears immediately, marked *Provisioning*, with its Fire buttons
disabled.

> "That agent didn't exist a second ago. It's already registered with a
> two-cent per-call limit and a ten-cent daily cap — governed before it can
> spend anything, not after. What it's doing now is getting an Algorand
> keypair, some ALGO for fees, and opting itself into USDC, because
> Algorand won't let an account receive an asset it hasn't opted into.
> Three real transactions, funded out of the treasury. No faucet, no human."

Wait for it — about fifteen seconds. The card goes green, the buttons
enable. Now click **Fire: weather** on the new card.

> "Brand-new agent, real Algorand address, paying for a real API call.
> Nothing restarted. And the onboarding itself is in the audit ledger — who
> added this agent and what limits they gave it is exactly the kind of thing
> an operator shouldn't be able to quietly rewrite later."

To re-run the demo: **Deregister agent** on that card. The account and its
history are kept, so onboarding it again is instant the second time — which
is worth knowing before you rehearse and wonder why it didn't pause.

---

## 1 · A normal spend (30 seconds)

Click **Fire: weather** on Weather Bot.

What to say while it settles (~2s):

> "That request was signed with the agent's own Algorand key, checked
> against nine policy rules, and only then paid. The transaction ID in the
> feed is a real Algorand testnet transaction — click it."

Click the tx link. A real block explorer, a real USDC transfer. The row in
the feed names the city it actually fetched, and the conditions that came
back are today's.

---

## 2 · Blocked before any money moves (40 seconds)

Click **Try: enrich (should deny)** on Weather Bot, then **Fire: weather**
on Rogue Bot three times.

> "Rogue Bot has a two-cent daily cap, on purpose. Two calls go through,
> the third is denied. And note *where* it's denied — the resource server
> never sees the third request at all. There's no payment to reverse,
> because none was ever attempted."

---

## 2b · When the vendor fails, the agent doesn't pay (40 seconds)

Optional, and the best answer to "but what if the API is down?". In the
terminal:

```bash
python3 agents/simulate.py once
```

The last two denials in that run are the ones to point at: a request
carrying a parameter no policy declares, and an unregistered agent.

Then ask for somewhere that doesn't exist:

```bash
curl -s -X POST http://127.0.0.1:4022/spend -H 'Content-Type: application/json' \
  -d "$(python3 -c "
import json,sys; sys.path.insert(0,'.')
from common.avm_client import agent_secret_key_b64, load_accounts
from common.identity import sign_request
a=load_accounts(); p={'city':'Xyzzyville'}
b={'agent_id':'agent_weather','action':'weather','amount_usd':0.01,'params':p}
b.update(sign_request(agent_secret_key_b64('agent_weather',a),'agent_weather','weather',0.01,p))
print(json.dumps(b))")"
```

> "Policy said yes — the agent was allowed, in budget, under its rate limit.
> It got as far as the paid API, and the upstream had no such place. Watch
> what it costs: nothing. Payment settles only after the API returns
> something under a 400, so a call that didn't deliver never takes the
> agent's money. And the denial says `upstream_not_found`, not 'settlement
> failed' — because the payment rail was fine, the vendor just didn't have
> it."

---

## 3 · The kill switch (30 seconds)

Click **Freeze agent** on Rogue Bot. The card goes cold and blue. Click
**Fire: weather**.

> "Every other control here is a budget — it bounds how much a misbehaving
> agent spends before someone notices. This is the control for after
> someone notices. It's not waiting for the budget to run out; it stops on
> the next request."

Unfreeze it before moving on.

---

## 4 · The runaway agent (40 seconds)

In the terminal:

```bash
python3 agents/simulate.py burst 25
```

> "This is the failure mode agents actually have. Not 'spends too much on
> one call' — every one of these is inside the per-request limit. It's
> 'gets stuck in a loop and issues a perfectly valid call twenty-five times
> in a row.' A daily cap doesn't stop that; it just gets consumed in
> seconds. The velocity limiter does."

Fifteen approved, the rest denied on rate.

---

## 5 · Human in the loop (40 seconds)

On Enrichment Bot, type `0.08` in the amount box and click **Fire: enrich**.

> "That's above this agent's auto-approval threshold. Policy alone isn't
> allowed to authorize it — so it's parked, holding its budget so nothing
> else can spend the same money while it waits."

The approval queue appears. Click **Approve & pay**. It settles on-chain,
with a transaction link.

---

## 6 · The part that matters (90 seconds)

Point at the **Audit integrity** panel: *Verified on Algorand.*

> "Everything you just watched went into an append-only, hash-chained
> ledger. Each entry commits to the one before it. And the head of that
> chain gets written into the note field of a real Algorand transaction —
> there's the link.
>
> Here's why that's not decoration. A spend-governance system's entire
> product is its record: this agent was allowed, this one was stopped. If
> that record only lives in my database, then I can rewrite it, and every
> guarantee I've just shown you is worth exactly as much as your trust in
> me. That's the one failure mode this kind of software can't have."

Click **Tamper with a record**.

> "So let's say I'm a dishonest operator. There was an eight-cent spend I'd
> rather nobody saw. I go into the database and rewrite it as one cent —
> and I'm not sloppy about it: I recompute that record's own hash so the
> entry is internally consistent. If you audited that row on its own, it
> checks out."

The panel turns red. Read the detail line out loud — it names the exact
entry and both hashes.

> "It doesn't check out, because the next entry in the chain committed to
> what that record *used* to hash to. And even if I'd cascaded the rewrite
> through the entire chain — which I could, it's my database — the hash for
> this point in history is already sitting in an Algorand block, and I
> can't edit that."

Now the close. In the terminal:

```bash
python3 scripts/verify_audit.py
```

> "This doesn't ask my dashboard, and it doesn't ask my API. It reads the
> database directly, recomputes every hash, then pulls each anchor back off
> the public AlgoNode indexer and compares. You don't have to trust the
> demo — you can run this yourself, against the same public chain I'm
> using."

`VERDICT: TAMPERED`, exit code 1.

Click **Undo tamper** to restore, and run it once more:

```bash
python3 scripts/verify_audit.py
```

`VERDICT: VERIFIED`, exit code 0.

---

## If asked

**"Couldn't you just use a database with an audit table?"**
Yes, and it would give you exactly the guarantees your users are willing
to extend to you personally. The chain is what makes the record checkable
by someone who doesn't trust the operator. That's the whole difference.

**"What if the facilitator is down?"**
Every payment fails. It's a real single point of failure and we didn't fix
it — self-hosting a facilitator moves the SPOF onto the same machine as
everything else rather than removing it. README, limitation 4.

**"Can you detect tampering with the most recent entry?"**
Not by hashing alone — nothing follows it to disagree, so a competent edit
is self-consistent. That's inherent to hash chains, not a bug here. The
anchor closes it, which is why anchoring runs automatically every few
events rather than on demand. README, limitation 3.

**"Is the dashboard operator cryptographically the agent?"**
No. The agents sign for real with their own keys — `agents/simulate.py`
does exactly that. The dashboard's buttons are a human operator and go
through a clearly-labeled `/admin/sign` endpoint. README, limitation 2.
