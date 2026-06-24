"""Construct the GUI, pump the event loop briefly, then close it.
Verifies the whole widget tree builds without error (no real interaction)."""

import gui

app = gui.App()
app.update_idletasks()
app.update()
# Confirm key widgets exist and initial states are right
assert app.preview_btn.cget("state") == "disabled"
assert app.write_btn.cget("state") == "disabled"
assert app.cancel_btn.cget("state") == "disabled"
assert app.tree is not None
app.destroy()
print("GUI smoke test passed")
