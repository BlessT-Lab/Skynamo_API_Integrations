# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Skynamo toolkit. It talks to **two separate Skynamo APIs** — see the table below, this
distinction matters constantly:

1. **Customer geolocation** — fills in customer latitude/longitude by geocoding their address
   fields via OpenStreetMap (Nominatim) — free, no API key. *(Public API)*
2. **Product images** — matches local image files to products by the code in the filename and
   uploads them (`POST /files` → `PATCH /products` with the file GUID), in merge or replace mode;
   and a separate tab to view/remove the images already attached to a product. *(Public API)*
3. **Reporting** — extracts business data (activities, customers, users, products, invoices) into a
   local SQLite store using bookmark deltas. *(Reporting API)*
4. **Dashboards** — renders a self-contained HTML dashboard from that store. *(local only)*
5. **Route planning** — works out which customers each rep is due to visit (from Skynamo's visit
   frequencies and visit history), splits them into day routes for a week, orders each day, and
   exports a map + itinerary, CSV and Google Maps/Waze links. *(Public API, read-only so far —
   writing scheduled visits back is phase 2 and waits on `diag_route_planning.py`)*

Ships as a CustomTkinter desktop GUI (six pages) plus an interactive CLI. The CLI covers
geolocation, image import and image management (`geo`/`images`/`manage`, chosen by a menu or an
argv shortcut); Reporting and Dashboards are GUI-only. See [README.md](README.md) for the full
user-facing walkthrough and API reference.

### The two APIs
| | Public API (`client.py`) | Reporting API (`reporting_client.py`) |
|---|---|---|
| Host | `api.skynamo.me/v1` | `analytics-api.svc.skynamo.me` |
| Auth | `X-API-CLIENT` + `X-API-KEY` headers | OAuth2 client-credentials → JWT Bearer |
| Direction | read **and write** | **read only** (11 GET endpoints) |
| Cost | included | **paid add-on** |
| Rate limits | undocumented | published and tight — see domain rules |

`docs/skynamo-reporting-api-and-powerbi.md` is the authoritative reference for the Reporting API
(the local `skynamo_swagger.json` only covers the Public API).

## Commands

```
py -m pip install -r requirements.txt                       # runtime deps
py gui.py                                                    # run the GUI
py skynamo_geolocation.py                                    # run the CLI (feature menu)
py skynamo_geolocation.py geo|images|manage                   # skip the menu
py test_cli_images.py                                         # CLI image flows (scripted prompts, offline)
py test_engine.py                                            # geolocation engine smoke tests (mocked, no network/keys)
py test_geocoder.py                                          # OSM precision mapping + query building (offline)
py test_products.py                                          # image filename parsing/escaping/matching (offline)
py test_client.py                                             # Public API request bodies (fake session, offline)
py test_image_engine.py                                      # image engine preview/commit (mocked client, offline)
py test_reporting_client.py                                  # OAuth/token/throttle/filter building (fake session)
py test_report_store.py                                      # SQLite schema/upsert/bookmarks (in-memory)
py test_report_engine.py                                     # extract plan/run (fake client, offline)
py test_dashboard.py                                         # HTML dashboard render + escaping (offline)
py test_diag_redaction.py                                    # asserts the auth diagnostic leaks no secrets (offline)
py test_visit_due.py                                         # when a customer is due, calendar maths, time zones (offline)
py test_routing.py                                           # haversine, 2-opt, the day split vs brute force (offline)
py test_route_engine.py                                      # load + plan a synthetic week (fake client, offline)
py test_route_map.py                                         # nav links + the route map HTML (offline)
py test_gui_smoke.py                                         # builds the GUI (all six pages) and tears it down
py diag_route_planning.py                                     # route planning facts from a live instance (read-only, then asks before writing)
py diag_image_upload.py                                       # diagnose a product-image upload failure (3 phases, asks before writing)
py diag_reporting_auth.py                                    # diagnose a Reporting API auth failure (read-only, needs creds)
py live_check_reporting.py                                   # verify entity payloads vs the registry (read-only, needs creds)
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
  `upload_images(move_processed=True)` finishes by calling `file_processed_images(plans)`, which
  moves each image into a `Successful/` or `Failed/` subfolder per `IMAGE_FOLDER_BY_STATUS`;
  `filing_summary(plans)` / `filing_failures(plans)` are what the front-ends report from.
- `skynamo_geo/products.py` — image/product helpers. `escape_code_to_filename`, `parse_image_stem`,
  `sequence_sort_key`, `sniff_image_format`, `build_code_index`, `collect_image_files`,
  `unique_destination`.
- `skynamo_geo/geocoder.py` — `Geocoder` base class; `NominatimGeocoder` (OpenStreetMap: free, no
  key, self-throttled to 1 req/s per usage policy) is the only implementation; front-ends construct
  it directly (`NominatimGeocoder()`). Adding another provider = new `Geocoder` subclass, zero
  engine changes.
- `skynamo_geo/client.py` — `SkynamoClient`. `fetch_all_customers`/`fetch_all_products`
  (`active_only=True` skips inactive). Writes go to the **collection** endpoint with an **array**
  body: `update_location` → `PATCH /customers`, `attach_files` → `PATCH /products` (by **`id`**,
  falling back to `code` only when the product has none);
  `/{id}` routes are GET-only. `upload_file` → `POST /files` with base64 `content`, returns the file GUID.
- `skynamo_geo/config.py` — all constants (endpoints, `ACCURACY_BY_PRECISION`, `STATUS_*`,
  `STATUS_IMG_*`, `STATUS_ATT_*`, `REPORT_FIELDNAMES`, `IMAGE_REPORT_FIELDNAMES`,
  `ATTACHED_IMAGE_REPORT_FIELDNAMES`, `ALLOWED_IMAGE_EXTENSIONS`, `WINDOWS_RESERVED_CHARS`,
  `IMAGE_FOLDER_SUCCESS`/`IMAGE_FOLDER_FAILED`/`IMAGE_FOLDER_BY_STATUS`).
  Changing statuses/columns/accuracy tiers/image rules happens here.
- `skynamo_geo/customers.py` — address helpers. `build_query(customer, field_roles)` returns an
  `AddressQuery` (`.text` single-line + `.structured` dict) from an ordered `(field_name, role)`
  list; `clean_value` drops junk; `has_coordinates` treats zero/`"0"`/null as missing.
- `skynamo_geo/settings.py` — non-secret config in `%APPDATA%/SkynamoGeo/config.json`. API keys and
  Reporting API client credentials are **never persisted** (re-entered each session);
  `purge_saved_credentials()` clears any keys older versions stored in the OS keyring, called on GUI
  startup. Both `_persist_*` helpers read-modify-write so the tabs don't clobber each other's keys.
- `skynamo_geo/reports.py` — shared `summarize(items)` and `write_report(rows, path, fieldnames)`.
  All three engines re-export these; don't re-implement a CSV writer.

### Route planning
- `skynamo_geo/visit_due.py` — pure date logic: `occurrences()` (the visits a (customer, rep) pair
  needs inside a window), `next_due`, `add_months`, `working_days`, and tolerant parsing of the
  instance's `utc_offset` and of API datetimes.
- `skynamo_geo/routing.py` — pure, stdlib-`math` route construction on index matrices:
  `haversine_km`, `matrices`, `nearest_neighbour`, `two_opt`, `split_tour` (the day split),
  `cheapest_insertion`, `assign_to_dates`. Knows nothing about customers or the API.
- `skynamo_geo/route_engine.py` — `load_planning_data(client, ...)` (network, cancellable, **no
  writes**: users, frequencies, customers, configuration, open scheduled visits, visit history) →
  `build_route_plan(data, reps, options)` (**pure — no network at all**). Keeping the planner pure
  is why changing a setting re-plans instantly, and why it is testable with no fake client.
  `resolve_place` turns a typed start point ("lat,lng" or an address) into coordinates.
  `RouteStop`/`RouteDay`/`RoutePlan` are the outputs; `RouteStop.error` is a stop's cause with no
  per-stop specifics, like `ImagePlan.error`.
- `skynamo_geo/route_map.py` — `build_route_html` (one self-contained page, inline SVG, no external
  requests — same rule as the dashboard), `gmaps_links`, `waze_link`, `stop_link`. Must not import
  `route_engine`; the engine imports it.
- `SkynamoClient` gained `(value, error)` reads that never raise — `_fetch_all` (paging, de-dup by
  id, backoff on 429/503 honouring `Retry-After`), `fetch_users`, `fetch_visit_frequencies`,
  `fetch_scheduled_visits(created_since)`, `fetch_interactions_page`, `get_configuration`,
  `get_scheduled_visit` — and the module-level `create_date_filter`.

### Reporting side (the second API)
- `skynamo_geo/reporting_config.py` — **the single declaration of the Reporting API surface**:
  endpoints, the 21 reporting periods, `RATE_LIMIT_BY_PERIOD`, `STATUS_RPT_*`, and
  `REPORTING_ENTITIES` (per entity: endpoint, primary key, columns, sub-entities, `bookmarkable`,
  `has_period`, `order_by`). Adding an entity is a change **here only** — client, store and engine
  all drive off the registry. `/v2/activities` expands **all 11** sub-entities; the four
  sales-document shapes (orders/quotes/credit requests) are generated by `_document_pair` rather
  than repeated. `DOCUMENT_TABLES` maps a human label to its header/line tables for the dashboard.
  A sub-entity may declare `synthetic_key: [cols]` when the API gives it no id — the store hashes
  those columns into the primary key (see below).
- `skynamo_geo/reporting_client.py` — `ReportingClient`, `TokenCache`, `_PeriodThrottle`,
  `build_filter`. Shaped like `SkynamoClient` ((value, error) returns) but OAuth2. Self-throttles
  per reporting period; refreshes the token once on `401`; backs off on `429`/`503` honouring
  `Retry-After`. Credentials never appear in a log or exception.
- `skynamo_geo/report_store.py` — `ReportStore`, SQLite at `%APPDATA%/SkynamoGeo/reporting.db`
  (stdlib `sqlite3`, no new dependency). Schema generated from the registry; `upsert_entity` is
  idempotent, stores nested sub-entity rows, and commits the root **and** its children in one
  transaction; bookmarks keyed `(endpoint, reporting_period)`; `runs` table is the tool's only
  persistent history; `reconcile` soft-deletes via `is_deleted`.
  `upsert_entity` returns **`(written, skipped)`** — `skipped` counts rows discarded for having no
  primary key, which is the signature of the registry naming a key field this instance does not
  send. `run_extract` turns that into a loud WARNING in the notes and CSV; a silent zero is
  indistinguishable from "there was no data", and several sub-entity keys are unverified guesses.
  **Threading:** one instance is usable from several threads (the GUI creates it on one worker and
  reads it from the main thread and others). Writes serialise on a re-entrant lock; **reads open
  their own short-lived connection and take no lock**, so a label refresh on the main thread can
  never freeze the UI behind a bulk upsert. WAL + `busy_timeout` cover concurrency between separate
  connections/processes. The lock guards the instance, not the file. `query()` is read-only by
  contract — it may run on a private connection, so a write through it would not commit.
- `skynamo_geo/report_engine.py` — same two-phase shape: `plan_extract(...)` (reads bookmarks,
  estimates the rate-limit budget, **no network calls**) → `run_extract(...)` (fetch → upsert →
  record run → **then** store the bookmark).
- `skynamo_geo/dashboard.py` — `build_dashboard(store, out_path, ...)`: one self-contained HTML file
  with hand-built inline SVG charts. No matplotlib (it would add ~40MB to the one-file exe) and no
  external requests. Everything from the instance goes through `html.escape`.

### GUI threading model (gui.py)
The GUI is a **`SidebarNav`** (a left navigation rail plus a content area) with five pages, one
`_build_*_tab` method each: **Customer Geolocation**, **Product Images**, **Manage Images**,
**Reporting**, **Dashboards**. `SidebarNav` deliberately exposes the slice of `CTkTabview`'s API the
app used — `add()`, `tab()`, `set()`, `get()` — so swapping the navigation was a change in
`_build_ui` and nowhere else; `self.tabview` is still the attribute name, and `test_gui_smoke`
needed no change. Tkinter is not thread-safe.
Each tab runs its engine on its own `threading.Thread`, pushes events onto its own `queue.Queue`,
and the main thread drains it via `self.after(100, ...)` — the **only** place widgets are touched.
Cancel is a per-tab `threading.Event` passed in as `should_cancel`. Tabs keep fully separate
state/queues/workers, distinguished by attribute prefix:

| Tab | Prefix | Shares |
|---|---|---|
| Customer Geolocation | *(plain names)* | own `SkynamoClient` |
| Route Planning | `route_*` / `_route_*` | own `SkynamoClient` + `PlanningData` |
| Product Images | `img_*` / `_img_*` | `img_client` + `products` |
| Manage Images | `mgmt_*` / `_mgmt_*` | reuses Product Images' client/products |
| Reporting | `rpt_*` / `_rpt_*` | own `ReportingClient` + `ReportStore` |
| Dashboards | `dash_*` / `_dash_*` | reads the store (no client, no thread needed) |

Any new long-running work must follow this pattern — never update a widget from a worker thread.

**The log is a `LogPanel`, collapsed by default.** It delegates `insert()`/`see()` to an inner
textbox, so the ~25 `X_log_line` callers were untouched, and it adds `expand()`/`alert()`. It stays
shut until it matters: every tab's `error` branch and the image tab's grouped failure summary open
it, because the reason an operation failed lives there and a reason nobody sees is no reason at all.
Any new failure path must open it too. **Button weight is derived, not specified**: `_button` reads
the `fg_color` the caller already passes (`FIELD` → outlined secondary, `RED` → outlined with red
text, anything else → filled primary), so hierarchy lives in one place instead of 32 call sites. A
screen should have exactly one filled button. Colour alone cannot carry every intent, so `kind`
overrides the inference — a **destructive primary** (Manage Images' Remove) is red *and* the point
of its screen, and inferring from `RED` alone rendered it as a Cancel and left that screen with no
primary at all. Every branch must set `text_color_disabled`: CustomTkinter does not change a
button's fill when disabled, so the default grey on a saturated fill is all but invisible.

**A label's wraplength must be measured from the window, never from its own container**
(`_wrap_to_width`). A card is sized by its children, so a long label inflates the card and reading
that card back returns a width the label itself caused — the card grew to 1128px inside an 894px
content area and the text was clipped by the window edge instead of wrapping. Convert for DPI too:
`winfo_width()` is physical pixels while a CTk widget's `wraplength` is logical, a 25% error on a
1.25-scaled display.
Note the Dashboards tab and the Reporting tab's store label deliberately **do not create** the
SQLite file just to render; they check `os.path.exists` first, so a user who never opens Reporting
never gets a stray database.

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
- **A failure the user cannot read is a failure they cannot report.** The image tab pushed every
  per-item outcome into a status label that the next progress event overwrote, and the results
  table has no notes column, so the *reason* an image failed reached only the saved CSV — half of
  this feature's original requirement ("a log on display **and** a file to show reason why the
  image failed to import") was simply absent, and a 1003-image failure was undiagnosable from the
  screen. Reasons now go to the log grouped by cause, and the dominant cause goes to the status
  line, which does not scroll. Grouping is on **`plan.error`**, not `plan.notes`: notes carry
  per-image specifics (`"Matches product 'Alpha'; ..."`, `"No product with code matching 'NOPE'"`)
  so grouping on them collapses nothing. Any new failure path must set `plan.error` to a cause with
  no filename, code or product name in it. An API body is not such a cause on its own: it repeats
  one message per offending item and quotes each item's id, which is why a live run reported 51
  failures as 17 problems. Pass it through `image_engine.stable_cause` first - it extracts
  Skynamo's coded messages (`F002`, `P023`), de-duplicates them and blanks quoted values.
- **`P023: File guid ... not found` is two different bugs.** Either the upload never stored the file
  (`POST /files` answers 200 with a guid even so) or `PATCH /products` will not accept a guid that
  does exist. Opposite fixes, so `_attach_failure_cause` spends **one** `GET /files/{guid}` per
  failed product to say which, and folds the answer into the cause. It only ever runs after a
  failure, so a healthy run costs nothing.
- **`POST /files` requires `content_hash`, and the spec does not say so.**
  `FilePost` lists no required fields, so we sent only `filename` + `content` and a live
  instance rejected **every** upload with
  `HTTP 400 {"errors":["F002: Content hash is required."]}`. The description is just
  "The content hash of the file (base64string)" — it never names the algorithm, so
  `FILE_HASH_ALGORITHM` in `config.py` is the single place it is decided (base64 of the digest of
  the file's **raw bytes**, not of the base64 text) and `diag_image_upload.py` probes the
  alternatives against a live endpoint. `client.content_hash_b64` computes it;
  `upload_file` derives it when the caller does not supply one, and a content string that will not
  base64-decode is **returned** as an error, never raised — the engine relies on that to fail one
  image rather than the whole run.
- **Patch a product by `id`, not by `code`.** `ProductPatch` declares `id` required (its `code`
  field only says "required if you do not specify id"), and a live instance rejected a code-only
  patch: every image uploaded and then failed to attach, reported as *uploaded but not attached*.
  `attach_files` keys on `id` and falls back to `code` only for a product that arrived without one
  — the same shape as `update_location`, which is the write path that was already known to work.
  This survived every offline test because the engine tests' fake client accepts whatever it is
  handed; `test_client.py` exists to pin the actual request bodies, and the fakes now take
  `require_product_id=True` to reproduce the live rejection.
- **An uploaded file is linked to its product by `transaction_id`, not by listing its guid.**
  `ProductPatch.transaction_id` is documented as "The transaction id associated with files in order
  to link files to a product", and `FilePost` takes one too; the same field is on `CustomerPatch`,
  `OrderPost`, `QuotePost` and `CreditRequestPost`, so it is *the* mechanism for attaching an
  uploaded file to anything. Until the link is made the file's guid does not resolve, so
  `GET /files/{guid}` 404s **and** a patch listing it answers `P023: File guid ... not found` — one
  symptom, and it looks exactly like a file that was never stored. `upload_images` mints **one
  transaction per product** (`new_transaction_id`) and sends it on every upload in that product's
  group and on the patch that attaches them. Per product, not per run: the patch links "the files
  of this transaction", so one id shared across products could attach every product's images to
  whichever is patched first.
- **An uploaded file expires if nothing links it — about 15 minutes.** `POST /files` returns an
  `expire_time` roughly a quarter of an hour ahead (`File.expire_time` exists in the spec but
  nothing says it is short, or that it applies to a file awaiting a transaction link). Two
  consequences. It is why a failed run leaves **no** orphan build-up: unattached files clean
  themselves up, so retrying is cheap and there is nothing to go and delete — which is just as well,
  since there is no `DELETE /files/{guid}`. And it puts a clock on the gap between a product's
  uploads and its `PATCH`: `upload_images` uploads one product's images and patches immediately, so
  the exposure is one product's worth of uploads, but a product with very many images on a slow link
  could in principle have its earliest uploads expire before the patch lands. If that ever shows up,
  it appears as `P023` on the *earliest* images of a large product only.
  **Verified live** (2026-09-14, instance `trainingblessings`): upload 200 + `GET /files/{guid}` 200
  + `PATCH /products` 200 with base64-MD5 `content_hash`, `id`, and a shared `transaction_id`.
- **Skynamo has no product-image endpoint.** Upload is generic: `POST /files` (base64 `content`) →
  file GUID → `PATCH /products` with `{code, files:[...]}`. In merge mode the `files` array is sent
  as the **union** of the product's existing GUIDs plus the new ones (don't clobber attached images);
  in **replace mode** (`upload_images(replace_existing=True)`) it's set to only this run's GUIDs.
  Format is gated on extension **and** magic bytes (`sniff_image_format`); PNG/JPEG only.
- **Filing processed images is a forward-only sweep, and deliberately partial.**
  `IMAGE_FOLDER_BY_STATUS` is the whole policy: every terminal outcome maps to `Successful/` or
  `Failed/`, and `STATUS_IMG_PENDING` is **absent on purpose** — an image that matched but was
  deselected (or that a cancelled run never reached) has not been processed, so it stays put and
  the folder keeps showing what is outstanding. Three rules the tests pin: a name already taken by
  an earlier run gets a ` (2)` suffix rather than overwriting it (`products.unique_destination`); a
  **cancelled run files nothing**, because a half-swept folder is harder to reason about than an
  untouched one; and a move that fails is a note on the plan, never an exception — the upload has
  already happened and losing the report over one locked file would be worse. This is also why
  `collect_image_files` must stay **non-recursive and files-only**: that is the only thing stopping
  a re-run from re-ingesting `Successful/` and `Failed/`.
- **Files and products have no DELETE endpoint** — there is no `DELETE /files/{guid}` and no
  `DELETE /products/{id}`. (The Public API *does* have DELETEs for `/invoices`,
  `/invoicesbyexternalid`, `/tasks`, `/scheduledvisits`, `/orderstatuses` and `/dealgroups/{id}` —
  just not for the resources this tool writes.) So "removing" an image
  (`image_engine.delete_selected_images`) means re-`PATCH /products` with the product's `files` list
  minus the removed GUIDs — it detaches the file from the product; the underlying file object may
  still exist server-side. `GET /files/{guid}` (`client.get_file`) resolves a GUID to a filename for
  display, since a product's `files` array is only ever bare GUID strings.

### Route planning rules
- **"f visits every c periods" is a count-in-a-window rule, not an interval.** With W = c periods,
  a customer is due on day d when fewer than f visits fall in (d − W, d]; with visits newest first
  that is `next_due = v_f + W`. "Twice a week" is therefore *not* "every 3.5 days" — that books a
  customer seen Monday and Tuesday last week. Months and years are calendar months (`add_months`
  clamps 31 Jan → 28/29 Feb). The window is **rolling**, not calendar-bucketed — a decided
  assumption; if Skynamo's own compliance reports bucket by calendar month, `visit_due` is the one
  place to change.
- **A repeat visit cannot go through the tour.** A "twice a week" customer contributes two stops at
  the *same coordinates*, so a tour puts them side by side and the split books both visits the same
  day. First visits go tour → split; later ones are inserted afterwards into a day at least
  W/(2f) days from the customer's other visits.
- **Split a tour; don't sweep.** One tour per rep (nearest neighbour + 2-opt), then `split_tour`
  cuts it into days by dynamic programming. A polar sweep around the start only works when the
  start is in the middle of the territory; a rep starting from home off to one side gets long thin
  wedges and drives out and back every day. The split also enforces hour and stop limits exactly,
  and a quadratic balance term stops a 9-hour day sitting next to a 2-hour one.
  `test_routing` checks the split against brute force and 2-opt across 200 seeded cases — there is
  no live oracle for "the best route", so the properties are the test.
- **When the week is full, drop the least urgent — and prove it.** Priority is days overdue; every
  first visit left unplanned has priority ≤ every first visit planned (asserted).
- **Distances are straight-line × road factor ÷ speed.** Free, offline, and close enough to get the
  *order* right; drive times are labelled as estimates everywhere they appear.
- **`GET /interactions` takes no filter at all**, so last-visit-per-customer means walking the whole
  history. Page 1 reports `total_item_count`, so the cost is shown before the walk; a same-day
  re-run with an unchanged total uses the cache under `%APPDATA%/SkynamoGeo/route_history/`; there
  is a hard request cap. Whether the walk can go backwards and stop early depends on id ordering,
  which `diag_route_planning.py` measures — not assumed.
- **Existing scheduled visits are only seen within a look-back.** `GET /scheduledvisits` filters on
  `create_date`, never on due date, rep or customer, so a visit created more than
  `ROUTE_SCHEDULED_LOOKBACK_DAYS` ago for a date inside the plan is invisible to the duplicate
  check. Load keeps every open visit; the plan window is applied at build time, so changing the
  week after connecting needs no refetch.
- **An approximate pin navigates by address.** `is_approximate` or accuracy > 500 m flags the stop,
  draws a dashed ring, and its Google Maps / Waze links use the address text (rebuilt from the
  Geolocation tab's saved field roles) — coordinates would drive the rep to a suburb centroid.
- **A rep's start address is personal data** — often their home. It is stored only in local
  `config.json` (`route_reps`), sent only to OpenStreetMap to look it up, and never printed: maps
  label it "Start".
- **Phase 2 (writing scheduled visits) is not built yet, deliberately.** The datetime format the
  API wants, whether a batch POST is all-or-nothing, whether the response echoes `comment`, and
  whether reps get a notification per visit are all undocumented. `diag_route_planning.py` measures
  them against a live instance; record its findings here before trusting the write path, as with
  `content_hash`.

### Reporting API rules (all learned the hard way from the spec)
- **Rate limits are the dominant design constraint**, and they scale *inversely* with how much data
  the period covers: `30 q/30s` (day/week/30-day), `4 q/min` (90-day/quarter), `4 q/10min`
  (180/365-day, financial), **`2 q/10min` for `AllData`**. Hence: `_PeriodThrottle`, the planner's
  budget warning, and the rule below.
- **Prefer one `entities`-expanded call over many paged calls.** Paging a year-wide query in 500-row
  pages can take an hour against a 4-per-10-minutes budget. `build_filter` expands all sub-entities
  by default for exactly this reason — and because they ride along in the same request, expanding
  more of them costs **no extra calls**, only a larger response.
- **"Activity" is a container, not a type.** What happened is in `activities.activity_type` (values
  are instance-specific, so never hard-code a list — the dashboard groups by whatever it finds) and
  in which sub-entity tables the activity produced rows in (`DOCUMENT_TABLES`). `order_totals.quote_id`
  is what makes quote→order conversion measurable.
- **Comments and emails have no id in the API.** Keying them on `activity_id` would make each
  overwrite the last, so their registry entries declare `synthetic_key` and `report_store.synthetic_key`
  hashes the identifying columns. Deterministic, so re-extracting the same content does not duplicate.
  Use the same mechanism for any future keyless sub-entity. It treats absent and null identically —
  the API is inconsistent about which it sends, and hashing `"None"` would duplicate the row.
- **A guessed primary key fails silently unless you look.** If a sub-entity's declared key is not a
  field the instance returns, every row of it is discarded. That is why the store counts skipped
  rows, `run_extract` warns, and `live_check_reporting.py` diffs each **sub-entity**'s columns and
  key (not just the roots) and names candidate `*_id` fields when the key is missing.
- **One expanded call is large.** `/v2/activities` with all 11 sub-entities and no paging returns
  the whole graph in one body, so it uses `REPORTING_TIMEOUT` (much longer than the Public API's
  `REQUEST_TIMEOUT`) and retries a network failure only `REPORTING_NETWORK_RETRIES` times — each
  retry re-enters the throttle, so on `AllData` (2 per 10 min) unbounded retries would spend the
  entire allowance on one failed call.
- **Dashboard figures are scoped to the store, not the instance.** Quote→order conversion counts
  only orders whose quote is also in the extract; otherwise a delta or short period yields >100%.
- **`order` is mandatory whenever `skip`/`limit` are used** — the spec says so twice. `build_filter`
  adds it automatically, and omits paging entirely for entities with no documented sortable field.
- **A bookmark is scoped to `(endpoint, reportingPeriod)`.** Reusing one across periods returns
  meaningless results, so the store is keyed on both. `/v2/products` has no period (unqualified
  bookmark); `/v2/users` has no bookmark at all (full reload).
- **Store the bookmark only after the rows commit** — otherwise an interrupted run silently skips
  data forever. `run_extract` does upsert → record run → set bookmark, in that order.
- **Bookmarks never report deletions** ("new data that was added"), which is why the store has
  `is_deleted` + `reconcile()` rather than trusting deltas.
- **Log the `x-date-range` response header.** It is the only proof of which window the server
  actually computed, and financial-period arithmetic (year start, month start day, week start) has
  real off-by-one risk. The Reporting tab prints it per entity after every run.
- **The registry is now verified against a live instance** (2026-09-14, `trainingblessings`,
  `Prev30Days`). The headline: all six sub-entity primary keys that were unverified guesses
  (`survey_item_id`, `completed_form_id`, `order_id`, `order_item_id`, `credit_request_id`,
  `credit_request_item_id`) are **present**, so the silent-discard failure mode is not happening.
  Four sub-entities (`comments`, `emails`, `quoteTotals`, `quotes`) had no rows in the sample and
  remain unverified. What the check did find: a dozen declared columns the API never sends (removed),
  several it does send that were being thrown away (added — every document row repeats its
  `customer_id`, `user_id` and `date`), and `/v2/invoices` failing outright.
- **A document row does not name the quote it came from.** `quote_id` is declared on order totals
  but a live instance does not send it, so quote→order conversion is **not computable**.
  `DOCUMENT_TOTALS_HAVE_QUOTE_ID` gates it: the column is kept (it is the only field that could ever
  carry this, and deleting it would delete the code too) and the dashboard says the conversion is
  unavailable rather than printing a confident `0%` — which reads as "nothing converts", a very
  different and more alarming claim than "we cannot see this".
- **Sending `limit`/`skip` without an `order` is a server-side SQL error, not a soft failure.**
  `/v2/invoices` declares no sortable field, `build_filter` sent `limit` anyway, and the instance
  answered `HTTP 424 "Incorrect syntax near 'OFFSET'. Invalid usage of the option NEXT in the FETCH
  statement."` — the entity could never be extracted. `build_filter` now omits paging entirely when
  no `order_by` is declared, which is what this file already claimed it did. `test_reporting_client`
  had asserted the broken behaviour while its own comment described the correct one.
- **Two response schemas in the spec are wrong** (`/v2/products` declares `CustomerExtended`,
  `/v2/yearonyearsales` declares `UserTimeSegment`) and 7 of 11 endpoints have empty descriptions.
  The registry's column lists are therefore **unverified against a live instance** — confirm before
  trusting them. `_rows_from` tolerates several envelope shapes for the same reason.
- **camelCase vs snake_case**: `VisitExtended`/`RfmVisit` use camelCase, everything else snake_case.
  `report_store.normalise_key` is the single funnel; never index a payload key directly.
- **The token lifetime is undocumented** — trust `expires_in`, refresh on `401`, never hard-code.
- **A token is not proof of access.** A wrong `audience` or a credential without the paid add-on
  still gets a token; every data call then `401`s. `test_connection` therefore treats a `401`/`403`
  from its probe as a real failure, and anything else as a caveat (`/v2/roles` is one of the
  undocumented endpoints). `_get` returns the status code so callers can tell those apart.
- **Never splice a token-endpoint body into an error.** A gateway can reject by echoing the request
  back, and the request body holds the client id and secret. `_oauth_error_detail` reads *only* the
  standard `error`/`error_description` fields — not the raw text, not a generic `message`.
- **`diag_reporting_auth.py` output is shareable only because it redacts** — bearer tokens, the
  id/secret (shown as length + digest), the JWT `sub`/`azp` (which carry the Client ID), and all row
  values. `test_diag_redaction.py` enforces that with canary values; keep it passing if you touch
  the script.

## Working agreements

- Before any GitHub-facing action (commit, push, PR, remote `git`/`gh`/API calls), show the exact
  planned steps/commands and wait for explicit approval. Local read-only inspection
  (`git status`/`log`/`diff`) is fine without asking.
- `gh` is not installed here; PRs are created via the GitHub API using the stored git credential.
- Keep the change log in [README.md](README.md) updated when behaviour changes.
