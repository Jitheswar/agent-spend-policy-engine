"""Tests for policy_store's hot-reload and live-edit behavior (#4: policy
limits should take effect without a restart)."""

import json
import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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


def test_hand_edit_takes_effect_without_restart(temp_policy):
    """The literal complaint: policy.json is static, needs a restart. This
    proves an on-disk edit -- not through the app at all, just editing the
    file like an operator would -- is picked up on the next read."""
    before = policy_store.get_policy()
    assert before["agents"]["agent_weather"]["daily_cap_usd"] == 1.0

    with open(temp_policy) as f:
        data = json.load(f)
    data["agents"]["agent_weather"]["daily_cap_usd"] = 5.0
    # Ensure the mtime actually advances on filesystems with coarse
    # (1-second) mtime resolution -- otherwise this write could land in the
    # same mtime bucket as the original and the reload wouldn't trigger,
    # which would be a false negative for this test, not a real bug.
    time.sleep(1.01)
    with open(temp_policy, "w") as f:
        json.dump(data, f)

    after = policy_store.get_policy()
    assert after["agents"]["agent_weather"]["daily_cap_usd"] == 5.0


def test_update_agent_persists_to_disk_and_is_immediately_visible(temp_policy):
    policy_store.update_agent("agent_weather", daily_cap_usd=2.5)

    # Visible immediately through the same process (no restart)...
    assert policy_store.get_policy()["agents"]["agent_weather"]["daily_cap_usd"] == 2.5

    # ...and durably on disk, so it also survives an actual restart.
    with open(temp_policy) as f:
        on_disk = json.load(f)
    assert on_disk["agents"]["agent_weather"]["daily_cap_usd"] == 2.5


def test_update_agent_rejects_non_mutable_fields(temp_policy):
    with pytest.raises(ValueError):
        policy_store.update_agent("agent_weather", display_name="Hacked Bot")


def test_update_agent_rejects_unknown_agent(temp_policy):
    with pytest.raises(KeyError):
        policy_store.update_agent("agent_nobody", daily_cap_usd=1.0)
