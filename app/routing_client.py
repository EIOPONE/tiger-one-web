"""Free routing and geocoding via the OpenStreetMap ecosystem's public
services — OSRM for driving-time calculation, Nominatim for turning a
site address into coordinates. Both are the public demo instances, not
self-hosted, chosen deliberately to keep costs at zero while this is
being tested — see the note about upgrading to a self-hosted or paid
instance before relying on this at real commercial volume, since the
public servers are rate-limited and offer no uptime guarantee.

Both functions are best-effort: any failure (network, rate limit, no
result found) returns None rather than raising, so a routing hiccup
never breaks the dashboard.
"""
from __future__ import annotations

import requests

# Nominatim's usage policy requires a real, identifying User-Agent —
# requests with a generic or missing one get blocked.
_HEADERS = {"User-Agent": "TigerOneWeb/1.0 (Tiger Concrete Ltd internal tool)"}

OSRM_BASE_URL = "https://router.project-osrm.org"
NOMINATIM_BASE_URL = "https://nominatim.openstreetmap.org"


def geocode_address(address: str) -> tuple[float, float] | None:
    """Turns a free-text address into (latitude, longitude), or None if
    it can't be resolved. Uses OSM's public Nominatim instance — call
    this sparingly (once per address, then cache the result) per their
    usage policy, not on every request."""
    if not address or not address.strip():
        return None
    try:
        resp = requests.get(
            f"{NOMINATIM_BASE_URL}/search",
            params={"q": address, "format": "json", "limit": 1},
            headers=_HEADERS, timeout=10,
        )
        if resp.status_code != 200:
            return None
        results = resp.json()
        if not results:
            return None
        return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception:
        return None


def get_eta_seconds(origin_lat: float, origin_lon: float, dest_lat: float, dest_lon: float) -> int | None:
    """Driving-time ETA in seconds between two points, via OSRM's public
    demo routing server. Returns None on any failure — no route found,
    server unreachable, rate-limited, etc."""
    try:
        url = f"{OSRM_BASE_URL}/route/v1/driving/{origin_lon},{origin_lat};{dest_lon},{dest_lat}"
        resp = requests.get(url, params={"overview": "false"}, headers=_HEADERS, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        routes = data.get("routes") or []
        if not routes:
            return None
        return int(routes[0]["duration"])
    except Exception:
        return None
