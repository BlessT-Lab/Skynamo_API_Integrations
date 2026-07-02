# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A tool that fills in customer latitude/longitude on a Skynamo instance by geocoding their address
fields via Google Maps or OpenStreetMap (user-selectable). Ships as both a CustomTkinter desktop
GUI and an interactive CLI. See [README.md](README.md) for the full user-facing walkthrough and
Skynamo API reference.

## Commands

```
py -m pip install -r requirements.txt                       # runtime deps
py gui.py                                                    # run the GUI
py skynamo_geolocation.py                                    # run the CLI
py test_engine.py                                            # engine smoke tests (mocked, no network/keys)
py test_geocoder.py                                          # provider factory + OSM precision mapping (offline)
py test_gui_smoke.py                                         # builds the GUI widget tree and tears it down
py -m pip install -r requirements-build.txt && build.bat     # -> dist/SkynamoGeo.exe (PyInstaller)
```

Tests are plain assert-based scripts run directly with `py`, not pytest — there is no single-test
runner; edit the script or add a new `test_*.py`. This is a Windows environment (`py` launcher,
`build.bat`); the Bash tool is also available for POSIX-style commands.

## Architecture

The central principle: **`skynamo_geo/` is UI-agnostic core; `gui.py` and `skynamo_geolocation.py`
are thin front-ends that call the same engine.** Keep business logic out of the front-ends so
behaviour stays identical across GUI and CLI — this is the main invariant to preserve.

- `skynamo_geo/engine.py` is the heart. It's a **two-phase preview-then-commit** design:
  `geocode_customers(...)` builds `Plan` objects and performs **no writes**; `write_locations(...)`
  PATCHes only plans where `include` is true and the plan is `writable`. Both take an
  `on_progress(event)` callback and a `should_cancel()` predicate — that's how the GUI streams
  progress and cancels without the core knowing anything about threads or widgets.
- `skynamo_geo/geocoder.py` — `Geocoder` base class; `GoogleGeocoder` and `NominatimGeocoder`
  (OpenStreetMap: free, no key, self-throttled to 1 req/s per usage policy) implement it.
  Front-ends construct one via `create_geocoder(provider, api_key)`; adding a provider = new
  subclass + register in `create_geocoder` and `GEOCODER_PROVIDERS` (config.py), zero engine changes.
- `skynamo_geo/client.py` — `SkynamoClient`. `fetch_all_customers(active_only=True)` skips inactive
  customers by default. `update_location` must PATCH the **collection** endpoint `/customers` with an
  **array** body — `/customers/{id}` is GET-only.
- `skynamo_geo/config.py` — all constants (endpoints, `ACCURACY_BY_PRECISION`, `STATUS_*`,
  `REPORT_FIELDNAMES`). Changing statuses/columns/accuracy tiers happens here.
- `skynamo_geo/customers.py` — address-field helpers; `has_coordinates` treats zero/`"0"`/null as missing.
- `skynamo_geo/settings.py` — non-secret config in `%APPDATA%/SkynamoGeo/config.json`; API keys in the
  OS keyring (never on disk).

### GUI threading model (gui.py)
Tkinter is not thread-safe. The engine runs on a `threading.Thread`; it pushes events onto a
`queue.Queue`; the main thread drains the queue via `self.after(100, self._poll_queue)` and is the
**only** place widgets are touched. Cancel is a `threading.Event` passed in as `should_cancel`. Any
new long-running work must follow this pattern — never update a widget from a worker thread.

## Domain rules that aren't obvious from the code

- **Accuracy is precision-derived, not fixed.** Google's `location_type` maps to a Skynamo
  `accuracy` (metres) via `ACCURACY_BY_PRECISION`; `APPROXIMATE`/`GEOMETRIC_CENTER`/partial matches
  are low-confidence, written with `is_approximate=true` and flagged for manual review. Always send
  an `accuracy` value.
- **The Skynamo `PATCH /customers` "Missing Authentication Token" error means wrong route, not auth**
  (AWS API Gateway quirk).
- Address field names vary per instance and live only in each customer's `custom_fields` array, which
  is why field mapping is interactive.

## Working agreements

- Before any GitHub-facing action (commit, push, PR, remote `git`/`gh`/API calls), show the exact
  planned steps/commands and wait for explicit approval. Local read-only inspection
  (`git status`/`log`/`diff`) is fine without asking.
- `gh` is not installed here; PRs are created via the GitHub API using the stored git credential.
- Keep the change log in [README.md](README.md) updated when behaviour changes.
