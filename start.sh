#!/usr/bin/env bash
# One-command bootstrap + launch for the Agent Spend Policy Engine.
#
# Safe to re-run: creates the venv/accounts only if missing, skips opt-in for
# accounts that are already opted in or still unfunded, and always (re)starts
# the three services fresh.
#
#   ./start.sh
#
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "==> Creating virtualenv..."
  python3 -m venv .venv
fi
source .venv/bin/activate

echo "==> Installing dependencies..."
pip install -q -r requirements.txt

if [ ! -f data/accounts.json ]; then
  echo "==> Generating Algorand testnet accounts..."
  python3 scripts/setup_accounts.py generate
  echo ""
  echo "!!! New accounts need testnet funding before payments will work."
  echo "!!! See README.md (Algorand dispenser for ALGO, faucet.circle.com for USDC)."
  echo ""
fi

echo "==> Checking account balances..."
python3 scripts/setup_accounts.py balances

echo "==> Opting funded accounts into testnet USDC (skips unfunded/already-done)..."
python3 scripts/setup_accounts.py optin

echo "==> Stopping any previous instances..."
pkill -f "uvicorn resource_server.main:app" 2>/dev/null || true
pkill -f "uvicorn policy_engine.app:app" 2>/dev/null || true
pkill -f "http.server 4023" 2>/dev/null || true
sleep 1

echo "==> Starting resource server on :4021 ..."
nohup uvicorn resource_server.main:app --host 127.0.0.1 --port 4021 > /tmp/resource_server.log 2>&1 &
RS_PID=$!

echo "==> Starting policy engine on :4022 ..."
nohup uvicorn policy_engine.app:app --host 127.0.0.1 --port 4022 > /tmp/policy_engine.log 2>&1 &
PE_PID=$!

echo "==> Starting dashboard on :4023 ..."
(cd dashboard && exec python3 -m http.server 4023) > /tmp/dashboard.log 2>&1 &
DASH_PID=$!

trap "echo; echo Stopping...; kill $RS_PID $PE_PID $DASH_PID 2>/dev/null; true" EXIT INT TERM

sleep 2
echo ""
echo "Resource server : http://127.0.0.1:4021/health"
echo "Policy engine    : http://127.0.0.1:4022/health"
echo "Dashboard        : http://127.0.0.1:4023/index.html"
echo ""
echo "Logs: /tmp/resource_server.log /tmp/policy_engine.log /tmp/dashboard.log"
echo "Ctrl+C to stop all three."
echo ""

wait $RS_PID $PE_PID $DASH_PID
