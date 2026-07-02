# Skynamo Customer Geolocation Updater

A desktop + CLI tool that fills in customer **latitude/longitude** on a Skynamo
instance by geocoding their address fields. It connects to the Skynamo public
API, reads customers, builds an address string from user-mapped custom fields,
geocodes each via **Google Maps or OpenStreetMap** (user's choice), and writes
the coordinates back — with a preview step so you approve results before
anything is committed.

---

## 1. What it does (end to end)

1. **Connect** to a Skynamo instance with an instance name + Skynamo API key.
2. **Fetch** all customers (paginated). **Inactive customers are skipped** —
   only customers with the top-level `active` flag set are processed.
3. **Map** one or more custom fields as the address source (field names differ
   per instance, so the user chooses; selected fields are joined with commas).
4. **Geocode** each address via the chosen provider — Google Maps (API key,
   most accurate) or OpenStreetMap/Nominatim (free, no key, ~1 address/second).
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
    config.py               # constants: URLs, page size, accuracy map, statuses
    client.py               # SkynamoClient: connect, fetch, update_location
    geocoder.py             # Geocoder base + GoogleGeocoder, GeocodeResult/Error
    customers.py            # address-field helpers (build/collect/has_coordinates)
    engine.py               # geocode_customers + write_locations + report (the core)
    settings.py             # config JSON + keyring secrets
  gui.py                    # CustomTkinter desktop app  (entry point for the .exe)
  skynamo_geolocation.py    # CLI front-end (thin wrapper over the engine)
  build.bat                 # PyInstaller -> dist/SkynamoGeo.exe
  requirements.txt          # runtime deps
  requirements-build.txt    # build-only deps (pyinstaller)
  test_engine.py            # engine smoke tests (mocked client + geocoder)
  test_geocoder.py          # provider factory + OSM precision mapping (offline)
  test_gui_smoke.py         # builds the GUI widget tree without interaction
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
Flow in the window: pick a **geocoding provider** (Google Maps or
OpenStreetMap — the Google key field is disabled when OSM is selected) →
**Connect & Load Customers** → tick address field(s) →
**Preview (geocode only)** → review/untick rows → **Write Selected to Skynamo**
→ **Save Report CSV**. A **Cancel** button stops a run; the work happens on a
background thread so the window never freezes.

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

- **Google Maps API key** (only if the Google Maps provider is selected) —
  needs the **Geocoding API** enabled in Google Cloud (Console → APIs &
  Services → Geocoding API). Free $200/month credit (~40k lookups), then
  ~$5 per 1,000.
- **OpenStreetMap (Nominatim)** — no key or account needed. The public service
  is rate-limited to ~1 request/second (the tool throttles itself) and street
  coverage is weaker than Google in some regions.
- **Skynamo API key** — Skynamo Insights → Settings → Integration Tokens →
  *Add access token*.

The GUI can remember these between runs (the "Remember" checkbox):
- **Secrets** (both API keys) are stored in the **Windows Credential Manager**
  via the `keyring` library — never written to disk in plain text.
- **Non-secret settings** (instance name, country, replace flag, selected
  address fields) are saved to `%APPDATA%\SkynamoGeo\config.json`.

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

The full spec is saved in `skynamo_swagger.json` for reference.

---

## 6. Geocoding & accuracy logic

Both providers report how precise each match is, which we translate into the
Skynamo `accuracy` value (metres) so downstream reports can trust precise pins
and treat coarse ones as approximate:

| Google `location_type` | Meaning                         | accuracy (m) | Confidence |
|------------------------|---------------------------------|--------------|------------|
| `ROOFTOP`              | exact street address            | 10           | high       |
| `RANGE_INTERPOLATED`   | interpolated along a road       | 50           | high       |
| `GEOMETRIC_CENTER`     | centre of a street/polyline     | 200          | **low**    |
| `APPROXIMATE`          | town/suburb centroid            | 3000         | **low**    |

| OSM precision (bucketed `addresstype`) | Meaning                | accuracy (m) | Confidence |
|----------------------------------------|------------------------|--------------|------------|
| `OSM_BUILDING`                          | building/house/amenity | 25           | high       |
| `OSM_ROAD`                              | street-level match     | 200          | **low**    |
| `OSM_AREA`                              | suburb/town centroid   | 3000         | **low**    |

- A **partial match** (Google) or any of the **low** precisions above is treated
  as **low confidence**: written with `is_approximate=true`, a coarse accuracy,
  and surfaced in the report/table for manual review.
- An optional **2-letter country code** (e.g. `ZA`) restricts geocoding to that
  country (Google: `components=country:XX` + `region`; OSM: `countrycodes=xx`),
  which removes wrong-continent matches for bare street names.
- See `ACCURACY_BY_PRECISION` and `LOW_CONFIDENCE_PRECISIONS` in
  `skynamo_geo/config.py`. (Standing rule: always send an `accuracy` value;
  derive it from precision when available, otherwise default ≥1000.)

---

## 7. The engine (skynamo_geo/engine.py)

Two phases, so any UI can do **preview-then-commit**:

- `geocode_customers(geocoder, customers, address_fields, replace_existing,
  country, on_progress, should_cancel) -> list[Plan]`
  Decides skip reasons (has-coords / no-address), geocodes the rest, and builds
  `Plan` objects. **Performs no writes.**
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
py test_engine.py        # engine logic: plan statuses, no-write-in-preview, accuracy
py test_gui_smoke.py     # GUI widget tree builds and tears down cleanly
```
`test_engine.py` uses a fake client + fake geocoder (no network, no real API
keys) and asserts that preview writes nothing and only approved rows get PATCHed.

**Not yet automated:** a true end-to-end run against a live Skynamo test
instance. Use the GUI's preview step to eyeball coordinates before writing.

---

## 10. Extending it (where to plug in)

- **Another geocoder** (e.g. Mapbox): subclass `Geocoder` in `geocoder.py`,
  register it in `create_geocoder` + `GEOCODER_PROVIDERS` (config.py), and the
  GUI/CLI pickers offer it automatically — no engine changes.
  `GoogleGeocoder` and `NominatimGeocoder` (OpenStreetMap) exist today.
- **Map preview** of pins before committing: a widget (e.g. `tkintermapview`) or
  Qt/web view consuming the existing `Plan` list.
- **Batch PATCH**: the Skynamo endpoint already accepts an array; optimise inside
  `write_locations` only.
- **Headless/scheduled runs**: call `engine.geocode_customers` +
  `engine.write_locations` directly — the core has no UI dependency.

---

## 11. Change log

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
