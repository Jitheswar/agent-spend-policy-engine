"""A real LLM agent (DeepSeek) whose every paid API call goes through the policy engine.

agents/simulate.py fires a curated, hand-written sequence of spends. It's a
good deterministic demo, but the requests are ones a human chose in advance.
This is the other half of the claim: an actual autonomous agent, deciding on
its own which paid APIs to call, that cannot spend a cent without the policy
engine agreeing first.

The wiring is the point. The model is given ordinary OpenAI-style tools
(`get_weather`, `enrich_company`). It never touches the resource server, has
no Algorand key, and knows nothing about x402. Every tool call is routed
through POST /spend, signed with the agent's own key, and the tool's return
value is whatever the policy engine hands back:

  - approved            -> the real API response (paid for on-chain), plus tx id
  - denied              -> the denial reason, verbatim, as the tool result
  - awaiting_approval   -> the agent blocks until a human clicks approve/reject

That last detail is what makes this more than a wrapper. A denial isn't an
exception this script swallows -- it goes back into the conversation as a
tool result, so you watch the model read "not approved for action 'enrich'"
and change its plan. The governance layer is inside the agent's loop, not
bolted on around it.

Setup:
    export DEEPSEEK_API_KEY=sk-...

Usage:
    python agents/llm_agent.py "What's the weather? Then enrich Acme Corp."
    python agents/llm_agent.py --agent agent_rogue "Check the weather 5 times."
    python agents/llm_agent.py            # uses the default demo task below

Notes:
  - --agent picks which registered identity the model runs as (default
    agent_weather). It changes what the model is ALLOWED to do, not what
    tools it's offered: both tools are always on the table, so the policy
    engine -- not this file -- is what stops anything. Running as
    agent_weather and asking for enrichment is the best 20 seconds of the
    demo.
  - No new SDK: DeepSeek's API is OpenAI-compatible, so this is plain
    `requests` against the same JSON shape, and adds no dependency.
"""

import argparse
import json
import os
import sys
import time

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common.avm_client import agent_secret_key_b64, load_accounts  # noqa: E402
from common.identity import sign_request  # noqa: E402

POLICY_ENGINE_URL = os.getenv("POLICY_ENGINE_URL", "http://127.0.0.1:4022")
DEEPSEEK_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com") + "/chat/completions"
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

DEFAULT_TASK = (
    "I'm considering doing business with Acme Corp. Get me their company "
    "enrichment profile, and also tell me the current weather so I know "
    "whether to fly out. Report back with whatever you were able to get."
)

# Maps a tool the model can call -> the policy engine action it spends on.
# The model sees only the left side; the engine only ever sees the right.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather report. This is a paid API call.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "City name."}
                },
                "required": ["location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "enrich_company",
            "description": (
                "Look up a company enrichment profile (size, industry, risk score). "
                "This is a paid API call and costs more than the weather API."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "Company name."}
                },
                "required": ["company"],
            },
        },
    },
]

TOOL_TO_ACTION = {"get_weather": "weather", "enrich_company": "enrich"}

SYSTEM_PROMPT = (
    "You are an autonomous agent with a budget. Your tools cost real money per "
    "call and are governed by a spend policy engine that can deny a call. "
    "If a tool returns a denial, do not retry it -- read the reason, adapt, and "
    "tell the user plainly what you could not do and why. Be concise."
)

# ANSI, purely so a live demo is readable across a room.
DIM, BOLD, GREEN, RED, YELLOW, CYAN, RESET = (
    "\033[2m", "\033[1m", "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[0m",
)


def call_spend(agent_id: str, action: str, accounts: dict, action_prices: dict) -> dict:
    """One signed POST /spend. Identical path to agents/simulate.py -- the
    agent proves it holds its own key, and the engine decides."""
    amount_usd = action_prices.get(action, 0.0)
    body = {"agent_id": agent_id, "action": action, "amount_usd": amount_usd}
    sk_b64 = agent_secret_key_b64(agent_id, accounts)
    body.update(sign_request(sk_b64, agent_id, action, amount_usd))

    resp = requests.post(f"{POLICY_ENGINE_URL}/spend", json=body, timeout=90)
    resp.raise_for_status()
    return resp.json()


def wait_for_human(request_id: int, poll_seconds: float, timeout_seconds: float) -> dict:
    """Block while a parked spend waits on a human in the dashboard.

    A real agent hitting a human-in-the-loop threshold has to actually stop,
    so this stops -- it doesn't fake the wait or assume approval. Returns the
    final request row; on timeout, returns the row as it stands (still
    awaiting_approval), which the caller reports to the model as a denial.
    """
    deadline = time.time() + timeout_seconds
    print(
        f"    {YELLOW}held for human approval -- approve or reject it in the "
        f"dashboard (waiting up to {int(timeout_seconds)}s){RESET}"
    )
    while time.time() < deadline:
        time.sleep(poll_seconds)
        rows = requests.get(f"{POLICY_ENGINE_URL}/requests", params={"limit": 200}, timeout=15).json()
        row = next((r for r in rows if r["id"] == request_id), None)
        if row is None:
            continue
        if row["decision"] != "awaiting_approval":
            return row
    return {"decision": "awaiting_approval", "reason": "no human decision within the wait window"}


def run_tool(tool_name: str, arguments: dict, agent_id: str, accounts: dict,
             action_prices: dict, args) -> str:
    """Execute one model tool call as a governed spend. The string returned
    here is exactly what the model sees next."""
    action = TOOL_TO_ACTION.get(tool_name)
    if action is None:
        return json.dumps({"status": "error", "reason": f"unknown tool {tool_name}"})

    arg_str = ", ".join(f"{k}={v!r}" for k, v in arguments.items())
    print(f"  {CYAN}→ tool call{RESET} {BOLD}{tool_name}{RESET}({arg_str})  "
          f"{DIM}[${action_prices.get(action, 0.0):.2f} · action '{action}']{RESET}")

    result = call_spend(agent_id, action, accounts, action_prices)
    decision = result["decision"]

    if decision == "awaiting_approval" and result.get("request_id") is not None:
        row = wait_for_human(result["request_id"], args.poll_seconds, args.approval_timeout)
        decision = row["decision"]
        result = {**result, **row}

    if decision == "approved":
        tx = result.get("tx_id")
        print(f"    {GREEN}APPROVED{RESET} {result['reason']}")
        if result.get("explorer_url"):
            print(f"    {DIM}paid on-chain: {result['explorer_url']}{RESET}")
        return json.dumps({
            "status": "approved",
            "data": result.get("resource_data"),
            "paid": True,
            "transaction": tx,
        })

    # Everything else -- denied, still-parked, rejected by a human -- is a
    # refusal from the agent's point of view, and is handed back as one.
    print(f"    {RED}BLOCKED{RESET} {result.get('reason', decision)}")
    return json.dumps({
        "status": "denied",
        "reason": result.get("reason", decision),
        "paid": False,
        "note": "This call was blocked by the spend policy engine. Do not retry it.",
    })


def chat(messages: list, api_key: str) -> dict:
    resp = requests.post(
        DEEPSEEK_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": DEEPSEEK_MODEL, "messages": messages, "tools": TOOLS, "temperature": 0.2},
        timeout=120,
    )
    if resp.status_code != 200:
        raise SystemExit(f"DeepSeek API error {resp.status_code}: {resp.text[:400]}")
    return resp.json()["choices"][0]["message"]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("task", nargs="?", default=DEFAULT_TASK, help="what to ask the agent to do")
    parser.add_argument("--agent", default="agent_weather", help="which registered agent identity to run as")
    parser.add_argument("--max-turns", type=int, default=8, help="safety stop on the tool-calling loop")
    parser.add_argument("--approval-timeout", type=float, default=120.0,
                        help="seconds to wait on a human approval before giving up")
    parser.add_argument("--poll-seconds", type=float, default=2.0, help="approval poll interval")
    args = parser.parse_args()

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise SystemExit("DEEPSEEK_API_KEY is not set. export DEEPSEEK_API_KEY=sk-...")

    accounts = load_accounts()
    if args.agent not in accounts:
        raise SystemExit(f"no key on file for '{args.agent}' -- known: {', '.join(k for k in accounts if k != 'server')}")

    policy = requests.get(f"{POLICY_ENGINE_URL}/policy", timeout=10).json()
    action_prices = {name: cfg["price_usd"] for name, cfg in policy["actions"].items()}
    agent_cfg = policy["agents"].get(args.agent, {})

    print(f"\n{BOLD}Agent{RESET}   {args.agent} ({agent_cfg.get('display_name', '?')})")
    print(f"{BOLD}Policy{RESET}  allowed: {', '.join(agent_cfg.get('allowed_actions', []))} · "
          f"per-request ${agent_cfg.get('per_request_limit_usd', 0):.2f} · "
          f"daily cap ${agent_cfg.get('daily_cap_usd', 0):.2f}")
    print(f"{BOLD}Model{RESET}   {DEEPSEEK_MODEL}")
    print(f"{BOLD}Task{RESET}    {args.task}\n" + "-" * 72)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": args.task},
    ]

    for _ in range(args.max_turns):
        message = chat(messages, api_key)
        messages.append(message)

        if message.get("content"):
            print(f"  {DIM}model:{RESET} {message['content'].strip()}")

        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            break

        for call in tool_calls:
            try:
                arguments = json.loads(call["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                arguments = {}
            output = run_tool(call["function"]["name"], arguments, args.agent,
                              accounts, action_prices, args)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": output})
    else:
        print(f"\n{YELLOW}Stopped at the {args.max_turns}-turn safety limit.{RESET}")

    print("-" * 72)
    print("Every spend above is in the audit ledger. Verify it against Algorand:")
    print("  python3 scripts/verify_audit.py\n")


if __name__ == "__main__":
    main()
