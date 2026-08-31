"""Offline tests for the HTML dashboard: renders from a seeded store, is
self-contained (no external requests), numbers match hand-computed values,
escapes instance text, and handles an empty store without raising."""

import os
import re
import shutil
import sqlite3
import tempfile

from skynamo_geo.dashboard import (
    build_dashboard, _bar_chart, _invoice_totals, _line_chart, _money,
    _nice_max, _num, _truncate,
)
from skynamo_geo.report_store import ReportStore

# --- formatting helpers ---
assert _num(1234567) == "1,234,567"
assert _num(0) == "0"
assert _num(None) == "0"
assert _num(12.345, 2) == "12.35"
assert _num("nonsense") == "0"
assert _nice_max(0) == 1.0
assert _nice_max(7) >= 7
assert _nice_max(830) >= 830
assert _truncate("short", 10) == "short"
assert _truncate("a very long label indeed", 10).endswith("…")

# --- charts degrade rather than crash ---
assert "No data" in _bar_chart([])
assert "No data" in _line_chart([])
# a single point renders as a bar, not a broken line
single = _line_chart([("2026-08-01", 5)])
assert "<svg" in single
# all-zero values must not divide by zero
assert "<svg" in _bar_chart([("a", 0), ("b", 0)])
assert "<svg" in _line_chart([("a", 0), ("b", 0), ("c", 0)])

tmp = tempfile.mkdtemp(prefix="skynamo_dash_")
try:
    # --- empty store still renders, with explicit no-data states ---
    empty = ReportStore(os.path.join(tmp, "empty.db"))
    empty_path = build_dashboard(empty, os.path.join(tmp, "empty.html"),
                                 title="Empty Test")
    empty_html = open(empty_path, encoding="utf-8").read()
    assert "Empty Test" in empty_html
    assert "No data" in empty_html or "Nothing has been extracted" in empty_html
    assert "Nothing has been extracted yet" in empty_html, \
        "freshness panel should tell the user to run an extract"
    empty.close()

    # --- seeded store: known numbers ---
    store = ReportStore(os.path.join(tmp, "seed.db"))
    store.upsert_entity("customers", [
        {"customer_id": "c1", "name": "Acme Ltd", "code": "A1", "is_active": True},
        {"customer_id": "c2", "name": "Globex", "code": "G1", "is_active": True},
        {"customer_id": "c3", "name": "Dormant", "code": "D1", "is_active": False},
    ])
    store.upsert_entity("users", [
        {"user_id": "u1", "display_name": "Rep One", "is_active": True},
    ])
    store.upsert_entity("products", [
        {"product_id": "p1", "name": "Widget", "code": "W1"},
        {"product_id": "p2", "name": "Gadget", "code": "G2"},
    ])
    store.upsert_entity("activities", [
        {
            "activity_id": "a1", "activity_type": "Visit",
            "customer_id": "c1", "customer_name": "Acme Ltd",
            "user_id": "u1", "display_name": "Rep One",
            "start_time": "2026-08-10T09:00:00",
            "visits": [{"activity_id": "a1", "duration_sec": 1800,
                        "is_onsite": True, "is_scheduled": True}],
            "orderTotals": [{"order_id": "o1", "date": "2026-08-10",
                             "subtotal_value": 300.0, "tax_value": 45.0}],
            "orders": [{"order_item_id": "oi1", "order_id": "o1",
                        "product_id": "p1", "product_code": "W1",
                        "quantity": 3, "item_subtotal_value": 300.0}],
        },
        {
            "activity_id": "a2", "activity_type": "Visit",
            "customer_id": "c2", "customer_name": "Globex",
            "user_id": "u1", "display_name": "Rep One",
            "start_time": "2026-08-11T09:00:00",
            "visits": [{"activity_id": "a2", "duration_sec": 900,
                        "is_onsite": False}],
            "orderTotals": [{"order_id": "o2", "date": "2026-08-11",
                             "subtotal_value": 100.0, "tax_value": 15.0}],
            "orders": [{"order_item_id": "oi2", "order_id": "o2",
                        "product_id": "p2", "product_code": "G2",
                        "quantity": 1, "item_subtotal_value": 100.0}],
        },
    ])
    store.upsert_entity("invoices", [
        {"sale_item_id": "si1", "sale_id": "s1", "customer_id": "c1",
         "date": "2026-08-12", "value": 250.0, "outstanding_balance": 50.0},
    ])
    store.record_run("activities", "ThisMonth", "full", 2,
                     "2026-08-01..2026-08-31", "extracted",
                     started_at="2026-08-27T09:00:00")

    path = build_dashboard(store, os.path.join(tmp, "dash.html"),
                           title="Seeded Dashboard", period_label="ThisMonth")
    dash = open(path, encoding="utf-8").read()

    # KPIs: 2 orders, 400 total order value, 2 visits, 2 active customers
    assert "Seeded Dashboard" in dash and "ThisMonth" in dash
    assert "400.00" in dash, "total order value 300+100"
    assert ">2<" in dash, "order count / visit count of 2"
    # active customers excludes the inactive one
    assert re.search(r"Active customers.*?>2<", dash, re.S), \
        "should count only the 2 active customers"
    # 45 minutes of visits total (1800+900 sec = 0.75h)
    assert "0.8" in dash or "0.75" in dash

    # Panels present
    for heading in ("Overview", "Order value over time",
                    "Top customers by order value",
                    "Top products by order value", "Visits by user",
                    "Visit type", "Targets vs actuals", "Data freshness"):
        assert heading in dash, heading

    # Customer/product attribution came through the joins
    assert "Acme Ltd" in dash and "Globex" in dash
    assert "Widget" in dash and "Gadget" in dash
    assert "Rep One" in dash
    # Freshness shows the server window
    assert "2026-08-01..2026-08-31" in dash

    # --- invoice totals come from whichever table holds the data ---
    # Regression: outstanding used to read only customer_invoices, so an
    # extract that populated the /v2/invoices root entity showed 0.00.
    inv_only = ReportStore(os.path.join(tmp, "inv_only.db"))
    inv_only.upsert_entity("invoices", [
        # two lines of ONE invoice: outstanding must not be double-counted
        {"sale_item_id": "x1", "sale_id": "s9", "value": 100.0,
         "outstanding_balance": 40.0},
        {"sale_item_id": "x2", "sale_id": "s9", "value": 150.0,
         "outstanding_balance": 40.0},
    ])
    invoiced, outstanding = _invoice_totals(inv_only)
    assert invoiced == 250.0, invoiced
    assert outstanding == 40.0, f"per-invoice, not per-line: {outstanding}"
    inv_html = open(build_dashboard(
        inv_only, os.path.join(tmp, "inv_only.html")), encoding="utf-8").read()
    assert "40.00" in inv_html, "outstanding must render from the invoices table"
    inv_only.close()

    # nested customer invoices are found too
    nested = ReportStore(os.path.join(tmp, "nested.db"))
    nested.upsert_entity("customers", [{
        "customer_id": "c1", "name": "Acme", "is_active": True,
        "invoices": [{"sale_item_id": "n1", "sale_id": "s1", "value": 75.0,
                      "outstanding_balance": 25.0}],
    }])
    assert _invoice_totals(nested) == (75.0, 25.0)
    nested.close()

    # an empty store reports zeros rather than raising
    blank = ReportStore(os.path.join(tmp, "blank.db"))
    assert _invoice_totals(blank) == (0, 0)
    blank.close()

    # --- activity breakdown: the point of the expansion -----------------
    br = ReportStore(os.path.join(tmp, "breakdown.db"))
    br.upsert_entity("products", [
        {"product_id": "p1", "name": "Widget", "code": "W1"}])
    br.upsert_entity("activities", [
        {   # an order-producing visit
            "activity_id": "a1", "activity_type": "Visit",
            "customer_name": "Acme", "display_name": "Rep One",
            "orderTotals": [{"order_id": "o1", "date": "2026-08-01",
                             "subtotal_value": 400.0, "quote_id": "q1"}],
            "orders": [{"order_item_id": "oi1", "order_id": "o1",
                        "product_id": "p1", "item_subtotal_value": 400.0}],
            "visits": [{"activity_id": "a1", "duration_sec": 1200,
                        "is_onsite": True}],
            "surveys": [{"survey_item_id": "s1", "product_id": "p1",
                         "stock_level": 14, "facings": 4}],
        },
        {   # a quote that was NOT converted
            "activity_id": "a2", "activity_type": "Quote",
            "customer_name": "Globex",
            "quoteTotals": [{"quote_id": "q2", "date": "2026-08-02",
                             "subtotal_value": 900.0}],
            "quotes": [{"quote_item_id": "qi2", "quote_id": "q2"}],
        },
        {   # the quote that WAS converted (o1 above references q1)
            "activity_id": "a3", "activity_type": "Quote",
            "quoteTotals": [{"quote_id": "q1", "date": "2026-08-01",
                             "subtotal_value": 400.0}],
        },
        {   # a credit request
            "activity_id": "a4", "activity_type": "Credit Request",
            "creditRequestTotals": [{"credit_request_id": "cr1",
                                     "subtotal_value": 50.0}],
        },
        {   # a phone call with only a comment
            "activity_id": "a5", "activity_type": "Call",
            "comments": [{"activity_id": "a5", "date": "2026-08-03",
                          "customer_comment": "called back"}],
        },
    ])
    html_out = open(build_dashboard(
        br, os.path.join(tmp, "breakdown.html"),
        title="Breakdown"), encoding="utf-8").read()

    # the panels exist
    for heading in ("What the activities were", "Documents produced",
                    "Value by document type", "Survey / stocktake findings"):
        assert heading in html_out, heading
    # each activity type is named, with its count
    for label in ("Visit", "Quote", "Credit Request", "Call"):
        assert label in html_out, label
    # 2 quotes, 1 order, 1 credit request, 1 visit, 1 survey, 1 comment
    assert "Orders" in html_out and "Quotes" in html_out
    assert "Credit requests" in html_out
    # quote -> order conversion: 1 of 2 quotes converted
    assert "1 of 2 quotes in this extract became orders (50%)" in html_out
    # document values are shown
    assert "900" in html_out or "1,300" in html_out
    # survey stock level appears
    assert "Widget" in html_out
    br.close()

    # an activity with no type falls back rather than vanishing
    untyped = ReportStore(os.path.join(tmp, "untyped.db"))
    untyped.upsert_entity("activities", [{"activity_id": "z1"}])
    out_untyped = open(build_dashboard(
        untyped, os.path.join(tmp, "untyped.html")), encoding="utf-8").read()
    assert "(unspecified)" in out_untyped
    untyped.close()

    # --- review regressions ---------------------------------------------
    from skynamo_geo.dashboard import (
        _activity_types, _document_breakdown, _document_values,
        _survey_findings,
    )

    # (a) conversion must never exceed 100%: a short period or a delta holds
    #     orders raised against quotes from an earlier window.
    conv = ReportStore(os.path.join(tmp, "conv.db"))
    conv.upsert_entity("activities", [{
        "activity_id": "c1",
        # three orders referencing quotes that are NOT in this extract
        "orderTotals": [{"order_id": "o1", "quote_id": "old1",
                         "subtotal_value": 10.0},
                        {"order_id": "o2", "quote_id": "old2",
                         "subtotal_value": 10.0},
                        {"order_id": "o3", "quote_id": "old3",
                         "subtotal_value": 10.0}],
        "orders": [{"order_item_id": "oi1", "order_id": "o1"}],
        # a single quote that was not converted
        "quoteTotals": [{"quote_id": "q9", "subtotal_value": 99.0}],
        "quotes": [{"quote_item_id": "qi9", "quote_id": "q9"}],
    }])
    out = _document_values(conv)
    assert "300%" not in out, "must not count quotes absent from the store"
    assert "0 of 1 quotes in this extract became orders (0%)" in out, out
    conv.close()

    # (b) surveys: LIMIT must not be applied before NULL stock levels are
    #     filtered, or a store with results reports "no results".
    surv = ReportStore(os.path.join(tmp, "surv.db"))
    nulls = [{"survey_item_id": f"n{i}", "product_id": f"pn{i}",
              "stock_level": None} for i in range(10)]
    reals = [{"survey_item_id": f"r{i}", "product_id": "pr1",
              "product_code": "R1", "stock_level": 42} for i in range(3)]
    surv.upsert_entity("products", [{"product_id": "pr1", "name": "Real Product"}])
    surv.upsert_entity("activities", [{"activity_id": "s1",
                                       "surveys": nulls + reals}])
    out = _survey_findings(surv, limit=8)
    assert "No survey" not in out, "results exist and must be shown"
    assert "Real Product" in out and "42" in out, out

    # (c) distinct products sharing a name must not be merged
    surv.upsert_entity("products", [
        {"product_id": "d1", "name": "Cola 500ml"},
        {"product_id": "d2", "name": "Cola 500ml"}])
    surv.upsert_entity("activities", [{"activity_id": "s2", "surveys": [
        {"survey_item_id": "x1", "product_id": "d1", "stock_level": 10},
        {"survey_item_id": "x2", "product_id": "d2", "stock_level": 90}]}])
    out = _survey_findings(surv, limit=10)
    assert out.count("Cola 500ml") == 2, "same-named products stay separate"
    assert "50.0" not in out, "must not average across distinct products"
    surv.close()

    # (d) negative values (credit requests are refunds) render inside the track
    neg = _bar_chart([("Credit requests", -100.0), ("Other", -50.0)],
                     value_format=_money)
    widths = [float(w) for w in re.findall(r'<rect [^>]*width="([\d.]+)"', neg)]
    assert widths and max(widths) <= 490, f"bars must stay in the track: {widths}"
    assert "-100.00" in neg and "amber" in neg
    # mixed signs: the negative is not reduced to a sliver
    mixed = _bar_chart([("Orders", 200.0), ("Credits", -200.0)])
    bars = [float(w) for w in re.findall(
        r'<rect [^>]*width="([\d.]+)"[^>]*rx="3"/>', mixed)]
    assert bars.count(max(bars)) >= 2, "equal magnitudes should be equal bars"

    # (e) zero-count document types are omitted, not drawn as 0 bars
    only = ReportStore(os.path.join(tmp, "onlyvisits.db"))
    only.upsert_entity("activities", [{
        "activity_id": "v1",
        "visits": [{"activity_id": "v1", "duration_sec": 60}]}])
    out = _document_breakdown(only)
    assert "Visits" in out
    for absent in ("Orders", "Quotes", "Credit requests", "Emails"):
        assert absent not in out, f"{absent} has no rows and should be omitted"
    only.close()

    # (f) activity types are capped like the other rankings
    many = ReportStore(os.path.join(tmp, "many.db"))
    many.upsert_entity("activities", [
        {"activity_id": f"m{i}", "activity_type": f"Type {i}"}
        for i in range(40)])
    out = _activity_types(many, limit=12)
    assert out.count("</text>") <= 12 * 2 + 2, "should be capped"
    assert "Showing the top 12" in out
    many.close()

    # (g) a read error is reported, not silently dropped from the chart
    class BrokenStore(ReportStore):
        def scalar(self, sql, params=(), default=0):
            if "quote_totals" in sql:
                raise sqlite3.OperationalError("database is locked")
            return super().scalar(sql, params, default)

    broken = BrokenStore(os.path.join(tmp, "broken.db"))
    broken.upsert_entity("activities", [{
        "activity_id": "b1",
        "orderTotals": [{"order_id": "o1", "subtotal_value": 5.0}],
        "orders": [{"order_item_id": "oi1", "order_id": "o1"}]}])
    out = _document_breakdown(broken)
    assert "Could not read" in out and "Quotes" in out, \
        "a failed series must be named, not silently vanish"
    broken.close()

    # --- self-contained: nothing loads from the network ---
    assert "http://" not in dash and "https://" not in dash, \
        "dashboard must not reference any external resource"
    for token in ("<script", "src=", "@import", "<link"):
        assert token not in dash, f"unexpected external/scripted content: {token}"

    # --- instance text is escaped ---
    store.upsert_entity("customers", [
        {"customer_id": "cx", "name": "<script>alert('x')</script>",
         "code": "X", "is_active": True}])
    store.upsert_entity("activities", [{
        "activity_id": "ax", "customer_id": "cx",
        "customer_name": "<script>alert('x')</script>",
        "display_name": "Rep <b>Bold</b>", "start_time": "2026-08-13T09:00:00",
        "orderTotals": [{"order_id": "ox", "date": "2026-08-13",
                         "subtotal_value": 999.0}],
    }])
    escaped_path = build_dashboard(store, os.path.join(tmp, "escaped.html"))
    escaped = open(escaped_path, encoding="utf-8").read()
    assert "<script>alert" not in escaped, "must not emit raw script tags"
    assert "&lt;script&gt;" in escaped, "should appear escaped instead"
    assert "<b>Bold</b>" not in escaped

    store.close()
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("All dashboard tests passed")
