"""Helpers for matching local image files to Skynamo products.

Image files are named after a product code, optionally with a trailing image
sequence (``ABC_1``, ``ABC 2``, ``ABC_A``). A product code may contain
characters that can't appear in a filename (``/``, ``:``, ...); each is written
as a hyphen instead.

Rather than trying to reverse a filename back into a product code (ambiguous:
is ``-`` a literal hyphen or an escaped ``/``? is ``ABC_1`` the code ``ABC``
plus sequence 1, or a literal code ``ABC_1``?), we apply the *same* forward
transform to every known product code and match filenames against that. The
authoritative product list decides; the filename only has to match one of the
forms a real code could take.
"""

import os
import re

from .config import (
    ALLOWED_IMAGE_EXTENSIONS, FILENAME_ESCAPE_CHAR, JPEG_SIGNATURE,
    PNG_SIGNATURE, WINDOWS_RESERVED_CHARS,
)

# Trailing " <n>" / "_<n>" / " <L>" / "_<L>" sequence marker (digits, or a
# single letter used to denote order). Greedy base so only the final marker is
# stripped: "AB_C_2" -> base "AB_C", sequence "2".
_SEQUENCE_RE = re.compile(r"^(.*)[ _](\d+|[A-Za-z])$")


def product_code(product):
    """The product's unique code, trimmed ("" if absent)."""
    return (product.get("code") or "").strip()


def escape_code_to_filename(code):
    """Rewrite a product code into the form it would take as a filename.

    Every filename-reserved character becomes a hyphen, matching how the user
    names the image files.
    """
    result = code
    for char in WINDOWS_RESERVED_CHARS:
        result = result.replace(char, FILENAME_ESCAPE_CHAR)
    return result


def parse_image_stem(stem):
    """Split a filename stem into (base, sequence, full_stem).

    sequence is the trailing order marker (a digit run or single letter) or
    None. base is the stem with that marker removed; full_stem is the stem
    unchanged (so the caller can also try it as a literal code).
    """
    match = _SEQUENCE_RE.match(stem)
    if match:
        return match.group(1), match.group(2), stem
    return stem, None, stem


def sequence_sort_key(sequence):
    """Sort key ordering images within one product.

    No sequence sorts first, then numeric order, then alphabetic (A, B, ...).
    """
    if sequence is None or sequence == "":
        return (0, 0)
    if sequence.isdigit():
        return (1, int(sequence))
    return (1, ord(sequence.upper()) - ord("A") + 1)


def sniff_image_format(path):
    """Return "png"/"jpeg" from the file's leading bytes, or None."""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return None
    if head.startswith(PNG_SIGNATURE):
        return "png"
    if head.startswith(JPEG_SIGNATURE):
        return "jpeg"
    return None


def has_allowed_extension(path):
    """True if the file's extension is one Skynamo accepts."""
    return os.path.splitext(path)[1].lower() in ALLOWED_IMAGE_EXTENSIONS


def build_code_index(products):
    """Map each product's filename-form code to the product(s) with that code.

    Keyed case-insensitively; the value is a list so callers can detect when
    two distinct codes collapse to the same filename form (an ambiguous match).
    """
    index = {}
    for product in products:
        code = product_code(product)
        if not code:
            continue
        key = escape_code_to_filename(code).casefold()
        index.setdefault(key, []).append(product)
    return index


def unique_destination(folder, filename):
    """A path for filename inside folder that no existing file occupies.

    Successive runs file images into the same subfolder, so a name that is
    already taken belongs to an earlier run's image - never overwrite it.
    "ABC.png" becomes "ABC (2).png", then "ABC (3).png", and so on.
    """
    stem, ext = os.path.splitext(filename)
    candidate = os.path.join(folder, filename)
    n = 2
    while os.path.exists(candidate):
        candidate = os.path.join(folder, f"{stem} ({n}){ext}")
        n += 1
    return candidate


def collect_image_files(folder):
    """Return full paths of files directly inside folder (non-recursive), sorted.

    Every file is returned regardless of extension so unsupported ones can be
    reported rather than silently skipped. Being non-recursive and files-only
    is what keeps a re-run from picking the Successful/ and Failed/ subfolders
    back up again (see image_engine.file_processed_images).
    """
    entries = []
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            entries.append(path)
    return entries
