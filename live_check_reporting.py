"""READ-ONLY live check against Skynamo's Reporting API.

Run this once you have Reporting API client credentials. It verifies the things
the offline tests cannot, because they depend on the live payloads:

  1. OAuth2 client-credentials flow works (and reports expires_in).
  2. GET /v2/roles returns something.
  3. For each registry entity: one Prev30Days call with a small limit, printing
     the x-date-range and x-bookmark headers and - most importantly - the ACTUAL
     keys in the payload, so the column lists in reporting_config.py can be
     confirmed. The spec is known to declare the wrong response schema for
     /v2/products and to leave 7 of 11 endpoints undocumented, so this is the
     step that turns assumptions into facts.
  4. Reports which registry columns were missing from the payload, and which
     payload fields the registry does not yet capture.

The Reporting API is read-only - all 11 endpoints are GET - so nothing here can
change your instance. Nothing is written to the local store either.

Credentials are read from the environment if set, else prompted (not echoed),
and are never written to disk.

    SKYNAMO_CLIENT_ID, SKYNAMO_CLIENT_SECRET

Usage:  py live_check_reporting.py
"""

import getpass
import json
import os
import sys

from skynamo_geo.report_store import normalise_key
from skynamo_geo.reporting_client import ReportingClient, ReportingError
from skynamo_geo.reporting_config import (
    REPORTING_ENTITIES, FILTERABLE_FIELDS_ENDPOINTS,
)

PROBE_PERIOD = "Prev30Days"   # the most generous rate-limit tier
PROBE_LIMIT = 5               # keep payloads small


def _credentials():
    client_id = (os.environ.get("SKYNAMO_CLIENT_ID") or "").strip()
    client_secret = (os.environ.get("SKYNAMO_CLIENT_SECRET") or "").strip()
    if client_id:
        print("Client ID:     (from SKYNAMO_CLIENT_ID)")
    else:
        try:
            client_id = input("Client ID: ").strip()
        except EOFError:
            sys.exit("No input available. Set SKYNAMO_CLIENT_ID / "
                     "SKYNAMO_CLIENT_SECRET, or run this in a terminal.")
    if client_secret:
        print("Client Secret: (from SKYNAMO_CLIENT_SECRET)")
    else:
        try:
            client_secret = getpass.getpass(
                "Client Secret (not echoed): ").strip()
        except (EOFError, OSError):
            sys.exit("No input available for the Client Secret.")
    if not (client_id and client_secret):
        sys.exit("Both a Client ID and a Client Secret are required.")
    return client_id, client_secret


def _describe(rows, spec):
    """Compare the payload's actual keys against the registry's columns."""
    if not rows:
        return
    sample = rows[0]
    if not isinstance(sample, dict):
        print(f"      !! expected objects, got {type(sample).__name__}")
        return

    actual_raw = sorted(sample.keys())
    actual = {normalise_key(k) for k in actual_raw}
    print(f"      payload keys ({len(actual_raw)}): "
          + ", ".join(actual_raw[:14])
          + (" ..." if len(actual_raw) > 14 else ""))

    declared = set(spec.get("columns") or {})
    missing = sorted(declared - actual)
    if missing:
        print(f"      registry columns NOT in payload: {', '.join(missing)}")
    extra = sorted(k for k in actual - declared
                   if not isinstance(sample.get(k), (list, dict)))
    if extra:
        print(f"      payload fields NOT in registry:  {', '.join(extra)}")
    if not missing and not extra:
        print("      registry columns match the payload exactly.")

    # Sub-entities only appear when `entities` expansion worked.
    for api_name, sub in (spec.get("sub_entities") or {}).items():
        value = sample.get(api_name) or sample.get(normalise_key(api_name))
        if isinstance(value, list):
            n = len(value)
            keys = sorted(value[0].keys()) if n and isinstance(value[0], dict) else []
            print(f"      sub-entity '{api_name}': {n} row(s)"
                  + (f", keys: {', '.join(keys[:10])}" if keys else ""))
        else:
            print(f"      sub-entity '{api_name}': absent in this sample")


def main():
    print("=" * 70)
    print(" Skynamo Reporting API - READ-ONLY live check")
    print("=" * 70)
    client_id, client_secret = _credentials()
    client = ReportingClient(client_id, client_secret)

    # --- 1. token ---
    print("\n[1] Requesting an OAuth2 token...")
    try:
        payload = client._fetch_token()
    except ReportingError as exc:
        sys.exit(f"    FAIL: {exc}")
    expires_in = payload.get("expires_in")
    print(f"    OK. expires_in={expires_in if expires_in is not None else 'ABSENT'}"
          f" (token lifetime is undocumented; the client trusts this value)")

    # --- 2. roles ---
    print("\n[2] GET /v2/roles ...")
    ok, message = client.test_connection()
    print(f"    {'OK' if ok else 'FAIL'}: {message}")
    if not ok:
        sys.exit(1)

    # --- 3. one probe per entity ---
    print(f"\n[3] Probing each entity ({PROBE_PERIOD}, limit={PROBE_LIMIT}).")
    print("    This is read-only. Sub-entities are expanded in the same call.")
    results = {}
    for entity, spec in REPORTING_ENTITIES.items():
        print(f"\n  --- {entity}  ({spec['endpoint']}) ---")
        rows, bookmark, date_range, error = client.fetch(
            entity, reporting_period=PROBE_PERIOD, limit=PROBE_LIMIT)
        if error:
            print(f"      ERROR: {error}")
            results[entity] = "error"
            continue
        print(f"      rows: {len(rows)}")
        print(f"      x-date-range: {date_range or '(absent)'}")
        print(f"      x-bookmark:   {bookmark or '(absent)'}"
              + ("" if spec.get("bookmarkable") else "   [registry: not bookmarkable]"))
        if not rows:
            print("      (no rows in this period - try a longer period to "
                  "confirm the schema)")
            results[entity] = "empty"
            continue
        _describe(rows, spec)
        results[entity] = "ok"

    # --- 4. filterable custom fields ---
    print("\n[4] Instance-specific filterable custom fields...")
    for kind in FILTERABLE_FIELDS_ENDPOINTS:
        rows, error = client.fetch_filterable_fields(kind)
        if error:
            print(f"    {kind}: ERROR {error}")
        else:
            names = [r.get("fieldName") for r in rows
                     if isinstance(r, dict)][:8]
            print(f"    {kind}: {len(rows)} field(s)"
                  + (f" - {', '.join(str(n) for n in names)}" if names else ""))

    print("\n" + "=" * 70)
    print(" SUMMARY")
    for entity, state in results.items():
        print(f"   {entity:<14} {state}")
    print("\n No writes were performed (the Reporting API is read-only).")
    print(" Update REPORTING_ENTITIES in skynamo_geo/reporting_config.py for any")
    print(" mismatch reported above, then re-run.")
    print("=" * 70)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)
