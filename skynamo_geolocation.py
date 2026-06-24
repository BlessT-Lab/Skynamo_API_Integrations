"""
Skynamo Customer Geolocation Updater - command-line interface
=============================================================
Interactive console tool that:
  1. Connects to a Skynamo instance using an API key + instance name.
  2. Fetches all customers (paginated).
  3. Lets the user map one or more custom fields as the address source.
  4. Geocodes addresses via the Google Maps Geocoding API.
  5. PATCHes latitude/longitude back to Skynamo, with an accuracy value
     derived from how precise the geocoder's match was.
  6. Prints a summary and saves a CSV report.

This is a thin front-end over skynamo_geo.engine - the GUI (gui.py) uses the
exact same engine, so behaviour stays identical across both.

Requirements:
    pip install requests questionary

You also need a Google Maps API key with the Geocoding API enabled.

Usage:
    python skynamo_geolocation.py
"""

import sys
from datetime import datetime

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")

try:
    import questionary
except ImportError:
    questionary = None  # fall back to plain input() prompts

from skynamo_geo import engine
from skynamo_geo.client import SkynamoClient
from skynamo_geo.config import (
    STATUS_SKIPPED_NO_ADDRESS, STATUS_UPDATED, STATUS_UPDATED_LOW_CONF,
    STATUS_SKIPPED_HAS_COORDS, STATUS_GEOCODE_FAILED, STATUS_UPDATE_FAILED,
)
from skynamo_geo.customers import build_address, collect_custom_field_names
from skynamo_geo.geocoder import GoogleGeocoder, GeocodeError


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
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print(" Skynamo Customer Geolocation Updater")
    print("=" * 60)

    # --- Credentials ---
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

    geocoder = GoogleGeocoder(google_key)
    print("\nValidating Google Maps key...")
    try:
        geocoder.validate(country=country or None)
        print("  Google Maps key OK.")
    except GeocodeError as exc:
        sys.exit(f"ERROR: {exc}")

    # --- Fetch customers ---
    print("\nFetching customers...")
    try:
        customers = client.fetch_all_customers(
            on_page=lambda n, total: print(
                f"  Fetched {n}{f' of {total}' if total else ''} customers..."))
    except requests.RequestException as exc:
        sys.exit(f"ERROR fetching customers: {exc}")
    if not customers:
        sys.exit("No customers found on this instance.")
    print(f"  Total customers: {len(customers)}")

    # --- Map address fields ---
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

    sample = next((build_address(c, address_fields) for c in customers
                   if build_address(c, address_fields)), None)
    if sample:
        print(f"  Sample address: {sample}")
    if not ask_confirm("Does this mapping look correct?", default=True):
        sys.exit("Aborted - rerun the script to remap fields.")

    replace_existing = ask_confirm(
        "Replace coordinates for customers that already have them? "
        "(No = only fill in missing coordinates)",
        default=False,
    )

    # --- Geocode (preview) then write ---
    total = len(customers)

    def on_geocode(ev):
        tag = f" [{ev['status']}]"
        print(f"[{ev['index']}/{ev['total']}] {ev['name']}{tag}")

    print(f"\nGeocoding {total} customers via Google Maps...\n")
    try:
        plans = engine.geocode_customers(
            geocoder, customers, address_fields,
            replace_existing=replace_existing, country=country or None,
            on_progress=on_geocode)
    except GeocodeError as exc:
        sys.exit(f"\nERROR: {exc}\nAborting - fix the Google API key/quota and rerun.")

    def on_write(ev):
        print(f"  wrote [{ev['index']}/{ev['total']}] {ev['name']} -> {ev['status']}")

    print("\nWriting coordinates to Skynamo...\n")
    report_rows = engine.write_locations(client, plans, on_progress=on_write)

    # --- Report ---
    counts = engine.summarize(plans)
    print("\n" + "=" * 60)
    print(" SUMMARY")
    print("=" * 60)
    print(f"  Total customers:               {total}")
    print(f"  Updated (precise):             {counts.get(STATUS_UPDATED, 0)}")
    print(f"  Updated (low confidence):      {counts.get(STATUS_UPDATED_LOW_CONF, 0)}")
    print(f"  Skipped (have coordinates):    {counts.get(STATUS_SKIPPED_HAS_COORDS, 0)}")
    print(f"  Skipped (no address):          {counts.get(STATUS_SKIPPED_NO_ADDRESS, 0)}")
    print(f"  Geocode failures:              {counts.get(STATUS_GEOCODE_FAILED, 0)}")
    print(f"  Update failures:               {counts.get(STATUS_UPDATE_FAILED, 0)}")

    no_address = [p for p in plans if p.status == STATUS_SKIPPED_NO_ADDRESS]
    if no_address:
        print("\n  Customers with NO ADDRESS:")
        for plan in no_address:
            print(f"    - {plan.name} (id={plan.customer_id}, code={plan.code})")

    low_conf = [p for p in plans if p.status == STATUS_UPDATED_LOW_CONF]
    if low_conf:
        print("\n  LOW-CONFIDENCE locations (written, but verify these):")
        for plan in low_conf:
            print(f"    - {plan.name} (id={plan.customer_id}) "
                  f"[{plan.precision}] {plan.notes}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_file = f"geolocation_report_{timestamp}.csv"
    engine.write_report(report_rows, report_file)
    print(f"\n  Full report saved to: {report_file}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        sys.exit(1)
