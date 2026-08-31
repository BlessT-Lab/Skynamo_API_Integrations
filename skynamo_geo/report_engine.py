"""Reporting-extract engine - UI-agnostic.

Two phases, the same shape as engine.py and image_engine.py so any front-end
can preview-then-commit:
  1. plan_extract(...) -> builds ExtractPlans (reads stored bookmarks to decide
                          full-load vs delta, estimates the rate-limit cost,
                          makes NO network calls)
  2. run_extract(...)  -> fetches each approved entity and upserts it into the
                          local store, recording the run

Reports progress via on_progress(event) and aborts via should_cancel(), the
same contracts the GUI worker threads already drive.
"""

from . import reports
from .reporting_config import (
    DEFAULT_RATE_LIMIT, RATE_LIMIT_BY_PERIOD, REPORTING_ENTITIES,
    REPORTING_REPORT_FIELDNAMES, STATUS_RPT_EXTRACTED, STATUS_RPT_FAILED,
    STATUS_RPT_PENDING, STATUS_RPT_SKIPPED,
)
from .reports import summarize  # re-exported: callers use report_engine.summarize

MODE_FULL = "full"
MODE_DELTA = "delta"


def _noop(*_args, **_kwargs):
    return None


def _never_cancel():
    return False


class ExtractPlan:
    """One entity's intended extract."""

    def __init__(self, entity, spec, reporting_period):
        self.entity = entity
        self.spec = spec
        self.endpoint = spec["endpoint"]
        # Entities without a reporting period ignore it entirely.
        self.reporting_period = reporting_period if spec.get("has_period") else ""
        self.bookmark = None
        self.mode = MODE_FULL
        self.estimated_calls = 1
        self.rows = 0
        self.date_range = ""
        self.status = ""
        self.notes = ""
        self.include = False

    @property
    def writable(self):
        return self.status in (STATUS_RPT_PENDING, STATUS_RPT_EXTRACTED)

    def to_report_row(self):
        return {
            "entity": self.entity,
            "mode": self.mode,
            "reporting_period": self.reporting_period,
            "rows": self.rows,
            "date_range": self.date_range,
            "status": self.status,
            "notes": self.notes,
        }


def rate_limit_for(period):
    """(max_queries, window_seconds) for a reporting period."""
    return RATE_LIMIT_BY_PERIOD.get(period, DEFAULT_RATE_LIMIT)


def plan_extract(store, entities, reporting_period, on_progress=_noop,
                 should_cancel=_never_cancel):
    """Build ExtractPlans. Performs NO network calls and NO writes.

    Decides full-load vs bookmark delta per entity, and warns when the whole
    selection will not fit inside the period's published query allowance.
    """
    plans = []
    total = len(entities)
    for index, entity in enumerate(entities, 1):
        if should_cancel():
            break
        spec = REPORTING_ENTITIES.get(entity)
        if spec is None:
            plan = ExtractPlan(entity, {"endpoint": "", "has_period": False},
                               reporting_period)
            plan.status = STATUS_RPT_SKIPPED
            plan.notes = f"Unknown entity {entity!r}"
            plans.append(plan)
            continue

        plan = ExtractPlan(entity, spec, reporting_period)
        if spec.get("bookmarkable"):
            plan.bookmark = store.get_bookmark(plan.endpoint,
                                               plan.reporting_period)
        if plan.bookmark:
            plan.mode = MODE_DELTA
            plan.notes = "Delta since the last extract"
        else:
            plan.mode = MODE_FULL
            plan.notes = ("Full load" if spec.get("bookmarkable")
                          else "Full load (endpoint has no bookmark)")

        # One call per entity: sub-entities come back expanded in the same
        # request, which is far cheaper than paging.
        plan.estimated_calls = 1
        subs = spec.get("sub_entities") or {}
        if subs:
            plan.notes += f"; expands {len(subs)} sub-entity set(s)"
        plan.status = STATUS_RPT_PENDING
        plan.include = True
        plans.append(plan)

        on_progress({
            "phase": "plan", "index": index, "total": total,
            "name": entity, "status": plan.status, "message": plan.notes,
        })

    _flag_rate_limit_budget(plans, reporting_period)
    return plans


def _flag_rate_limit_budget(plans, reporting_period):
    """Warn when the selection exceeds the period's query allowance.

    The limits scale inversely with how much data the period covers, so a wide
    selection on AllData is the classic way to get throttled.
    """
    included = [p for p in plans if p.status == STATUS_RPT_PENDING]
    if not included:
        return
    # Entities without a period are not billed against the period's budget.
    billed = [p for p in included if p.reporting_period]
    if not billed:
        return
    needed = sum(p.estimated_calls for p in billed)
    max_calls, window = rate_limit_for(reporting_period)
    if needed > max_calls:
        warning = (f"exceeds the '{reporting_period}' allowance of {max_calls} "
                   f"queries per {window}s ({needed} needed) - the run will "
                   f"self-throttle and take longer")
        for plan in billed:
            plan.notes += f"; {warning}"


def run_extract(client, store, plans, on_progress=_noop,
                should_cancel=_never_cancel, started_at=""):
    """Fetch and store each approved plan. Returns report rows for ALL plans.

    Per-entity failures are recorded and never abort the run, matching
    write_locations/upload_images. A new bookmark is stored only after its rows
    are committed, so an interrupted run never skips data next time.
    """
    to_run = [p for p in plans if p.include and p.writable]
    total = len(to_run)
    done = 0

    for plan in to_run:
        if should_cancel():
            break
        done += 1

        rows, new_bookmark, date_range, error = client.fetch(
            plan.entity,
            reporting_period=plan.reporting_period or None,
            bookmark=plan.bookmark)
        plan.date_range = date_range or ""

        if error:
            plan.status = STATUS_RPT_FAILED
            plan.notes = _append_note(plan.notes, error)
        else:
            try:
                written, dropped = store.upsert_entity(plan.entity, rows)
            except Exception as exc:   # a bad payload must not kill the run
                plan.status = STATUS_RPT_FAILED
                plan.notes = _append_note(plan.notes, f"store error: {exc}")
            else:
                plan.rows = sum(written.values())
                plan.status = STATUS_RPT_EXTRACTED
                detail = ", ".join(f"{t}={n}" for t, n in written.items() if n)
                if detail:
                    plan.notes = _append_note(plan.notes, detail)
                # Rows the store had to discard because the registry's key for
                # that table is not a field this instance returns. Several
                # sub-entity keys are unverified guesses, and a silent zero
                # looks exactly like "there was no data" - so say so loudly.
                if dropped:
                    lost = ", ".join(f"{t} ({n} rows)"
                                     for t, n in sorted(dropped.items()))
                    plan.notes = _append_note(
                        plan.notes,
                        f"WARNING: discarded rows with no primary key: {lost}"
                        " - the registry's key for those tables is probably"
                        " wrong for this instance; run live_check_reporting.py")
                # Only now is it safe to advance the watermark.
                if new_bookmark and plan.spec.get("bookmarkable"):
                    store.set_bookmark(plan.endpoint, plan.reporting_period,
                                       new_bookmark, when=started_at)

        store.record_run(plan.entity, plan.reporting_period, plan.mode,
                         plan.rows, plan.date_range, plan.status, plan.notes,
                         started_at=started_at)

        on_progress({
            "phase": "extract", "index": done, "total": total,
            "name": plan.entity, "status": plan.status, "message": plan.notes,
        })

    return [p.to_report_row() for p in plans]


def _append_note(notes, extra):
    return (notes + "; " if notes else "") + extra


def write_report(report_rows, path, fieldnames=REPORTING_REPORT_FIELDNAMES):
    """Write the extract report to a CSV at path."""
    return reports.write_report(report_rows, path, fieldnames)
