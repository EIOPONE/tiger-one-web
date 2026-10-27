"""Web Push notifications — sends a real OS-level notification to a
driver's phone/tablet even when the Tiger One driver app isn't open,
using the standard Web Push protocol (the same mechanism behind push
notifications on any installed PWA).

Needs a VAPID keypair, which identifies this server to the push
services (Google's for Chrome, Mozilla's for Firefox, etc.) — set
VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY and VAPID_CLAIM_EMAIL as Render env
vars. Until those are set, every function here is a no-op: nothing
raises, nothing is sent, and the rest of the app works exactly as
before. See README.md for how to generate a keypair and what each
variable needs to be.

Like routing_client.py and traccar_client.py, every call is
best-effort: any failure (network, an expired/revoked subscription,
misconfigured keys) is swallowed rather than raised, so a push hiccup
never breaks scheduling a delivery. The one piece of information a
caller does get back is whether a given subscription is now dead
(HTTP 404/410 from the push service) and should be deleted — pushing
to a stale subscription forever would otherwise pile up silently.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_CLAIM_EMAIL = os.environ.get("VAPID_CLAIM_EMAIL", "")


def is_configured() -> bool:
    return bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY and VAPID_CLAIM_EMAIL)


@dataclass
class PushResult:
    sent: bool
    expired: bool = False  # True => the caller should delete this subscription


def send_push(endpoint: str, p256dh: str, auth: str, title: str, body: str, url: str = "/driver") -> PushResult:
    """Sends one push notification to one subscription. Returns a
    PushResult rather than raising, win or lose, so a caller can fan a
    notification out to several of a driver's devices without one dead
    subscription stopping the others."""
    if not is_configured():
        return PushResult(sent=False)
    try:
        import json
        from pywebpush import webpush

        webpush(
            subscription_info={
                "endpoint": endpoint,
                "keys": {"p256dh": p256dh, "auth": auth},
            },
            data=json.dumps({"title": title, "body": body, "url": url}),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": f"mailto:{VAPID_CLAIM_EMAIL}"},
            ttl=60 * 60,  # an hour is plenty for a "new job" alert to still be useful
        )
        return PushResult(sent=True)
    except Exception as exc:  # pragma: no cover - exercised via monkeypatched fixtures in tests
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code in (404, 410):
            return PushResult(sent=False, expired=True)
        return PushResult(sent=False)
