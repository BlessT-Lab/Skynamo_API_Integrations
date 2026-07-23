"""Construct the GUI, pump the event loop briefly, then close it.
Verifies the whole widget tree builds without error (no real interaction)."""

import gui

app = gui.App()
app.update_idletasks()
app.update()
# All three tabs build
assert app.tabview.tab("Customer Geolocation") is not None
assert app.tabview.tab("Product Images") is not None
assert app.tabview.tab("Manage Images") is not None
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
assert app.img_replace_var.get() is False   # replace-existing checkbox exists
# Manage Images tab: key widgets exist and start disabled
assert app.mgmt_delete_btn.cget("state") == "disabled"
assert app.mgmt_cancel_btn.cget("state") == "disabled"
assert app.mgmt_tree is not None
app.destroy()
print("GUI smoke test passed")
