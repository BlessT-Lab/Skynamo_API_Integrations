"""Helpers every engine shares - UI-agnostic.

Every engine (geolocation, product images, reporting extracts, route
planning) produces a list of plain dicts and needs the same things: count them
by status, write them to a CSV, append to a stop's notes, and default the
on_progress / should_cancel callbacks. These lived copied into each engine,
and a copy is a change that reaches only some of them; they belong here.

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


def noop(*_args, **_kwargs):
    """The default on_progress: ignore the event."""
    return None


def never_cancel():
    """The default should_cancel: never."""
    return False


def append_note(notes, extra):
    """`notes` with `extra` added, "; "-separated."""
    return (notes + "; " if notes else "") + extra
