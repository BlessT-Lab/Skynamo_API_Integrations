"""Diagnose a Reporting API authentication failure.

Shows the token exchange and a couple of endpoint probes in enough detail to
identify the problem, with every secret and every row value redacted so the
output can be shared.

What is redacted, and why:
  * access_token / id_token / refresh_token   - a live bearer credential
  * the Client ID and Secret                  - shown only as length + a short
                                                fingerprint, so two entries can
                                                be compared without revealing
                                                either
  * JWT sub / azp                             - these carry the Client ID
  * response row values                       - customer/product records are
                                                real business data; only row
                                                counts and field NAMES print

Everything here is read-only apart from minting a token, which changes nothing.

This deliberately performs the token POST itself rather than going through
ReportingClient: the whole point is to show the exchange the client summarises,
including responses the client turns into a one-line message. Shared constants
(URL, audience, timeout, endpoints) still come from skynamo_geo.config /
reporting_config so the two cannot drift.

Credentials come from the environment if set, else prompted (not echoed):

    SKYNAMO_CLIENT_ID, SKYNAMO_CLIENT_SECRET

Usage:  py diag_reporting_auth.py
"""

import base64
import getpass
import hashlib
import json
import os
import sys

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")

from skynamo_geo.config import REQUEST_TIMEOUT
from skynamo_geo.reporting_config import (
    ANALYTICS_BASE, ROLES_ENDPOINT, TOKEN_AUDIENCE, TOKEN_URL,
)

LINE = "-" * 70

# Response keys whose values are credentials and must never be printed.
_SECRET_KEYS = {"access_token", "id_token", "refresh_token"}
# JWT claims that carry the Client ID.
_IDENTIFYING_CLAIMS = {"sub", "azp", "client_id"}
_REDACTED = "<redacted>"


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


def fingerprint(value):
    """Length plus a short digest - identifies a value without revealing it."""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"length {len(value)}, sha256:{digest}"


def redact(value, _key=None):
    """Recursively replace secret values and row data with placeholders."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key.lower() in _SECRET_KEYS:
                out[key] = f"{_REDACTED} ({len(str(item))} chars)"
            else:
                out[key] = redact(item, key)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def show_response(resp, rows_as_shape=False):
    """Print a response's status, useful headers and a redacted body.

    rows_as_shape=True prints only the row count and field names, for
    endpoints that return real business records.
    """
    print(f"  HTTP {resp.status_code} {resp.reason}")
    for key in ("content-type", "www-authenticate", "retry-after",
                "x-date-range", "x-bookmark", "x-amzn-errortype"):
        if key in resp.headers:
            print(f"  {key}: {resp.headers[key]}")
    text = (resp.text or "").strip()
    if not text:
        print("  body: (empty)")
        return None
    try:
        body = resp.json()
    except ValueError:
        # Non-JSON bodies can echo the request (some gateways do), so never
        # print them - just say what came back.
        print(f"  body: non-JSON, {len(text)} chars (not printed - a non-JSON "
              f"error body can echo the request, including credentials)")
        return None

    if rows_as_shape:
        rows = body if isinstance(body, list) else None
        if rows is None and isinstance(body, dict):
            for key in ("data", "items", "results", "value"):
                if isinstance(body.get(key), list):
                    rows = body[key]
                    break
        if rows is None:
            print(f"  body: {type(body).__name__}, keys: "
                  f"{sorted(body)[:15] if isinstance(body, dict) else 'n/a'}")
        else:
            print(f"  rows: {len(rows)}")
            if rows and isinstance(rows[0], dict):
                print(f"  field names ({len(rows[0])}): "
                      f"{', '.join(sorted(rows[0])[:20])}")
            print("  (row values not printed - they are business data)")
        return body

    pretty = json.dumps(redact(body), indent=2)[:1200]
    print("  body (secrets redacted):")
    for line in pretty.splitlines():
        print(f"    {line}")
    return body


def decode_jwt_claims(token):
    """Decode the JWT payload (claims only - no signature verification)."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)   # restore base64url pad
    try:
        return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception:
        return None


def main():
    print("=" * 70)
    print(" Reporting API auth diagnostic (read-only, output redacted)")
    print("=" * 70)
    client_id, secret = credentials()
    print(f"\n  Client ID:     {fingerprint(client_id)}")
    print(f"  Client Secret: {fingerprint(secret)}")
    print("  (values never printed - length and digest only)")

    if len(secret) < 20:
        print("\n  !! The Secret looks short for a client secret. If you were "
              "issued one long key rather than an ID + Secret pair, you "
              "probably clicked 'Add access token' (which issues a Public API "
              "x-api-key) instead of 'Add client credential'.")

    session = requests.Session()

    # --- 1. the token exchange ---
    print(f"\n{LINE}\n[1] POST {TOKEN_URL}")
    print(f"    audience: {TOKEN_AUDIENCE}")
    print(f"    grant_type: client_credentials\n{LINE}")
    try:
        resp = session.post(TOKEN_URL, timeout=REQUEST_TIMEOUT, json={
            "client_id": client_id,
            "client_secret": secret,
            "audience": TOKEN_AUDIENCE,
            "grant_type": "client_credentials",
        })
    except requests.RequestException as exc:
        print(f"  NETWORK ERROR: {type(exc).__name__}: {exc}")
        print("\n  An SSL error usually means a proxy is intercepting TLS; a "
              "timeout means login.skynamo.me is not reachable from here.")
        print(f"\n  HTTPS_PROXY={os.environ.get('HTTPS_PROXY', '(unset)')}")
        print(f"  REQUESTS_CA_BUNDLE="
              f"{os.environ.get('REQUESTS_CA_BUNDLE', '(unset)')}")
        sys.exit(1)

    body = show_response(resp)

    if not resp.ok or not (body or {}).get("access_token"):
        print(f"\n{LINE}\n  TOKEN NOT ISSUED - what the error means\n{LINE}")
        code = str((body or {}).get("error") or "").lower()
        hints = {
            "access_denied":
                "Credentials rejected, OR the Reporting API add-on is not "
                "enabled for this instance. This same response covers a wrong "
                "id/secret and a valid-but-unentitled credential, so ask "
                "Skynamo support to confirm the add-on is active.",
            "invalid_client":
                "The Client ID is not recognised, or the Secret does not "
                "match it. Re-copy both from Settings > Integration Tokens.",
            "unauthorized_client":
                "The client exists but is not allowed the client_credentials "
                "grant - a provisioning issue on Skynamo's side.",
            "invalid_request":
                "The request shape was rejected - most often a missing or "
                f"wrong 'audience'. It must be exactly {TOKEN_AUDIENCE!r}, "
                "trailing slash included.",
            "invalid_grant": "The grant type was rejected for this client.",
            "invalid_scope": "A scope was requested this client does not have.",
        }
        print(f"  {hints.get(code, 'No specific hint for this error code.')}")
        print("\n  Verify, in order:")
        print("   1. You clicked 'Add client credential', not 'Add access token'.")
        print("   2. The Reporting API is a PAID add-on - confirm it is on the")
        print("      subscription (the button is absent when it is not).")
        print("   3. Both values copied whole, no stray whitespace.")
        print("   4. Ask support to confirm the credential is active and")
        print("      entitled to the Analytics/Reporting API.")
        sys.exit(1)

    token = body["access_token"]
    print(f"\n  TOKEN ISSUED. expires_in={body.get('expires_in', 'ABSENT')} "
          f"token_type={body.get('token_type', 'ABSENT')} "
          f"({len(token)} chars, not printed)")

    claims = decode_jwt_claims(token)
    if claims:
        print("\n  JWT claims (decoded, not verified; identifiers redacted):")
        for key in ("iss", "aud", "scope", "gty", "exp", "iat", "permissions"):
            if key in claims:
                print(f"    {key}: {claims[key]}")
        for key in sorted(_IDENTIFYING_CLAIMS & set(claims)):
            print(f"    {key}: {_REDACTED} (carries the Client ID)")
        others = sorted(set(claims) - {"iss", "aud", "scope", "gty", "exp",
                                       "iat", "permissions"}
                        - _IDENTIFYING_CLAIMS)
        if others:
            print(f"    (other claims present: {', '.join(others)})")
        aud = claims.get("aud")
        auds = aud if isinstance(aud, list) else [aud]
        if TOKEN_AUDIENCE not in [str(a) for a in auds]:
            print(f"\n  !! The token's audience {aud!r} does not include "
                  f"{TOKEN_AUDIENCE!r}. Calls to the Analytics API will 401 "
                  f"even though the token was issued.")

    # --- 2. endpoint probes ---
    headers = {"Authorization": f"Bearer {token}"}
    probes = (
        ("roles, no filter", f"{ANALYTICS_BASE}{ROLES_ENDPOINT}", None, False),
        ("roles, filter={}", f"{ANALYTICS_BASE}{ROLES_ENDPOINT}",
         {"filter": "{}"}, False),
        ("customers, filter={} limit=1", f"{ANALYTICS_BASE}/v2/customers",
         {"filter": json.dumps({"order": {"code": "ASC"}, "limit": 1}),
          "reportingPeriod": "Prev30Days"}, True),
    )
    for label, url, params, shape_only in probes:
        print(f"\n{LINE}\n[2] GET {label}\n{LINE}")
        try:
            r = session.get(url, params=params, headers=headers,
                            timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            print(f"  NETWORK ERROR: {type(exc).__name__}: {exc}")
            continue
        show_response(r, rows_as_shape=shape_only)
        if r.status_code in (401, 403):
            print("  -> the token was issued but is not accepted here: an "
                  "audience or entitlement problem, not a wrong password.")

    print(f"\n{LINE}")
    print(" Done. Nothing was written - the Reporting API is read-only.")
    print(" Secrets, identifiers and row values above are redacted, so this")
    print(" output can be shared. Skim it once before sharing regardless.")
    print(LINE)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)
