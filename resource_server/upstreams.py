"""The actual APIs this server resells behind the x402 paywall.

Until now /weather and /enrich returned hardcoded dicts. Everything above
them -- policy, payment, settlement, the audit ledger -- was real, and the
product at the bottom was a stub, which made the whole thing a demo of a
thing rather than the thing. These are real upstreams:

  - weather -> open-meteo.com   (geocode a city, then read current
    conditions). Free, no API key, no account.
  - enrich  -> SEC EDGAR        (resolve a ticker or company name to a CIK,
    then pull that filer's public profile and recent filings). Free, no API
    key; SEC's fair-access policy asks callers to identify themselves in
    User-Agent, hence SEC_USER_AGENT below.

Both are free, which is the point worth being clear about: the money in
this system is not covering an upstream bill. It's demonstrating metered,
per-call settlement between an agent and an API it has no account with.
Swapping either upstream for a paid one changes nothing above this file.

Two design rules everything here follows, because the layer above depends
on them:

1. **Failures raise UpstreamError with an HTTP status, and the route turns
   that into a non-2xx response.** The x402 middleware only settles payment
   after the handler returns something under 400 (see the settle branch in
   x402/http/middleware/fastapi.py), so a failed upstream call means the
   agent is never charged. That guarantee is why nothing in here returns a
   200 carrying an error payload -- doing so would take the agent's money
   for a response that isn't the product.

2. **Caching is allowed to serve a paid call.** A cache hit is still a sale;
   that's ordinary for a metered API and it keeps a demo loop from hammering
   somebody's free endpoint. What it must never do is outlive the data's
   usefulness, so the TTLs below are per-source rather than global.
"""

import threading
import time

import requests

from common import config

OPEN_METEO_GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

# SEC asks for a descriptive User-Agent with a contact address and will
# throttle or block generic ones. Override this with something that reaches
# you before pointing any real traffic at it.
SEC_USER_AGENT = config.SEC_USER_AGENT

# Kept well under the policy engine's 60s outbound budget: that timeout has
# to cover this call *plus* the facilitator round trip and settlement. An
# upstream allowed to sit near the full 60s would starve the payment leg and
# surface as a payment failure, which is the wrong diagnosis entirely.
UPSTREAM_TIMEOUT_SECONDS = config.UPSTREAM_TIMEOUT_SECONDS

WEATHER_TTL_SECONDS = 300
COMPANY_TTL_SECONDS = 3600
TICKER_INDEX_TTL_SECONDS = 24 * 3600

# WMO 4677 weather interpretation codes, which is what Open-Meteo's
# `weather_code` field carries. Without this the response hands the agent a
# bare integer and makes it somebody else's problem to decode.
WMO_CODES = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snowfall",
    73: "Moderate snowfall",
    75: "Heavy snowfall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


class UpstreamError(Exception):
    """An upstream call that must not result in a charge.

    `status` is the HTTP status the route should return. It matters more
    than it looks: anything >= 400 tells the x402 middleware to skip
    settlement, so picking the status correctly is what actually decides
    whether the agent pays.

    `code` is the stable machine-readable half, so the policy engine can
    tell "the vendor is down" from "you asked for a city that isn't real"
    without parsing prose.
    """

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class _TTLCache:
    """Small thread-safe TTL cache.

    Thread-safe rather than plain dict because the routes are sync handlers,
    which Starlette runs in a threadpool -- concurrent agents genuinely do
    land in here at the same time.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, object]] = {}

    def get(self, key: str):
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if time.time() > expires_at:
                del self._entries[key]
                return None
            return value

    def put(self, key: str, value: object, ttl: float) -> None:
        with self._lock:
            self._entries[key] = (time.time() + ttl, value)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_cache = _TTLCache()


def _get_json(url: str, params: dict | None = None, headers: dict | None = None):
    """One upstream GET, with every failure mode mapped to an UpstreamError.

    Deliberately exhaustive about the failure taxonomy: a timeout, a DNS
    failure, a 500 from the vendor and a 200 carrying unparseable bytes are
    all "the agent doesn't pay", but they are very different things for
    whoever is on call, and collapsing them into one message throws that away.
    """
    try:
        response = requests.get(
            url, params=params, headers=headers, timeout=UPSTREAM_TIMEOUT_SECONDS
        )
    except requests.Timeout:
        raise UpstreamError(504, "upstream_timeout", f"{url} did not respond within {UPSTREAM_TIMEOUT_SECONDS}s")
    except requests.RequestException as e:
        raise UpstreamError(502, "upstream_unreachable", f"could not reach {url}: {e}")

    if response.status_code == 404:
        raise UpstreamError(404, "upstream_not_found", f"{url} returned 404")
    if response.status_code == 429:
        raise UpstreamError(502, "upstream_rate_limited", f"{url} rate-limited this server")
    if not response.ok:
        raise UpstreamError(502, "upstream_error", f"{url} returned {response.status_code}")

    try:
        return response.json()
    except ValueError:
        raise UpstreamError(502, "upstream_malformed", f"{url} returned a non-JSON body")


# ---------------------------------------------------------------------------
# Weather -- open-meteo.com
# ---------------------------------------------------------------------------


def fetch_weather(city: str) -> dict:
    """Current conditions for a named place. Two upstream calls: geocode,
    then forecast."""
    city = (city or "").strip()
    if not city:
        raise UpstreamError(400, "missing_param", "'city' is required")

    cache_key = f"weather:{city.lower()}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return {**cached, "cached": True}

    geo = _get_json(OPEN_METEO_GEOCODE, params={"name": city, "count": 1, "format": "json"})
    results = geo.get("results") or []
    if not results:
        # A city that doesn't exist is the agent's mistake, not the
        # upstream's -- but it still must not be a sale. There's no product
        # to deliver, so 404 and no settlement.
        raise UpstreamError(404, "place_not_found", f"no place matching '{city}'")

    place = results[0]
    forecast = _get_json(
        OPEN_METEO_FORECAST,
        params={
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code",
            "timezone": "auto",
        },
    )

    current = forecast.get("current") or {}
    if "temperature_2m" not in current:
        raise UpstreamError(502, "upstream_malformed", "forecast response carried no current conditions")

    result = {
        "location": {
            "name": place.get("name"),
            "admin1": place.get("admin1"),
            "country": place.get("country"),
            "latitude": place.get("latitude"),
            "longitude": place.get("longitude"),
            "timezone": forecast.get("timezone"),
        },
        "observed_at": current.get("time"),
        "conditions": WMO_CODES.get(current.get("weather_code"), "Unknown"),
        "weather_code": current.get("weather_code"),
        "temperature_c": current.get("temperature_2m"),
        "relative_humidity_pct": current.get("relative_humidity_2m"),
        "wind_speed_kmh": current.get("wind_speed_10m"),
        "source": "open-meteo.com",
        "cached": False,
    }
    _cache.put(cache_key, {**result, "cached": False}, WEATHER_TTL_SECONDS)
    return result


# ---------------------------------------------------------------------------
# Company enrichment -- SEC EDGAR
# ---------------------------------------------------------------------------


def _ticker_index() -> list[dict]:
    """SEC's ticker -> CIK map, cached for a day.

    It's ~800KB and changes rarely, so refetching it per request would be
    both slow and rude to a free public endpoint.
    """
    cached = _cache.get("sec:tickers")
    if cached is not None:
        return cached

    raw = _get_json(SEC_TICKERS_URL, headers={"User-Agent": SEC_USER_AGENT})
    # Delivered as {"0": {...}, "1": {...}} rather than a list.
    index = [
        {"cik": int(row["cik_str"]), "ticker": str(row["ticker"]), "title": str(row["title"])}
        for row in raw.values()
        if isinstance(row, dict) and "cik_str" in row
    ]
    if not index:
        raise UpstreamError(502, "upstream_malformed", "SEC ticker index was empty")
    _cache.put("sec:tickers", index, TICKER_INDEX_TTL_SECONDS)
    return index


def _resolve_company(query: str) -> dict:
    """Best match for a ticker or company name, most-exact first.

    Ordered rather than fuzzy on purpose: an agent that asks for "Apple"
    should get Apple Inc., not whichever of the forty filers with "apple" in
    the name happens to sort first. Among equally-good substring matches the
    shortest title wins, which reliably prefers the parent company over its
    subsidiaries and trusts.
    """
    index = _ticker_index()
    q = query.strip().lower()

    for row in index:
        if row["ticker"].lower() == q:
            return row
    for row in index:
        if row["title"].lower() == q:
            return row

    prefix = [r for r in index if r["title"].lower().startswith(q)]
    if prefix:
        return min(prefix, key=lambda r: len(r["title"]))

    contains = [r for r in index if q in r["title"].lower()]
    if contains:
        return min(contains, key=lambda r: len(r["title"]))

    raise UpstreamError(404, "company_not_found", f"no SEC filer matching '{query}'")


def fetch_company(query: str) -> dict:
    """Public filer profile for a ticker or company name.

    Note what is deliberately absent: the stub this replaced returned a
    `risk_score` of 0.12. EDGAR publishes no such number, and inventing one
    inside an API an agent pays real money for is exactly the kind of thing
    this project exists to argue against. Everything below is a field SEC
    actually publishes.
    """
    query = (query or "").strip()
    if not query:
        raise UpstreamError(400, "missing_param", "'company' is required")

    cache_key = f"company:{query.lower()}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return {**cached, "cached": True}

    match = _resolve_company(query)
    data = _get_json(
        SEC_SUBMISSIONS_URL.format(cik=match["cik"]),
        headers={"User-Agent": SEC_USER_AGENT},
    )

    recent = (data.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    filings = [
        {
            "form": forms[i],
            "filed": (recent.get("filingDate") or [])[i],
            "accession": (recent.get("accessionNumber") or [])[i],
        }
        for i in range(min(5, len(forms)))
    ]

    business = (data.get("addresses") or {}).get("business") or {}
    result = {
        "company": data.get("name"),
        "cik": data.get("cik"),
        "tickers": data.get("tickers") or [],
        "exchanges": data.get("exchanges") or [],
        "industry": data.get("sicDescription"),
        "sic": data.get("sic"),
        "entity_type": data.get("entityType"),
        "state_of_incorporation": data.get("stateOfIncorporation"),
        "fiscal_year_end": data.get("fiscalYearEnd"),
        "filer_category": data.get("category"),
        "business_address": {
            "city": business.get("city"),
            "state": business.get("stateOrCountryDescription"),
            "zip": business.get("zipCode"),
        },
        "recent_filings": filings,
        "matched_on": match["ticker"],
        "source": "sec.gov/EDGAR",
        "cached": False,
    }
    _cache.put(cache_key, {**result, "cached": False}, COMPANY_TTL_SECONDS)
    return result
