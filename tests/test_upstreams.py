"""The real upstreams behind the paywall, and the property that protects
the agent's money when one of them fails.

Two things are tested here and they are not the same thing:

1. resource_server/upstreams.py parses real API shapes correctly and maps
   every failure mode onto the right HTTP status. Network is mocked
   throughout -- these run offline, like the rest of the suite.

2. A route that fails does not result in a settlement. That one is the
   load-bearing claim: the x402 middleware settles payment only after the
   handler returns a status under 400, so an upstream outage costs the agent
   nothing. It's asserted against the real installed middleware rather than
   inferred from reading it, because it's exactly the sort of guarantee a
   dependency upgrade could silently reverse.
"""

import json
import os
import sys
from types import SimpleNamespace

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from resource_server import upstreams  # noqa: E402
from resource_server.upstreams import UpstreamError  # noqa: E402


@pytest.fixture(autouse=True)
def clear_cache():
    """Every test starts cold. Without this, a cached Tokyo from one test
    silently satisfies the next one's assertion about a fetch happening."""
    upstreams._cache.clear()
    yield
    upstreams._cache.clear()


class FakeResponse:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 400
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


GEOCODE_OK = {
    "results": [
        {
            "name": "Tokyo",
            "admin1": "Tokyo",
            "country": "Japan",
            "latitude": 35.6895,
            "longitude": 139.69171,
        }
    ]
}

FORECAST_OK = {
    "timezone": "Asia/Tokyo",
    "current": {
        "time": "2026-08-10T14:15",
        "temperature_2m": 30.3,
        "relative_humidity_2m": 63,
        "wind_speed_10m": 7.9,
        "weather_code": 95,
    },
}


def route_get(monkeypatch, by_url: dict):
    """Stubs requests.get, dispatching on a substring of the URL."""
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append((url, params))
        for fragment, response in by_url.items():
            if fragment in url:
                if isinstance(response, Exception):
                    raise response
                return response
        raise AssertionError(f"unexpected upstream call to {url}")

    monkeypatch.setattr(upstreams.requests, "get", fake_get)
    return calls


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------


def test_weather_decodes_the_wmo_code(monkeypatch):
    """Open-Meteo returns a bare integer for conditions. Handing an agent a
    95 and letting it work out that means "thunderstorm" would be shipping
    an unfinished product."""
    route_get(monkeypatch, {"geocoding": FakeResponse(GEOCODE_OK), "forecast": FakeResponse(FORECAST_OK)})

    result = upstreams.fetch_weather("Tokyo")

    assert result["conditions"] == "Thunderstorm"
    assert result["temperature_c"] == 30.3
    assert result["location"]["country"] == "Japan"
    assert result["source"] == "open-meteo.com"


def test_unknown_city_is_a_404_not_an_empty_success(monkeypatch):
    """The status is the whole point: a 404 means the middleware skips
    settlement. Returning 200 with an empty body would charge the agent for
    a place that doesn't exist."""
    route_get(monkeypatch, {"geocoding": FakeResponse({"results": []})})

    with pytest.raises(UpstreamError) as excinfo:
        upstreams.fetch_weather("Xyzzyville")

    assert excinfo.value.status == 404
    assert excinfo.value.code == "place_not_found"


def test_weather_is_cached_within_its_ttl(monkeypatch):
    calls = route_get(
        monkeypatch, {"geocoding": FakeResponse(GEOCODE_OK), "forecast": FakeResponse(FORECAST_OK)}
    )

    first = upstreams.fetch_weather("Tokyo")
    second = upstreams.fetch_weather("  tokyo  ")  # same place, sloppier input

    assert len(calls) == 2, "the second lookup should not have hit the network"
    assert first["cached"] is False
    assert second["cached"] is True


def test_a_forecast_without_current_conditions_is_a_502(monkeypatch):
    """A 200 carrying the wrong shape is an upstream fault, not a delivery.
    Passing it through would sell the agent an empty object."""
    route_get(
        monkeypatch,
        {"geocoding": FakeResponse(GEOCODE_OK), "forecast": FakeResponse({"current": {}})},
    )

    with pytest.raises(UpstreamError) as excinfo:
        upstreams.fetch_weather("Tokyo")

    assert excinfo.value.status == 502


@pytest.mark.parametrize(
    "raised,expected_code",
    [
        (requests.Timeout(), "upstream_timeout"),
        (requests.ConnectionError(), "upstream_unreachable"),
    ],
)
def test_transport_failures_map_to_distinct_codes(monkeypatch, raised, expected_code):
    route_get(monkeypatch, {"geocoding": raised})

    with pytest.raises(UpstreamError) as excinfo:
        upstreams.fetch_weather("Tokyo")

    assert excinfo.value.code == expected_code
    assert excinfo.value.status >= 500


def test_a_rate_limited_upstream_is_not_reported_as_not_found(monkeypatch):
    """429 and 404 both mean no sale, but conflating them would tell an
    operator their query was wrong when actually they're being throttled."""
    route_get(monkeypatch, {"geocoding": FakeResponse(None, status_code=429)})

    with pytest.raises(UpstreamError) as excinfo:
        upstreams.fetch_weather("Tokyo")

    assert excinfo.value.code == "upstream_rate_limited"


# ---------------------------------------------------------------------------
# Company enrichment
# ---------------------------------------------------------------------------


TICKERS_OK = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 111111, "ticker": "AHTC", "title": "Apple Hospitality Trust Corp"},
    "2": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
}

SUBMISSIONS_OK = {
    "name": "Apple Inc.",
    "cik": "0000320193",
    "tickers": ["AAPL"],
    "exchanges": ["Nasdaq"],
    "sic": "3571",
    "sicDescription": "Electronic Computers",
    "entityType": "operating",
    "stateOfIncorporation": "CA",
    "fiscalYearEnd": "0926",
    "category": "Large accelerated filer",
    "addresses": {"business": {"city": "CUPERTINO", "stateOrCountryDescription": "CA", "zipCode": "95014"}},
    "filings": {
        "recent": {
            "form": ["10-Q", "8-K"],
            "filingDate": ["2026-07-31", "2026-07-30"],
            "accessionNumber": ["0000320193-26-000020", "0000320193-26-000018"],
        }
    },
}


def test_company_lookup_returns_only_fields_sec_publishes(monkeypatch):
    """The stub this replaced invented a risk_score. An API an agent pays
    real money for must not return a number nobody computed."""
    route_get(
        monkeypatch,
        {"company_tickers": FakeResponse(TICKERS_OK), "submissions": FakeResponse(SUBMISSIONS_OK)},
    )

    result = upstreams.fetch_company("AAPL")

    assert result["company"] == "Apple Inc."
    assert result["industry"] == "Electronic Computers"
    assert result["recent_filings"][0]["form"] == "10-Q"
    assert "risk_score" not in result
    assert "employees" not in result


def test_a_name_prefers_the_parent_over_a_longer_namesake(monkeypatch):
    """"Apple" must resolve to Apple Inc., not Apple Hospitality Trust.
    Silently enriching the wrong company is worse than failing."""
    route_get(
        monkeypatch,
        {"company_tickers": FakeResponse(TICKERS_OK), "submissions": FakeResponse(SUBMISSIONS_OK)},
    )

    assert upstreams._resolve_company("Apple")["ticker"] == "AAPL"
    assert upstreams._resolve_company("apple inc.")["ticker"] == "AAPL"
    assert upstreams._resolve_company("MSFT")["ticker"] == "MSFT"


def test_an_unlisted_company_is_a_404(monkeypatch):
    route_get(monkeypatch, {"company_tickers": FakeResponse(TICKERS_OK)})

    with pytest.raises(UpstreamError) as excinfo:
        upstreams.fetch_company("Acme Corp")

    assert excinfo.value.status == 404
    assert excinfo.value.code == "company_not_found"


def test_sec_calls_identify_this_client(monkeypatch):
    """SEC's fair-access policy asks callers to say who they are, and
    throttles the ones that don't."""
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen[url] = headers
        return FakeResponse(TICKERS_OK if "company_tickers" in url else SUBMISSIONS_OK)

    monkeypatch.setattr(upstreams.requests, "get", fake_get)
    upstreams.fetch_company("AAPL")

    assert all(h and "User-Agent" in h for h in seen.values())


def test_the_ticker_index_is_fetched_once(monkeypatch):
    """It's ~800KB of rarely-changing data on a free public endpoint."""
    calls = route_get(
        monkeypatch,
        {"company_tickers": FakeResponse(TICKERS_OK), "submissions": FakeResponse(SUBMISSIONS_OK)},
    )

    upstreams.fetch_company("AAPL")
    upstreams.fetch_company("MSFT")

    index_calls = [url for url, _ in calls if "company_tickers" in url]
    assert len(index_calls) == 1


# ---------------------------------------------------------------------------
# The route layer: a failure must not become a sale
# ---------------------------------------------------------------------------


def test_routes_return_the_upstream_status_and_code(monkeypatch):
    """The route has to convert an UpstreamError into a real non-2xx. A 200
    carrying {"error": ...} would look fine to a human and would settle."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from resource_server import main

    monkeypatch.setattr(
        main.upstreams, "fetch_weather",
        lambda city: (_ for _ in ()).throw(UpstreamError(502, "upstream_unreachable", "vendor down")),
    )
    monkeypatch.setattr(
        main.upstreams, "fetch_company",
        lambda company: (_ for _ in ()).throw(UpstreamError(404, "company_not_found", "no filer")),
    )

    # The real handlers mounted on a bare app: what's under test is the
    # status they produce, and going through main.app would just hit the
    # payment middleware's 402 (correctly) before reaching them.
    app = FastAPI()
    app.get("/weather")(main.get_weather)
    app.get("/enrich")(main.get_enrichment)

    with TestClient(app, raise_server_exceptions=False) as client:
        weather = client.get("/weather", params={"city": "Tokyo"})
        enrich = client.get("/enrich", params={"company": "Acme"})

    assert weather.status_code == 502
    assert weather.json()["detail"]["code"] == "upstream_unreachable"
    assert enrich.status_code == 404
    assert enrich.json()["detail"]["code"] == "company_not_found"


def test_a_failing_route_is_never_settled(monkeypatch):
    """The guarantee the agent's money depends on.

    Drives the real PaymentMiddlewareASGI with a stubbed facilitator: the
    payment verifies, the route then fails, and settlement must not run.
    Asserted against the installed library rather than trusted from reading
    it -- if an upgrade ever moves settlement ahead of the handler, agents
    start paying for outages and this is what says so.
    """
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from x402.http import x402_http_server
    from x402.http.middleware.fastapi import PaymentMiddlewareASGI

    settlements = []

    async def fake_process_http_request(self, context, paywall_config=None):
        return SimpleNamespace(
            type="payment-verified", payment_payload={"stub": True},
            payment_requirements={"stub": True}, response=None,
        )

    async def fake_process_settlement(self, payload, requirements):
        settlements.append((payload, requirements))
        return SimpleNamespace(success=True, headers={"X-PAYMENT-RESPONSE": "settled"}, error_reason=None)

    server_cls = x402_http_server.x402HTTPResourceServer
    monkeypatch.setattr(server_cls, "requires_payment", lambda self, context: True)
    monkeypatch.setattr(server_cls, "initialize", lambda self: None)
    monkeypatch.setattr(server_cls, "process_http_request", fake_process_http_request)
    monkeypatch.setattr(server_cls, "process_settlement", fake_process_settlement)

    app = FastAPI()

    @app.get("/broken")
    def broken():
        raise HTTPException(status_code=502, detail={"code": "upstream_unreachable"})

    @app.get("/working")
    def working():
        return {"ok": True}

    app.add_middleware(PaymentMiddlewareASGI, routes={}, server=object())

    with TestClient(app, raise_server_exceptions=False) as client:
        failed = client.get("/broken")
        assert failed.status_code == 502
        assert settlements == [], "a failed call must not take the agent's money"

        succeeded = client.get("/working")
        assert succeeded.status_code == 200
        assert len(settlements) == 1, "a successful call must still settle"


def test_classify_failure_separates_the_vendor_from_the_payment():
    """Both are denials, but they send whoever is on call to different
    systems -- and only one of them is anything to do with Algorand."""
    from policy_engine.app import _classify_failure

    payment_code, _ = _classify_failure(402, "insufficient funds")
    vendor_code, vendor_reason = _classify_failure(
        502, json.dumps({"detail": {"code": "upstream_unreachable", "message": "vendor down"}})
    )
    missing_code, _ = _classify_failure(
        404, json.dumps({"detail": {"code": "place_not_found", "message": "no place matching 'x'"}})
    )
    auth_code, _ = _classify_failure(403, json.dumps({"detail": "bad token"}))

    assert payment_code == "settlement_failed"
    assert vendor_code == "upstream_unreachable"
    assert vendor_reason == "vendor down"
    assert missing_code == "upstream_not_found"
    assert auth_code == "policy_auth_failed"


def test_classify_failure_survives_a_non_json_body():
    """A reverse proxy erroring out returns HTML, not our error shape."""
    from policy_engine.app import _classify_failure

    code, reason = _classify_failure(503, "<html>502 Bad Gateway</html>")

    assert code == "upstream_unavailable"
    assert reason
