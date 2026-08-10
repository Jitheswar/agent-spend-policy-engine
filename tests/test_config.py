"""The configuration layer.

Three claims worth holding down, because each one failing is silent:

  - `.env` is actually read, and the real environment beats it.
  - One NETWORK setting derives the whole profile consistently, from the
    same source the payment layer uses. A system that anchors to testnet
    while settling on mainnet looks like it's working.
  - Mainnet needs a second, explicit opt-in.

Most of these reload the module under a patched environment, because config
resolves at import time -- that's what makes every other module able to read
a plain constant instead of calling getenv on every request.
"""

import importlib
import os
import subprocess
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common import config  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def reload_config(monkeypatch, env: dict, dotenv_path: str | None = None):
    """Re-import config under a controlled environment.

    Every variable config reads is cleared first, so a developer's own
    exported NETWORK or ALGOD_URL can't quietly change what these assert.
    """
    for key in list(os.environ):
        if key in {
            "NETWORK", "ALLOW_MAINNET", "ALGOD_URL", "ALGOD_TOKEN", "INDEXER_URL",
            "USDC_ASA_ID", "EXPLORER_TX_URL", "FACILITATOR_URL", "DASHBOARD_PORT",
            "DASHBOARD_ORIGINS", "AUTO_ANCHOR_THRESHOLD", "ASPE_DISABLE_ANCHOR",
            "SEC_USER_AGENT", "DATA_DIR", "ACCOUNTS_PATH",
            "ADMIN_TOKEN", "ADMIN_TOKEN_PATH", "ALLOW_DEMO_ENDPOINTS",
        }:
            monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    if dotenv_path is not None:
        monkeypatch.setattr(config, "ENV_PATH", dotenv_path)
        monkeypatch.setenv("__ASPE_TEST_DOTENV", dotenv_path)

    module = importlib.reload(config)
    if dotenv_path is not None:
        # reload() re-reads the real ENV_PATH; re-point and re-resolve the
        # file layer explicitly so the temp .env is what's under test.
        module._FILE_ENV = module._load_dotenv(dotenv_path)
    return module


@pytest.fixture(autouse=True)
def restore_config():
    """Leave the module as we found it -- other test files import it."""
    yield
    importlib.reload(config)


# ---------------------------------------------------------------------------
# .env loading
# ---------------------------------------------------------------------------


def test_dotenv_parses_the_boring_subset(tmp_path):
    path = tmp_path / ".env"
    path.write_text(textwrap.dedent("""
        # a comment
        NETWORK=testnet
        QUOTED="has spaces"
        SINGLE='single quoted'
        SPACED  =   padded

        NOT_A_PAIR
    """).strip())

    values = config._load_dotenv(str(path))

    assert values["NETWORK"] == "testnet"
    assert values["QUOTED"] == "has spaces"
    assert values["SINGLE"] == "single quoted"
    assert values["SPACED"] == "padded"
    assert "NOT_A_PAIR" not in values
    assert not any(k.startswith("#") for k in values)


def test_a_missing_dotenv_is_not_an_error():
    """Cloning the repo and running it must work with no .env at all."""
    assert config._load_dotenv("/nonexistent/path/.env") == {}


def test_the_real_environment_beats_the_file(monkeypatch, tmp_path):
    """`NETWORK=mainnet ./start.sh` has to win over a committed default, or
    CI and one-off overrides need a file edit."""
    path = tmp_path / ".env"
    path.write_text("SEC_USER_AGENT=from-the-file\n")

    module = reload_config(monkeypatch, {"SEC_USER_AGENT": "from-the-shell"}, str(path))

    assert module.env("SEC_USER_AGENT") == "from-the-shell"


def test_the_file_is_used_when_the_shell_is_silent(monkeypatch, tmp_path):
    path = tmp_path / ".env"
    path.write_text("SEC_USER_AGENT=from-the-file\n")

    module = reload_config(monkeypatch, {}, str(path))

    assert module.env("SEC_USER_AGENT") == "from-the-file"


# ---------------------------------------------------------------------------
# Network profiles
# ---------------------------------------------------------------------------


def test_testnet_profile_is_internally_consistent(monkeypatch):
    module = reload_config(monkeypatch, {"NETWORK": "testnet"})

    assert "testnet" in module.ALGOD_URL
    assert "testnet" in module.INDEXER_URL
    assert "testnet" in module.EXPLORER_TX_URL
    assert module.USDC_ASA_ID == 10458941
    assert module.IS_MAINNET is False


def test_mainnet_profile_is_internally_consistent(monkeypatch):
    """The failure this rules out: paying on one chain while anchoring and
    linking to another. Every endpoint has to move together."""
    module = reload_config(monkeypatch, {"NETWORK": "mainnet", "ALLOW_MAINNET": "true"})

    assert "mainnet" in module.ALGOD_URL
    assert "mainnet" in module.INDEXER_URL
    assert "mainnet" in module.EXPLORER_TX_URL
    assert module.USDC_ASA_ID == 31566704
    assert module.IS_MAINNET is True


def test_the_profile_comes_from_the_payment_library(monkeypatch):
    """Derived from x402's own network config rather than a second copy of
    the same constants -- that library is what actually builds and settles
    the payment, so a divergence between the two would be unfixable by
    inspection."""
    from x402.mechanisms.avm import get_network_config

    module = reload_config(monkeypatch, {"NETWORK": "testnet"})
    upstream = get_network_config(module.CAIP2)

    assert module.ALGOD_URL == upstream["algod_url"]
    assert module.INDEXER_URL == upstream["indexer_url"]
    assert module.USDC_ASA_ID == upstream["default_asset"]["asa_id"]


def test_an_endpoint_can_still_be_overridden_individually(monkeypatch):
    module = reload_config(
        monkeypatch, {"NETWORK": "testnet", "ALGOD_URL": "http://my-node:8080"}
    )

    assert module.ALGOD_URL == "http://my-node:8080"
    assert "testnet" in module.INDEXER_URL, "an override must not disturb the rest"


def test_an_unknown_network_fails_loudly(monkeypatch):
    with pytest.raises(SystemExit) as excinfo:
        reload_config(monkeypatch, {"NETWORK": "localnet"})

    assert "testnet" in str(excinfo.value)


def test_cors_origins_follow_the_dashboard_port(monkeypatch):
    """Otherwise changing the port breaks the browser with a preflight
    failure while curl keeps working, which sends you looking in entirely
    the wrong place."""
    module = reload_config(monkeypatch, {"DASHBOARD_PORT": "9999"})

    assert "http://127.0.0.1:9999" in module.DASHBOARD_ORIGINS
    assert "http://localhost:9999" in module.DASHBOARD_ORIGINS


def test_explorer_url_returns_nothing_for_no_transaction():
    """A denial has no transaction, and a link to nowhere is worse than no
    link."""
    assert config.explorer_url(None) is None
    assert config.explorer_url("ABC123").endswith("ABC123")


# ---------------------------------------------------------------------------
# The mainnet guard
# ---------------------------------------------------------------------------


def test_mainnet_refuses_to_start_without_an_explicit_opt_in(monkeypatch):
    """Real funds shouldn't be reachable by a typo in a config file."""
    with pytest.raises(SystemExit) as excinfo:
        reload_config(monkeypatch, {"NETWORK": "mainnet"})

    message = str(excinfo.value)
    assert "ALLOW_MAINNET" in message
    assert "real funds" in message.lower()


def test_mainnet_refuses_to_run_the_admin_plane_unauthenticated(monkeypatch):
    """An empty ADMIN_TOKEN is a legitimate setting -- it means "no auth on
    /admin", which is defensible on a throwaway testnet box. Combined with
    real funds it means anyone who can reach the port controls the kill
    switch and the approval queue, so the two settings are refused together
    rather than left to be discovered."""
    with pytest.raises(SystemExit) as excinfo:
        reload_config(
            monkeypatch, {"NETWORK": "mainnet", "ALLOW_MAINNET": "true", "ADMIN_TOKEN": ""}
        )

    assert "ADMIN_TOKEN" in str(excinfo.value)


def test_demo_endpoints_default_off_on_mainnet(monkeypatch):
    """/admin/sign signs as any agent. Leaving it on where funds are real
    would make the identity guarantee decorative for anyone holding the
    admin token, so mainnet has to opt into it separately."""
    testnet = reload_config(monkeypatch, {"NETWORK": "testnet"})
    assert testnet.ALLOW_DEMO_ENDPOINTS is True

    mainnet = reload_config(
        monkeypatch, {"NETWORK": "mainnet", "ALLOW_MAINNET": "true", "ADMIN_TOKEN": "s3cret"}
    )
    assert mainnet.ALLOW_DEMO_ENDPOINTS is False


def test_an_admin_token_is_generated_and_reused(monkeypatch, tmp_path):
    """A security control that ships off because switching it on is a chore
    is a security control nobody has. With nothing configured, one is minted
    and persisted -- and the same one comes back next time, so a restart
    doesn't invalidate the dashboard's copy mid-demo."""
    token_path = tmp_path / "admin_token.txt"
    module = reload_config(monkeypatch, {"ADMIN_TOKEN_PATH": str(token_path)})

    first = module.admin_token()
    assert first and len(first) >= 32
    assert module.admin_auth_enabled() is True

    module._admin_token_cache = None  # force a re-read from disk
    assert module.admin_token() == first
    assert token_path.read_text().strip() == first


def test_importing_config_does_not_mint_a_token(monkeypatch, tmp_path):
    """Resolved lazily on purpose: importing a config module must not have
    the side effect of writing a file, or `python3 -m common.config` and
    every test reload would scatter secrets around."""
    token_path = tmp_path / "admin_token.txt"
    reload_config(monkeypatch, {"ADMIN_TOKEN_PATH": str(token_path)})

    assert not token_path.exists()


def test_the_guard_holds_in_a_fresh_interpreter():
    """Belt and braces: the reload-based tests above could in principle be
    fooled by module state, and this one is the guard that matters."""
    result = subprocess.run(
        [sys.executable, "-m", "common.config"],
        cwd=ROOT,
        env={**os.environ, "NETWORK": "mainnet", "ALLOW_MAINNET": ""},
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "ALLOW_MAINNET" in (result.stdout + result.stderr)


# ---------------------------------------------------------------------------
# No stale duplicates
# ---------------------------------------------------------------------------


def test_no_module_carries_its_own_copy_of_a_network_constant():
    """The explorer URL used to be hardcoded in two files with no override.
    Constants that exist twice drift, and the drift is invisible until
    something links to the wrong chain.
    """
    offenders = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [
            d for d in dirnames
            if d not in {".venv", ".git", "__pycache__", ".pytest_cache", "tests"}
        ]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            if os.path.abspath(path) == os.path.abspath(config.__file__):
                continue
            body = open(path).read()
            for needle in ("algonode.cloud", "lora.algokit.io", "10458941", "31566704"):
                # A mention in prose is fine; a value being assigned is not.
                for line in body.splitlines():
                    if needle in line and "#" not in line.split(needle)[0]:
                        offenders.append(f"{os.path.relpath(path, ROOT)}: {line.strip()}")

    assert not offenders, "network constants outside common/config.py:\n" + "\n".join(offenders)


def test_env_example_documents_every_knob():
    """A knob nobody can discover is a knob nobody uses.

    Scans the WHOLE repository, not just common/config.py. It used to scan
    only that one module, which meant a `config.env("…")` call anywhere else
    was undocumented and undetected -- scripts/phase1_client.py had two of
    them. The guarantee this test is cited for is "every setting is written
    down", and that has to mean every setting.
    """
    example = open(os.path.join(ROOT, ".env.example")).read()

    referenced: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [
            d for d in dirnames if d not in {".venv", ".git", "__pycache__", ".pytest_cache", "tests"}
        ]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            for line in open(os.path.join(dirpath, name)).read().splitlines():
                for marker in ('env("', 'env_bool("'):
                    if marker in line and "def " not in line:
                        referenced.add(line.split(marker, 1)[1].split('"', 1)[0])

    missing = sorted(name for name in referenced if name not in example)
    assert not missing, f"undocumented in .env.example: {', '.join(missing)}"
