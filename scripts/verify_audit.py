"""Independently verify the audit ledger against Algorand testnet.

Run this to check, without trusting the dashboard or the running policy
engine, that the audit trail hasn't been edited:

    python3 scripts/verify_audit.py

It reads `data/policy_engine.db` directly (no HTTP, the services don't even
need to be running), recomputes every hash in the chain from scratch, and
then checks each anchor's hash against what's actually recorded in the
Algorand block -- fetched from the public AlgoNode indexer, which nobody
running this system can write to.

Exit status is 0 only if the chain is intact AND at least one anchor was
confirmed against the chain. Anything else is non-zero, so this drops
straight into CI or a cron job.

    --json      machine-readable output
    --no-chain  skip the network round trips (local hash check only)
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common import anchor  # noqa: E402
from policy_engine import storage  # noqa: E402

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)


def colour(text: str, code: str) -> str:
    return text if not sys.stdout.isatty() else f"{code}{text}{RESET}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--no-chain", action="store_true", help="skip the on-chain checks")
    args = parser.parse_args()

    if not os.path.exists(storage.DB_PATH):
        print(f"No audit database at {storage.DB_PATH}. Nothing to verify yet.")
        return 2

    report = anchor.integrity_report(check_onchain=not args.no_chain, anchor_limit=50)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        chain = report["chain"]
        print(f"\n{colour('AUDIT LEDGER', BOLD)}  {colour(storage.DB_PATH, DIM)}")
        print(f"  events        : {chain['event_count']}")
        print(f"  head          : seq {chain['head_seq']}  {colour(str(chain['head_hash']), DIM)}")

        mark = colour("PASS", GREEN) if chain["ok"] else colour("FAIL", RED)
        print(f"  hash chain    : {mark}  {chain['detail']}")

        print(f"\n{colour('ON-CHAIN ANCHORS', BOLD)}  ({len(report['anchors'])} checked)")
        if not report["anchors"]:
            print(colour("  none yet -- run POST /audit/anchor to notarize the current head", YELLOW))
        for a in report["anchors"]:
            if a["ok"] and a["onchain_match"]:
                mark = colour("VERIFIED", GREEN)
            elif a["ok"]:
                mark = colour("PENDING ", YELLOW)
            else:
                mark = colour("BROKEN  ", RED)
            print(f"  {mark} seq {a['seq']:<6} tx {a['tx_id'][:16]}…")
            print(f"           {colour(a['detail'], DIM)}")
            if a.get("explorer_url"):
                print(f"           {colour(a['explorer_url'], DIM)}")

        verdict_colour = {"verified": GREEN, "unanchored": YELLOW, "tampered": RED}[report["verdict"]]
        print(f"\n  {colour('VERDICT: ' + report['verdict'].upper(), verdict_colour + BOLD)}")
        print(f"  {report['detail']}\n")

    if report["verdict"] == "tampered":
        return 1
    if report["verdict"] == "unanchored":
        # Not a failure of integrity -- the chain is fine -- but it isn't
        # *proven* to anyone who doesn't already trust this database, which
        # is the entire claim. Distinct exit code so a caller can tell
        # "compromised" from "not yet notarized"; they warrant very
        # different reactions.
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
