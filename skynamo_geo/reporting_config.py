"""Constants for Skynamo's Reporting (Analytics) API.

This is a DIFFERENT product from the Public API in config.py: another host,
OAuth2 client-credentials instead of x-api-key headers, read-only, its own JSON
query language, named reporting periods, bookmark-based deltas, and published
(tight) rate limits. See docs/skynamo-reporting-api-and-powerbi.md.

The entity surface is declared once here, as data, so adding an entity later is
a change to REPORTING_ENTITIES and nothing else - the client, store and engine
all drive off this registry.
"""

# --- Endpoints and auth --------------------------------------------------
ANALYTICS_BASE = "https://analytics-api.svc.skynamo.me"
TOKEN_URL = "https://login.skynamo.me/oauth/token"
# Fixed, required, and the trailing slash matters. It is an opaque OAuth
# audience identifier, not a URL that resolves.
TOKEN_AUDIENCE = "https://integration.skynamo.me/"
# Token lifetime is undocumented: trust `expires_in`, fall back to this.
TOKEN_DEFAULT_TTL = 3600
TOKEN_REFRESH_SKEW = 60  # refresh this many seconds before expiry

# --- Reporting periods ---------------------------------------------------
# The 21 values of ReportingPeriodTypeV2, grouped for the UI.
PERIOD_GROUPS = [
    ("Day", ["ThisDay", "PrevDay"]),
    ("Week", ["ThisWeek", "PrevWeek"]),
    ("Month", ["ThisMonth", "PrevMonth"]),
    ("Rolling", ["This30Days", "Prev30Days", "This90Days", "Prev90Days",
                 "This180Days", "Prev180Days", "This365Days", "Prev365Days"]),
    ("Financial", ["FinThisQuarter", "FinPrevQuarter", "FinThisSixMonths",
                   "FinPrevSixMonths", "FinThisYear", "FinPrevYear"]),
    ("Everything", ["AllData"]),
]
REPORTING_PERIODS = [p for _group, periods in PERIOD_GROUPS for p in periods]
DEFAULT_REPORTING_PERIOD = "Prev30Days"

# Rate limits are published per reporting period and scale inversely with how
# much data the period covers: {period: (max_queries, window_seconds)}.
# NOTE: the spec's rate-limit prose says "LastMonth" but the enum defines
# "PrevMonth" - PrevMonth is the real value.
_LIMIT_30_PER_30S = (30, 30)
_LIMIT_4_PER_MIN = (4, 60)
_LIMIT_4_PER_10MIN = (4, 600)
_LIMIT_2_PER_10MIN = (2, 600)

RATE_LIMIT_BY_PERIOD = {}
for _p in ("ThisDay", "PrevDay", "ThisWeek", "PrevWeek",
           "This30Days", "Prev30Days", "ThisMonth", "PrevMonth"):
    RATE_LIMIT_BY_PERIOD[_p] = _LIMIT_30_PER_30S
for _p in ("This90Days", "Prev90Days", "FinThisQuarter", "FinPrevQuarter"):
    RATE_LIMIT_BY_PERIOD[_p] = _LIMIT_4_PER_MIN
for _p in ("This180Days", "Prev180Days", "FinThisSixMonths", "FinPrevSixMonths",
           "This365Days", "Prev365Days", "FinThisYear", "FinPrevYear"):
    RATE_LIMIT_BY_PERIOD[_p] = _LIMIT_4_PER_10MIN
RATE_LIMIT_BY_PERIOD["AllData"] = _LIMIT_2_PER_10MIN
# Anything unrecognised gets the most conservative budget.
DEFAULT_RATE_LIMIT = _LIMIT_2_PER_10MIN

# --- Sub-entity column shapes -------------------------------------------
# Orders, quotes and credit requests are the same shape with different key
# names ("As orders, keyed quote_id / quote_item_id" in the spec), so build
# them rather than repeating four near-identical literals.

def _document_total_columns(doc_id):
    """Header/total row of a sales document (one per document)."""
    return {
        doc_id: "TEXT", "activity_id": "TEXT", "date": "TEXT",
        "reference": "TEXT", "discount": "REAL",
        "prices_include_tax": "INTEGER", "discount_value": "REAL",
        "subtotal_value": "REAL", "tax_value": "REAL", "quote_id": "TEXT",
        "customer_id": "TEXT", "customer_name": "TEXT",
        "user_id": "TEXT", "display_name": "TEXT",
        "summary_tax_note": "TEXT", "summary_reporting_note": "TEXT",
    }


def _document_item_columns(item_id, doc_id):
    """Line-item row of a sales document (many per document)."""
    return {
        item_id: "TEXT", doc_id: "TEXT", "activity_id": "TEXT",
        "product_id": "TEXT", "product_code": "TEXT", "quantity": "REAL",
        "unit_name": "TEXT", "unit_multiplier": "REAL",
        "list_price": "REAL", "unit_price": "REAL",
        "item_discount": "REAL", "item_discount_value": "REAL",
        "item_subtotal_value": "REAL", "tax_rate": "REAL",
        "item_tax_value": "REAL", "quote_id": "TEXT",
    }


def _document_pair(api_total, api_items, table_prefix, doc_id, item_id):
    """The {totals, items} sub-entity pair for one document type."""
    return {
        api_total: {
            "table": f"{table_prefix}_totals",
            "primary_key": doc_id,
            "parent_key": "activity_id",
            "columns": _document_total_columns(doc_id),
        },
        api_items: {
            "table": f"{table_prefix}_items",
            "primary_key": item_id,
            "parent_key": "activity_id",
            "columns": _document_item_columns(item_id, doc_id),
        },
    }


# Every sub-entity /v2/activities can expand. They all come back in the SAME
# request, so including them costs no extra API calls - only a larger response.
_ACTIVITY_SUB_ENTITIES = {
    "visits": {
        "table": "activity_visits",
        "primary_key": "activity_id",
        "parent_key": "activity_id",
        "columns": {
            "activity_id": "TEXT", "start_time": "TEXT", "end_time": "TEXT",
            "duration_sec": "INTEGER", "is_scheduled": "INTEGER",
            "is_onsite": "INTEGER",
        },
    },
    "surveys": {
        "table": "surveys",
        "primary_key": "survey_item_id",
        "parent_key": "activity_id",
        "columns": {
            "survey_item_id": "TEXT", "survey_id": "TEXT",
            "activity_id": "TEXT", "product_id": "TEXT",
            "product_code": "TEXT", "product_name": "TEXT",
            "stock_level": "REAL", "facings": "REAL", "retail_price": "REAL",
        },
    },
    "forms": {
        "table": "activity_forms",
        "primary_key": "completed_form_id",
        "parent_key": "activity_id",
        "columns": {
            "completed_form_id": "TEXT", "activity_id": "TEXT",
            "completed_date": "TEXT", "form_type": "TEXT",
            "json_view": "TEXT",
        },
    },
    # No unique field in the spec for these two, so the store derives a
    # deterministic key from the identifying columns (see synthetic_key).
    "comments": {
        "table": "activity_comments",
        "primary_key": "row_key",
        "parent_key": "activity_id",
        "synthetic_key": ["activity_id", "date", "customer_comment"],
        "columns": {
            "row_key": "TEXT", "activity_id": "TEXT", "date": "TEXT",
            "customer_comment": "TEXT",
        },
    },
    "emails": {
        "table": "activity_emails",
        "primary_key": "row_key",
        "parent_key": "activity_id",
        "synthetic_key": ["activity_id", "date", "recipients", "description"],
        "columns": {
            "row_key": "TEXT", "activity_id": "TEXT", "date": "TEXT",
            "recipients": "TEXT", "description": "TEXT",
        },
    },
}
_ACTIVITY_SUB_ENTITIES.update(_document_pair(
    "orderTotals", "orders", "order", "order_id", "order_item_id"))
_ACTIVITY_SUB_ENTITIES.update(_document_pair(
    "quoteTotals", "quotes", "quote", "quote_id", "quote_item_id"))
_ACTIVITY_SUB_ENTITIES.update(_document_pair(
    "creditRequestTotals", "creditRequests", "credit_request",
    "credit_request_id", "credit_request_item_id"))

# Which table holds each document type, for breakdowns in the dashboard.
DOCUMENT_TABLES = {
    "Orders": ("order_totals", "order_items"),
    "Quotes": ("quote_totals", "quote_items"),
    "Credit requests": ("credit_request_totals", "credit_request_items"),
    "Visits": ("activity_visits", None),
    "Surveys": ("surveys", None),
    "Forms": ("activity_forms", None),
    "Comments": ("activity_comments", None),
    "Emails": ("activity_emails", None),
}

# --- Entity registry -----------------------------------------------------
# Per entity:
#   endpoint      - path under ANALYTICS_BASE
#   table         - SQLite table name
#   primary_key   - column that uniquely identifies a row (upsert key)
#   columns       - {column: SQLite type}
#   has_period    - endpoint accepts reportingPeriod & friends
#   bookmarkable  - endpoint supports bookmark / returns x-bookmark
#   order_by      - a field valid for `order` (required to use skip/limit)
#   sub_entities  - {api_name: {...}} expanded in one call via `entities`
#
# Sub-entities carry parent_key so rows can be linked back to their root.
# Column sets follow the documented schemas; two endpoints' declared response
# schemas look wrong in the spec (/v2/products, /v2/yearonyearsales), so
# payloads should be confirmed live before being trusted - which is what the
# live check script is for.

REPORTING_ENTITIES = {
    "activities": {
        "endpoint": "/v2/activities",
        "table": "activities",
        "primary_key": "activity_id",
        "has_period": True,
        "bookmarkable": True,
        "order_by": "start_time",
        "columns": {
            "activity_id": "TEXT", "activity_type": "TEXT",
            "customer_id": "TEXT", "customer_name": "TEXT",
            "customer_code": "TEXT", "customer_is_active": "INTEGER",
            "user_id": "TEXT", "display_name": "TEXT",
            "user_is_active": "INTEGER",
            "start_time": "TEXT", "end_time": "TEXT",
            "longitude": "REAL", "latitude": "REAL", "comment": "TEXT",
        },
        # All 11 documented sub-entities - they arrive in the same request, so
        # this is what turns a vague "activity" into "an order / a quote / a
        # visit / a survey / a form / an email".
        "sub_entities": _ACTIVITY_SUB_ENTITIES,
    },
    "customers": {
        "endpoint": "/v2/customers",
        "table": "customers",
        "primary_key": "customer_id",
        "has_period": True,
        "bookmarkable": True,
        "order_by": "code",
        # display_name/added_by_user_id/user_is_active are OFF by default in
        # the API, so ask for them explicitly.
        "extra_fields": ["added_by_user_id", "display_name", "user_is_active"],
        "columns": {
            "customer_id": "TEXT", "name": "TEXT", "code": "TEXT",
            "is_active": "INTEGER", "longitude": "REAL", "latitude": "REAL",
            "added_date": "TEXT", "added_by_user_id": "TEXT",
            "display_name": "TEXT", "user_is_active": "INTEGER",
            "json_view": "TEXT",
        },
        "sub_entities": {
            "invoices": {
                "table": "customer_invoices",
                "primary_key": "sale_item_id",
                "parent_key": "customer_id",
                "columns": {
                    "sale_item_id": "TEXT", "sale_id": "TEXT",
                    "customer_id": "TEXT", "date": "TEXT",
                    "reference": "TEXT", "status": "TEXT",
                    "due_date": "TEXT", "tax_inclusion": "TEXT",
                    "total_tax": "REAL", "total": "REAL",
                    "outstanding_balance": "REAL", "product_id": "TEXT",
                    "product_code": "TEXT", "quantity": "REAL",
                    "line_tax": "REAL", "value": "REAL",
                },
            },
            "customerTargets": {
                "table": "customer_targets",
                "primary_key": "customer_target_id",
                "parent_key": "customer_id",
                "columns": {
                    "customer_target_id": "TEXT", "customer_id": "TEXT",
                    "month": "TEXT", "sales_target": "REAL",
                },
            },
        },
    },
    "users": {
        "endpoint": "/v2/users",
        "table": "users",
        "primary_key": "user_id",
        "has_period": True,
        "bookmarkable": False,   # no bookmark param; small table, full reload
        "order_by": "display_name",
        "columns": {
            "user_id": "TEXT", "login_name": "TEXT", "display_name": "TEXT",
            "is_active": "INTEGER", "role": "TEXT", "email": "TEXT",
            "cell_phone": "TEXT", "last_sync_time": "TEXT",
        },
        "sub_entities": {
            "userTargets": {
                "table": "user_targets",
                "primary_key": "user_target_id",
                "parent_key": "user_id",
                "columns": {
                    "user_target_id": "TEXT", "user_id": "TEXT",
                    "month": "TEXT", "target": "REAL",
                },
            },
        },
    },
    "products": {
        "endpoint": "/v2/products",
        "table": "products",
        "primary_key": "product_id",
        "has_period": False,     # no reporting period on this endpoint
        "bookmarkable": True,    # bookmark is unqualified by period here
        "order_by": "code",
        "columns": {
            "product_id": "TEXT", "name": "TEXT", "code": "TEXT",
            "description": "TEXT", "is_active": "INTEGER",
            "json_view": "TEXT",
        },
        "sub_entities": {},      # `entities` is the empty base schema here
    },
    "invoices": {
        "endpoint": "/v2/invoices",
        "table": "invoices",
        "primary_key": "sale_item_id",
        "has_period": True,
        "bookmarkable": True,
        "order_by": None,        # sortable fields undocumented for this one
        "columns": {
            "sale_item_id": "TEXT", "sale_id": "TEXT", "customer_id": "TEXT",
            "date": "TEXT", "reference": "TEXT", "status": "TEXT",
            "due_date": "TEXT", "tax_inclusion": "TEXT",
            "total_tax": "REAL", "total": "REAL",
            "outstanding_balance": "REAL", "product_id": "TEXT",
            "product_code": "TEXT", "quantity": "REAL",
            "line_tax": "REAL", "value": "REAL",
        },
        "sub_entities": {},
    },
}

# Metadata endpoints (no period, no bookmark) - used to discover an instance's
# filterable custom fields.
FILTERABLE_FIELDS_ENDPOINTS = {
    "customer": "/v2/customerfilterablefields",
    "product": "/v2/productfilterablefields",
}
ROLES_ENDPOINT = "/v2/roles"

# --- Extract statuses / report columns -----------------------------------
STATUS_RPT_PENDING = "pending-extract"
STATUS_RPT_EXTRACTED = "extracted"
STATUS_RPT_SKIPPED = "skipped"
STATUS_RPT_FAILED = "extract-failed"

REPORTING_REPORT_FIELDNAMES = [
    "entity", "mode", "reporting_period", "rows", "date_range",
    "status", "notes",
]

# Where the local extract store lives (inside the existing config dir).
STORE_FILENAME = "reporting.db"
