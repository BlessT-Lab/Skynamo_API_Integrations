"""
Skynamo Toolkit - command-line interface
========================================
Interactive console front-end over the skynamo_geo core. Three features, the
same ones the GUI offers (the Reporting/Dashboards tabs are GUI-only):

  geo     Customer geolocation - geocode customer addresses via OpenStreetMap
          (Nominatim) and PATCH latitude/longitude back to Skynamo, with an
          accuracy value derived from how precise the match was.
  images  Product image import - match local image files to products by the
          product code in the filename and upload them.
  manage  Manage product images - list the images already attached to a
          product and detach the ones you choose.

Every feature previews before it commits, and writes a CSV report of what it
did. The GUI (gui.py) calls the exact same engines, so behaviour is identical.

Requirements:
    pip install requests questionary

Usage:
    python skynamo_geolocation.py            # pick a feature from a menu
    python skynamo_geolocation.py geo        # jump straight to a feature
    python skynamo_geolocation.py images
    python skynamo_geolocation.py manage
"""

import os
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

from skynamo_geo import engine, image_engine
from skynamo_geo.client import SkynamoClient
from skynamo_geo.config import (
    IMAGE_FOLDER_FAILED, IMAGE_FOLDER_SUCCESS,
    STATUS_SKIPPED_NO_ADDRESS, STATUS_UPDATED, STATUS_UPDATED_LOW_CONF,
    STATUS_SKIPPED_HAS_COORDS, STATUS_GEOCODE_FAILED, STATUS_UPDATE_FAILED,
    ADDRESS_ROLES, ADDRESS_ROLE_LABELS, DEFAULT_ROLE,
    STATUS_IMG_PENDING, STATUS_IMG_UPLOADED, STATUS_IMG_NO_MATCH,
    STATUS_IMG_BAD_FORMAT, STATUS_IMG_AMBIGUOUS, STATUS_IMG_UPLOAD_FAILED,
    STATUS_ATT_LOADED, STATUS_ATT_FETCH_FAILED, STATUS_ATT_DELETED,
    STATUS_ATT_DELETE_FAILED, ATTACHED_IMAGE_REPORT_FIELDNAMES,
)
from skynamo_geo.customers import build_query, collect_custom_field_names
from skynamo_geo.geocoder import NominatimGeocoder, GeocodeError
from skynamo_geo.products import product_code as product_code_of

WIDTH = 64

FEATURES = [
    ("geo", "Customer geolocation - geocode addresses and write coordinates"),
    ("images", "Product image import - upload images matched by product code"),
    ("manage", "Manage product images - list and remove images on a product"),
]


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


def ask_select(message, choices):
    """Return one item chosen from choices."""
    if questionary:
        answer = questionary.select(message, choices=choices).ask()
        if answer is None:
            sys.exit("Cancelled.")
        return answer
    print(f"\n{message}")
    for i, choice in enumerate(choices, 1):
        print(f"  {i}. {choice}")
    while True:
        raw = input(f"Enter a number (1-{len(choices)}): ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return choices[int(raw) - 1]


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
# Shared bits
# ---------------------------------------------------------------------------

def banner(title):
    print("=" * WIDTH)
    print(f" {title}")
    print("=" * WIDTH)


def connect():
    """Prompt for credentials and return a validated SkynamoClient."""
    instance_name = ask_text("Skynamo instance name:")
    while not instance_name:
        instance_name = ask_text(
            "Instance name cannot be empty. Skynamo instance name:")
    api_key = ask_text("Skynamo API key:", password=True)
    while not api_key:
        api_key = ask_text(
            "API key cannot be empty. Skynamo API key:", password=True)

    client = SkynamoClient(instance_name, api_key)
    print("\nValidating credentials...")
    ok, message = client.test_connection()
    if not ok:
        sys.exit(f"ERROR: {message}")
    print(f"  {message}")
    return client


def fetch_products(client):
    """Fetch all active products with progress, or exit if there are none."""
    print("\nFetching products...")
    try:
        products = client.fetch_all_products(
            on_page=lambda n, total: print(
                f"  Fetched {n}{f' of {total}' if total else ''} products..."))
    except requests.RequestException as exc:
        sys.exit(f"ERROR fetching products: {exc}")
    if not products:
        sys.exit("No products found on this instance.")
    print(f"  Total products: {len(products)}")
    return products


def print_counts(title, counts, rows):
    """Print a '  label   count' block for the statuses that occurred."""
    print("\n" + "=" * WIDTH)
    print(f" {title}")
    print("=" * WIDTH)
    label_width = max(len(label) for label, _status in rows)
    for label, status in rows:
        print(f"  {label.ljust(label_width)}  {counts.get(status, 0)}")


def save_report(write, report_rows, prefix, **kwargs):
    """Write a timestamped CSV into the current directory."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = f"{prefix}_{timestamp}.csv"
    write(report_rows, path, **kwargs)
    print(f"\n  Full report saved to: {path}")
    return path


# ---------------------------------------------------------------------------
# Feature: customer geolocation
# ---------------------------------------------------------------------------

def run_geolocation(client):
    banner("Customer Geolocation")

    # --- Optional country bias ---
    country = ask_text(
        "Restrict geocoding to a country? Enter a 2-letter code "
        "(e.g. ZA, GB, US) or leave blank for no restriction:"
    ).strip().upper()
    if country and len(country) != 2:
        print(f"  '{country}' is not a 2-letter code - ignoring country restriction.")
        country = ""

    geocoder = NominatimGeocoder()
    print("\nValidating geocoder...")
    try:
        geocoder.validate(country=country or None)
        print("  Geocoder OK.")
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
    print("Pick the fields, then tag each with the address component it holds")
    print("(street, city, etc.) - this improves geocoding accuracy.")
    selected = ask_checkbox("Select address field(s):", field_names)
    while not selected:
        print("You must select at least one field.")
        selected = ask_checkbox("Select address field(s):", field_names)

    role_labels = [ADDRESS_ROLE_LABELS[r] for r in ADDRESS_ROLES]
    label_to_role = {ADDRESS_ROLE_LABELS[r]: r for r in ADDRESS_ROLES}
    field_roles = []
    for name in selected:
        label = ask_select(f"What is '{name}'?", role_labels)
        field_roles.append((name, label_to_role.get(label, DEFAULT_ROLE)))
    print("  Address mapping: "
          + ", ".join(f"{name} ({ADDRESS_ROLE_LABELS[role]})"
                      for name, role in field_roles))

    sample = next((build_query(c, field_roles).text for c in customers
                   if build_query(c, field_roles).text), None)
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
        print(f"[{ev['index']}/{ev['total']}] {ev['name']} [{ev['status']}]")

    print(f"\nGeocoding {total} customers via OpenStreetMap...\n")
    try:
        plans = engine.geocode_customers(
            geocoder, customers, field_roles,
            replace_existing=replace_existing, country=country or None,
            on_progress=on_geocode)
    except GeocodeError as exc:
        sys.exit(f"\nERROR: {exc}\nAborting - fix the provider issue and rerun.")

    def on_write(ev):
        print(f"  wrote [{ev['index']}/{ev['total']}] {ev['name']} -> {ev['status']}")

    print("\nWriting coordinates to Skynamo...\n")
    report_rows = engine.write_locations(client, plans, on_progress=on_write)

    # --- Report ---
    counts = engine.summarize(plans)
    print_counts("SUMMARY", counts, [
        ("Updated (precise)", STATUS_UPDATED),
        ("Updated (low confidence)", STATUS_UPDATED_LOW_CONF),
        ("Skipped (have coordinates)", STATUS_SKIPPED_HAS_COORDS),
        ("Skipped (no address)", STATUS_SKIPPED_NO_ADDRESS),
        ("Geocode failures", STATUS_GEOCODE_FAILED),
        ("Update failures", STATUS_UPDATE_FAILED),
    ])
    print(f"  {'Total customers'.ljust(26)}  {total}")

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

    save_report(engine.write_report, report_rows, "geolocation_report")


# ---------------------------------------------------------------------------
# Feature: product image import
# ---------------------------------------------------------------------------

def ask_folder():
    """Prompt for an existing directory."""
    while True:
        raw = ask_text("Path to the folder of images:")
        folder = os.path.expanduser(raw.strip().strip('"').strip("'"))
        if not folder:
            print("  A folder path is required.")
            continue
        if not os.path.isdir(folder):
            print(f"  Not a folder: {folder}")
            continue
        return folder


def run_image_import(client):
    banner("Product Image Import")
    print("Images are named after the product code, e.g. ABC.png.")
    print("Multiple images per product: ABC_1 / ABC 2 / ABC_A.")
    print("A character a filename can't contain (like /) becomes a hyphen.")
    print("PNG and JPG/JPEG only.\n")

    folder = ask_folder()
    products = fetch_products(client)

    # --- Scan (preview - no uploads) ---
    def on_scan(ev):
        print(f"[{ev['index']}/{ev['total']}] {ev['name']} "
              f"[{ev['status']}] {ev['message']}")

    print(f"\nMatching images in {folder} ...\n")
    plans = image_engine.scan_images(products, folder, on_progress=on_scan)
    if not plans:
        sys.exit("No files found in that folder.")

    counts = image_engine.summarize(plans)
    print_counts("MATCH SUMMARY", counts, [
        ("Matched (ready to upload)", STATUS_IMG_PENDING),
        ("No matching product", STATUS_IMG_NO_MATCH),
        ("Ambiguous match", STATUS_IMG_AMBIGUOUS),
        ("Unsupported format", STATUS_IMG_BAD_FORMAT),
    ])

    for label, status in (("NO MATCHING PRODUCT", STATUS_IMG_NO_MATCH),
                          ("AMBIGUOUS", STATUS_IMG_AMBIGUOUS),
                          ("UNSUPPORTED", STATUS_IMG_BAD_FORMAT)):
        offenders = [p for p in plans if p.status == status]
        if offenders:
            print(f"\n  {label}:")
            for plan in offenders:
                print(f"    - {plan.filename}: {plan.notes}")

    matched = [p for p in plans if p.status == STATUS_IMG_PENDING]
    if not matched:
        print("\nNothing matched a product - no uploads to make.")
        save_report(image_engine.write_report, [p.to_report_row() for p in plans],
                    "product_images_report")
        return

    # --- Review what will be uploaded ---
    print(f"\n  {len(matched)} image(s) will be uploaded:")
    for plan in matched:
        existing = len(plan.product.get("files") or [])
        seq = f" seq={plan.sequence}" if plan.sequence else ""
        print(f"    - {plan.filename}  ->  {plan.product_code} "
              f"({plan.product_name}){seq}, {existing} already attached")

    if not ask_confirm("Upload all of these?", default=True):
        labels = [f"{p.filename} -> {p.product_code}" for p in matched]
        chosen = set(ask_checkbox("Select the images to upload:", labels))
        for plan, label in zip(matched, labels):
            plan.include = label in chosen
        selected = [p for p in matched if p.include]
        if not selected:
            sys.exit("Nothing selected - aborted.")
        print(f"  {len(selected)} image(s) selected.")
    else:
        selected = matched

    # --- Replace mode (destructive - be explicit) ---
    replace_existing = ask_confirm(
        "Replace existing images? YES removes every image already on each "
        "product being uploaded to; NO adds to them",
        default=False,
    )
    if replace_existing:
        affected = {}
        for plan in selected:
            n = len(plan.product.get("files") or [])
            if n:
                affected[plan.product_code] = n
        if affected:
            print("\n  REPLACE MODE will detach existing images from:")
            for code, n in sorted(affected.items()):
                print(f"    - {code}: {n} existing image(s)")
            if not ask_confirm("Continue with replace mode?", default=False):
                sys.exit("Aborted.")

    # --- File the processed images away (moves the user's own files) ---
    move_processed = ask_confirm(
        f"Afterwards, move processed files into '{IMAGE_FOLDER_SUCCESS}' and "
        f"'{IMAGE_FOLDER_FAILED}' subfolders of {folder}? Images that matched "
        f"but were not selected stay where they are",
        default=False,
    )

    if not ask_confirm(f"Upload {len(selected)} image(s) to Skynamo now?",
                       default=True):
        sys.exit("Aborted - nothing was uploaded.")

    # --- Upload ---
    def on_upload(ev):
        if ev["phase"] == "filing":
            if ev["index"] == 1:
                # the counter restarts here; say so or it reads as a glitch
                print("\nFiling processed images...\n")
            print(f"  [{ev['index']}/{ev['total']}] {ev['name']} "
                  f"-> {ev['message']}")
            return
        # The engine attaches a product's files in one PATCH after all of its
        # images are POSTed, so at this point the status is still transitional
        # ("pending-upload" = sent, not yet attached). Printing it verbatim
        # reads as if nothing happened, so translate it. The final per-image
        # outcome is in the summary below.
        state = "FAILED" if ev["status"] == STATUS_IMG_UPLOAD_FAILED else "sent"
        print(f"  [{ev['index']}/{ev['total']}] {ev['name']} -> {state}")

    print("\nUploading...\n")
    report_rows = image_engine.upload_images(
        client, plans, replace_existing=replace_existing,
        move_processed=move_processed, on_progress=on_upload)

    # --- Report ---
    counts = image_engine.summarize(plans)
    print_counts("UPLOAD SUMMARY", counts, [
        ("Uploaded", STATUS_IMG_UPLOADED),
        ("Upload failed", STATUS_IMG_UPLOAD_FAILED),
        ("Not uploaded (deselected)", STATUS_IMG_PENDING),
        ("No matching product", STATUS_IMG_NO_MATCH),
        ("Ambiguous match", STATUS_IMG_AMBIGUOUS),
        ("Unsupported format", STATUS_IMG_BAD_FORMAT),
    ])

    groups = image_engine.failure_reasons(plans)
    if groups:
        total = sum(count for _r, count, _e in groups)
        print(f"\n  FAILURES ({total} image(s), grouped by reason):")
        for reason, count, example in groups:
            if count == 1:
                print(f"    - {example}: {reason}")
            else:
                print(f"    - {count} image(s): {reason}")
                print(f"        e.g. {example}")

    if move_processed:
        filed = image_engine.filing_summary(plans)
        if filed:
            print("\n  FILED:")
            for name, n in sorted(filed.items()):
                print(f"    - {n} image(s) into {os.path.join(folder, name)}")
        stuck = image_engine.filing_failures(plans)
        if stuck:
            print("\n  NOT MOVED:")
            for plan in stuck:
                print(f"    - {plan.filename}: {plan.notes}")

    save_report(image_engine.write_report, report_rows,
                "product_images_report")


# ---------------------------------------------------------------------------
# Feature: manage images already on a product
# ---------------------------------------------------------------------------

def run_manage_images(client):
    banner("Manage Product Images")
    print("Skynamo has no delete endpoint, so removing an image detaches it")
    print("from the product - the underlying file may still exist server-side.\n")

    products = fetch_products(client)
    by_code = {product_code_of(p).casefold(): p for p in products
               if product_code_of(p)}

    while True:
        code = ask_text("Product code:").strip()
        product = by_code.get(code.casefold())
        if product:
            break
        print(f"  No product with code {code!r} on this instance.")
        if not ask_confirm("Try another code?", default=True):
            sys.exit("Aborted.")

    print(f"  {product_code_of(product)} - {product.get('name', '')}")

    # --- List (preview - no writes) ---
    def on_list(ev):
        print(f"  [{ev['index']}/{ev['total']}] {ev['name']} [{ev['status']}]")

    print("\nResolving attached images...\n")
    images = image_engine.list_attached_images(client, product,
                                              on_progress=on_list)
    if not images:
        print("This product has no images attached.")
        return

    print(f"\n  {len(images)} image(s) attached:")
    for i, img in enumerate(images, 1):
        note = f"  ({img.notes})" if img.notes else ""
        print(f"    {i}. {img.filename}  [{img.status}]{note}")
        print(f"       {img.guid}")

    failed = [i for i in images if i.status == STATUS_ATT_FETCH_FAILED]
    if failed:
        print(f"\n  Note: {len(failed)} GUID(s) would not resolve to a "
              f"filename; they show as '(unknown)' but can still be removed.")

    if not ask_confirm("Remove any of these from the product?", default=False):
        print("Nothing removed.")
        return

    labels = [f"{i}. {img.filename}" for i, img in enumerate(images, 1)]
    chosen = set(ask_checkbox("Select the images to REMOVE:", labels))
    for img, label in zip(images, labels):
        img.delete = label in chosen
    to_remove = [i for i in images if i.delete]
    if not to_remove:
        print("Nothing selected - nothing removed.")
        return

    print(f"\n  These {len(to_remove)} image(s) will be detached from "
          f"{product_code_of(product)}:")
    for img in to_remove:
        print(f"    - {img.filename}")
    keeping = len(images) - len(to_remove)
    print(f"  {keeping} image(s) will remain attached.")
    if not ask_confirm("Remove them now?", default=False):
        sys.exit("Aborted - nothing was removed.")

    # --- Remove ---
    def on_delete(ev):
        print(f"  [{ev['index']}/{ev['total']}] {ev['name']} -> {ev['status']}")

    print("\nRemoving...\n")
    report_rows = image_engine.delete_selected_images(
        client, product, images, on_progress=on_delete)

    counts = image_engine.summarize(images)
    print_counts("REMOVAL SUMMARY", counts, [
        ("Removed", STATUS_ATT_DELETED),
        ("Removal failed", STATUS_ATT_DELETE_FAILED),
        ("Still attached", STATUS_ATT_LOADED),
        ("Name lookup failed", STATUS_ATT_FETCH_FAILED),
    ])

    save_report(image_engine.write_report, report_rows,
                "product_images_removed",
                fieldnames=ATTACHED_IMAGE_REPORT_FIELDNAMES)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

RUNNERS = {
    "geo": run_geolocation,
    "images": run_image_import,
    "manage": run_manage_images,
}


def pick_feature(argv):
    """Resolve the feature from argv, or ask. Returns a key in RUNNERS."""
    if argv:
        arg = argv[0].strip().lower().lstrip("-")
        if arg in ("h", "help"):
            print(__doc__.strip())
            sys.exit(0)
        if arg in RUNNERS:
            return arg
        sys.exit(f"Unknown feature {argv[0]!r}. "
                 f"Expected one of: {', '.join(RUNNERS)} "
                 f"(or no argument for a menu).")

    labels = [f"{key:<8} {description}" for key, description in FEATURES]
    chosen = ask_select("What would you like to do?", labels)
    return chosen.split()[0]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    banner("Skynamo Toolkit")
    feature = pick_feature(argv)
    client = connect()
    RUNNERS[feature](client)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        sys.exit(1)
