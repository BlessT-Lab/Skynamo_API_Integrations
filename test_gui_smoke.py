"""Construct the GUI, pump the event loop briefly, then close it.
Verifies the whole widget tree builds without error (no real interaction)."""

import gui

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
from skynamo_geo.reporting_config import REPORTING_ENTITIES
assert set(app.rpt_entity_vars) == set(REPORTING_ENTITIES)
assert "allows" in app.rpt_hint_label.cget("text")
# Dashboards tab: build available, open disabled until something is built
assert app.dash_build_btn.cget("state") == "normal"
assert app.dash_open_btn.cget("state") == "disabled"
# with no store yet, the info panel points the user at the Reporting tab
assert "Reporting tab" in app.dash_info.get("1.0", "end")
app.destroy()
print("GUI smoke test passed")
