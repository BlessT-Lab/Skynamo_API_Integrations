"""Construct the GUI, pump the event loop briefly, then close it.
Verifies the whole widget tree builds without error (no real interaction).

The store path is redirected to a temp location throughout, so this test never
reads or creates the real %APPDATA% store and gives the same result on a clean
machine as on one that has already run an extract.
"""

import os
import shutil
import tempfile

import gui
from skynamo_geo.report_store import ReportStore
from skynamo_geo.reporting_config import REPORTING_ENTITIES

tmp = tempfile.mkdtemp(prefix="skynamo_gui_smoke_")
real_default_store_path = gui.default_store_path
try:
    # ---- Part 1: no store yet -------------------------------------------
    missing = os.path.join(tmp, "does_not_exist", "reporting.db")
    gui.default_store_path = lambda: missing

    app = gui.App()
    app.update_idletasks()
    app.update()

    # All five tabs build
    assert app.tabview.tab("Customer Geolocation") is not None
    assert app.tabview.tab("Product Images") is not None
    assert app.tabview.tab("Manage Images") is not None
    assert app.tabview.tab("Reporting") is not None
    assert app.tabview.tab("Dashboards") is not None
    # Geolocation tab: key widgets exist and start disabled
    assert app.preview_btn.cget("state") == "disabled"
    assert app.write_btn.cget("state") == "disabled"
    assert app.cancel_btn.cget("state") == "disabled"
    assert app.tree is not None
    # Product Images tab: key widgets exist and start disabled
    assert app.img_preview_btn.cget("state") == "disabled"
    assert app.img_upload_btn.cget("state") == "disabled"
    assert app.img_cancel_btn.cget("state") == "disabled"
    assert app.img_tree is not None
    assert app.img_replace_var.get() is False   # replace mode off by default
    # Manage Images tab: key widgets exist and start disabled
    assert app.mgmt_delete_btn.cget("state") == "disabled"
    assert app.mgmt_cancel_btn.cget("state") == "disabled"
    assert app.mgmt_tree is not None
    # Reporting tab: key widgets exist and start disabled
    assert app.rpt_plan_btn.cget("state") == "disabled"
    assert app.rpt_run_btn.cget("state") == "disabled"
    assert app.rpt_cancel_btn.cget("state") == "disabled"
    assert app.rpt_tree is not None
    # every registry entity gets a checkbox, and the rate-limit hint is rendered
    assert set(app.rpt_entity_vars) == set(REPORTING_ENTITIES)
    assert "allows" in app.rpt_hint_label.cget("text")
    # Dashboards tab: build available, open disabled until something is built
    assert app.dash_build_btn.cget("state") == "normal"
    assert app.dash_open_btn.cget("state") == "disabled"
    # with no store, both panels say so and point at the Reporting tab
    assert "Reporting tab" in app.dash_info.get("1.0", "end")
    assert "created on the first extract" in app.rpt_store_label.cget("text")
    # ...and nothing was created just to render those labels
    assert not os.path.exists(missing), "must not create the store to render"

    # ---- Part 2: a store that already has rows --------------------------
    # Re-point the path and re-render on the same app (one Tk root; creating a
    # second is needlessly fragile).
    seeded = os.path.join(tmp, "seeded.db")
    store = ReportStore(seeded)
    store.upsert_entity("products", [
        {"product_id": "p1", "name": "Widget", "code": "W1"}])
    store.record_run("products", "", "full", 1, "2026-08-01..2026-08-31",
                     "extracted", started_at="2026-08-27T09:00:00")
    store.close()
    gui.default_store_path = lambda: seeded

    app.refresh_dash_info()
    app._rpt_refresh_store_label()
    app.update_idletasks()

    info = app.dash_info.get("1.0", "end")
    assert "products" in info and "Last extract per entity" in info, info
    assert "Reporting tab" not in info, "should not nag when a store exists"
    assert "products=1" in app.rpt_store_label.cget("text"), \
        app.rpt_store_label.cget("text")
    app.destroy()
finally:
    gui.default_store_path = real_default_store_path
    shutil.rmtree(tmp, ignore_errors=True)

print("GUI smoke test passed")
