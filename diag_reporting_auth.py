"""Diagnose a Reporting API authentication failure.

Shows the RAW token exchange - status code, response headers and full body -
which the normal client deliberately summarises. OAuth error bodies name the
actual problem (invalid_client, access_denied, unauthorized_client, an audience
mismatch, ...) and contain no credential material.

Then, if a token is issued, it decodes the JWT's claims (audience, issuer,
expiry, scopes) and probes a couple of endpoints, printing exactly what comes
back. Everything is read-only.

Your Client ID and Secret are never printed, never logged and never written to
disk. Read from the environment if set, otherwise prompted (secret not echoed):

    SKYNAMO_CLIENT_ID, SKYNAMO_CLIENT_SECRET

Usage:  py diag_reporting_auth.py
"""

import base64
import getpass
import json
import os
import sys

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")

from skynamo_geo.reporting_config import (
    ANALYTICS_BASE, ROLES_ENDPOINT, TOKEN_AUDIENCE, TOKEN_URL,
)

LINE = "-" * 70


def credentials():
    client_id = (os.environ.get("SKYNAMO_CLIENT_ID") or "").strip()
    secret = (os.environ.get("SKYNAMO_CLIENT_SECRET") or "").strip()
    if client_id:
        print("Client ID:     (from SKYNAMO_CLIENT_ID)")
    else:
        try:
            client_id = input("Client ID: ").strip()
        except EOFError:
            sys.exit("No input available. Set SKYNAMO_CLIENT_ID / "
                     "SKYNAMO_CLIENT_SECRET, or run this in a terminal.")
    if secret:
        print("Client Secret: (from SKYNAMO_CLIENT_SECRET)")
    else:
        try:
            secret = getpass.getpass("Client Secret (not echoed): ").strip()
        except (EOFError, OSError):
            sys.exit("No input available for the Client Secret.")
    if not (client_id and secret):
        sys.exit("Both a Client ID and a Client Secret are required.")
    return client_id, secret


def shape(value):
    """Describe a credential without revealing it."""
    return f"length {len(value)}, starts {value[:3]!r}, ends {value[-2:]!r}"


def show_response(resp):
    print(f"  HTTP {resp.status_code} {resp.reason}")
    interesting = ("content-type", "www-authenticate", "retry-after",
                   "x-date-range", "x-bookmark", "x-amzn-errortype")
    for key in interesting:
        if key in resp.headers:
            print(f"  {key}: {resp.headers[key]}")
    text = (resp.text or "").strip()
    if not text:
        print("  body: (empty)")
        return None
    try:
        body = resp.json()
    except ValueError:
        print(f"  body (not JSON, first 400 chars):\n    {text[:400]}")
        return None
    pretty = json.dumps(body, indent=2)[:1200]
    print("  body:")
    for line in pretty.splitlines():
        print(f"    {line}")
    return body


def decode_jwt_claims(token):
    """Decode the JWT payload (claims only - no signature verification)."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)      # restore base64url padding
    try:
        return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception:
        return None


def main():
    print("=" * 70)
    print(" Reporting API auth diagnostic (read-only)")
    print("=" * 70)
    client_id, secret = credentials()
    print(f"\n  Client ID shape:     {shape(client_id)}")
    print(f"  Client Secret shape: {shape(secret)}")
    print("  (shapes only - the values themselves are never printed)")

    if client_id.count("-") == 4 and len(client_id) == 36:
        print("\n  Note: the Client ID looks like a GUID, which is expected.")
    if len(secret) < 20:
        print("\n  !! The Secret looks short for a client secret. If you were "
              "issued a single long key rather than an ID + Secret pair, you "
              "probably clicked 'Add access token' (Public API x-api-key) "
              "instead of 'Add client credential' (Reporting API).")

    session = requests.Session()

    # --- 1. the token exchange, raw ---
    print(f"\n{LINE}\n[1] POST {TOKEN_URL}")
    print(f"    audience: {TOKEN_AUDIENCE}")
    print(f"    grant_type: client_credentials\n{LINE}")
    try:
        resp = session.post(TOKEN_URL, timeout=30, json={
            "client_id": client_id,
            "client_secret": secret,
            "audience": TOKEN_AUDIENCE,
            "grant_type": "client_credentials",
        })
    except requests.RequestException as exc:
        print(f"  NETWORK ERROR: {type(exc).__name__}: {exc}")
        print("\n  If this is an SSL error you are probably behind a proxy that")
        print("  intercepts TLS. If it is a timeout/connection error, check")
        print("  whether login.skynamo.me is reachable from this network.")
        print(f"\n  HTTPS_PROXY={os.environ.get('HTTPS_PROXY', '(unset)')}")
        print(f"  REQUESTS_CA_BUNDLE={os.environ.get('REQUESTS_CA_BUNDLE', '(unset)')}")
        sys.exit(1)

    body = show_response(resp)

    if not resp.ok or not (body or {}).get("access_token"):
        print(f"\n{LINE}\n  TOKEN NOT ISSUED - what the error means\n{LINE}")
        code = str((body or {}).get("error") or "").lower()
        hints = {
            "access_denied":
                "The credentials were rejected, or the Reporting API add-on is "
                "not enabled for this instance. This is the response you get "
                "for a wrong id/secret AND for a credential that exists but "
                "is not entitled - ask Skynamo support to confirm the add-on "
                "is active.",
            "invalid_client":
                "The Client ID is not recognised, or the Secret does not match "
                "it. Re-copy both from Settings > Integration Tokens.",
            "unauthorized_client":
                "The client exists but is not permitted the "
                "client_credentials grant - a provisioning issue on Skynamo's "
                "side. Contact support.",
            "invalid_request":
                "The request shape was rejected - most often a missing or "
                "wrong 'audience'. It must be exactly "
                f"{TOKEN_AUDIENCE!r}, trailing slash included.",
            "invalid_grant":
                "The grant type was rejected for this client.",
            "invalid_scope":
                "A scope was requested that this client does not have.",
        }
        print(f"  {hints.get(code, 'No specific hint for this error code.')}")
        print("\n  Things to verify, in order:")
        print("   1. You clicked 'Add client credential', not 'Add access token'.")
        print("   2. The Reporting API is a PAID add-on - confirm it is on your")
        print("      subscription (the button is absent when it is not).")
        print("   3. Both values were copied whole, with no stray whitespace.")
        print("   4. Ask support to confirm the credential is active and")
        print("      entitled to the Analytics/Reporting API.")
        sys.exit(1)

    token = body["access_token"]
    print(f"\n  TOKEN ISSUED. expires_in="
          f"{body.get('expires_in', 'ABSENT')} "
          f"token_type={body.get('token_type', 'ABSENT')}")

    claims = decode_jwt_claims(token)
    if claims:
        print("\n  JWT claims (decoded, not verified):")
        for key in ("iss", "aud", "sub", "azp", "scope", "gty", "exp", "iat",
                    "permissions"):
            if key in claims:
                print(f"    {key}: {claims[key]}")
        others = [k for k in claims if k not in
                  ("iss", "aud", "sub", "azp", "scope", "gty", "exp", "iat",
                   "permissions")]
        if others:
            print(f"    (other claims: {', '.join(others)})")
        aud = claims.get("aud")
        auds = aud if isinstance(aud, list) else [aud]
        if TOKEN_AUDIENCE not in [str(a) for a in auds]:
            print(f"\n  !! The token's audience {aud!r} does not include "
                  f"{TOKEN_AUDIENCE!r}. Calls to the Analytics API will 401.")

    # --- 2. probe endpoints ---
    headers = {"Authorization": f"Bearer {token}"}
    for label, url, params in (
        ("roles, no filter", f"{ANALYTICS_BASE}{ROLES_ENDPOINT}", None),
        ("roles, filter={}", f"{ANALYTICS_BASE}{ROLES_ENDPOINT}",
         {"filter": "{}"}),
        ("customers, filter={} limit", f"{ANALYTICS_BASE}/v2/customers",
         {"filter": json.dumps({"order": {"code": "ASC"}, "limit": 1}),
          "reportingPeriod": "Prev30Days"}),
    ):
        print(f"\n{LINE}\n[2] GET {label}\n{LINE}")
        try:
            r = session.get(url, params=params, headers=headers, timeout=60)
        except requests.RequestException as exc:
            print(f"  NETWORK ERROR: {type(exc).__name__}: {exc}")
            continue
        show_response(r)

    print(f"\n{LINE}")
    print(" Done. Nothing was written - the Reporting API is read-only.")
    print(" Paste this output (it contains no credentials) to get help.")
    print(LINE)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)
