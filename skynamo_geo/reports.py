"""Shared report helpers - UI-agnostic.

Every engine (geolocation, product images, reporting extracts) produces a list
of plain dicts and needs the same two things: count them by status, and write
them to a CSV. These lived duplicated in each engine; they belong here.

Callers keep importing `write_report`/`summarize` from their own engine module,
which re-exports these, so nothing outside had to change.
"""

import csv


def summarize(items):
    """Count items by their `status` attribute. Returns {status: count}."""
    counts = {}
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    return counts


def write_report(report_rows, path, fieldnames):
    """Write report rows to a CSV at path. Returns the path written.

    utf-8-sig so Excel opens it with the right encoding; newline="" per the
    csv module's requirement.
    """
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(report_rows)
    return path
