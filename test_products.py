"""Offline tests for product-image filename helpers: code escaping, sequence
parsing/ordering, format sniffing, code indexing, and folder collection.
No network - pure logic plus a temp folder of byte fixtures."""

import os
import shutil
import tempfile

from skynamo_geo.products import (
    escape_code_to_filename, parse_image_stem, sequence_sort_key,
    sniff_image_format, has_allowed_extension, build_code_index,
    collect_image_files, product_code,
)

# --- escape_code_to_filename: reserved chars -> hyphen ---
assert escape_code_to_filename("ABC") == "ABC"
assert escape_code_to_filename("AB/C") == "AB-C"
assert escape_code_to_filename('A/B:C*D?E"F<G>H|I\\J') == "A-B-C-D-E-F-G-H-I-J"

# --- parse_image_stem: (base, sequence, full_stem) ---
assert parse_image_stem("ABC") == ("ABC", None, "ABC")
assert parse_image_stem("ABC_1") == ("ABC", "1", "ABC_1")
assert parse_image_stem("ABC 2") == ("ABC", "2", "ABC 2")
assert parse_image_stem("ABC_A") == ("ABC", "A", "ABC_A")
assert parse_image_stem("ABC B") == ("ABC", "B", "ABC B")
assert parse_image_stem("AB_C_2") == ("AB_C", "2", "AB_C_2")   # only last marker

# --- sequence_sort_key: none first, then numeric, then alpha ---
seqs = ["2", None, "1", "A", "B"]
assert sorted(seqs, key=sequence_sort_key) == [None, "1", "A", "2", "B"]
assert sequence_sort_key(None) == (0, 0)
assert sequence_sort_key("1") == (1, 1)
assert sequence_sort_key("A") == (1, 1)   # A denotes first, same slot as 1

# --- has_allowed_extension ---
assert has_allowed_extension("x.png")
assert has_allowed_extension("x.JPG")
assert has_allowed_extension("x.jpeg")
assert not has_allowed_extension("x.gif")
assert not has_allowed_extension("x.txt")

# --- build_code_index: casefolded keys, collisions listed ---
products = [
    {"id": 1, "code": "ABC", "name": "Alpha"},
    {"id": 2, "code": "A/B", "name": "Slash"},
    {"id": 3, "code": "P/Q", "name": "PeeQue"},
    {"id": 4, "code": "P-Q", "name": "PeeDashQue"},   # escapes to same as P/Q
    {"id": 5, "code": "", "name": "NoCode"},          # skipped
]
index = build_code_index(products)
assert index["abc"][0]["id"] == 1
assert index["a-b"][0]["id"] == 2
assert len(index["p-q"]) == 2                          # P/Q and P-Q collide
assert "" not in index                                 # empty code skipped
assert product_code(products[0]) == "ABC"

# --- sniff_image_format + collect_image_files on a temp folder ---
tmp = tempfile.mkdtemp(prefix="skynamo_img_test_")
try:
    png = os.path.join(tmp, "real.png")
    with open(png, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    jpg = os.path.join(tmp, "real.jpg")
    with open(jpg, "wb") as f:
        f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 16)
    txt = os.path.join(tmp, "fake.png")   # .png extension but text content
    with open(txt, "wb") as f:
        f.write(b"this is not an image")
    os.mkdir(os.path.join(tmp, "subdir"))  # must be ignored by collect

    assert sniff_image_format(png) == "png"
    assert sniff_image_format(jpg) == "jpeg"
    assert sniff_image_format(txt) is None

    files = collect_image_files(tmp)
    names = sorted(os.path.basename(p) for p in files)
    assert names == ["fake.png", "real.jpg", "real.png"], names  # no subdir
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("All product helper tests passed")
