"""Live-editable, hot-reloading policy config.

policy.json is the source of truth on disk. get_policy() checks the file's
mtime on every call and reloads if it changed -- hand-editing the file (or
another process writing it) takes effect on the next request, no restart
needed. update_agent() writes changes back to the same file, so edits made
through the admin API persist across restarts too, and are immediately
visible to get_policy() since it's the same on-disk source of truth.
"""

import json
import os
import threading

POLICY_PATH = os.path.join(os.path.dirname(__file__), "policy.json")

_lock = threading.Lock()
_cache: dict = {"mtime": None, "data": None}

# Fields a live PATCH is allowed to touch. display_name and which agents
# exist at all are not editable through this API -- this is about
# adjusting spend governance live, not reshaping the agent roster.
MUTABLE_AGENT_FIELDS = {"per_request_limit_usd", "daily_cap_usd", "allowed_actions"}


def _load_locked() -> dict:
    mtime = os.path.getmtime(POLICY_PATH)
    if _cache["data"] is None or _cache["mtime"] != mtime:
        try:
            with open(POLICY_PATH) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            # Another process (an operator's editor) may be mid-write when
            # we hit this file. Fall back to the last good snapshot rather
            # than serving a crash/half-written config to every request.
            if _cache["data"] is not None:
                return _cache["data"]
            raise
        _cache["data"] = data
        _cache["mtime"] = mtime
    return _cache["data"]


def get_policy() -> dict:
    with _lock:
        return _load_locked()


def update_agent(agent_id: str, **fields) -> dict:
    unknown = set(fields) - MUTABLE_AGENT_FIELDS
    if unknown:
        raise ValueError(f"not editable: {', '.join(sorted(unknown))}")
    with _lock:
        data = _load_locked()
        if agent_id not in data["agents"]:
            raise KeyError(agent_id)
        data["agents"][agent_id].update(fields)
        tmp_path = POLICY_PATH + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, POLICY_PATH)  # atomic on POSIX
        _cache["data"] = data
        _cache["mtime"] = os.path.getmtime(POLICY_PATH)
        return data["agents"][agent_id]
