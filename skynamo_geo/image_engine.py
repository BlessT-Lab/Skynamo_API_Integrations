"""Product-image import engine - UI-agnostic.

Two phases, mirroring engine.py so any front-end can preview-then-commit:
  1. scan_images(...)   -> builds ImagePlans (matches files to products, no uploads)
  2. upload_images(...) -> POSTs each approved image to /files and attaches the
                           returned GUID to its product via PATCH /products

Both report progress via on_progress(event) and can be aborted via
should_cancel(), the same contracts the GUI worker thread already drives.
"""

import base64
import csv
import os

from .config import (
    IMAGE_REPORT_FIELDNAMES, STATUS_IMG_AMBIGUOUS, STATUS_IMG_BAD_FORMAT,
    STATUS_IMG_NO_MATCH, STATUS_IMG_PENDING, STATUS_IMG_UPLOAD_FAILED,
    STATUS_IMG_UPLOADED,
)
from .products import (
    build_code_index, collect_image_files, has_allowed_extension,
    parse_image_stem, product_code, sequence_sort_key, sniff_image_format,
)


def _noop(*_args, **_kwargs):
    return None


def _never_cancel():
    return False


def _product_key(product):
    """Stable identity for grouping plans by product (id, else code)."""
    pid = product.get("id")
    return pid if pid is not None else product_code(product)


def _dedupe(seq):
    """List with duplicates removed, order preserved."""
    seen = set()
    out = []
    for item in seq:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


class ImagePlan:
    """One image file's matching outcome and intended upload."""

    def __init__(self, path):
        self.path = path
        self.filename = os.path.basename(path)
        self.product_code = ""      # matched code, or best-guess base
        self.sequence = None
        self.product = None         # matched product dict, or None
        self.file_guid = ""         # set after a successful upload
        self.status = ""            # a STATUS_IMG_* value
        self.notes = ""
        self.include = False        # whether upload_images should process it

    @property
    def writable(self):
        return self.product is not None and self.status in (
            STATUS_IMG_PENDING, STATUS_IMG_UPLOADED)

    @property
    def product_name(self):
        return (self.product or {}).get("name", "") if self.product else ""

    def to_report_row(self):
        return {
            "filename": self.filename,
            "product_code": self.product_code,
            "matched_product": self.product_name,
            "sequence": self.sequence or "",
            "status": self.status,
            "notes": self.notes,
        }


def scan_images(products, folder, on_progress=_noop, should_cancel=_never_cancel):
    """Match every file in folder to a product. Performs NO uploads.

    Each file becomes an ImagePlan whose status records whether it matched a
    product, matched none, matched several (ambiguous), or isn't a supported
    image at all.
    """
    index = build_code_index(products)
    files = collect_image_files(folder)
    total = len(files)
    plans = []
    for i, path in enumerate(files, 1):
        if should_cancel():
            break
        plan = ImagePlan(path)
        stem, _ext = os.path.splitext(plan.filename)
        base, sequence, full_stem = parse_image_stem(stem)
        plan.product_code = base

        if not has_allowed_extension(path):
            plan.status = STATUS_IMG_BAD_FORMAT
            plan.notes = "Unsupported file extension (expected .png/.jpg/.jpeg)"
        elif sniff_image_format(path) is None:
            plan.status = STATUS_IMG_BAD_FORMAT
            plan.notes = "File is not a valid PNG or JPEG"
        else:
            # Prefer a literal code (the whole stem), then fall back to the
            # base with a stripped sequence marker.
            matches = None
            for candidate, seq in ((full_stem, None), (base, sequence)):
                hit = index.get(candidate.casefold())
                if hit:
                    matches = hit
                    plan.sequence = seq
                    break
            if not matches:
                plan.status = STATUS_IMG_NO_MATCH
                plan.notes = f"No product with code matching '{stem}'"
            elif len(matches) > 1:
                codes = ", ".join(sorted(product_code(p) for p in matches))
                plan.status = STATUS_IMG_AMBIGUOUS
                plan.notes = f"Filename matches multiple product codes: {codes}"
            else:
                plan.product = matches[0]
                plan.product_code = product_code(matches[0])
                plan.status = STATUS_IMG_PENDING
                plan.include = True
                plan.notes = f"Matches product '{plan.product_name}'"

        plans.append(plan)
        on_progress({
            "phase": "scan", "index": i, "total": total,
            "name": plan.filename, "status": plan.status, "message": plan.notes,
        })

    _flag_duplicate_sequences(plans)
    return plans


def _flag_duplicate_sequences(plans):
    """Note when two matched images claim the same order slot for one product."""
    by_product = {}
    for plan in plans:
        if plan.product is not None and plan.status == STATUS_IMG_PENDING:
            by_product.setdefault(_product_key(plan.product), []).append(plan)
    for group in by_product.values():
        seen = {}
        for plan in group:
            key = sequence_sort_key(plan.sequence)
            seen.setdefault(key, []).append(plan)
        for dupes in seen.values():
            if len(dupes) > 1:
                for plan in dupes:
                    plan.notes += "; duplicate image order for this product"


def upload_images(client, plans, on_progress=_noop, should_cancel=_never_cancel):
    """Upload approved images and attach them to their products.

    Images are grouped by product and uploaded in sequence order. All of a
    product's newly uploaded GUIDs are merged with its existing `files` in one
    PATCH, so nothing already attached is lost. Returns report rows for every
    plan (skips and failures included).
    """
    to_upload = [p for p in plans if p.include and p.writable]

    # Group by product, preserving first-seen product order, and sort each
    # product's images by their sequence marker.
    groups = {}
    for plan in to_upload:
        groups.setdefault(_product_key(plan.product), []).append(plan)
    for group in groups.values():
        group.sort(key=lambda p: sequence_sort_key(p.sequence))

    total = len(to_upload)
    done = 0
    cancelled = False
    for group in groups.values():
        if cancelled:
            break
        product = group[0].product
        existing = _dedupe(list(product.get("files") or []))
        uploaded = []  # (plan, guid) for images that uploaded this round
        for plan in group:
            if should_cancel():
                cancelled = True
                break
            done += 1
            guid, error = _upload_one(client, plan)
            if guid:
                plan.file_guid = guid
                uploaded.append(plan)
            else:
                plan.status = STATUS_IMG_UPLOAD_FAILED
                plan.notes = _append_note(plan.notes, error)
            on_progress({
                "phase": "upload", "index": done, "total": total,
                "name": plan.filename, "status": plan.status,
                "message": plan.notes,
            })

        if not uploaded:
            continue
        desired = _dedupe(existing + [p.file_guid for p in uploaded])
        ok, error = client.attach_files(product_code(product), desired)
        for plan in uploaded:
            if ok:
                plan.status = STATUS_IMG_UPLOADED
            else:
                plan.status = STATUS_IMG_UPLOAD_FAILED
                plan.notes = _append_note(plan.notes,
                                          f"uploaded but not attached: {error}")

    return [p.to_report_row() for p in plans]


def _upload_one(client, plan):
    """Read, base64-encode and POST one image. Returns (guid, error)."""
    try:
        with open(plan.path, "rb") as f:
            content = base64.b64encode(f.read()).decode("ascii")
    except OSError as exc:
        return None, f"Could not read file: {exc}"
    return client.upload_file(plan.filename, content)


def _append_note(notes, extra):
    return (notes + "; " if notes else "") + extra


def summarize(plans):
    """Count plans by status for a summary display."""
    counts = {}
    for plan in plans:
        counts[plan.status] = counts.get(plan.status, 0) + 1
    return counts


def write_report(report_rows, path):
    """Write the product-image report rows to a CSV at path."""
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=IMAGE_REPORT_FIELDNAMES)
        writer.writeheader()
        writer.writerows(report_rows)
    return path
