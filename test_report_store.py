"""Offline tests for the SQLite extract store: schema from the registry,
idempotent upserts, camelCase normalisation, nested sub-entity storage,
period-scoped bookmarks, run history and reconcile. In-memory - no network,
no files left behind."""

from skynamo_geo.report_store import (
    ReportStore, normalise_key, normalise_row, default_store_path,
)
from skynamo_geo.reporting_config import REPORTING_ENTITIES

# --- key normalisation (camelCase schemas vs snake_case ones) ---
assert normalise_key("customerId") == "customer_id"
assert normalise_key("isOnsite") == "is_onsite"
assert normalise_key("customer_id") == "customer_id"
assert normalise_key("RowVersion") == "row_version"
assert normalise_key("rowVersion") == "row_version"
assert normalise_key("") == ""
assert normalise_row({"customerId": 1, "name": "x"}) == {
    "customer_id": 1, "name": "x"}

# default path lives beside the settings file
assert default_store_path().endswith("reporting.db")

store = ReportStore(":memory:")

# --- schema: a table per root and per sub-entity ---
tables = {r[0] for r in store.query(
    "SELECT name FROM sqlite_master WHERE type='table'")}
for entity, spec in REPORTING_ENTITIES.items():
    assert spec["table"] in tables, entity
    for sub in (spec.get("sub_entities") or {}).values():
        assert sub["table"] in tables, sub["table"]
assert "bookmarks" in tables and "runs" in tables

# --- upsert is idempotent on the primary key ---
rows = [{"product_id": "p1", "name": "Widget", "code": "W1", "is_active": True}]
store.upsert_entity("products", rows)
store.upsert_entity("products", rows)          # same row again
assert store.scalar("SELECT COUNT(*) FROM products") == 1
assert store.scalar("SELECT is_active FROM products WHERE product_id='p1'") == 1

# an update changes the row rather than adding one
store.upsert_entity("products", [
    {"product_id": "p1", "name": "Widget v2", "code": "W1", "is_active": False}])
assert store.scalar("SELECT COUNT(*) FROM products") == 1
assert store.scalar("SELECT name FROM products WHERE product_id='p1'",
                    default="") == "Widget v2"
assert store.scalar("SELECT is_active FROM products WHERE product_id='p1'") == 0

# rows without a primary key value are skipped, not stored under NULL
before = store.scalar("SELECT COUNT(*) FROM products")
store.upsert_entity("products", [{"name": "no id"}, {"product_id": "", "name": "blank"}])
assert store.scalar("SELECT COUNT(*) FROM products") == before

# unknown extra keys in a payload are ignored (API may return more than we declare)
store.upsert_entity("products", [
    {"product_id": "p2", "name": "Extra", "surprise_field": "ignored"}])
assert store.scalar("SELECT COUNT(*) FROM products") == before + 1

# --- nested sub-entities are stored and linked to their parent ---
activity = {
    "activity_id": "a1", "activity_type": "Visit", "customer_id": "c1",
    "customer_name": "Acme", "user_id": "u1", "display_name": "Rep One",
    "start_time": "2026-07-01T08:00:00",
    "visits": [{"activity_id": "a1", "duration_sec": 1800, "is_onsite": True}],
    "orderTotals": [{"order_id": "o1", "date": "2026-07-01",
                     "subtotal_value": 100.0, "tax_value": 15.0}],
    "orders": [
        {"order_item_id": "oi1", "order_id": "o1", "product_code": "W1",
         "quantity": 2, "item_subtotal_value": 100.0},
    ],
}
written = store.upsert_entity("activities", [activity])
assert written["activities"] == 1
assert written["activity_visits"] == 1
assert written["order_totals"] == 1
assert written["order_items"] == 1
# parent_key backfilled onto children that omit it
assert store.scalar("SELECT activity_id FROM order_totals WHERE order_id='o1'",
                    default="") == "a1"
assert store.scalar("SELECT is_onsite FROM activity_visits "
                    "WHERE activity_id='a1'") == 1

# camelCase sub-entity payloads normalise on the way in
store.upsert_entity("activities", [{
    "activityId": "a2", "activityType": "Call",
    "visits": [{"activityId": "a2", "durationSec": 60, "isOnsite": False}],
}])
assert store.scalar("SELECT COUNT(*) FROM activities WHERE activity_id='a2'") == 1
assert store.scalar("SELECT duration_sec FROM activity_visits "
                    "WHERE activity_id='a2'") == 60

# a nested value that is not a list is ignored rather than raising
store.upsert_entity("activities", [{"activity_id": "a3", "visits": None}])
assert store.scalar("SELECT COUNT(*) FROM activities WHERE activity_id='a3'") == 1

# --- bookmarks are scoped to (endpoint, reporting period) ---
assert store.get_bookmark("/v2/customers", "ThisMonth") is None
store.set_bookmark("/v2/customers", "ThisMonth", "884")
assert store.get_bookmark("/v2/customers", "ThisMonth") == "884"
# a different period must NOT see it - reusing one across periods is meaningless
assert store.get_bookmark("/v2/customers", "PrevMonth") is None
store.set_bookmark("/v2/customers", "PrevMonth", "991")
assert store.get_bookmark("/v2/customers", "ThisMonth") == "884"
assert store.get_bookmark("/v2/customers", "PrevMonth") == "991"
# updating in place
store.set_bookmark("/v2/customers", "ThisMonth", "885")
assert store.get_bookmark("/v2/customers", "ThisMonth") == "885"
# an empty bookmark is not recorded
store.set_bookmark("/v2/products", "", None)
assert store.get_bookmark("/v2/products", "") is None

# --- run history ---
store.record_run("customers", "ThisMonth", "full", 12, "range-a", "extracted",
                 started_at="2026-08-27T09:00:00")
store.record_run("customers", "ThisMonth", "delta", 3, "range-b", "extracted",
                 started_at="2026-08-27T10:00:00")
store.record_run("products", "", "full", 5, "", "extracted")
latest = store.last_runs()
assert latest["customers"]["rows"] == 3, "most recent run per entity"
assert latest["customers"]["mode"] == "delta"
assert latest["products"]["rows"] == 5

# --- counts ---
counts = store.counts()
assert counts["products"] == 2
assert counts["activities"] == 3
assert counts["order_items"] == 1

# --- reconcile soft-deletes only the keys absent from a full sweep ---
removed = store.reconcile("products", "product_id", ["p1"])   # p2 now gone
assert removed == 1
assert store.scalar("SELECT is_deleted FROM products WHERE product_id='p2'") == 1
assert store.scalar("SELECT is_deleted FROM products WHERE product_id='p1'") == 0
# counts exclude soft-deleted rows
assert store.counts()["products"] == 1
# rows are retained, not dropped
assert store.scalar("SELECT COUNT(*) FROM products") == 2

store.close()
print("All report store tests passed")
