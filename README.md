# Skynamo Toolkit

A desktop + CLI tool for bulk operations and reporting against a Skynamo instance.
Each data-changing feature has a **preview step** so you approve results before
anything is committed:

1. **Customer geolocation** — fills in customer **latitude/longitude** by geocoding
   their address fields via **OpenStreetMap (Nominatim)** (free, no API key).
2. **Product image import** — matches local image files to products by the **product
   code** in the filename and uploads them, with an optional *replace existing* mode
   and a separate tab for **viewing and removing** images already on a product.
3. **Reporting** — extracts business data from Skynamo's **Reporting API** into a
   local SQLite store, using bookmark deltas so you never re-pull what you already
   have. See [§12](#12-reporting).
4. **Dashboards** — builds a self-contained, shareable **HTML dashboard** from that
   store. See [§13](#13-dashboards).

The desktop GUI has a tab per feature (five tabs). The CLI covers geolocation,
image import and image management; Reporting and Dashboards are GUI-only.

> **This tool talks to two different Skynamo APIs.** Features 1–2 use the **Public
> API** (`api.skynamo.me/v1`, API-key auth, included in your subscription). Feature 3
> uses the **Reporting API** (`analytics-api.svc.skynamo.me`, OAuth2 client
> credentials, read-only, and a **paid add-on**). They are separate products with
> separate credentials — [§12.1](#121-how-it-differs-from-the-public-api) explains why
> that matters.

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
    reports.py              # shared summarize() + write_report() used by every engine
    reporting_config.py     # Reporting API registry: endpoints, periods, rate limits, entities
    reporting_client.py     # ReportingClient: OAuth2 token cache, per-period throttle, filter builder
    report_store.py         # ReportStore: SQLite schema/upsert/bookmarks/run history
    report_engine.py        # plan_extract + run_extract (reporting core)
    dashboard.py            # build_dashboard -> one self-contained HTML file
    settings.py             # non-secret config JSON (no credentials stored)
  gui.py                    # CustomTkinter desktop app, a tab per feature (entry point for the .exe)
  skynamo_geolocation.py    # CLI front-end: geo / images / manage (thin wrapper over the engines)
  build.bat                 # PyInstaller -> dist/SkynamoGeo.exe
  requirements.txt          # runtime deps
  requirements-build.txt    # build-only deps (pyinstaller)
  test_engine.py            # geolocation engine smoke tests (mocked client + geocoder)
  test_geocoder.py          # OSM precision mapping + query building (offline)
  test_products.py          # image filename parsing/escaping/matching (offline)
  test_image_engine.py      # image engine preview/commit (mocked client, offline)
  test_cli_images.py        # CLI image flows: scripted prompts, fake client (offline)
  test_reporting_client.py  # OAuth/token/throttle/filter building (fake session, offline)
  test_report_store.py      # SQLite schema/upsert/bookmarks (in-memory, offline)
  test_report_engine.py     # extract plan/run (fake client, offline)
  test_dashboard.py         # HTML dashboard render + escaping (offline)
  test_gui_smoke.py         # builds the GUI (all five tabs) without interaction
  skynamo_swagger.json      # downloaded Skynamo API spec (reference only)
  docs/                     # Skynamo API knowledge articles (see section 5)
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
Starts with a menu of three features; interactive prompts then mirror the
equivalent GUI steps, including the preview-before-commit confirmation and the
CSV report (written to the current directory).

Pass the feature name to skip the menu:
```
py skynamo_geolocation.py geo       # geocode customer addresses
py skynamo_geolocation.py images    # upload product images from a folder
py skynamo_geolocation.py manage    # list / remove images on one product
py skynamo_geolocation.py --help
```
Reporting and Dashboards are GUI-only — the reporting engine is UI-agnostic, so
a CLI flow is possible, but the extract is long-running and rate-limited enough
that the tabbed UI with a progress bar and cancel is the better fit.

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

### Further reading — the knowledge articles in `docs/`

The notes above cover only the slice of the API this toolkit uses. Three standalone articles document
the wider picture:

- **[Skynamo Public API — Complete Reference](docs/skynamo-public-api-guide.md)** — all 62 paths and
  118 operations of `v1.0.28`: connecting, pagination, the filter syntax, write semantics, files,
  custom fields, an explicit can/cannot list, troubleshooting, and a full field reference for every
  schema.
- **[Pulling Skynamo data for BI with the Public API](docs/skynamo-public-api-for-bi.md)** — the
  extraction-capability matrix, three delta tiers, the `row_version` watermark pattern, Python and
  Power Query extractors, and a dimensional model.
- **[Skynamo Reporting API and the Power BI Connector](docs/skynamo-reporting-api-and-powerbi.md)** —
  the *separate, paid* Analytics API (`v2.0`): OAuth2 client-credentials, its filter query language,
  reporting periods, bookmark-based deltas, published rate limits, and the official Power BI
  connector.

> `skynamo_swagger.json` is `v1.0.27`; the articles are written against the live `v1.0.28`
> (`https://apidocs.skynamo.com/swagger_2.0.1023_1.0.28.json`). The only differences are
> `DELETE /visitfrequencies` and the `ignore_deals` flag on `/dealgroups`.

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
py test_cli_images.py    # CLI image flows: confirmation gating, deselection, replace warning
py test_reporting_client.py  # OAuth token cache/refresh, 401 retry, 429 backoff, throttle, filters
py test_report_store.py      # schema from registry, idempotent upsert, period-scoped bookmarks
py test_report_engine.py     # plan makes no calls; bookmark advances only after a commit
py test_dashboard.py         # renders from a seeded store; self-contained; escapes instance text
py test_gui_smoke.py     # GUI (all five tabs) builds and tears down cleanly
```
Every suite is offline: fake clients/sessions, in-memory SQLite, and byte
fixtures in temp folders — no network and no real credentials. They assert the
things that matter structurally: preview/plan phases write nothing and make no
API calls, only approved rows are committed, bookmarks never advance before their
rows commit, and the dashboard never references an external resource.

**Not yet automated:** a true end-to-end run against a live Skynamo instance. Use
each tab's preview/plan step to eyeball results before committing, and see
[§12.2](#122-credentials) for the Reporting API credentials needed to try that
side at all.

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
- **Reporting/Dashboards in the CLI**: `report_engine` and `dashboard` are
  UI-agnostic like everything else, so a `report`/`dash` feature could be added
  to `skynamo_geolocation.py` alongside the existing three with no core changes.
- **More reporting entities**: add an entry to `REPORTING_ENTITIES` in
  `skynamo_geo/reporting_config.py` — the client, store schema and extract engine
  all drive off that registry, so no other code changes.

---

## 12. Reporting

The **Reporting** tab extracts business data into a local database, which the
Dashboards tab then reads. It uses Skynamo's **Reporting API** — a different
product from the Public API the rest of this tool uses.

### 12.1 How it differs from the Public API

| | Public API | Reporting API |
|---|---|---|
| Host | `api.skynamo.me/v1` | `analytics-api.svc.skynamo.me` |
| Auth | `X-API-KEY` + `X-API-CLIENT` | **OAuth2 client credentials → JWT** |
| Cost | included | **paid add-on** |
| Direction | read **and write** | **read only** |
| Dates | build it yourself | 21 named reporting periods, financial-year aware |
| Deltas | `row_version` on some paths | **bookmarks** (`x-bookmark` header) |
| Rate limits | undocumented | **published and tight** (see below) |

Full reference: [docs/skynamo-reporting-api-and-powerbi.md](docs/skynamo-reporting-api-and-powerbi.md).

### 12.2 Credentials

**Skynamo insights → Settings → Integration Tokens → *Add client credential***.

> This is the same screen as the Public API key, with a **different button**.
> *Add access token* gives you an `x-api-key` (Public API); *Add client credential*
> gives you a **Client ID + Client Secret** pair (Reporting API). Clicking the wrong
> one is the most common setup mistake. If the button is missing, the paid add-on
> probably isn't enabled on your subscription.

As with the Skynamo API key, **neither the Client ID nor the Secret is ever
stored** — you re-enter them each session. Only your period and entity selection
persist.

### 12.3 Reporting periods and rate limits

You pick a named period rather than building date filters. The rate limit **scales
inversely with how much data the period covers**, so choose the shortest period
that answers your question:

| Limit | Periods |
|---|---|
| 30 queries / 30s | `ThisDay`, `PrevDay`, `ThisWeek`, `PrevWeek`, `This30Days`, `Prev30Days`, `ThisMonth`, `PrevMonth` |
| 4 queries / minute | `This90Days`, `Prev90Days`, `FinThisQuarter`, `FinPrevQuarter` |
| 4 queries / 10 min | `This180Days`, `Prev180Days`, `This365Days`, `Prev365Days`, `FinThisYear`, `FinPrevYear` |
| **2 queries / 10 min** | `AllData` |

The tab shows the allowance for your current selection and warns when a run will be
throttled. It self-throttles rather than failing, so a large run just takes longer.
**`AllData` is for a one-off backfill, not a schedule** — seed history once, then
use a short period with bookmarks.

### 12.4 How an extract works

1. **Plan Extract** — reads the stored bookmarks and shows, per entity, whether it
   will be a **full** load or a **delta**, and what it will cost. **Makes no API
   calls at all.**
2. Untick anything you don't want (same tick-the-row behaviour as the other tabs).
3. **Run Extract** — fetches each entity (sub-entities expanded in the *same* call,
   which is far cheaper than paging), merges the rows into the store by primary key,
   records the run, and only *then* advances the bookmark. A failure on one entity
   never aborts the rest.
4. **Save Report CSV** for a per-entity record of what happened.

Entities in this version: `activities` (with visits, order totals, order lines),
`customers` (with invoices, targets), `users` (with targets), `products`,
`invoices`.

The store lives at `%APPDATA%\SkynamoGeo\reporting.db` (plain SQLite — open it with
any SQLite tool if you want to query it directly). Re-extracting the same rows is
idempotent, so running twice never duplicates data.

> **Two caveats worth knowing.** Bookmarks report *added* data, not deletions, so
> rows deleted in Skynamo linger until a reconcile marks them deleted. And the API
> spec has known defects — two endpoints declare the wrong response schema and 7 of
> 11 are undocumented — so the column definitions in `reporting_config.py` should be
> confirmed against your instance before you rely on them.

---

## 13. Dashboards

The **Dashboards** tab renders the local store into **one self-contained `.html`
file**: charts are inline SVG, the CSS is inline, and nothing loads from the
internet. It opens in any browser, prints cleanly, and can be emailed as-is.

Because it reads only the local store, **building a dashboard makes no API calls**
— it's free to rebuild as often as you like, and costs nothing against your rate
limit.

Panels: an overview KPI row (orders and value, invoiced, outstanding, visits,
active customers), order value over time, top customers, top products, visits by
user, visit type (on-site/off-site/scheduled), targets vs actuals, and a **data
freshness** panel showing when each entity was last extracted and the exact server
window (`x-date-range`) it used — so nobody mistakes a stale dashboard for a live
one.

Any panel with no data says so explicitly rather than rendering an empty box. If
the whole store is empty, the tab tells you to run an extract first.

---

## 14. Change log

- **v2.8.0** (2026-08-27) — **Product images in the CLI.** The CLI now opens with
  a feature menu — `geo`, `images`, `manage` — or takes the feature as an
  argument (`py skynamo_geolocation.py images`) to skip it; `--help` prints usage.
  **images** matches a folder against product codes, prints a match summary
  (including *why* each unmatched/ambiguous/unsupported file was rejected) and how
  many images each product already has, then uploads only after you confirm, with
  optional individual selection. **manage** looks a product up by code, resolves
  its attached images to filenames and detaches the ones you tick. Replace mode
  spells out exactly which products will lose existing images and asks a second
  time. Both write the same CSV reports the GUI does. Credentials and connection
  are now a shared step, and the geolocation flow is unchanged. New offline suite
  `test_cli_images.py` drives the flows with scripted prompts, asserting nothing
  uploads before confirmation and that deselection is honoured.
- **v2.7.0** (2026-08-27) — **Reporting connector + Dashboards.** Two new tabs.
  **Reporting** talks to Skynamo's Reporting API (a separate paid product: OAuth2
  client credentials, read-only) and extracts activities/customers/users/products/
  invoices into a local SQLite store at `%APPDATA%\SkynamoGeo\reporting.db`, using
  bookmark deltas so repeat runs only fetch what changed. Plan-then-run: the plan
  phase makes **no API calls** and estimates the cost against the period's published
  rate limit (which the client also self-throttles to). **Dashboards** renders that
  store into one self-contained HTML file — inline SVG charts, no external requests,
  no new dependencies — with a data-freshness panel showing the server window each
  extract actually used. New core: `reporting_config.py` (the entity registry — the
  one place to add entities), `reporting_client.py`, `report_store.py`,
  `report_engine.py`, `dashboard.py`, plus `reports.py` which de-duplicates the CSV
  writer and `summarize` that were copied across the engines. Four new offline test
  suites. Also corrected a docs claim: files and products have no DELETE endpoint,
  but the Public API does have DELETEs for invoices/tasks/scheduled visits/order
  statuses/deal groups.
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
