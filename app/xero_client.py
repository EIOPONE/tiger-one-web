"""Xero integration — OAuth2 connect flow, token refresh, and the two data
flows: pushing customers as Contacts and completed orders as Invoices, plus
pulling Contact updates back from Xero.

New Xero apps (created from 2 March 2026 onward) only get the new granular
scopes — accounting.invoices and accounting.payments replaced the old
single accounting.transactions scope. This module requests the granular
set from the start rather than the deprecated broad one.

All outbound HTTP goes through _get/_post/_put so tests can monkeypatch
those two functions instead of needing real Xero credentials.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import requests

AUTHORIZE_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
CONNECTIONS_URL = "https://api.xero.com/connections"
API_BASE = "https://api.xero.com/api.xro/2.0"

# Granular scopes (post March 2026): identity + contacts (unchanged) +
# invoices (replaces the old broad accounting.transactions for our needs)
# + offline_access so we get a refresh token and don't need the driver^H^H
# the office to re-approve every 30 minutes.
SCOPES = "openid profile email offline_access accounting.contacts accounting.invoices"


class XeroError(RuntimeError):
    """Raised for any Xero API failure — caught at the call site so a Xero
    hiccup never breaks the office's ability to save a customer or order."""


def _post(url: str, **kwargs) -> requests.Response:
    return requests.post(url, timeout=20, **kwargs)


def _get(url: str, **kwargs) -> requests.Response:
    return requests.get(url, timeout=20, **kwargs)


def _put(url: str, **kwargs) -> requests.Response:
    return requests.put(url, timeout=20, **kwargs)


def build_authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    from urllib.parse import urlencode
    params = {
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
        "scope": SCOPES, "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code_for_tokens(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    resp = _post(TOKEN_URL, auth=(client_id, client_secret), data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
    })
    if resp.status_code != 200:
        raise XeroError(f"Xero rejected the authorization code: {resp.status_code} {resp.text}")
    return resp.json()


def refresh_tokens(client_id: str, client_secret: str, refresh_token: str) -> dict:
    resp = _post(TOKEN_URL, auth=(client_id, client_secret), data={
        "grant_type": "refresh_token", "refresh_token": refresh_token,
    })
    if resp.status_code != 200:
        raise XeroError(f"Could not refresh the Xero connection: {resp.status_code} {resp.text}")
    return resp.json()


def get_connected_tenants(access_token: str) -> list[dict]:
    resp = _get(CONNECTIONS_URL, headers={"Authorization": f"Bearer {access_token}"})
    if resp.status_code != 200:
        raise XeroError(f"Could not list connected Xero organisations: {resp.status_code} {resp.text}")
    return resp.json()


def _headers(access_token: str, tenant_id: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}", "Xero-tenant-id": tenant_id,
        "Accept": "application/json", "Content-Type": "application/json",
    }


def find_or_create_contact(access_token: str, tenant_id: str, customer) -> str:
    """Matches an existing Xero contact by name if one exists (so connecting
    to a Xero org that already has these customers doesn't create dupes),
    otherwise creates one. Returns the Xero ContactID."""
    headers = _headers(access_token, tenant_id)
    search = _get(f"{API_BASE}/Contacts", headers=headers,
                   params={"where": f'Name=="{customer.display_name}"'})
    if search.status_code == 200:
        contacts = search.json().get("Contacts", [])
        if contacts:
            return contacts[0]["ContactID"]

    payload = {"Contacts": [{
        "Name": customer.display_name,
        "EmailAddress": customer.email or None,
        "Phones": [{"PhoneType": "MOBILE", "PhoneNumber": customer.mobile}] if customer.mobile else [],
        "Addresses": [{
            "AddressType": "STREET", "AddressLine1": customer.address_1,
            "AddressLine2": customer.address_2, "City": customer.town, "PostalCode": customer.postcode,
        }] if customer.address_1 else [],
    }]}
    resp = _post(f"{API_BASE}/Contacts", headers=headers, json=payload)
    if resp.status_code not in (200, 201):
        raise XeroError(f"Could not create Xero contact: {resp.status_code} {resp.text}")
    return resp.json()["Contacts"][0]["ContactID"]


def create_invoice(access_token: str, tenant_id: str, order, xero_contact_id: str) -> tuple[str, str]:
    """Creates an AUTHORISED invoice in Xero for a completed order. Returns
    (InvoiceID, InvoiceNumber)."""
    headers = _headers(access_token, tenant_id)
    line_items = [{
        "Description": f"{item.description} ({item.quantity} {item.unit})",
        "Quantity": float(item.quantity), "UnitAmount": float(item.unit_price),
        "AccountCode": "200",  # standard Xero default sales account code
    } for item in order.items]

    payload = {"Invoices": [{
        "Type": "ACCREC",
        "Contact": {"ContactID": xero_contact_id},
        "LineItems": line_items,
        "Reference": order.order_number,
        "Status": "AUTHORISED",
    }]}
    resp = _post(f"{API_BASE}/Invoices", headers=headers, json=payload)
    if resp.status_code not in (200, 201):
        raise XeroError(f"Could not create Xero invoice: {resp.status_code} {resp.text}")
    invoice = resp.json()["Invoices"][0]
    return invoice["InvoiceID"], invoice.get("InvoiceNumber", "")


def is_expired(expires_at: datetime) -> bool:
    now = datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return now >= expires_at - timedelta(minutes=2)  # refresh a little early, not right at the wire


def tokens_to_row_fields(token_response: dict) -> dict:
    """Common shape from either the initial exchange or a refresh — what
    crud.save_xero_connection needs to persist."""
    expires_in = int(token_response.get("expires_in", 1800))
    return {
        "access_token": token_response["access_token"],
        "refresh_token": token_response["refresh_token"],
        "expires_at": datetime.now(timezone.utc) + timedelta(seconds=expires_in),
    }
