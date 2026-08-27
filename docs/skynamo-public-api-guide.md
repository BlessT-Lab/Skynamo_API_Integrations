# Skynamo Public API — Complete Reference

**Written against Public API `v1.0.28`** (Swagger 2.0, live spec:
`https://apidocs.skynamo.com/swagger_2.0.1023_1.0.28.json`).

> **About the copy bundled in this repo.** [`skynamo_swagger.json`](../skynamo_swagger.json) is
> `v1.0.27`. The only differences from 1.0.28 are:
> - `DELETE /visitfrequencies` is new (with the `VisitFrequencyDelete` schema).
> - `GET /dealgroups` and `GET /dealgroups/{id}` gained an extra `flags` value, `ignore_deals`.
>
> Everything else — all 62 paths, all 124 shared definitions, every parameter description and enum —
> is byte-identical. Where this document says "118 operations", the bundled 1.0.27 file has 117.

Every factual claim below is tagged by where it comes from:

| Tag | Meaning |
|---|---|
| **[spec]** | Read directly out of the Swagger document |
| **[kb]** | Stated in a `support.skynamo.com` article (linked) |
| **[verified]** | Proven in production by the code in this repo |
| **[inferred]** | Reasoned from platform behaviour — **not documented anywhere**. Treat as a hypothesis. |

---

## Contents

1. [What this API is](#1-what-this-api-is)
2. [Which Skynamo API do you want?](#2-which-skynamo-api-do-you-want)
3. [Connecting to an instance](#3-connecting-to-an-instance)
4. [Request and response conventions](#4-request-and-response-conventions)
5. [Pagination](#5-pagination)
6. [Filtering](#6-filtering)
7. [Flags](#7-flags)
8. [Endpoint catalogue](#8-endpoint-catalogue)
9. [Write semantics](#9-write-semantics)
10. [Files and images](#10-files-and-images)
11. [Custom fields](#11-custom-fields)
12. [The integrations endpoint](#12-the-integrations-endpoint)
13. [What it can and cannot do](#13-what-it-can-and-cannot-do)
14. [Errors and troubleshooting](#14-errors-and-troubleshooting)
15. [Limits and operational notes](#15-limits-and-operational-notes)
16. [Versioning and change management](#16-versioning-and-change-management)
17. [Known documentation defects](#17-known-documentation-defects)
18. [Documented gaps](#18-documented-gaps)
19. [Appendix A — recipes](#appendix-a--recipes)
20. [Appendix B — full field reference](#appendix-b--full-field-reference)

---

## 1. What this API is

A **read/write REST API over a single Skynamo instance's operational data** — customers, contacts,
products, pricing, stock, orders, quotes, credit requests, invoices, field activity (visits,
interactions, completed forms), scheduled work, and reference data. **[spec]**

| Property | Value |
|---|---|
| Base URL | `https://api.skynamo.me/v1` |
| Protocol | **HTTPS only** (`schemes: ["https"]`) |
| Style | REST, JSON, Swagger 2.0 |
| Paths | 62 |
| Operations | **118** — 58 `GET`, 25 `POST`, 14 `PUT`, 14 `PATCH`, 7 `DELETE` |
| Schema definitions | 125 |
| Functional groups (tags) | 34 |
| Content type | `application/json` (`produces` is set per operation) |
| Auth | API key in the `x-api-key` header, on **all 118 operations** |
| Tenant selection | `x-api-client` header, **required on all 118 operations** |

All facts in this table are **[spec]**.

### Architecture

The API is an **AWS API Gateway** front end. Every operation carries an
`x-amazon-apigateway-integration` block of the form: **[spec]**

```
type:           http_proxy
connectionType: VPC_LINK
uri:            http://api.${stageVariables.domainName}/api/public/<resource>
```

Three consequences worth understanding:

- **`x-api-client` is a tenant router.** The gateway resolves your instance name to a backend
  domain and proxies through to that instance's private `/api/public/*` service. One global
  hostname, many tenants. **[inferred, from the integration URIs]**
- **The gateway validates parameters but not bodies.** The document sets
  `x-amazon-apigateway-request-validator: params-only`, whose definition is
  `{validateRequestBody: false, validateRequestParameters: true}`. So a malformed query string or a
  missing required header is rejected at the edge, while a malformed JSON body is passed through to
  the Skynamo backend, which returns its own `400`. **[spec]**
- **API keys are gateway keys.** `x-amazon-apigateway-api-key-source: HEADER`. **[spec]**

### What it is *not*

- **Not a query language.** No aggregation, no grouping, no joins, no projection, no sorting.
- **Not a bulk export.** Maximum 200 records per request, no streaming, no file dump.
- **Not browser-callable.** There is **no `OPTIONS` method on any of the 62 paths**, so there is no
  CORS preflight. Calls must originate from a server, script, or desktop app. **[spec]**
- **Not event-driven.** No webhooks, no callbacks, no subscriptions, no change feed. Polling is the
  only option. **[spec]** — see [§13](#13-what-it-can-and-cannot-do).

---

## 2. Which Skynamo API do you want?

Skynamo publishes **two unrelated APIs**. Picking the wrong one costs a lot of rework, and the
support knowledge base does not always distinguish them clearly.

| | **Public API** (this document) | **Analytics / Reporting API** |
|---|---|---|
| Host | `api.skynamo.me/v1` | `analytics-api.svc.skynamo.me` |
| Auth | `x-api-key` + `x-api-client` headers | OAuth2 client-credentials → JWT Bearer |
| Purpose | Operational integration — read *and write* | Reporting and analytics — **read only** |
| Cost | Included | **Paid add-on** |
| Query power | Paging; filters on 14 of 30 list endpoints; no sorting | Sparse fieldsets, `where`, `order`, `skip`/`limit`, sub-entity expansion |
| Delta support | `row_version` filters on 7 endpoints | `bookmark` parameter + `x-bookmark` response header |
| Writes | Yes | No |
| Power BI | Hand-built queries only | Official custom connector |
| Documented rate limits | **None** | Yes, explicit per-period quotas |

**Rules of thumb:**

- **Writing data into Skynamo** → Public API. It is the only one that can write.
- **Building a BI model or dashboard** → start with the Reporting API. See
  [Skynamo Reporting API and Power BI](skynamo-reporting-api-and-powerbi.md).
- **BI without the paid add-on**, or you need entities the Reporting API does not expose →
  [Pulling Skynamo data for BI with the Public API](skynamo-public-api-for-bi.md).

---

## 3. Connecting to an instance

### 3.1 The two headers

Every request needs both. **[spec]**

| Header | Required | Value |
|---|---|---|
| `x-api-key` | Yes, all 118 ops | The API key generated in Skynamo. Spec description: *"The API key that was generated in Skynamo"*. |
| `x-api-client` | Yes, all 118 ops | The instance name. Spec description: *"The name of the Skynamo Instance the request is sent to. This is typically the company name or the first part of the URL used to access the Skynamo Instance."* |
| `Content-Type` | On requests with a body | `application/json` **[verified]** |

Header names are documented in lower case but are matched case-insensitively in practice — this
repo sends `X-API-CLIENT` / `X-API-KEY` and it works
([`client.py:11`](../skynamo_geo/client.py:11)). **[verified]**

### 3.2 Working out `x-api-client`

Take the first label of the hostname you use to log into Skynamo. The spec's own example bolds the
part you need: **[spec]**

```
demo.za.skynamo.me   →   x-api-client: demo
acme.eu.skynamo.me   →   x-api-client: acme
```

The KB agrees: *"Your Instance Name which can be found on your instance URL"*
([Postman Examples](https://support.skynamo.com/support/solutions/skynamo-api-postman-examples)). **[kb]**

> **`x-api-client` selects a tenant, not an API version.** The name invites confusion. Version is
> pinned by the `/v1` path segment.

### 3.3 Getting an API key

**Skynamo insights → Settings → Integration tokens → *Add access token* → enter a Name and
Description → Add.** *"A token will now be available for download."* **[kb]**

Sources: [How to Create a Skynamo API Key](https://support.skynamo.com/support/solutions/how-to-create-a-skynamo-api-key),
[Creating a Public API Key](https://support.skynamo.com/support/solutions/creating-a-public-api-key).
The first says to *"Log in as Account Manager"*; the second names no role. Treat Account Manager as
the only documented requirement.

> **Careful in that screen.** The same *Integration Tokens* page has a second button, **Add client
> credential**, which issues **Reporting API** OAuth credentials — a different API entirely. For the
> Public API you want **Add access token**.

Keep the key secret and out of source control. This repo deliberately never persists it:
`purge_saved_credentials()` clears keys that older versions wrote to the Windows credential store
([README §4](../README.md#4-credentials--settings)). **[verified]**

### 3.4 Smoke test

The cheapest valid call is one customer:

```bash
curl -s "https://api.skynamo.me/v1/customers?page_number=1&page_size=1" \
  -H "x-api-key: $SKYNAMO_API_KEY" \
  -H "x-api-client: yourinstance"
```

```powershell
$headers = @{ 'x-api-key' = $env:SKYNAMO_API_KEY; 'x-api-client' = 'yourinstance' }
Invoke-RestMethod -Uri 'https://api.skynamo.me/v1/customers?page_number=1&page_size=1' -Headers $headers |
    ConvertTo-Json -Depth 6
```

```python
import os, requests

session = requests.Session()
session.headers.update({
    "x-api-key": os.environ["SKYNAMO_API_KEY"],
    "x-api-client": "yourinstance",
    "Content-Type": "application/json",
})

r = session.get("https://api.skynamo.me/v1/customers",
                params={"page_number": 1, "page_size": 1}, timeout=30)
r.raise_for_status()
print(r.json()["page"])   # {'page_number': 1, 'page_size': 1, 'total_item_count': ..., ...}
```

A `200` with a `data` array and a `page` object means both headers are good. This is exactly what
[`SkynamoClient.test_connection`](../skynamo_geo/client.py:17) does. **[verified]**

### 3.5 Hosts to avoid

- **`services.skynamo.com/APIDocs`** still serves a Swagger UI shell advertising base URL
  `api.uk.dev.skynamo.me/v1` and a UK/ZA version picker at `1.0.7`. Every spec file it references
  now 404s. It is dead; ignore it.
- **Regional API hosts.** `urls.json` at `apidocs.skynamo.com` publishes exactly one spec, whose
  `host` is `api.skynamo.me` with no regional variants. **[spec]** The historical
  `api.uk.dev.skynamo.me` suggests region- and environment-split hosts once existed and were
  consolidated behind `x-api-client`. **[inferred]** No document states that `api.skynamo.me` is
  global, so if data residency matters to you, confirm with Skynamo directly.

---

## 4. Request and response conventions

### 4.1 Collection vs item endpoints — the rule that trips everyone up

Of 62 paths, 28 are item paths (`/{id}`, `/{guid}`, `/{ExternalID}`). **Item paths are read-only,
with exactly one exception in the entire API: `DELETE /dealgroups/{id}`.** **[spec]**

Every other write — create, replace, update, delete — goes to the **collection** path with an
**array** body:

```
✗  PATCH /v1/customers/123     →  does not exist
✓  PATCH /v1/customers         →  body: [ { "id": 123, ... } ]
```

**[verified]** — this is why [`update_location`](../skynamo_geo/client.py:72) and
[`attach_files`](../skynamo_geo/client.py:181) both target the collection.

### 4.2 List response envelope

Collection `GET`s return a two-key envelope. **[spec]**

```json
{
  "data": [ { "id": 1, "code": "CUST001", "name": "Acme Traders", "...": "..." } ],
  "page": {
    "page_number": 1,
    "page_size": 200,
    "total_item_count": 1543,
    "filtered_item_count": 1543
  }
}
```

Three collection endpoints have **no `page` object**: `/configurations` and
`/integrationformvalues` (singletons — `/configurations` returns a bare `Configuration` object, not
an envelope) and `/logentries` (returns `{data: […]}` only). **[spec]**

### 4.3 Item response — no envelope

`GET /customers/{id}` returns the **bare entity object**, not `{data: …}`. **[spec]**

```json
{ "id": 123, "code": "CUST001", "name": "Acme Traders", "...": "..." }
```

> This asymmetry is a real inconsistency in the API. A generic client cannot use one unwrapping
> path for both list and item calls. `GET /files/{guid}` is a further exception — it returns
> `{data: [ … ]}`, an array wrapper for a single file
> ([`get_file`](../skynamo_geo/client.py:157) reads `data[0]`). **[verified]**

### 4.4 Write responses

`POST` returns a message plus one row per created entity, echoing its new identifiers. The echoed
fields vary by resource. **[spec]**

```json
{ "message": "…", "data": [ { "id": 5012, "code": "CUST001", "row_version": 88213 } ] }
```

| Endpoint | `data[]` fields echoed |
|---|---|
| `POST /customers` | `id`, `code`, `row_version` |
| `POST /products` | `id`, `code` |
| `POST /orders` | `id`, `interaction_id` |
| `POST /files` | `id` (the GUID), `filename`, `expire_time`, `content_hash`, `transaction_id` |

`PUT`, `PATCH` and `DELETE` return only `{ "message": "…" }` — **no echo of what changed, and no
per-row result**. If you need to know the resulting state, read it back. **[spec]**

Every successful operation returns **`200`**. There is no `201 Created` and no `204 No Content`
anywhere in the API. **[spec]**

### 4.5 Error model

`400` and `404` share one schema, `ErrorModel`: **[spec]**

```json
{
  "message": "Invalid request",
  "errors": [
    { "index": 0, "detail": ["Customer name is required"] },
    { "index": 3, "detail": ["Unknown price list name", "Invalid discount"] }
  ]
}
```

**`index` is the position of the offending item in the array you submitted.** That makes batch
writes diagnosable: submit 200 customers, get back the indexes that failed, map them to your own
records and retry just those. Build your client around this rather than treating a `400` as
all-or-nothing. **[spec]**

Response codes declared across the API: `200` on all 118 operations, `400` on all 118, `404` on 31
operations — the 28 item routes, `DELETE /dealgroups/{id}`, and the two singleton
collections `/configurations` and `/integrationformvalues`. **No `401`, `403`, `409`, `429` or any `5xx` is documented anywhere.** **[spec]**
In practice `401`/`403` do occur for bad credentials — this repo checks for them explicitly
([`client.py:27`](../skynamo_geo/client.py:27)) — so code defensively rather than trusting the
declared list. **[verified]**

---

## 5. Pagination

Two query parameters, on 27 of the 30 collection endpoints. **[spec]**

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `page_number` | integer | `1` | 1-based |
| `page_size` | integer | `50` | *"(Maximum = 200)"* |

`page_size=200` is the ceiling and the right choice for any bulk read — it cuts request count 4×
versus the default ([`config.py:4`](../skynamo_geo/config.py:4)). **[spec] [verified]**

`total_item_count` is the total matching the request; `filtered_item_count` reflects the `filters`
you supplied. **[spec]** With no filters they are equal in practice. **[verified]**

### The ordering hazard

**There is no `sort`, `order_by`, or equivalent parameter anywhere in the API** — a scan of the
whole document finds zero occurrences. **[spec]** Result ordering is therefore *undefined and not
contractually stable*.

This matters more than it first appears. Offset pagination over an unordered, concurrently-mutating
set can **skip or duplicate rows** mid-walk: if a record on page 2 is updated while you are reading
page 5 and the backend's natural order shifts, you lose or double-count it.

Mitigations, in order of preference:

1. **Bound the walk with a filter** so the working set is stable and small — e.g.
   `filters=["greater_than(row_version,<watermark>)"]` (only possible on the 13 filterable
   endpoints, [§6](#6-filtering)).
2. **Snapshot `total_item_count` on the first and last page** and re-run if it moved.
3. **Deduplicate on the primary key** as you accumulate, rather than trusting page boundaries.
4. **Extract during quiet hours.**

### Termination

Stop when either condition holds — belt and braces, because the two can disagree if rows change
mid-walk: **[verified]**

```python
if total and raw_count >= total:      # reached the reported total
    break
if len(body["data"]) < PAGE_SIZE:     # short page = last page
    break
```

Taken from [`fetch_all_customers`](../skynamo_geo/client.py:33), which also guards against an empty
`data` array on the first page.

### The three exceptions

| Endpoint | Behaviour |
|---|---|
| `/configurations` | Singleton. Returns a bare `Configuration` object. No paging. **[spec]** |
| `/integrationformvalues` | Singleton. No paging. **[spec]** |
| `/logentries` | **No paging parameters at all.** *"This call retrieves 200 entries. Utilize the timestamp of the last entry as a filter for the subsequent 200 entries."* — a hand-rolled timestamp cursor. **[spec]** |

---

## 6. Filtering

### 6.1 Syntax

`filters` is a **query-string parameter of type `string`** whose value looks like a JSON array of
function calls: **[spec]**

```
?filters=["equals(active,true)"]
?filters=["starts_with(code,HB_)"]
?filters=["greater_than_equals(create_date, 2026-01-01)"]
?filters=["less_than(id,5)"]
```

URL-encoded, that first example is:

```
?filters=%5B%22equals(active%2Ctrue)%22%5D
```

### 6.2 Operators

Six, consistent wherever filtering is offered: **[spec]**

| Operator | Meaning |
|---|---|
| `equals` | `=` |
| `less_than` | `<` |
| `less_than_equals` | `<=` |
| `greater_than` | `>` |
| `greater_than_equals` | `>=` |
| `starts_with` | prefix match |

There is **no** `not_equals`, `contains`, `ends_with`, `in`, `between`, `or`, or null-check
operator. `/stocklevels` is the one place a `null` literal is documented:
`["equals(warehouse_id, null)"]` selects the null warehouse. **[spec]**

> **Combining filters is undocumented.** Every example in the spec contains exactly one predicate.
> The array syntax implies multiple entries are allowed and AND-ed —
> `?filters=["equals(active,true)","greater_than(id,100)"]` — but **no example or statement confirms
> it**. Test against your instance before relying on it. **[inferred]**

### 6.3 Date formats

Three accepted forms, documented on `create_date`: **[spec]**

| Form | Example |
|---|---|
| Short date | `["greater_than_equals(create_date, 2026-01-01)"]` |
| Short datetime | `["greater_than_equals(create_date, 2026-01-01T20:00:00)"]` |
| Roundtrip | `["equals(create_date, 2026-01-01T00:00:00.0000000%2B02:00)"]` |

**Encode `+` as `%2B`.** An unencoded `+` in a query string decodes to a space, silently corrupting
the UTC offset. The spec states this explicitly on `/logentries`: *"Make sure to encode characters
such as the plus sign (+)."* **[spec]**

### 6.4 Where filtering is available

**14 of 30 collection endpoints.** **[spec]**

| Endpoint | Filterable fields |
|---|---|
| `/customers` | `id`, `code`, `active`, `create_date`, `row_version` |
| `/products` | `id`, `row_version`, `customer_id`, `customer_code`, `user_id` |
| `/contacts` | `id`, `active`, `create_date`, `row_version`, `customer_id?`, `customer_code?` |
| `/customercomments` | `id`, `row_version`, `customer_id?`, `customer_code?` |
| `/scheduledvisits` | `id`, `create_date`, `row_version` |
| `/tasks` | `id`, `create_date`, `row_version` |
| `/visitfrequencies` | `id`, `create_date`, `row_version` |
| `/stocklevels` | `warehouse_id` (**`equals` only**, `null` allowed) |
| `/orderstatuses` | `id`, `document_id`, `date` |
| `/formdefinitions` | `id`, `active`, `type` |
| `/dealgroups` | `id`, `name` (alias `group_name`), `order_price_editable`, `last_modified_time`, `currency_code`, `customers_id`, `customers_code`, `customers_name`, `deals_id`, `deals_product_id`, `deals_product_code`, `deals_order_unit_id`, `deals_order_unit_name`, `deals_effective_date`, `deals_expiry_date`, `deals_buy_free_quantity`, `deals_buy_free_units`, `deals_price_bracket_quantity`, `deals_price_bracket_price`, `deals_modified_by` |
| `/customerdealgroupallocations` | `id`, `code`, `name`, `active`, `create_date`, `version`, `deal_groups_id`, `deal_groups_name` |
| `/dealgroupcustomerallocations` | `deal_group_id` (alias `id`), `deal_group_name` (aliases `name`, `group_name`), `order_price_editable`, `last_modified_time`, `currency_code`, `customers_*`, `deals_*` (as `/dealgroups`) |
| `/logentries` | `time` — **`greater_than_equals` only, and `filters` is REQUIRED** |

`/logentries` is the only endpoint where `filters` is mandatory. A bare `GET /logentries` fails.
**[spec]**

### 6.5 The 16 endpoints with no filtering at all

`/orders` · `/invoices` · `/quotes` · `/creditrequests` · `/interactions` · `/completedforms` ·
`/emailinteractions` · `/orderitemstatuses` · `/prices` · `/pricelists` · `/taxrates` ·
`/warehouses` · `/users` · `/currencies` · `/configurations` · `/integrationformvalues` **[spec]**

(`/invoicesbyexternalid` is not counted — it is a write-only collection whose `GET` is item-only.)

**Every high-volume transactional entity is on this list.** You cannot ask for "orders since
Tuesday" — you page the entire order history and filter client-side. This single fact dominates any
data-extraction design; see [the BI article](skynamo-public-api-for-bi.md).

### 6.6 Ten filter parameters that do not work

The document defines these shared parameters and then **references none of them from any
operation** — verified by counting `$ref` usage across all 118 operations: **[spec]**

`Filters` · `FiltersById` · `FiltersByVersion` · `FiltersByActive` · `FiltersByCreateDate` ·
`FiltersByUserId` · `FiltersByCode` · `FiltersByType` · `FiltersByCustomerId` ·
`FiltersByCustomerCode`

They render in Swagger UI's schema list and describe an appealing `filters.id=` / `filters.code=`
dotted style. **None of it is wired up.** Ignore them entirely — this is the single most misleading
thing in the document.

---

## 7. Flags

One optional query parameter, `flags`, accepted by 53 operations. **[spec]**

| Value | Where | Effect |
|---|---|---|
| `show_nulls` | 53 operations | Include fields whose value is null |
| `show_enums` | `/formdefinitions` only | Also include enumerator values |
| `ignore_deals` | `/dealgroups` and `/dealgroups/{id}` only (**1.0.28+**) | Omit the `deals` array |

```
GET /v1/customers?page_size=200&flags=show_nulls
```

> **Default responses omit null fields.** Two customers can come back with different key sets purely
> because one has no `price_list_id`. For anything that lands data in a typed store — a warehouse
> table, a dataframe, a CSV — **always send `flags=show_nulls`**, or your schema will shift row to
> row. Three endpoints accept no `flags` at all (`/currencies`, `/integrationformvalues`,
> `/logentries`), so handle absent keys defensively regardless. **[spec]**

Whether multiple flag values can be combined, and with what separator, is **not documented**.
**[spec: absent]**

---

## 8. Endpoint catalogue

All 62 paths, grouped. `L` = collection (list) path, `I` = item path.
**F** marks a filterable collection. **[spec]**

### Customers and CRM

| Path | Methods | F | Notes |
|---|---|---|---|
| `/customers` `L` | `GET` `POST` `PUT` `PATCH` | ✅ | Addresses live in `custom_fields`, not top-level |
| `/customers/{id}` `I` | `GET` | | |
| `/contacts` `L` | `GET` `POST` `PUT` `PATCH` | ✅ | `POST` requires `name` + `customer_code` |
| `/contacts/{id}` `I` | `GET` | | |
| `/customercomments` `L` | `GET` `POST` | ✅ | Create-only; no update or delete |
| `/customercomments/{id}` `I` | `GET` | | |
| `/visitfrequencies` `L` | `GET` `POST` `PUT` `PATCH` `DELETE` | ✅ | `DELETE` new in 1.0.28 — *"the customer is shown as requiring no visits from the user"* |
| `/visitfrequencies/{id}` `I` | `GET` | | |

### Products, pricing and stock

| Path | Methods | F | Notes |
|---|---|---|---|
| `/products` `L` | `GET` `POST` `PUT` `PATCH` | ✅ | `files` is an array of file GUIDs |
| `/products/{id}` `I` | `GET` | | |
| `/pricelists` `L` | `GET` `POST` `PUT` `PATCH` | | |
| `/pricelists/{id}` `I` | `GET` | | |
| `/prices` `L` | `GET` `POST` | | `POST` is upsert; **omit the price to delete it** |
| `/stocklevels` `L` | `GET` `POST` | ✅ | `POST` is upsert; **omit `level` and `label` to delete** |
| `/warehouses` `L` | `GET` `POST` `PUT` `PATCH` | | |
| `/warehouses/{id}` `I` | `GET` | | |
| `/taxrates` `L` | `GET` `POST` `PUT` `PATCH` | | |
| `/taxrates/{id}` `I` | `GET` | | |
| `/currencies` `L` | `GET` `POST` `PUT` `PATCH` | | **No `flags` support** |
| `/currencies/{id}` `I` | `GET` | | |

### Deals

| Path | Methods | F | Notes |
|---|---|---|---|
| `/dealgroups` `L` | `GET` `POST` `PUT` | ✅ | `flags=ignore_deals` (1.0.28+) |
| `/dealgroups/{id}` `I` | `GET` `DELETE` | | **The only item-level write in the API.** Blocked if customers are linked |
| `/customerdealgroupallocations` `L` | `GET` `POST` | ✅ | `POST` **replaces** a customer's whole deal-group list |
| `/customerdealgroupallocations/{id}` `I` | `GET` | | `{id}` is the **customer** id |
| `/dealgroupcustomerallocations` `L` | `GET` `POST` | ✅ | `POST` **replaces** a deal group's whole customer list |
| `/dealgroupcustomerallocations/{id}` `I` | `GET` | | `{id}` is the **deal group** id |

### Sales documents

| Path | Methods | F | Notes |
|---|---|---|---|
| `/orders` `L` | `GET` `POST` | | **No `PUT`/`PATCH`/`DELETE` — orders are immutable once created** |
| `/orders/{id}` `I` | `GET` | | |
| `/quotes` `L` | `GET` `POST` | | Immutable once created |
| `/quotes/{id}` `I` | `GET` | | |
| `/creditrequests` `L` | `GET` `POST` | | Immutable once created |
| `/creditrequests/{id}` `I` | `GET` | | |
| `/invoices` `L` | `GET` `POST` `PUT` `PATCH` `DELETE` | | Fully writable. Keyed on Skynamo `id` |
| `/invoices/{id}` `I` | `GET` | | |
| `/invoicesbyexternalid` `L` | `POST` `PUT` `PATCH` `DELETE` | | Same operations keyed on **your** `external_id`. **No collection `GET`** |
| `/invoicesbyexternalid/{ExternalID}` `I` | `GET` | | |
| `/orderstatuses` `L` | `GET` `POST` `DELETE` | ✅ | Order-level fulfilment status. `status` ∈ `Logged`, `Failed` |
| `/orderstatuses/{id}` `I` | `GET` | | |
| `/orderitemstatuses` `L` | `GET` `POST` `PUT` `PATCH` | | Line-level fulfilment: ordered vs outstanding quantity and value |
| `/orderitemstatuses/{id}` `I` | `GET` | | |

### Field activity

| Path | Methods | F | Notes |
|---|---|---|---|
| `/interactions` `L` | `GET` | | **Read-only.** The activity spine — links visits to orders/quotes/forms |
| `/interactions/{id}` `I` | `GET` | | |
| `/emailinteractions` `L` | `GET` | | Read-only. Includes full email `content` |
| `/emailinteractions/{id}` `I` | `GET` | | |
| `/scheduledvisits` `L` | `GET` `POST` `PUT` `PATCH` `DELETE` | ✅ | |
| `/scheduledvisits/{id}` `I` | `GET` | | |
| `/tasks` `L` | `GET` `POST` `PUT` `PATCH` `DELETE` | ✅ | |
| `/tasks/{id}` `I` | `GET` | | |

### Forms

| Path | Methods | F | Notes |
|---|---|---|---|
| `/formdefinitions` `L` | `GET` | ✅ | Read-only. **The key to decoding custom fields.** `flags=show_enums` |
| `/formdefinitions/{id}` `I` | `GET` | | |
| `/completedforms` `L` | `GET` | | Read-only. Answers arrive as a `custom_fields` array |
| `/completedforms/{id}` `I` | `GET` | | |
| `/customfields` `L` | `PATCH` | | **`PATCH` only** — renames a custom field. No `GET` |

### Platform

| Path | Methods | F | Notes |
|---|---|---|---|
| `/users` `L` | `GET` | | **Read-only.** No user provisioning via this API |
| `/users/{id}` `I` | `GET` | | |
| `/configurations` `L` | `GET` | | Singleton. Instance-wide settings; no paging |
| `/files` `L` | `POST` | | Upload only. **No collection `GET`** |
| `/files/{guid}` `I` | `GET` | | Returns `{data:[…]}`, not a bare object |
| `/integrations` `L` | `POST` | | Trigger an integration action |
| `/integrationformvalues` `L` | `GET` | | Singleton. Active integration's field values |
| `/logentries` `L` | `GET` | ✅ | `filters` **required**. 200 rows/call, timestamp cursor |
| `/logentries/{id}` `I` | `GET` | | |

---

## 9. Write semantics

### 9.1 Verb meanings

Stated consistently per resource. Using `/customers` as the template: **[spec]**

| Verb | Spec description | Behaviour |
|---|---|---|
| `POST` | *"Creates new customers"* | Create. Omitted fields take defaults |
| `PUT` | *"Replaces a set of customers with the provided list of customers"* | **Replace.** *"All values not specified will assume their default values"* — omitting a field **wipes it** |
| `PATCH` | *"Updates a set of customers with the provided list of customers"* | **Merge.** *"Only values specified will be updated"* |
| `DELETE` | *"Delete existing …"* | Body is a bare **array of IDs** |

> **`PUT` is destructive.** It is a true replace, not an upsert-with-merge. Send a `CustomerPut`
> with only `id` and `name` and you clear that customer's location, price list, discount, assigned
> users and custom fields. **Use `PATCH` unless you genuinely intend to reset everything.**

### 9.2 Array bodies

Every write body is an array — even for one record. **[spec] [verified]**

```json
PATCH /v1/customers
[
  { "id": 123, "location": { "latitude": -33.92, "longitude": 18.42, "accuracy": 1000, "is_approximate": false } },
  { "id": 456, "name": "Acme Traders (Pty) Ltd" }
]
```

The only non-array write bodies are `POST /files` (a single `FilePost`) and `POST /integrations`
(a single `IntegrationRequest`). **[spec]**

Batch this. It is the difference between one request and two hundred. This repo currently PATCHes
one customer per call and flags the batching opportunity as a known optimisation
([README §9](../README.md)). **[verified]**

**Maximum array length is not documented.** **[spec: absent]**

### 9.3 Identify by `id` or by `code`

Most `Patch` schemas mark `id` as `required` but then say of `code`: *"required if you do not specify
id"*. So either key works. **[spec]**

```json
[{ "id": 123, "name": "New name" }]                 // by Skynamo id
[{ "code": "CUST001", "name": "New name" }]         // by your code
```

This repo relies on it: `attach_files` keys products purely on `code`
([`client.py:181`](../skynamo_geo/client.py:181)). **[verified]**

The same either/or pattern appears across the API — `product_id` or `product_code`, `warehouse_id`
or `warehouse_name`, `price_list_id` or `price_list_name`, `assigned_user_id` or
`assigned_user_name`. Where both are supplied, the ID wins: *"ignored if priceListID is specified"*.
**[spec]**

> **Swagger `required` is being misused here.** `TaskPost` marks *both* `assigned_user_id` and
> `assigned_user_name` as `required` while their descriptions say either/or. Same on
> `ScheduledVisitPost`, `CustomerCommentPost` and `VisitFrequencyPost`. Read `required` in this
> document as "one of this group", and trust the prose over the flag. **[spec]**

### 9.4 DELETE takes a request body

```
DELETE /v1/tasks
[ 101, 102, 103 ]
```

Endpoints: `/invoices`, `/invoicesbyexternalid` (string external IDs), `/orderstatuses`,
`/scheduledvisits`, `/tasks`, `/visitfrequencies` (1.0.28+). Plus `DELETE /dealgroups/{id}`, which
takes no body. **[spec]**

> **A body on `DELETE` is unusual and some HTTP stacks silently drop it.** `requests` and
> `Invoke-RestMethod` handle it; certain proxies and older clients do not. If a delete
> "succeeds" but nothing changes, check the body actually left your process. **[inferred]**

### 9.5 Upsert-and-delete-by-omission

Two endpoints overload `POST` as an upsert where **omitting a value deletes the record**: **[spec]**

| Endpoint | Delete trigger |
|---|---|
| `POST /prices` | *"If the price are not specified then the price will be deleted"* |
| `POST /stocklevels` | *"If the level and label are not specified then the stocklevel will be deleted"* |

Distinguish "field absent" from "field null" from "field zero" carefully in your serialiser here.
A JSON library that omits `None` will delete data you meant to zero.

### 9.6 Allocations replace, they do not append

`POST /customerdealgroupallocations` — *"This replaces the existing deal group list for each
specified customer."* `POST /dealgroupcustomerallocations` — *"This replaces the existing customer
list for each specified deal group."* **[spec]**

To add one deal group to a customer you must send the **complete** desired list. Read the current
allocation first. An empty or null array is a legitimate "clear all", explicitly *"not treated as an
error"*. **[spec]**

---

## 10. Files and images

There is **no product-image or customer-image endpoint**. File handling is generic and two-step.

### 10.1 Upload

```json
POST /v1/files
{
  "filename": "ABC123.png",
  "content": "<base64 of the file bytes>",
  "content_hash": "<base64 hash>",
  "transaction_id": 12345
}
```

Only `filename` and `content` are needed in practice
([`upload_file`](../skynamo_geo/client.py:133)). **[verified]**

The response's **`data[0].id` is the file's GUID string**, despite the spec typing it as `integer`
and giving `id`, `filename` and `transaction_id` the identical description *"The Guid of the created
file"*. **[spec defect / verified]**

### 10.2 Attach to a parent

`PATCH` the parent with the desired `files` array. **[verified]**

```json
PATCH /v1/products
[ { "code": "ABC123", "files": ["8f3c…", "b12a…"] } ]
```

**This sets `files` to exactly what you send.** One call therefore attaches, replaces *and*
detaches, depending on what you put in the array. To add without losing existing images, read the
product's current `files`, union in the new GUIDs, and send the result. To detach, send the list
minus the GUID you want gone. **[verified]** — this is precisely how
[`image_engine`](../skynamo_geo/image_engine.py) implements merge, replace and remove modes.

`customers` and `products` both accept `files` and `transaction_id` on `PUT`/`PATCH`. **[spec]**

### 10.3 Two rules that are not in the spec

From [How to upload customer images using Postman](https://support.skynamo.com/support/solutions/how-to-upload-customer-images-using-postman): **[kb]**

1. ***"Images last 10 minutes."*** An uploaded file is a **temporary** object — `File.expire_time`
   confirms the concept exists **[spec]**. Attach it to a parent promptly. Do not upload a batch of
   500 images and then start attaching them; interleave upload-then-attach per file.
2. ***"if you used the transaction ID somewhere (customers or products), then files not specified
   with the same transaction ID are removed."*** `transaction_id` groups files into a unit, and the
   parent `PATCH` **sweeps away files in that transaction which you did not list**. If you use
   `transaction_id` at all, be exhaustive within it.

### 10.4 Reading a file back

`GET /files/{guid}` returns `{data:[{id, filename, expire_time, content, content_hash,
transaction_id}]}`. **[spec]**

A parent's `files` array contains **bare GUID strings with no names or metadata**. There is no
expansion parameter, so displaying "the images on this product" costs **one request per GUID**
([`list_attached_images`](../skynamo_geo/image_engine.py)). **[verified]**

### 10.5 There is no delete

**No `DELETE /files/{guid}` exists. Neither does `DELETE /products/{id}` or
`DELETE /customers/{id}`.** **[spec]**

"Removing" an image means re-`PATCH`ing the parent's `files` without that GUID — it **detaches**
the file. The underlying file object may well persist server-side; nothing in the public API can
remove it. Say "detach", not "delete", in any UI you build on this. **[verified]**

---

## 11. Custom fields

### 11.1 Shape

A per-instance EAV list carried by `Customer`, `Product`, `CompletedForm` and
`IntegrationFormValues`: **[spec]**

```json
"custom_fields": [
  { "id": 41, "name": "Physical Address Line 1", "value": "12 Main Road" },
  { "id": 42, "name": "City",                    "value": "Cape Town" }
]
```

`value` is always a **string**; *"format will depend on type of custom field"*. **[spec]**

> **Instance-specific by definition.** Field names, ids and even which entities carry which fields
> vary from one Skynamo instance to the next. This is exactly why this repo's geolocation tool makes
> address-field mapping an interactive step rather than hard-coding names
> ([README §6](../README.md#6-geocoding--accuracy-logic)) — customer addresses live *only* in
> `custom_fields`, never as top-level fields. **[verified]**

### 11.2 Discovery

`GET /formdefinitions?flags=show_enums` returns each form's fields with `id`, `name`, `required`,
`type`, and `enumeration_values` (each with `id`, `label`, `parent_id` and an optional `comment`
object that can require a follow-up comment field). Documented `type` values: `Text`, `Number`,
`SingleSelect`, `MultiSelect`, `NestedSingleSelect`, `NestedMultiSelect`, `Address`,
`UserSingleSelect`, `UserMultiSelect`, `HeadingLabel`, `NormalLabel`, `FinePrintLabel`. **[spec]**

`FormDefinition.type` values: `Static`, `Standalone`, `Visit`, `Order`, `Quote`, `CreditRequest`,
`Activity`. **[spec]**

### 11.3 Renaming

`PATCH /customfields` takes `[{id, name}]` — both required. It is the **only** operation on that
path; there is no `GET`, `POST` or `DELETE`. **[spec]**

> **Consequence for anything downstream: key on `id`, never on `name`.** A rename here silently
> breaks every integration, report or pivot that matched on the label.

---

## 12. The integrations endpoint

`POST /integrations` executes one of four named actions. Body is a single `IntegrationRequest`.
**[spec]**

| `action` | Companion field | Purpose |
|---|---|---|
| `AutoGrowEnums` | `enum_grow_data: [{customfield_id, enums: […]}]` | Append options to a select-type custom field |
| `AddCustomFields` | `fields_to_add: [{form_id, name, type}]` | Create custom fields on a form |
| `ImportNow` | — | **Trigger the instance's ERP import on demand** |
| `ResubmitOrderItemDocuments` | `document_ids: [int]` | Re-send order documents |

Response is `{message, fields_added: {id, name}}`. Its `400` uses a bare `{message}` rather than
`ErrorModel` — the one operation in the API that does. **[spec]**

`ImportNow` is the closest thing to a control-plane operation here: it lets an external scheduler
drive Skynamo's own import cycle instead of guessing when it runs.

`GET /integrationformvalues` returns *"the active integration form values"* — `id`, `name`,
`last_modified_time` and a `custom_fields` array: the current configuration of whichever integration
is live. **[spec]**

---

## 13. What it can and cannot do

### Can

**Read** — every core entity: customers, contacts, products, order units, price lists, prices,
stock levels, warehouses, tax rates, currencies, deal groups and allocations, orders, quotes, credit
requests, invoices, order/order-item statuses, interactions, email interactions, completed forms,
form definitions, scheduled visits, tasks, visit frequencies, customer comments, users, files, log
entries, instance configuration.

**Write** —
- Create, replace and merge: customers, contacts, products, price lists, tax rates, currencies,
  warehouses, visit frequencies, order item statuses, invoices (by Skynamo id *or* your external
  id), scheduled visits, tasks.
- Create only: orders, quotes, credit requests, customer comments, order statuses.
- Upsert: prices, stock levels.
- Delete: invoices, order statuses, scheduled visits, tasks, visit frequencies, deal groups.
- Batch any of the above — array bodies with positional error reporting.
- Upload files and attach them to customers or products.
- Set customer geolocation with an explicit `accuracy` and `is_approximate` flag.
- Rename custom fields; add custom fields and grow their enums; **trigger an ERP import**.

**Operational** — page at 200/request; filter and delta-detect on 13 endpoints; discover the
instance's custom-field schema; read an audit log with a timestamp cursor.

### Cannot

**Query power**
- **No sorting.** No `sort`/`order_by` parameter exists. Result order is undefined. **[spec]**
- **No aggregation.** No counts, sums, averages, or group-by. Everything is computed client-side.
- **No sparse fieldsets.** No way to request "just id and code" — you always get the full entity.
- **No joins or expansion.** File GUIDs resolve one request at a time; nothing expands a nested
  reference.
- **No filtering on 16 of 30 collection endpoints** — including *every* high-volume transactional
  entity. **[spec]**
- **No `not_equals`, `contains`, `in`, `between`, `or`,** or null-check filter operators.
- **No filtering on `last_modified_time`** except on `/dealgroups`. **[spec]**
- **No cursor or keyset pagination.** Offset only, which is unsafe over an unordered mutable set.
- **No bulk export.** 200 rows per request, no streaming, no file drop.

**Data lifecycle**
- **No delete for customers, products, contacts, orders, quotes, credit requests, files,
  customer comments, price lists, warehouses, tax rates or currencies.** **[spec]**
- **No tombstones.** Nothing anywhere reports that a record *was* deleted, so a consumer cannot
  detect deletions except by full reconciliation.
- **Orders, quotes and credit requests are immutable** — `POST` only, no update path. **[spec]**
- **No file deletion**, only detachment. **[spec]**

**Platform**
- **No webhooks, callbacks, subscriptions or change feed.** Zero occurrences of `webhook` or
  `callback` in the spec, and zero results for `webhook` in the support KB. **Polling is the only
  mechanism.** **[spec] [kb]**
- **No CORS** — no `OPTIONS` on any path, so no browser-side calls. **[spec]**
- **No user or role management.** `/users` is read-only; roles and team hierarchy are not exposed
  at all. **[spec]**
- **No documented rate limit** — which is a risk to design around, not permission to hammer it.
  See [§15](#15-limits-and-operational-notes).
- **No idempotency keys.** A retried `POST` will create duplicates. **[spec: absent]**
- **No `409 Conflict` or optimistic concurrency.** `row_version` is readable but there is no
  documented if-match semantic, so concurrent writers silently overwrite each other. **[spec]**
- **No `GET` on `/files` or `/invoicesbyexternalid` collections**, and no `GET` on `/customfields`.
  **[spec]**

---

## 14. Errors and troubleshooting

| Symptom | Almost certainly | Fix |
|---|---|---|
| **`{"message":"Missing Authentication Token"}`** | **Wrong route — not an auth problem.** AWS API Gateway returns this for a path/method pair that is not deployed. | Check the verb against [§8](#8-endpoint-catalogue). Most often: you used `/{id}` with a write verb. Also check for a stray trailing slash or wrong casing. **[verified]** |
| `403` / `401` | Missing, wrong, or revoked `x-api-key`; or `x-api-client` naming an instance the key does not belong to. | Re-run the [§3.4](#34-smoke-test) smoke test. Note neither code is declared in the spec, but both occur. **[verified]** |
| `400` with `errors[].index` | One or more items in your array body are invalid. | Map each `index` back to your source record; fix and resubmit just those. **[spec]** |
| `400` with a bare `message` | Body failed backend validation, or you called `/integrations`. Remember the gateway does **not** validate bodies (`params-only`). | Validate your payload against the `*Post`/`*Put`/`*Patch` schema in [Appendix B](#appendix-b--full-field-reference). **[spec]** |
| `404` on an item route | No such id/code/GUID **in this instance**. | Confirm `x-api-client`. A valid id in one tenant is a `404` in another. |
| Response keys missing from row to row | Nulls are omitted by default. | Add `flags=show_nulls`. **[spec]** |
| A filter is silently ignored | You used one of the ten dead `Filters*` parameters, or a field that endpoint does not support. | Check [§6.4](#64-where-filtering-is-available) and [§6.6](#66-ten-filter-parameters-that-do-not-work). |
| Date filter matches nothing | Unencoded `+` in a UTC offset became a space. | Encode as `%2B`. **[spec]** |
| `PUT` wiped fields you did not send | Working as designed — `PUT` replaces. | Use `PATCH`. **[spec]** |
| A price or stock level vanished after an update | You omitted `price`, or `level`+`label`. That is the documented delete trigger. | Always send the value explicitly. **[spec]** |
| Adding one deal group removed the others | Allocation `POST`s replace the whole list. | Read current allocations, append, send the full list. **[spec]** |
| An uploaded image never appears | The file expired before you attached it (10 minutes), or a `transaction_id` sweep removed it. | Attach immediately after upload; be exhaustive within a `transaction_id`. **[kb]** |
| Pagination returns duplicates or misses rows | Undefined ordering plus concurrent writes. | Bound the walk with a filter; dedupe on primary key. See [§5](#5-pagination). |
| `GET /logentries` returns `400` | `filters` is **required** there. | Send `filters=["greater_than_equals(time, …)"]`. **[spec]** |
| `DELETE` reports success, nothing deleted | Your HTTP client dropped the request body. | Confirm the body is on the wire. **[inferred]** |

---

## 15. Limits and operational notes

| Item | Value | Source |
|---|---|---|
| `page_size` maximum | **200** | **[spec]** — *"(Maximum = 200)"* |
| `page_size` default | 50 | **[spec]** |
| `page_number` default | 1 | **[spec]** |
| `/logentries` rows per call | 200, timestamp cursor, no paging params | **[spec]** |
| Uploaded file lifetime | **~10 minutes** before attachment | **[kb]** |
| Client HTTP timeout | 30 s is a workable default | **[verified]** — [`config.py:6`](../skynamo_geo/config.py:6) |
| **Rate limit / quota / throttling** | **Not documented.** Zero occurrences of `429`, `rate limit`, `throttl`, `quota`, `usage plan` or `Retry-After` in the spec; zero KB results for `throttl`. | **[spec] [kb]** |
| API Gateway usage plan | Because `x-api-key` is an API-Gateway-sourced key, a usage plan is almost certainly enforced, and a `429` with `x-amzn-ErrorType: ThrottledException` would be the standard response. **No values are published — do not code to a guessed number.** | **[inferred]** |
| Request/integration timeout | REST API Gateway integrations have historically capped around **29 seconds**. Nothing Skynamo publishes confirms this. | **[inferred]** |
| Maximum array-body length | Not documented. | **[spec: absent]** |
| Maximum upload size | Not documented. API Gateway payloads are typically capped ~10 MB, and base64 inflates bytes by ~33%. | **[inferred]** |

### Recommended client behaviour

Given that no limits are published, be conservative and instrument everything:

- **`page_size=200`** for every bulk read.
- **Serial, or low bounded concurrency** (2–4). You have no published budget to spend.
- **Retry with exponential backoff and jitter** on `429` and `5xx`. Honour `Retry-After` if it
  appears. Never retry a `400`.
- **Never blind-retry a `POST`** — there are no idempotency keys, so you will create duplicates.
  Read back and reconcile instead.
- **Batch writes** into array bodies and use `errors[].index` for partial failures.
- **Always `flags=show_nulls`** when landing data anywhere typed.
- **Log every request**: endpoint, params, status, row count, duration. When something regresses,
  this is the only evidence you will have.
- **Timeout at ~30 s** and treat a timeout as retryable-but-possibly-applied for writes.

---

## 16. Versioning and change management

| | |
|---|---|
| URL version | `/v1` — the only version ever published on `api.skynamo.me` |
| Contract version | `info.version` = `1.0.28` |
| Spec filename | `swagger_2.0.1023_1.0.28.json` — `2.0.1023` appears to be a platform build, `1.0.28` the API contract **[inferred]** |
| Spec index | `https://apidocs.skynamo.com/urls.json` → `[{"name":"1.0.28","url":"./swagger_2.0.1023_1.0.28.json"}]` |

**Only the current spec is published.** `urls.json` lists exactly one entry — there is no archive
and no version picker, so you cannot fetch an older contract to diff against. Keep your own dated
copies. **[spec]**

**There is no changelog, deprecation policy, sunset schedule, or version-negotiation header
anywhere.** **[spec] [kb]** Changes are silent. The previous generation of the API (`1.0.7`, build
`2.0.388`, with separate UK and ZA specs) has been withdrawn; comparing tag lists shows 1.0.28 added
Configurations, Currencies, Customer Deal Group Allocations, Customfields, Deal Group Customer
Allocations, Email Interactions, Files, Form Definitions, Integrations, Integration Form Values,
Invoices By External ID, Log Entries, Order Statuses and Order Item Statuses since then.

### Fields already marked deprecated **[spec]**

| Field | Status |
|---|---|
| `Interaction.product_numbers_survey_id` | *"deprecated and will be removed. Please use stocktake_id."* |
| `Configuration.minimum_password_length` | `(Deprecated)` |
| `Configuration.minimum_password_non_apha_length` | `(Deprecated)` |
| `Configuration.max_invalid_password_attempts` | `(Deprecated)` |
| `Configuration.days_until_password_expiry` | `(Deprecated)` |

### Detecting changes yourself

Since nobody will tell you, automate the diff:

```bash
curl -s -o skynamo_swagger_new.json https://apidocs.skynamo.com/swagger_2.0.1023_1.0.28.json
py - <<'PY'
import json
old = json.load(open('skynamo_swagger.json', encoding='utf-8-sig'))
new = json.load(open('skynamo_swagger_new.json', encoding='utf-8-sig'))
M = ('get', 'post', 'put', 'patch', 'delete')
ops = lambda d: {(m, p) for p, o in d['paths'].items() for m in o if m in M}
print('version', old['info']['version'], '->', new['info']['version'])
print('+ops ', sorted(ops(new) - ops(old)))
print('-ops ', sorted(ops(old) - ops(new)))
print('+defs', sorted(set(new['definitions']) - set(old['definitions'])))
print('-defs', sorted(set(old['definitions']) - set(new['definitions'])))
PY
```

Note the filename encodes the version, so **the URL itself changes on each release**. Read
`urls.json` first to discover the current filename.

---

## 17. Known documentation defects

Real errors in the published spec. Where behaviour is ambiguous, **verify against your instance**
before building on it.

| # | Defect | Impact |
|---|---|---|
| 1 | **The spec's own three "Helpful links" all 404.** `info.description` points at `/support/solutions/articles/<numeric-id>-<slug>` URLs; the KB moved to `/support/solutions/<slug>`. | Use the links in [§3.3](#33-getting-an-api-key) instead. |
| 2 | **`FiltersProducts` contradicts itself.** Lists filterable fields as `id`, `row_version`, `customer_id`, `customer_code`, `user_id` — then gives `["starts_with(code,HB_)"]` as an example. `code` is not in the list. | Test whether `code` filtering works on `/products`. |
| 3 | **Invoice status enums differ between read and write.** Read (`Invoice.status`): `Draft`, `Authorized`, `Delivered`, `Outstanding`, `Paid`, `Deleted`. Write (`InvoicePost.status`/`InvoicePatch.status`): `Paid`, `NotSpecified`, `OutStanding`, `Deleted`, `Void`. Note `Outstanding` vs **`OutStanding`**. | You cannot round-trip a status. Map explicitly in both directions, and mind the casing. |
| 4 | **Line items have no primary key.** `OrderItem`, `QuoteItem` and `CreditRequestItem` have **no `id` and no `product_id`** — only `product_code`. `InvoiceItem` has both product identifiers but still no `id`. | Line items are not individually addressable, and order/quote/credit lines join to products by code only. Significant for BI — see [that article](skynamo-public-api-for-bi.md). |
| 5 | **`Customer.default_warehouse_name` is typed `integer`.** Almost certainly a string. | Do not rely on the declared type; sniff the value. |
| 6 | **`POST /files` response typing is wrong.** `data[].id` is declared `integer` but returns a GUID string, and `id`, `filename` and `transaction_id` all carry the identical description *"The Guid of the created file"*. | Treat `data[0].id` as a string. **[verified]** |
| 7 | **`required` used to mean "one of".** `TaskPost` marks both `assigned_user_id` and `assigned_user_name` required; likewise `ScheduledVisitPost`, `CustomerCommentPost`, `VisitFrequencyPost`. | Generated clients will demand both. Trust the descriptions. |
| 8 | **`PUT /invoices`** is described as *"Replaces a set of invoices with the provided list of **products**"*. | Copy-paste error; it does take invoices. |
| 9 | **Copy-pasted schema descriptions.** `TimeZone`, `DistanceUnit` and `CurrencyBase` all say *"This is a contact in Skynamo…"*. `OrderItemStatus.last_modified_time` says *"The last time the completed form was modified"*. `LogEntry` is *"used for fetching information about invoices"*. | Descriptions are unreliable on these schemas; trust field names and types. |
| 10 | **Placeholder left in the spec.** `FiltersFormdefinitions` ships the example `["greater_than_equals(type, ???)"]`. | Use the documented `FormDefinition.type` values from [§11.2](#112-discovery). |
| 11 | **Question-marked filter fields.** `FiltersContacts` and `FiltersComments` list `customer_id?` and `customer_code?` — the author was unsure whether they work. | Test before depending on them. |
| 12 | **`Configuration.form_photo_wait_time` and `session_duration`** are typed `date-time` but are plainly durations. | Parse accordingly. |
| 13 | **Ten `Filters*` parameters are defined but unused.** See [§6.6](#66-ten-filter-parameters-that-do-not-work). | The most misleading thing in the document. |
| 14 | **Recurring typos**: *"Availiable values"*, *"specfic"*, *"transtion id"*, *"ot"* for "or". | Cosmetic, but do not copy them into your own docs. |

---

## 18. Documented gaps

Genuinely absent from both the spec and the support KB. Listed so you know these were *looked for*,
not overlooked. If you need any of them, ask Skynamo directly.

- **Public API rate limits, quotas, burst behaviour, `429` semantics and `Retry-After`.**
- **API key lifecycle** — expiry, rotation, revocation, and whether there is a limit on how many
  keys an instance can hold.
- **API key scope** — per-user or per-instance, and what permissions a key carries. (Created inside
  one instance's Settings and paired with a separate `x-api-client` header, per-instance looks very
  likely — but nothing says so. **[inferred]**)
- **Which admin role grants access to Integration Tokens.** One article says *"Account Manager"*,
  which does not match Skynamo's documented Instance/Subscription Administrator roles.
- **Request and integration timeouts.**
- **Maximum array-body length and maximum upload size.**
- **Whether multiple `filters` predicates combine, and with what semantics.**
- **Whether multiple `flags` values combine, and with what separator.**
- **The regional-host story** — whether `api.skynamo.me` is genuinely global, and where data resides.
- **The string `"Missing Authentication Token"`** — appears nowhere in the spec or the KB, despite
  being the most common failure a new integrator hits. The entry in
  [§14](#14-errors-and-troubleshooting) comes from this repo's own production experience.
- **Any changelog, deprecation policy or sunset schedule.**

---

## Appendix A — recipes

### A.1 Page through everything

```python
API_BASE = "https://api.skynamo.me/v1"
PAGE_SIZE = 200          # API maximum


def fetch_all(session, endpoint, filters=None, flags="show_nulls", page_size=PAGE_SIZE):
    """Page through any Skynamo collection endpoint. Yields rows.

    filters: list of predicate strings, e.g. ['greater_than(row_version,123)']
    flags:   'show_nulls' keeps the response shape stable — important when
             landing data in anything typed.
    """
    page_number = 1
    seen = 0
    while True:
        params = {"page_number": page_number, "page_size": page_size}
        if flags:
            params["flags"] = flags
        if filters:
            # The API wants a JSON-array-looking string.
            params["filters"] = "[" + ",".join(f'"{f}"' for f in filters) + "]"

        r = session.get(f"{API_BASE}/{endpoint}", params=params, timeout=30)
        r.raise_for_status()
        body = r.json()

        rows = body.get("data") or []
        if not rows:
            break
        yield from rows

        seen += len(rows)
        total = (body.get("page") or {}).get("total_item_count")
        if total and seen >= total:
            break
        if len(rows) < page_size:
            break
        page_number += 1
```

### A.2 Incremental customer pull by `row_version`

```python
def customers_since(session, watermark=0):
    """Only customers changed since `watermark`. Returns (rows, new_watermark).

    row_version is a monotonically increasing database version stamp — a far
    safer cursor than a wall clock, which is vulnerable to skew and ties.
    """
    rows = list(fetch_all(
        session, "customers",
        filters=[f"greater_than(row_version,{watermark})"],
    ))
    new_watermark = max((int(c["row_version"]) for c in rows if c.get("row_version")),
                        default=watermark)
    return rows, new_watermark
```

Works the same way on `/products`, `/contacts`, `/customercomments`, `/scheduledvisits`, `/tasks`
and `/visitfrequencies` — the seven endpoints exposing a `row_version` filter.

### A.3 Batch PATCH with per-row error handling

```python
def patch_batch(session, endpoint, items):
    """PATCH up to `len(items)` records in one call.

    Returns a list of (index, [messages]) for the rows that failed; empty on
    full success. The API reports failures by position in the array you sent,
    which is what makes batching safe.
    """
    r = session.patch(f"{API_BASE}/{endpoint}", json=items, timeout=30)
    if r.ok:
        return []
    try:
        errors = r.json().get("errors") or []
    except ValueError:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
    return [(e.get("index"), e.get("detail", [])) for e in errors]


failures = patch_batch(session, "customers", [
    {"id": 123, "location": {"latitude": -33.92, "longitude": 18.42,
                             "accuracy": 1000, "is_approximate": False}},
    {"id": 456, "name": "Acme Traders (Pty) Ltd"},
])
for index, messages in failures:
    print(f"row {index} rejected: {'; '.join(messages)}")
```

### A.4 Upload an image and attach it to a product

```python
import base64, pathlib


def upload_and_attach(session, product_code, image_path, existing_guids=()):
    """Upload one image and merge it into a product's files list.

    Upload then attach immediately: files expire ~10 minutes after upload.
    The parent PATCH sets `files` to exactly what you send, so pass the union
    with whatever is already attached or you will detach it.
    """
    path = pathlib.Path(image_path)
    content = base64.b64encode(path.read_bytes()).decode("ascii")

    r = session.post(f"{API_BASE}/files",
                     json={"filename": path.name, "content": content}, timeout=60)
    r.raise_for_status()
    guid = r.json()["data"][0]["id"]        # a GUID string, despite the spec saying integer

    files = list(dict.fromkeys([*existing_guids, guid]))   # de-dupe, keep order
    r = session.patch(f"{API_BASE}/products",
                      json=[{"code": product_code, "files": files}], timeout=30)
    r.raise_for_status()
    return guid
```

### A.5 Walk the audit log

```python
from urllib.parse import quote


def log_entries_since(session, iso_timestamp):
    """Walk /logentries, 200 rows at a time, cursoring on the last timestamp.

    /logentries has no paging parameters and REQUIRES a filter. Encode '+' in
    the UTC offset as %2B or the offset silently becomes a space.
    """
    cursor = iso_timestamp
    while True:
        predicate = f'["greater_than_equals(time, {cursor})"]'
        r = session.get(f"{API_BASE}/logentries?filters={quote(predicate, safe='[](),')}",
                        timeout=30)
        r.raise_for_status()
        rows = r.json().get("data") or []
        if not rows:
            return
        yield from rows
        if len(rows) < 200:
            return
        cursor = rows[-1]["time"].replace("+", "%2B")


for entry in log_entries_since(session, "2026-07-01T00:00:00%2B02:00"):
    print(entry["time"], entry["error_level"], entry["tag"], entry["message"])
```

> Duplicate boundary rows are possible because the cursor is `>=`. De-duplicate on `id`.

### A.6 Discover an instance's custom-field schema

```python
def custom_field_schema(session):
    """Map custom-field id -> (form name, field name, type, enum labels).

    Field *names* change (PATCH /customfields renames them), so anything
    downstream must key on the id.
    """
    schema = {}
    for form in fetch_all(session, "formdefinitions", flags="show_enums"):
        for field in (form.get("custom_fields") or []):
            schema[field["id"]] = {
                "form": form.get("name"),
                "form_type": form.get("type"),
                "field": field.get("name"),
                "type": field.get("type"),
                "required": field.get("required"),
                "options": [e.get("label")
                            for e in (field.get("enumeration_values") or [])],
            }
    return schema
```

---

## Appendix B — full field reference

Generated from `v1.0.28`. `*` marks a field the spec flags `required` — read that as *"one of this
group"* where the description says either/or ([§9.3](#93-identify-by-id-or-by-code)).

Naming convention across the document: the base name (`Customer`) is the **read** shape returned by
`GET`; `…Post` / `…Put` / `…Patch` / `…Delete` are the **write** shapes.

### Customers

#### `Customer`

This is a customer in Skynamo, used for fetching information about customers

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique id of the customer |
| `code` | `string` | The unique code associated with this customer |
| `name` | `string` | The name of the customer |
| `active` | `boolean` | Whether or not the customer is active |
| `location` | [`Location`](#location) | — |
| `price_list_id` | `integer` | The unique identifier of the price list associated with this customer |
| `price_list_name` | `string` | The name of the price list associated with this customer |
| `assigned_users` | [`CustomerAssignedUsers`](#customerassignedusers) | — |
| `default_discount` | `number` (float) | The default discount applied for this customer when creating orders |
| `default_warehouse_id` | `integer` | The unique identifier of the warehouse associated with this customer |
| `default_warehouse_name` | `integer` | The name of the warehouse associated with this customer |
| `row_version` | `number` (long) | An automatically generated, unique number used to version-stamp table rows in the database |
| `last_modified_time` | `string` (date-time) | The last time this customer was modified |
| `create_date` | `string` (date-time) | The time at which this customer was created |
| `custom_fields` | [`CustomFields`](#customfields) | — |
| `files` | array of `string` | List of file Guids |

#### `CustomerPost`

This is a customer in Skynamo, used for adding a customer (All values not specified will assume their default values)

| Field | Type | Description |
|---|---|---|
| `code` | `string` | The unique code associated with this customer (automatically generated if not supplied) |
| `name`* | `string` | The name of the customer |
| `active` | `boolean` | Whether or not the customer is active |
| `location` | [`Location`](#location) | — |
| `default_discount` | `number` (float) | The default discount applied for this customer when creating orders |
| `price_list_id` | `integer` | The unique identifier of the price list associated with this customer (Alternative to priceListName) |
| `price_list_name` | `string` | The name of the price list associated with this customer (Alternative to priceListID - ignored if priceListID is specified) |
| `assigned_users` | [`CustomerAssignedUsers`](#customerassignedusers) | — |
| `default_warehouse_id` | `integer` | The unique identifier of the warehouse associated with this customer |
| `custom_fields` | [`CustomFields`](#customfields) | — |

#### `CustomerPut`

This is a customer in Skynamo, used for updating information about a customer (All values not specified will assume their default values)

| Field | Type | Description |
|---|---|---|
| `id`* | `integer` | The unique id of the customer |
| `code` | `string` | The unique code associated with this customer |
| `name` | `string` | The name of the customer |
| `active` | `boolean` | Whether or not the customer is active |
| `location` | [`Location`](#location) | — |
| `default_discount` | `number` (float) | The default discount applied for this customer when creating orders |
| `price_list_id` | `integer` | The unique identifier of the price list associated with this customer (Alternative to priceListName) |
| `price_list_name` | `string` | The name of the price list associated with this customer (Alternative to priceListID - ignored if priceListID is specified) |
| `assigned_users` | [`CustomerAssignedUsers`](#customerassignedusers) | — |
| `default_warehouse_id` | `integer` | The unique identifier of the warehouse associated with this customer |
| `custom_fields` | [`CustomFields`](#customfields) | — |
| `transaction_id` | `integer` | The transaction id associated with files in order to link files to a customer |
| `files` | array of `string` | List of file Guids |

#### `CustomerPatch`

This is a customer in Skynamo, used for updating information about a customer (Only values specified will be updated)

| Field | Type | Description |
|---|---|---|
| `id`* | `integer` | The unique id of the customer |
| `code` | `string` | The unique code associated with this customer (required if you do not specify id) |
| `name` | `string` | The name of the customer |
| `active` | `boolean` | Whether or not the customer is active |
| `location` | [`Location`](#location) | — |
| `default_discount` | `number` (float) | The default discount applied for this customer when creating orders |
| `price_list_id` | `integer` | The unique identifier of the price list associated with this customer (Alternative to priceListName) |
| `price_list_name` | `string` | The name of the price list associated with this customer (Alternative to priceListID - ignored if priceListID is specified) |
| `assigned_users` | [`CustomerAssignedUsers`](#customerassignedusers) | — |
| `default_warehouse_id` | `integer` | The unique identifier of the warehouse associated with this customer |
| `custom_fields` | [`CustomFields`](#customfields) | — |
| `transaction_id` | `integer` | The transaction id associated with files in order to link files to a customer |
| `files` | array of `string` | List of file Guids |

#### `Location`

| Field | Type | Description |
|---|---|---|
| `latitude` | `number` (double) | The latitude of the customer |
| `longitude` | `number` (double) | The longitude of the customer |
| `accuracy` | `number` (double) | Accuracy of location |
| `is_approximate` | `boolean` | This property is used by reports to determine whether a visit at a customer is on-site or off-site. When false, the report will assume the location (including accuracy) is accurate enough to use for on-site/off-site calculations. |

#### `CustomerAssignedUsers`

List of user ids that are assigned to this customer

Type: array of `integer`. No named properties in the spec.

#### `CustomerComment`

This is a customer comment in Skynamo, used for fetching information about customer comments

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique id of the customer comment interaction |
| `comment` | `string` | The comment |
| `customer_id` | `integer` | The unique identifier of the customer where the comment has been logged |
| `customer_code` | `string` | The unique code of the customer where the comment has been logged |
| `date` | `string` (date-time) | The date when the customer comment interaction was logged at the customer |
| `row_version` | `number` (long) | An automatically generated, unique number used to version-stamp table rows in the database |
| `last_modified_time` | `string` (date-time) | The last time the customer comment was modified |
| `user_id` | `integer` | The unique identifier of the user that logged the customer comment |
| `user_name` | `string` | The user name of the the user that logged the customer comment |

#### `CustomerCommentPost`

This is a customer comment in Skynamo, used for adding a customer comment

| Field | Type | Description |
|---|---|---|
| `comment`* | `string` | The comment |
| `customer_id`* | `integer` | The unique identifier of the customer where the comment is to be logged (required if customer_code is not provided) |
| `customer_code`* | `string` | The unique code of the customer where the comment is to be logged (required if customer_id is not provided) |
| `date` | `string` (date-time) | The date when the customer comment interaction is to be logged at the customer (defaults to the current date if not specified) |

### Contacts

#### `Contact`

This is a contact in Skynamo, used for fetching information about contacts

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | A unique ID assigned to this contact |
| `name` | `string` | The contact's first name(s) |
| `surname` | `string` | The contact's surname |
| `customer_id` | `integer` | A unique ID assigned to the customer this contact belongs to |
| `customer_code` | `string` | The customer code of the customer this contact belongs to |
| `customer_name` | `string` | The name of the customer this contact belongs to |
| `title` | `string` | The abbreviated title of the contact, e.g. Mr, Mrs, Prof, Dr |
| `company` | `string` | The company name of the customer this contact belongs to |
| `website` | `string` | The URL of the contact's website |
| `work_number` | `string` | The contact's work phone number |
| `mobile_number` | `string` | The contact's mobile phone number |
| `email` | `string` | The contact's email address |
| `job_title` | `string` | The contact's job title or role at the company |
| `notes` | `string` | Additional notes that have been added to this contact |
| `row_version` | `number` (long) | An automatically generated, unique number used to version-stamp table rows in the database |
| `last_modified_time` | `string` (date-time) | The last date that this contact was modified |
| `create_date` | `string` (date-time) | The date on which this contact was created |
| `active` | `boolean` | A boolean that indicates whether this contact is active (TRUE) or inactive (FALSE) |
| `address` | `string` | The contact's address. Newline-separated values are supported. |

#### `ContactPost`

This is a contact in Skynamo, used for adding a contact

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | A unique ID assigned to this contact |
| `name`* | `string` | The contact's first name(s) |
| `surname` | `string` | The contact's surname |
| `customer_code`* | `string` | The customer code of the customer this contact belongs to |
| `title` | `string` | The abbreviated title of the contact, e.g. Mr, Mrs, Prof, Dr |
| `company` | `string` | The company name of the customer this contact belongs to |
| `website` | `string` | The URL of the contact's website |
| `work_number` | `string` | The contact's work phone number |
| `mobile_number` | `string` | The contact's mobile phone number |
| `email` | `string` | The contact's email address |
| `job_title` | `string` | The contact's job title or role at the company |
| `notes` | `string` | Additional notes that have been added to this contact |
| `active` | `boolean` | A boolean that indicates whether this contact is active (TRUE) or inactive (FALSE) Set to TRUE if not specified |
| `address` | `string` | The contact's address. Newline-separated values are supported. |

#### `ContactPut`

This is a contact in Skynamo, used for replacing a contact

| Field | Type | Description |
|---|---|---|
| `id`* | `integer` | A unique ID assigned to the contact |
| `name`* | `string` | The contact's first name(s) |
| `surname` | `string` | The contact's surname |
| `customer_code`* | `string` | The customer code of the customer this contact belongs to |
| `title` | `string` | The abbreviated title of the contact, e.g. Mr, Mrs, Prof, Dr |
| `company` | `string` | The company name of the customer this contact belongs to |
| `website` | `string` | The URL of the contact's website |
| `work_number` | `string` | The contact's work phone number |
| `mobile_number` | `string` | The contact's mobile phone number |
| `email` | `string` | The contact's email address |
| `job_title` | `string` | The contact's job title or role at the company |
| `notes` | `string` | Additional notes that have been added to this contact |
| `active` | `boolean` | A boolean that indicates whether this contact is active (TRUE) or inactive (FALSE) Set to TRUE if not specified |
| `address` | `string` | The contact's address. Newline-separated values are supported. |

#### `ContactPatch`

This is a contact in Skynamo, used for updating information about a contact (only values specified will be updated)

| Field | Type | Description |
|---|---|---|
| `id`* | `integer` | A unique ID assigned to the contact |
| `name` | `string` | The contact's first name(s) |
| `surname` | `string` | The contact's surname |
| `customer_code` | `string` | The customer code of the customer this contact belongs to |
| `title` | `string` | The abbreviated title of the contact, e.g. Mr, Mrs, Prof, Dr |
| `company` | `string` | The company name of the customer this contact belongs to |
| `website` | `string` | The URL of the contact's website |
| `work_number` | `string` | The contact's work phone number |
| `mobile_number` | `string` | The contact's mobile phone number |
| `email` | `string` | The contact's email address |
| `job_title` | `string` | The contact's job title or role at the company |
| `notes` | `string` | Additional notes that have been added to this contact |
| `active` | `boolean` | A boolean that indicates whether this contact is active (TRUE) or inactive (FALSE) |
| `address` | `string` | The contact's address. Newline-separated values are supported. |

### Products and order units

#### `Product`

This is a product in Skynamo, used for fetching information about products

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique id of the product |
| `row_version` | `number` (long) | A sequence number for changes to the product (if the number changes then the product has changed) |
| `code` | `string` | The unique code associated with this product |
| `name` | `string` | The name of the product |
| `active` | `boolean` | Whether or not the product is active |
| `order_units` | [`OrderUnits`](#orderunits) | — |
| `last_modified_time` | `string` (date-time) | The last time this product was modified |
| `custom_fields` | array of [`CustomField`](#customfield) | Certain custom fields may be required depending on the custom fields that have been set up |
| `files` | array of `string` | List of file Guids |

#### `ProductPost`

This is a product in Skynamo, used for adding a product (All values not specified will assume their default values)

| Field | Type | Description |
|---|---|---|
| `code` | `string` | The unique code associated with this product (automatically generated if not supplied) |
| `name`* | `string` | The name of the product |
| `active` | `boolean` | Whether or not the product is active |
| `order_units` | array of [`OrderUnit`](#orderunit) | List of all the order units associated with a product |
| `custom_fields` | array of [`CustomField`](#customfield) | Certain custom fields may be required depending on the custom fields that have been set up |

#### `ProductPut`

This is a product in Skynamo, used for updating information about a product (All values not specified will assume their default values)

| Field | Type | Description |
|---|---|---|
| `id`* | `integer` | The unique id of the product |
| `code` | `string` | The unique code associated with this product |
| `name` | `string` | The name of the product |
| `active` | `boolean` | Whether or not the product is active |
| `order_units` | array of [`OrderUnit`](#orderunit) | List of all the order units associated with a product |
| `custom_fields` | array of [`CustomField`](#customfield) | Certain custom fields may be required depending on the custom fields that have been set up |
| `transaction_id` | `integer` | The transaction id associated with files in order to link files to a product |
| `files` | array of `string` | List of file Guids |

#### `ProductPatch`

This is a product in Skynamo, used for updating information about a product (Only values specified will be updated)

| Field | Type | Description |
|---|---|---|
| `id`* | `integer` | The unique id of the product |
| `code` | `string` | The unique code associated with this product (required if you do not specify id) |
| `name` | `string` | The name of the product |
| `active` | `boolean` | Whether or not the product is active |
| `order_units` | array of [`OrderUnit`](#orderunit) | List of all the order units associated with a product |
| `custom_fields` | array of [`CustomField`](#customfield) | Certain custom fields may be required depending on the custom fields that have been set up |
| `transaction_id` | `integer` | The transaction id associated with files in order to link files to a product |
| `files` | array of `string` | List of file Guids |

#### `OrderUnits`

List of all the order units associated with a product

Type: array of [`OrderUnit`](#orderunit). No named properties in the spec.

#### `OrderUnit`

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique id of the order unit |
| `name` | `string` | The name of the order unit |
| `multiplier` | `number` (float) | The multiplier of the order unit |
| `active` | `boolean` | Indicates whether the order unit is active or not |
| `minimum_order_quantity` | `integer` | The minimum quantity of the order unit on an order |
| `packaging_option` | `object` | The packaging options of the order unit |

### Pricing, tax and currency

#### `PriceList`

This is a price list in Skynamo, used for fetching information about price lists

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique identifier of the price list |
| `name` | `string` | The name of the price list |
| `active` | `boolean` | Indicates whether the price list is active or not |
| `prices_include_vat` | `boolean` | Indicates whether the price list is vat inclusive or not |
| `last_modified_time` | `string` (date-time) | The last time the price list was modified |
| `currency_code` | `string` | The unique code associated with this currency |
| `currency_rate_to_base` | `number` (double) | The currency rate to the base rate currency |

#### `PriceListPost`

This is a price list in Skynamo, used for creating a price list.

| Field | Type | Description |
|---|---|---|
| `name` | `string` | The name of the price list |
| `active` | `boolean` | Indicates whether the price list is active or not |
| `prices_include_vat` | `boolean` | Indicates whether the price list is vat inclusive or not |
| `currency_code` | `string` | The unique code associated with this currency |

#### `PriceListPut`

This is a price list in Skynamo, used for replacing information of a price list.

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique identifier of the price list |
| `name` | `string` | The name of the price list |
| `active` | `boolean` | Indicates whether the price list is active or not |
| `prices_include_vat` | `boolean` | Indicates whether the price list is vat inclusive or not |
| `currency_code` | `string` | The unique code associated with this currency |

#### `PriceListPatch`

This is a price list in Skynamo, used for updating information creating a price list.

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique identifier of the price list |
| `name` | `string` | The name of the price list |
| `active` | `boolean` | Indicates whether the price list is active or not |
| `prices_include_vat` | `boolean` | Indicates whether the price list is vat inclusive or not |
| `currency_code` | `string` | The unique code associated with this currency |

#### `OrderUnitPrice`

This is a price of an order unit in Skynamo, used for fetching information about prices on products

| Field | Type | Description |
|---|---|---|
| `price` | `number` (float) | The price of the order unit |
| `price_list_id` | `integer` | The unique identifier of the price list associated with this order unit price |
| `price_list_name` | `string` | The name of the price list associated with this order unit price |
| `product_id` | `integer` | The unique identifier of the product associated with this order unit price |
| `product_code` | `string` | The code of the product associated with the stock level |
| `product_name` | `string` | The name of the product associated with this order unit price |
| `order_unit_id` | `integer` | The unique identifier of the order unit associated with this order unit price |
| `order_unit_name` | `string` | The name of the order unit associated with this order unit price |
| `last_modified_time` | `string` (date-time) | The last time the order unit associated with this order unit price was modified |
| `tax_rate_id` | `integer` | The unique identifier of the tax rate associated with this order unit price |

#### `OrderUnitPricePost`

This is a price of an order unit in Skynamo, used for creating and updating information about prices on products

| Field | Type | Description |
|---|---|---|
| `price` | `number` (float) | The price of the order unit |
| `price_list_id` | `integer` | The unique identifier of the price list associated with this order unit price |
| `price_list_name` | `string` | The name of the price list associated with this order unit price |
| `product_id` | `integer` | The unique identifier of the product associated with this order unit price |
| `product_code` | `string` | The code of the product associated with the stock level |
| `order_unit_id` | `integer` | The unique identifier of the order unit associated with this order unit price |
| `order_unit_name` | `string` | The name of the order unit associated with this order unit price |
| `last_modified_time` | `string` (date-time) | The last time the order unit associated with this order unit price was modified |
| `tax_rate_id` | `integer` | The unique identifier of the tax rate associated with this order unit price |

#### `TaxRate`

This is a tax rate in Skynamo, used for fetching information about tax rates

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique id of the tax rate |
| `name` | `string` | The name of the tax rate |
| `rate` | `number` (float) | The rate of the tax rate |
| `active` | `boolean` | Indicates whether the tax rate is active or not |
| `last_modified_time` | `string` (date-time) | The last time the tax rate was modified |

#### `TaxRatePost`

This is a tax rate in Skynamo, used for creating tax rates.

| Field | Type | Description |
|---|---|---|
| `name` | `string` | The name of the tax rate |
| `rate` | `number` (float) | The rate of the tax rate |
| `active` | `boolean` | Indicates whether the tax rate is active or not |

#### `TaxRatePut`

This is a tax rate in Skynamo, used for updating information about tax rates.

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique id of the tax rate |
| `name` | `string` | The name of the tax rate |
| `rate` | `number` (float) | The rate of the tax rate |
| `active` | `boolean` | Indicates whether the tax rate is active or not |

#### `TaxRatePatch`

This is a tax rate in Skynamo, used for updating information about tax rates.

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique id of the tax rate |
| `name` | `string` | The name of the tax rate |
| `rate` | `number` (float) | The rate of the tax rate |
| `active` | `boolean` | Indicates whether the tax rate is active or not |

#### `Currency`

This is a currency in Skynamo, used for fetching information about currency rates

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique id of the currency |
| `code` | `string` | The unique code associated with this currency |
| `rate_to_base` | `number` (double) | The rate to base rate of the currency |

#### `CurrencyBase`

This is a contact in Skynamo, used for updating information about a contact (only values specified will be updated)

| Field | Type | Description |
|---|---|---|
| `code` | `string` | A unique code of the base currency |
| `is_multi_currency_enabled` | `boolean` | Indicates if multi currency is enabled |
| `symbol` | `string` | The symbol of the base currency |
| `decimal_separator` | `string` | The decimal separator used |

#### `CurrencyPost`

This is a currency in Skynamo, used for adding a currency

| Field | Type | Description |
|---|---|---|
| `code`* | `string` | The unique code associated with this currency |
| `rate_to_base`* | `number` (double) | The rate to base rate of the currency |

#### `CurrencyPut`

This is a currency in Skynamo, used for updating information about a currency

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique id of the currency |
| `code`* | `string` | The unique code associated with this currency |
| `rate_to_base`* | `number` (double) | The rate to base rate of the currency |

#### `CurrencyPatch`

This is a currency in Skynamo, used for updating information about a currency

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique id of the currency |
| `code`* | `string` | The unique code associated with this currency |
| `rate_to_base`* | `number` (double) | The rate to base rate of the currency |

### Stock and warehouses

#### `StockLevel`

This is a stock level in Skynamo, used for fetching stock level information about products

| Field | Type | Description |
|---|---|---|
| `product_id` | `integer` | The unique identifier of the product associated with the stock level |
| `product_code` | `string` | The code of the product associated with the stock level |
| `product_name` | `string` | The name of the product associated with the stock level |
| `order_unit_id` | `integer` | The unique identifier of the order unit |
| `order_unit_name` | `string` | The name of the order unit |
| `warehouse_id` | `integer` | The unique identifier of the warehouse associated with the stock level |
| `warehouse_name` | `string` | The name of the warehouse associated with the stock level |
| `level` | `number` (float) | The amount of stock in the warehouse |
| `label` | `string` | The string categorizing the amount of stock in the warehouse |
| `last_modified_time` | `string` (date-time) | The last time this stock level was modified |

#### `StockLevelPost`

This is a stock level in Skynamo, used for updating information about a stock level. (If label or level is empty the stocklevel will be deleted

| Field | Type | Description |
|---|---|---|
| `product_id` | `integer` | The product id associated with this stocklevel |
| `product_code` | `string` | The product code associated with this stocklevel (required if you do not specify product_id) |
| `order_unit_id` | `integer` | The order unit id associated with this stocklevel |
| `order_unit_name` | `string` | The order unit name associated with this stocklevel (required if you do not specify order_unit_id) |
| `warehouse_id` | `integer` | The warehouse id associated with this stocklevel |
| `warehouse_name` | `string` | The warehouse name associated with this stocklevel (required if you do not specify warehouse_id) |
| `level` | `number` (float) | The quantity value of this stocklevel |
| `label` | `string` | The quantity label of this stocklevel (should match a label on insights) |

#### `Warehouse`

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique id of the warehouse |
| `name` | `string` | The name of the warehouse |
| `order_email` | `string` | The email recipients when warehouse selected |
| `credit_request_email` | `string` | The email recipients when warehouse selected |
| `quote_email` | `string` | The email recipients when warehouse selected |
| `active` | `boolean` | Indicates whether the warehouse is active or not |
| `last_modified_time` | `string` (date-time) | The last time the warehouse was modified |

#### `WarehousePost`

This is a warehouse in Skynamo, used for creating a warehouse.

| Field | Type | Description |
|---|---|---|
| `name` | `string` | The name of the warehouse |
| `order_email` | `string` | The email recipients when warehouse selected |
| `credit_request_email` | `string` | The email recipients when warehouse selected |
| `quote_email` | `string` | The email recipients when warehouse selected |
| `active` | `boolean` | Indicates whether the warehouse is active or not |

#### `WarehousePut`

This is a warehouse in Skynamo, used for replacing information of a warehouse.

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique id of the warehouse |
| `name` | `string` | The name of the warehouse |
| `order_email` | `string` | The email recipients when warehouse selected |
| `credit_request_email` | `string` | The email recipients when warehouse selected |
| `quote_email` | `string` | The email recipients when warehouse selected |
| `active` | `boolean` | Indicates whether the warehouse is active or not |

#### `WarehousePatch`

This is a warehouse in Skynamo, used for updating information creating a warehouse.

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique id of the warehouse |
| `name` | `string` | The name of the warehouse |
| `order_email` | `string` | The email recipients when warehouse selected |
| `credit_request_email` | `string` | The email recipients when warehouse selected |
| `quote_email` | `string` | The email recipients when warehouse selected |
| `active` | `boolean` | Indicates whether the warehouse is active or not |

### Deals and allocations

#### `DealGroup`

This is a deal group in Skynamo, used for fetching information about deal groups

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique id of the deal group |
| `group_name` | `string` | The name of the deal group |
| `order_price_editable` | `boolean` | A boolean that indicates whether the deal prices are editable when an order, quote or credit request is placed |
| `last_modified_time` | `string` (date-time) | The last time the deal group was modified |
| `currency_code` | `string` | The unique code associated with this currency |
| `deals` | array of [`DealGroupItem`](#dealgroupitem) | A list of deals included in the deal group |

#### `DealGroupItem`

This is a deal in Skynamo, used for fetching information about deals

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique id of the deal |
| `product_id` | `integer` | The product id associated with this deal |
| `product_code` | `string` | The product code associated with this deal |
| `order_unit_id` | `integer` | The order unit id associated with this deal |
| `order_unit_name` | `string` | The order unit name associated with this deal |
| `effective_date` | `string` (date-time) | The time at which this deal takes effect |
| `expiry_date` | `string` (date-time) | The time at which this deal expires |
| `buy_free_quantity` | `number` (float) | The quantity needed for this buy-free deal to take effect |
| `buy_free_units` | `number` (float) | The free units associated with this buy-free deal |
| `price_bracket_quantity` | `number` (float) | The quantity needed for this price bracket deal to take effect |
| `price_bracket_price` | `number` (float) | The price associated with this price bracket deal |
| `modified_by` | `string` | The user who last modified this deal |

#### `DealGroupPost`

Request body for creating a new deal group

| Field | Type | Description |
|---|---|---|
| `group_name`* | `string` | The name of the deal group |
| `order_price_editable`* | `boolean` | A boolean that indicates whether the deal prices are editable when an order, quote or credit request is placed |
| `currency_code` | `string` | The currency code for the deal group. If not provided, the base currency is used. |
| `deals`* | array of [`DealGroupDealDetail`](#dealgroupdealdetail) | A list of deal details to include in the deal group |

#### `DealGroupPut`

Request body for updating an existing deal group. Either id or lookup_group_name must be provided to identify the deal group. new_group_name is optional and renames the deal group when provided.

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique id of the deal group to update |
| `lookup_group_name` | `string` | The name of the existing deal group to update |
| `new_group_name` | `string` | The new name to assign to the deal group |
| `order_price_editable`* | `boolean` | A boolean that indicates whether the deal prices are editable when an order, quote or credit request is placed |
| `currency_code`* | `string` | The currency code for the deal group |
| `deals`* | array of [`DealGroupDealDetail`](#dealgroupdealdetail) | A list of deal details to add or update in the deal group. PUT matches an existing deal by id first; otherwise the server uses the supplied deal identifiers to locate the existing deal before replacing the remaining fields. Existing deals not included in the request remain unchanged. |

#### `DealGroupResponse`

The deal group data returned after a create or update operation

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique id of the deal group |
| `group_name` | `string` | The name of the deal group |
| `order_price_editable` | `boolean` | A boolean that indicates whether the deal prices are editable when an order, quote or credit request is placed |
| `currency_code` | `string` | The unique code associated with this currency |
| `row_version` | `string` | The table row version-stamp number of the entity |
| `deals` | array of [`DealGroupItem`](#dealgroupitem) | A list of deals included in the deal group |

#### `DealGroupDealDetail`

A deal detail within a deal group request. Provide either product_id or product_code to identify the product, and either order_unit_id or order_unit_name to identify the order unit.

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique id of an existing deal detail. Used in PUT operations to identify existing deals to update. |
| `product_id` | `integer` | The product id associated with this deal |
| `product_code` | `string` | The product code associated with this deal |
| `order_unit_id` | `integer` | The order unit id associated with this deal |
| `order_unit_name` | `string` | The order unit name associated with this deal |
| `effective_date`* | `string` (date-time) | The time at which this deal takes effect |
| `expiry_date`* | `string` (date-time) | The time at which this deal expires |
| `buy_free_quantity` | `number` (float) | The quantity needed for this buy-free deal to take effect |
| `buy_free_units` | `number` (float) | The free units associated with this buy-free deal |
| `price_bracket_quantity` | `number` (float) | The quantity needed for this price bracket deal to take effect |
| `price_bracket_price` | `number` (float) | The price associated with this price bracket deal |

#### `CustomerDealGroupAllocation`

A customer with its allocated deal groups

| Field | Type | Description |
|---|---|---|
| `customer_id` | `integer` | The unique id of the customer |
| `customer_code` | `string` | The unique code of the customer |
| `customer_name` | `string` | The name of the customer |
| `deal_groups` | array of `object` | The list of deal groups allocated to this customer |

#### `CustomerDealGroupAllocationPost`

Request body for setting deal group allocations on a customer. Provide either id or code to identify the customer.

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique id of the customer |
| `code` | `string` | The unique code of the customer |
| `deal_groups` | array of `object` | The list of deal groups to allocate to this customer. This replaces any existing allocations. The array may be null or empty (blank) to clear all allocations and is not treated as an error. |

#### `DealGroupCustomerAllocation`

A deal group with its allocated customers

| Field | Type | Description |
|---|---|---|
| `deal_group_id` | `integer` | The unique id of the deal group |
| `deal_group_name` | `string` | The name of the deal group |
| `customers` | array of `object` | The list of customers allocated to this deal group |

#### `DealGroupCustomerAllocationPost`

Request body for setting customer allocations on a deal group. Provide either deal_group_id or deal_group_name to identify the deal group.

| Field | Type | Description |
|---|---|---|
| `deal_group_id` | `integer` | The unique id of the deal group |
| `deal_group_name` | `string` | The name of the deal group |
| `customers`* | array of `object` | The list of customers to allocate to this deal group. This replaces any existing allocations. |

### Orders

#### `Order`

This is an order in Skynamo, used for fetching information about orders

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique id of the order |
| `date` | `string` (date-time) | The date when the order was issued |
| `customer_id` | `integer` | The unique id of the customer that placed the order |
| `customer_code` | `string` | The unique code of the customer that placed the order |
| `customer_name` | `string` | The name of the customer that placed the order |
| `reference` | `string` | The reference used to identify the order by a human or external system |
| `interaction_id` | `integer` | The unique id of the interaction of the order |
| `discount` | `number` (float) | The discount percentage on the order |
| `discount_amount` | `number` (float) | The discount amount on the order |
| `total_amount` | `number` (float) | The total amount on the order |
| `prices_include_vat` | `boolean` | Indicates whether the price is vat inclusive or not |
| `warehouse_id` | `integer` | The unique identifier of the warehouse associated with the stock level |
| `warehouse_name` | `string` | The name of the warehouse associated with the stock level |
| `email_recipients` | `string` | The email recipients on the order |
| `last_modified_time` | `string` (date-time) | The last time this order was modified |
| `currency_code` | `string` | The unique code associated with this currency |
| `currency_rate_to_base` | `number` (double) | The currency rate to the base rate currency |
| `items` | array of [`OrderItem`](#orderitem) | A list of items included in the order |

#### `OrderItem`

| Field | Type | Description |
|---|---|---|
| `quantity` | `number` (float) | The quantity of the product that has been included in the order |
| `unit_price` | `number` (float) | The unit price of the product included in the order |
| `list_price` | `number` (float) | The list price of the product included in the order |
| `order_unit_name` | `string` | The order unit name |
| `product_code` | `string` | The unique code of the product that has been included in the order |
| `product_name` | `string` | The name of the product that has been included in the order |
| `comment` | `string` | The comment applying to this item |
| `cost` | `number` (float) | The total cost of the order item |
| `tax_rate_value` | `number` (float) | The tax rate value used when the order item was created |
| `tax_rate_id` | `integer` | The unique identifier of the tax rate associated with this order item |

#### `OrderPost`

This is a order in Skynamo, used for adding an order (All values not specified will assume their default values)

| Field | Type | Description |
|---|---|---|
| `date`* | `string` (date-time) | The date when the order was issued |
| `customer_id`* | `integer` | The unique id of the customer that placed the order |
| `user_id`* | `integer` | The unique id of the user that placed the order |
| `discount` | `number` (float) | The discount percentage on the order |
| `quote_id` | `integer` | The unique id of the quote associated with the order |
| `prices_include_vat` | `boolean` | Indicates whether the price is vat inclusive or not |
| `warehouse_id` | `integer` | The unique identifier of the warehouse associated with the stock level |
| `transaction_id` | `integer` | The transaction id associated with files in order to link files |
| `currency_code` | `string` | The unique code associated with this currency |
| `currency_rate_to_base` | `number` (double) | The currency rate to the base rate currency |
| `items`* | array of [`OrderItemPost`](#orderitempost) | A list of items included in the order |
| `forms` | array of [`OrderForms`](#orderforms) | Certain custom fields may be required depending on the custom fields that have been set up |

#### `OrderItemPost`

| Field | Type | Description |
|---|---|---|
| `product_id` | `integer` | The unique id of the product that has been included in the order |
| `product_code` | `string` | The unique code of the product that has been included in the order |
| `unit_name` | `string` | The item unit name |
| `comment` | `string` | The comment applying to this item |
| `multiplier` | `number` (float) | The item multiplier |
| `quantity` | `number` (float) | The quantity of the product that has been included in the order |
| `price` | `number` (float) | The total price of the product included in the order |
| `list_price` | `number` (float) | The price list price of the product included in the order |
| `unit_price` | `number` (float) | The unit price of the product included in the order |
| `tax_rate_id` | `integer` | The unique identifier of the tax rate associated with this order item |
| `tax_rate_value` | `number` (float) | The value of the tax rate associated with this order item |

#### `OrderForms`

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique id of the form that has been included in the order |
| `custom_fields` | array of [`OrderFormCustomFields`](#orderformcustomfields) | A list of customfields included in the form |

#### `OrderFormCustomFields`

This is a customfield in Skynamo, used for adding customfields on a form for an order (All values not specified will assume their default values)

| Field | Type | Description |
|---|---|---|
| `id`* | `integer` | The unique identifier of the custom field |
| `value` | `string` | The value of the custom field |

### Quotes

#### `Quote`

This is a quote in Skynamo, used for fetching information about quotes

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique id of the quote |
| `date` | `string` (date-time) | The date when the quote was issued |
| `customer_id` | `integer` | The unique id of the customer that placed the quote |
| `customer_code` | `string` | The unique code of the customer that placed the quote |
| `customer_name` | `string` | The name of the customer that placed the quote |
| `reference` | `string` | The reference used to identify the quote by a human or external system |
| `discount` | `number` (float) | The discount percentage on the quote |
| `discount_amount` | `number` (float) | The discount amount on the quote |
| `total_amount` | `number` (float) | The total amount on the quote |
| `prices_include_vat` | `boolean` | Indicates whether the price is vat inclusive or not |
| `warehouse_id` | `integer` | The unique identifier of the warehouse associated with the quote |
| `warehouse_name` | `string` | The name of the warehouse associated with the quote |
| `email_recipients` | `string` | The email recipients on the quote |
| `last_modified_time` | `string` (date-time) | The last time this quote was modified |
| `currency_code` | `string` | The unique code associated with this currency |
| `currency_rate_to_base` | `number` (double) | The currency rate to the base rate currency |
| `items` | array of [`QuoteItem`](#quoteitem) | A list of items included in the quote |

#### `QuoteItem`

| Field | Type | Description |
|---|---|---|
| `quantity` | `number` (float) | The quantity of the product that has been included in the quote |
| `unit_price` | `number` (float) | The unit price of the product included in the quote |
| `comment` | `string` | The comment applying to this item |
| `list_price` | `number` (float) | The list price of the product included in the quote |
| `order_unit_name` | `string` | The order unit name |
| `product_code` | `string` | The unique code of the product that has been included in the quote |
| `product_name` | `string` | The name of the product that has been included in the quote |
| `cost` | `number` (float) | The total cost of the order item |
| `tax_rate_value` | `number` (float) | The tax rate value used when the quote item was created |
| `tax_rate_id` | `integer` | The unique identifier of the tax rate associated with this quote item |

#### `QuotePost`

This is a quote in Skynamo, used for adding a quote (All values not specified will assume their default values)

| Field | Type | Description |
|---|---|---|
| `date`* | `string` (date-time) | The date when the quote was issued |
| `customer_id`* | `integer` | The unique id of the customer that placed the quote |
| `user_id`* | `integer` | The unique id of the user that placed the quote |
| `discount` | `number` (float) | The discount percentage on the quote |
| `prices_include_vat` | `boolean` | Indicates whether the price is vat inclusive or not |
| `warehouse_id` | `integer` | The unique identifier of the warehouse associated with the stock level |
| `transaction_id` | `integer` | The transaction id associated with files in order to link files |
| `currency_code` | `string` | The unique code associated with this currency |
| `currency_rate_to_base` | `number` (double) | The currency rate to the base rate currency |
| `items`* | array of [`QuoteItemPost`](#quoteitempost) | A list of items included in the quote |
| `forms` | array of [`QuoteForms`](#quoteforms) | Certain custom fields may be required depending on the custom fields that have been set up |

#### `QuoteItemPost`

| Field | Type | Description |
|---|---|---|
| `product_id` | `integer` | The unique id of the product that has been included in the quote |
| `product_code` | `string` | The unique code of the product that has been included in the quote |
| `unit_name` | `string` | The item unit name |
| `comment` | `string` | The comment applying to this item |
| `multiplier` | `number` (float) | The item multiplier |
| `quantity` | `number` (float) | The quantity of the product that has been included in the quote |
| `price` | `number` (float) | The total price of the product included in the quote |
| `list_price` | `number` (float) | The price list price of the product included in the quote |
| `unit_price` | `number` (float) | The unit price of the product included in the quote |
| `tax_rate_id` | `integer` | The unique identifier of the tax rate associated with the item |
| `tax_rate_value` | `number` (float) | The value of the tax rate associated with the item |

#### `QuoteForms`

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique id of the form that has been included in the quote |
| `custom_fields` | array of [`QuoteFormCustomFields`](#quoteformcustomfields) | A list of customfields included in the form |

#### `QuoteFormCustomFields`

This is a customfield in Skynamo, used for adding customfields on a form for a quote (All values not specified will assume their default values)

| Field | Type | Description |
|---|---|---|
| `id`* | `integer` | The unique identifier of the custom field |
| `value` | `string` | The value of the custom field |

### Credit requests

#### `CreditRequest`

This is a credit request in Skynamo, used for fetching information about credit requests

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique id of the credit request |
| `date` | `string` (date-time) | The date when the credit request was issued |
| `customer_id` | `integer` | The unique id of the customer that placed the credit request |
| `customer_code` | `string` | The unique code of the customer that placed the credit request |
| `customer_name` | `string` | The name of the customer that placed the credit request |
| `reference` | `string` | The reference used to identify the credit request by a human or external system |
| `discount` | `number` (float) | The discount percentage on the credit request |
| `discount_amount` | `number` (float) | The discount amount on the credit request |
| `total_amount` | `number` (float) | The total amount on the credit request |
| `prices_include_vat` | `boolean` | Indicates whether the price is vat inclusive or not |
| `warehouse_id` | `integer` | The unique identifier of the warehouse associated with the credit request |
| `warehouse_name` | `string` | The name of the warehouse associated with the credit request |
| `email_recipients` | `string` | The email recipients on the credit request |
| `last_modified_time` | `string` (date-time) | The last time this credit request was modified |
| `currency_code` | `string` | The unique code associated with this currency |
| `currency_rate_to_base` | `number` (double) | The currency rate to the base rate currency |
| `items` | array of [`CreditRequestItem`](#creditrequestitem) | A list of items included in the credit request |

#### `CreditRequestItem`

| Field | Type | Description |
|---|---|---|
| `quantity` | `number` (float) | The quantity of the product that has been included in the credit request |
| `unit_price` | `number` (float) | The unit price of the product included in the credit request |
| `comment` | `string` | The comment applying to this item |
| `list_price` | `number` (float) | The list price of the product included in the credit request |
| `order_unit_name` | `string` | The order unit name |
| `product_code` | `string` | The unique code of the product that has been included in the credit request |
| `product_name` | `string` | The name of the product that has been included in the credit request |
| `cost` | `number` (float) | The total cost of the credit request item |
| `tax_rate_value` | `number` (float) | The tax rate value used when the credit request item was created |
| `tax_rate_id` | `integer` | The unique identifier of the tax rate associated with this credit request item |

#### `CreditRequestPost`

This is a credit request in Skynamo, used for adding a credit request (All values not specified will assume their default values)

| Field | Type | Description |
|---|---|---|
| `date`* | `string` (date-time) | The date when the credit request was issued |
| `customer_id`* | `integer` | The unique id of the customer that placed the credit request |
| `user_id`* | `integer` | The unique id of the user that placed the credit request |
| `discount` | `number` (float) | The discount percentage on the credit request |
| `prices_include_vat` | `boolean` | Indicates whether the price is vat inclusive or not |
| `warehouse_id` | `integer` | The unique identifier of the warehouse associated with the stock level |
| `transaction_id` | `integer` | The transaction id associated with files in order to link files |
| `currency_code` | `string` | The unique code associated with this currency |
| `currency_rate_to_base` | `number` (double) | The currency rate to the base rate currency |
| `items`* | array of [`CreditRequestItemPost`](#creditrequestitempost) | A list of items included in the credit request |
| `forms` | array of [`CreditRequestForms`](#creditrequestforms) | Certain custom fields may be required depending on the custom fields that have been set up |

#### `CreditRequestItemPost`

| Field | Type | Description |
|---|---|---|
| `product_id` | `integer` | The unique id of the product that has been included in the credit request |
| `product_code` | `string` | The unique code of the product that has been included in the credit request |
| `unit_name` | `string` | The item unit name |
| `comment` | `string` | The comment applying to this item |
| `multiplier` | `number` (float) | The item multiplier |
| `quantity` | `number` (float) | The quantity of the product that has been included in the credit request |
| `price` | `number` (float) | The total price of the product included in the credit request |
| `list_price` | `number` (float) | The price list price of the product included in the credit request |
| `unit_price` | `number` (float) | The unit price of the product included in the credit request |
| `tax_rate_id` | `integer` | The unique identifier of the tax rate associated with the item |
| `tax_rate_value` | `number` (float) | The value of the tax rate associated with the item |

#### `CreditRequestForms`

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique id of the form that has been included in the credit request |
| `custom_fields` | array of [`CreditRequestFormCustomFields`](#creditrequestformcustomfields) | A list of customfields included in the form |

#### `CreditRequestFormCustomFields`

This is a customfield in Skynamo, used for adding customfields on a form for a credit request (All values not specified will assume their default values)

| Field | Type | Description |
|---|---|---|
| `id`* | `integer` | The unique identifier of the custom field |
| `value` | `string` | The value of the custom field |

### Invoices

#### `Invoice`

This is an invoice in Skynamo, used for fetching information about invoices

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique id of the invoice |
| `date` | `string` (date-time) | The date when the invoice was issued |
| `customer_id` | `integer` | The unique id of the customer that was invoiced |
| `customer_code` | `string` | The unique code of the customer that was invoiced |
| `reference` | `string` | The reference used to identify the invoice by a human or external system |
| `row_version` | `number` (long) | A sequence number for changes to the invoice (if the number changes then the invoice has changed) |
| `last_modified_time` | `string` (date-time) | The last time this invoice was modified |
| `status` | `string` | The status of the invoice **One of:** `Draft`, `Authorized`, `Delivered`, `Outstanding`, `Paid`, `Deleted` |
| `due_date` | `string` (date-time) | The invoice due date |
| `external_id` | `string` | The external id of the invoice |
| `tax_inclusion` | `string` | States if the invoice is tax-inclusive ot tax-exclusive **One of:** `Included`, `Excluded` |
| `tax` | `number` (double) | The total tax amount of the invoice |
| `total` | `number` (double) | The total amount of the invoice |
| `outstanding_balance` | `number` (double) | The total outstanding balance of the invoice |
| `currency_code` | `string` | The unique code associated with this currency |
| `currency_rate_to_base` | `number` (double) | The currency rate to the base rate currency |
| `items` | array of [`InvoiceItem`](#invoiceitem) | A list of items included in the invoice |

#### `InvoiceItem`

| Field | Type | Description |
|---|---|---|
| `product_id`* | `integer` | The unique id of the product - required if product_code not specified |
| `product_code` | `string` | The unique code of the produc - required if product_id not specified |
| `quantity`* | `number` (float) | The quantity of the product |
| `tax_amount` | `number` (double) | The total tax amount of the product |
| `value` | `number` (double) | The value of the product |

#### `InvoicePost`

This is an invoice in Skynamo, used for adding an invoice (All values not specified will assume their default values)

| Field | Type | Description |
|---|---|---|
| `date`* | `string` (date-time) | The date when the invoice was issued |
| `customer_id`* | `integer` | The unique id of the customer that was invoiced - required if customer_code is not specified |
| `customer_code` | `string` | The unique code of the customer that was invoiced - required if customer_id is not specified |
| `reference` | `string` | The reference used to identify the invoice by a human or external system |
| `status` | `string` | The status of the invoice **One of:** `Paid`, `NotSpecified`, `OutStanding`, `Deleted`, `Void` |
| `due_date` | `string` (date-time) | The invoice due date |
| `external_id` | `string` | The external id of the invoice |
| `tax_inclusion` | `string` | States if the invoice is tax-inclusive ot tax-exclusive **One of:** `Included`, `Excluded` |
| `tax` | `number` (double) | The total tax amount of the invoice |
| `total` | `number` (double) | The total amount of the invoice |
| `outstanding_balance` | `number` (double) | The total outstanding balance of the invoice |
| `currency_code` | `string` | The unique code associated with this currency |
| `currency_rate_to_base` | `number` (double) | The currency rate to the base rate currency |
| `items` | array of [`InvoiceItem`](#invoiceitem) | A list of items included in the invoice |

#### `InvoicePut`

This is an invoice in Skynamo, used for updating an invoice (All values not specified will assume their default values)

| Field | Type | Description |
|---|---|---|
| `id`* | `integer` | The unique id of the invoice |
| `date`* | `string` (date-time) | The date when the invoice was issued |
| `customer_id`* | `integer` | The unique id of the customer that was invoiced - required if customer_code is not specified |
| `customer_code` | `string` | The unique code of the customer that was invoiced - required if customer_id is not specified |
| `reference` | `string` | The reference used to identify the invoice by a human or external system |
| `status` | `string` | The status of the invoice **One of:** `Paid`, `NotSpecified`, `OutStanding`, `Deleted`, `Void` |
| `due_date` | `string` (date-time) | The invoice due date |
| `external_id` | `string` | The external id of the invoice |
| `tax_inclusion` | `string` | States if the invoice is tax-inclusive ot tax-exclusive **One of:** `Included`, `Excluded` |
| `tax` | `number` (double) | The total tax amount of the invoice |
| `total` | `number` (double) | The total amount of the invoice |
| `outstanding_balance` | `number` (double) | The total outstanding balance of the invoice |
| `currency_code` | `string` | The unique code associated with this currency |
| `currency_rate_to_base` | `number` (double) | The currency rate to the base rate currency |
| `items` | array of [`InvoiceItem`](#invoiceitem) | A list of items included in the invoice |

#### `InvoicePatch`

This is an invoice in Skynamo, used for updating information about an invoice (Only values specified will be updated)

| Field | Type | Description |
|---|---|---|
| `id`* | `integer` | The unique id of the invoice |
| `date` | `string` (date-time) | The date when the invoice was issued |
| `customer_id` | `integer` | The unique id of the customer that was invoiced |
| `customer_code` | `string` | The unique code of the customer that was invoiced |
| `reference` | `string` | The reference used to identify the invoice by a human or external system |
| `status` | `string` | The status of the invoice **One of:** `Paid`, `NotSpecified`, `OutStanding`, `Deleted`, `Void` |
| `due_date` | `string` (date-time) | The invoice due date |
| `external_id` | `string` | The external id of the invoice |
| `tax_inclusion` | `string` | States if the invoice is tax-inclusive ot tax-exclusive **One of:** `Included`, `Excluded` |
| `tax` | `number` (double) | The total tax amount of the invoice |
| `total` | `number` (double) | The total amount of the invoice |
| `outstanding_balance` | `number` (double) | The total outstanding balance of the invoice |
| `currency_code` | `string` | The unique code associated with this currency |
| `currency_rate_to_base` | `number` (double) | The currency rate to the base rate currency |
| `items` | array of [`InvoiceItem`](#invoiceitem) | A list of items included in the invoice |

#### `InvoiceDelete`

A list of invoice IDs that should be deleted

Type: array of `integer`. No named properties in the spec.

#### `InvoiceExternalDelete`

A list of external invoice IDs that should be deleted

Type: array of `integer`. No named properties in the spec.

### Order and order-item statuses

#### `OrderStatus`

Represents an order status entry in Skynamo, providing comprehensive information about order statuses.

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | A unique ID assigned to this order status. |
| `document_id` | `integer` | The unique ID of the order associated with the order status. |
| `external_reference` | `string` | The external reference for the order associated with the order status. |
| `date` | `string` (date-time) | The date and time that the order status changed. |
| `status` | `string` | The status value of this order status entry **One of:** `Logged`, `Failed` |
| `status_reason` | `string` | A reason for changing to this order status |

#### `OrderStatusPost`

Represents an order status entry in Skynamo, used for adding an order status.

| Field | Type | Description |
|---|---|---|
| `document_id`* | `integer` | The Skynamo id of the order this status applies to. |
| `date`* | `string` (date-time) | The date and time that the order status changed. |
| `status`* | `string` | The status value of this order status. **One of:** `Logged`, `Failed` |
| `status_reason` | `string` | A reason for changing to this order status. |

#### `OrderStatusDelete`

A list of order status IDs that should be deleted

Type: array of `integer`. No named properties in the spec.

#### `OrderItemStatus`

Represents the order item status in Skynamo, providing comprehensive information about order item statuses.

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | A unique ID assigned to this order item status. |
| `customer_id` | `integer` | The unique ID of the customer associated with the order status. |
| `customer_code` | `string` | The unique code of the customer associated with the order status. |
| `order_id` | `string` | The Skynamo order ID of the order status. |
| `external_reference` | `string` | The external reference for the order status order. |
| `last_modified_time` | `string` (date-time) | The last time the completed form was modified |
| `items` | array of [`OrderStatusItem`](#orderstatusitem) | A list of items included in the status order. |

#### `OrderItemStatusPost`

Represents an order status in Skynamo, used for adding an order item status.

| Field | Type | Description |
|---|---|---|
| `customer_id` | `integer` | The unique ID of the customer associated with the order. |
| `order_id` | `string` | The Skynamo order ID of the order status. |
| `external_reference` | `string` | The external reference for the order status order. |
| `items` | array of [`OrderStatusItemPost`](#orderstatusitempost) | A list of items included in the status order. |

#### `OrderItemStatusPut`

Represents an order status in Skynamo, used for replacing an order status.

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | A unique ID assigned to the order status. |
| `customer_id` | `integer` | The unique ID of the customer associated with the order status. |
| `order_id` | `string` | The Skynamo order ID of the order status. |
| `external_reference` | `string` | The external reference for the order status order. |
| `items` | array of [`OrderStatusItemPut`](#orderstatusitemput) | A list of items included in the status order. |

#### `OrderItemStatusPatch`

Represents an order status in Skynamo, used for updating information about an order status (only specified values will be updated).

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | A unique ID assigned to this order status. |
| `status` | `string` | The status of the order status. |
| `customer_id` | `integer` | The unique ID of the customer associated with the order status. |
| `order_id` | `string` | The Skynamo order ID of the order status. |
| `external_reference` | `string` | The external reference for the order status order. |
| `items` | array of [`OrderStatusItemPatch`](#orderstatusitempatch) | A list of items included in the status order. |

#### `OrderStatusItem`

| Field | Type | Description |
|---|---|---|
| `status` | `string` | The status for the order status item. |
| `product_id` | `integer` | The product ID associated with this order status item. |
| `product_code` | `string` | The unique code of the product associated with this order status item. |
| `unit_name` | `string` | The unit name for the order status item. |
| `status_quantity` | `number` (float) | The outstanding quantity for the order status item. |
| `order_quantity` | `number` (float) | The ordered quantity of the order status item as mentioned on the Skynamo order. |
| `status_line_total` | `number` (float) | The total value of the order status item based on its status quantity. |
| `order_line_total` | `number` (float) | The total value mentioned on the Skynamo order item for this order status item. |

#### `OrderStatusItemPost`

| Field | Type | Description |
|---|---|---|
| `status` | `string` | The status for the order status item. |
| `product_id` | `integer` | The product ID associated with this order status item. |
| `unit_name` | `string` | The unit name for the order status item. |
| `status_quantity` | `number` (float) | The outstanding quantity for the order status item. |
| `order_quantity` | `number` (float) | The ordered quantity of the order status item as mentioned on the Skynamo order. |
| `status_line_total` | `number` (float) | The total value of the order status item based on its status quantity. |
| `order_line_total` | `number` (float) | The total value mentioned on the Skynamo order item for this order status item. |

#### `OrderStatusItemPut`

| Field | Type | Description |
|---|---|---|
| `status` | `string` | The status for the order status item. |
| `product_id` | `integer` | The product ID associated with this order status item. |
| `unit_name` | `string` | The unit name for the order status item. |
| `status_quantity` | `number` (float) | The outstanding quantity for the order status item. |
| `order_quantity` | `number` (float) | The ordered quantity of the order status item as mentioned on the Skynamo order. |
| `status_line_total` | `number` (float) | The total value of the order status item based on its status quantity. |
| `order_line_total` | `number` (float) | The total value mentioned on the Skynamo order item for this order status item. |

#### `OrderStatusItemPatch`

| Field | Type | Description |
|---|---|---|
| `status` | `string` | The status for the order status item. |
| `product_id` | `integer` | The product ID associated with this order status item. |
| `unit_name` | `string` | The unit name for the order status item. |
| `status_quantity` | `number` (float) | The outstanding quantity for the order status item. |
| `order_quantity` | `number` (float) | The ordered quantity of the order status item as mentioned on the Skynamo order. |
| `status_line_total` | `number` (float) | The total value of the order status item based on its status quantity. |
| `order_line_total` | `number` (float) | The total value mentioned on the Skynamo order item for this order status item. |

### Field activity

#### `Interaction`

This is an interaction in Skynamo, used for fetching information about interactions

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique ID of the interaction |
| `customer_id` | `integer` | The unique ID of the customer associated with the interaction |
| `customer_code` | `string` | The unique code of the customer associated with the interaction |
| `customer_name` | `string` | The name of the customer associated with the interaction |
| `comment` | `string` | A comment about the interaction (if applicable) |
| `date` | `string` (date-time) | The date and time when the interaction occurred |
| `end_time` | `string` (date-time) | The date and time when the interaction ended (if applicable) |
| `is_visit` | `boolean` | True if the interaction is a visit |
| `location` | [`Location`](#location) | — |
| `last_modified_time` | `string` (date-time) | The last time the interaction was modified |
| `user_id` | `integer` | The unique ID of the user that did the interaction |
| `user_name` | `string` | The user name of the user that did the interaction |
| `credit_request_id` | `integer` | The unique ID of the credit request that was placed at the interaction (if applicable) |
| `order_id` | `integer` | The unique ID of the order that was placed at the interaction (if applicable) |
| `email_id` | `integer` | The unique ID of the email that was placed at the interaction (if applicable) |
| `quote_id` | `integer` | The unique ID of the quote that was placed at the interaction (if applicable) |
| `product_numbers_survey_id` | `integer` | This property is deprecated and will be removed. Please use stocktake_id. |
| `stocktake_id` | `integer` | The unique ID of the stocktake that was completed at the interaction (if applicable) |
| `completed_form_ids` | array of `integer` | A list of the unique IDs of the completed forms that were placed at the interaction (if applicable) |

#### `EmailInteraction`

This is an email interaction in Skynamo, used for fetching information about interactions

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique ID of the email interaction |
| `date` | `string` (date-time) | The date and time when the email interaction occurred |
| `customer_id` | `integer` | The unique ID of the customer associated with the email interaction |
| `customer_code` | `string` | The unique code of the customer associated with the email interaction |
| `customer_name` | `string` | The name of the customer associated with the email interaction |
| `last_modified_time` | `string` (date-time) | The last time the email interaction was modified |
| `recipients` | `string` | The recipients of the email |
| `subject` | `string` | The subject of the email |
| `user_id` | `integer` | The unique ID of the user that did the email interaction |
| `content` | `string` | The content of the email |

#### `ScheduledVisit`

This is a scheduled visit in Skynamo, used for fetching information about scheduled visits

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique identifier of the scheduled visit |
| `comment` | `string` | A place for a user to place a comment about the scheduled visit |
| `create_date` | `string` (date-time) | The date when the scheduled visit was created |
| `due_date` | `string` (date-time) | The date when the scheduled visit is due (the time should be ignored if all_day is set to true) |
| `customer_id` | `integer` | The unique identifier of the customer where the scheduled visit should be completed |
| `customer_code` | `string` | The unique code of the customer where the scheduled visit should be completed |
| `customer_name` | `string` | The name of the customer where the scheduled visit should be completed |
| `row_version` | `number` (long) | An automatically generated, unique number used to version-stamp table rows in the database |
| `last_modified_time` | `string` (date-time) | The last time the scheduled visit was modified |
| `reminder_offset` | `string` | A timespan indicating when the reminder for this scheduled visit will be sent out (it is the duration between the reminder and the due date) |
| `completer_visit_id` | `integer` | The unique identifier of the visit that was done to complete this scheduled visit (the scheduled visit is not complete if this field is empty) |
| `completed_date` | `string` (date-time) | The date the scheduled visit was completed (the scheduled visit is not completed if this field is empty) |
| `assigned_user_id` | `integer` | The unique identifier of the user that must complete this scheduled visit |
| `assigned_user_name` | `string` | The user name of the the user that must complete this scheduled visit |
| `creator_user_id` | `integer` | The unique identifier of the user that created this scheduled visit |
| `creator_user_name` | `string` | The user name of the user that created this scheduled visit |
| `all_day` | `boolean` | True if the scheduled visit can be completed at any time on the due_date (ignore the time segment of due_date and the end_time if true) |
| `end_time` | `string` (date-time) | The end time of the scheduled visit (ignore if all_day is set to true) |

#### `ScheduledVisitPost`

This is a scheduled visit in Skynamo, used for creating scheduled visits

| Field | Type | Description |
|---|---|---|
| `comment` | `string` | A place for a user to place a comment about the scheduled visit |
| `due_date`* | `string` (date-time) | The date when the scheduled visit is due (the time segment will be ignored if all_day is set to true) |
| `customer_id`* | `integer` | The unique identifier of the customer where the scheduled visit should be completed (required if customer_code is not provided) |
| `customer_code`* | `string` | The unique code of the customer where the scheduled visit should be completed (required if customer_id is not provided) |
| `reminder_offset` | `string` | A timespan indicating when the reminder for this scheduled visit will be sent out (it is the duration between the reminder and the due date) |
| `assigned_user_id`* | `integer` | The unique identifier of the user that must complete this scheduled visit (required if assigned_user_name is not provided) |
| `assigned_user_name`* | `string` | The user name of the the user that must complete this scheduled visit (required if assigned_user_id is not provided) |
| `all_day` | `boolean` | True if the scheduled visit can be completed at any time on the due_date (automatically set to true if no end_time is provided; automatically set to false if an end_time is provided; will ignore the time segment of due_date if true) |
| `end_time` | `string` (date-time) | The end time of the scheduled visit (required if all_day is set to false; must be empty if all_day is set to true) |

#### `ScheduledVisitPut`

This is a scheduled visit in Skynamo, used for updating information about scheduled visits (all values not specified will assume their default values)

| Field | Type | Description |
|---|---|---|
| `id`* | `integer` | The unique identifier of the scheduled visit |
| `comment` | `string` | A place for a user to place a comment about the scheduled visit |
| `due_date`* | `string` (date-time) | The date when the scheduled visit is due (the time segment will be ignored if all_day is set to true) |
| `customer_id`* | `integer` | The unique identifier of the customer where the scheduled visit should be completed (required if customer_code is not provided) |
| `customer_code`* | `string` | The unique code of the customer where the scheduled visit should be completed (required if customer_id is not provided) |
| `reminder_offset` | `string` | A timespan indicating when the reminder for this scheduled visit will be sent out (it is the duration between the reminder and the due date) |
| `assigned_user_id`* | `integer` | The unique identifier of the user that must complete this scheduled visit (required if assigned_user_name is not provided) |
| `assigned_user_name`* | `string` | The user name of the the user that must complete this scheduled visit (required if assigned_user_id is not provided) |
| `all_day` | `boolean` | True if the scheduled visit can be completed at any time on the due_date (automatically set to true if no end_time is provided; automatically set to false if an end_time is provided; will ignore the time segment of due_date if true) |
| `end_time` | `string` (date-time) | The end time of the scheduled visit (required if all_day is set to false; must be empty if all_day is set to true) |

#### `ScheduledVisitPatch`

This is a scheduled visit in Skynamo, used for updating information about scheduled visits

| Field | Type | Description |
|---|---|---|
| `id`* | `integer` | The unique identifier of the scheduled visit |
| `comment` | `string` | A place for a user to place a comment about the scheduled visit |
| `due_date` | `string` (date-time) | The date when the scheduled visit is due (required if end_time is provided; the time segment will be ignored if all_day is set to true) |
| `customer_id` | `integer` | The unique identifier of the customer where the scheduled visit should be completed |
| `customer_code` | `string` | The unique code of the customer where the scheduled visit should be completed |
| `reminder_offset` | `string` | A timespan indicating when the reminder for this scheduled visit will be sent out (it is the duration between the reminder and the due date) |
| `assigned_user_id` | `integer` | The unique identifier of the user that must complete this scheduled visit |
| `assigned_user_name` | `string` | The user name of the the user that must complete this scheduled visit |
| `all_day` | `boolean` | True if the scheduled visit can be completed at any time on the due_date (automatically set to false if an end_time is provided; will ignore the time segment of due_date if true) |
| `end_time` | `string` (date-time) | The end time of the scheduled visit (must be empty if all_day is set to true) |

#### `ScheduledVisitDelete`

A list of scheduled visit IDs that should be deleted

Type: array of `integer`. No named properties in the spec.

#### `Task`

This is a task in Skynamo, used for fetching information about tasks

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique identifier of the task |
| `description` | `string` | The description of what the task entails |
| `create_date` | `string` (date-time) | The date when the task was created |
| `due_date` | `string` (date-time) | The date when the task is due (the time should be ignored if anytime is set to true) |
| `customer_id` | `integer` | The unique identifier of the customer where the task should be completed (if applicable) |
| `customer_code` | `string` | The unique code of the customer where the task should be completed (if applicable) |
| `customer_name` | `string` | The name of the customer where the task should be completed (if applicable) |
| `row_version` | `number` (long) | An automatically generated, unique number used to version-stamp table rows in the database |
| `last_modified_time` | `string` (date-time) | The last time the task was modified |
| `reminder_offset` | `string` | A timespan indicating when the reminder for this task will be sent out (it is the duration between the reminder and the due date) |
| `completed_date` | `string` (date-time) | The date the task was completed (the task is not completed if this field is empty) |
| `assigned_user_id` | `integer` | The unique identifier of the user that must complete this task |
| `assigned_user_name` | `string` | The user name of the the user that must complete this task |
| `creator_user_id` | `integer` | The unique identifier of the user that created this task |
| `creator_user_name` | `string` | The user name of the user that created this task |
| `anytime` | `boolean` | True if the task can be completed at any time on the due_date (ignore the time segment of due_date if true) |

#### `TaskPost`

This is a task in Skynamo, used for creating tasks.

| Field | Type | Description |
|---|---|---|
| `description`* | `string` | The description of what the task entails |
| `due_date`* | `string` (date-time) | The date when the task is due (the time will be ignored if anytime is set to true) |
| `customer_id` | `integer` | The unique identifier of the customer where the task should be completed (if applicable) |
| `customer_code` | `string` | The unique code of the customer where the task should be completed (if applicable) |
| `reminder_offset` | `string` | A timespan indicating when the reminder for this task will be sent out (it is the duration between the reminder and the due date) |
| `completed_date` | `string` (date-time) | The date the task was completed (the task is not completed if this field is empty) |
| `assigned_user_id`* | `integer` | The unique identifier of the user that must complete this task (required if assigned_user_name is not provided) |
| `assigned_user_name`* | `string` | The user name of the the user that must complete this task (required if assigned_user_id is not provided) |
| `anytime`* | `boolean` | True if the task can be completed at any time on the due_date (ignore the time segment of due_date if true) |

#### `TaskPut`

This is a task in Skynamo, used for updating information about tasks.

| Field | Type | Description |
|---|---|---|
| `id`* | `integer` | The unique identifier of the task |
| `description`* | `string` | The description of what the task entails |
| `due_date`* | `string` (date-time) | The date when the task is due (the time will be ignored if anytime is set to true) |
| `customer_id` | `integer` | The unique identifier of the customer where the task should be completed (if applicable) |
| `customer_code` | `string` | The unique code of the customer where the task should be completed (if applicable) |
| `reminder_offset` | `string` | A timespan indicating when the reminder for this task will be sent out (it is the duration between the reminder and the due date) |
| `completed_date` | `string` (date-time) | The date the task was completed (the task is not completed if this field is empty) |
| `assigned_user_id`* | `integer` | The unique identifier of the user that must complete this task (required if assigned_user_name is not provided) |
| `assigned_user_name`* | `string` | The user name of the the user that must complete this task (required if assigned_user_id is not provided) |
| `anytime`* | `boolean` | True if the task can be completed at any time on the due_date (ignore the time segment of due_date if true) |

#### `TaskPatch`

This is a task in Skynamo, used for updating information about tasks.

| Field | Type | Description |
|---|---|---|
| `id`* | `integer` | The unique identifier of the task |
| `description` | `string` | The description of what the task entails |
| `due_date` | `string` (date-time) | The date when the task is due (the time will be ignored if anytime is set to true) |
| `customer_id` | `integer` | The unique identifier of the customer where the task should be completed (if applicable) |
| `customer_code` | `string` | The unique code of the customer where the task should be completed (if applicable) |
| `reminder_offset` | `string` | A timespan indicating when the reminder for this task will be sent out (it is the duration between the reminder and the due date) |
| `completed_date` | `string` (date-time) | The date the task was completed (the task is not completed if this field is empty) |
| `assigned_user_id` | `integer` | The unique identifier of the user that must complete this task |
| `assigned_user_name` | `string` | The user name of the the user that must complete this task |
| `anytime` | `boolean` | True if the task can be completed at any time on the due_date (ignore the time segment of due_date if true) |

#### `TaskDelete`

A list of task IDs that should be deleted

Type: array of `integer`. No named properties in the spec.

#### `VisitFrequency`

The frequency that a user should visit a customer. Example: once(1) every 2 weeks

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique identifier of the visit frequency |
| `customer_id` | `integer` | The unique identifier of the customer where the visit frequency should be used |
| `customer_code` | `string` | The unique code of the customer where the visit frequency should be used (customer_code must correspond with customer_id and customer_name) |
| `customer_name` | `string` | The name of the customer where the visit frequency should be used (customer_name must correspond with customer_id and customer_code) |
| `user_id` | `integer` | The unique identifier of the user that the visit frequency is assigned to |
| `user_name` | `string` | The user name of the user that the visit frequency is assigned to (user_name must correspond with user_id) |
| `cycle` | `integer` | Number of cycles per period ("2" in the example at the top) |
| `frequency` | `integer` | Number of visits per cycle ("once(1)" in example at the top) |
| `period` | `string` | The duration of a period. ("weeks" in example at the top) Contains one of the following values: week, month or year |
| `row_version` | `number` (long) | An automatically generated, unique number used to version-stamp table rows in the database |
| `last_modified_time` | `string` (date-time) | The last time the visit frequency was modified |

#### `VisitFrequencyPost`

This is a visit frequency in Skynamo, used for adding a visit frequency. Example: once(1) every 2 weeks

| Field | Type | Description |
|---|---|---|
| `customer_id`* | `integer` | The unique identifier of the customer where the visit frequency should be used |
| `customer_code`* | `string` | The unique code of the customer where the visit frequency should be used (customer_code must correspond with customer_id and customer_name) |
| `user_id`* | `integer` | The unique identifier of the user that the visit frequency is assigned to |
| `user_name`* | `string` | The user name of the user that the visit frequency is assigned to (user_name must correspond with user_id) |
| `cycle`* | `integer` | Number of cycles per period ("2" in the example at the top) |
| `frequency`* | `integer` | Number of visits per cycle ("once(1)" in example at the top) |
| `period`* | `string` | The duration of a period. ("weeks" in example at the top) Contains one of the following values: week, month or year |

#### `VisitFrequencyPut`

This is a visit frequency in Skynamo, used for replacing a visit frequency. Example: once(1) every 2 weeks

| Field | Type | Description |
|---|---|---|
| `id`* | `integer` | A unique ID assigned to the visit frequency |
| `customer_id`* | `integer` | The unique identifier of the customer where the visit frequency should be used |
| `customer_code`* | `string` | The unique code of the customer where the visit frequency should be used (customer_code must correspond with customer_id and customer_name) |
| `user_id`* | `integer` | The unique identifier of the user that the visit frequency is assigned to |
| `user_name`* | `string` | The user name of the user that the visit frequency is assigned to (user_name must correspond with user_id) |
| `cycle`* | `integer` | Number of cycles per period ("2" in the example at the top) |
| `frequency`* | `integer` | Number of visits per cycle ("once(1)" in example at the top) |
| `period`* | `string` | The duration of a period. ("weeks" in example at the top) Contains one of the following values: week, month or year |

#### `VisitFrequencyPatch`

This is a visit frequency in Skynamo, used for updating information about a visit frequency (only values specified will be updated). Example: once(1) every 2 weeks

| Field | Type | Description |
|---|---|---|
| `id`* | `integer` | A unique ID assigned to the visit frequency |
| `customer_id` | `integer` | The unique identifier of the customer where the visit frequency should be used |
| `customer_code` | `string` | The unique code of the customer where the visit frequency should be used (customer_code must correspond with customer_id and customer_name) |
| `user_id` | `integer` | The unique identifier of the user that the visit frequency is assigned to |
| `user_name` | `string` | The user name of the user that the visit frequency is assigned to (user_name must correspond with user_id) |
| `cycle` | `integer` | Number of cycles per period ("2" in the example at the top) |
| `frequency` | `integer` | Number of visits per cycle ("once(1)" in example at the top) |
| `period` | `string` | The duration of a period. ("weeks" in example at the top) Contains one of the following values: week, month or year |

#### `VisitFrequencyDelete`

A list of visit frequency IDs that should be deleted

Type: array of `integer`. No named properties in the spec.

### Forms and custom fields

#### `CompletedForm`

This is a completed form in Skynamo, used for fetching information about completed forms

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique ID of the completed form |
| `date` | `string` (date-time) | The date and time when this form was completed |
| `customer_id` | `integer` | The unique ID of the customer where the form was completed |
| `customer_code` | `string` | The unique code of the customer where the form was completed |
| `customer_name` | `string` | The name of the customer where the form was completed |
| `user_id` | `integer` | The unique ID of the user that completed the form |
| `user_name` | `string` | The user name of the user that completed the form |
| `interaction_id` | `integer` | The unique ID of the interaction in which this form was completed |
| `form_id` | `integer` | The unique ID of the form definition that was completed |
| `form_name` | `string` | The name of the form definition that was completed |
| `custom_fields` | [`CustomFields`](#customfields) | — |
| `last_modified_time` | `string` (date-time) | The last time the completed form was modified |

#### `FormDefinition`

This is a form definition in Skynamo, used for fetching information about a form

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique ID of the form definition |
| `name` | `string` | The name of the form |
| `type` | `string` | The type of the form |
| `recipients` | `string` | The email recipients |
| `users` | array of `integer` | The user ids that can complete this form. If empty all user can use the form |
| `last_modified_time` | `string` (date-time) | The last time the form definition was modified |
| `active` | `boolean` | Indicates whether the form is active or not |
| `custom_fields` | [`FormDefinitionCustomFields`](#formdefinitioncustomfields) | — |

#### `FormDefinitionCustomFields`

List of custom fields of the form definition

Type: array of [`FormDefinitionCustomField`](#formdefinitioncustomfield). No named properties in the spec.

#### `FormDefinitionCustomField`

Custom field of a form definition

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique id of the custom field |
| `name` | `string` | The name of the custom field |
| `required` | `boolean` | Indicates if the field is required |
| `type` | `string` | Indicates the custom field type |
| `enumeration_values` | [`FormDefinitionCustomFieldEnumerators`](#formdefinitioncustomfieldenumerators) | — |

#### `FormDefinitionCustomFieldEnumerators`

List of availible enumeration values of the cutomform definition

Type: array of [`FormDefinitionCustomFieldEnumerator`](#formdefinitioncustomfieldenumerator). No named properties in the spec.

#### `FormDefinitionCustomFieldEnumerator`

An enumerator of a custom field definition

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | — |
| `label` | `string` | — |
| `parent_id` | `integer` | — |
| `comment` | [`CustomFieldEnumeratorComment`](#customfieldenumeratorcomment) | — |

#### `CustomFields`

Certain custom fields may be required depending on the custom fields that have been set up

Type: array of [`CustomField`](#customfield). No named properties in the spec.

#### `CustomField`

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique id of the custom field |
| `name` | `string` | The name of the custom field |
| `value` | `string` | The value of the custom field (format will depend on type of custom field) |

#### `CustomFieldEnumeratorComment`

This is a comment object of a form enumator value used to state if a certain custom field needs to be included with a value.

| Field | Type | Description |
|---|---|---|
| `required` | `boolean` | Indicates wheter a comment is required |
| `required_custom_field_id` | `integer` | The unique identifier of the custom field that stores the comment. Is required |

#### `CustomfieldPatch`

This is a customfield in Skynamo, used for patching a customfield

| Field | Type | Description |
|---|---|---|
| `id`* | `integer` | A unique ID of the customfield |
| `name`* | `string` | The name of the customfield |

### Users and configuration

#### `User`

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique id of the user |
| `user_name` | `string` | The user name of the user that they use to login |
| `display_name` | `string` | The display name of the user |
| `email` | `string` | The email of the user |
| `active` | `boolean` | Indicates whether the user is active or not |
| `access` | `string` | The platform the user can log into |

#### `Configuration`

This is a configuration in Skynamo, used for fetching information about set configurations values

| Field | Type | Description |
|---|---|---|
| `company_name` | `string` | The name of the company |
| `company_logo` | `string` | The guid of the company image |
| `hide_links_in_emails` | `boolean` | Indicates whether links are hidden in emails |
| `credit_request_recipients` | `string` | Emails recipients for credit requests |
| `order_email_recipients` | `string` | Emails recipients for orders |
| `quote_recipients` | `string` | Emails recipients for quotes |
| `show_order_item_discount_in_emails` | `boolean` | Indicates whether the discount is displayed on the order email |
| `show_product_thumbnail_in_emails` | `boolean` | Indicates if images for products are included in the emails |
| `new_customer_email_recipients` | `string` | Recipients for new customers that was added |
| `form_completed_email_recipients` | `string` | Recipients for forms that were completed |
| `import_email_recipients` | `string` | Recipients for import results once completed |
| `export_customers_on_imports` | `boolean` | Indicates whether a customer export will execute once integration is triggered |
| `export_products_on_imports` | `boolean` | Indicates whether a product export will execute once integration is triggered |
| `export_credit_requests_on_imports` | `boolean` | Indicates whether a credit request export will execute once integration is triggered |
| `export_orders_on_imports` | `boolean` | Indicates whether a order export will execute once integration is triggered |
| `export_quotes_on_imports` | `boolean` | Indicates whether a quote export will execute once integration is triggered |
| `reporting_status` | `boolean` | The status of reporting |
| `default_price_list_id` | `integer` | The id of the default price list |
| `minimum_price_list_id` | `integer` | The id of the minimum price list |
| `warning_price_list_id` | `integer` | The id of the warning price list |
| `cost_price_list_id` | `integer` | The id of the cost price list |
| `deals_enabled` | `boolean` | Indicates whether deals are enabled |
| `overall_discount_enabled` | `boolean` | Indicates whether overall discount is enabled |
| `warehouse_required` | `boolean` | Indicates whether a warehouse is required |
| `credit_request_signature_required` | `boolean` | Indicates if a signature is required on credit requests |
| `order_signature_required` | `boolean` | Indicates if a signature is required on orders |
| `quote_signature_required` | `boolean` | Indicates if a signature is required on credit requests |
| `allow_offsite_visits` | `boolean` | Indicates if offisite visits are allowed |
| `currency` | [`CurrencyBase`](#currencybase) | — |
| `number_of_decimals_on_product_pricing` | `integer` | The number of decimals to show on product pricing |
| `time_zone` | [`TimeZone`](#timezone) | — |
| `distance_unit` | [`DistanceUnit`](#distanceunit) | — |
| `default_tax_rate_id` | `integer` | The is of the default tax rate |
| `mobile_excessive_data_usage_threshold` | `number` (double) | Mobile data threshold |
| `maximum_mobile_photo_size` | `number` (float) | Maximum photo size |
| `form_photo_wait_time` | `string` (date-time) | The max wait time for images to upload before forms are completed |
| `session_duration` | `string` (date-time) | The duration of a users login session |
| `minimum_password_length` | `integer` | (Deprecated) The minimum length that a password can be |
| `minimum_password_non_apha_length` | `integer` | (Deprecated) The minimum length that a password can be of non alpha characters |
| `max_invalid_password_attempts` | `integer` | (Deprecated) The maximum number of invalid password attempts |
| `days_until_password_expiry` | `integer` | (Deprecated) Days until password expires |

#### `TimeZone`

This is a contact in Skynamo, used for updating information about a contact (only values specified will be updated)

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | A unique ID assigned to the timezone |
| `utc_offset` | `string` | The utc offset of the timezone |
| `country_code` | `string` | The country code of the timezone |
| `country_name` | `string` | The country name of the timezone |

#### `DistanceUnit`

This is a contact in Skynamo, used for updating information about a contact (only values specified will be updated)

| Field | Type | Description |
|---|---|---|
| `code` | `string` | The distance unit code |
| `symbol` | `string` | The distance unit symbol |

### Files

#### `File`

This is a file in Skynamo

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique ID of the file |
| `filename` | `string` | The name of the file |
| `expire_time` | `string` (date-time) | The expire time of the file |
| `content` | `string` | The content of the file (base64string) |
| `content_hash` | `string` | The content hash of the file (base64string) |
| `transaction_id` | `integer` | The transtion id of the file |

#### `FilePost`

This is a file in Skynamo

| Field | Type | Description |
|---|---|---|
| `filename` | `string` | The name of the file |
| `content` | `string` | The content of the file (base64string) |
| `content_hash` | `string` | The content hash of the file (base64string) |
| `transaction_id` | `integer` | The transition id of the file used for orders |

### Integrations

#### `IntegrationRequest`

This is an integration request, used for executing integration actions

| Field | Type | Description |
|---|---|---|
| `action` | `string` | The integration action to be executed **One of:** `AutoGrowEnums`, `AddCustomFields`, `ImportNow`, `ResubmitOrderItemDocuments` |
| `enum_grow_data` | array of [`EnumGrowData`](#enumgrowdata) | Used in conjunction with the action 'AutoGrowEnums' |
| `fields_to_add` | array of [`FieldGrowData`](#fieldgrowdata) | Used in conjunction with the action 'AddCustomFields' |
| `document_ids` | array of `integer` | Used in conjunction with the action 'ResubmitOrderItemDocuments' |

#### `IntegrationFormValues`

This is the integration form values in Skynamo

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique ID of the active integration |
| `name` | `string` | The name of the active integration |
| `last_modified_time` | `string` (date-time) | The last time the integration values was modified |
| `custom_fields` | [`CustomFields`](#customfields) | — |

#### `EnumGrowData`

Object for specifying auto grow enum information

| Field | Type | Description |
|---|---|---|
| `customfield_id` | `integer` | The unique id of the customfield to grow |
| `enums` | array of `string` | — |

#### `FieldGrowData`

Object for specifying auto grow field information

| Field | Type | Description |
|---|---|---|
| `form_id` | `integer` | The unique id of the warehouse |
| `name` | `string` | The name of the warehouse |
| `type` | `string` | Supported field types **One of:** `Text`, `Number`, `SingleSelect`, `MultiSelect`, `NestedSingleSelect`, `NestedMultiSelect`, `Address`, `UserSingleSelect`, `UserMultiSelect`, `HeadingLabel`, `NormalLabel`, `FinePrintLabel` |

### Logging, paging and errors

#### `LogEntry`

This is a log entry in Skynamo, used for fetching information about invoices

| Field | Type | Description |
|---|---|---|
| `id` | `integer` | The unique id of the log entry |
| `time` | `string` (date-time) | The date when the log entry was issued |
| `tag` | `string` | The tag of the log entry |
| `error_level` | `string` | The error level of the log entry |
| `user` | `string` | The username associated with the log entry |
| `message` | `string` | The message content of the log entry |

#### `PagingResponse`

| Field | Type | Description |
|---|---|---|
| `page_number` | `integer` | — |
| `page_size` | `integer` | — |
| `total_item_count` | `integer` | — |
| `filtered_item_count` | `integer` | — |

#### `ErrorModel`

| Field | Type | Description |
|---|---|---|
| `message` | `string` | Describes whether an error has occured |
| `errors` | array of `object` | A list of the errors that have occured |

