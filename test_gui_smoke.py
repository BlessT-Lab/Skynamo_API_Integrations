"""Construct the GUI, pump the event loop briefly, then close it.
Verifies the whole widget tree builds without error (no real interaction).

Both the store path and the saved config are redirected, so this test never
reads or creates anything in the real %APPDATA% and gives the same result on a
clean machine as on one that has already run an extract and saved its
preferences. Asserting a widget's default against whatever the developer last
ticked would fail on their machine and pass in CI, or the reverse.
"""

import json
import os
import shutil
import tempfile

import gui
from skynamo_geo.report_store import ReportStore
from skynamo_geo.reporting_config import REPORTING_ENTITIES

tmp = tempfile.mkdtemp(prefix="skynamo_gui_smoke_")
real_default_store_path = gui.default_store_path
real_load_config = gui.settings.load_config
real_save_config = gui.settings.save_config
real_config_dir = gui.settings._config_dir
# A config that round-trips like the real file, held in memory.
disk = {"cfg": {}}
def fake_load():
    return json.loads(json.dumps(disk["cfg"]))
def fake_save(cfg):
    disk["cfg"] = json.loads(json.dumps(cfg))
    return "config.json"
try:
    # The startup scrub looks in the data folder; keep it out of %APPDATA%.
    gui.settings._config_dir = lambda: os.path.join(tmp, "appdata")
    gui.settings.save_config = fake_save
    # ---- Part 1: no store, no saved config ------------------------------
    missing = os.path.join(tmp, "does_not_exist", "reporting.db")
    gui.default_store_path = lambda: missing
    gui.settings.load_config = fake_load

    app = gui.App()
    app.update_idletasks()
    app.update()

    # All five tabs build
    assert app.tabview.tab("Customer Geolocation") is not None
    assert app.tabview.tab("Product Images") is not None
    assert app.tabview.tab("Manage Images") is not None
    assert app.tabview.tab("Reporting") is not None
    assert app.tabview.tab("Dashboards") is not None
    assert app.tabview.tab("Route Planning") is not None
    # Route Planning: nothing to plan or save until data is loaded
    for btn in (app.route_plan_btn, app.route_map_btn, app.route_csv_btn,
                app.route_open_btn, app.route_cancel_btn):
        assert btn.cget("state") == "disabled"
    assert app.route_connect_btn.cget("state") == "normal"
    # the default week starts on a Monday
    from datetime import date as _date
    assert _date.fromisoformat(app.route_start_entry.get()).weekday() == 0
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
    assert app.img_move_var.get() is False      # filing is opt-in
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
    # with no store, both panels say so and point at the Reporting section
    assert "Go to Reporting" in app.dash_info.get("1.0", "end")
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
    assert "Go to Reporting" not in info, "should not nag when a store exists"
    assert "products=1" in app.rpt_store_label.cget("text"), \
        app.rpt_store_label.cget("text")
    # ---- Part 3: a saved config is honoured -----------------------------
    # Reload settings into the same app; a second Tk root is needlessly fragile.
    # It was saved by an older version, so it carries the instance name in the
    # clear - as a setting, and as the key of the remembered start points.
    disk["cfg"] = {
        "instance_name": "acme",
        "route_reps": {"acme": {"7": {"address": "1 Main Rd", "include": True,
                                      "lat": -26.1, "lng": 28.0}}},
        "image_replace_existing": True,
        "image_move_processed": True,
        "route_options": {"n_days": "4", "max_stops": "9",
                          "weekdays": [0, 1, 2, 3, 5],
                          "holidays": "2026-12-25", "count_any_rep": True},
    }
    app._load_saved_settings()
    app.update_idletasks()
    assert app.img_replace_var.get() is True
    assert app.img_move_var.get() is True
    # The instance name is never loaded back...
    for entry in (app.instance_entry, app.img_instance_entry,
                  app.route_instance_entry):
        assert entry.get() == "", entry.get()
    # ...and the startup scrub took it off the disk, keeping the start points
    # under a fingerprint that the same name finds again.
    assert "acme" not in json.dumps(disk["cfg"]), disk["cfg"]
    key = gui.settings.instance_key("ACME ", disk["cfg"])
    assert disk["cfg"]["route_reps"][key]["7"]["address"] == "1 Main Rd"
    assert gui.settings.is_instance_key(key)
    assert app.route_days_entry.get() == "4"
    assert app.route_stops_entry.get() == "9"
    assert [v.get() for v in app.route_weekday_vars] ==         [True, True, True, True, False, True, False]
    assert app.route_holidays_entry.get() == "2026-12-25"
    assert app.route_anyrep_var.get() is True
    # the form reads back into valid options
    options, err = app._route_options()
    assert err == "" and options.n_days == 4 and options.max_stops == 9

    # ---- Part 4: saving never writes the instance name -------------------
    app.remember_var.set(True)
    for entry in (app.instance_entry, app.img_instance_entry,
                  app.route_instance_entry):
        entry.insert(0, "acme")
    app.route_instance = "acme"
    rep_entry = gui.ctk.CTkEntry(app)
    rep_entry.insert(0, "2 Side Rd")
    app.route_rep_rows = {7: {"name": "Rep", "var": gui.ctk.BooleanVar(value=True),
                              "entry": rep_entry, "status": None}}
    app._persist_settings()
    app._persist_image_settings()
    app._persist_route_settings()
    assert "acme" not in json.dumps(disk["cfg"]).lower(), disk["cfg"]
    # the start point is still remembered, under the same fingerprint
    assert disk["cfg"]["route_reps"][key]["7"]["address"] == "2 Side Rd"

    app.destroy()
finally:
    gui.default_store_path = real_default_store_path
    gui.settings.load_config = real_load_config
    gui.settings.save_config = real_save_config
    gui.settings._config_dir = real_config_dir
    shutil.rmtree(tmp, ignore_errors=True)

print("GUI smoke test passed")
