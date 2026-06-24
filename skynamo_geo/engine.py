"""Processing engine - UI-agnostic.

Two phases so any front-end can do preview-then-commit:
  1. geocode_customers(...)  -> builds Plans (geocodes, but writes nothing)
  2. write_locations(...)    -> PATCHes the approved Plans to Skynamo

Both report progress via an on_progress(event) callback and can be aborted
via a should_cancel() predicate, so a GUI worker thread or the CLI can drive
them identically.
"""

import csv
import time

from .config import (
    GEOCODE_DELAY_SECONDS, REPORT_FIELDNAMES,
    STATUS_GEOCODE_FAILED, STATUS_PENDING, STATUS_SKIPPED_HAS_COORDS,
    STATUS_SKIPPED_NO_ADDRESS, STATUS_UPDATED, STATUS_UPDATED_LOW_CONF,
    STATUS_UPDATE_FAILED,
)
from .customers import build_address, has_coordinates
from .geocoder import GeocodeError


def _noop(*_args, **_kwargs):
    return None


def _never_cancel():
    return False


class Plan:
    """One customer's geocoding outcome and intended write."""

    def __init__(self, customer):
        self.customer_id = customer.get("id")
        self.code = customer.get("code", "")
        self.name = customer.get("name", "")
        self.address = ""
        self.result = None          # GeocodeResult, or None
        self.status = ""            # a STATUS_* value
        self.low_confidence = False
        self.notes = ""
        # Whether write_locations should PATCH this plan. Defaults on only for
        # rows that actually produced coordinates; the GUI may toggle it.
        self.include = False

    @property
    def writable(self):
        """A plan can be written if it has coordinates and isn't a skip/failure."""
        return self.result is not None and self.status in (
            STATUS_PENDING, STATUS_UPDATED, STATUS_UPDATED_LOW_CONF)

    @property
    def lat(self):
        return self.result.lat if self.result else ""

    @property
    def lng(self):
        return self.result.lng if self.result else ""

    @property
    def accuracy(self):
        return self.result.accuracy if self.result else ""

    @property
    def precision(self):
        return self.result.precision if self.result else ""

    def to_report_row(self):
        return {
            "customer_id": self.customer_id, "code": self.code,
            "name": self.name, "status": self.status,
            "address_used": self.address, "latitude": self.lat,
            "longitude": self.lng, "accuracy": self.accuracy,
            "match_precision": self.precision, "notes": self.notes,
        }


def geocode_customers(geocoder, customers, address_fields,
                      replace_existing=False, country=None,
                      on_progress=_noop, should_cancel=_never_cancel,
                      delay=GEOCODE_DELAY_SECONDS):
    """Geocode customers into Plans. Performs NO writes.

    Raises GeocodeError on fatal provider problems (bad key/quota) so the
    caller can stop the whole run rather than failing every row.
    """
    plans = []
    total = len(customers)
    for index, customer in enumerate(customers, 1):
        if should_cancel():
            break
        plan = Plan(customer)

        if not replace_existing and has_coordinates(customer):
            plan.status = STATUS_SKIPPED_HAS_COORDS
            plan.notes = "Already has coordinates"
        else:
            plan.address = build_address(customer, address_fields)
            if not plan.address:
                plan.status = STATUS_SKIPPED_NO_ADDRESS
                plan.notes = "No address"
            else:
                result = geocoder.geocode(plan.address, country=country)
                if delay:
                    time.sleep(delay)
                if result is None:
                    plan.status = STATUS_GEOCODE_FAILED
                    plan.notes = "Address could not be geocoded"
                else:
                    plan.result = result
                    plan.low_confidence = result.is_low_confidence
                    plan.status = STATUS_PENDING
                    plan.include = True
                    note_parts = [f"Google match: '{result.formatted_address}'"]
                    if result.partial_match:
                        note_parts.append("partial match")
                    if plan.low_confidence:
                        note_parts.append("LOW CONFIDENCE - review manually")
                    plan.notes = "; ".join(note_parts)

        plans.append(plan)
        on_progress({
            "phase": "geocode", "index": index, "total": total,
            "name": plan.name, "status": plan.status, "message": plan.notes,
        })
    return plans


def write_locations(client, plans, on_progress=_noop,
                    should_cancel=_never_cancel):
    """PATCH the approved (include + writable) plans to Skynamo.

    Updates each plan's status/notes in place and returns report rows for
    every plan (so skips and failures are recorded too).
    """
    to_write = [p for p in plans if p.include and p.writable]
    total = len(to_write)
    written = 0
    for plan in to_write:
        if should_cancel():
            break
        written += 1
        try:
            ok, error = client.update_location(
                plan.customer_id, plan.result.lat, plan.result.lng,
                accuracy=plan.result.accuracy,
                is_approximate=plan.low_confidence)
        except Exception as exc:  # network etc. - record, keep going
            ok, error = False, str(exc)

        if ok:
            plan.status = (STATUS_UPDATED_LOW_CONF if plan.low_confidence
                           else STATUS_UPDATED)
        else:
            plan.status = STATUS_UPDATE_FAILED
            plan.notes = (plan.notes + "; " if plan.notes else "") + error

        on_progress({
            "phase": "write", "index": written, "total": total,
            "name": plan.name, "status": plan.status,
            "message": plan.notes,
        })
    return [p.to_report_row() for p in plans]


def summarize(plans):
    """Count plans by status for a summary display."""
    counts = {}
    for plan in plans:
        counts[plan.status] = counts.get(plan.status, 0) + 1
    return counts


def write_report(report_rows, path):
    """Write the report rows to a CSV at path."""
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=REPORT_FIELDNAMES)
        writer.writeheader()
        writer.writerows(report_rows)
    return path
