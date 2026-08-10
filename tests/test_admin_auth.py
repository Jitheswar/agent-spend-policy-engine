"""Authentication on the admin plane, and the gate on the demo-only routes.

The rest of the suite runs with ADMIN_TOKEN empty (see tests/conftest.py) so
it can drive /admin routes directly -- those tests are about what the routes
DO. This module is about whether they can be reached at all, so it switches
authentication on for real.

The token is resolved per request via config.admin_token(), which is what
lets these tests flip it with monkeypatch instead of reloading the app --
the same call-time-not-import-time reasoning as anchor.anchoring_disabled().

Why this matters more than a normal auth test: everything under /admin is an
authority over money. Freeze, unfreeze, onboard an agent, release a held
spend, edit a cap. For a system whose entire claim is "a person decides what
the agents may do", leaving those reachable by anything that could open a
socket to the port made the claim untrue -- the kill switch was the one
control with no lock on it.
"""

import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common import config  # noqa: E402
from policy_engine import policy_store, storage  # noqa: E402

TOKEN = "test-admin-token-not-a-real-secret"

# One of each shape: a plain POST, a path-parameterised POST, a PATCH, a
# DELETE, and the two demo routes. Anything that can freeze an agent,
# release money, or rewrite the ledger belongs here.
ADMIN_ROUTES = [
    ("post", "/admin/reset", None),
    ("post", "/admin/agents/agent_weather/freeze", {"frozen": True}),
    ("patch", "/admin/agents/agent_weather", {"daily_cap_usd": 99.0}),
    ("delete", "/admin/agents/agent_weather", None),
    ("post", "/admin/holds/1/approve", None),
    ("post", "/admin/holds/1/reject", None),
    ("post", "/admin/demo/tamper", {"field": "amount_usd"}),
    ("post", "/admin/demo/restore", None),
    ("post", "/admin/sign", {"agent_id": "agent_weather", "action": "weather", "amount_usd": 0.01}),
]


@pytest.fixture
def isolated_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    monkeypatch.setattr(storage, "DB_PATH", path)
    storage.init_db()
    yield path
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def isolated_policy(monkeypatch, tmp_path):
    """A throwaway copy of policy.json.

    Unlike the rest of the suite, this module CALLS the admin routes for
    real once a valid token is presented -- including freeze and DELETE --
    so it cannot be allowed anywhere near the committed policy file. (It
    was, briefly, and deleted agent_weather out of it: every other test
    module then failed on an agent that no longer existed, which is a
    memorable way to learn that a test asserting "this route works" is a
    test that runs the route.)
    """
    path = tmp_path / "policy.json"
    shutil.copy(config.POLICY_PATH, path)
    monkeypatch.setattr(policy_store, "POLICY_PATH", str(path))
    monkeypatch.setattr(policy_store, "_cache", {"mtime": None, "data": None})
    yield path


@pytest.fixture
def client(isolated_db, isolated_policy):
    from fastapi.testclient import TestClient
    from policy_engine import app as app_module

    return TestClient(app_module.app)


@pytest.fixture
def authed(monkeypatch):
    """Admin authentication switched on for this test."""
    monkeypatch.setattr(config, "_ADMIN_TOKEN_ENV", TOKEN)


def call(client, method: str, path: str, body, token: str | None = None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    # TestClient.delete() takes no `json` -- these routes carry no body anyway.
    kwargs = {"headers": headers} if method == "delete" else {"json": body, "headers": headers}
    return getattr(client, method)(path, **kwargs)


@pytest.mark.parametrize("method,path,body", ADMIN_ROUTES)
def test_every_admin_route_refuses_an_unauthenticated_caller(client, authed, method, path, body):
    response = call(client, method, path, body)
    assert response.status_code == 401, f"{method.upper()} {path} was reachable without a token"
    assert "admin authorization required" in response.json()["error"]


@pytest.mark.parametrize("method,path,body", ADMIN_ROUTES)
def test_every_admin_route_accepts_the_right_token(client, authed, method, path, body):
    response = call(client, method, path, body, token=TOKEN)
    # Not asserting 200: several of these legitimately 404/409 on an empty
    # database (there is no hold #1). What matters is that none of them stop
    # at the auth layer.
    assert response.status_code != 401


def test_a_wrong_token_is_refused(client, authed):
    assert call(client, "post", "/admin/reset", None, token="not-the-token").status_code == 401


def test_a_token_without_the_bearer_scheme_is_refused(client, authed):
    response = client.post("/admin/reset", headers={"Authorization": TOKEN})
    assert response.status_code == 401


def test_spend_and_the_read_only_views_stay_open(client, authed):
    """/spend is deliberately unauthenticated: anyone may ATTEMPT a spend,
    and the signature decides whether it succeeds (see common/identity.py).
    Putting the admin token in front of it would replace a cryptographic
    identity check with a shared secret, which is strictly weaker."""
    assert client.get("/health").status_code == 200
    assert client.get("/agents").status_code == 200
    assert client.get("/requests").status_code == 200
    assert client.get("/audit/verify?onchain=false").status_code == 200

    denied = client.post(
        "/spend",
        json={
            "agent_id": "nobody",
            "action": "weather",
            "timestamp": 0,
            "nonce": "n",
            "signature": "s",
        },
    )
    assert denied.status_code == 200
    assert denied.json()["decision"] == "denied"


def test_disabled_auth_is_honoured_but_explicit(client, monkeypatch):
    """An empty ADMIN_TOKEN means "no authentication", which is a real
    choice for a throwaway testnet box. It has to be reachable deliberately
    -- config refuses to combine it with mainnet, and start.sh prints it."""
    monkeypatch.setattr(config, "_ADMIN_TOKEN_ENV", "")
    assert config.admin_auth_enabled() is False
    assert client.post("/admin/reset").status_code == 200


# ---------------------------------------------------------------------------
# The demo-only routes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,body",
    [
        ("/admin/sign", {"agent_id": "agent_weather", "action": "weather", "amount_usd": 0.01}),
        ("/admin/demo/tamper", {"field": "amount_usd"}),
        ("/admin/demo/restore", None),
    ],
)
def test_demo_routes_disappear_when_disabled(client, authed, monkeypatch, path, body):
    """404, not 403: a route that is switched off should look absent rather
    than merely locked."""
    monkeypatch.setattr(config, "ALLOW_DEMO_ENDPOINTS", False)
    response = call(client, "post", path, body, token=TOKEN)
    assert response.status_code == 404
    assert response.json()["error"] == "not enabled"


def test_the_signing_oracle_is_off_before_it_is_authenticated(client, authed, monkeypatch):
    """/admin/sign will sign a /spend request as ANY agent. While it is
    reachable, "only a caller holding an agent's private key can spend as
    that agent" is true only of callers who also lack the admin token --
    so the flag has to be checked whether or not the token is presented.
    """
    monkeypatch.setattr(config, "ALLOW_DEMO_ENDPOINTS", False)
    body = {"agent_id": "agent_weather", "action": "weather", "amount_usd": 0.01}

    assert call(client, "post", "/admin/sign", body).status_code == 404
    assert call(client, "post", "/admin/sign", body, token=TOKEN).status_code == 404


def test_governance_routes_survive_the_demo_flag(client, authed, monkeypatch):
    """Switching the demo routes off must not take the real controls with
    them -- freezing an agent is not a demo affordance."""
    monkeypatch.setattr(config, "ALLOW_DEMO_ENDPOINTS", False)
    response = call(
        client, "post", "/admin/agents/agent_weather/freeze", {"frozen": False}, token=TOKEN
    )
    assert response.status_code == 200


def test_a_non_ascii_credential_is_refused_not_a_crash(client, authed):
    """secrets.compare_digest raises TypeError on non-ASCII str, and
    Starlette decodes header bytes as latin-1 -- so `Authorization: Bearer
    tokén` over a raw socket returned a 500 with a traceback instead of a
    401. A wrong credential is never a bug report, and an unauthenticated
    caller must not be able to reach an exception path at all.

    Sent as raw bytes through the ASGI layer because httpx refuses to
    encode the header -- it takes a hand-built request to see this, which
    is exactly the caller who would build one.
    """
    from policy_engine import app as app_module

    scopes = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "path": "/admin/reset", "raw_path": b"/admin/reset",
        "query_string": b"", "root_path": "", "scheme": "http",
        "headers": [(b"host", b"testserver"), (b"authorization", b"Bearer t\xf6k\xe9n")],
        "client": ("127.0.0.1", 1234), "server": ("testserver", 80),
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    import asyncio

    asyncio.run(app_module.app(scopes, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    assert status == 401


def test_a_non_ascii_policy_token_is_refused_not_a_crash():
    """Same failure, same reasoning, on the resource server's token."""
    from common import policy_auth

    ok, reason, _ = policy_auth.verify_token("abc.t\xf6k\xe9n", "weather", {})
    assert not ok
    assert "invalid signature" in reason
