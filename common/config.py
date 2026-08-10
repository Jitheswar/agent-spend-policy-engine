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

# Real money. Mainnet takes a second, explicit opt-in so it isn't reachable
# by a typo in a config file.
if IS_MAINNET and not env_bool("ALLOW_MAINNET"):
    raise SystemExit(
        "NETWORK=mainnet moves real funds. Every /admin route -- freeze,\n"
        "unfreeze, onboard, release a held spend, edit a cap -- is a control\n"
        "over money that is worth something. If you have read that and still\n"
        "mean it, set ALLOW_MAINNET=true alongside it."
    )

# `None` means "no ADMIN_TOKEN was set, mint one and persist it" (see
# admin_token()). An explicitly EMPTY value means "run the admin plane with
# no authentication at all" -- which is a legitimate thing to want on a
# throwaway testnet box, and an indefensible thing to want anywhere real.
_ADMIN_TOKEN_ENV = env("ADMIN_TOKEN")

if IS_MAINNET and _ADMIN_TOKEN_ENV == "":
    raise SystemExit(
        "ADMIN_TOKEN is explicitly empty, which disables authentication on\n"
        "every /admin route -- the kill switch, the approval queue, the caps.\n"
        "That cannot be combined with NETWORK=mainnet. Unset ADMIN_TOKEN to\n"
        "have one generated, or set it to a secret you control."
    )

# The demo-only endpoints: POST /admin/sign (signs a /spend request on any
# agent's behalf) and POST /admin/demo/* (doctors and restores audit
# records). They exist to make the system demonstrable, and /admin/sign in
# particular is a signing oracle -- with it enabled, "only a caller holding
# an agent's private key can spend" is true only of callers who also lack
# the admin token. Default on for testnet, off for mainnet.
ALLOW_DEMO_ENDPOINTS = env_bool("ALLOW_DEMO_ENDPOINTS", not IS_MAINNET)

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
#
# Two other things are derived from it, so raising it moves them too:
# policy_auth.TOKEN_TTL_SECONDS (a token has to outlive the round trip it
# authorizes) and the age at which storage.reap_stale_reservations() calls
# a 'pending' reservation abandoned (nothing in flight can be older).
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
ADMIN_TOKEN_PATH = env("ADMIN_TOKEN_PATH", os.path.join(DATA_DIR, "admin_token.txt"))
SETUP_VERIFIED_PATH = os.path.join(DATA_DIR, ".setup_verified")


# ---------------------------------------------------------------------------
# Admin plane authentication
# ---------------------------------------------------------------------------

_admin_token_cache: str | None = None


def admin_token() -> str:
    """The bearer token every /admin route requires. `""` means disabled.

    Resolved lazily rather than at import, because the fallback branch
    *writes a file* -- and importing a config module should not have that
    side effect (tests reload this module repeatedly, and `python3 -m
    common.config` should be safe to run anywhere).

    Precedence is the usual one: an ADMIN_TOKEN in the environment or .env
    wins. With nothing set, one is generated once and persisted next to the
    other local secrets, so a fresh clone gets a real token without anyone
    having to think about it -- the failure mode to avoid is an auth
    mechanism that ships disabled because turning it on was a chore.
    """
    global _admin_token_cache
    if _ADMIN_TOKEN_ENV is not None:
        return _ADMIN_TOKEN_ENV
    if _admin_token_cache is not None:
        return _admin_token_cache

    if os.path.exists(ADMIN_TOKEN_PATH):
        with open(ADMIN_TOKEN_PATH) as f:
            _admin_token_cache = f.read().strip()
    else:
        import secrets

        token = secrets.token_urlsafe(32)
        os.makedirs(os.path.dirname(ADMIN_TOKEN_PATH), exist_ok=True)
        tmp = ADMIN_TOKEN_PATH + ".tmp"
        with open(tmp, "w") as f:
            f.write(token + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, ADMIN_TOKEN_PATH)
        _admin_token_cache = token
    return _admin_token_cache


def admin_auth_enabled() -> bool:
    return admin_token() != ""


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------

AUTO_ANCHOR_THRESHOLD = int(env("AUTO_ANCHOR_THRESHOLD", "8"))
ANCHOR_ACCOUNT = env("ANCHOR_ACCOUNT", "server")

PROVISION_ALGO = float(env("PROVISION_ALGO", "0.5"))
PROVISION_USDC = float(env("PROVISION_USDC", "0.5"))

SEC_USER_AGENT = env("SEC_USER_AGENT", "agent-spend-policy-engine/0.1 (contact@example.com)")
UPSTREAM_TIMEOUT_SECONDS = int(env("UPSTREAM_TIMEOUT_SECONDS", "10"))

# The window the per-agent rate limit is measured over. Lives here rather
# than as a constant in the policy engine so the limit an operator reasons
# about ("15 per minute") and the window the code enforces can't drift.
VELOCITY_WINDOW_SECONDS = int(env("VELOCITY_WINDOW_SECONDS", "60"))

# Hard ceilings on the call arguments a /spend request may carry. These are
# NOT the governance control -- policy.json's per-action `max_length` is
# (see _resolve_params). These exist because /spend is unauthenticated by
# design (anyone may *attempt* a spend; the signature decides whether it
# succeeds) and every attempt, including a denial, is written into an
# append-only ledger that nothing can delete. Without a ceiling, an
# arbitrary amount of attacker-chosen bytes becomes permanent.
MAX_PARAM_KEYS = int(env("MAX_PARAM_KEYS", "16"))
MAX_PARAM_KEY_LENGTH = int(env("MAX_PARAM_KEY_LENGTH", "64"))
MAX_PARAM_VALUE_LENGTH = int(env("MAX_PARAM_VALUE_LENGTH", "512"))


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
    admin = (
        f"required (token in {ADMIN_TOKEN_PATH})"
        if admin_auth_enabled() and _ADMIN_TOKEN_ENV is None
        else "required (ADMIN_TOKEN from the environment)"
        if admin_auth_enabled()
        else "** DISABLED -- every /admin route is open **"
    )
    return [
        f"network      : {NETWORK}" + ("  ** REAL FUNDS **" if IS_MAINNET else "  (test funds)"),
        f"algod        : {ALGOD_URL}",
        f"indexer      : {INDEXER_URL}",
        f"paying asset : USDC (ASA {USDC_ASA_ID})",
        f"facilitator  : {FACILITATOR_URL}",
        f"accounts     : {ACCOUNTS_PATH}",
        f"admin auth   : {admin}",
        f"demo routes  : {'enabled (/admin/sign, /admin/demo/*)' if ALLOW_DEMO_ENDPOINTS else 'disabled'}",
        f"config from  : {ENV_PATH if os.path.exists(ENV_PATH) else '(no .env -- using defaults)'}",
    ]


# The subset start.sh needs in order to launch the services on the ports
# this module resolved. Emitted as shell assignments so there is exactly one
# resolver: the script used to hardcode 4021/4022/4023 and would happily
# bind ports the rest of the system had been told to stop using.
_EXPORTABLE = (
    "BIND_HOST",
    "RESOURCE_SERVER_PORT",
    "POLICY_ENGINE_PORT",
    "DASHBOARD_PORT",
    "POLICY_ENGINE_URL",
)


def _shell_quote(value: str) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"


if __name__ == "__main__":
    if "--export" in sys.argv:
        for name in _EXPORTABLE:
            print(f"{name}={_shell_quote(globals()[name])}")
    elif "--admin-token" in sys.argv:
        print(admin_token())
    else:
        print("\n".join(summary()))
    sys.exit(0)
