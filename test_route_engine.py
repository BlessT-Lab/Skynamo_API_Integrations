"""Offline tests for the route planning engine.

A synthetic instance around Johannesburg with the awkward cases planted in it:
a customer with no coordinates, an inactive one, an approximate pin, a pin in
Cape Town, a broken frequency record, a "twice a week" customer, a visit
already scheduled, a customer two reps share, and a rep whose week is far too
full. The visit history is long enough to span several real pages, so the
interaction walk is exercised too.
"""

import csv
import json
import os
import random
import shutil
import tempfile
from datetime import date, datetime, timedelta

from skynamo_geo.config import (
    PAGE_SIZE, ROUTE_REPORT_FIELDNAMES, STATUS_RT_ALREADY,
    STATUS_RT_INVALID_FREQ, STATUS_RT_NO_LOCATION, STATUS_RT_NOT_FOUND,
    STATUS_RT_OUT_OF_RANGE, STATUS_RT_OVER_CAPACITY, STATUS_RT_PLANNED,
)
from skynamo_geo.route_engine import (
    UNPLANNED, RepInput, RouteOptions, build_route_plan, load_planning_data,
    write_report,
)
from skynamo_geo.routing import day_minutes, matrices

TODAY = date(2026, 10, 6)                 # a Tuesday
MONDAY = date(2026, 10, 12)               # the week being planned

# ---------------------------------------------------------------------------
# A synthetic instance
# ---------------------------------------------------------------------------
rng = random.Random(1006)


def cust(cid, lat, lng, assigned, approx=False, accuracy=25, code=None):
    return {"id": cid, "code": code or f"C{cid:03d}", "name": f"Shop {cid}",
            "active": True, "assigned_users": assigned,
            "location": {"latitude": lat, "longitude": lng,
                         "accuracy": accuracy, "is_approximate": approx}}


REP_A, REP_B, REP_C = 11, 22, 33
customers = []
for cid in range(1, 15):                   # rep A's patch, north
    customers.append(cust(cid, -26.10 + rng.uniform(-0.08, 0.08),
                          28.05 + rng.uniform(-0.08, 0.08), [REP_A]))
for cid in range(15, 27):                  # rep B's patch, south-west
    customers.append(cust(cid, -26.26 + rng.uniform(-0.08, 0.08),
                          27.93 + rng.uniform(-0.08, 0.08), [REP_B]))
customers.append(cust(27, 0, 0, [REP_A]))                     # no coordinates
customers.append(cust(28, -26.12, 28.04, [REP_A], approx=True, accuracy=3000))
customers.append(cust(29, -33.92, 18.42, [REP_A]))            # Cape Town pin
customers.append(cust(30, -26.11, 28.06, [REP_A, REP_B]))     # shared
customers.append(cust(31, -26.09, 28.03, [REP_A]))            # twice a week
inactive = cust(32, -26.10, 28.05, [REP_A])
inactive["active"] = False
customers.append(inactive)
for cid in range(40, 100):                 # rep C: far more than a week holds
    customers.append(cust(cid, -25.75 + rng.uniform(-0.1, 0.1),
                          28.19 + rng.uniform(-0.1, 0.1), [REP_C]))


def freq(cid, uid, f=1, c=1, p="week"):
    return {"customer_id": cid, "customer_code": f"C{cid:03d}",
            "customer_name": f"Shop {cid}", "user_id": uid,
            "frequency": f, "cycle": c, "period": p}


frequencies = []
frequencies += [freq(cid, REP_A) for cid in range(1, 15)]
frequencies += [freq(cid, REP_B, c=2) for cid in range(15, 27)]
frequencies += [freq(27, REP_A), freq(28, REP_A), freq(29, REP_A),
                freq(30, REP_A), freq(30, REP_B),
                freq(31, REP_A, f=2), freq(32, REP_A),
                freq(5, REP_A, f=0)]                           # broken record
frequencies += [freq(cid, REP_C) for cid in range(40, 100)]

# History: everyone seen 8-13 days ago, except 15-20 (seen 3 days ago, so not
# due on their fortnightly cycle) and 31 (never visited). Plus noise that is
# not a visit, which must be ignored.
interactions = []
iid = 0
for c in customers:
    cid = c["id"]
    if cid == 31:
        continue
    uids = c["assigned_users"]
    ago = 3 if 15 <= cid <= 20 else rng.randint(8, 13)
    for uid in uids:
        iid += 1
        interactions.append({"id": iid, "customer_id": cid, "user_id": uid,
                             "is_visit": True,
                             "date": f"{TODAY - timedelta(days=ago)}T10:15:00"})
while len(interactions) < 450:            # three pages of 200
    iid += 1
    interactions.append({"id": iid, "customer_id": rng.randint(1, 30),
                         "user_id": REP_A, "is_visit": False,
                         "date": f"{TODAY}T09:00:00"})

# An open visit for rep A at customer 3, inside the week; and a completed one
# that must NOT count.
scheduled = [
    {"id": 900, "customer_id": 3, "assigned_user_id": REP_A,
     "due_date": f"{MONDAY + timedelta(days=1)}T09:00:00",
     "create_date": f"{TODAY}T08:00:00"},
    {"id": 901, "customer_id": 4, "assigned_user_id": REP_A,
     "due_date": f"{MONDAY}T09:00:00", "completed_date": f"{TODAY}T08:00:00",
     "create_date": f"{TODAY}T08:00:00"},
]


class FakeClient:
    """Read-only fake honouring the API's shape: interactions only by page."""

    def __init__(self):
        self.calls = []

    def fetch_users(self):
        self.calls.append("users")
        return [{"id": u, "display_name": n} for u, n in
                ((REP_A, "Anna"), (REP_B, "Ben"), (REP_C, "Cara"))], ""

    def fetch_visit_frequencies(self, should_cancel=None):
        self.calls.append("frequencies")
        return list(frequencies), ""

    def fetch_all_customers(self, on_page=None, active_only=True):
        self.calls.append("customers")
        rows = [c for c in customers if c.get("active", True) or not active_only]
        if on_page:
            on_page(len(rows), len(rows))
        return rows

    def get_configuration(self):
        self.calls.append("configuration")
        return {"time_zone": {"utc_offset": "+02:00"}}, ""

    def fetch_scheduled_visits(self, created_since=None, should_cancel=None):
        assert isinstance(created_since, date), "filter is by create date only"
        self.calls.append("scheduled")
        return [v for v in scheduled
                if date.fromisoformat(v["create_date"][:10]) >= created_since], ""

    def fetch_interactions_page(self, page_number):
        assert page_number >= 1
        self.calls.append(("interactions", page_number))
        lo = (page_number - 1) * PAGE_SIZE
        return (interactions[lo:lo + PAGE_SIZE],
                {"total_item_count": len(interactions)}, "")


def reps(include_c=True):
    out = [RepInput(REP_A, "Anna", (-26.10, 28.05)),
           RepInput(REP_B, "Ben", (-26.26, 27.93), end=(-26.20, 28.00))]
    if include_c:
        out.append(RepInput(REP_C, "Cara", (-25.75, 28.19)))
    return out


ROLES = [("Address", "other")]
for c in customers:                        # an address for nav fallbacks
    c["custom_fields"] = [{"name": "Address", "value": f"{c['id']} Main Rd"}]

opts = RouteOptions(MONDAY, n_days=5, max_hours=8, max_stops=8,
                    address_roles=ROLES)
tmp = tempfile.mkdtemp(prefix="route_engine_")
try:
    cache = os.path.join(tmp, "history.json")

    # --- load: reads everything, writes nothing, walks the history ---------
    client = FakeClient()
    events = []
    data, err = load_planning_data(client, opts, cache_path=cache, today=TODAY,
                                   on_progress=events.append)
    assert err == "", err
    pages = [c for c in client.calls if isinstance(c, tuple)]
    assert [p[1] for p in pages] == [1, 2, 3], pages
    assert len(data.customers) == len(customers) - 1      # inactive excluded
    assert data.visits[(1, REP_A)], "history should be keyed by pair"
    assert (31, REP_A) not in data.visits                  # never visited
    # non-visit interactions never count as a visit
    assert all(d < TODAY for ds in data.visits.values() for d in ds)
    assert [v["id"] for v in data.open_visits] == [900], data.open_visits
    est = [e for e in events if e["phase"] == "history" and e["status"] == "estimate"]
    assert est and "450 interactions" in est[0]["message"], est

    # the same day again reads one page (for the total) and uses the cache
    client2 = FakeClient()
    data2, err = load_planning_data(client2, opts, cache_path=cache, today=TODAY)
    assert err == ""
    assert [c for c in client2.calls if isinstance(c, tuple)] == [("interactions", 1)]
    assert data2.visits == data.visits

    # a cancelled walk says so; an over-limit history refuses before walking
    walking = {"on": False}
    def note_history(ev):
        if ev["phase"] == "history":
            walking["on"] = True
    walker = FakeClient()
    _d, err = load_planning_data(walker, opts, today=TODAY,
                                 on_progress=note_history,
                                 should_cancel=lambda: walking["on"])
    assert err == "Cancelled", err
    assert ("interactions", 3) not in walker.calls, "cancel must stop the walk"

    # --- build: pure ---------------------------------------------------------
    plan = build_route_plan(data, reps(), opts, instance="acme")
    by_status = {}
    for s in plan.stops:
        by_status.setdefault(s.status, []).append(s)

    # every unplanned stop says why, and its status is a known one
    for s in plan.stops:
        if s.status != STATUS_RT_PLANNED:
            assert s.status in UNPLANNED and s.notes and s.error, vars(s)
            assert s.day is None

    def one(status, cid):
        hits = [s for s in by_status.get(status, []) if s.customer_id == cid]
        assert hits, (status, cid, {k: len(v) for k, v in by_status.items()})
        return hits[0]

    one(STATUS_RT_NO_LOCATION, 27)
    one(STATUS_RT_NOT_FOUND, 32)
    one(STATUS_RT_INVALID_FREQ, 5)
    one(STATUS_RT_OUT_OF_RANGE, 29)
    one(STATUS_RT_ALREADY, 3)
    assert 4 in {s.customer_id for s in plan.planned()}, \
        "a COMPLETED scheduled visit must not count as covering the week"

    # customers seen 3 days ago on a fortnightly cycle are not due at all
    assert not {s.customer_id for s in plan.stops} & set(range(15, 21))

    # approximate pin: planned, flagged, navigates by address
    approx = one(STATUS_RT_PLANNED, 28)
    assert approx.approximate and "approximate" in approx.notes
    row = approx.to_report_row()
    assert "Main+Rd" in row["waze"] or "Main%20Rd" in row["waze"], row["waze"]
    exact = one(STATUS_RT_PLANNED, 1)
    assert "ll=" in exact.to_report_row()["waze"]

    # twice a week, never visited: two visits on different, spaced days
    twice = sorted((s for s in plan.planned() if s.customer_id == 31),
                   key=lambda s: s.day)
    assert len(twice) == 2, [vars(s) for s in plan.stops if s.customer_id == 31]
    assert (twice[1].day - twice[0].day).days >= twice[1].min_gap_days

    # the shared customer is visited by both reps; same-day overlap is noted
    shared = [s for s in plan.planned() if s.customer_id == 30]
    assert {s.user_id for s in shared} == {REP_A, REP_B}
    if shared[0].day == shared[1].day:
        assert all("also planned for" in s.notes for s in shared)

    # rep C has 60 customers due and 5 x 8 slots: the week is full
    over = [s for s in by_status.get(STATUS_RT_OVER_CAPACITY, [])
            if s.user_id == REP_C]
    kept = [s for s in plan.planned() if s.user_id == REP_C and s.occurrence == 1]
    assert over and len(kept) <= 40
    assert max(s.priority for s in over) <= min(s.priority for s in kept), \
        "a dropped stop must never outrank a planned one"

    # --- every day respects its limits, and its clock adds up ---------------
    for day in plan.days:
        assert day.date in plan.dates
        assert 1 <= len(day.stops) <= opts.max_stops
        pts = [day.start, day.end] + [(s.lat, s.lng) for s in day.stops]
        _k, m = matrices(pts, opts.road_factor, opts.speed_kmh,
                         opts.min_leg_minutes)
        used = day_minutes(list(range(2, len(pts))), m, 0, 1,
                           opts.service_minutes)
        assert used <= opts.max_minutes + 1e-6, (day.rep_name, day.date, used)
        assert [s.sequence for s in day.stops] == list(range(1, len(day.stops) + 1))
        t = day.leave
        for s in day.stops:
            assert s.arrive >= t and s.depart > s.arrive and s.day == day.date
            t = s.depart
        assert day.finish >= t
        assert day.leave.hour == 8 and day.leave.minute == 0

    # no customer is booked twice on one day by the same rep
    seen = set()
    for s in plan.planned():
        key = (s.user_id, s.customer_id, s.day)
        assert key not in seen, key
        seen.add(key)

    # a rep with no start address is warned about, not silently skipped
    no_start = reps(include_c=False) + [RepInput(REP_C, "Cara", None)]
    p2 = build_route_plan(data, no_start, opts)
    assert any("Cara" in w and "no start" in w for w in p2.warnings)
    assert not [s for s in p2.stops if s.user_id == REP_C]

    # --- deterministic, whatever order the records arrived in ---------------
    def fingerprint(p):
        return [(s.user_id, s.day, s.sequence, s.customer_id, s.status)
                for s in p.stops]
    again = build_route_plan(data, reps(), opts, instance="acme")
    assert fingerprint(again) == fingerprint(plan)
    shuffled = data.__class__()
    shuffled.__dict__.update(data.__dict__)
    shuffled.frequencies = list(reversed(data.frequencies))
    assert fingerprint(build_route_plan(shuffled, reps(), opts)) == fingerprint(plan)

    # --- the week can change after loading, without refetching -------------
    # The open visit at customer 3 is due in the week of 12 Oct. Planning the
    # FOLLOWING week from the same data must not treat it as covering that one.
    later = RouteOptions(MONDAY + timedelta(days=7), n_days=5, max_hours=8,
                         max_stops=8)
    p3 = build_route_plan(data, reps(include_c=False), later)
    assert not [s for s in p3.stops
                if s.customer_id == 3 and s.status == STATUS_RT_ALREADY]

    # --- turning what a person typed into a start point --------------------
    from skynamo_geo.route_engine import resolve_place

    class Found:
        def __init__(self, lat, lng, low, precision="OSM_AREA"):
            self.lat, self.lng = lat, lng
            self.is_low_confidence, self.precision = low, precision

    class FakeGeocoder:
        def __init__(self, result):
            self.result, self.asked = result, []
        def geocode(self, text):
            self.asked.append(text)
            return self.result

    geo = FakeGeocoder(Found(-26.1, 28.05, low=False))
    assert resolve_place("-26.1, 28.05", geo) == ((-26.1, 28.05),
                                                  "coordinates as typed", "")
    assert geo.asked == [], "typed coordinates are never looked up"
    point, note, err = resolve_place("Sandton City", geo)
    assert point == (-26.1, 28.05) and err == "" and geo.asked == ["Sandton City"]
    point, note, err = resolve_place("Somewhere", FakeGeocoder(
        Found(-26.0, 28.0, low=True)))
    assert point and "approximate" in note, note
    assert resolve_place("Nowhere", FakeGeocoder(None))[2].startswith("address not found")
    assert resolve_place("", geo)[2] == "no start address"
    assert resolve_place("95, 10", geo)[2] == "coordinates out of range"

    # --- the report ------------------------------------------------------------
    rows = [s.to_report_row() for s in plan.stops]
    assert all(list(r) == ROUTE_REPORT_FIELDNAMES for r in rows)
    out = write_report(rows, os.path.join(tmp, "route.csv"))
    with open(out, encoding="utf-8-sig") as f:
        header = next(csv.reader(f))
    assert header == ROUTE_REPORT_FIELDNAMES
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("All route engine tests passed")
