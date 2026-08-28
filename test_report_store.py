"""Offline tests for the SQLite extract store: schema from the registry,
idempotent upserts, camelCase normalisation, nested sub-entity storage,
period-scoped bookmarks, run history and reconcile. In-memory - no network,
no files left behind."""

import os
import shutil
import sqlite3
import tempfile
import threading

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

# --- thread safety -------------------------------------------------------
# Regression: the GUI creates the store on the connect worker thread, then uses
# it from the main thread (labels/dashboard) and from other worker threads
# (plan/run extract). A default sqlite3 connection is bound to its creating
# thread, which raised "SQLite objects created in a thread can only be used in
# that same thread". These tests exercise exactly that pattern.
import queue as _queue
import threading

tmpdir = tempfile.mkdtemp(prefix="skynamo_store_threads_")
try:
    db = os.path.join(tmpdir, "threaded.db")

    # (a) created on one thread, used on another - the reported failure
    created = _queue.Queue()

    def build():
        created.put(ReportStore(db))

    t = threading.Thread(target=build)
    t.start()
    t.join()
    remote = created.get()

    # this main-thread access is what used to raise
    remote.upsert_entity("products", [{"product_id": "p1", "name": "W"}])
    assert remote.counts()["products"] == 1
    remote.record_run("products", "", "full", 1, "", "extracted")
    assert "products" in remote.last_runs()
    remote.set_bookmark("/v2/products", "", "bm-1")
    assert remote.get_bookmark("/v2/products", "") == "bm-1"

    # (b) and used from yet another thread, as plan/run extract would
    errors = []
    results = {}

    def worker():
        try:
            results["bookmark"] = remote.get_bookmark("/v2/products", "")
            remote.upsert_entity("products", [{"product_id": "p2", "name": "G"}])
            results["counts"] = remote.counts()["products"]
        except Exception as exc:            # noqa: BLE001 - recording it is the test
            errors.append(exc)

    t2 = threading.Thread(target=worker)
    t2.start()
    t2.join()
    assert not errors, f"cross-thread use must not raise: {errors}"
    assert results["bookmark"] == "bm-1"
    assert results["counts"] == 2

    # (c) concurrent readers and writers stay consistent under the lock
    errors.clear()

    def hammer(n):
        try:
            for i in range(25):
                remote.upsert_entity("products", [
                    {"product_id": f"t{n}-{i}", "name": f"P{n}-{i}"}])
                remote.counts()
                remote.query("SELECT COUNT(*) FROM products")
        except Exception as exc:            # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors, f"concurrent access must not raise: {errors}"
    # 2 from earlier + 4 threads x 25 rows, none lost or duplicated
    assert remote.counts()["products"] == 2 + 4 * 25, remote.counts()["products"]

    # (d) upsert_entity nests upsert(), so the lock must be re-entrant
    #     (a plain Lock would deadlock here - this call proves it does not)
    remote.upsert_entity("activities", [{
        "activity_id": "a1",
        "visits": [{"activity_id": "a1", "duration_sec": 60}],
    }])
    assert remote.counts()["activity_visits"] == 1

    remote.close()
finally:
    shutil.rmtree(tmpdir, ignore_errors=True)

# --- upsert_entity is one transaction -----------------------------------
# A failure part way through a sub-entity must not leave the root rows behind.
tmpdir2 = tempfile.mkdtemp(prefix="skynamo_store_atomic_")
try:
    atomic = ReportStore(os.path.join(tmpdir2, "atomic.db"))

    good = {
        "activity_id": "ok1", "activity_type": "Visit",
        "visits": [{"activity_id": "ok1", "duration_sec": 30}],
    }
    atomic.upsert_entity("activities", [good])
    assert atomic.scalar("SELECT COUNT(*) FROM activities") == 1

    # Make the SECOND table's write blow up, after the root has been written.
    original = atomic._write_rows
    calls = {"n": 0}

    def exploding(table, primary_key, columns, rows):
        calls["n"] += 1
        if calls["n"] == 2:            # the sub-entity write
            raise sqlite3.OperationalError("simulated failure")
        return original(table, primary_key, columns, rows)

    atomic._write_rows = exploding
    try:
        atomic.upsert_entity("activities", [{
            "activity_id": "bad1", "activity_type": "Visit",
            "visits": [{"activity_id": "bad1", "duration_sec": 60}],
        }])
        raise AssertionError("expected the simulated failure to propagate")
    except sqlite3.OperationalError:
        pass
    finally:
        atomic._write_rows = original

    # The root row must have been rolled back with its children.
    assert atomic.scalar(
        "SELECT COUNT(*) FROM activities WHERE activity_id='bad1'") == 0, \
        "root rows must roll back when a sub-entity write fails"
    assert atomic.scalar(
        "SELECT COUNT(*) FROM activity_visits WHERE activity_id='bad1'") == 0
    # ...and the earlier good data is untouched
    assert atomic.scalar("SELECT COUNT(*) FROM activities") == 1
    atomic.close()
finally:
    shutil.rmtree(tmpdir2, ignore_errors=True)

# --- a read does not wait behind a long write ---------------------------
# Regression: reads used to take the same lock as writes, so a label refresh on
# the Tk main thread could freeze the UI for the length of a bulk upsert.
tmpdir3 = tempfile.mkdtemp(prefix="skynamo_store_nonblock_")
try:
    nb = ReportStore(os.path.join(tmpdir3, "nonblock.db"))
    assert not nb._memory

    holding = threading.Event()
    release = threading.Event()
    read_done = threading.Event()

    def long_write():
        # Hold the write lock, as a bulk upsert does.
        with nb._lock:
            holding.set()
            release.wait(timeout=10)

    writer = threading.Thread(target=long_write)
    writer.start()
    assert holding.wait(timeout=5), "writer never acquired the lock"

    def reader():
        nb.counts()          # must not block on the held write lock
        nb.last_runs()
        nb.get_bookmark("/v2/products", "")
        read_done.set()

    r = threading.Thread(target=reader)
    r.start()
    assert read_done.wait(timeout=5), \
        "a read blocked while the write lock was held - the GUI would freeze"
    release.set()
    writer.join(timeout=5)
    r.join(timeout=5)

    # an in-memory store cannot reopen, so it legitimately shares the lock
    mem = ReportStore(":memory:")
    assert mem._memory and mem.counts() is not None
    mem.close()
    nb.close()
finally:
    shutil.rmtree(tmpdir3, ignore_errors=True)

# --- synthetic keys for sub-entities the API gives no key ---------------
# Comments and emails on an activity have no unique field. Keying them on
# activity_id would make each one overwrite the last.
from skynamo_geo.report_store import synthetic_key

assert synthetic_key({"a": "1", "b": "2"}, ["a", "b"]) == \
    synthetic_key({"a": "1", "b": "2"}, ["a", "b"]), "must be deterministic"
assert synthetic_key({"a": "1"}, ["a"]) != synthetic_key({"a": "2"}, ["a"])
# a missing field is treated as empty rather than raising
assert synthetic_key({}, ["a", "b"])
# the separator prevents ("ab","c") colliding with ("a","bc")
assert synthetic_key({"a": "ab", "b": "c"}, ["a", "b"]) != \
    synthetic_key({"a": "a", "b": "bc"}, ["a", "b"])

tmpdir4 = tempfile.mkdtemp(prefix="skynamo_store_synthetic_")
try:
    syn = ReportStore(os.path.join(tmpdir4, "syn.db"))
    activity = {
        "activity_id": "a1", "activity_type": "Visit",
        "comments": [
            {"activity_id": "a1", "date": "2026-08-01", "customer_comment": "first"},
            {"activity_id": "a1", "date": "2026-08-02", "customer_comment": "second"},
            {"activity_id": "a1", "date": "2026-08-03", "customer_comment": "third"},
        ],
        "emails": [
            {"activity_id": "a1", "date": "2026-08-01",
             "recipients": "a@b.c", "description": "quote sent"},
            {"activity_id": "a1", "date": "2026-08-02",
             "recipients": "d@e.f", "description": "follow up"},
        ],
    }
    written = syn.upsert_entity("activities", [activity])
    assert written["activity_comments"] == 3, \
        f"all three comments must survive, got {written.get('activity_comments')}"
    assert written["activity_emails"] == 2
    assert syn.scalar("SELECT COUNT(*) FROM activity_comments") == 3
    # re-extracting the same payload must not duplicate
    syn.upsert_entity("activities", [activity])
    assert syn.scalar("SELECT COUNT(*) FROM activity_comments") == 3, \
        "synthetic keys must make re-extraction idempotent"
    assert syn.scalar("SELECT COUNT(*) FROM activity_emails") == 2
    # every comment is linked back to its activity
    assert syn.scalar(
        "SELECT COUNT(*) FROM activity_comments WHERE activity_id='a1'") == 3
    syn.close()
finally:
    shutil.rmtree(tmpdir4, ignore_errors=True)

# --- all 11 activity document types round-trip --------------------------
tmpdir5 = tempfile.mkdtemp(prefix="skynamo_store_docs_")
try:
    docs = ReportStore(os.path.join(tmpdir5, "docs.db"))
    docs.upsert_entity("activities", [{
        "activity_id": "a9", "activity_type": "Order",
        "orderTotals": [{"order_id": "o1", "subtotal_value": 100.0}],
        "orders": [{"order_item_id": "oi1", "order_id": "o1", "quantity": 2}],
        "quoteTotals": [{"quote_id": "q1", "subtotal_value": 250.0}],
        "quotes": [{"quote_item_id": "qi1", "quote_id": "q1", "quantity": 5}],
        "creditRequestTotals": [{"credit_request_id": "cr1",
                                 "subtotal_value": 30.0}],
        "creditRequests": [{"credit_request_item_id": "cri1",
                            "credit_request_id": "cr1", "quantity": 1}],
        "surveys": [{"survey_item_id": "s1", "survey_id": "sv1",
                     "product_code": "W1", "stock_level": 12, "facings": 3}],
        "forms": [{"completed_form_id": "f1", "form_type": "Audit"}],
        "visits": [{"activity_id": "a9", "duration_sec": 900}],
        "comments": [{"activity_id": "a9", "date": "x", "customer_comment": "c"}],
        "emails": [{"activity_id": "a9", "date": "x", "recipients": "r",
                    "description": "d"}],
    }])
    counts = docs.counts()
    for table in ("order_totals", "order_items", "quote_totals", "quote_items",
                  "credit_request_totals", "credit_request_items", "surveys",
                  "activity_forms", "activity_visits", "activity_comments",
                  "activity_emails"):
        assert counts.get(table) == 1, f"{table}={counts.get(table)}"
    # the activity itself records what type it was
    assert docs.scalar("SELECT activity_type FROM activities WHERE "
                       "activity_id='a9'", default="") == "Order"
    docs.close()
finally:
    shutil.rmtree(tmpdir5, ignore_errors=True)

print("All report store tests passed")
