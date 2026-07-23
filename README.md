# Skynamo Toolkit

A desktop + CLI tool for bulk operations against a Skynamo instance's public API.
Two features, each with a **preview step** so you approve results before anything
is committed:

1. **Customer geolocation** — fills in customer **latitude/longitude** by geocoding
   their address fields via **OpenStreetMap (Nominatim)** (free, no API key).
2. **Product image import** — matches local image files to products by the **product
   code** in the filename and uploads them, with an optional *replace existing* mode
   and a separate tab for **viewing and removing** images already on a product.

The desktop GUI has a tab per feature; the CLI covers geolocation.

---

## 1. What it does (end to end)

1. **Connect** to a Skynamo instance with an instance name + Skynamo API key.
2. **Fetch** all customers (paginated). **Inactive customers are skipped** —
   only customers with the top-level `active` flag set are processed.
3. **Map** custom fields as the address source and **tag each with its role**
   (street / city / state / postcode / country / other) — field names differ per
   instance, so the user chooses. Roles drive a structured, correctly-ordered
   query; junk values ("0", "N/A", blanks) are stripped.
4. **Geocode** each address via OpenStreetMap/Nominatim (free, no key,
   ~1 address/second). It uses the role components for a structured search
   (much more accurate than a free-form string) and keeps the most precise of
   several candidates.
5. **Preview** the results (coordinates, precision, confidence) — *no writes yet*.
6. **Write** the approved rows back to Skynamo via `PATCH /customers`.
7. **Report** a console/table summary + a CSV, including customers with no
   address and low-confidence matches flagged for manual review.

---

## 2. Project layout

```
GeoLocation_Script/
  skynamo_geo/              # UI-agnostic core (reusable by any front-end)
    __init__.py
    config.py               # constants: URLs, page size, accuracy map, statuses, image rules
    client.py               # SkynamoClient: connect, fetch customers/products, update_location, upload_file, get_file, attach_files
    geocoder.py             # Geocoder base + NominatimGeocoder, GeocodeResult/Error
    customers.py            # address helpers (build_query/clean_value/collect/has_coordinates)
    products.py             # image helpers (filename parsing, code escaping, matching, format sniff)
    engine.py               # geocode_customers + write_locations + report (geolocation core)
    image_engine.py         # scan_images + upload_images + list_attached_images + delete_selected_images (product-image core)
    settings.py             # non-secret config JSON (no credentials stored)
  gui.py                    # CustomTkinter desktop app, a tab per feature (entry point for the .exe)
  skynamo_geolocation.py    # CLI front-end for geolocation (thin wrapper over the engine)
  build.bat                 # PyInstaller -> dist/SkynamoGeo.exe
  requirements.txt          # runtime deps
  requirements-build.txt    # build-only deps (pyinstaller)
  test_engine.py            # geolocation engine smoke tests (mocked client + geocoder)
  test_geocoder.py          # OSM precision mapping + query building (offline)
  test_products.py          # image filename parsing/escaping/matching (offline)
  test_image_engine.py      # image engine preview/commit (mocked client, offline)
  test_gui_smoke.py         # builds the GUI (both tabs) without interaction
  skynamo_swagger.json      # downloaded Skynamo API spec (reference only)
  README.md                 # this file
```

**Design principle:** the core in `skynamo_geo/` knows nothing about any UI.
Both the GUI and the CLI call the same `engine` functions, so behaviour stays
identical and new front-ends (or a future web/scheduled runner) can reuse it.

---

## 3. Running it

### GUI (recommended for end users)
```
py -m pip install -r requirements.txt
py gui.py
```
The window has a tab per feature. **Customer Geolocation**: **Connect & Load
Customers** → tick address field(s) → **Preview (geocode only)** → review/untick
rows → **Write Selected to Skynamo** → **Save Report CSV**. **Product Images**:
**Connect & Load Products** → **Choose folder…** (optionally tick *Replace
existing images*) → **Preview (match only)** → review/untick rows → **Upload
Selected to Skynamo** → **Save Report CSV**. **Manage Images** (reuses the
Product Images connection): type a product code → **Load Images** → tick images →
**Remove Selected from Product** → **Save Report CSV**. Each tab has a **Cancel**
button; work happens on a background thread so the window never freezes.

The GUI uses a dark theme built on a `#1a1a1a` (rgb 26,26,26) background with
card-style panels; the palette constants live at the top of `gui.py`.

### CLI
```
py -m pip install -r requirements.txt
py skynamo_geolocation.py
```
Interactive prompts mirror the GUI steps, then geocodes and writes in one pass.

### Standalone .exe (no Python on the target machine)
```
py -m pip install -r requirements.txt -r requirements-build.txt
build.bat
```
Produces `dist\SkynamoGeo.exe` (single, double-clickable, no console window).
> One-file exes can trip antivirus/SmartScreen heuristics on first run — you may
> need to allow it.

---

## 4. Credentials & settings

- **OpenStreetMap (Nominatim)** — no key or account needed. The public service
  is rate-limited to ~1 request/second (the tool throttles itself).
- **Skynamo API key** — Skynamo Insights → Settings → Integration Tokens →
  *Add access token*.

**API keys are never stored.** You re-enter the Skynamo key every time you
launch the app. The GUI's **"Remember settings"** checkbox only persists
**non-secret settings** (instance name, country, replace flag, selected
address fields) to `%APPDATA%\SkynamoGeo\config.json`. Any API keys that an
earlier version saved to the Windows Credential Manager are purged on startup.

---

## 5. Skynamo API reference (as used here)

- **Base URL:** `https://api.skynamo.me/v1`
- **Auth headers:** `X-API-CLIENT: <instance name>`, `X-API-KEY: <api key>`,
  `Content-Type: application/json`
- **List customers:** `GET /customers?page_number=N&page_size=200`
  (max page size 200). Response: `{ "data": [...], "page": { "total_item_count": N, ... } }`.
  Each customer has a top-level boolean `active` (default `true`).
  `SkynamoClient.fetch_all_customers(active_only=True)` filters out inactive
  customers by default (pagination still counts raw rows, so termination is
  unaffected). Pass `active_only=False` to include them.
- **Update location:** `PATCH /customers` with an **array** of objects:
  ```json
  [{ "id": 123, "location": {
       "latitude": -33.92, "longitude": 18.42,
       "accuracy": 10, "is_approximate": false } }]
  ```
  > Note: `/customers/{id}` is **GET-only**. Updates must go to the collection
  > endpoint `PATCH /customers`. (A wrong route returns AWS API Gateway's
  > misleading `{"message":"Missing Authentication Token"}` — that means *route
  > not found*, not an auth problem.)
- Address data lives in each customer's `custom_fields` array
  (`[{id, name, value}]`), never as top-level fields. Field names vary per
  instance, which is why mapping is interactive.
- **List products:** `GET /products?page_number=N&page_size=200` (same envelope
  and paging as customers). Each product has `id`, `code` (unique), `name`,
  `active`, and `files` (an array of file GUIDs).
- **Upload a file:** `POST /files` with `{ "filename": "...", "content": "<base64>" }`.
  The response's `data[].id` is the created file's **GUID**.
- **Attach a file to a product:** `PATCH /products` (collection endpoint, array
  body) with `[{ "code": "ABC", "files": ["<guid>", …] }]` — matched by `code`.
  Send the full desired `files` list; this same call is used to attach (merge),
  replace, and detach — it just sets the product's `files` to exactly what you send.
- **Read a file:** `GET /files/{guid}` returns the file's metadata (incl.
  `filename`) — used to show attached images by name.
- **No delete endpoint exists.** There is no `DELETE /files/{guid}` and no
  `DELETE /products/{id}`. Removing an image from a product means PATCHing the
  product's `files` list without that GUID (detach); the file object itself is
  not deletable via the public API.

The full spec is saved in `skynamo_swagger.json` for reference.

---

## 6. Geocoding & accuracy logic

### Role-based field mapping (accuracy starts here)
Each chosen Skynamo field is tagged with the address component it holds —
**street / city / state / postcode / country**, or **other** (folded into the
street line). From that mapping `customers.build_query` produces an
`AddressQuery` with two forms:
- `text` — a clean, canonically-ordered single-line address (street, other,
  city, state, postcode, country) used as a fallback query and for
  display/reports;
- `structured` — a `{street, city, state, postalcode, country}` dict used for
  Nominatim's **structured search**.

Values are cleaned first (`clean_value`): whitespace collapsed and junk
placeholders — `""`, `"0"`, `"-"`, `"N/A"`, `"none"`, `"null"`, `"."` — dropped
so they never skew a match.

### OpenStreetMap (Nominatim) accuracy
- Uses the `structured` components rather than one free-form string — far more
  precise — and **falls back** to the free-form `text` only if that finds
  nothing.
- Fetches several candidates (`limit=5`, `addressdetails=1`, `dedupe=1`) and
  keeps the **most precise** one (`_pick_best`: building > road > area, ties
  broken by Nominatim's `importance`) instead of blindly taking the first.

### Precision → accuracy
Nominatim reports how precise each match is, which we translate into the
Skynamo `accuracy` value (metres) so downstream reports can trust precise pins
and treat coarse ones as approximate:

| OSM precision (bucketed `addresstype`) | Meaning                | accuracy (m) | Confidence |
|----------------------------------------|------------------------|--------------|------------|
| `OSM_BUILDING`                          | building/house/amenity | 25           | high       |
| `OSM_ROAD`                              | street-level match     | 200          | **low**    |
| `OSM_AREA`                              | suburb/town centroid   | 3000         | **low**    |

- Any of the **low** precisions above is treated as **low confidence**: written
  with `is_approximate=true`, a coarse accuracy, and surfaced in the
  report/table for manual review.
- **Result validation** (`engine._validate_result`): the matched country and
  postcode are compared against the input. A country mismatch (vs the 2-letter
  code) or a postcode mismatch (vs a mapped postcode field) also flags the row
  low-confidence with a note — catching pins that landed in the wrong place even
  when the provider reported a "precise" match.
- An optional **2-letter country code** (e.g. `ZA`) restricts geocoding to that
  country (`countrycodes=xx`), which removes wrong-continent matches for bare
  street names.
- See `ACCURACY_BY_PRECISION` and `LOW_CONFIDENCE_PRECISIONS` in
  `skynamo_geo/config.py`. (Standing rule: always send an `accuracy` value;
  derive it from precision when available, otherwise default ≥1000.)

---

## 6a. Product image import

Point the **Product Images** tab at a folder of images. Each image is matched to
a Skynamo product by the **product code** in its filename, then uploaded and
attached to that product.

### Naming rules
- Name each file after the product code: `ABC.png` → product `ABC`.
- **Multiple images per product** use a trailing sequence, separated by `_` or a
  space, using digits *or* a letter for order:
  `ABC_1`, `ABC_2` / `ABC 1`, `ABC 2` / `ABC_A`, `ABC A`.
- If a product code contains a character that can't appear in a filename
  (`/ \ : * ? " < > |`), write it as a **hyphen** (`-`): code `AB/C` → file `AB-C.png`.
- Only **PNG** and **JPG/JPEG** are supported (checked by extension *and* by the
  file's actual content, so a mis-named file is caught).

### How matching works
Filenames are ambiguous (is `-` a real hyphen or an escaped `/`? is `ABC_1` the
code `ABC` plus sequence 1, or a literal code `ABC_1`?), so the tool doesn't try
to reverse a filename into a code. Instead it applies the **same** hyphen-escaping
to every real product code and matches the filename against that set — trying the
whole filename as a literal code first, then the code with a trailing sequence
marker removed. If two different codes escape to the same filename form, the match
is flagged **ambiguous** and skipped rather than guessed.

### Upload
Skynamo has no dedicated product-image endpoint, so upload is two steps:
`POST /files` (the image, base64-encoded) returns a file **GUID**, then
`PATCH /products` attaches it via `{ "code": "...", "files": [guid, …] }`.
Images for one product upload in sequence order. The preview table shows an
**Existing** column (how many images the matched product already has).

**Merge (default):** the tool sends the **union** of the product's existing
files plus the new ones, so images already on the product are preserved.

**Replace (tick "Replace existing images"):** the product's files list is set to
*only* this run's uploaded images — anything it had before is dropped (detached).
The report notes how many existing images were replaced.

### Manage / remove existing images
The **Manage Images** tab views and removes images already on a product. Enter a
product code and **Load Images**; the tool resolves each attached file's GUID to
its filename (`GET /files/{guid}`) and lists them. Tick the ones to remove and
**Remove Selected from Product**.

> **"Remove" means detach, not delete.** Skynamo's public API has *no* delete
> endpoint (no `DELETE /files/{guid}`, no `DELETE /products/{id}`). Removal
> re-`PATCH`es the product's `files` list without the ticked GUIDs, so the image
> no longer shows against the product — but the underlying file may still exist
> on Skynamo's servers.

### Log & report
Every outcome shows in the on-screen table and log, and **Save Report CSV**
writes a report — for uploads: `filename, product_code, matched_product,
sequence, status, notes` (statuses `pending-upload`, `uploaded`,
`no-matching-product`, `unsupported-format`, `ambiguous-match`, `upload-failed`);
for removals: `product_code, matched_product, filename, file_guid, status, notes`
(statuses `attached`, `fetch-failed`, `removed`, `remove-failed`).

---

## 7. The engine (skynamo_geo/engine.py)

Two phases, so any UI can do **preview-then-commit**:

- `geocode_customers(geocoder, customers, field_roles, replace_existing,
  country, on_progress, should_cancel) -> list[Plan]`
  Decides skip reasons (has-coords / no-address), builds each address from
  `field_roles` (an ordered list of `(field_name, role)`), geocodes the rest,
  validates the result, and builds `Plan` objects. **Performs no writes.**
- `write_locations(client, plans, on_progress, should_cancel) -> report_rows`
  PATCHes only plans where `include` is true and the plan is `writable`
  (has coordinates). Updates each plan's status in place.
- `summarize(plans)` — counts by status. `write_report(rows, path)` — CSV.

Both accept:
- `on_progress(event)` — `event = {phase, index, total, name, status, message}`,
  emitted per item. The GUI pushes these onto a queue and updates widgets on the
  main thread; the CLI prints them.
- `should_cancel()` — returns `True` to stop cleanly (the GUI's Cancel button).

`replace_existing=False` (default) only fills in **missing/zero** coordinates;
`True` overwrites existing ones. Zero/`"0"`/null lat-or-lng all count as missing
(see `has_coordinates` in `customers.py`).

---

## 8. Report / CSV columns

`customer_id, code, name, status, address_used, latitude, longitude,
accuracy, match_precision, notes`

Statuses: `updated`, `updated-low-confidence`, `skipped-has-coordinates`,
`skipped-no-address`, `geocode-failed`, `update-failed` (and `pending-write`
during preview, before committing).

---

## 9. Testing

```
py test_engine.py        # geolocation engine: plan statuses, no-write-in-preview, accuracy
py test_geocoder.py      # OSM precision mapping + query building
py test_products.py      # image filename parsing/escaping/matching/format sniff
py test_image_engine.py  # image engine: match/upload, replace mode, list/remove attached images
py test_gui_smoke.py     # GUI (both tabs) builds and tears down cleanly
```
The engine tests use a fake client (and fake geocoder) — no network, no real API
keys — and assert that preview writes/uploads nothing and only approved rows are
committed. `test_products`/`test_image_engine` build byte fixtures in a temp
folder.

**Not yet automated:** a true end-to-end run against a live Skynamo test
instance. Use each tab's preview step to eyeball results before committing.

---

## 10. Extending it (where to plug in)

- **Another geocoder** (e.g. Mapbox): subclass `Geocoder` in `geocoder.py` and
  construct it in the GUI/CLI connect flow in place of `NominatimGeocoder()` —
  no engine changes needed.
- **Map preview** of pins before committing: a widget (e.g. `tkintermapview`) or
  Qt/web view consuming the existing `Plan` list.
- **Batch PATCH**: the Skynamo endpoint already accepts an array; optimise inside
  `write_locations` only.
- **Headless/scheduled runs**: call `engine.geocode_customers` +
  `engine.write_locations` (or `image_engine.scan_images` +
  `image_engine.upload_images`) directly — the core has no UI dependency.
- **Product images in the CLI**: `skynamo_geolocation.py` covers geolocation only
  today; `image_engine` + `products` are UI-agnostic, so a CLI flow can be added
  with no core changes.

---

## 11. Change log

- **v2.5.0** (2026-07-23) — **Replace + manage product images.** The Product
  Images tab gains a **Replace existing images** option (upload sets the
  product's files to only this run's images instead of merging) and an
  **Existing** count column in the preview. New **Manage Images** tab: look up a
  product by code, list its attached images by filename (`GET /files/{guid}`),
  and remove (detach) selected ones. Skynamo has no delete endpoint, so removal
  re-PATCHes the product's `files` list without the removed GUIDs — the file may
  persist server-side. New: `client.get_file`; `image_engine` gains
  `replace_existing`, `AttachedImage`, `list_attached_images`,
  `delete_selected_images`; extended offline tests.
- **v2.4.0** (2026-07-23) — **Product image import.** New **Product Images** tab
  (the GUI is now a tab per feature): match a folder of images to products by the
  code in each filename and upload them. Naming supports multi-image sequences
  (`CODE_1`/`CODE 2`/`CODE_A`) and hyphen-escaped reserved characters; PNG/JPEG
  only (validated by content). Matching forward-transforms real product codes to
  their filename form and flags ambiguous collisions. Upload is `POST /files`
  (base64) → `PATCH /products` with the file GUID, preserving a product's existing
  files. On-screen log + CSV report of every outcome. New core: `products.py`,
  `image_engine.py`; new client methods `fetch_all_products`/`upload_file`/
  `attach_files`; new offline tests `test_products.py`/`test_image_engine.py`.
- **v2.3.0** (2026-07-23) — **Removed Google Maps.** Geocoding is now
  OpenStreetMap (Nominatim)-only — no per-lookup cost, no API key. Removed
  `GoogleGeocoder`, the provider picker (GUI segmented button, CLI prompt),
  the Google API key field, `GEOCODER_PROVIDERS`/`DEFAULT_PROVIDER`/
  `create_geocoder`, and the Google-derived location-type accuracy tiers
  (`ROOFTOP`/`RANGE_INTERPOLATED`/`GEOMETRIC_CENTER`/`APPROXIMATE`). The GUI
  and CLI now construct `NominatimGeocoder()` directly.
- **v2.2.0** (2026-07-09) — **Accuracy overhaul.** Address fields are now mapped
  to **roles** (street/city/state/postcode/country/other) in the GUI (a dropdown
  per field) and CLI. Nominatim uses those roles for a **structured search** with
  multi-candidate selection (`_pick_best`) and a free-form fallback — a big OSM
  precision gain; the same clean, role-ordered query also helps Google. Field
  values are cleaned (junk like "0"/"N/A" dropped). New **result validation**
  flags country/postcode mismatches as low-confidence. `geocode_customers` now
  takes `field_roles`; `config.py` gains role + cleaning constants; new offline
  tests in `test_geocoder.py`/`test_engine.py`.
- **v2.1.1** (2026-07-03) — **API keys are no longer remembered.** The GUI never
  stores the Skynamo or Google keys; the checkbox (now "Remember settings")
  persists only non-secret settings. Any keys saved by an earlier version are
  purged from the OS keyring on startup (`settings.purge_saved_credentials`).
- **v2.1.0** (2026-07-02) — Added **OpenStreetMap (Nominatim)** as a second
  geocoding provider, selectable in the GUI (segmented button; Google key field
  disabled when OSM is chosen) and CLI (select prompt). Free, no API key,
  self-throttled to 1 req/s per the Nominatim usage policy; OSM matches map to
  `OSM_BUILDING`/`OSM_ROAD`/`OSM_AREA` accuracy tiers. Provider choice persists
  in config. Restyled the GUI: dark theme on a `#1a1a1a` base, card panels,
  accent-coloured step badges, modern buttons/entries, dark results table.
  New offline `test_geocoder.py`.
- **v2.0.1** — `fetch_all_customers` now skips inactive customers by default
  (top-level `active` flag); `active_only=False` opts back in.
- **v2.0.0** — Refactored the single script into the `skynamo_geo` package;
  added the CustomTkinter GUI with preview-then-commit, background threading,
  cancel, secure credential storage (keyring), settings persistence, and a
  PyInstaller `.exe` build. CLI rewritten to share the engine.
- **v1.x** — Single-file CLI. Switched geocoding from Nominatim to Google Maps
  for accuracy; added precision-derived accuracy + low-confidence flagging;
  fixed the update route to `PATCH /customers` (array body); corrected
  pagination to `page_number`/`page_size`.
```
(When you add changes, append a dated entry here and update the relevant section above.)
```
