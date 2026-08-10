"""Every knob in one place, resolved from one NETWORK setting.

Before this, configuration was thirteen `os.getenv` calls spread across six
modules, each carrying its own inline default. Three concrete problems came
out of that, and this module exists to fix all three:

1. **Nothing loaded `.env`.** It was in .gitignore, implying it worked, and
   it didn't -- every knob had to be exported into the shell, every session.

2. **The network was smeared.** ALGOD_URL, INDEXER_URL, USDC_ASA_ID and the
   facilitator each defaulted to testnet *independently*, and the explorer
   URL was hardcoded twice with no override at all. Switching networks meant
   setting four variables consistently and editing two files, and getting it
   half-right gave you a system that paid on one chain and linked to
   explorers on another -- which looks like it works.

3. **No way to see what you're pointed at.** `summary()` exists so start.sh
   can print the resolved configuration before anything spends money.

The network profile is derived from `x402.mechanisms.avm`'s own network
config rather than a second copy of the same constants. That library is what
actually builds and settles the payments, so deriving from it means the
infrastructure this code talks to and the chain the payment layer targets
cannot disagree -- the failure mode where anchoring writes to testnet while
payments settle on mainnet is unrepresentable rather than merely unlikely.

Only `testnet` and `mainnet` are offered. Localnet is deliberately absent:
settlement goes through a public x402 facilitator, and no facilitator on the
internet can reach a chain running on your laptop, so a localnet profile
would configure a system that cannot complete a payment.
"""

import os
import sys

from x402.mechanisms.avm import (
    ALGORAND_MAINNET_CAIP2,
    ALGORAND_TESTNET_CAIP2,
    get_network_config,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(ROOT, ".env")


def _load_dotenv(path: str = ENV_PATH) -> dict[str, str]:
    """Minimal `.env` reader: `KEY=VALUE`, `#` comments, optional quotes.

    Hand-rolled rather than adding python-dotenv, in keeping with the rest
    of this project -- it's twenty lines and the format we need is the
    boring subset. No interpolation, no `export` prefixes, no multiline
    values; anything fancier belongs in a real secrets manager, not here.

    Values already present in the real environment WIN over the file. That
    precedence is the standard one and it matters: it's what lets
    `NETWORK=mainnet ./start.sh` or a CI variable override a committed
    developer default without editing anything on disk.
    """
    values: dict[str, str] = {}
    if not os.path.exists(path):
        return values

    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key:
                values[key] = value
    return values


_FILE_ENV = _load_dotenv()


def env(name: str, default: str | None = None) -> str | None:
    """Real environment first, then .env, then the default."""
    if name in os.environ:
        return os.environ[name]
    if name in _FILE_ENV:
        return _FILE_ENV[name]
    return default


def env_bool(name: str, default: bool = False) -> bool:
    value = env(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Network profile
# ---------------------------------------------------------------------------

NETWORK = (env("NETWORK", "testnet") or "testnet").strip().lower()

_CAIP2_BY_NETWORK = {
    "testnet": ALGORAND_TESTNET_CAIP2,
    "mainnet": ALGORAND_MAINNET_CAIP2,
}

# The only pieces the x402 library doesn't carry, so the only ones kept
# here. Everything else below comes out of get_network_config().
_EXPLORER_BY_NETWORK = {
    "testnet": "https://lora.algokit.io/testnet/transaction/{}",
    "mainnet": "https://lora.algokit.io/mainnet/transaction/{}",
}

# Where a human goes to fund an account. Network-scoped for the same reason
# everything else here is: telling a mainnet operator to visit a testnet
# dispenser is the same class of mistake as anchoring to the wrong chain,
# and an empty list is the honest answer for mainnet -- there is no faucet
# for money that's worth something.
_FAUCETS_BY_NETWORK = {
    "testnet": [
        ("ALGO (fees + minimum balance)", "https://lora.algokit.io/testnet/fund"),
        ("USDC (what payments move)", "https://faucet.circle.com  -> Algorand testnet"),
    ],
    "mainnet": [],
}

if NETWORK not in _CAIP2_BY_NETWORK:
    raise SystemExit(
        f"NETWORK='{NETWORK}' is not supported. Use 'testnet' or 'mainnet'.\n"
        "Localnet isn't offered: payments settle through a public x402 "
        "facilitator, which cannot reach a chain running on your machine."
    )

CAIP2 = _CAIP2_BY_NETWORK[NETWORK]
_PROFILE = get_network_config(CAIP2)

IS_MAINNET = NETWORK == "mainnet"

# Real money, and this system still has no authentication on its admin
# routes (README "Known limitations" #8) -- anything that can reach the
# policy engine's port can unfreeze an agent or release a held spend. That
# combination shouldn't be reachable by a typo in a config file, so mainnet
# takes a second, explicit opt-in.
if IS_MAINNET and not env_bool("ALLOW_MAINNET"):
    raise SystemExit(
        "NETWORK=mainnet moves real funds, and this system has no auth on its\n"
        "/admin routes -- anyone who can reach its port can release a held\n"
        "spend or unfreeze an agent. If you have read that and still mean it,\n"
        "set ALLOW_MAINNET=true alongside it."
    )

ALGOD_URL = env("ALGOD_URL", _PROFILE["algod_url"])
INDEXER_URL = env("INDEXER_URL", _PROFILE["indexer_url"])
ALGOD_TOKEN = env("ALGOD_TOKEN", "")
USDC_ASA_ID = int(env("USDC_ASA_ID", str(_PROFILE["default_asset"]["asa_id"])))
EXPLORER_TX_URL = env("EXPLORER_TX_URL", _EXPLORER_BY_NETWORK[NETWORK])
FAUCETS = _FAUCETS_BY_NETWORK[NETWORK]

FACILITATOR_URL = env("FACILITATOR_URL", "https://facilitator.goplausible.xyz")


def explorer_url(tx_id: str | None) -> str | None:
    """Nothing to link to is a legitimate outcome (a denial has no
    transaction), so this returns None rather than a link to nowhere."""
    return EXPLORER_TX_URL.format(tx_id) if tx_id else None


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

RESOURCE_SERVER_PORT = int(env("RESOURCE_SERVER_PORT", "4021"))
POLICY_ENGINE_PORT = int(env("POLICY_ENGINE_PORT", "4022"))
DASHBOARD_PORT = int(env("DASHBOARD_PORT", "4023"))
BIND_HOST = env("BIND_HOST", "127.0.0.1")

RESOURCE_SERVER_URL = env("RESOURCE_SERVER_URL", f"http://127.0.0.1:{RESOURCE_SERVER_PORT}")
POLICY_ENGINE_URL = env("POLICY_ENGINE_URL", f"http://127.0.0.1:{POLICY_ENGINE_PORT}")

# Only the dashboard's own origin needs cross-origin access to the engine.
# Derived from DASHBOARD_PORT so changing the port doesn't silently break
# the browser with a CORS preflight failure while curl keeps working -- a
# failure that sends you looking in entirely the wrong place.
DASHBOARD_ORIGINS = [
    origin.strip()
    for origin in (
        env("DASHBOARD_ORIGINS")
        or f"http://127.0.0.1:{DASHBOARD_PORT},http://localhost:{DASHBOARD_PORT}"
    ).split(",")
    if origin.strip()
]

# How long the engine waits on the resource server. Covers the upstream
# fetch AND the facilitator round trip, so it's generous by design; see the
# note on UPSTREAM_TIMEOUT_SECONDS in resource_server/upstreams.py.
OUTBOUND_TIMEOUT_SECONDS = int(env("OUTBOUND_TIMEOUT_SECONDS", "60"))


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DATA_DIR = env("DATA_DIR", os.path.join(ROOT, "data"))
ACCOUNTS_PATH = env("ACCOUNTS_PATH", os.path.join(DATA_DIR, "accounts.json"))
DB_PATH = env("DB_PATH", os.path.join(DATA_DIR, "policy_engine.db"))
POLICY_PATH = env("POLICY_PATH", os.path.join(ROOT, "policy_engine", "policy.json"))
POLICY_AUTH_SECRET_PATH = env(
    "POLICY_AUTH_SECRET_PATH", os.path.join(DATA_DIR, "policy_auth_secret.txt")
)
SETUP_VERIFIED_PATH = os.path.join(DATA_DIR, ".setup_verified")


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------

AUTO_ANCHOR_THRESHOLD = int(env("AUTO_ANCHOR_THRESHOLD", "8"))
ANCHOR_ACCOUNT = env("ANCHOR_ACCOUNT", "server")

PROVISION_ALGO = float(env("PROVISION_ALGO", "0.5"))
PROVISION_USDC = float(env("PROVISION_USDC", "0.5"))

SEC_USER_AGENT = env("SEC_USER_AGENT", "agent-spend-policy-engine/0.1 (contact@example.com)")
UPSTREAM_TIMEOUT_SECONDS = int(env("UPSTREAM_TIMEOUT_SECONDS", "10"))


def anchoring_disabled() -> bool:
    """Read at call time, not import time, so a test conftest can set it
    without caring about import order (see tests/conftest.py)."""
    return env_bool("ASPE_DISABLE_ANCHOR")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def summary() -> list[str]:
    """What this process is actually pointed at, for start.sh to print.

    Worth showing unprompted rather than on request: the expensive mistake
    here isn't a setting you can't find, it's one you never thought to check
    because everything looked like it was working.
    """
    return [
        f"network      : {NETWORK}" + ("  ** REAL FUNDS **" if IS_MAINNET else "  (test funds)"),
        f"algod        : {ALGOD_URL}",
        f"indexer      : {INDEXER_URL}",
        f"paying asset : USDC (ASA {USDC_ASA_ID})",
        f"facilitator  : {FACILITATOR_URL}",
        f"accounts     : {ACCOUNTS_PATH}",
        f"config from  : {ENV_PATH if os.path.exists(ENV_PATH) else '(no .env -- using defaults)'}",
    ]


if __name__ == "__main__":
    print("\n".join(summary()))
    sys.exit(0)
