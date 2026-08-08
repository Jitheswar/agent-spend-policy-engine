"""Tests for onboarding a new agent at runtime (policy_store.add_agent /
remove_agent, and the account-creation half of common/provisioning).

Nothing here touches the network. The funding path in provisioning.provision()
is three real Algorand transactions and is proven by actually running it (see
DEMO.md); what's tested here is everything that decides *whether an agent is
governed*, which is the part a regression would quietly break:

  - a newly added agent lands in policy with its caps, and persists;
  - adding one can't silently clobber an existing agent's limits;
  - an agent can't be created with permissions for an action nobody defined;
  - key generation is idempotent, so re-onboarding never strands the funds
    of an account that already exists.
"""

import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common import provisioning  # noqa: E402
from policy_engine import policy_store  # noqa: E402


@pytest.fixture
def temp_policy(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".json")
    initial = {
        "actions": {"weather": {"resource_path": "/weather", "price_usd": 0.01}},
        "agents": {
            "agent_weather": {
                "display_name": "Weather Bot",
                "allowed_actions": ["weather"],
                "per_request_limit_usd": 0.02,
                "daily_cap_usd": 1.0,
            }
        },
    }
    with os.fdopen(fd, "w") as f:
        json.dump(initial, f)
    monkeypatch.setattr(policy_store, "POLICY_PATH", path)
    monkeypatch.setattr(policy_store, "_cache", {"mtime": None, "data": None})
    yield path
    os.remove(path)


@pytest.fixture
def temp_accounts(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump({"server": {"address": "SERVER", "mnemonic": "x", "avm_private_key_b64": "y"}}, f)
    monkeypatch.setattr(provisioning, "ACCOUNTS_PATH", path)
    yield path
    os.remove(path)


def test_new_agent_is_governed_from_the_moment_it_exists(temp_policy):
    """The whole point of live onboarding: the agent arrives already carrying
    limits, not in some permissive interim state."""
    policy_store.add_agent(
        "agent_new",
        "New Bot",
        allowed_actions=["weather"],
        per_request_limit_usd=0.02,
        daily_cap_usd=0.10,
    )
    cfg = policy_store.get_policy()["agents"]["agent_new"]
    assert cfg["daily_cap_usd"] == 0.10
    assert cfg["per_request_limit_usd"] == 0.02
    assert cfg["allowed_actions"] == ["weather"]
    # Defaults must be the permissive-but-present ones, not missing keys --
    # /spend reads .get("frozen") and friends on every request.
    assert cfg["frozen"] is False
    assert "require_approval_above_usd" in cfg


def test_new_agent_persists_to_disk(temp_policy):
    """It has to survive a restart, or "added live" means "added until the
    next deploy" -- which is the thing this feature exists to fix."""
    policy_store.add_agent("agent_new", "New Bot", allowed_actions=[], daily_cap_usd=0.5)
    with open(temp_policy) as f:
        on_disk = json.load(f)
    assert on_disk["agents"]["agent_new"]["daily_cap_usd"] == 0.5


def test_adding_an_existing_id_is_refused(temp_policy):
    """Silently overwriting would mean a "create" call could reset a live
    agent's caps to something looser without anyone seeing it happen."""
    with pytest.raises(ValueError, match="already exists"):
        policy_store.add_agent("agent_weather", "Impostor", daily_cap_usd=999.0)
    assert policy_store.get_policy()["agents"]["agent_weather"]["daily_cap_usd"] == 1.0


def test_cannot_set_non_policy_fields(temp_policy):
    with pytest.raises(ValueError, match="not settable"):
        policy_store.add_agent("agent_new", "New Bot", some_made_up_field=True)


def test_remove_agent(temp_policy):
    policy_store.add_agent("agent_new", "New Bot", allowed_actions=[])
    policy_store.remove_agent("agent_new")
    assert "agent_new" not in policy_store.get_policy()["agents"]
    with pytest.raises(KeyError):
        policy_store.remove_agent("agent_new")


def test_ensure_account_generates_once_and_then_reuses(temp_accounts):
    """Re-onboarding the same id must return the SAME address. Generating a
    fresh keypair instead would strand whatever testnet funds the previous
    account held, and would do it invisibly."""
    address, created = provisioning.ensure_account("agent_new")
    assert created is True
    assert len(address) == 58  # Algorand addresses are 58 chars

    again, created_again = provisioning.ensure_account("agent_new")
    assert created_again is False
    assert again == address


def test_generated_key_matches_the_address_it_claims(temp_accounts):
    """The stored private key must actually control the stored address --
    otherwise /spend's identity check rejects every request this agent makes,
    and the failure looks like a signing bug rather than an onboarding one."""
    import base64

    import algosdk

    address, _ = provisioning.ensure_account("agent_new")
    with open(temp_accounts) as f:
        stored = json.load(f)["agent_new"]

    sk_bytes = base64.b64decode(stored["avm_private_key_b64"])
    assert algosdk.encoding.encode_address(sk_bytes[32:]) == address
    assert algosdk.account.address_from_private_key(stored["avm_private_key_b64"]) == address
