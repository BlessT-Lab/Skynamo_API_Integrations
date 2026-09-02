"""Offline tests for the CLI's product-image flows.

Drives skynamo_geolocation.run_image_import / run_manage_images with scripted
prompt answers and a fake client, asserting the things the engine tests can't:
that nothing uploads until the user confirms, that deselection is honoured,
that replace mode demands an extra confirmation, that filing the processed
files away is opt-in, and that the reports land.

No network, no real credentials; runs in a temp cwd so report CSVs don't
litter the repo.
"""

import io
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stdout

import skynamo_geolocation as cli
from skynamo_geo.config import (
    IMAGE_FOLDER_FAILED, IMAGE_FOLDER_SUCCESS,
    STATUS_IMG_PENDING, STATUS_IMG_UPLOADED, STATUS_ATT_DELETED,
    STATUS_ATT_LOADED,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class FakeClient:
    def __init__(self, products, upload_ok=True, attach_ok=True,
                 files_by_guid=None, require_product_id=False):
        self.products = products
        self.uploaded = []
        self.attached = []
        self.attach_keys = []      # (code, product_id) per attach call
        self._n = 0
        self.upload_ok = upload_ok
        self.attach_ok = attach_ok
        # a live instance rejects a ProductPatch with no id
        self.require_product_id = require_product_id
        self.files_by_guid = files_by_guid or {}

    def test_connection(self):
        return True, "Connected."

    def fetch_all_products(self, on_page=None, active_only=True):
        if on_page:
            on_page(len(self.products), len(self.products))
        return self.products

    def upload_file(self, filename, content_b64):
        self.uploaded.append(filename)
        if not self.upload_ok:
            return None, "upload boom"
        self._n += 1
        return f"guid-{self._n}", ""

    def attach_files(self, code, files, product_id=None):
        self.attached.append((code, list(files)))
        self.attach_keys.append((code, product_id))
        if not self.attach_ok:
            return False, "attach boom"
        if self.require_product_id and product_id is None:
            return False, "HTTP 400: id is required"
        return True, ""

    def get_file(self, guid):
        name = self.files_by_guid.get(guid)
        if name is None:
            return None, "file not found"
        return {"id": guid, "filename": name}, ""


class Script:
    """Scripted prompt answers, patched over the CLI's ask_* helpers."""

    def __init__(self, texts=(), confirms=(), checkboxes=(), selects=()):
        self.texts = list(texts)
        self.confirms = list(confirms)
        self.checkboxes = list(checkboxes)
        self.selects = list(selects)
        self.confirm_log = []

    def install(self):
        self._saved = (cli.ask_text, cli.ask_confirm, cli.ask_checkbox,
                       cli.ask_select)
        cli.ask_text = self._text
        cli.ask_confirm = self._confirm
        cli.ask_checkbox = self._checkbox
        cli.ask_select = self._select
        return self

    def restore(self):
        (cli.ask_text, cli.ask_confirm, cli.ask_checkbox,
         cli.ask_select) = self._saved

    def __enter__(self):
        return self.install()

    def __exit__(self, *_exc):
        self.restore()
        return False

    def _text(self, message, password=False):
        assert self.texts, f"unexpected ask_text: {message}"
        return self.texts.pop(0)

    def _confirm(self, message, default=False):
        assert self.confirms, f"unexpected ask_confirm: {message}"
        answer = self.confirms.pop(0)
        self.confirm_log.append((message, answer))
        return answer

    def _checkbox(self, message, choices):
        assert self.checkboxes, f"unexpected ask_checkbox: {message}"
        picker = self.checkboxes.pop(0)
        return picker(choices) if callable(picker) else picker

    def _select(self, message, choices):
        assert self.selects, f"unexpected ask_select: {message}"
        return self.selects.pop(0)


def make_products():
    return [
        {"id": 1, "code": "ABC", "name": "Alpha", "files": ["old-guid"]},
        {"id": 2, "code": "XYZ", "name": "Exwhyzed", "files": []},
    ]


def make_folder(names=("ABC.png", "XYZ.png", "NOPE.png")):
    tmp = tempfile.mkdtemp(prefix="skynamo_cli_img_")
    for name in names:
        with open(os.path.join(tmp, name), "wb") as f:
            f.write(PNG)
    return tmp


def run(fn, client, script):
    """Run a CLI flow with stdout captured; returns (output, SystemExit or None)."""
    buf = io.StringIO()
    raised = None
    with script, redirect_stdout(buf):
        try:
            fn(client)
        except SystemExit as exc:
            raised = exc
    return buf.getvalue(), raised


# ---------------------------------------------------------------------------

work = tempfile.mkdtemp(prefix="skynamo_cli_cwd_")
original_cwd = os.getcwd()
os.chdir(work)          # reports are written to cwd
folders = []
try:
    # --- feature routing ---
    assert cli.pick_feature(["geo"]) == "geo"
    assert cli.pick_feature(["images"]) == "images"
    assert cli.pick_feature(["manage"]) == "manage"
    assert cli.pick_feature(["--images"]) == "images"   # leading dashes ok
    assert cli.pick_feature(["IMAGES"]) == "images"     # case-insensitive
    assert set(cli.RUNNERS) == {"geo", "images", "manage"}
    # menu path returns the key from the chosen label
    with Script(selects=["images  Product image import - ..."]):
        assert cli.pick_feature([]) == "images"
    # unknown feature exits
    try:
        cli.pick_feature(["nope"])
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "Unknown feature" in str(exc)

    # --- import: happy path, upload everything ---
    folder = make_folder(); folders.append(folder)
    client = FakeClient(make_products())
    out, exc = run(cli.run_image_import, client, Script(
        texts=[folder],
        confirms=[True,     # upload all of these?
                  False,    # replace existing images?
                  False,    # move processed files into subfolders?
                  True],    # upload N now?
    ))
    assert exc is None, exc
    assert "MATCH SUMMARY" in out and "UPLOAD SUMMARY" in out
    assert sorted(client.uploaded) == ["ABC.png", "XYZ.png"]
    # merge mode keeps the pre-existing GUID on ABC
    abc = [a for a in client.attached if a[0] == "ABC"]
    assert abc and abc[0][1][0] == "old-guid", abc
    # the unmatched file is reported, never uploaded
    assert "NO MATCHING PRODUCT" in out and "NOPE.png" in out
    assert "NOPE.png" not in client.uploaded
    # existing-attachment count is surfaced before uploading
    assert "1 already attached" in out
    # progress must not print the transitional "pending-upload" as an outcome
    assert "-> sent" in out
    assert "-> pending-upload" not in out
    # a report landed in cwd
    reports = [f for f in os.listdir(work) if f.startswith("product_images_report")]
    assert reports, os.listdir(work)

    # --- import: the attach identifies the product by id ---
    folder = make_folder(); folders.append(folder)
    client = FakeClient(make_products(), require_product_id=True)
    out, exc = run(cli.run_image_import, client, Script(
        texts=[folder],
        confirms=[True, False, False, True],
    ))
    assert exc is None, exc
    assert "Uploaded" in out
    assert all(pid is not None for _c, pid in client.attach_keys), \
        client.attach_keys
    assert "upload-failed" not in out

    # --- import: declining the final confirm uploads NOTHING ---
    folder = make_folder(); folders.append(folder)
    client = FakeClient(make_products())
    out, exc = run(cli.run_image_import, client, Script(
        texts=[folder],
        confirms=[True,      # upload all
                  False,     # replace? no
                  False,     # move files? no
                  False],    # upload now? NO
    ))
    assert isinstance(exc, SystemExit) and "Aborted" in str(exc)
    assert client.uploaded == [], "preview must not upload"
    assert client.attached == []

    # --- import: individual selection is honoured ---
    folder = make_folder(); folders.append(folder)
    client = FakeClient(make_products())
    out, exc = run(cli.run_image_import, client, Script(
        texts=[folder],
        confirms=[False,     # upload all? no -> choose individually
                  False,     # replace? no
                  False,     # move files? no
                  True],     # upload now
        checkboxes=[lambda choices: [c for c in choices if c.startswith("XYZ")]],
    ))
    assert exc is None, exc
    assert client.uploaded == ["XYZ.png"], client.uploaded
    assert [a[0] for a in client.attached] == ["XYZ"]

    # --- import: selecting nothing aborts ---
    folder = make_folder(); folders.append(folder)
    client = FakeClient(make_products())
    out, exc = run(cli.run_image_import, client, Script(
        texts=[folder],
        confirms=[False],            # choose individually
        checkboxes=[[]],             # ...then pick none
    ))
    assert isinstance(exc, SystemExit) and "Nothing selected" in str(exc)
    assert client.uploaded == []

    # --- import: replace mode demands an extra confirmation ---
    folder = make_folder(); folders.append(folder)
    client = FakeClient(make_products())
    script = Script(
        texts=[folder],
        confirms=[True,      # upload all
                  True,      # replace existing? YES
                  True,      # continue with replace mode?
                  False,     # move files? no
                  True],     # upload now
    )
    out, exc = run(cli.run_image_import, client, script)
    assert exc is None, exc
    assert "REPLACE MODE will detach existing images from" in out
    assert "ABC: 1 existing image(s)" in out
    # replace mode drops the old GUID
    abc = [a for a in client.attached if a[0] == "ABC"]
    assert abc and "old-guid" not in abc[0][1], abc
    # the extra confirmation really was asked
    assert any("Continue with replace mode" in m for m, _a in script.confirm_log)

    # --- import: declining the replace warning aborts before uploading ---
    folder = make_folder(); folders.append(folder)
    client = FakeClient(make_products())
    out, exc = run(cli.run_image_import, client, Script(
        texts=[folder],
        confirms=[True,      # upload all
                  True,      # replace? yes
                  False],    # continue with replace mode? NO
    ))
    assert isinstance(exc, SystemExit) and "Aborted" in str(exc)
    assert client.uploaded == [], "must abort before any upload"

    # --- import: files stay put unless the move prompt is answered yes ---
    folder = make_folder(); folders.append(folder)
    client = FakeClient(make_products())
    out, exc = run(cli.run_image_import, client, Script(
        texts=[folder],
        confirms=[True,      # upload all
                  False,     # replace? no
                  False,     # move files? NO
                  True],     # upload now
    ))
    assert exc is None, exc
    assert sorted(os.listdir(folder)) == ["ABC.png", "NOPE.png", "XYZ.png"], \
        os.listdir(folder)
    assert "FILED:" not in out

    # --- import: saying yes files each image by its outcome ---
    folder = make_folder(); folders.append(folder)
    client = FakeClient(make_products())
    script = Script(
        texts=[folder],
        confirms=[True,      # upload all
                  False,     # replace? no
                  True,      # move files? YES
                  True],     # upload now
    )
    out, exc = run(cli.run_image_import, client, script)
    assert exc is None, exc
    assert any("move processed files" in m for m, _a in script.confirm_log), \
        script.confirm_log
    good = os.path.join(folder, IMAGE_FOLDER_SUCCESS)
    bad = os.path.join(folder, IMAGE_FOLDER_FAILED)
    assert sorted(os.listdir(good)) == ["ABC.png", "XYZ.png"], os.listdir(good)
    assert sorted(os.listdir(bad)) == ["NOPE.png"], os.listdir(bad)
    # only the two subfolders are left in the root
    assert sorted(os.listdir(folder)) == [IMAGE_FOLDER_FAILED,
                                          IMAGE_FOLDER_SUCCESS]
    assert "FILED:" in out
    assert IMAGE_FOLDER_SUCCESS in out and IMAGE_FOLDER_FAILED in out
    # the per-file progress line reads as a move, not "-> sent"
    assert "-> moved to" in out

    # --- import: no matches at all -> no upload, report still written ---
    folder = make_folder(names=("ZZZ.png",)); folders.append(folder)
    client = FakeClient(make_products())
    out, exc = run(cli.run_image_import, client, Script(texts=[folder]))
    assert exc is None, exc
    assert "Nothing matched a product" in out
    assert client.uploaded == []

    # --- manage: list, select one, confirm -> removed ---
    products = make_products()
    products[0]["files"] = ["g1", "g2"]
    client = FakeClient(products, files_by_guid={"g1": "front.png",
                                                 "g2": "back.png"})
    out, exc = run(cli.run_manage_images, client, Script(
        texts=["ABC"],
        confirms=[True,      # remove any of these?
                  True],     # remove them now?
        checkboxes=[lambda choices: [c for c in choices if "front.png" in c]],
    ))
    assert exc is None, exc
    assert "front.png" in out and "back.png" in out
    assert "REMOVAL SUMMARY" in out
    # only g2 is kept
    assert client.attached == [("ABC", ["g2"])], client.attached
    removed = [f for f in os.listdir(work)
               if f.startswith("product_images_removed")]
    assert removed, os.listdir(work)

    # --- manage: declining removes nothing ---
    products = make_products()
    products[0]["files"] = ["g1"]
    client = FakeClient(products, files_by_guid={"g1": "front.png"})
    out, exc = run(cli.run_manage_images, client, Script(
        texts=["ABC"],
        confirms=[False],    # remove any? no
    ))
    assert exc is None
    assert "Nothing removed" in out
    assert client.attached == []

    # --- manage: selecting nothing removes nothing ---
    products = make_products()
    products[0]["files"] = ["g1"]
    client = FakeClient(products, files_by_guid={"g1": "front.png"})
    out, exc = run(cli.run_manage_images, client, Script(
        texts=["ABC"],
        confirms=[True],
        checkboxes=[[]],
    ))
    assert exc is None
    assert "Nothing selected" in out
    assert client.attached == []

    # --- manage: product with no images ---
    client = FakeClient(make_products())          # XYZ has files: []
    out, exc = run(cli.run_manage_images, client, Script(texts=["XYZ"]))
    assert exc is None
    assert "no images attached" in out

    # --- manage: unknown code, then a good one (case-insensitive) ---
    products = make_products()
    products[0]["files"] = ["g1"]
    client = FakeClient(products, files_by_guid={"g1": "front.png"})
    out, exc = run(cli.run_manage_images, client, Script(
        texts=["NOSUCH", "abc"],     # wrong code, then right one lowercased
        confirms=[True,              # try another code?
                  False],            # remove any? no
    ))
    assert exc is None
    assert "No product with code 'NOSUCH'" in out
    assert "front.png" in out

    # --- manage: unknown code then giving up ---
    client = FakeClient(make_products())
    out, exc = run(cli.run_manage_images, client, Script(
        texts=["NOSUCH"],
        confirms=[False],            # try another? no
    ))
    assert isinstance(exc, SystemExit) and "Aborted" in str(exc)

    # --- manage: an unresolvable GUID is flagged but still removable ---
    products = make_products()
    products[0]["files"] = ["g1", "ghost"]
    client = FakeClient(products, files_by_guid={"g1": "front.png"})
    out, exc = run(cli.run_manage_images, client, Script(
        texts=["ABC"],
        confirms=[True, True],
        checkboxes=[lambda choices: [c for c in choices if "(unknown)" in c]],
    ))
    assert exc is None, exc
    assert "would not resolve to a filename" in out
    assert client.attached == [("ABC", ["g1"])], client.attached
finally:
    os.chdir(original_cwd)
    shutil.rmtree(work, ignore_errors=True)
    for f in folders:
        shutil.rmtree(f, ignore_errors=True)

print("All CLI image tests passed")
