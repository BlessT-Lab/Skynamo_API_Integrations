"""Offline smoke tests for the product-image engine: scan matches files to
products and performs no uploads; upload_images uploads only approved plans, in
sequence order, preserving each product's existing files; and, when asked,
files every processed image into a Successful/ or Failed/ subfolder. Uses a
fake client and a temp folder of image fixtures - no network."""

import os
import shutil
import tempfile

from skynamo_geo import image_engine
from skynamo_geo.products import (
    collect_image_files, sequence_sort_key, unique_destination,
)
from skynamo_geo.config import (
    IMAGE_FOLDER_FAILED, IMAGE_FOLDER_SUCCESS,
    STATUS_IMG_PENDING, STATUS_IMG_UPLOADED, STATUS_IMG_NO_MATCH,
    STATUS_IMG_BAD_FORMAT, STATUS_IMG_AMBIGUOUS, STATUS_IMG_UPLOAD_FAILED,
    STATUS_ATT_LOADED, STATUS_ATT_FETCH_FAILED, STATUS_ATT_DELETED,
    STATUS_ATT_DELETE_FAILED,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16


class FakeClient:
    def __init__(self, upload_ok=True, attach_ok=True, files_by_guid=None,
                 require_product_id=False):
        self.uploaded = []   # (filename, content_b64)
        self.attached = []   # (code, [guids])
        self.attach_keys = []  # (code, product_id) per attach call
        self._n = 0
        self.upload_ok = upload_ok
        self.attach_ok = attach_ok
        # A live instance rejects a ProductPatch with no id (it is the declared
        # required field). Set this to hold the engine to that.
        self.require_product_id = require_product_id
        # guid -> filename; a guid absent from the map fails to resolve
        self.files_by_guid = files_by_guid or {}

    def upload_file(self, filename, content_b64):
        self.uploaded.append((filename, content_b64))
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

# ---------------------------------------------------------------------------
# Filing processed images into Successful/ and Failed/
# ---------------------------------------------------------------------------

def contents(folder, *parts):
    """Sorted names directly inside folder/*parts (empty if there is no such dir)."""
    path = os.path.join(folder, *parts)
    return sorted(os.listdir(path)) if os.path.isdir(path) else []


# --- off by default: an ordinary run must not touch the folder ---
f0 = make_folder()
try:
    before = contents(f0)
    plans0 = image_engine.scan_images(PRODUCTS, f0)
    image_engine.upload_images(FakeClient(), plans0)
    assert contents(f0) == before, "upload_images must not move files by default"
    assert contents(f0, IMAGE_FOLDER_SUCCESS) == []
    assert all(p.moved_to == "" for p in plans0)
    assert image_engine.filing_summary(plans0) == {}
finally:
    shutil.rmtree(f0, ignore_errors=True)

# --- move_processed=True files every outcome, and only those ---
f1 = make_folder()
try:
    plans1 = image_engine.scan_images(PRODUCTS, f1)
    by1 = {p.filename: p for p in plans1}
    by1["ABC_2.png"].include = False          # deselected: must stay put
    rows1 = image_engine.upload_images(FakeClient(), plans1,
                                       move_processed=True)
    assert isinstance(rows1, list)            # still returns report rows

    # Uploaded -> Successful
    assert contents(f1, IMAGE_FOLDER_SUCCESS) == [
        "A-B.png", "ABC.png", "ABC_1.png", "XYZ.jpg"], \
        contents(f1, IMAGE_FOLDER_SUCCESS)
    # Everything the run could not upload -> Failed
    assert contents(f1, IMAGE_FOLDER_FAILED) == [
        "BAD.png", "DOC.txt", "NOPE.png", "P-Q.png"], \
        contents(f1, IMAGE_FOLDER_FAILED)
    # The deselected image is all that is left loose in the root
    assert contents(f1) == ["ABC_2.png", IMAGE_FOLDER_FAILED,
                            IMAGE_FOLDER_SUCCESS], contents(f1)
    assert by1["ABC_2.png"].status == STATUS_IMG_PENDING
    assert by1["ABC_2.png"].moved_to == ""

    # moved_to is recorded relative to the image folder, and path follows
    assert by1["ABC.png"].moved_to == os.path.join(IMAGE_FOLDER_SUCCESS,
                                                   "ABC.png")
    assert by1["NOPE.png"].moved_to == os.path.join(IMAGE_FOLDER_FAILED,
                                                    "NOPE.png")
    assert os.path.isfile(by1["ABC.png"].path)
    assert os.path.dirname(by1["ABC.png"].path).endswith(IMAGE_FOLDER_SUCCESS)

    assert image_engine.filing_summary(plans1) == {IMAGE_FOLDER_SUCCESS: 4,
                                                   IMAGE_FOLDER_FAILED: 4}
    # the report carries where each file went
    rows = {r["filename"]: r for r in rows1}
    assert rows["ABC.png"]["moved_to"].endswith("ABC.png")
    assert rows["ABC_2.png"]["moved_to"] == ""

    # a re-scan does not pick the subfolders back up
    assert [os.path.basename(x) for x in collect_image_files(f1)] == ["ABC_2.png"]
finally:
    shutil.rmtree(f1, ignore_errors=True)

# --- a failed upload is filed as Failed, not Successful ---
f2 = make_folder()
try:
    plans2 = image_engine.scan_images(PRODUCTS, f2)
    image_engine.upload_images(FakeClient(upload_ok=False), plans2,
                               move_processed=True)
    assert contents(f2, IMAGE_FOLDER_SUCCESS) == []
    assert "XYZ.jpg" in contents(f2, IMAGE_FOLDER_FAILED)
    assert "ABC.png" in contents(f2, IMAGE_FOLDER_FAILED)
finally:
    shutil.rmtree(f2, ignore_errors=True)

# --- a cancelled run files nothing ---
f3 = make_folder()
try:
    before3 = contents(f3)
    plans3 = image_engine.scan_images(PRODUCTS, f3)
    hits = {"n": 0}
    def cancel3():
        hits["n"] += 1
        return hits["n"] > 1
    image_engine.upload_images(FakeClient(), plans3, move_processed=True,
                               should_cancel=cancel3)
    assert contents(f3) == before3, "a cancelled run must leave the folder alone"
    assert all(p.moved_to == "" for p in plans3)
finally:
    shutil.rmtree(f3, ignore_errors=True)

# --- an earlier run's image is never overwritten ---
f4 = make_folder()
try:
    image_engine.upload_images(FakeClient(),
                               image_engine.scan_images(PRODUCTS, f4),
                               move_processed=True)
    # a new image with a name already used by the first run arrives
    with open(os.path.join(f4, "ABC.png"), "wb") as f:
        f.write(PNG + b"second")
    plans4 = image_engine.scan_images(PRODUCTS, f4)
    image_engine.upload_images(FakeClient(), plans4, move_processed=True)
    assert contents(f4, IMAGE_FOLDER_SUCCESS) == [
        "A-B.png", "ABC (2).png", "ABC.png", "ABC_1.png", "ABC_2.png",
        "XYZ.jpg"], contents(f4, IMAGE_FOLDER_SUCCESS)
    # the newcomer took the suffixed name; the original is untouched
    with open(os.path.join(f4, IMAGE_FOLDER_SUCCESS, "ABC (2).png"), "rb") as f:
        assert f.read().endswith(b"second")
    with open(os.path.join(f4, IMAGE_FOLDER_SUCCESS, "ABC.png"), "rb") as f:
        assert not f.read().endswith(b"second")
    assert next(p for p in plans4 if p.filename == "ABC.png").moved_to == \
        os.path.join(IMAGE_FOLDER_SUCCESS, "ABC (2).png")
finally:
    shutil.rmtree(f4, ignore_errors=True)

# --- a move that cannot happen is noted, never raised ---
f5 = make_folder()
try:
    plans5 = image_engine.scan_images(PRODUCTS, f5)
    gone = next(p for p in plans5 if p.filename == "NOPE.png")
    os.remove(gone.path)                       # vanished between scan and filing
    # block the Successful/ folder with a plain file of the same name
    with open(os.path.join(f5, IMAGE_FOLDER_SUCCESS), "wb") as f:
        f.write(b"not a directory")
    image_engine.upload_images(FakeClient(), plans5, move_processed=True)
    assert "no longer there" in gone.notes, gone.notes
    assert gone.moved_to == ""
    blocked = next(p for p in plans5 if p.filename == "ABC.png")
    assert "could not file the image" in blocked.notes, blocked.notes
    assert blocked.moved_to == ""
    assert os.path.isfile(blocked.path), "a failed move must leave the file put"
    # the Failed side was unaffected by the Successful side's problem
    assert "P-Q.png" in contents(f5, IMAGE_FOLDER_FAILED)
finally:
    shutil.rmtree(f5, ignore_errors=True)

# --- file_processed_images stands alone and reports what it filed ---
f6 = make_folder()
try:
    plans6 = image_engine.scan_images(PRODUCTS, f6)
    events = []
    counts = image_engine.file_processed_images(
        plans6, on_progress=events.append)
    # nothing was uploaded, so every matched image is still pending and stays
    assert counts == {IMAGE_FOLDER_FAILED: 4}, counts
    assert contents(f6, IMAGE_FOLDER_FAILED) == [
        "BAD.png", "DOC.txt", "NOPE.png", "P-Q.png"]
    assert contents(f6, IMAGE_FOLDER_SUCCESS) == []
    assert {e["phase"] for e in events} == {"filing"}
    assert [e["index"] for e in events] == [1, 2, 3, 4]
    assert all(e["total"] == 4 for e in events)
finally:
    shutil.rmtree(f6, ignore_errors=True)

# --- the attach must identify the product by id, not just by code ---
# The live API rejected a ProductPatch keyed only on code, so every image
# uploaded and then failed to attach. A fake that accepts anything hid it.
f_id = make_folder()
try:
    strict = FakeClient(require_product_id=True)
    plans_id = image_engine.scan_images(PRODUCTS, f_id)
    image_engine.upload_images(strict, plans_id)
    by_id = {p.filename: p for p in plans_id}
    assert by_id["ABC.png"].status == STATUS_IMG_UPLOADED, by_id["ABC.png"].notes
    assert by_id["XYZ.jpg"].status == STATUS_IMG_UPLOADED
    # every attach carried the product's id
    assert all(pid is not None for _code, pid in strict.attach_keys), \
        strict.attach_keys
    assert ("ABC", 1) in strict.attach_keys, strict.attach_keys
    assert ("A/B", 3) in strict.attach_keys, strict.attach_keys

    # a product that genuinely has no id still attaches, by code
    no_id = [{"code": "NOID", "name": "Idless"}]
    folder_noid = tempfile.mkdtemp(prefix="skynamo_noid_")
    try:
        with open(os.path.join(folder_noid, "NOID.png"), "wb") as f:
            f.write(PNG)
        lax = FakeClient()
        plans_noid = image_engine.scan_images(no_id, folder_noid)
        image_engine.upload_images(lax, plans_noid)
        assert lax.attach_keys == [("NOID", None)], lax.attach_keys
        assert plans_noid[0].status == STATUS_IMG_UPLOADED
    finally:
        shutil.rmtree(folder_noid, ignore_errors=True)
finally:
    shutil.rmtree(f_id, ignore_errors=True)

# --- filing twice must not nest the subfolders ---
f8 = make_folder()
try:
    plans8 = image_engine.scan_images(PRODUCTS, f8)
    # An uploaded plan is still `writable`, so a second Upload click re-runs the
    # whole thing over the same plans. Filing has to be idempotent per plan.
    image_engine.upload_images(FakeClient(), plans8, move_processed=True)
    first = {p.filename: p.moved_to for p in plans8}
    image_engine.upload_images(FakeClient(), plans8, move_processed=True)
    assert {p.filename: p.moved_to for p in plans8} == first
    assert not os.path.exists(os.path.join(f8, IMAGE_FOLDER_SUCCESS,
                                           IMAGE_FOLDER_SUCCESS))
    assert not os.path.exists(os.path.join(f8, IMAGE_FOLDER_FAILED,
                                           IMAGE_FOLDER_FAILED))
    assert contents(f8, IMAGE_FOLDER_SUCCESS) == [
        "A-B.png", "ABC.png", "ABC_1.png", "ABC_2.png", "XYZ.jpg"]
    assert image_engine.filing_failures(plans8) == []
finally:
    shutil.rmtree(f8, ignore_errors=True)

# --- filing_failures reports only real move failures ---
f9 = make_folder()
try:
    # filing not requested at all -> not a failure, even though nothing moved
    plans9 = image_engine.scan_images(PRODUCTS, f9)
    image_engine.upload_images(FakeClient(), plans9)
    assert image_engine.filing_failures(plans9) == []
    assert all(p.filing_error == "" for p in plans9)

    # a genuine failure is reported, and only that plan
    plans9b = image_engine.scan_images(PRODUCTS, f9)
    doomed = next(p for p in plans9b if p.filename == "NOPE.png")
    os.remove(doomed.path)
    image_engine.upload_images(FakeClient(), plans9b, move_processed=True)
    assert [p.filename for p in image_engine.filing_failures(plans9b)] == \
        ["NOPE.png"]
    assert doomed.filing_error and "no longer there" in doomed.filing_error
finally:
    shutil.rmtree(f9, ignore_errors=True)

# --- a shutil.Error from the move is reported, never raised ---
f10 = make_folder()
real_move = image_engine.shutil.move
try:
    def boom(src, dst):
        raise shutil.Error("destination already exists")
    image_engine.shutil.move = boom
    plans10 = image_engine.scan_images(PRODUCTS, f10)
    image_engine.upload_images(FakeClient(), plans10, move_processed=True)
    stuck = image_engine.filing_failures(plans10)
    assert stuck, "a shutil.Error must be caught and reported, not raised"
    assert all("could not file the image" in p.filing_error for p in stuck)
    assert all(p.moved_to == "" for p in stuck)
finally:
    image_engine.shutil.move = real_move
    shutil.rmtree(f10, ignore_errors=True)

# --- unique_destination never returns an occupied path ---
f7 = tempfile.mkdtemp(prefix="skynamo_uniq_")
try:
    assert unique_destination(f7, "a.png") == os.path.join(f7, "a.png")
    open(os.path.join(f7, "a.png"), "wb").close()
    assert unique_destination(f7, "a.png") == os.path.join(f7, "a (2).png")
    open(os.path.join(f7, "a (2).png"), "wb").close()
    assert unique_destination(f7, "a.png") == os.path.join(f7, "a (3).png")
    open(os.path.join(f7, "plain"), "wb").close()   # no extension
    assert unique_destination(f7, "plain") == os.path.join(f7, "plain (2)")
finally:
    shutil.rmtree(f7, ignore_errors=True)


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
