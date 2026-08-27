# Pulling Skynamo data for BI with the Public API

How to build a reporting dataset out of the **Skynamo Public API** (`api.skynamo.me/v1`,
`v1.0.28`) — what is extractable, what is not, how to do it incrementally, and how to model the
result.

**Companion documents**
- [Skynamo Public API — Complete Reference](skynamo-public-api-guide.md) — endpoints, auth, filters,
  full field reference.
- [Skynamo Reporting API and Power BI](skynamo-reporting-api-and-powerbi.md) — the **paid,
  purpose-built analytics API**. Read the next section before committing to the approach here.

Source tags used throughout: **[spec]** from the Swagger document · **[kb]** from
`support.skynamo.com` · **[verified]** proven by code in this repo · **[inferred]** reasoned, not
documented.

---

## Contents

1. [Read this first: you may want the other API](#1-read-this-first-you-may-want-the-other-api)
2. [TL;DR — five constraints that decide your design](#2-tldr--five-constraints-that-decide-your-design)
3. [Why the Public API is not a database](#3-why-the-public-api-is-not-a-database)
4. [The extraction-capability matrix](#4-the-extraction-capability-matrix)
5. [Three extraction tiers](#5-three-extraction-tiers)
6. [The `row_version` watermark pattern](#6-the-row_version-watermark-pattern)
7. [Paging safely](#7-paging-safely)
8. [`flags=show_nulls` is mandatory](#8-flagsshow_nulls-is-mandatory)
9. [Extraction in Python](#9-extraction-in-python)
10. [Extraction in Power BI / Power Query](#10-extraction-in-power-bi--power-query)
11. [Dimensional model](#11-dimensional-model)
12. [Money and tax](#12-money-and-tax)
13. [Custom fields to columns](#13-custom-fields-to-columns)
14. [Metrics you can build](#14-metrics-you-can-build)
15. [Metrics you cannot build](#15-metrics-you-cannot-build)
16. [Deleted-record blindness](#16-deleted-record-blindness)
17. [Throughput and politeness](#17-throughput-and-politeness)
18. [Operational checklist](#18-operational-checklist)
19. [Appendix — per-endpoint extraction cookbook](#appendix--per-endpoint-extraction-cookbook)

---

## 1. Read this first: you may want the other API

Skynamo has a **second, separate API built specifically for reporting** — the Analytics /
Reporting API. If your goal is a BI model, it is very likely the better starting point, and the
gap is not marginal.

| Capability | Public API (this document) | Reporting API |
|---|---|---|
| Cost | Included | **Paid add-on** |
| Server-side filtering | 14 of 30 endpoints, 6 operators | Every endpoint, `EQ LT LE GT GE NE IN` |
| Sorting | **None** | `order` on selected fields |
| Sparse fieldsets | **None** | `fields` — include/exclude per field |
| Sub-entity expansion | **None** | `entities` — one call returns a whole graph |
| Incremental delta | `row_version` filter on 7 endpoints | `bookmark` param + `x-bookmark` header, **every endpoint** |
| Reporting periods | Build it yourself | 21 built-in periods, financial-year aware |
| **Sales line-item keys** | **Absent** — lines have no id | `order_item_id`, `quote_item_id`, `sale_item_id`, … |
| **Sales targets** | **Not exposed** | `CustomerTarget`, `UserTarget`, `AssignedUserTarget` + actuals |
| **Travel claims / time & motion** | **Not exposed** | `TravelClaim`, `UserTimeSegment` |
| **Pre-aggregated document totals** | Compute yourself | `OrderTotal`, `QuoteTotal`, `CreditRequestTotal` |
| Visit duration / on-site flag | Derive from timestamps + `is_approximate` | `duration_sec`, `is_onsite`, `is_scheduled` precomputed |
| Power BI | Hand-built M queries | Official custom connector |
| Documented rate limits | **None** | Yes, explicit per-period quotas |
| Writes | Yes | No |

**Choose the Public API for BI when** you do not have the paid add-on; you need entities the
Reporting API does not carry (stock levels, price lists, deal groups, form definitions, contacts,
instance configuration); you need to *write* as well as read; or you want no dependency on a
connector that Skynamo's own documentation calls *"still in development"* **[kb]**.

**A hybrid is often best:** Reporting API for the sales-and-activity star schema, Public API for the
reference dimensions it does not expose.

Everything below assumes you have made that decision and are proceeding with the Public API.

---

## 2. TL;DR — five constraints that decide your design

1. **Sixteen of thirty list endpoints cannot be filtered at all — including every high-volume
   transactional entity.** Orders, invoices, quotes, credit requests, interactions and completed
   forms must be read in full, every run. **[spec]**
2. **There is no sorting anywhere**, so offset pagination walks an undefined order. Long walks over
   changing data can skip or duplicate rows. **[spec]**
3. **`row_version` is your only reliable cursor**, and only seven endpoints expose it as a filter.
   **[spec]**
4. **Nulls are omitted unless you ask for them.** Always send `flags=show_nulls` or your column set
   shifts row to row. **[spec]**
5. **Nothing reports deletions.** No tombstones, no soft-delete markers, no audit of removal. Only a
   periodic full-key reconciliation will find them. **[spec]**

Design implication: **extract → land raw → transform downstream.** Do not attempt to query the API
to answer questions. Copy it, then query your copy.

---

## 3. Why the Public API is not a database

| You might want | The API offers |
|---|---|
| `WHERE date BETWEEN … AND …` | Nothing on orders/invoices/quotes/interactions. Full scan. |
| `ORDER BY` | Nothing, anywhere. **[spec]** |
| `SUM`, `COUNT`, `GROUP BY` | Nothing. All aggregation is client-side. |
| `SELECT id, code` | Nothing. Always the full entity. |
| `JOIN` / `?expand=` | Nothing. File GUIDs resolve one request at a time. |
| Keyset / cursor pagination | Offset only (`page_number`, `page_size`). |
| A change feed | Polling only. |

So the architecture is fixed:

```
Skynamo Public API
      │   paged JSON, flags=show_nulls
      ▼
RAW LANDING  ── one file/table per endpoint per run, unmodified, with an extract timestamp
      │
      ▼
STAGING      ── typed, deduplicated, custom fields pivoted, surrogate keys assigned
      │
      ▼
MODEL        ── conformed dimensions + fact tables (star schema)
      │
      ▼
Power BI / Tableau / SQL
```

Land the raw payload **before** transforming it. Three reasons specific to this API: nulls-omitted
responses mean schema drift you will want to diagnose after the fact; there is no way to re-query
history, so an unretained extract is gone; and line items have no keys, so if you ever need to change
your surrogate-key scheme you must re-derive it from the original payloads.

---

## 4. The extraction-capability matrix

Every collection endpoint, what you can filter on, and which delta fields the returned entity
carries. All **[spec]**, generated from `v1.0.28`.

| Endpoint | Server-side filter | `row_version` | `last_modified_time` | `create_date` | `active` | Strategy |
|---|---|---|---|---|---|---|
| `/customers` | `id`, `code`, `active`, `create_date`, `row_version` | ✅ | ✅ | ✅ | ✅ | **T1** watermark |
| `/products` | `id`, `row_version`, `customer_id`, `customer_code`, `user_id` | ✅ | ✅ | — | ✅ | **T1** watermark |
| `/contacts` | `id`, `active`, `create_date`, `row_version`, `customer_id?`, `customer_code?` | ✅ | ✅ | ✅ | ✅ | **T1** watermark |
| `/customercomments` | `id`, `row_version`, `customer_id?`, `customer_code?` | ✅ | ✅ | — | — | **T1** watermark |
| `/scheduledvisits` | `id`, `create_date`, `row_version` | ✅ | ✅ | ✅ | — | **T1** watermark |
| `/tasks` | `id`, `create_date`, `row_version` | ✅ | ✅ | ✅ | — | **T1** watermark |
| `/visitfrequencies` | `id`, `create_date`, `row_version` | ✅ | ✅ | — | — | **T1** watermark |
| `/orderstatuses` | `id`, `document_id`, `date` | — | — | — | — | **T1** date window |
| `/dealgroups` | `id`, `name`, `last_modified_time`, `currency_code`, `customers_*`, `deals_*` | — | ✅ | — | — | **T1** `last_modified_time` |
| `/stocklevels` | `warehouse_id` (`equals` only) | — | ✅ | — | — | **T2** snapshot |
| `/customerdealgroupallocations` | `id`, `code`, `name`, `active`, `create_date`, `version`, `deal_groups_*` | — | — | — | ✅ | **T3** full |
| `/dealgroupcustomerallocations` | `deal_group_id`, `deal_group_name`, `customers_*`, `deals_*` | — | — | — | — | **T3** full |
| `/formdefinitions` | `id`, `active`, `type` | — | ✅ | — | ✅ | **T3** full |
| `/logentries` | `time` (**required**, `>=` only) | — | — | — | — | **Cursor feed** |
| `/orders` | ❌ **none** | — | ✅ | — | — | **T2** full scan |
| `/invoices` | ❌ **none** | ✅ | ✅ | — | — | **T2** full scan |
| `/quotes` | ❌ **none** | — | ✅ | — | — | **T2** full scan |
| `/creditrequests` | ❌ **none** | — | ✅ | — | — | **T2** full scan |
| `/interactions` | ❌ **none** | — | ✅ | — | — | **T2** full scan |
| `/completedforms` | ❌ **none** | — | ✅ | — | — | **T2** full scan |
| `/emailinteractions` | ❌ **none** | — | ✅ | — | — | **T2** full scan |
| `/orderitemstatuses` | ❌ **none** | — | ✅ | — | — | **T2** full scan |
| `/prices` | ❌ **none** | — | ✅ | — | — | **T2** snapshot |
| `/pricelists` | ❌ **none** | — | ✅ | — | ✅ | **T3** full |
| `/taxrates` | ❌ **none** | — | ✅ | — | ✅ | **T3** full |
| `/warehouses` | ❌ **none** | — | ✅ | — | ✅ | **T3** full |
| `/users` | ❌ **none** | — | — | — | ✅ | **T3** full |
| `/currencies` | ❌ **none** (also **no `flags`**) | — | — | — | — | **T3** full |
| `/configurations` | singleton, no paging | — | — | — | — | **T3** full |
| `/integrationformvalues` | singleton, no paging | — | ✅ | — | — | **T3** full |

> **Note the cruel detail on `/invoices`:** it *has* a `row_version` field in the payload but **no
> filter to query it with**. Same for `last_modified_time` on almost everything. The delta data is
> right there and unusable server-side. **[spec]**

> **Ignore the ten `Filters*` parameters that Swagger UI shows but no operation references** —
> `Filters`, `FiltersById`, `FiltersByCode`, `FiltersByActive`, `FiltersByCreateDate`,
> `FiltersByVersion`, `FiltersByUserId`, `FiltersByType`, `FiltersByCustomerId`,
> `FiltersByCustomerCode`. They describe a `filters.id=` dotted style that is not wired up. **[spec]**

---

## 5. Three extraction tiers

### Tier 1 — server-side delta (cheap, run often)

Seven endpoints accept a `row_version` filter. Ask only for what changed:

```
GET /v1/customers?flags=show_nulls&page_size=200&filters=["greater_than(row_version,884213)"]
```

`/customers` · `/products` · `/contacts` · `/customercomments` · `/scheduledvisits` · `/tasks` ·
`/visitfrequencies`

Two more get a Tier-1-style bound without `row_version`:

- **`/orderstatuses`** — filter on `date`, e.g. `["greater_than_equals(date, 2026-07-01)"]`.
- **`/dealgroups`** — the only endpoint anywhere with a `last_modified_time` filter.

Run these as often as you like — hourly is entirely reasonable. Cost is proportional to *change*,
not to table size.

### Tier 2 — full scan with client-side delta (expensive, run nightly)

No filtering exists. You must page the entire collection and compare `last_modified_time` yourself:

`/orders` · `/invoices` · `/quotes` · `/creditrequests` · `/interactions` · `/completedforms` ·
`/emailinteractions` · `/orderitemstatuses` · `/prices` · `/stocklevels`

**Budget it honestly.** At `page_size=200`:

| Rows | Requests | At ~1 s/request |
|---|---|---|
| 10 000 | 50 | ~1 min |
| 100 000 | 500 | ~8 min |
| 1 000 000 | 5 000 | ~1 h 25 min |

Multiply by the number of Tier-2 endpoints. For a mature instance this is an overnight job, and it
grows without bound because **order history never shrinks** — orders are immutable and cannot be
deleted **[spec]**.

Ways to make it bearable:

1. **Archive by immutability.** Orders, quotes and credit requests have no update path
   **[spec]** — once you have captured a document it cannot change. Keep the last high-water `id`
   you saw and stop paging when you reach documents you already hold. This is *not* the same as
   server-side filtering: you still page from page 1 in an undefined order, so you cannot early-exit
   safely unless the ordering happens to be stable on your instance. **Test it, don't assume it.**
   **[inferred]**
2. **Reconstruct from `/interactions` where you can.** `Interaction` carries `order_id`, `quote_id`,
   `credit_request_id`, `email_id`, `stocktake_id` and `completed_form_ids`, plus
   `last_modified_time`, `user_id` and a `location` **[spec]**. It is still unfiltered, but it is a
   *narrower* row than a full order-with-items, so scanning it is cheaper. Use it as the activity
   spine, then fetch only the documents it points at that you don't already have — via
   `GET /orders/{id}`, one request each. Worth it when the change rate is low relative to history
   size.
3. **Snapshot, don't delta, for `/prices` and `/stocklevels`.** These are current-state tables with
   no history and no keys of their own. Store each run as a dated snapshot and let the warehouse
   handle change detection.
4. **Accept a staleness budget.** Nightly for Tier 2, hourly for Tier 1, and say so on the report.

### Tier 3 — small full reload (trivial)

Reference data, typically tens to hundreds of rows. Truncate and reload every run; do not bother
with delta logic:

`/users` · `/warehouses` · `/taxrates` · `/pricelists` · `/currencies` · `/formdefinitions` ·
`/configurations` · `/integrationformvalues` · `/customerdealgroupallocations` ·
`/dealgroupcustomerallocations`

### The `/logentries` cursor feed

The one endpoint with genuine cursor semantics, and the closest thing to a change feed the API has.
`filters` is **required**; only `greater_than_equals(time, …)` is supported; there are no paging
parameters; it returns 200 rows and you re-query from the last row's timestamp. **[spec]**

```
GET /v1/logentries?filters=["greater_than_equals(time, 2026-07-01T00:00:00%2B02:00)"]
```

`LogEntry` is `{id, time, tag, error_level, user, message}` **[spec]** — an operational/audit log,
not a business change feed. It will tell you about integration runs and errors, and it is useful for
a pipeline-health dashboard, but **it is not a substitute for entity-level delta**. Do not build
fact tables on it.

Two gotchas: encode `+` as `%2B` in the offset, and because the operator is `>=` you will re-receive
boundary rows — deduplicate on `id`. **[spec]**

---

## 6. The `row_version` watermark pattern

`row_version` is described as *"An automatically generated, unique number used to version-stamp table
rows in the database"* **[spec]** — a monotonically increasing sequence, bumped on every write.

**Use it, not a wall clock.** A timestamp watermark is vulnerable to clock skew between your
scheduler and Skynamo's database, to rows sharing a timestamp across a page boundary, and to
timezone handling. A database version stamp has none of those problems.

```python
def extract_with_watermark(session, endpoint, watermark, key="row_version"):
    """Pull only rows changed since `watermark`. Returns (rows, new_watermark).

    Uses greater_than (exclusive) so the row at the watermark is not re-fetched.
    Advance the stored watermark only after the load has committed — if it fails
    mid-way, the next run must re-read the same window.
    """
    rows = list(fetch_all(session, endpoint,
                          filters=[f"greater_than({key},{watermark})"]))
    new_watermark = max((int(r[key]) for r in rows if r.get(key) is not None),
                        default=watermark)
    return rows, new_watermark
```

Rules that matter:

- **Advance the watermark only after a successful commit.** Store it transactionally with the data,
  or in a table you update in the same transaction. A crash between load and watermark-update must
  cost you a re-read, not a gap.
- **Use `greater_than`, not `greater_than_equals`** so you don't re-fetch the boundary row on every
  run. Then make your loader an idempotent upsert anyway.
- **Never reset a watermark to 0 casually** — that is a full reload.
- **Keep one watermark per endpoint.** They are independent sequences.

A minimal store:

```python
import json, pathlib

class WatermarkStore:
    """Per-endpoint watermarks, persisted as JSON. Swap for a warehouse table in production."""

    def __init__(self, path="watermarks.json"):
        self.path = pathlib.Path(path)
        self._data = json.loads(self.path.read_text()) if self.path.exists() else {}

    def get(self, endpoint):
        return self._data.get(endpoint, 0)

    def set(self, endpoint, value):
        self._data[endpoint] = value
        self.path.write_text(json.dumps(self._data, indent=2))
```

---

## 7. Paging safely

`page_size=200` is the maximum and the only sensible choice — 4× fewer requests than the default 50.
**[spec] [verified]**

### The hazard

There is **no sort parameter**, so ordering is undefined and not contractually stable. **[spec]**
Offset-paginating an unordered set that is being written to concurrently can silently **skip** rows
(a row moves from page 7 to page 3 after you have read page 3) or **duplicate** them (the reverse).
Nobody raises an error; you just get a wrong number in a report.

### Mitigations, best first

1. **Bound the walk with a filter.** A Tier-1 `row_version` window is usually small enough to read
   in one or two pages, closing the window in which drift can occur. This is the real reason Tier 1
   is safer than Tier 2, quite apart from being faster.
2. **Bracket the walk with `total_item_count`.** Record it from the first page and the last. If it
   moved, the underlying set changed during your read — flag the run and re-extract.
3. **Deduplicate on the primary key as you accumulate**, not at the end. Most entities have an `id`;
   `/prices`, `/stocklevels` and the allocation endpoints do not — for those, key on the natural
   composite (see [§11](#11-dimensional-model)).
4. **Extract in a quiet window.** Field-sales instances are busy during business hours and quiet at
   night; that is when Tier 2 should run anyway.
5. **Reconcile periodically.** Weekly, compare your row count and key set per entity against
   `total_item_count` from a fresh page-1 call. Investigate any gap.

### Termination

Stop on either condition — they can disagree if rows change mid-walk, so check both **[verified]**:

```python
if total and seen >= total:        # reached the reported total
    break
if len(rows) < page_size:          # short page = last page
    break
```

---

## 8. `flags=show_nulls` is mandatory

By default the API **omits null fields entirely**. **[spec]** Two customers in the same response can
have different key sets:

```json
{"data": [
  {"id": 1, "code": "A", "name": "Acme",  "price_list_id": 3, "location": {"latitude": -33.9, "longitude": 18.4}},
  {"id": 2, "code": "B", "name": "Beta"}
]}
```

Customer 2 has no `price_list_id` and no `location` key at all. Consequences if you don't fix it:

- `pandas.json_normalize` infers columns from whichever rows it sees — column sets vary between runs.
- Parquet/Delta writes fail or silently evolve the schema.
- Power Query's `Table.FromRecords` produces different column lists per page.
- "Missing" and "null" become indistinguishable, so you cannot tell an unset price list from one you
  failed to read.

**Always send it:**

```
GET /v1/customers?page_size=200&flags=show_nulls
```

Applies to 53 operations. **Three endpoints accept no `flags` at all** — `/currencies`,
`/integrationformvalues`, `/logentries` **[spec]** — so keep defensive `.get()`-style access even
with the flag on. Whether multiple flag values can be combined, and with what separator, is not
documented. **[spec: absent]**

---

## 9. Extraction in Python

A complete, runnable extractor. It reuses the paging contract proven in
[`SkynamoClient.fetch_all_customers`](../skynamo_geo/client.py:33) **[verified]**, generalised to any
endpoint.

```python
"""Skynamo Public API -> raw JSON landing zone."""
import json, os, pathlib, random, time
from datetime import datetime, timezone

import requests

API_BASE = "https://api.skynamo.me/v1"
PAGE_SIZE = 200        # API maximum
TIMEOUT = 30           # seconds
MAX_RETRIES = 5


def make_session(instance, api_key):
    s = requests.Session()
    s.headers.update({
        "x-api-client": instance,
        "x-api-key": api_key,
        "Content-Type": "application/json",
    })
    return s


def _get(session, url, params):
    """GET with exponential backoff on 429/5xx. Never retries a 4xx other than 429."""
    for attempt in range(MAX_RETRIES):
        try:
            r = session.get(url, params=params, timeout=TIMEOUT)
        except requests.RequestException:
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep(2 ** attempt + random.random())
            continue

        if r.status_code == 429 or r.status_code >= 500:
            if attempt == MAX_RETRIES - 1:
                r.raise_for_status()
            # Honour Retry-After if the gateway sends one; otherwise back off.
            wait = float(r.headers.get("Retry-After", 2 ** attempt)) + random.random()
            time.sleep(wait)
            continue

        r.raise_for_status()
        return r
    raise RuntimeError("unreachable")


def fetch_all(session, endpoint, filters=None, flags="show_nulls", page_size=PAGE_SIZE):
    """Page through a Skynamo collection endpoint. Yields rows.

    flags='show_nulls' keeps the response shape stable, which matters as soon as
    you land the data anywhere typed.
    """
    page_number, seen, first_total = 1, 0, None
    while True:
        params = {"page_number": page_number, "page_size": page_size}
        if flags:
            params["flags"] = flags
        if filters:
            params["filters"] = "[" + ",".join(f'"{f}"' for f in filters) + "]"

        body = _get(session, f"{API_BASE}/{endpoint}", params).json()
        rows = body.get("data") or []
        if not rows:
            break
        yield from rows
        seen += len(rows)

        page = body.get("page") or {}
        total = page.get("total_item_count")
        if first_total is None:
            first_total = total
        elif total is not None and total != first_total:
            # The underlying set changed mid-walk. Undefined ordering means rows
            # may have been skipped or duplicated -- surface it, don't hide it.
            print(f"  ! {endpoint}: total_item_count moved {first_total} -> {total}; "
                  f"treat this run as suspect")

        if total and seen >= total:
            break
        if len(rows) < page_size:
            break
        page_number += 1


# --- Tier definitions, straight out of the capability matrix ------------------

TIER1 = {                       # endpoint -> filterable delta field
    "customers": "row_version",
    "products": "row_version",
    "contacts": "row_version",
    "customercomments": "row_version",
    "scheduledvisits": "row_version",
    "tasks": "row_version",
    "visitfrequencies": "row_version",
}

TIER2 = [                       # no server-side filter: full scan every run
    "orders", "invoices", "quotes", "creditrequests", "interactions",
    "completedforms", "emailinteractions", "orderitemstatuses",
    "prices", "stocklevels",
]

TIER3 = [                       # small reference tables: truncate and reload
    "users", "warehouses", "taxrates", "pricelists", "currencies",
    "formdefinitions", "dealgroups",
    "customerdealgroupallocations", "dealgroupcustomerallocations",
]

NO_FLAGS = {"currencies", "integrationformvalues", "logentries"}   # reject `flags`


def land(rows, endpoint, run_ts, root="raw"):
    """Write one endpoint's rows as newline-delimited JSON, with lineage."""
    out = pathlib.Path(root) / endpoint / f"{run_ts}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps({"_extracted_at": run_ts,
                                 "_endpoint": endpoint,
                                 **row}, ensure_ascii=False) + "\n")
    return out


def run(instance, api_key, watermarks, include_tier2=True):
    session = make_session(instance, api_key)
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    for endpoint, field in TIER1.items():
        wm = watermarks.get(endpoint)
        rows = list(fetch_all(session, endpoint,
                              filters=[f"greater_than({field},{wm})"]))
        if rows:
            land(rows, endpoint, run_ts)
            # Advance only after the write succeeded.
            watermarks.set(endpoint, max(int(r[field]) for r in rows
                                         if r.get(field) is not None))
        print(f"T1 {endpoint:24} {len(rows):>7} rows (from {field} > {wm})")

    for endpoint in TIER3:
        flags = None if endpoint in NO_FLAGS else "show_nulls"
        rows = list(fetch_all(session, endpoint, flags=flags))
        land(rows, endpoint, run_ts)
        print(f"T3 {endpoint:24} {len(rows):>7} rows (full reload)")

    if include_tier2:
        for endpoint in TIER2:
            started = time.monotonic()
            rows = list(fetch_all(session, endpoint))
            land(rows, endpoint, run_ts)
            print(f"T2 {endpoint:24} {len(rows):>7} rows "
                  f"({time.monotonic() - started:.0f}s full scan)")

    return run_ts


if __name__ == "__main__":
    run(os.environ["SKYNAMO_INSTANCE"],
        os.environ["SKYNAMO_API_KEY"],
        WatermarkStore(),                       # from section 6
        include_tier2=os.environ.get("NIGHTLY") == "1")
```

Notes on the choices made here:

- **`_extracted_at` and `_endpoint` on every row.** Lineage costs nothing at write time and is
  invaluable when a number is questioned.
- **JSONL, not a single JSON array.** Streamable, appendable, and readable by every warehouse loader.
- **Tier 2 gated behind an env var** so the hourly run stays cheap and the nightly run does the
  expensive work.
- **No parallelism.** With no published rate limit, concurrency is an unbounded risk for a bounded
  gain. Add it only after measuring, and cap it low ([§17](#17-throughput-and-politeness)).

### Flattening for a warehouse

The header/line shape needs splitting. Orders are the tricky case because **line items have no
identifier** — see [§11](#11-dimensional-model) for why the ordinal matters.

```python
def split_order(order):
    """Split one order into (header, lines). Lines get a synthetic key.

    OrderItem has no id and no product_id -- only product_code -- so the line
    key must be (order_id, ordinal) and the product join must go via code.
    """
    items = order.pop("items", None) or []
    header = order
    lines = []
    for ordinal, item in enumerate(items, start=1):
        lines.append({
            "order_id": header["id"],
            "line_ordinal": ordinal,                    # positional, NOT stable across reloads
            "order_line_key": f'{header["id"]}-{ordinal}',
            **item,
        })
    return header, lines
```

---

## 10. Extraction in Power BI / Power Query

### The credentials problem

The plain **Get Data → Web** dialog **cannot send custom headers**. Skynamo needs two
(`x-api-key`, `x-api-client`), so you must use `Web.Contents` with a `Headers` record in a query.

That works in Power BI Desktop, but has consequences you should know before choosing this route:

| Issue | Detail |
|---|---|
| **Refresh in the Service** | A query with a dynamic URL built by string concatenation is not refreshable in the Power BI Service. You must use `Web.Contents(base, [RelativePath=…, Query=…])` so the base URL stays static. |
| **Key handling** | An API key pasted into `Headers` is stored in the `.pbix`. Anyone with the file has your key. Prefer a **custom connector** with a proper credential kind, or an on-premises **data gateway** holding the credential. |
| **Anonymous auth** | You will set the data source's authentication to *Anonymous* because the real auth is in your header. That is expected, and it is exactly why the key ends up in the file. |
| **No incremental refresh out of the box** | Power BI incremental refresh needs `RangeStart`/`RangeEnd` folded into the source query. Only the 13 filterable endpoints can do that, and only on their own fields — not on date for orders/invoices. |
| **Long refreshes** | Tier 2 full scans in Power Query are slow and fragile. For anything beyond a small instance, extract with a real pipeline ([§9](#9-extraction-in-python)) and point Power BI at the warehouse. |

**Recommendation:** use Power Query for exploration and small instances. For a production model,
extract to a warehouse and let Power BI read that. If you want a supported native Power BI path,
that is the Reporting API's custom connector —
see [that article](skynamo-reporting-api-and-powerbi.md).

### A refresh-safe paging function

```m
// fnSkynamoGetAll: page through any Skynamo Public API collection endpoint.
//
//   Endpoint : "customers", "orders", ...
//   Filters  : null, or a list like {"greater_than(row_version,884213)"}
//
// RelativePath/Query are used (not string concatenation) so the base URL stays
// static and the query remains refreshable in the Power BI Service.
(Endpoint as text, optional Filters as nullable list) as table =>
let
    BaseUrl   = "https://api.skynamo.me/v1",
    ApiKey    = SkynamoApiKey,      // define these as parameters
    Instance  = SkynamoInstance,
    PageSize  = 200,                // API maximum

    FilterText =
        if Filters = null or List.Count(Filters) = 0 then null
        else "[" & Text.Combine(List.Transform(Filters, each """" & _ & """"), ",") & "]",

    FetchPage = (PageNumber as number) as record =>
        let
            QueryRecord = Record.RemoveFields(
                [
                    page_number = Text.From(PageNumber),
                    page_size   = Text.From(PageSize),
                    flags       = "show_nulls",
                    filters     = FilterText
                ],
                if FilterText = null then {"filters"} else {},
                MissingField.Ignore
            ),
            Response = Json.Document(
                Web.Contents(
                    BaseUrl,
                    [
                        RelativePath = Endpoint,
                        Query        = QueryRecord,
                        Headers      = [
                            #"x-api-key"    = ApiKey,
                            #"x-api-client" = Instance,
                            #"Accept"       = "application/json"
                        ]
                    ]
                )
            ),
            Rows = try Response[data] otherwise {}
        in
            [ Rows = Rows, Count = List.Count(Rows) ],

    // Keep requesting pages until one comes back short. List.Generate is the
    // idiomatic way to express "loop until a condition" in M.
    Pages = List.Generate(
        () => [ Index = 1, Page = FetchPage(1) ],
        each [Page][Count] > 0,
        each [ Index = [Index] + 1, Page = FetchPage([Index] + 1) ],
        each [Page]
    ),

    // Stop after the first short page: everything beyond it is empty.
    ShortPageAt = List.PositionOf(List.Transform(Pages, each [Count] < PageSize), true),
    Kept        = if ShortPageAt = -1 then Pages else List.FirstN(Pages, ShortPageAt + 1),

    AllRows = List.Combine(List.Transform(Kept, each [Rows])),
    Table   = if List.IsEmpty(AllRows)
              then #table({}, {})
              else Table.FromList(AllRows, Splitter.SplitByNothing(), {"Record"}),
    Expanded = if Table.RowCount(Table) = 0
               then Table
               else Table.ExpandRecordColumn(
                        Table, "Record",
                        Record.FieldNames(Table.Column(Table, "Record"){0}))
in
    Expanded
```

Usage:

```m
Customers = fnSkynamoGetAll("customers", null),
Orders    = fnSkynamoGetAll("orders", null),
NewCusts  = fnSkynamoGetAll("customers", {"greater_than(row_version," & Text.From(LastWatermark) & ")"})
```

### Expanding order lines in M

```m
let
    Orders   = fnSkynamoGetAll("orders", null),
    // Number the lines before expanding: OrderItem has no id, so position is
    // the only thing distinguishing two otherwise-identical lines.
    Indexed  = Table.AddColumn(Orders, "ItemsIndexed", each
                   Table.AddIndexColumn(
                       Table.FromRecords([items]), "line_ordinal", 1, 1)),
    Lines    = Table.ExpandTableColumn(
                   Table.SelectColumns(Indexed, {"id", "ItemsIndexed"}),
                   "ItemsIndexed",
                   {"line_ordinal", "product_code", "product_name", "quantity",
                    "unit_price", "list_price", "order_unit_name", "cost",
                    "tax_rate_id", "tax_rate_value", "comment"}),
    Keyed    = Table.AddColumn(Lines, "order_line_key", each
                   Text.From([id]) & "-" & Text.From([line_ordinal]), type text)
in
    Keyed
```

### Power BI settings that reduce request volume

The KB's Reporting-API rate-limit article recommends two Power BI Desktop settings that are worth
applying here too, because Power Query otherwise fires far more requests than you expect: **[kb]**

- **Options → Data Load → Background Data:** turn **off** *"Allow data preview to download in the
  background"*.
- **Options → Data Load → Parallel loading of tables:** disable it.

The article notes *"these settings do not persist"* — re-check them per file. **[kb]**

---

## 11. Dimensional model

A star schema that fits what the API actually returns. Grain and key notes are the important part —
several of these are forced by API limitations rather than chosen.

### Dimensions

| Dimension | Source | Key | Notes |
|---|---|---|---|
| `dim_customer` | `/customers` | `id` | `code` is the business key. Addresses come from `custom_fields`, not columns ([§13](#13-custom-fields-to-columns)). `location.latitude/longitude/accuracy/is_approximate` support geo analysis. |
| `dim_contact` | `/contacts` | `id` | FK to customer. |
| `dim_product` | `/products` | `id` | `code` is the business key — **and the only join key sales lines give you.** |
| `dim_order_unit` | `/products[].order_units` | `(product_id, order_unit_id)` | Bridge. Carries `multiplier`, `minimum_order_quantity`. Needed to compare quantities across units. |
| `dim_user` | `/users` | `id` | `user_name`, `display_name`, `active`, `access` (platform). **No role, team or manager** — see [§15](#15-metrics-you-cannot-build). |
| `dim_warehouse` | `/warehouses` | `id` | |
| `dim_price_list` | `/pricelists` | `id` | `prices_include_vat` matters for revenue maths. |
| `dim_tax_rate` | `/taxrates` | `id` | Current rate only; **no rate history**. Sales lines carry `tax_rate_value` as-at, so prefer that. |
| `dim_currency` | `/currencies` | `id` / `code` | Current `rate_to_base` only; **no history**. Documents carry `currency_rate_to_base` as-at. |
| `dim_deal_group` | `/dealgroups` | `id` | |
| `dim_form` | `/formdefinitions` | `id` | Plus a `dim_form_field` from `custom_fields`, keyed on field `id`. |
| `dim_date` | generated | date | Build it yourself; the API has no date dimension. |

### Facts

| Fact | Source | Grain | Key |
|---|---|---|---|
| `fact_order_header` | `/orders` | one order | `id` |
| `fact_order_line` | `/orders[].items` | one order line | **`(order_id, ordinal)` — synthetic** |
| `fact_quote_header` / `_line` | `/quotes` | one quote / line | `id` / **`(quote_id, ordinal)`** |
| `fact_credit_request_header` / `_line` | `/creditrequests` | one CR / line | `id` / **`(credit_request_id, ordinal)`** |
| `fact_invoice_header` | `/invoices` | one invoice | `id` (also `external_id`) |
| `fact_invoice_line` | `/invoices[].items` | one invoice line | **`(invoice_id, ordinal)`** — has `product_id` *and* `product_code` |
| `fact_interaction` | `/interactions` | one interaction | `id`. The activity spine; `is_visit`, `location`, links to documents |
| `fact_email_interaction` | `/emailinteractions` | one email | `id` |
| `fact_completed_form` | `/completedforms` | one completed form | `id` |
| `fact_form_answer` | `/completedforms[].custom_fields` | one answer | `(completed_form_id, custom_field_id)` — EAV |
| `fact_order_status` | `/orderstatuses` | one status event | `id`, FK `document_id` |
| `fact_order_item_status` | `/orderitemstatuses[].items` | one line status | `(id, product_id, unit_name)` |
| `fact_stock_level` | `/stocklevels` | **periodic snapshot** | `(snapshot_date, product_id, order_unit_id, warehouse_id)` |
| `fact_price` | `/prices` | **periodic snapshot** | `(snapshot_date, price_list_id, product_id, order_unit_id)` |
| `fact_scheduled_visit` | `/scheduledvisits` | one scheduled visit | `id` |
| `fact_task` | `/tasks` | one task | `id` |
| `fact_customer_comment` | `/customercomments` | one comment | `id` |
| `fact_visit_frequency` | `/visitfrequencies` | one customer-user rule | `id` |

### Grain warning 1 — sales line items have no key

`OrderItem`, `QuoteItem` and `CreditRequestItem` have **no `id`**. `InvoiceItem` has none either.
**[spec]** So there is no server-supplied way to say "this line" — and two lines on one order can be
byte-identical (same product, same unit, same quantity, same price) and still be legitimately
distinct.

Your only option is a positional surrogate: `(order_id, ordinal)` where `ordinal` is the index in
the `items` array. Live with these consequences and document them:

- **It is only as stable as the array order.** Nothing in the spec promises the array order is
  stable between reads. **[spec: absent]** For orders it is probably safe *because orders are
  immutable* — no update path exists, so the stored document should not change **[inferred]**. For
  invoices, which *are* updatable via `PUT`/`PATCH`, a reload can genuinely re-order or replace
  lines. Treat invoice lines as **delete-and-reinsert per invoice**, never as upsert-by-ordinal.
- **Do not expose the ordinal as a business identifier** in a report. It is an internal artefact.
- **Prefer header-level measures** where you can. `Order.total_amount`,
  `Invoice.total`/`tax`/`outstanding_balance` are server-computed and need no line reconstruction.

### Grain warning 2 — sales lines join to products by code only

`OrderItem`, `QuoteItem` and `CreditRequestItem` carry `product_code` and `product_name` but **no
`product_id`**. **[spec]** So `fact_order_line → dim_product` must join on `code`.

That means:

- **`code` must be unique and stable** in your `dim_product`. It is described as *"The unique code
  associated with this product"* **[spec]**, and this repo relies on that for image matching
  **[verified]**.
- **A renamed or re-issued product code silently re-points history.** If codes are ever recycled in
  your instance, past order lines will join to the wrong product. Check this before you trust
  product-level trends.
- **Unmatched lines are inevitable** — products deleted from the master, or typo'd codes. Add an
  explicit "unknown product" member rather than dropping the rows, or your revenue will not foot.
- `InvoiceItem` does have `product_id`; use it there in preference to `code`.

### Grain warning 3 — denormalised attributes on facts are as-at, not current

Nearly every transactional entity repeats `customer_id`, `customer_code`, `customer_name`,
`user_id`, `user_name`, `product_code`, `product_name`, `warehouse_name`. **[spec]**

These are the values **as at the time of the document**. Do not use them as your dimension source —
join to the dimension on the id. *Do* keep them: they are the closest thing the API gives you to a
slowly-changing dimension, and comparing them against the current dimension is a cheap way to detect
renames.

### Grain warning 4 — snapshots, not histories

`/prices` and `/stocklevels` return **current state only**, with no `id` and no history. **[spec]**
If you need trends you must snapshot on a schedule and keep every snapshot. Miss a night and that
night is gone forever. The same applies to `dim_tax_rate.rate` and `dim_currency.rate_to_base`.

---

## 12. Money and tax

Getting revenue right here takes care, because tax treatment and currency are per-document.

### VAT / tax inclusion

| Field | Where | Meaning |
|---|---|---|
| `prices_include_vat` | `Order`, `Quote`, `CreditRequest` | Whether that document's prices are tax-inclusive |
| `tax_inclusion` | `Invoice` | `Included` or `Excluded` |
| `prices_include_vat` | `PriceList` | Whether that price list is tax-inclusive |
| `tax_rate_value`, `tax_rate_id` | sales line items | The rate **as at the transaction** |
| `tax`, `total`, `outstanding_balance` | `Invoice` | Server-computed |
| `number_of_decimals_on_product_pricing` | `Configuration` | Instance-wide display precision |
| `default_tax_rate_id` | `Configuration` | Instance default |

All **[spec]**.

> **`prices_include_vat` varies per document.** Summing `total_amount` across orders without
> normalising mixes tax-inclusive and tax-exclusive figures. Derive an explicit
> `net_amount` / `tax_amount` / `gross_amount` triple per document in staging, using that document's
> own flag and its lines' `tax_rate_value`, and report on those. Never sum a raw `total_amount`
> across a mixed set.

Prefer the invoice's server-computed `tax` and `total` over anything you recompute from lines — they
are authoritative and they already handle the instance's rounding rules.

### Multi-currency

| Field | Where |
|---|---|
| `currency_code`, `currency_rate_to_base` | `Order`, `Quote`, `CreditRequest`, `Invoice`, `PriceList` |
| `rate_to_base` | `Currency` (current only) |
| `CurrencyBase` | inside `Configuration` — `code`, `is_multi_currency_enabled`, `symbol`, `decimal_separator` |

All **[spec]**.

**Convert with the document's own `currency_rate_to_base`, not the current rate from
`/currencies`.** The document field is the rate as at the transaction; `/currencies` only ever gives
you today's, with no history. Using today's rate silently restates your entire sales history every
time FX moves.

```sql
-- base-currency revenue, tax-normalised, per the document's own rate
SELECT
    o.id,
    o.currency_code,
    o.total_amount                                        AS total_doc_currency,
    o.total_amount * COALESCE(o.currency_rate_to_base, 1) AS total_base_currency
FROM fact_order_header o;
```

Check `Configuration.currency.is_multi_currency_enabled` first. If it is false, the whole concern
collapses and `currency_rate_to_base` should be 1 throughout — worth asserting in your pipeline so
you notice if it ever changes.

### Discounts

`Order`, `Quote` and `CreditRequest` carry both `discount` (a percentage) and `discount_amount` (a
value) at header level; line items carry neither, but do carry `list_price` alongside `unit_price`.
**[spec]** So line-level discount is derivable as `list_price - unit_price`, while header-level
discount applies on top. Decide once where discount lives in your model and apply it consistently —
double-counting header and line discount is the classic error here.

---

## 13. Custom fields to columns

Custom fields are an **EAV list on `Customer`, `Product`, `CompletedForm` and
`IntegrationFormValues`** **[spec]**:

```json
"custom_fields": [
  {"id": 41, "name": "Physical Address Line 1", "value": "12 Main Road"},
  {"id": 42, "name": "City", "value": "Cape Town"}
]
```

`value` is **always a string**, whatever the underlying type. **[spec]**

For customers this is not a corner case: **customer addresses exist only in `custom_fields`**, never
as top-level columns, and the field names differ per instance — which is why this repo's geolocation
tool makes field mapping an interactive step ([README §6](../README.md#6-geocoding--accuracy-logic))
**[verified]**.

### The rule: key on `id`, never on `name`

`PATCH /customfields` exists purely to **rename** a custom field, and it is keyed on `id`. **[spec]**
A rename in Skynamo therefore changes every `name` you have been matching on, with no notification
and no version bump you can detect.

```python
def pivot_custom_fields(rows, field_ids):
    """Pivot the custom_fields EAV list into columns.

    Keyed on custom-field id, because names are renameable via
    PATCH /customfields. Emits stable cf_<id> columns plus a label lookup you
    can join for display.
    """
    out = []
    for row in rows:
        flat = {k: v for k, v in row.items() if k != "custom_fields"}
        by_id = {cf["id"]: cf.get("value") for cf in (row.get("custom_fields") or [])}
        for fid in field_ids:
            flat[f"cf_{fid}"] = by_id.get(fid)
        out.append(flat)
    return out
```

Build the label lookup from `/formdefinitions` (see the recipe in
[the reference guide](skynamo-public-api-guide.md#appendix-a--recipes)) and load it as a small
dimension. Your fact and dimension tables then reference `cf_41`; only the display layer resolves it
to "Physical Address Line 1". A rename becomes a one-row dimension update instead of a pipeline
break.

### Typing

Everything arrives as a string, so cast in staging using the `type` from `/formdefinitions`
(`Text`, `Number`, `SingleSelect`, `MultiSelect`, `NestedSingleSelect`, `NestedMultiSelect`,
`Address`, `UserSingleSelect`, `UserMultiSelect`, and three label types). **[spec]** Cast
defensively — a `Number` field that users have typed free text into will contain free text.

For select types, `/formdefinitions?flags=show_enums` gives you `enumeration_values` with `id`,
`label` and `parent_id`, so nested selects can be resolved to a hierarchy. **[spec]**

### Completed forms

`CompletedForm.custom_fields` is the answer set, with `form_id`, `form_name`, `customer_id`,
`user_id`, `interaction_id` and `date`. **[spec]** Model it as
`fact_form_answer(completed_form_id, custom_field_id, value)` rather than pivoting — different forms
have entirely different field sets, so a wide table would be mostly null. Pivot only in the report
layer, per form.

---

## 14. Metrics you can build

| Metric | Sources | Notes |
|---|---|---|
| **Sales by rep / customer / product / period** | `/orders` (+ items), `/interactions` for `user_id` | `Order` has no `user_id` of its own — get the rep from the linked interaction. Normalise tax first ([§12](#12-money-and-tax)). |
| **Quote → order conversion** | `/quotes`, `/orders` | `OrderPost` accepts `quote_id`; the read `Order` schema does **not** expose it, so link via `Interaction.quote_id` / `.order_id` on the same interaction. **[spec]** |
| **Credit / return rate** | `/creditrequests` vs `/orders` | By customer, product, or rep. |
| **Order fulfilment** | `/orderstatuses` (`Logged`/`Failed` + `status_reason`), `/orderitemstatuses` | Line-level ordered-vs-outstanding quantity and value. `/orderstatuses` is date-filterable. |
| **AR ageing** | `/invoices` | `due_date`, `outstanding_balance`, `status`, `total`. Bucket against your run date. |
| **Invoice-to-order match** | `/invoices`, `/orders` | Via `reference` / `external_id` — no FK exists between them. **[spec]** |
| **Visit frequency adherence** | `/visitfrequencies` vs `/interactions` (`is_visit`) | `VisitFrequency` gives `cycle`, `frequency`, `period` (`week`/`month`/`year`) per customer-user. Compare planned vs actual visits. |
| **Call coverage / customers not visited** | `dim_customer` LEFT JOIN `fact_interaction` | Active customers with no visit in N days. |
| **On-site vs off-site visits** | `Interaction.location`, `Customer.location`, `Configuration.allow_offsite_visits` | `Location.is_approximate` is explicitly *"used by reports to determine whether a visit at a customer is on-site or off-site"* **[spec]**. Do not compute distance where `is_approximate` is true — see the note below. |
| **Scheduled-visit completion** | `/scheduledvisits` | `completer_visit_id` and `completed_date` are empty until done. **[spec]** |
| **Task completion / overdue** | `/tasks` | `completed_date` empty = open; compare `due_date`. |
| **Form completion rates** | `/completedforms`, `/formdefinitions` | Expected forms per visit type vs actual. |
| **Form answer analysis** | `/completedforms[].custom_fields` | Merchandising, competitor pricing, compliance — whatever the instance's forms capture. |
| **Stock trends** | `/stocklevels` snapshots | Requires you to have kept snapshots. |
| **Price-list coverage / gaps** | `/prices`, `/products`, `/pricelists` | Products with no price on an active list. |
| **Deal / promotion participation** | `/dealgroups`, `/dealgroupcustomerallocations` | `effective_date`, `expiry_date`, buy-free and price-bracket terms. |
| **Data quality** | `/customers` | Customers with no coordinates, no price list, no assigned user; `Location.is_approximate = true`. |
| **Pipeline health** | `/logentries` | Integration errors by `error_level` and `tag`. |

> **The geolocation caveat matters for any distance metric.** `accuracy` is in metres and
> `is_approximate` flags a coarse match. This repo writes `is_approximate=true` for street- and
> area-level geocodes with `accuracy` up to 3000 m ([`config.py`](../skynamo_geo/config.py))
> **[verified]**. A customer pinned to a suburb centroid will show a visit 2 km away as off-site.
> **Filter on `is_approximate = false` before computing any on-site/off-site or travel-distance
> measure**, and report the excluded proportion.

---

## 15. Metrics you cannot build

From the Public API alone. Several of these *are* available from the
[Reporting API](skynamo-reporting-api-and-powerbi.md) — noted where so.

| Not available | Why | Reporting API? |
|---|---|---|
| **Stable line-item identity** | Sales line items have no `id`. Positional keys only. **[spec]** | ✅ `order_item_id`, `quote_item_id`, `credit_request_item_id`, `sale_item_id`, `survey_item_id` |
| **Sales targets vs actuals** | No target entity exists anywhere in the Public API. **[spec]** | ✅ `CustomerTarget`, `UserTarget`, `AssignedUserTarget` + `Actuals` |
| **Travel distance / mileage claims** | Not exposed. **[spec]** | ✅ `TravelClaim` — `claimed_distance`, `recorded_distance`, odometer readings |
| **Time & motion / working-day breakdown** | Not exposed. **[spec]** | ✅ `UserTimeSegment` — activity, duration, recorded distance |
| **Visit duration and on-site flag, precomputed** | Derive from `date`/`end_time` and geo yourself. | ✅ `duration_sec`, `is_onsite`, `is_scheduled` |
| **Stocktake / survey results** | `Interaction.stocktake_id` is a dangling reference — **there is no stocktake endpoint**. **[spec]** | ✅ `Survey` — `stock_level`, `facings`, `retail_price` per product |
| **User roles, teams, hierarchy, managers** | `User` exposes only `user_name`, `display_name`, `email`, `active`, `access`. **[spec]** | ✅ `UserExtended.role`, plus `GET /v2/roles` |
| **Deletion detection** | No tombstones anywhere. **[spec]** | ❌ Same limitation |
| **Price / tax-rate / FX history** | Current state only; no SCD source. **[spec]** | ❌ Same limitation |
| **Customer attribute history** | Only `last_modified_time` and `row_version` — no before/after. **[spec]** | ❌ Same limitation |
| **GPS breadcrumb trails / routes** | Only a single point per interaction. **[spec]** | Partial — `recorded_distance` on time segments |
| **Login / session analytics** | Not exposed. `/logentries` has a `user` field but is an operational log. **[spec]** | ✅ `UserExtended.last_sync_time` |
| **Server-side aggregation of any kind** | No `SUM`/`COUNT`/`GROUP BY`. **[spec]** | Partial — `OrderTotal`/`QuoteTotal`/`CreditRequestTotal` are pre-aggregated |
| **Anything "since timestamp X" on orders/invoices/quotes/interactions** | No filters on those endpoints. **[spec]** | ✅ `bookmark` + `reportingPeriod` |

---

## 16. Deleted-record blindness

**Nothing in the Public API reports that a record was deleted.** No tombstones, no soft-delete
marker, no deletion audit. **[spec]** Yet deletion is real:

| What can vanish | How |
|---|---|
| Invoices | `DELETE /invoices`, `DELETE /invoicesbyexternalid` |
| Tasks | `DELETE /tasks` |
| Scheduled visits | `DELETE /scheduledvisits` |
| Order statuses | `DELETE /orderstatuses` |
| Visit frequencies | `DELETE /visitfrequencies` (1.0.28+) |
| Deal groups | `DELETE /dealgroups/{id}` |
| Prices | `POST /prices` with the price omitted |
| Stock levels | `POST /stocklevels` with `level` and `label` omitted |
| Deal-group allocations | `POST` to either allocation endpoint replaces the whole list |
| Product / customer file attachments | `PATCH` the parent with a shorter `files` array |

A `row_version` watermark **cannot** see any of these — a deleted row has no new version to report.
Your warehouse will happily keep a deleted invoice forever, inflating revenue.

### Reconciliation

The only remedy is a periodic full-key sweep. Weekly is usually enough.

```python
def reconcile_keys(session, endpoint, warehouse_ids, key="id"):
    """Find rows we hold that no longer exist upstream.

    A row_version watermark cannot detect deletions -- a deleted row has no new
    version. Only comparing full key sets will find them.
    """
    live = {r[key] for r in fetch_all(session, endpoint) if r.get(key) is not None}
    vanished = set(warehouse_ids) - live
    appeared = live - set(warehouse_ids)          # watermark gaps, or a missed run
    return vanished, appeared
```

Then **soft-delete in your warehouse** — set `is_deleted` and `deleted_detected_at`; never hard
delete. You lose the ability to distinguish "deleted upstream" from "our extract broke" if you do.

Also note **`active` is not deletion**. `Customer`, `Product`, `Contact`, `User`, `TaxRate`,
`PriceList`, `Warehouse` and `FormDefinition` all carry an `active` boolean **[spec]** — that is
deactivation, and inactive records still return from the API. This repo filters them out by default
but keeps the option to include them ([`fetch_all_customers(active_only=…)`](../skynamo_geo/client.py:33))
**[verified]**. For BI, **extract inactive records too** — historical orders reference products that
have since been deactivated, and dropping them breaks your joins.

---

## 17. Throughput and politeness

**No rate limit is documented for the Public API.** Zero occurrences of `429`, `rate limit`,
`throttl`, `quota`, `usage plan` or `Retry-After` in the spec; zero KB results for `throttl`.
**[spec] [kb]**

That is not permission. `x-api-key` is an AWS API Gateway key
(`x-amazon-apigateway-api-key-source: HEADER` **[spec]**), and API Gateway keys are normally attached
to a **usage plan** with a rate and burst limit, returning `429` with
`x-amzn-ErrorType: ThrottledException` when exceeded. **No values are published, so code for the
limit existing without guessing its number.** **[inferred]**

For comparison, Skynamo's **Reporting API** publishes hard quotas — as low as **2 requests per 10
minutes** for its `AllData` period **[spec, Analytics v2]**. Take that as evidence the platform does
throttle, and be conservative here.

### Recommended settings

| Setting | Value | Why |
|---|---|---|
| `page_size` | **200** | The maximum; 4× fewer requests than default. **[spec]** |
| Concurrency | **1–4** | No published budget to spend. Measure before raising. |
| Retry | Exponential backoff + jitter on `429` and `5xx`; honour `Retry-After` | Standard, and safe. |
| Retry on `4xx` | **Never** (except `429`) | A `400` will not fix itself. |
| Retry on `POST` | **Never blind-retry** | No idempotency keys — you will create duplicates. **[spec: absent]** Read back and reconcile. |
| HTTP timeout | **~30 s** | Matches this repo's default **[verified]**, and sits just above the ~29 s API Gateway integration limit **[inferred]**. |
| Tier 1 schedule | Hourly | Cheap; proportional to change. |
| Tier 2 schedule | Nightly, off-peak | Expensive full scans; also reduces mid-walk drift. |
| Reconciliation | Weekly | The only way to catch deletions. |

### Instrument every run

Because you are flying without published limits, your own telemetry is the only early warning you
get. Log per endpoint per run: request count, row count, duration, HTTP status histogram, and
`total_item_count` first vs last. Alert on a Tier-2 row count that moves by more than a few percent
run-over-run, on any `429`, and on any `total_item_count` drift mid-walk.

---

## 18. Operational checklist

**Extraction**
- [ ] `flags=show_nulls` on every endpoint that accepts it (all but `/currencies`,
      `/integrationformvalues`, `/logentries`).
- [ ] `page_size=200` everywhere.
- [ ] Termination checks both `total_item_count` and short-page.
- [ ] `total_item_count` compared first page vs last; drift logged.
- [ ] Backoff and retry on `429`/`5xx`; never on other `4xx`; never blind-retry a write.
- [ ] Raw payloads landed unmodified with `_extracted_at` before any transform.
- [ ] Inactive records extracted, not filtered out.

**Delta**
- [ ] `row_version` watermarks on the 7 supporting endpoints; `date` on `/orderstatuses`;
      `last_modified_time` on `/dealgroups`.
- [ ] Watermarks advanced **only** after a committed load.
- [ ] `greater_than`, not `greater_than_equals`; loader idempotent regardless.
- [ ] Tier 2 full scans scheduled off-peak with a measured runtime budget.
- [ ] Weekly full-key reconciliation with soft-delete.

**Modelling**
- [ ] Line-item surrogate keys documented as positional and non-authoritative.
- [ ] Invoice lines delete-and-reinsert per invoice, never upsert-by-ordinal.
- [ ] Sales lines joined to products by `code`; unmatched rows routed to an "unknown product" member,
      not dropped.
- [ ] Custom fields keyed on **id**, with a label dimension from `/formdefinitions`.
- [ ] Tax normalised per document using its own `prices_include_vat` / `tax_inclusion`.
- [ ] Currency converted with the **document's** `currency_rate_to_base`, not today's rate.
- [ ] `is_approximate = true` excluded from distance and on-site metrics, with the excluded share
      reported.
- [ ] Denormalised `*_name` / `*_code` fields on facts treated as as-at, not current.

**Documentation**
- [ ] Staleness stated on every report (Tier 1 hourly, Tier 2 nightly).
- [ ] Known blind spots published alongside the model — deletions, no targets, no price history.

---

## Appendix — per-endpoint extraction cookbook

`{wm}` = your stored watermark. All URLs relative to `https://api.skynamo.me/v1`. **[spec]**

### Tier 1 — server-side delta

| Endpoint | Request | Delta key |
|---|---|---|
| `/customers` | `?flags=show_nulls&page_size=200&filters=["greater_than(row_version,{wm})"]` | `row_version` |
| `/products` | `?flags=show_nulls&page_size=200&filters=["greater_than(row_version,{wm})"]` | `row_version` |
| `/contacts` | `?flags=show_nulls&page_size=200&filters=["greater_than(row_version,{wm})"]` | `row_version` |
| `/customercomments` | `?flags=show_nulls&page_size=200&filters=["greater_than(row_version,{wm})"]` | `row_version` |
| `/scheduledvisits` | `?flags=show_nulls&page_size=200&filters=["greater_than(row_version,{wm})"]` | `row_version` |
| `/tasks` | `?flags=show_nulls&page_size=200&filters=["greater_than(row_version,{wm})"]` | `row_version` |
| `/visitfrequencies` | `?flags=show_nulls&page_size=200&filters=["greater_than(row_version,{wm})"]` | `row_version` |
| `/orderstatuses` | `?flags=show_nulls&page_size=200&filters=["greater_than_equals(date,{date})"]` | `date` |
| `/dealgroups` | `?flags=show_nulls&page_size=200&filters=["greater_than_equals(last_modified_time,{ts})"]` | `last_modified_time` |

### Tier 2 — full scan, client-side delta on `last_modified_time`

| Endpoint | Request | Notes |
|---|---|---|
| `/orders` | `?flags=show_nulls&page_size=200` | Immutable; nested `items` |
| `/quotes` | `?flags=show_nulls&page_size=200` | Immutable; nested `items` |
| `/creditrequests` | `?flags=show_nulls&page_size=200` | Immutable; nested `items` |
| `/invoices` | `?flags=show_nulls&page_size=200` | Mutable — reload lines per invoice. Has unusable `row_version` |
| `/interactions` | `?flags=show_nulls&page_size=200` | Activity spine; narrow rows |
| `/completedforms` | `?flags=show_nulls&page_size=200` | Answers in `custom_fields` |
| `/emailinteractions` | `?flags=show_nulls&page_size=200` | Includes full email `content` — can be large |
| `/orderitemstatuses` | `?flags=show_nulls&page_size=200` | Nested `items` |
| `/prices` | `?flags=show_nulls&page_size=200` | **Snapshot**; no key of its own |
| `/stocklevels` | `?flags=show_nulls&page_size=200` | **Snapshot**. Optional `filters=["equals(warehouse_id,{id})"]`, `null` allowed |

### Tier 3 — small full reload

| Endpoint | Request | Notes |
|---|---|---|
| `/users` | `?flags=show_nulls&page_size=200` | |
| `/warehouses` | `?flags=show_nulls&page_size=200` | |
| `/taxrates` | `?flags=show_nulls&page_size=200` | |
| `/pricelists` | `?flags=show_nulls&page_size=200` | |
| `/currencies` | `?page_size=200` | **No `flags` support** |
| `/formdefinitions` | `?flags=show_enums&page_size=200` | `show_enums` gives you select options |
| `/customerdealgroupallocations` | `?flags=show_nulls&page_size=200` | |
| `/dealgroupcustomerallocations` | `?flags=show_nulls&page_size=200` | |
| `/configurations` | `?flags=show_nulls` | Singleton — **bare object, no `data` envelope** |
| `/integrationformvalues` | *(no params)* | Singleton; no `flags` |

### Cursor feed

| Endpoint | Request | Notes |
|---|---|---|
| `/logentries` | `?filters=["greater_than_equals(time,{ts})"]` | `filters` **required**; no paging; 200 rows/call; cursor on last `time`; encode `+` as `%2B`; dedupe on `id` |
