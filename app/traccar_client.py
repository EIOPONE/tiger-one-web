"""Traccar integration — pulls live positions from a self-hosted (or
managed) Traccar server and matches them to Tiger One's own vehicles.

Traccar's REST API accepts HTTP Basic Auth using the same email/password
you log into its web interface with — no separate API key setup needed.
"""
from __future__ import annotations

import requests


class TraccarError(RuntimeError):
    """Raised for any Traccar API failure — caught at the call site so a
    Traccar hiccup never breaks the rest of the app."""


def _get(url: str, **kwargs) -> requests.Response:
    return requests.get(url, timeout=15, **kwargs)


def get_positions(base_url: str, username: str, password: str) -> list[dict]:
    """Latest known position for every device on the Traccar server. Each
    entry includes deviceId, latitude, longitude, fixTime (when the GPS fix
    was actually taken), and speed, among other fields."""
    url = base_url.rstrip("/") + "/api/positions"
    resp = _get(url, auth=(username, password))
    if resp.status_code != 200:
        raise TraccarError(f"Could not fetch positions from Traccar: {resp.status_code} {resp.text}")
    return resp.json()


def get_devices(base_url: str, username: str, password: str) -> list[dict]:
    """The list of devices registered on the Traccar server — each has an
    id (Traccar's internal numeric ID) and a uniqueId (the identifier you
    typed into Traccar Client on the tablet, e.g. 'TC01'). Useful for the
    office to pick from when linking a vehicle to its Traccar device."""
    url = base_url.rstrip("/") + "/api/devices"
    resp = _get(url, auth=(username, password))
    if resp.status_code != 200:
        raise TraccarError(f"Could not fetch devices from Traccar: {resp.status_code} {resp.text}")
    return resp.json()
