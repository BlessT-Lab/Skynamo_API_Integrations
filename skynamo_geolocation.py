"""
Skynamo Customer Geolocation Updater
=====================================
Interactive script that:
  1. Connects to a Skynamo instance using an API key + instance name.
  2. Fetches all customers (paginated).
  3. Lets the user map one or more custom fields as the address source.
  4. Geocodes addresses via the Google Maps Geocoding API.
  5. PATCHes latitude/longitude back to Skynamo, with an accuracy value
     derived from how precise the geocoder's match was.
  6. Prints a summary and saves a CSV report (including customers
     skipped because they had no address, and low-confidence matches
     flagged for manual review).

Requirements:
    pip install requests questionary

You also need a Google Maps API key with the Geocoding API enabled:
    https://console.cloud.google.com/  ->  APIs & Services  ->  Geocoding API

Usage:
    python skynamo_geolocation.py
"""

import csv
import sys
import time
from datetime import datetime

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")

try:
    import questionary
except ImportError:
    questionary = None  # fall back to plain input() prompts

API_BASE = "https://api.skynamo.me/v1"
PAGE_SIZE = 200  # API maximum
REQUEST_TIMEOUT = 30
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
GEOCODE_DELAY_SECONDS = 0.05  # Google allows ~50 req/s; a small delay is plenty

# Google's location_type tells us how precise the match is. We translate it
# into a Skynamo "accuracy" value (metres) so downstream reports can trust
# precise pins and treat coarse ones as approximate.
#   ROOFTOP            - exact street address
#   RANGE_INTERPOLATED - interpolated between two known points on a road
#   GEOMETRIC_CENTER   - centre of a street/polyline (e.g. a road, not a number)
#   APPROXIMATE        - region/locality centroid (town or suburb level)
ACCURACY_BY_PRECISION = {
    "ROOFTOP": 10,
    "RANGE_INTERPOLATED": 50,
    "GEOMETRIC_CENTER": 200,
    "APPROXIMATE": 3000,
}
DEFAULT_ACCURACY = 3000  # used if Google returns an unrecognised precision

# Match precisions we consider too coarse to trust without a human check.
LOW_CONFIDENCE_PRECISIONS = {"APPROXIMATE", "GEOMETRIC_CENTER"}

# Report statuses
STATUS_UPDATED = "updated"
STATUS_UPDATED_LOW_CONF = "updated-low-confidence"
STATUS_SKIPPED_HAS_COORDS = "skipped-has-coordinates"
STATUS_SKIPPED_NO_ADDRESS = "skipped-no-address"
STATUS_GEOCODE_FAILED = "geocode-failed"
STATUS_UPDATE_FAILED = "update-failed"


# ---------------------------------------------------------------------------
# Prompt helpers (questionary if available, plain input otherwise)
# ---------------------------------------------------------------------------

def ask_text(message, password=False):
    if questionary:
        fn = questionary.password if password else questionary.text
        answer = fn(message).ask()
        if answer is None:
            sys.exit("Cancelled.")
        return answer.strip()
    import getpass
    if password:
        return getpass.getpass(f"{message} ").strip()
    return input(f"{message} ").strip()


def ask_confirm(message, default=False):
    if questionary:
        answer = questionary.confirm(message, default=default).ask()
        if answer is None:
            sys.exit("Cancelled.")
        return answer
    suffix = "[Y/n]" if default else "[y/N]"
    raw = input(f"{message} {suffix} ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


def ask_checkbox(message, choices):
    """Return a list of selected items from choices (ordered as chosen)."""
    if questionary:
        selected = questionary.checkbox(message, choices=choices).ask()
        if selected is None:
            sys.exit("Cancelled.")
        return selected
    print(f"\n{message}")
    for i, choice in enumerate(choices, 1):
        print(f"  {i}. {choice}")
    raw = input("Enter numbers separated by commas (e.g. 1,3,4): ").strip()
    selected = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= len(choices):
            selected.append(choices[int(part) - 1])
    return selected


# ---------------------------------------------------------------------------
# Skynamo API client
# ---------------------------------------------------------------------------

class SkynamoClient:
    def __init__(self, instance_name, api_key):
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "X-API-CLIENT": instance_name,
            "X-API-KEY": api_key,
        })

    def test_connection(self):
        """Make a minimal call to validate credentials. Returns (ok, message)."""
        try:
            resp = self.session.get(
                f"{API_BASE}/customers",
                params={"page_number": 1, "page_size": 1},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            return False, f"Connection error: {exc}"
        if resp.status_code in (401, 403):
            return False, "Authentication failed - check your API key and instance name."
        if not resp.ok:
            return False, f"Unexpected response: HTTP {resp.status_code} - {resp.text[:200]}"
        return True, "Connected."

    def fetch_all_customers(self):
        """Paginate through /customers using the API's paging response."""
        customers = []
        page_number = 1
        while True:
            resp = self.session.get(
                f"{API_BASE}/customers",
                params={"page_number": page_number, "page_size": PAGE_SIZE},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            body = resp.json()
            data = body.get("data", [])
            if not data:
                break
            customers.extend(data)
            total = (body.get("page") or {}).get("total_item_count")
            total_text = f" of {total}" if total else ""
            print(f"  Fetched page {page_number} "
                  f"({len(customers)}{total_text} customers)...")
            if total and len(customers) >= total:
                break
            if len(data) < PAGE_SIZE:
                break
            page_number += 1
        return customers

    def update_location(self, customer_id, latitude, longitude,
                        accuracy=DEFAULT_ACCURACY, is_approximate=False):
        """PATCH a customer's location. Returns (ok, error_message).

        The Skynamo API only accepts updates on the collection endpoint
        (PATCH /customers) with an array of CustomerPatch objects;
        /customers/{id} is GET-only.
        """
        resp = self.session.patch(
            f"{API_BASE}/customers",
            json=[{
                "id": customer_id,
                "location": {
                    "latitude": latitude,
                    "longitude": longitude,
                    "accuracy": accuracy,
                    "is_approximate": is_approximate,
                },
            }],
            timeout=REQUEST_TIMEOUT,
        )
        if resp.ok:
            return True, ""
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"


# ---------------------------------------------------------------------------
# Customer data helpers
# ---------------------------------------------------------------------------

def get_custom_field_value(customer, field_name):
    for field in customer.get("custom_fields") or []:
        if field.get("name") == field_name:
            return (field.get("value") or "").strip()
    return ""


def collect_custom_field_names(customers):
    """Unique custom field names across all customers, in first-seen order."""
    names = []
    seen = set()
    for customer in customers:
        for field in customer.get("custom_fields") or []:
            name = field.get("name")
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return names


def build_address(customer, address_fields):
    """Combine the mapped fields into one address string. Empty parts are dropped."""
    parts = [get_custom_field_value(customer, name) for name in address_fields]
    return ", ".join(p for p in parts if p)


def has_coordinates(customer):
    """True only if the customer has real, non-zero coordinates.

    Missing, null, or zero latitude/longitude (including "0" stored as a
    string) all count as having NO coordinates, so such customers are
    geocoded and updated whenever they have an address - even when the
    replace-existing option is off.
    """
    location = customer.get("location") or {}

    def is_real(value):
        try:
            return float(value) != 0.0
        except (TypeError, ValueError):
            return False

    return is_real(location.get("latitude")) and is_real(location.get("longitude"))


# ---------------------------------------------------------------------------
# Geocoding (Google Maps Geocoding API)
# ---------------------------------------------------------------------------

class GeocodeResult:
    """Outcome of a single geocode attempt."""

    def __init__(self, lat, lng, precision, formatted_address,
                 partial_match):
        self.lat = lat
        self.lng = lng
        self.precision = precision  # Google location_type
        self.formatted_address = formatted_address
        self.partial_match = partial_match

    @property
    def accuracy(self):
        return ACCURACY_BY_PRECISION.get(self.precision, DEFAULT_ACCURACY)

    @property
    def is_low_confidence(self):
        """True if the match is coarse or only partially matched the input."""
        return self.partial_match or self.precision in LOW_CONFIDENCE_PRECISIONS


class GeocodeError(Exception):
    """Raised for fatal Google API errors (bad key, billing, quota)."""


def geocode_address(session, api_key, address, country=None, retries=2):
    """Geocode one address via Google. Returns a GeocodeResult or None.

    None means the address simply could not be found (ZERO_RESULTS).
    A GeocodeError is raised for configuration problems (REQUEST_DENIED)
    so the caller can stop rather than hammering the API for every row.
    """
    params = {"address": address, "key": api_key}
    if country:
        # Restrict results to the given country (ISO 3166-1 alpha-2 code).
        params["components"] = f"country:{country}"
        params["region"] = country.lower()

    for attempt in range(retries + 1):
        try:
            resp = session.get(GEOCODE_URL, params=params,
                               timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            body = resp.json()
        except requests.RequestException:
            if attempt < retries:
                time.sleep(2)
                continue
            return None

        status = body.get("status")
        if status == "OK":
            top = body["results"][0]
            loc = top["geometry"]["location"]
            return GeocodeResult(
                lat=loc["lat"],
                lng=loc["lng"],
                precision=top["geometry"].get("location_type", ""),
                formatted_address=top.get("formatted_address", ""),
                partial_match=top.get("partial_match", False),
            )
        if status == "ZERO_RESULTS":
            return None
        if status == "OVER_QUERY_LIMIT":
            # Rate limited - back off and retry.
            if attempt < retries:
                time.sleep(2)
                continue
            raise GeocodeError(
                "Google API rate/quota limit hit (OVER_QUERY_LIMIT). "
                "Check your billing and quota.")
        # REQUEST_DENIED, INVALID_REQUEST, etc. - not worth retrying.
        raise GeocodeError(
            f"Google API error: {status} - "
            f"{body.get('error_message', 'no detail')}")
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print(" Skynamo Customer Geolocation Updater")
    print("=" * 60)

    # --- Step 1: credentials ---
    instance_name = ask_text("Skynamo instance name:")
    while not instance_name:
        instance_name = ask_text("Instance name cannot be empty. Skynamo instance name:")
    api_key = ask_text("Skynamo API key:", password=True)
    while not api_key:
        api_key = ask_text("API key cannot be empty. Skynamo API key:", password=True)

    client = SkynamoClient(instance_name, api_key)
    print("\nValidating credentials...")
    ok, message = client.test_connection()
    if not ok:
        sys.exit(f"ERROR: {message}")
    print(f"  {message}")

    # --- Google Maps geocoding key + optional country bias ---
    google_key = ask_text("Google Maps API key:", password=True)
    while not google_key:
        google_key = ask_text("API key cannot be empty. Google Maps API key:",
                              password=True)
    country = ask_text(
        "Restrict geocoding to a country? Enter a 2-letter code "
        "(e.g. ZA, GB, US) or leave blank for no restriction:"
    ).strip().upper()
    if country and len(country) != 2:
        print(f"  '{country}' is not a 2-letter code - ignoring country restriction.")
        country = ""

    geocode_session = requests.Session()
    print("\nValidating Google Maps key...")
    try:
        geocode_address(geocode_session, google_key,
                        "Cape Town", country=country or None)
        print("  Google Maps key OK.")
    except GeocodeError as exc:
        sys.exit(f"ERROR: {exc}")

    # --- Step 2: fetch customers ---
    print("\nFetching customers...")
    try:
        customers = client.fetch_all_customers()
    except requests.RequestException as exc:
        sys.exit(f"ERROR fetching customers: {exc}")
    if not customers:
        sys.exit("No customers found on this instance.")
    print(f"  Total customers: {len(customers)}")

    # --- Step 3: map address fields ---
    field_names = collect_custom_field_names(customers)
    if not field_names:
        sys.exit("No custom fields found on customers - nothing to map as an address.")

    print("\nMap the field(s) that make up the customer address.")
    print("Selected fields are combined in order, e.g. Street + City + Country.")
    address_fields = ask_checkbox("Select address field(s):", field_names)
    while not address_fields:
        print("You must select at least one field.")
        address_fields = ask_checkbox("Select address field(s):", field_names)
    print(f"  Address mapping: {' + '.join(address_fields)}")

    # Show a sample so the user can sanity-check the mapping
    sample = next((build_address(c, address_fields) for c in customers
                   if build_address(c, address_fields)), None)
    if sample:
        print(f"  Sample address: {sample}")
    if not ask_confirm("Does this mapping look correct?", default=True):
        sys.exit("Aborted - rerun the script to remap fields.")

    # --- Step 4: replace mode ---
    replace_existing = ask_confirm(
        "Replace coordinates for customers that already have them? "
        "(No = only fill in missing coordinates)",
        default=False,
    )

    # --- Step 5: process ---
    report_rows = []
    counts = {STATUS_UPDATED: 0, STATUS_UPDATED_LOW_CONF: 0,
              STATUS_SKIPPED_HAS_COORDS: 0, STATUS_SKIPPED_NO_ADDRESS: 0,
              STATUS_GEOCODE_FAILED: 0, STATUS_UPDATE_FAILED: 0}
    total = len(customers)

    print(f"\nProcessing {total} customers via Google Maps Geocoding...\n")

    for index, customer in enumerate(customers, 1):
        cid = customer.get("id")
        name = customer.get("name", "")
        code = customer.get("code", "")
        prefix = f"[{index}/{total}] {name} (id={cid})"

        def record(status, address="", lat="", lng="", accuracy="",
                   precision="", notes=""):
            counts[status] += 1
            report_rows.append({
                "customer_id": cid, "code": code, "name": name,
                "status": status, "address_used": address,
                "latitude": lat, "longitude": lng, "accuracy": accuracy,
                "match_precision": precision, "notes": notes,
            })

        if not replace_existing and has_coordinates(customer):
            record(STATUS_SKIPPED_HAS_COORDS)
            print(f"{prefix}: skipped (already has coordinates)")
            continue

        address = build_address(customer, address_fields)
        if not address:
            record(STATUS_SKIPPED_NO_ADDRESS)
            print(f"{prefix}: skipped (no address)")
            continue

        try:
            result = geocode_address(geocode_session, google_key, address,
                                     country=country or None)
        except GeocodeError as exc:
            sys.exit(f"\nERROR: {exc}\nAborting - fix the Google API key/quota "
                     f"and rerun.")
        time.sleep(GEOCODE_DELAY_SECONDS)

        if result is None:
            record(STATUS_GEOCODE_FAILED, address=address,
                   notes="Address could not be geocoded")
            print(f"{prefix}: geocode FAILED for '{address}'")
            continue

        low_conf = result.is_low_confidence
        note_parts = [f"Google match: '{result.formatted_address}'"]
        if result.partial_match:
            note_parts.append("partial match")
        if low_conf:
            note_parts.append("LOW CONFIDENCE - review manually")
        notes = "; ".join(note_parts)

        try:
            ok, error = client.update_location(
                cid, result.lat, result.lng,
                accuracy=result.accuracy, is_approximate=low_conf)
        except requests.RequestException as exc:
            ok, error = False, str(exc)

        if ok:
            status = STATUS_UPDATED_LOW_CONF if low_conf else STATUS_UPDATED
            record(status, address=address, lat=result.lat, lng=result.lng,
                   accuracy=result.accuracy, precision=result.precision,
                   notes=notes)
            tag = f" [{result.precision}" + (", LOW CONFIDENCE]" if low_conf else "]")
            print(f"{prefix}: updated -> ({result.lat:.6f}, {result.lng:.6f}){tag}")
        else:
            record(STATUS_UPDATE_FAILED, address=address, lat=result.lat,
                   lng=result.lng, accuracy=result.accuracy,
                   precision=result.precision, notes=error)
            print(f"{prefix}: update FAILED - {error}")

    # --- Step 6: report ---
    print("\n" + "=" * 60)
    print(" SUMMARY")
    print("=" * 60)
    print(f"  Total customers:               {total}")
    print(f"  Updated (precise):             {counts[STATUS_UPDATED]}")
    print(f"  Updated (low confidence):      {counts[STATUS_UPDATED_LOW_CONF]}")
    print(f"  Skipped (have coordinates):    {counts[STATUS_SKIPPED_HAS_COORDS]}")
    print(f"  Skipped (no address):          {counts[STATUS_SKIPPED_NO_ADDRESS]}")
    print(f"  Geocode failures:              {counts[STATUS_GEOCODE_FAILED]}")
    print(f"  Update failures:               {counts[STATUS_UPDATE_FAILED]}")

    no_address = [r for r in report_rows if r["status"] == STATUS_SKIPPED_NO_ADDRESS]
    if no_address:
        print("\n  Customers with NO ADDRESS:")
        for row in no_address:
            print(f"    - {row['name']} (id={row['customer_id']}, code={row['code']})")

    low_conf_rows = [r for r in report_rows
                     if r["status"] == STATUS_UPDATED_LOW_CONF]
    if low_conf_rows:
        print("\n  LOW-CONFIDENCE locations (written, but verify these):")
        for row in low_conf_rows:
            print(f"    - {row['name']} (id={row['customer_id']}) "
                  f"[{row['match_precision']}] {row['notes']}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_file = f"geolocation_report_{timestamp}.csv"
    with open(report_file, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "customer_id", "code", "name", "status", "address_used",
            "latitude", "longitude", "accuracy", "match_precision", "notes",
        ])
        writer.writeheader()
        writer.writerows(report_rows)
    print(f"\n  Full report saved to: {report_file}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        sys.exit(1)

