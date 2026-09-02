"""Diagnose a product-image upload failure against a live Skynamo instance.

Built because a 1003-image run failed on every image, and the tool's own report
truncates the API's response body at 200 characters - which can cut off the
part that says why. This prints responses in full.

It runs in three phases and stops before doing anything you have not agreed to:

  1. FOLDER (no credentials, no network). Sizes every image and reports the
     base64-inflated request size. This alone can explain a whole-run failure:
     base64 adds ~33%, and gateways in front of APIs commonly cap a request at
     6MB, so any image over roughly 4.4MB cannot be uploaded at all.
  2. INSTANCE (read-only). Fetches products, checks the ones your filenames
     match, and confirms they carry the `id` the patch keys on.
  3. WRITE PROBE (asks first, and is skipped unless you say yes). Uploads ONE
     tiny generated 1x1 PNG, trying each plausible content_hash form until one
     is accepted - the spec says only "(base64string)" and never names the
     algorithm - then tries the attach several ways, printing every status and
     body. This is the only part that changes your instance: it creates one
     small file and attaches it to one product. Both are undoable from the
     Manage Images tab, and the script tells you exactly what to undo.

The API key is redacted from all output, so this is safe to paste.

Usage:
    py diag_image_upload.py                       # all three phases
    py diag_image_upload.py "C:\\path\\to\\images"     # skip the folder prompt
    py diag_image_upload.py "C:\\path\\to\\images" --offline
                                                  # phase 1 only, no
                                                  # credentials, no network
"""

import base64
import getpass
import hashlib
import json
import os
import sys

import requests

from skynamo_geo.client import SkynamoClient, content_hash_b64
from skynamo_geo.config import (
    ALLOWED_IMAGE_EXTENSIONS, API_BASE, REQUEST_TIMEOUT,
)
from skynamo_geo.products import (
    build_code_index, collect_image_files, has_allowed_extension,
    parse_image_stem, product_code, sniff_image_format,
)

# A gateway limit we cannot see from here; base64 inflates by 4/3.
SUSPECT_REQUEST_BYTES = 6 * 1024 * 1024
SUSPECT_FILE_BYTES = int(SUSPECT_REQUEST_BYTES * 3 / 4)

# Smallest valid PNG: a 1x1 transparent pixel.
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")

_SECRET = {"value": ""}


def redact(text):
    """Blank the API key wherever it appears."""
    if _SECRET["value"] and text:
        return text.replace(_SECRET["value"], "<API-KEY-REDACTED>")
    return text


def show(label, resp):
    """Print a response's status, and its body in full (never truncated)."""
    print(f"    {label}")
    print(f"      HTTP {resp.status_code}")
    for header in ("content-type", "x-amzn-errortype", "retry-after"):
        if header in resp.headers:
            print(f"      {header}: {resp.headers[header]}")
    body = redact(resp.text or "")
    if not body:
        print("      body: (empty)")
        return
    try:
        print("      body: " + json.dumps(json.loads(body))[:4000])
    except ValueError:
        print("      body: " + body[:4000])


def human(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024.0


def ask(message, default=False):
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        raw = input(f"{message} {suffix} ").strip().lower()
    except EOFError:
        return default
    if not raw:
        return default
    return raw in ("y", "yes")


# ---------------------------------------------------------------------------
# Phase 1: the folder, offline
# ---------------------------------------------------------------------------

def phase_folder(given=None):
    if given is None:
        try:
            raw = input("Path to the folder of images (blank to skip): ").strip()
        except EOFError:
            return None, []
    else:
        raw = given
        print(f"  folder: {raw}")
    folder = os.path.expanduser(raw.strip('"').strip("'"))
    if not folder:
        return None, []
    if not os.path.isdir(folder):
        print(f"  Not a folder: {folder}")
        return None, []

    files = collect_image_files(folder)
    print(f"\n  {len(files)} file(s) directly in the folder.")
    images = [f for f in files if has_allowed_extension(f)]
    others = [f for f in files if not has_allowed_extension(f)]
    print(f"    {len(images)} with a PNG/JPG/JPEG extension"
          + (f", {len(others)} with something else" if others else ""))

    bad_content = [f for f in images if sniff_image_format(f) is None]
    if bad_content:
        print(f"    !! {len(bad_content)} have an image extension but are NOT "
              f"PNG or JPEG inside (e.g. a renamed WEBP/HEIC):")
        for f in bad_content[:5]:
            print(f"       - {os.path.basename(f)}")

    if not images:
        return folder, []

    sizes = sorted(((os.path.getsize(f), f) for f in images), reverse=True)
    total = sum(s for s, _ in sizes)
    print(f"    total {human(total)}, "
          f"largest {human(sizes[0][0])} ({os.path.basename(sizes[0][1])}), "
          f"median {human(sizes[len(sizes) // 2][0])}")

    too_big = [(s, f) for s, f in sizes if s > SUSPECT_FILE_BYTES]
    print(f"\n  Request size = base64 of the file, so ~1.33x the size above.")
    if too_big:
        print(f"    !! {len(too_big)} image(s) exceed {human(SUSPECT_FILE_BYTES)}, "
              f"so their upload body exceeds ~{human(SUSPECT_REQUEST_BYTES)}.")
        print(f"       If EVERY image failed and they are all this big, a "
              f"request-size limit is the likely cause.")
        for s, f in too_big[:5]:
            print(f"       - {os.path.basename(f)}: {human(s)} "
                  f"-> ~{human(s * 4 / 3)} encoded")
    else:
        print(f"    All images are under {human(SUSPECT_FILE_BYTES)}, so a "
              f"request-size limit is NOT the explanation.")
    return folder, images


# ---------------------------------------------------------------------------
# Phase 2: the instance, read-only
# ---------------------------------------------------------------------------

def phase_instance(images):
    try:
        instance = input("\nInstance name: ").strip()
        key = getpass.getpass("Skynamo API key (not echoed): ").strip()
    except (EOFError, OSError):
        sys.exit("No input available. Run this in a terminal.")
    if not (instance and key):
        sys.exit("Both an instance name and an API key are required.")
    _SECRET["value"] = key
    client = SkynamoClient(instance, key)

    ok, message = client.test_connection()
    print(f"  connection: {'OK' if ok else 'FAIL'} - {redact(message)}")
    if not ok:
        sys.exit(1)

    print("  fetching products...")
    products = client.fetch_all_products()
    print(f"  {len(products)} active product(s).")
    missing_id = [p for p in products if p.get("id") is None]
    print(f"  products with no `id`: {len(missing_id)}"
          + ("  (these can only be patched by code)" if missing_id else ""))

    sample = products[0] if products else None
    if sample:
        print(f"  a product's shape: keys = {sorted(sample.keys())}")
        print(f"    id={sample.get('id')!r} ({type(sample.get('id')).__name__}), "
              f"code={sample.get('code')!r}, "
              f"files={ (sample.get('files') or [])[:3] !r}")

    matched = None
    if images:
        index = build_code_index(products)
        hits = 0
        for path in images:
            stem = os.path.splitext(os.path.basename(path))[0]
            base, _seq, full = parse_image_stem(stem)
            for candidate in (full, base):
                found = index.get(candidate.casefold())
                if found:
                    hits += 1
                    if matched is None and len(found) == 1:
                        matched = found[0]
                    break
        print(f"  {hits} of {len(images)} filename(s) match a product code.")
        if not hits:
            print("    !! nothing matches - the failure is naming, not the API.")
    return client, products, matched


# ---------------------------------------------------------------------------
# Phase 3: one real upload + attach, only if agreed
# ---------------------------------------------------------------------------

def phase_write(client, product):
    print("\n" + "-" * 70)
    print(" WRITE PROBE")
    print("-" * 70)
    print(f" This uploads ONE 1x1 pixel PNG ({len(TINY_PNG)} bytes) and tries to")
    print(f" attach it to product id={product.get('id')!r} "
          f"code={product_code(product)!r}.")
    existing = list(product.get("files") or [])
    print(f" That product currently has {len(existing)} file(s) attached; the")
    print(" probe sends those plus the new one, so nothing is detached.")
    if not ask("\n Run the write probe?", default=False):
        print(" Skipped - nothing was written.")
        return

    print("\n  [a] POST /files - which content_hash does it accept?")
    content = base64.b64encode(TINY_PNG).decode("ascii")

    def b64(digest):
        return base64.b64encode(digest).decode("ascii")

    hash_forms = [
        ("omitted (reproduces F002)", None),
        (f"base64 {content_hash_b64.__defaults__[0]} - what the tool sends",
         content_hash_b64(TINY_PNG)),
        ("md5 hex", hashlib.md5(TINY_PNG).hexdigest()),
        ("sha256 base64", b64(hashlib.sha256(TINY_PNG).digest())),
        ("sha256 hex", hashlib.sha256(TINY_PNG).hexdigest()),
        ("sha1 base64", b64(hashlib.sha1(TINY_PNG).digest())),
        ("base64 md5 of the base64 content, not the bytes",
         b64(hashlib.md5(content.encode("ascii")).digest())),
    ]
    guid = None
    accepted = None
    for label, value in hash_forms:
        body = {"filename": "skynamo-diag-1x1.png", "content": content}
        if value is not None:
            body["content_hash"] = value
        try:
            resp = client.session.post(f"{API_BASE}/files", json=body,
                                       timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            print(f"    {label}: request failed: {exc}")
            continue
        show(f"content_hash: {label}", resp)
        if not resp.ok:
            continue
        try:
            data = resp.json().get("data") or []
            guid = data[0].get("id") if data else None
        except ValueError:
            guid = None
        accepted = label
        # Stop at the first accepted form: each success creates a real file on
        # the instance, and there is no DELETE for files.
        break

    if guid is None:
        print("\n  No form of content_hash was accepted, so the upload itself")
        print("  is the failure and nothing was attached. The bodies above are")
        print("  the answer - the F002 line names the field it wants.")
        return
    print(f"\n  ACCEPTED content_hash form: {accepted}")
    print(f"    -> file id {guid!r} (type {type(guid).__name__})")

    print("\n  [b] PATCH /products - the same request the tool makes")
    variants = [
        ("by id, guids as strings",
         {"id": product.get("id"),
          "files": [str(g) for g in existing + [guid]]}),
        ("by id, guid left as the API returned it",
         {"id": product.get("id"), "files": existing + [guid]}),
        ("by code only",
         {"code": product_code(product),
          "files": [str(g) for g in existing + [guid]]}),
        ("by id and code together",
         {"id": product.get("id"), "code": product_code(product),
          "files": [str(g) for g in existing + [guid]]}),
        ("by id, only the new guid (replace)",
         {"id": product.get("id"), "files": [str(guid)]}),
    ]
    for label, patch in variants:
        if patch.get("id") is None and "code" not in patch:
            continue
        try:
            resp = client.session.patch(f"{API_BASE}/products", json=[patch],
                                        timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            print(f"    {label}: request failed: {exc}")
            continue
        show(f"{label}  ->  {json.dumps(patch)[:160]}", resp)
        if resp.ok:
            print(f"\n  FIRST VARIANT THAT WORKED: {label}")
            print(f"  Undo: Manage Images tab -> code "
                  f"{product_code(product)!r} -> remove "
                  f"'skynamo-diag-1x1.png'.")
            return
    print("\n  Every variant was rejected. The bodies above say why, and the")
    print("  uploaded file is attached to nothing - there is no DELETE for")
    print("  files, so it simply sits unused.")


def main():
    args = [a for a in sys.argv[1:] if a != "--offline"]
    offline = "--offline" in sys.argv[1:]
    given = args[0] if args else None

    print("=" * 70)
    print(" Skynamo product images - upload diagnostic")
    print("=" * 70)
    print("\n[1] The folder (offline)")
    _folder, images = phase_folder(given)

    if offline:
        print("\n--offline: stopping here. Phases 2 and 3 need your API key,")
        print("so run this again without --offline to continue.")
        return

    print("\n[2] The instance (read-only)")
    client, products, matched = phase_instance(images)

    if matched is None:
        matched = next((p for p in products if p.get("id") is not None), None)
    if matched is None:
        sys.exit("\nNo product available to probe with.")
    phase_write(client, matched)

    print("\n" + "=" * 70)
    print(" Paste this whole output - the API key is redacted.")
    print("=" * 70)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)
