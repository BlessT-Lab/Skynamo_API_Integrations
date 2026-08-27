"""Offline tests for the reporting extract engine: plan phase touches no
network, delta vs full is decided from stored bookmarks, the rate-limit budget
warning fires, and run_extract commits rows before advancing the bookmark.
Fake client + in-memory store - no network."""

from skynamo_geo import report_engine
from skynamo_geo.report_engine import (
    ExtractPlan, MODE_DELTA, MODE_FULL, plan_extract, run_extract, rate_limit_for,
)
from skynamo_geo.report_store import ReportStore
from skynamo_geo.reporting_config import (
    STATUS_RPT_EXTRACTED, STATUS_RPT_FAILED, STATUS_RPT_PENDING,
    STATUS_RPT_SKIPPED,
)


class FakeClient:
    """Records fetches; returns scripted rows/bookmarks/errors per entity."""

    def __init__(self, rows_by_entity=None, errors=None, bookmarks=None):
        self.fetches = []
        self.rows_by_entity = rows_by_entity or {}
        self.errors = errors or {}
        self.bookmarks = bookmarks or {}

    def fetch(self, entity, reporting_period=None, bookmark=None,
              sub_entities=None, limit=None):
        self.fetches.append({
            "entity": entity, "reporting_period": reporting_period,
            "bookmark": bookmark,
        })
        if entity in self.errors:
            return [], None, "", self.errors[entity]
        rows = self.rows_by_entity.get(entity, [])
        return (rows, self.bookmarks.get(entity),
                f"range-for-{entity}", "")


# --- rate_limit_for reflects the published tiers ---
assert rate_limit_for("Prev30Days") == (30, 30)
assert rate_limit_for("This90Days") == (4, 60)
assert rate_limit_for("FinThisYear") == (4, 600)
assert rate_limit_for("AllData") == (2, 600)
assert rate_limit_for("NotAPeriod") == (2, 600)   # conservative fallback

# --- plan phase: NO network calls at all ---
store = ReportStore(":memory:")
client = FakeClient()
plans = plan_extract(store, ["customers", "products"], "ThisMonth")
assert client.fetches == [], "plan_extract must not touch the client"
by_entity = {p.entity: p for p in plans}
assert by_entity["customers"].status == STATUS_RPT_PENDING
assert by_entity["customers"].include is True
assert by_entity["customers"].mode == MODE_FULL      # no bookmark stored yet
assert by_entity["customers"].reporting_period == "ThisMonth"
# products has no reporting period, so it carries none
assert by_entity["products"].reporting_period == ""

# a stored bookmark switches that entity to delta
store.set_bookmark("/v2/customers", "ThisMonth", "884")
plans2 = plan_extract(store, ["customers"], "ThisMonth")
assert plans2[0].mode == MODE_DELTA and plans2[0].bookmark == "884"
# ...but only for the period it was issued against
plans3 = plan_extract(store, ["customers"], "PrevMonth")
assert plans3[0].mode == MODE_FULL and plans3[0].bookmark is None

# users has no bookmark support -> always full, and says why
plans4 = plan_extract(store, ["users"], "ThisMonth")
assert plans4[0].mode == MODE_FULL
assert "no bookmark" in plans4[0].notes

# unknown entity is skipped, not fatal
plans5 = plan_extract(store, ["nope"], "ThisMonth")
assert plans5[0].status == STATUS_RPT_SKIPPED and plans5[0].include is False

# sub-entity expansion is mentioned so the cost is visible
assert "sub-entity" in by_entity["customers"].notes

# --- rate-limit budget warning ---
# AllData allows 2 queries/600s; 5 entities cannot fit.
wide = plan_extract(store, ["activities", "customers", "users", "products",
                            "invoices"], "AllData")
billed = [p for p in wide if p.reporting_period]
assert billed, "some entities are billed against the period"
assert all("exceeds the 'AllData' allowance" in p.notes for p in billed)
# products is not billed (no period) so it is not warned
prod = next(p for p in wide if p.entity == "products")
assert "exceeds" not in prod.notes

# A generous period with few entities produces no warning.
narrow = plan_extract(store, ["customers"], "Prev30Days")
assert "exceeds" not in narrow[0].notes

# --- run_extract: stores rows, then the bookmark ---
store2 = ReportStore(":memory:")
client2 = FakeClient(
    rows_by_entity={
        "products": [{"product_id": "p1", "name": "Widget", "code": "W1"}],
        "customers": [{"customer_id": "c1", "name": "Acme", "code": "A1"}],
    },
    bookmarks={"products": "777", "customers": "888"})
plans6 = plan_extract(store2, ["products", "customers"], "ThisMonth")
rows = run_extract(client2, store2, plans6, started_at="2026-08-27T09:00:00")

assert len(client2.fetches) == 2
done = {p.entity: p for p in plans6}
assert done["products"].status == STATUS_RPT_EXTRACTED
assert done["products"].rows == 1
assert done["products"].date_range == "range-for-products"
assert store2.scalar("SELECT COUNT(*) FROM products") == 1
assert store2.scalar("SELECT COUNT(*) FROM customers") == 1
# bookmarks advanced, keyed correctly
assert store2.get_bookmark("/v2/products", "") == "777"
assert store2.get_bookmark("/v2/customers", "ThisMonth") == "888"
# run history recorded for every plan
assert len(store2.query("SELECT * FROM runs")) == 2
# report covers every plan
assert len(rows) == len(plans6)
assert {r["entity"] for r in rows} == {"products", "customers"}

# --- a failed entity does not abort the run, and does NOT advance its bookmark ---
store3 = ReportStore(":memory:")
client3 = FakeClient(
    rows_by_entity={"customers": [{"customer_id": "c9", "name": "Later"}]},
    errors={"products": "HTTP 500: boom"},
    bookmarks={"products": "999", "customers": "111"})
plans7 = plan_extract(store3, ["products", "customers"], "ThisMonth")
run_extract(client3, store3, plans7)
res = {p.entity: p for p in plans7}
assert res["products"].status == STATUS_RPT_FAILED
assert "boom" in res["products"].notes
assert res["customers"].status == STATUS_RPT_EXTRACTED, "run kept going"
assert store3.get_bookmark("/v2/products", "") is None, \
    "a failed extract must not advance its bookmark"
assert store3.get_bookmark("/v2/customers", "ThisMonth") == "111"

# --- a payload the store rejects is reported, not raised ---
class BadStore(ReportStore):
    def upsert_entity(self, entity, rows):
        raise ValueError("bad payload")

store4 = BadStore(":memory:")
client4 = FakeClient(rows_by_entity={"products": [{"product_id": "p1"}]},
                     bookmarks={"products": "555"})
plans8 = plan_extract(store4, ["products"], "ThisMonth")
run_extract(client4, store4, plans8)
assert plans8[0].status == STATUS_RPT_FAILED
assert "store error" in plans8[0].notes
assert store4.get_bookmark("/v2/products", "") is None

# --- deselected plans are not fetched ---
store5 = ReportStore(":memory:")
client5 = FakeClient(rows_by_entity={"products": [{"product_id": "p1"}]})
plans9 = plan_extract(store5, ["products", "customers"], "ThisMonth")
for p in plans9:
    if p.entity == "customers":
        p.include = False
run_extract(client5, store5, plans9)
assert [f["entity"] for f in client5.fetches] == ["products"]

# --- cancel stops the run early ---
store6 = ReportStore(":memory:")
client6 = FakeClient(rows_by_entity={})
plans10 = plan_extract(store6, ["products", "customers", "users"], "ThisMonth")
calls = {"n": 0}
def cancel():
    calls["n"] += 1
    return calls["n"] > 1
run_extract(client6, store6, plans10, should_cancel=cancel)
assert len(client6.fetches) <= 1

# --- summarize / report columns ---
counts = report_engine.summarize(plans6)
assert counts.get(STATUS_RPT_EXTRACTED) == 2
row = plans6[0].to_report_row()
assert set(row) == {"entity", "mode", "reporting_period", "rows",
                    "date_range", "status", "notes"}

print("All report engine tests passed")
