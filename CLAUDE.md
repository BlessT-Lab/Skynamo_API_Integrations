# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Skynamo toolkit with two features that talk to a Skynamo instance's public API:
1. **Customer geolocation** — fills in customer latitude/longitude by geocoding their address
   fields via OpenStreetMap (Nominatim) — free, no API key.
2. **Product images** — matches local image files to products by the code in the filename and
   uploads them (`POST /files` → `PATCH /products` with the file GUID), in merge or replace mode;
   and a separate tab to view/remove the images already attached to a product.

Ships as a CustomTkinter desktop GUI (three tabs — geolocation, image import, image management)
plus an interactive CLI (geolocation only, so far). See [README.md](README.md) for the full
user-facing walkthrough and Skynamo API reference.

## Commands

```
py -m pip install -r requirements.txt                       # runtime deps
py gui.py                                                    # run the GUI
py skynamo_geolocation.py                                    # run the CLI
py test_engine.py                                            # geolocation engine smoke tests (mocked, no network/keys)
py test_geocoder.py                                          # OSM precision mapping + query building (offline)
py test_products.py                                          # image filename parsing/escaping/matching (offline)
py test_image_engine.py                                      # image engine preview/commit (mocked client, offline)
py test_gui_smoke.py                                         # builds the GUI (both tabs) and tears it down
py -m pip install -r requirements-build.txt && build.bat     # -> dist/SkynamoGeo.exe (PyInstaller)
```

Tests are plain assert-based scripts run directly with `py`, not pytest — there is no single-test
runner; edit the script or add a new `test_*.py`. This is a Windows environment (`py` launcher,
`build.bat`); the Bash tool is also available for POSIX-style commands.

## Architecture

The central principle: **`skynamo_geo/` is UI-agnostic core; `gui.py` and `skynamo_geolocation.py`
are thin front-ends that call the same engine.** Keep business logic out of the front-ends so
behaviour stays identical across GUI and CLI — this is the main invariant to preserve.

- `skynamo_geo/engine.py` is the geolocation heart. **Two-phase preview-then-commit**:
  `geocode_customers(...)` builds `Plan` objects and performs **no writes**; `write_locations(...)`
  PATCHes only plans where `include` is true and the plan is `writable`. Both take an
  `on_progress(event)` callback and a `should_cancel()` predicate — that's how the GUI streams
  progress and cancels without the core knowing anything about threads or widgets.
- `skynamo_geo/image_engine.py` — the **same two-phase shape** for product images, twice over:
  `scan_images(products, folder, ...)` builds `ImagePlan` objects (matches files to products, **no
  uploads**) → `upload_images(client, plans, replace_existing=..., ...)` uploads approved plans and
  attaches file GUIDs; and `list_attached_images(client, product, ...)` builds `AttachedImage`
  objects (resolves a product's file GUIDs to names, **no writes**) → `delete_selected_images(...)`
  detaches the ticked ones. Same `on_progress`/`should_cancel` contracts, so the GUI drives all of
  it identically to the geo engine. `write_report(rows, path, fieldnames=...)` serves every report.
- `skynamo_geo/products.py` — image/product helpers. `escape_code_to_filename`, `parse_image_stem`,
  `sequence_sort_key`, `sniff_image_format`, `build_code_index`, `collect_image_files`.
- `skynamo_geo/geocoder.py` — `Geocoder` base class; `NominatimGeocoder` (OpenStreetMap: free, no
  key, self-throttled to 1 req/s per usage policy) is the only implementation; front-ends construct
  it directly (`NominatimGeocoder()`). Adding another provider = new `Geocoder` subclass, zero
  engine changes.
- `skynamo_geo/client.py` — `SkynamoClient`. `fetch_all_customers`/`fetch_all_products`
  (`active_only=True` skips inactive). Writes go to the **collection** endpoint with an **array**
  body: `update_location` → `PATCH /customers`, `attach_files` → `PATCH /products` (by `code`);
  `/{id}` routes are GET-only. `upload_file` → `POST /files` with base64 `content`, returns the file GUID.
- `skynamo_geo/config.py` — all constants (endpoints, `ACCURACY_BY_PRECISION`, `STATUS_*`,
  `STATUS_IMG_*`, `STATUS_ATT_*`, `REPORT_FIELDNAMES`, `IMAGE_REPORT_FIELDNAMES`,
  `ATTACHED_IMAGE_REPORT_FIELDNAMES`, `ALLOWED_IMAGE_EXTENSIONS`, `WINDOWS_RESERVED_CHARS`).
  Changing statuses/columns/accuracy tiers/image rules happens here.
- `skynamo_geo/customers.py` — address helpers. `build_query(customer, field_roles)` returns an
  `AddressQuery` (`.text` single-line + `.structured` dict) from an ordered `(field_name, role)`
  list; `clean_value` drops junk; `has_coordinates` treats zero/`"0"`/null as missing.
- `skynamo_geo/settings.py` — non-secret config in `%APPDATA%/SkynamoGeo/config.json`. API keys are
  never persisted (re-entered each session); `purge_saved_credentials()` clears any keys older
  versions stored in the OS keyring, called on GUI startup.

### GUI threading model (gui.py)
The GUI is a `CTkTabview` with three tabs: **Customer Geolocation**, **Product Images**, and
**Manage Images** (`_build_geo_tab`/`_build_image_tab`/`_build_manage_tab`). Tkinter is not
thread-safe. Each tab runs its engine on its own `threading.Thread`, pushes events onto its own
`queue.Queue`, and the main thread drains it via `self.after(100, ...)` — the **only** place widgets
are touched. Cancel is a per-tab `threading.Event` passed in as `should_cancel`. The three tabs keep
fully separate state/queues/workers, distinguished by attribute prefix: the geo tab's plain names,
the Product Images tab's `img_*`/`_img_*`, and the Manage Images tab's `mgmt_*`/`_mgmt_*`. Product
Images and Manage Images share the one connected `SkynamoClient` + loaded `products` list (Manage
Images tells the user to connect on the Product Images tab first). Any new long-running work must
follow this pattern — never update a widget from a worker thread.

## Domain rules that aren't obvious from the code

- **Accuracy is precision-derived, not fixed.** Each provider's precision label maps to a Skynamo
  `accuracy` (metres) via `ACCURACY_BY_PRECISION`; coarse/partial matches are low-confidence, written
  with `is_approximate=true` and flagged for manual review. Always send an `accuracy` value.
- **Address fields are role-mapped, not just concatenated.** The user tags each field with a role
  (street/city/state/postcode/country/other). This feeds Nominatim a **structured search** (its big
  accuracy lever) — with multi-candidate selection (`_pick_best`) and a clean, canonically-ordered
  single-line query as a free-form fallback. `engine._validate_result` then flags country/postcode
  disagreements as low-confidence. Changing roles/tiers happens in `config.py`.
- **The Skynamo `PATCH /customers` "Missing Authentication Token" error means wrong route, not auth**
  (AWS API Gateway quirk).
- Address field names vary per instance and live only in each customer's `custom_fields` array, which
  is why field mapping is interactive.
- **Image→product matching is forward-transform, not reverse-parse.** Filenames are ambiguous (`-`
  could be a literal hyphen or an escaped reserved char; `ABC_1` could be code `ABC`+seq 1 or a
  literal code). So we apply the *same* escaping (`escape_code_to_filename`) to every authoritative
  product code and match filename stems against that set — trying the whole stem (literal code) first,
  then the stem with a trailing sequence marker stripped. Two codes escaping to the same form → the
  match is flagged **ambiguous**, not guessed.
- **Skynamo has no product-image endpoint.** Upload is generic: `POST /files` (base64 `content`) →
  file GUID → `PATCH /products` with `{code, files:[...]}`. In merge mode the `files` array is sent
  as the **union** of the product's existing GUIDs plus the new ones (don't clobber attached images);
  in **replace mode** (`upload_images(replace_existing=True)`) it's set to only this run's GUIDs.
  Format is gated on extension **and** magic bytes (`sniff_image_format`); PNG/JPEG only.
- **There is no delete endpoint anywhere in Skynamo's API** — no `DELETE /files/{guid}`, no
  `DELETE /products/{id}`. "Removing" an image (`image_engine.delete_selected_images`) means
  re-`PATCH /products` with the product's `files` list minus the removed GUIDs — it detaches the
  file from the product; the underlying file object may still exist server-side. `GET /files/{guid}`
  (`client.get_file`) resolves a GUID to a filename for display, since a product's `files` array is
  only ever bare GUID strings.

## Working agreements

- Before any GitHub-facing action (commit, push, PR, remote `git`/`gh`/API calls), show the exact
  planned steps/commands and wait for explicit approval. Local read-only inspection
  (`git status`/`log`/`diff`) is fine without asking.
- `gh` is not installed here; PRs are created via the GitHub API using the stored git credential.
- Keep the change log in [README.md](README.md) updated when behaviour changes.
