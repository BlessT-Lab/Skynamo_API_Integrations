"""Offline smoke tests for the product-image engine: scan matches files to
products and performs no uploads; upload_images uploads only approved plans, in
sequence order, preserving each product's existing files. Uses a fake client
and a temp folder of image fixtures - no network."""

import os
import shutil
import tempfile

from skynamo_geo import image_engine
from skynamo_geo.products import sequence_sort_key
from skynamo_geo.config import (
    STATUS_IMG_PENDING, STATUS_IMG_UPLOADED, STATUS_IMG_NO_MATCH,
    STATUS_IMG_BAD_FORMAT, STATUS_IMG_AMBIGUOUS, STATUS_IMG_UPLOAD_FAILED,
    STATUS_ATT_LOADED, STATUS_ATT_FETCH_FAILED, STATUS_ATT_DELETED,
    STATUS_ATT_DELETE_FAILED,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16


class FakeClient:
    def __init__(self, upload_ok=True, attach_ok=True, files_by_guid=None):
        self.uploaded = []   # (filename, content_b64)
        self.attached = []   # (code, [guids])
        self._n = 0
        self.upload_ok = upload_ok
        self.attach_ok = attach_ok
        # guid -> filename; a guid absent from the map fails to resolve
        self.files_by_guid = files_by_guid or {}

    def upload_file(self, filename, content_b64):
        self.uploaded.append((filename, content_b64))
        if not self.upload_ok:
            return None, "upload boom"
        self._n += 1
        return f"guid-{self._n}", ""

    def attach_files(self, code, files):
        self.attached.append((code, list(files)))
        if not self.attach_ok:
            return False, "attach boom"
        return True, ""

    def get_file(self, guid):
        name = self.files_by_guid.get(guid)
        if name is None:
            return None, "file not found"
        return {"id": guid, "filename": name}, ""


PRODUCTS = [
    {"id": 1, "code": "ABC", "name": "Alpha", "files": ["existing-guid"]},
    {"id": 2, "code": "XYZ", "name": "Exwhyzed", "files": []},
    {"id": 3, "code": "A/B", "name": "Slash"},          # no files key at all
    {"id": 4, "code": "P/Q", "name": "PeeQue"},         # collides with P-Q
    {"id": 5, "code": "P-Q", "name": "PeeDashQue"},
]


def make_folder():
    tmp = tempfile.mkdtemp(prefix="skynamo_imgeng_")
    fixtures = {
        "ABC.png": PNG,          # -> product ABC, no sequence
        "ABC_1.png": PNG,        # -> product ABC, seq 1
        "ABC_2.png": PNG,        # -> product ABC, seq 2
        "XYZ.jpg": JPEG,         # -> product XYZ
        "A-B.png": PNG,          # -> product A/B (escaped slash)
        "P-Q.png": PNG,          # -> ambiguous (P/Q and P-Q)
        "NOPE.png": PNG,         # -> no matching product
        "BAD.png": b"not image", # -> unsupported (bad content)
        "DOC.txt": b"whatever",  # -> unsupported (bad extension)
    }
    for name, data in fixtures.items():
        with open(os.path.join(tmp, name), "wb") as f:
            f.write(data)
    return tmp


tmp = make_folder()
try:
    # --- scan: correct status per file, and NO uploads happen ---
    client = FakeClient()
    plans = image_engine.scan_images(PRODUCTS, tmp)
    by_name = {p.filename: p for p in plans}

    assert by_name["ABC.png"].status == STATUS_IMG_PENDING
    assert by_name["ABC.png"].product["id"] == 1
    assert by_name["ABC.png"].sequence is None
    assert by_name["ABC_1.png"].sequence == "1"
    assert by_name["ABC_2.png"].sequence == "2"
    assert by_name["XYZ.jpg"].status == STATUS_IMG_PENDING
    assert by_name["A-B.png"].status == STATUS_IMG_PENDING
    assert by_name["A-B.png"].product_code == "A/B"        # real code, not escaped
    assert by_name["P-Q.png"].status == STATUS_IMG_AMBIGUOUS
    assert by_name["NOPE.png"].status == STATUS_IMG_NO_MATCH
    assert by_name["BAD.png"].status == STATUS_IMG_BAD_FORMAT
    assert by_name["DOC.txt"].status == STATUS_IMG_BAD_FORMAT
    assert client.uploaded == [] and client.attached == []  # scan wrote nothing

    # --- upload: only approved plans, sequence order, existing files kept ---
    report = image_engine.upload_images(client, plans)

    abc_plans = sorted(
        [by_name["ABC.png"], by_name["ABC_1.png"], by_name["ABC_2.png"]],
        key=lambda p: sequence_sort_key(p.sequence))
    for p in abc_plans + [by_name["XYZ.jpg"], by_name["A-B.png"]]:
        assert p.status == STATUS_IMG_UPLOADED, (p.filename, p.status)
        assert p.file_guid

    abc_attaches = [a for a in client.attached if a[0] == "ABC"]
    assert len(abc_attaches) == 1, abc_attaches
    _code, abc_files = abc_attaches[0]
    # Existing GUID preserved at the front, then the three images in seq order.
    assert abc_files == ["existing-guid"] + [p.file_guid for p in abc_plans], abc_files

    # A/B attaches under its real code (with the escaped-slash filename matched)
    ab_attaches = [a for a in client.attached if a[0] == "A/B"]
    assert len(ab_attaches) == 1 and len(ab_attaches[0][1]) == 1

    # Non-matches never uploaded
    for name in ("P-Q.png", "NOPE.png", "BAD.png", "DOC.txt"):
        assert by_name[name].file_guid == ""

    # Report covers every plan (skips/failures included)
    assert len(report) == len(plans)

    # --- attach failure marks the uploaded images failed ---
    client_fail = FakeClient(attach_ok=False)
    plans2 = image_engine.scan_images(PRODUCTS, tmp)
    image_engine.upload_images(client_fail, plans2)
    xyz2 = next(p for p in plans2 if p.filename == "XYZ.jpg")
    assert xyz2.status == STATUS_IMG_UPLOAD_FAILED
    assert "not attached" in xyz2.notes

    # --- upload failure (POST /files) marks the image failed, no attach ---
    client_up = FakeClient(upload_ok=False)
    plans3 = image_engine.scan_images(PRODUCTS, tmp)
    image_engine.upload_images(client_up, plans3)
    xyz3 = next(p for p in plans3 if p.filename == "XYZ.jpg")
    assert xyz3.status == STATUS_IMG_UPLOAD_FAILED and xyz3.file_guid == ""
    assert client_up.attached == []   # nothing attached when uploads all fail

    # --- cancel stops the upload run early ---
    client_c = FakeClient()
    plans4 = image_engine.scan_images(PRODUCTS, tmp)
    calls = {"n": 0}
    def cancel():
        calls["n"] += 1
        return calls["n"] > 1
    image_engine.upload_images(client_c, plans4, should_cancel=cancel)
    assert len(client_c.uploaded) <= 1

    # --- replace_existing wipes the product's prior files ---
    client_r = FakeClient()
    plans_r = image_engine.scan_images(PRODUCTS, tmp)
    by_name_r = {p.filename: p for p in plans_r}
    image_engine.upload_images(client_r, plans_r, replace_existing=True)
    abc_r = [a for a in client_r.attached if a[0] == "ABC"]
    assert len(abc_r) == 1
    # existing-guid is gone; only this run's uploaded GUIDs remain
    assert "existing-guid" not in abc_r[0][1], abc_r[0][1]
    assert all(g.startswith("guid-") for g in abc_r[0][1])
    assert "replaced 1 existing image" in by_name_r["ABC.png"].notes
    # XYZ had no existing files -> no "replaced" note
    assert "replaced" not in by_name_r["XYZ.jpg"].notes
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# --- list_attached_images: resolves names, no writes, flags fetch failures ---
prod = {"id": 9, "code": "ATT", "name": "Attached",
        "files": ["g1", "g2", "gX"]}
lc = FakeClient(files_by_guid={"g1": "front.png", "g2": "back.jpg"})
attached = image_engine.list_attached_images(lc, prod)
assert lc.attached == []                      # listing writes nothing
assert [a.filename for a in attached] == ["front.png", "back.jpg", "(unknown)"]
assert attached[0].status == STATUS_ATT_LOADED
assert attached[2].status == STATUS_ATT_FETCH_FAILED   # gX not resolvable

# --- delete_selected_images: detaches only the ticked GUIDs in one PATCH ---
dc = FakeClient(files_by_guid={"g1": "front.png", "g2": "back.jpg"})
imgs = image_engine.list_attached_images(dc, prod)
imgs[1].delete = True                         # remove "back.jpg" (g2)
rows = image_engine.delete_selected_images(dc, prod, imgs)
assert len(dc.attached) == 1
_code, kept = dc.attached[0]
assert kept == ["g1", "gX"], kept             # g2 dropped, others kept in order
assert imgs[1].status == STATUS_ATT_DELETED
assert imgs[0].status == STATUS_ATT_LOADED    # untouched
assert len(rows) == 3                         # report covers every image

# Nothing ticked -> no PATCH is issued at all
dc2 = FakeClient(files_by_guid={"g1": "front.png"})
imgs2 = image_engine.list_attached_images(dc2, {"id": 9, "code": "ATT",
                                                "files": ["g1"]})
image_engine.delete_selected_images(dc2, prod, imgs2)
assert dc2.attached == []

# Delete failure marks the removed image failed
fc = FakeClient(attach_ok=False, files_by_guid={"g1": "front.png"})
fimgs = image_engine.list_attached_images(fc, {"id": 9, "code": "ATT",
                                               "files": ["g1"]})
fimgs[0].delete = True
image_engine.delete_selected_images(fc, prod, fimgs)
assert fimgs[0].status == STATUS_ATT_DELETE_FAILED

print("All image engine tests passed")
