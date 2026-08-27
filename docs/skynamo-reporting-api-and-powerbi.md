# Skynamo Reporting API and the Power BI Connector

The **Analytics API** (marketed as the *Reporting API*) is Skynamo's purpose-built, read-only
analytics interface — a separate product from the
[Public API](skynamo-public-api-guide.md), with its own host, its own authentication, its own query
language, and an official Power BI custom connector.

**Written against Analytics API `v2.0`**, spec fetched from
`https://analytics-api.svc.skynamo.me/swagger/v2/swagger.json` (OpenAPI 3.0.4).

Source tags: **[spec]** from the Analytics API OpenAPI document · **[kb]** from
`support.skynamo.com` · **[inferred]** reasoned, not documented.

> **This document corrects the support KB in three places.** The KB describes *"four endpoints that
> returns data for 19 entities/tables"*. The **live v2 spec has 11 endpoints**, and v1 — the
> 4-endpoint version — is explicitly marked *"This API version has been deprecated."* **[spec]** The
> KB also gives two different table counts (18 and 21) in the same article, and its access
> instructions contradict a second KB article. Details in [§3](#3-getting-credentials) and
> [§6](#6-endpoints).

---

## Contents

1. [How it differs from the Public API](#1-how-it-differs-from-the-public-api)
2. [Commercial access](#2-commercial-access)
3. [Getting credentials](#3-getting-credentials)
4. [Authenticating](#4-authenticating)
5. [Exploring the API](#5-exploring-the-api)
6. [Endpoints](#6-endpoints)
7. [The filter query language](#7-the-filter-query-language)
8. [Reporting periods](#8-reporting-periods)
9. [Incremental extraction with bookmarks](#9-incremental-extraction-with-bookmarks)
10. [Rate limits](#10-rate-limits)
11. [Entities and fields](#11-entities-and-fields)
12. [The Power BI custom connector](#12-the-power-bi-custom-connector)
13. [What it can and cannot do](#13-what-it-can-and-cannot-do)
14. [Troubleshooting](#14-troubleshooting)
15. [Open questions for Skynamo](#15-open-questions-for-skynamo)

---

## 1. How it differs from the Public API

| | **Public API** | **Analytics / Reporting API** |
|---|---|---|
| Host | `api.skynamo.me/v1` | `analytics-api.svc.skynamo.me` |
| Spec | Swagger 2.0, `v1.0.28` | **OpenAPI 3.0.4, `v2.0`** |
| Auth | `x-api-key` + `x-api-client` headers | **OAuth2 client-credentials → JWT Bearer** |
| Cost | Included | **Paid add-on** **[kb]** |
| Direction | Read **and write** | **Read only** — 11 endpoints, all `GET` **[spec]** |
| Filtering | 14 of 30 endpoints, 6 operators | Every data endpoint; `EQ LT LE GT GE NE IN` **[spec]** |
| Sorting | **None** | `order` on selected fields **[spec]** |
| Field selection | **None** | `fields` — include/exclude per field **[spec]** |
| Sub-entity expansion | **None** | `entities` — a whole graph in one call **[spec]** |
| Paging | `page_number` / `page_size` (max 200) | `skip` / `limit` — **`order` required to use them** **[spec]** |
| Delta | `row_version` filter on 7 endpoints | **`bookmark` param + `x-bookmark` response header** **[spec]** |
| Date handling | Build it yourself | 21 built-in reporting periods, financial-year aware **[spec]** |
| Rate limits | Undocumented | **Published, explicit, per-period** **[spec]** |
| Power BI | Hand-built M queries | Official custom connector **[kb]** |

### What the Reporting API has that the Public API simply does not

All **[spec]**:

- **Sales targets and actuals** — `CustomerTarget`, `UserTarget`, `AssignedUserTarget` with an
  `Actuals` collection.
- **Travel claims** — `TravelClaim` with `claimed_distance`, `recorded_distance`, `odometer_start`,
  `odometer_end`.
- **Time and motion** — `UserTimeSegment` with `activity`, `duration_sec`, `recorded_distance`.
- **Stocktake / survey results** — `Survey` with `stock_level`, `facings`, `retail_price` per
  product. (The Public API's `Interaction.stocktake_id` is a dangling reference — no endpoint serves
  it.)
- **Line-item identifiers** — `order_item_id`, `quote_item_id`, `credit_request_item_id`,
  `sale_item_id`, `survey_item_id`. The Public API's sales lines have no key at all.
- **Pre-aggregated document totals** — `OrderTotal`, `QuoteTotal`, `CreditRequestTotal` with
  `discount_value`, `subtotal_value`, `tax_value`.
- **Precomputed visit metrics** — `duration_sec`, `is_onsite`, `is_scheduled` on `Visit`.
- **User roles** — `UserExtended.role` and a `GET /v2/roles` endpoint.
- **RFM visit counts** and **year-on-year sales** as dedicated endpoints.

**If you are building a BI model, start here.** Fall back to the Public API for what this one does
not carry — stock levels, price lists, deal groups, contacts, form definitions, instance
configuration — and for anything you need to *write*. See
[Pulling Skynamo data for BI with the Public API](skynamo-public-api-for-bi.md).

---

## 2. Commercial access

The Reporting API and the Power BI connector are a **paid add-on**. The KB is blunt about it:
*"The good things in life are never free, and the same goes for access to the Skynamo Reporting API
and the Skynamo Analytics Power BI Connector."*
([Skynamo Reporting API and Power BI Connector](https://support.skynamo.com/support/solutions/skynamo-reporting-api-and-power-bi-connector)) **[kb]**

Documented route: **add the feature to your subscription via billing, then contact
`support@skynamo.com`** — who provide the Client ID, Client Secret, and the connector file. **[kb]**

Pricing is not published. Talk to your Customer Success contact.

---

## 3. Getting credentials

**Skynamo insights → Settings → Integration Tokens → *Add client credential*.** **[kb]**
([Creating Reporting API credentials](https://support.skynamo.com/support/solutions/creating-reporting-api-credentials))

> **This is the same screen as the Public API key, with a different button.**
>
> | Button | Issues | For |
> |---|---|---|
> | **Add access token** | An `x-api-key` value | **Public API** |
> | **Add client credential** | A Client ID + Client Secret pair | **Reporting API** |
>
> Clicking the wrong one is the most common setup mistake here.

> **⚠️ The KB contradicts itself on this.** *Creating Reporting API credentials* documents the
> self-service flow above; the *Power BI Connector* article says Skynamo support emails you the
> credentials. **[kb]** Most likely the self-service button is the current mechanism and the
> support-email route is legacy or a fallback for the connector file **[inferred]** — but if the
> button is absent in your instance, the add-on probably is not enabled. Raise it with support.

Treat the Client Secret exactly as you would a password: never in source control, never in a
`.pbix` you share.

---

## 4. Authenticating

OAuth2 **client-credentials** grant against Skynamo's identity service, then a JWT Bearer token on
every API call. **[spec] [kb]**

The spec declares: **[spec]**

```
securitySchemes:
  Bearer:
    type: http
    scheme: bearer
    bearerFormat: JWT
    description: "Enter JWT Bearer token only"
security: [ { Bearer: [] } ]
```

### Getting a token **[kb]**

```bash
curl -s -X POST "https://login.skynamo.me/oauth/token" \
  -H "content-type: application/json" \
  -d '{
        "client_id":     "'"$SKYNAMO_CLIENT_ID"'",
        "client_secret": "'"$SKYNAMO_CLIENT_SECRET"'",
        "audience":      "https://integration.skynamo.me/",
        "grant_type":    "client_credentials"
      }'
```

```powershell
$body = @{
    client_id     = $env:SKYNAMO_CLIENT_ID
    client_secret = $env:SKYNAMO_CLIENT_SECRET
    audience      = 'https://integration.skynamo.me/'
    grant_type    = 'client_credentials'
} | ConvertTo-Json

$token = (Invoke-RestMethod -Method Post -Uri 'https://login.skynamo.me/oauth/token' `
             -ContentType 'application/json' -Body $body).access_token
```

```python
import os, requests

def get_token():
    r = requests.post("https://login.skynamo.me/oauth/token", timeout=30, json={
        "client_id":     os.environ["SKYNAMO_CLIENT_ID"],
        "client_secret": os.environ["SKYNAMO_CLIENT_SECRET"],
        "audience":      "https://integration.skynamo.me/",
        "grant_type":    "client_credentials",
    })
    r.raise_for_status()
    return r.json()["access_token"]
```

The `audience` value is fixed and required — `https://integration.skynamo.me/`, with the trailing
slash. **[kb]**

> **Two things that look like errors but are not.** `audience` is an **opaque OAuth identifier, not
> an endpoint** — it is never fetched, and `https://integration.skynamo.me/` does not resolve in a
> browser. And the token endpoint is **`POST`-only**: a `GET` to
> `https://login.skynamo.me/oauth/token` returns `404`. Verified — a `POST` with invalid credentials
> returns `401 {"error":"access_denied","error_description":"Unauthorized"}`, which is the correct
> response and confirms the endpoint is live.

### Calling the API

```bash
curl -s -G "https://analytics-api.svc.skynamo.me/v2/customers" \
  --data-urlencode 'filter={}' \
  -H "Authorization: Bearer $TOKEN"
```

> **There is no `x-api-client` header here.** The instance is identified by the credentials
> themselves, not by a routing header — unlike the Public API. **[spec]** (The **Power BI connector**
> does ask for an instance name separately **[kb]**, which suggests either the connector passes it
> some other way or its credentials are not instance-scoped. Worth confirming with support if you are
> building your own client for a multi-instance setup. **[inferred]**)

### Token lifetime

**Not documented anywhere.** **[spec: absent] [kb: absent]**

> A widely-repeated claim that the token *"is only valid for 24 hours"* could not be confirmed on any
> live Skynamo page — its likely source is a retired KB article. **Do not hard-code a lifetime.**
> Read `expires_in` from the token response if present, refresh on `401`, and cache the token between
> calls rather than fetching one per request.

```python
import time

class TokenCache:
    """Cache the bearer token and refresh shortly before it expires.

    Lifetime is undocumented, so trust `expires_in` from the response and fall
    back to a short default rather than assuming a long one.
    """
    def __init__(self, fetch, default_ttl=3600, skew=60):
        self._fetch, self._default_ttl, self._skew = fetch, default_ttl, skew
        self._token, self._expires_at = None, 0.0

    def get(self):
        if self._token is None or time.monotonic() >= self._expires_at - self._skew:
            payload = self._fetch()
            self._token = payload["access_token"]
            ttl = float(payload.get("expires_in") or self._default_ttl)
            self._expires_at = time.monotonic() + ttl
        return self._token
```

---

## 5. Exploring the API

**Swagger UI:** `https://analytics-api.svc.skynamo.me/swagger/index.html?urls.primaryName=v2`
— **select `v2`**; the default may land you on the deprecated v1. **[kb]**

**Raw specs** (both publicly fetchable, no auth needed — handy for diffing and codegen):

| Version | URL | Status |
|---|---|---|
| v2 | `https://analytics-api.svc.skynamo.me/swagger/v2/swagger.json` | Current — 11 endpoints **[spec]** |
| v1 | `https://analytics-api.svc.skynamo.me/swagger/v1/swagger.json` | *"This API version has been deprecated."* — 4 endpoints **[spec]** |

A `.yaml` variant of each is served at the same path.

**Smoke test:** in Swagger UI, click *Authorize*, paste the bearer token, expand **Users**,
*Try it out*, and — per the KB — *"In the filter box, replace all code with `{}`"*, then Execute.
Expect `200`.
([Testing if there is data available from Swagger UI](https://support.skynamo.com/support/solutions/testing-if-there-is-data-available-from-swagger-ui)) **[kb]**

An empty filter `{}` is the documented minimum: *"The most basic filter you can provide is an empty
JSON object {}"*. **[spec]**

---

## 6. Endpoints

Base URL `https://analytics-api.svc.skynamo.me`. **All 11 are `GET`.** **[spec]**

### Data endpoints

| Endpoint | Tag | Returns | Filter language | Reporting period | Bookmark |
|---|---|---|---|---|---|
| `/v2/activities` | Activities | `ActivityExtended` | ✅ full | ✅ | ✅ |
| `/v2/customers` | Customers | `CustomerExtended` | ✅ full | ✅ | ✅ |
| `/v2/users` | Users | `UserExtended` | ✅ full | ✅ | — |
| `/v2/products` | Products | Product fields | ✅ (no `entities`) | — | ✅ |
| `/v2/invoices` | Invoices | `Invoice` | ✅ | ✅ | ✅ |
| `/v2/RFMvisits` | Activities | `RfmVisit` | — (`dateTime`, `timeZone` only) | — | — |
| `/v2/latestVisits` | Visits | `VisitExtended` | — (no parameters) | — | — |
| `/v2/yearonyearsales` | Invoices | *(see note)* | — (`userIds` + period params) | ✅ | — |

### Metadata endpoints

| Endpoint | Returns | Purpose |
|---|---|---|
| `/v2/roles` | `Roles` — `{roleId, name}` | The instance's user roles |
| `/v2/customerfilterablefields` | `RootOption` — `{fieldId, fieldName, isMultiSelect, subOptions[]}` | **Discover the instance's filterable customer custom fields**, including nested option trees |
| `/v2/productfilterablefields` | `RootOption` | Same, for products |

The two `*filterablefields` endpoints are the Reporting API's answer to the Public API's
`/formdefinitions` — call them first to learn what this particular instance can be filtered on.
**[spec]**

> **Two response schemas in the spec look wrong.** `GET /v2/products` declares its `200` response as
> `CustomerExtended`, and `GET /v2/yearonyearsales` declares `UserTimeSegment`. Neither matches the
> endpoint's evident purpose (and the products endpoint has its own `FilterProductFields` schema
> listing `product_id`, `name`, `code`, `description`, `is_active`, `json_view`, `rowVersion`).
> **Verify the actual payloads against your instance before modelling them.** **[spec defect]**

### Undocumented in the spec

`/v2/RFMvisits`, `/v2/latestVisits`, `/v2/invoices`, `/v2/roles`, `/v2/yearonyearsales` and both
`*filterablefields` endpoints have **empty `summary` and `description`**. Only `/v2/activities`,
`/v2/customers`, `/v2/users` and `/v2/products` carry the long documentation. **[spec]** Expect to
experiment with the rest.

### About the KB's entity counts

The KB says *"four endpoints that returns data for 19 entities/tables"*, and elsewhere in the same
article *"The Navigator Page shows 18 tables available for selection"* — while its data section says
21. **[kb]**

The four-endpoint description matches **v1**, which is deprecated. **[spec]** From the live v2 spec,
counting distinct data-bearing schemas reachable across all 11 endpoints, there are **26**
([§11](#11-entities-and-fields)). How many surface as tables in the Power BI Navigator is a
*connector* question, not an API one, and the connector is *"still in development"* **[kb]** — so
treat any fixed table count as a snapshot rather than a contract.

---

## 7. The filter query language

Data endpoints take one **`filter` query parameter containing a JSON object**. **[spec]** This is the
Reporting API's real advantage: it is an actual query language.

```
GET /v2/customers?filter={}
```

Five keys, all optional: **[spec]**

| Key | Purpose |
|---|---|
| `fields` | Include/exclude individual fields. Only list the ones you want to **override** |
| `where` | Predicates on selected fields |
| `order` | Sort direction per field |
| `entities` | Include sub-entities (the graph expansion) |
| `skip` / `limit` | Paging — **`order` is required to use either** |

A worked example, straight from the spec: **[spec]**

```json
{
  "skip": 10,
  "limit": 10,
  "fields":   { "comment": false },
  "where":    { "user_is_active": { "op": "EQ", "value": "1" } },
  "order":    { "display_name": "DESC" },
  "entities": {
    "creditRequestTotals": { "include": true },
    "quoteTotals":         { "include": true }
  }
}
```

URL-encoded onto a request:

```bash
curl -s -G "https://analytics-api.svc.skynamo.me/v2/activities" \
  --data-urlencode 'filter={"limit":100,"order":{"start_time":"DESC"},"entities":{"visits":{"include":true},"orderTotals":{"include":true}}}' \
  --data-urlencode 'reportingPeriod=ThisMonth' \
  -H "Authorization: Bearer $TOKEN"
```

### `where` operators **[spec]**

`FilterWhereOperatorEnum`: **`EQ`**, **`LT`**, **`LE`**, **`GT`**, **`GE`**, **`NE`**, **`IN`**.

Each predicate is `{"op": "<operator>", "value": "<string>"}` — `value` is typed `string` even for
numbers and booleans, hence `"1"` for true in the spec's own example. **[spec]**

Note this includes **`NE`** and **`IN`**, neither of which the Public API has.

### `fields` — sparse fieldsets with per-field defaults

Every field has a documented default. Some are **off** by default — typically the denormalised
`*_name` / `*_code` convenience columns. **[spec]**

`FilterCustomerFields` defaults: **[spec]**

| Field | Default |
|---|---|
| `customer_id`, `name`, `code`, `is_active`, `longitude`, `latitude`, `added_date`, `json_view`, `rowVersion` | **on** |
| `added_by_user_id`, `display_name`, `user_is_active` | **off** |

So to get the creating user you must ask:

```json
{ "fields": { "added_by_user_id": true, "display_name": true } }
```

Check the `Filter…Fields` schema for each entity before assuming a column will be present.

### `order` and `where` are restricted to specific fields

Not every field is sortable or filterable. **[spec]**

| Entity | `where` on | `order` on |
|---|---|---|
| Customer | `code`, `name`, `is_active` | `code`, `name`, `added_date` |
| Activity | `activity_type`, `customer_is_active`, `user_is_active` | `activity_id`, `activity_type`, `customer_name`, `display_name`, `start_time` |
| User | `is_active`, `role` | `display_name` |
| Product | `name`, `code`, `is_active` | `name`, `code` |
| Customer→Invoice | `customer_id`, `customer_is_active`, `product_id`, `product_is_active` | *(see `FilterCustomerInvoiceSortOrder`)* |

For richer customer/product predicates, use the custom fields exposed by
`/v2/customerfilterablefields` and `/v2/productfilterablefields`.

### `entities` — the sub-entity graph

Each sub-entity is `{"include": true}` plus its own `fields`, `where`, `order`, `skip` and `limit`.
Default is `include: false`. **[spec]**

| Root | Available sub-entities **[spec]** |
|---|---|
| `/v2/activities` | `orderTotals`, `quoteTotals`, `creditRequestTotals`, `orders`, `quotes`, `creditRequests`, `surveys`, `forms`, `visits`, `comments`, `emails` |
| `/v2/customers` | `customerUsers`, `invoices`, `customerTargets` |
| `/v2/users` | `travelClaims`, `userTimeSegments`, `tasks`, `userTargets`, `assignedUserTargets` |
| `/v2/products` | *(none — `entities` is the empty base schema)* |

This is genuinely powerful: one `/v2/activities` call with all eleven sub-entities included returns
the entire field-activity graph for a period, already joined. It is also the fastest way to hit a
rate limit, so scope it deliberately.

### `skip` / `limit` — order is mandatory

The spec repeats it for both: *"**ORDER BY** required to use Skip"*, *"**ORDER BY** required to use
Limit"*. **[spec]** Without an `order` clause your paging is either rejected or non-deterministic.
Always pair them:

```json
{ "order": { "name": "ASC" }, "skip": 0, "limit": 500 }
```

> **Default `limit` is not documented**, nor is a maximum. **[spec: absent]** Set `limit` explicitly
> and page rather than relying on defaults.

---

## 8. Reporting periods

Instead of hand-building date predicates, you name a period. Five optional parameters shape it.
**[spec]**

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `reportingPeriod` | enum | `Prev30Days` | See below |
| `reportingDate` | string | today | The anchor date the period is computed around |
| `financialYearStartMonth` | `Jan`…`Dec` | `Jan` | Affects the `Fin*` periods |
| `monthStartDay` | integer | `1` | Affects the `Fin*` periods |
| `weekStart` | `Sunday`…`Saturday` | `Sunday` | Affects the week periods |
| `timeZone` | string | `UTC` | Present on every data endpoint |

### The 21 periods **[spec]**

| Group | Values |
|---|---|
| Day | `ThisDay`, `PrevDay` |
| Week | `ThisWeek`, `PrevWeek` |
| Month | `ThisMonth`, `PrevMonth` |
| Rolling | `This30Days`, `Prev30Days`, `This90Days`, `Prev90Days`, `This180Days`, `Prev180Days`, `This365Days`, `Prev365Days` |
| Financial | `FinThisQuarter`, `FinPrevQuarter`, `FinThisSixMonths`, `FinPrevSixMonths`, `FinThisYear`, `FinPrevYear` |
| Everything | `AllData` |

Worked example from the spec — previous financial quarter with a March year-end: **[spec]**

```
GET /v2/activities
  ?filter={"entities":{"orderTotals":{"include":true}}}
  &reportingPeriod=FinPrevQuarter
  &reportingDate=2021/03/01
  &financialYearStartMonth=Mar
  &monthStartDay=1
```

### Verify the window the server actually used

The response carries an **`x-date-range` header** giving *"the start and end epoch date value for the
report as calculated on the server"*. **[spec]**

**Log it on every call.** Financial-period arithmetic with a configurable year start, month start day
and week start has plenty of room for off-by-one surprises, and this header is the only way to prove
which window your numbers came from.

```python
r = session.get(url, params=params, timeout=60)
r.raise_for_status()
print("server window:", r.headers.get("x-date-range"))
```

---

## 9. Incremental extraction with bookmarks

The Reporting API has a **proper delta mechanism** — the single biggest operational advantage over
the Public API, where 16 of 30 endpoints cannot be filtered at all.

Available on `/v2/activities`, `/v2/customers`, `/v2/products` and `/v2/invoices`. **[spec]**

How it works, per the spec: **[spec]**

> *"This parameter provides the ability to retrieve only new data that was added since your previous
> request with the same reporting period. If you inspect the response headers of this request you
> will notice a `x-bookmark` header that will provide the bookmark value you can use in your next
> request to only receive the delta."*

```
1.  GET /v2/customers?filter={}&reportingPeriod=ThisMonth
        → 200, response header:  x-bookmark: 8842137
2.  store 8842137
3.  GET /v2/customers?filter={}&reportingPeriod=ThisMonth&bookmark=8842137
        → only rows changed since
```

**Rules that matter:**

- **A bookmark is scoped to the reporting period.** *"since your previous request **with the same
  reporting period**"*. Change `reportingPeriod` and your bookmark is meaningless — keep one
  bookmark per `(endpoint, reportingPeriod)` pair.
- **`/v2/products` has no reporting period**, so its bookmark is unqualified — the spec's wording
  drops the period clause for that endpoint. **[spec]**
- **`/v2/users` has no `bookmark`.** Full reload every time — it is a small table.
- **Store the bookmark only after the load commits**, exactly as with a `row_version` watermark.
- **Bookmarks almost certainly do not report deletions**, being described purely as *"new data that
  was added"*. Reconcile keys periodically. **[inferred]**

```python
BOOKMARKED = ["activities", "customers", "products", "invoices"]

def extract(session, endpoint, bookmarks, reporting_period="Prev30Days", filter_obj=None):
    """Pull a bookmarked delta. Returns (rows, new_bookmark).

    The bookmark is scoped to (endpoint, reportingPeriod) -- reusing one across
    periods gives meaningless results, so the store is keyed on both.
    """
    import json
    key = f"{endpoint}:{reporting_period}"
    params = {"filter": json.dumps(filter_obj or {})}
    if endpoint != "products":
        params["reportingPeriod"] = reporting_period
    if bookmarks.get(key):
        params["bookmark"] = bookmarks[key]

    r = session.get(f"https://analytics-api.svc.skynamo.me/v2/{endpoint}",
                    params=params, timeout=120)
    r.raise_for_status()
    return r.json(), r.headers.get("x-bookmark"), r.headers.get("x-date-range")
```

---

## 10. Rate limits

**Unlike the Public API, these are published — and they are tight.** The limit scales inversely with
how much data the period covers. Quoted verbatim from the endpoint descriptions: **[spec]**

| Limit | Reporting periods |
|---|---|
| **30 queries per 30 seconds** | `ThisDay`, `PrevDay`, `ThisWeek`, `PrevWeek`, `This30Days`, `Prev30Days`, `ThisMonth`, `LastMonth` |
| **4 queries per 1 minute** | `This90Days`, `Prev90Days`, `FinThisQuarter`, `FinPrevQuarter` |
| **4 queries per 10 minutes** | `This180Days`, `Prev180Days`, `FinThisSixMonths`, `FinPrevSixMonths`, `This365Days`, `Prev365Days`, `FinThisYear`, `FinPrevYear` |
| **2 queries per 10 minutes** | `AllData` |

*"Each reporting period will have a dedicated rate limit to prevent abuse."* **[spec]**

> **Note the enum mismatch.** The rate-limit text says `LastMonth`; the `ReportingPeriodTypeV2` enum
> defines **`PrevMonth`**. **[spec defect]** Use `PrevMonth` — it is the one in the enum.

### What this means in practice

- **`AllData` is for a one-off backfill, not a schedule.** Two calls per ten minutes across your
  whole organisation. Use it once to seed history, then move to bookmarks with a short period.
- **`skip`/`limit` paging is expensive at long periods.** Paging a `FinThisYear` query in 500-row
  pages costs one query per page against a 4-per-10-minutes budget — a 20-page extract takes nearly
  an hour. **Prefer `entities` expansion over many small calls**: one request returning a joined
  graph beats twenty returning fragments.
- **Design your schedule around the tiers.** Hourly incremental on `Prev30Days` (30 per 30 s is
  generous); anything financial-year-wide, daily at most.
- **Power BI will blow through these without help.** See [§12](#12-the-power-bi-custom-connector).

### The throttled response

The KB documents the Power BI symptom: **[kb]**

> `OLE DB or ODBC error: [Expression.Error] You are making too many requests. Please see the
> documentation for rate limitations`

Documented remedy: *"Please wait for 1 hour, and then resubmit your details"*
([rate limitations article](https://support.skynamo.com/support/solutions/skynamo-powerbi-reporting-error-you-are-making-too-many-requests.-please-see-the-documentation-for-rate-limitations)) **[kb]**

> **The one-hour wait is longer than any published window** (the longest is 10 minutes). Either the
> connector's own backoff is coarse, or a separate longer-term quota exists that is not documented.
> **[inferred]** Build in generous backoff.

The HTTP status code and headers returned on throttle are **not documented** for this API — the spec
declares only `200` on every operation. **[spec]** Assume `429`, handle `503` too, and honour
`Retry-After` if present.

---

## 11. Entities and fields

The 26 data-bearing schemas in v2, by endpoint. **[spec]**

### `/v2/activities` → `ActivityExtended`

Root fields: `activity_id`, `activity_type`, `customer_id`, `customer_name`, `customer_code`,
`customer_is_active`, `user_id`, `display_name`, `user_is_active`, `start_time`, `end_time`,
`longitude`, `latitude`, `comment`, `RowVersion`.

| Sub-entity | Key fields |
|---|---|
| `OrderTotal` | `order_id`, `activity_id`, `date`, `reference`, `discount`, `prices_include_tax`, `discount_value`, `subtotal_value`, `tax_value`, `quote_id`, user/customer attributes, `summary_tax_note`, `summary_reporting_note` |
| `Order` | **`order_item_id`**, `order_id`, `product_id`, `product_code`, `quantity`, `unit_name`, `unit_multiplier`, `list_price`, `unit_price`, `item_discount`, `item_discount_value`, `item_subtotal_value`, `tax_rate`, `item_tax_value`, `quote_id` |
| `QuoteTotal` / `Quote` | As orders, keyed `quote_id` / **`quote_item_id`** |
| `CreditRequestTotal` / `CreditRequest` | As orders, keyed `credit_request_id` / **`credit_request_item_id`** |
| `Survey` | **`survey_item_id`**, `survey_id`, product attributes, **`stock_level`**, **`facings`**, **`retail_price`** |
| `Form` | `completed_form_id`, `activity_id`, `completed_date`, **`json_view`**, `form_type` |
| `Visit` | `activity_id`, `start_time`, `end_time`, **`duration_sec`**, **`is_scheduled`**, **`is_onsite`** |
| `Comment` | `activity_id`, `date`, `customer_comment` |
| `Emails` | `activity_id`, `date`, `recipients`, `description` |

**Note `Order.quote_id` and `OrderTotal.quote_id`** — this gives you quote→order conversion as a
direct foreign key. The Public API's read `Order` schema does not expose it at all.

### `/v2/customers` → `CustomerExtended`

Root: `customer_id`, `name`, `code`, `is_active`, `longitude`, `latitude`, `added_date`,
`added_by_user_id`*, `display_name`*, `user_is_active`*, **`json_view`**, `RowVersion`.
(*off by default.)

| Sub-entity | Key fields |
|---|---|
| `CustomerUser` | Customer↔user assignment plus **`visits_per_cycle`**, `period`, `periods_per_cycle` — the visit-frequency rule |
| `Invoice` | **`sale_item_id`**, `sale_id`, `date`, `reference`, `status`, `due_date`, `tax_inclusion`, `total_tax`, `total`, **`outstanding_balance`**, product attributes, `quantity`, `line_tax`, `value`, `RowVersion` |
| `CustomerTarget` | **`customer_target_id`**, `month`, **`sales_target`** |

`json_view` carries the customer's custom-field payload — the analogue of the Public API's
`custom_fields` array. Combine with `/v2/customerfilterablefields` to interpret it.

### `/v2/users` → `UserExtended`

Root: `user_id`, `login_name`, `display_name`, `is_active`, **`role`**, `email`, `cell_phone`,
**`last_sync_time`**.

| Sub-entity | Key fields |
|---|---|
| `TravelClaim` | **`travel_claim_id`**, `date`, **`claimed_distance`**, **`recorded_distance`**, `comment`, `odometer_start`, `odometer_end` |
| `UserTimeSegment` | **`activity`**, `start_time`, `end_time`, **`duration_sec`**, **`recorded_distance`** |
| `Task` | `task_id`, `task_type`, `added_date`, `due_date`, `activity_id`, `comment`, `completed_date`, `is_anytime`, `end_time`, **`task_status`** |
| `UserTarget` | **`user_target_id`**, `month`, **`target`** |
| `AssignedUserTarget` | `user_target_id`, `target`, `name`, `type`, `period`, `start_date`, **`Actuals[]`** |
| `AssignedUserTargetActual` | `user_target_actual_id`, `date_of_actual_utc`, `last_modified_time_utc`, **`actual`** |

`UserTargetType` and `UserTargetPeriod` are integer enums with values `0`, `1`, `2` and **no labels
in the spec** — you will need to determine the meanings empirically or ask support. **[spec defect]**

### Other endpoints

| Endpoint | Schema | Fields |
|---|---|---|
| `/v2/products` | Product | `product_id`, `name`, `code`, `description`, `is_active`, `json_view`, `rowVersion` (from `FilterProductFields`) |
| `/v2/invoices` | `Invoice` | As the customer sub-entity above |
| `/v2/latestVisits` | `VisitExtended` | `customerId`, `customerCode`, `customerName`, **`dateDiff`**, `date`, `displayName`, `userId`, **`isOnsite`**, `customerUsers[]` → `VisitUser` (`visitsPerCycle`, `periodsPerCycle`, `period`) |
| `/v2/RFMvisits` | `RfmVisit` | `customerId`, `customerCode`, `customerName`, **`count`** |
| `/v2/roles` | `Roles` | `roleId`, `name` |
| `/v2/*filterablefields` | `RootOption` | `fieldId`, `fieldName`, `isMultiSelect`, `subOptions[]` → `SubOption` (recursive: `optionId`, `optionName`, `subOptions[]`) |

> **`VisitExtended` and `RfmVisit` use camelCase** (`customerId`, `isOnsite`) while every other
> schema uses snake_case (`customer_id`, `is_onsite`). **[spec]** Handle both in your mapping layer.

### Naming differences from the Public API

Same concepts, different names. Watch these when conforming the two sources: **[spec]**

| Concept | Public API | Reporting API |
|---|---|---|
| Active flag | `active` | `is_active` |
| Version stamp | `row_version` | `RowVersion` / `rowVersion` |
| Invoice | `id` | `sale_id` |
| Invoice line | *(no key)* | `sale_item_id` |
| Interaction | `id` | `activity_id` |
| Tax inclusive | `prices_include_vat` | `prices_include_tax` |
| Stocktake | `stocktake_id` (dangling) | `Survey` / `survey_id` |
| User display name | `display_name` | `display_name` |
| Custom fields | `custom_fields[]` array | `json_view` string |

---

## 12. The Power BI custom connector

Skynamo ships an official custom connector, **`SkynamoAnalytics.pqx`**. **[kb]**

### Installation **[kb]**

1. Obtain `SkynamoAnalytics.pqx` **and** the accompanying `.reg` file (thumbprint registration) from
   Skynamo support.
2. Copy the `.pqx` into `C:\Users\<you>\Documents\Power BI Desktop\Custom Connectors`
   (create the folder if absent).
3. Run the `.reg` file — it writes to
   `HKEY_LOCAL_MACHINE\Software\Policies\Microsoft\Power BI Desktop`. **Requires local admin
   rights.**
4. Restart Power BI Desktop.

> The registry key is machine-wide policy under `HKLM`, so a locked-down corporate desktop may block
> it. Involve IT before you start. **[inferred]**

### Connector inputs **[kb]**

| Input | Required | Default |
|---|---|---|
| Skynamo Instance Name | **Yes** | — |
| Client ID | Yes | — |
| Client Secret | Yes | — |
| Reporting Period | No | *"Defaults to the last 30 days"* |
| Financial Year start date | No | *"Defaults to 1 January"* |

These map onto `reportingPeriod=Prev30Days` and `financialYearStartMonth=Jan` — the same defaults the
API declares. **[spec] [kb]**

### Licensing **[kb]**

- Power BI **Desktop** is free for personal development.
- To share reports, *"each user has, at least, a Power BI Pro license"*.
- To refresh in the **Power BI Service**, the **On-Premises Data Gateway** machine needs the same
  connector installation and registry setup.

### Settings you must change

The connector will hit the rate limits from [§10](#10-rate-limits) unless you stop Power BI firing
speculative parallel requests. Per the KB: **[kb]**

- **Options → Data Load → Background Data:** turn **off** *"Allow data preview to download in the
  background"*.
- **Options → Data Load → Parallel loading of tables:** **disable**.

> *"these settings do not persist"* **[kb]** — they are per-file and can revert. Re-check them
> whenever you open the report, and before every scheduled refresh setup.

### Data shape

The connector's tables are deliberately trimmed: *"The columns returned per table have been limited
to improve the efficiency of the connector."* **[kb]** So the connector shows **fewer columns than
the API can return**. If you need a field the Navigator does not offer, query the API directly with
an explicit `fields` clause ([§7](#7-the-filter-query-language)).

ERDs per endpoint are provided in the KB article. **[kb]**

### Maturity

*"The Skynamo Analytics Power BI Connector is still in development."* **[kb]** Plan for change: pin
the connector version you tested against, keep a copy of the `.pqx`, and re-validate your model after
any connector update.

---

## 13. What it can and cannot do

### Can

- **Query properly** — sparse fieldsets, `where` with seven operators including `NE` and `IN`,
  `order`, `skip`/`limit`.
- **Return a joined graph in one call** via `entities`, up to eleven sub-entities on
  `/v2/activities`.
- **Handle reporting periods natively**, financial-year aware, with `x-date-range` confirming the
  server's computed window.
- **Extract incrementally** with `bookmark` / `x-bookmark` on activities, customers, products and
  invoices — no full scans.
- **Deliver what the Public API cannot**: sales targets and actuals, travel claims, time segments,
  stocktake/survey results, line-item keys, pre-aggregated document totals, precomputed visit
  duration and on-site flags, user roles.
- **Discover instance-specific filterable custom fields** via the two `*filterablefields` endpoints.
- **Feed Power BI through a supported connector.**
- **Tell you its rate limits up front.**

### Cannot

- **Write anything.** All 11 operations are `GET`. **[spec]** For writes, use the
  [Public API](skynamo-public-api-guide.md).
- **Cost nothing.** Paid add-on. **[kb]**
- **Cover the whole domain.** No stock levels, price lists, prices, deal groups, contacts, form
  definitions, tax rates, currencies, warehouses, or instance configuration. **[spec]**
- **Report deletions.** `bookmark` is described as *"new data that was added"*. No tombstones.
  **[spec] [inferred]**
- **Provide attribute history.** `RowVersion` and `last_modified_time_utc` say *that* something
  changed, never *what it was before*. No SCD source. **[spec]**
- **Be queried freely at scale.** `AllData` is capped at 2 requests per 10 minutes. **[spec]**
- **Guarantee a stable connector.** *"still in development"*, and its columns are deliberately
  trimmed. **[kb]**
- **Tell you its token lifetime.** Undocumented. **[spec: absent]**
- **Document 7 of its 11 endpoints.** Empty `summary`/`description` on `/v2/RFMvisits`,
  `/v2/latestVisits`, `/v2/invoices`, `/v2/roles`, `/v2/yearonyearsales` and both
  `*filterablefields`. **[spec]**
- **Be trusted blindly on two response schemas** — `/v2/products` and `/v2/yearonyearsales` declare
  types that look wrong. **[spec defect]**

### Recommended split

| Need | Use |
|---|---|
| Sales, activity, visits, targets, travel, forms, invoices | **Reporting API** |
| Stock levels, prices, price lists, deal groups, contacts, form definitions, tax rates, currencies, warehouses, configuration | **Public API** ([BI guide](skynamo-public-api-for-bi.md)) |
| Writing anything back | **Public API** |
| Self-service Power BI, small model | **Reporting API connector** |
| Governed warehouse pipeline | **Reporting API** for facts + **Public API** for the missing dimensions |

---

## 14. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `401 access_denied` on the token call | Wrong `client_id`/`client_secret`, or missing/incorrect `audience`. | `audience` must be exactly `https://integration.skynamo.me/`, trailing slash included. **[kb]** |
| `404` on the token call | You sent a `GET`. The endpoint is **`POST`-only**. | Use `POST` with a JSON body. |
| `https://integration.skynamo.me/` does not load in a browser | Expected — it is an OAuth audience identifier, not an endpoint. | Nothing to fix. |
| `401` on an API call | Token expired, or sent without the `Bearer ` prefix. | Lifetime is undocumented — refresh on `401` and cache per [§4](#4-authenticating). |
| You were issued an `x-api-key`, not a client ID/secret | You clicked **Add access token** instead of **Add client credential**. | Both live on Settings → Integration Tokens. See [§3](#3-getting-credentials). |
| The **Add client credential** button is missing | The paid add-on is probably not enabled. | Check billing, then contact `support@skynamo.com`. **[kb]** |
| Swagger UI shows only 4 endpoints | You are on the deprecated **v1**. | Append `?urls.primaryName=v2`. **[spec]** |
| *"You are making too many requests"* in Power BI | Rate limit for your reporting period. | Wait (KB says an hour), disable background data preview and parallel table loading. **[kb]** |
| Refresh fails only in the Service, works on Desktop | The gateway machine lacks the connector and registry setup. | Install `.pqx` + `.reg` on the gateway host. **[kb]** |
| The `.reg` step fails | It writes to `HKLM` policy and needs local admin. | Get IT to apply it. **[kb]** |
| `skip`/`limit` behaves oddly or is rejected | No `order` clause. | *"ORDER BY required to use Skip/Limit"*. **[spec]** |
| A field you expected is missing | It is off by default. | Set it explicitly in `fields` — check the entity's `Filter…Fields` schema. **[spec]** |
| Sub-entity arrays are empty | `include` defaults to `false`. | `"entities": {"visits": {"include": true}}`. **[spec]** |
| Wrong date window in the results | Financial-period parameters interact. | Read the **`x-date-range`** response header to see the server's computed window. **[spec]** |
| `reportingPeriod=LastMonth` rejected | The rate-limit text names `LastMonth`; the enum defines `PrevMonth`. | Use `PrevMonth`. **[spec defect]** |
| Bookmark returns nothing / everything | Bookmarks are scoped to the reporting period. | Key your store on `(endpoint, reportingPeriod)`. **[spec]** |
| Numbers disagree with the Public API | Different names and different tax semantics. | See the naming table in [§11](#11-entities-and-fields); note `prices_include_tax` vs `prices_include_vat`. |
| Deleted records persist in your model | No tombstones. | Periodic full-key reconciliation with soft-delete. |
| Column names inconsistent between tables | `VisitExtended`/`RfmVisit` are camelCase; everything else snake_case. | Normalise in your mapping layer. **[spec]** |

---

## 15. Open questions for Skynamo

Genuinely undocumented. Worth asking your Customer Success contact or `support@skynamo.com` before
building anything load-bearing:

1. **What is the OAuth token lifetime?** Does the response include `expires_in`? (The widely-quoted
   "24 hours" is unverified — see [§4](#4-authenticating).)
2. **What HTTP status and headers are returned on throttle?** Is it `429`? Is `Retry-After` sent? The
   spec declares only `200`. **[spec]**
3. **Why does the KB's remedy say wait one hour** when the longest published window is 10 minutes?
   Is there an additional daily or hourly quota?
4. **Are client credentials instance-scoped?** If so, why does the Power BI connector ask for an
   instance name separately — and how do you serve a multi-instance group?
5. **What are the `UserTargetType` and `UserTargetPeriod` integer values** `0`, `1`, `2`? The spec
   gives no labels. **[spec defect]**
6. **What do `/v2/products` and `/v2/yearonyearsales` actually return?** Their declared response
   schemas (`CustomerExtended`, `UserTimeSegment`) look wrong. **[spec defect]**
7. **What are `/v2/RFMvisits`, `/v2/latestVisits` and `/v2/yearonyearsales` for**, and what do
   `dateDiff` and `count` mean? All undocumented. **[spec]**
8. **Is there a default and maximum `limit`?**
9. **Is v1 scheduled for removal, and is there a v3 roadmap?** No deprecation dates are published.
10. **Is `bookmark` genuinely insert-only**, or does it also surface updates and deletes?
11. **What is the current connector table count**, and is there a changelog for `SkynamoAnalytics.pqx`?
12. **Is a Tableau, Qlik or generic ODBC path supported?** The endpoint descriptions still reference a
    *"Skynamo Reporting API with Qlik"* support article that is no longer published. **[spec]**
