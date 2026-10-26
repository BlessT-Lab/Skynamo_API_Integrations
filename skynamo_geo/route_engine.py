"""Route planning engine - UI-agnostic.

Two stages, with the network kept out of the middle:

  load_planning_data(client, ...)   network, cancellable, NO writes:
                                    users, visit frequencies, customers, the
                                    instance time zone, open scheduled visits,
                                    and each customer's recent visit history
  build_route_plan(data, reps, ...) pure - no network at all: who is due,
                                    which day, in what order, at what time

Keeping the planner pure means changing a setting re-plans instantly without
refetching, and it can be tested exhaustively with no fake client.

Same contracts as the other engines: on_progress(event) with
{phase, index, total, name, status, message}, and should_cancel().
"""

import bisect
import json
import math
import os
import re
from datetime import date, datetime, time, timedelta

from . import reports
from .reports import summarize  # re-exported, like the other engines
from .reports import (  # shared; see reports.py
    append_note as _append_note, never_cancel as _never_cancel,
    noop as _noop,
)
from .config import (
    PAGE_SIZE, ROUTE_APPROX_ACCURACY_M, ROUTE_BALANCE_WEIGHT,
    ROUTE_DEFAULT_DAY_START, ROUTE_DEFAULT_DAYS, ROUTE_DEFAULT_MAX_HOURS,
    ROUTE_DEFAULT_MAX_STOPS, ROUTE_DEFAULT_ROAD_FACTOR,
    ROUTE_DEFAULT_SERVICE_MINUTES, ROUTE_DEFAULT_SPEED_KMH,
    ROUTE_DEFAULT_WEEKDAYS, ROUTE_FAR_START_KM, ROUTE_MAX_HISTORY_REQUESTS,
    ROUTE_MAX_RADIUS_KM, ROUTE_MIN_LEG_MINUTES, ROUTE_REPORT_FIELDNAMES,
    ROUTE_SCHEDULED_LOOKBACK_DAYS, STATUS_RT_ALREADY, STATUS_RT_INVALID_FREQ,
    STATUS_RT_NO_LOCATION, STATUS_RT_NOT_FOUND, STATUS_RT_NOT_ROUTED,
    STATUS_RT_OUT_OF_RANGE,
    STATUS_RT_OVER_CAPACITY, STATUS_RT_PLANNED,
)
from .customers import build_query, has_coordinates
from .route_map import stop_link, waze_link
from .routing import (
    assign_to_dates, cheapest_insertion, day_minutes, haversine_km, matrices,
    nearest_neighbour, split_tour, two_opt,
)
from .visit_due import (
    MAX_OCCURRENCES, FrequencyError, label, localize, min_gap_days, occurrences,
    parse_api_datetime, parse_frequency, parse_utc_offset, window_days,
    working_days,
)

UNPLANNED = (STATUS_RT_ALREADY, STATUS_RT_INVALID_FREQ, STATUS_RT_NO_LOCATION,
             STATUS_RT_NOT_FOUND, STATUS_RT_NOT_ROUTED, STATUS_RT_OUT_OF_RANGE,
             STATUS_RT_OVER_CAPACITY)

# Day-to-date scoring. Visiting before a stop's earliest allowed day breaks
# the spacing between a customer's visits, so it costs far more than lateness;
# early is cheapest - it only wastes a little of the visit's value.
_PENALTY_BEFORE_EARLIEST = 1000
_PENALTY_LATE_PER_DAY = 10
_PENALTY_EARLY_PER_DAY = 1


def _parse_clock(text, default=ROUTE_DEFAULT_DAY_START):
    """'08:00' -> time(8, 0); falls back to the default on nonsense."""
    for candidate in (text, default):
        match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(candidate or ""))
        if match and int(match.group(1)) < 24 and int(match.group(2)) < 60:
            return time(int(match.group(1)), int(match.group(2)))
    return time(8, 0)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

class RouteOptions:
    """Everything that shapes a plan, apart from who and where."""

    def __init__(self, start_date, n_days=ROUTE_DEFAULT_DAYS,
                 weekdays=ROUTE_DEFAULT_WEEKDAYS, holidays=(),
                 day_start=ROUTE_DEFAULT_DAY_START,
                 max_hours=ROUTE_DEFAULT_MAX_HOURS,
                 max_stops=ROUTE_DEFAULT_MAX_STOPS,
                 service_minutes=ROUTE_DEFAULT_SERVICE_MINUTES,
                 road_factor=ROUTE_DEFAULT_ROAD_FACTOR,
                 speed_kmh=ROUTE_DEFAULT_SPEED_KMH,
                 min_leg_minutes=ROUTE_MIN_LEG_MINUTES,
                 max_radius_km=ROUTE_MAX_RADIUS_KM,
                 balance=ROUTE_BALANCE_WEIGHT, count_any_rep=False,
                 address_roles=()):
        self.start_date = start_date
        self.n_days = int(n_days)
        self.weekdays = tuple(weekdays)
        self.holidays = tuple(holidays)
        self.day_start = _parse_clock(day_start)
        self.max_hours = float(max_hours)
        self.max_stops = int(max_stops)
        self.service_minutes = float(service_minutes)
        self.road_factor = float(road_factor)
        self.speed_kmh = float(speed_kmh)
        self.min_leg_minutes = float(min_leg_minutes)
        self.max_radius_km = float(max_radius_km)
        self.balance = float(balance)
        self.count_any_rep = bool(count_any_rep)
        # Ordered (field, role) pairs - the Geolocation tab's address mapping,
        # used to give approximate pins an address to navigate to.
        self.address_roles = list(address_roles)

    @property
    def max_minutes(self):
        return self.max_hours * 60.0

    def dates(self):
        return working_days(self.start_date, self.n_days, self.weekdays,
                            self.holidays)


class RepInput:
    """A rep to plan for, and where their day starts and ends."""

    def __init__(self, user_id, name, start, end=None, include=True):
        self.user_id = user_id
        self.name = name
        self.start = tuple(start) if start else None
        self.end = tuple(end) if end else self.start
        self.include = include


class PlanningData:
    """What load_planning_data fetched. Plain data; build reads only this."""

    def __init__(self):
        self.users = []
        self.frequencies = []
        self.customers = {}          # id -> customer
        self.visits = {}             # (customer_id, user_id) -> [dates newest first]
        self.visits_any = {}         # customer_id -> [dates newest first]
        self.open_visits = []        # open scheduled visits due inside the window
        self.configuration = {}
        # The instance's time zone: every date - visit history, open visits,
        # the day's clock - is read in it. None means it could not be read, and
        # this PC's zone is used for all of them instead (never a mixture).
        self.tz = None
        self.warnings = []
        self.history_note = ""


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

class RouteStop:
    """One visit a rep owes a customer - planned, or with the reason it isn't."""

    def __init__(self, user_id, rep_name, customer):
        self.user_id = user_id
        self.rep_name = rep_name
        self.customer_id = customer.get("id") if customer else None
        self.code = (customer or {}).get("code", "")
        self.name = (customer or {}).get("name", "")
        location = (customer or {}).get("location") or {}
        self.lat = location.get("latitude")
        self.lng = location.get("longitude")
        self.accuracy = location.get("accuracy")
        self.approximate = bool(location.get("is_approximate")) or (
            self.accuracy is not None and self.accuracy > ROUTE_APPROX_ACCURACY_M)
        self.address = ""
        self.frequency_label = ""
        self.last_visit = None
        self.earliest = None
        self.due = None
        self.overdue_days = None
        self.priority = 0.0
        self.occurrence = 1
        self.min_gap_days = 1
        self.day = None              # date, once planned
        self.sequence = 0
        self.arrive = None
        self.depart = None
        self.leg_km = 0.0
        self.leg_minutes = 0.0
        self.status = STATUS_RT_PLANNED
        self.notes = ""
        self.error = ""              # a cause with no per-stop specifics
        self.include = True
        self.token = ""              # set when written (phase 2)
        self.scheduled_visit_id = None

    @property
    def key(self):
        return (self.customer_id if self.customer_id is not None else -1,
                self.occurrence)

    def mark(self, status, note, error=None):
        self.status = status
        self.notes = _append_note(self.notes, note)
        self.error = error or note
        self.include = False

    def to_report_row(self):
        located = self.lat is not None and self.lng is not None
        return {
            "rep": self.rep_name,
            "user_id": self.user_id,
            "day": self.day.isoformat() if self.day else "",
            "sequence": self.sequence or "",
            "customer_id": self.customer_id,
            "code": self.code,
            "name": self.name,
            "status": self.status,
            "arrive": self.arrive.strftime("%H:%M") if self.arrive else "",
            "depart": self.depart.strftime("%H:%M") if self.depart else "",
            "leg_km": f"{self.leg_km:.1f}" if self.day else "",
            "leg_minutes": f"{self.leg_minutes:.0f}" if self.day else "",
            "due_date": self.due.isoformat() if self.due else "",
            "overdue_days": "" if self.overdue_days is None else self.overdue_days,
            "last_visit": self.last_visit.isoformat() if self.last_visit else "",
            "frequency": self.frequency_label,
            "latitude": self.lat if located else "",
            "longitude": self.lng if located else "",
            "approximate": "yes" if self.approximate else "",
            "google_maps": (stop_link(self.lat, self.lng, self.address,
                                      self.approximate) if located else ""),
            "waze": (waze_link(self.lat, self.lng, self.address,
                               self.approximate) if located else ""),
            "notes": self.notes,
        }


class RouteDay:
    """One rep's day: ordered stops between their start and end."""

    def __init__(self, user_id, rep_name, day, start, end):
        self.user_id = user_id
        self.rep_name = rep_name
        self.date = day
        self.start = start
        self.end = end
        self.stops = []
        self.km = 0.0
        self.drive_minutes = 0.0
        self.service_minutes = 0.0
        self.leave = None
        self.finish = None


class RoutePlan:
    def __init__(self, instance, options, dates):
        self.instance = instance
        self.options = options
        self.dates = dates
        self.days = []
        self.stops = []
        self.warnings = []
        self.created_at = datetime.now()
        # True when planning stopped before every rep was routed: the plan is
        # a partial result and must not be presented as the week.
        self.cancelled = False

    def planned(self):
        return [s for s in self.stops if s.status == STATUS_RT_PLANNED]


_LATLNG = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")


def resolve_place(text, geocoder=None):
    """Turn a typed start/end point into coordinates.

    Returns (point, note, error): `point` is (lat, lng) or None. Accepts
    "lat,lng" directly - no lookup, and the only way to be exact about a
    place OpenStreetMap does not know - or an address, which is geocoded. A
    coarse geocode (a suburb or town centroid) is accepted but noted: every
    day's first and last legs are measured from it.
    """
    text = (text or "").strip()
    if not text:
        return None, "", "no start address"
    match = _LATLNG.match(text)
    if match:
        lat, lng = float(match.group(1)), float(match.group(2))
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            return None, "", "coordinates out of range"
        return (lat, lng), "coordinates as typed", ""
    if geocoder is None:
        return None, "", "address not looked up yet"
    result = geocoder.geocode(text)
    if result is None:
        return None, "", "address not found - try 'lat,lng'"
    note = "found"
    if result.is_low_confidence:
        note = (f"approximate ({result.precision}) - day start and end legs "
                f"are measured from an area, not the address")
    return (result.lat, result.lng), note, ""


# ---------------------------------------------------------------------------
# Stage 1: load (network)
# ---------------------------------------------------------------------------

def _visit_date(text, tz):
    """The local calendar date of an interaction's timestamp."""
    dt = parse_api_datetime(text)
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(tz) if tz is not None else dt.astimezone()
    return dt.date()


def _keep_newest(bucket, key, d, keep):
    """Insert d into bucket[key] (newest first), keeping only `keep` dates."""
    dates = bucket.setdefault(key, [])
    # bisect on negated ordinals keeps newest-first ordering
    neg = [-x.toordinal() for x in dates]
    i = bisect.bisect_left(neg, -d.toordinal())
    if i < len(dates) and dates[i] == d:
        return
    dates.insert(i, d)
    del dates[keep:]


def _tz_key(tz):
    """A stable name for a time zone, for the cache key."""
    return "local" if tz is None else tz.tzname(None)


def _cache_matches(cache, total, today, keep, tz):
    """Whether a cache can stand in for walking the history again.

    The dates in it depend on more than the day and the interaction count:
    how many visits were kept per pair (raise a customer to "twice a week"
    and a list trimmed to one visit makes them look never visited) and which
    time zone they were dated in.
    """
    return bool(cache
                and cache.get("fetched_on") == today.isoformat()
                and cache.get("total") == total
                and int(cache.get("keep") or 0) >= keep
                and cache.get("tz") == _tz_key(tz))


def _read_cache(path):
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_cache(path, data, total, today, keep):
    if not path:
        return
    payload = {
        "fetched_on": today.isoformat(), "total": total, "keep": keep,
        "tz": _tz_key(data.tz),
        "pairs": {f"{c}|{u}": [d.isoformat() for d in ds]
                  for (c, u), ds in data.visits.items()},
        "any": {str(c): [d.isoformat() for d in ds]
                for c, ds in data.visits_any.items()},
    }
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except OSError:
        pass                      # a cache that cannot be written is not fatal


def _load_cache_into(data, cache):
    def key_of(text):
        try:
            return int(text)
        except ValueError:
            return text
    for k, ds in (cache.get("pairs") or {}).items():
        c, _, u = k.partition("|")
        data.visits[(key_of(c), key_of(u))] = [date.fromisoformat(x) for x in ds]
    for c, ds in (cache.get("any") or {}).items():
        data.visits_any[key_of(c)] = [date.fromisoformat(x) for x in ds]


def _load_history(client, data, keep, cache_path, today, on_progress,
                  should_cancel):
    """Each pair's newest visits, by walking /interactions (it has no filter).

    Page 1 gives the total, so the cost is known - and reported - before the
    walk. A same-day re-run with an unchanged total reuses the cache instead.
    """
    items, info, error = client.fetch_interactions_page(
        1, should_cancel=should_cancel)
    if error:
        return f"Could not read the visit history: {error}"
    total = int(info.get("total_item_count") or 0)
    pages = max(1, math.ceil(total / PAGE_SIZE)) if total else 1

    cache = _read_cache(cache_path)
    if _cache_matches(cache, total, today, keep, data.tz):
        _load_cache_into(data, cache)
        data.history_note = (f"Visit history from today's cache "
                             f"({total:,} interactions).")
        on_progress({"phase": "history", "index": pages, "total": pages,
                     "name": "", "status": "cached",
                     "message": data.history_note})
        return ""
    if pages > ROUTE_MAX_HISTORY_REQUESTS:
        return (f"The visit history is {total:,} interactions ({pages:,} "
                f"requests), over the {ROUTE_MAX_HISTORY_REQUESTS:,}-request "
                f"limit for one run.")

    on_progress({"phase": "history", "index": 1, "total": pages, "name": "",
                 "status": "estimate",
                 "message": f"{total:,} interactions - {pages:,} request(s)"})
    page_number = 1
    while True:
        for item in items:
            if not item.get("is_visit"):
                continue
            d = _visit_date(item.get("date"), data.tz)
            cid, uid = item.get("customer_id"), item.get("user_id")
            if d is None or cid is None:
                continue
            _keep_newest(data.visits_any, cid, d, keep)
            if uid is not None:
                _keep_newest(data.visits, (cid, uid), d, keep)
        on_progress({"phase": "history", "index": page_number, "total": pages,
                     "name": "", "status": "", "message": ""})
        if len(items) < PAGE_SIZE or page_number >= pages:
            break
        if should_cancel():
            return "Cancelled"
        page_number += 1
        items, _info, error = client.fetch_interactions_page(
            page_number, should_cancel=should_cancel)
        if error:
            return f"Visit history stopped at page {page_number}: {error}"
        if not items:
            break
    data.history_note = f"Visit history read: {total:,} interactions."
    _write_cache(cache_path, data, total, today, keep)
    return ""


def load_planning_data(client, cache_path=None, today=None,
                       on_progress=_noop, should_cancel=_never_cancel):
    """Fetch everything a plan needs. Returns (PlanningData, error). NO writes.

    Takes no plan settings on purpose: what is loaded must not depend on the
    week being planned, or changing the week would need a refetch - and the
    time zone, which it once took from them, belongs to the instance.
    """
    today = today or date.today()
    data = PlanningData()

    def step(phase, message):
        on_progress({"phase": phase, "index": 0, "total": 0, "name": "",
                     "status": "", "message": message})

    # First: every date read below is interpreted in this zone.
    step("configuration", "Reading the instance time zone...")
    data.configuration, _error = client.get_configuration()
    offset = ((data.configuration or {}).get("time_zone") or {}).get("utc_offset")
    data.tz = parse_utc_offset(offset)
    if data.tz is None:
        data.warnings.append(
            f"Could not read the instance's time zone ({offset!r}) - dates and "
            f"times use this PC's time zone instead.")

    step("users", "Reading users...")
    data.users, error = client.fetch_users()
    if error:
        return data, f"Could not read users: {error}"
    if should_cancel():
        return data, "Cancelled"

    step("frequencies", "Reading visit frequencies...")
    data.frequencies, error = client.fetch_visit_frequencies(
        should_cancel=should_cancel)
    if error:
        return data, f"Could not read visit frequencies: {error}"

    step("customers", "Reading customers...")
    try:
        customers = client.fetch_all_customers(
            on_page=lambda n, total: on_progress({
                "phase": "customers", "index": n, "total": total or 0,
                "name": "", "status": "", "message": ""}))
    except Exception as exc:          # the older fetcher raises
        return data, f"Could not read customers: {exc}"
    data.customers = {c.get("id"): c for c in customers}
    if should_cancel():
        return data, "Cancelled"

    step("scheduled", "Reading scheduled visits...")
    # Anchored to today, not the plan window: the week can change after
    # loading, and build_route_plan applies the window itself.
    since = today - timedelta(days=ROUTE_SCHEDULED_LOOKBACK_DAYS)
    visits, error = client.fetch_scheduled_visits(
        created_since=since, should_cancel=should_cancel)
    if error:
        return data, f"Could not read scheduled visits: {error}"
    data.open_visits = [v for v in visits
                        if not (v.get("completed_date")
                                or v.get("completer_visit_id"))]
    data.warnings.append(
        f"Visits already scheduled are only seen if created in the last "
        f"{ROUTE_SCHEDULED_LOOKBACK_DAYS} days - the API cannot search by "
        f"due date.")

    keep = 1
    for record in data.frequencies:
        try:
            keep = max(keep, parse_frequency(record)[0])
        except FrequencyError:
            pass
    keep = min(keep, MAX_OCCURRENCES)
    error = _load_history(client, data, keep, cache_path, today,
                          on_progress, should_cancel)
    if error:
        return data, error
    return data, ""


# ---------------------------------------------------------------------------
# Stage 2: build (pure)
# ---------------------------------------------------------------------------

def _tightest(records):
    """The most demanding of several frequency records for one pair."""
    best, best_rate = None, -1.0
    for rec in records:
        f, c, p = rec["_parsed"]
        rate = f / window_days(c, p)
        if rate > best_rate:
            best, best_rate = rec, rate
    return best


def _penalty(stop, day):
    if stop.earliest and day < stop.earliest:
        return _PENALTY_BEFORE_EARLIEST * (stop.earliest - day).days
    if stop.due and day > stop.due:
        return _PENALTY_LATE_PER_DAY * (day - stop.due).days
    if stop.due and day < stop.due:
        return _PENALTY_EARLY_PER_DAY * (stop.due - day).days
    return 0


def _due_stops(data, reps, options, dates, plan):
    """Every visit each rep owes in the window, with reasons for the rest."""
    by_rep = {r.user_id: r for r in reps if r.include}
    window_start, window_end = dates[0], dates[-1]
    open_count = {}
    for v in data.open_visits:
        # Its local date in the instance's zone - a naive .date() of
        # "Sun 22:30Z" is Sunday even where it is already Monday, which put a
        # covering visit outside the week and planned a duplicate.
        due = _visit_date(v.get("due_date"), data.tz)
        if due is None or not window_start <= due <= window_end:
            continue
        pair = (v.get("customer_id"), v.get("assigned_user_id"))
        open_count[pair] = open_count.get(pair, 0) + 1

    grouped = {}
    for rec in data.frequencies:
        uid = rec.get("user_id")
        if uid not in by_rep:
            continue
        rep = by_rep[uid]
        try:
            rec = dict(rec, _parsed=parse_frequency(rec))
        except FrequencyError as exc:
            stop = RouteStop(uid, rep.name, data.customers.get(rec.get("customer_id"))
                             or {"id": rec.get("customer_id"),
                                 "code": rec.get("customer_code"),
                                 "name": rec.get("customer_name")})
            stop.mark(STATUS_RT_INVALID_FREQ, f"frequency record: {exc}",
                      "Visit frequency record cannot be planned from")
            plan.stops.append(stop)
            continue
        grouped.setdefault((rec.get("customer_id"), uid), []).append(rec)

    stops = []
    for (cid, uid), records in sorted(grouped.items(),
                                      key=lambda kv: (str(kv[0][1]), str(kv[0][0]))):
        rep = by_rep[uid]
        rec = _tightest(records)
        frequency, cycle, period = rec["_parsed"]
        customer = data.customers.get(cid)
        if customer is None:
            stop = RouteStop(uid, rep.name, {"id": cid,
                                             "code": rec.get("customer_code"),
                                             "name": rec.get("customer_name")})
            stop.mark(STATUS_RT_NOT_FOUND,
                      "customer is inactive, deleted or not visible to this key",
                      "Customer is inactive or not found")
            plan.stops.append(stop)
            continue

        history = (data.visits_any.get(cid, []) if options.count_any_rep
                   else data.visits.get((cid, uid), []))
        occ = occurrences(history, frequency, cycle, period,
                          window_start, window_end)
        if not occ:
            continue                       # not due: counted, not listed
        covered = open_count.get((cid, uid), 0)
        gap = min_gap_days(frequency, cycle, period)
        for k, (earliest, due, overdue) in enumerate(occ, 1):
            stop = RouteStop(uid, rep.name, customer)
            stop.frequency_label = label(frequency, cycle, period)
            stop.last_visit = history[0] if history else None
            stop.earliest, stop.due, stop.overdue_days = earliest, due, overdue
            stop.occurrence = k
            stop.min_gap_days = gap
            # never visited: treat as a full window overdue - urgent, but not
            # so urgent that it swamps a customer who is genuinely late
            lag = overdue if overdue is not None else window_days(cycle, period)
            stop.priority = lag - (due - window_start).days - 1000 * (k - 1)
            if overdue is None:
                stop.notes = "no visit on record"
            if len(records) > 1:
                stop.notes = _append_note(
                    stop.notes, f"{len(records)} frequency records - used "
                                f"the tightest ({stop.frequency_label})")
            if uid not in (customer.get("assigned_users") or []):
                stop.notes = _append_note(
                    stop.notes, "frequency set but rep is not assigned to "
                                "this customer")
            if options.address_roles:
                stop.address = build_query(customer, options.address_roles).text
            if stop.approximate:
                stop.notes = _append_note(stop.notes,
                                          "pin is approximate - confirm address")
            if k <= covered:
                stop.mark(STATUS_RT_ALREADY,
                          "an open scheduled visit already covers this",
                          "Already has an open scheduled visit")
            elif not has_coordinates(customer):
                stop.lat = stop.lng = None
                stop.mark(STATUS_RT_NO_LOCATION,
                          "no coordinates - fix in Customer Geolocation",
                          "Customer has no coordinates")
            stops.append(stop)
    return stops


def _drop_one(order, prio, mins, start, end, keys):
    """The stop to give up when the week is full: lowest priority first,
    then the one whose removal saves the most travel, then by key."""
    path = [start] + order + [end]
    detour = {}
    for i in range(1, len(path) - 1):
        a, n, b = path[i - 1], path[i], path[i + 1]
        detour[n] = mins[a][n] + mins[n][b] - mins[a][b]
    return min(order, key=lambda n: (prio[n], -detour[n], keys[n]))


def _route_rep(rep, stops, options, dates, plan, on_progress, tz=None):
    """Plan one rep's week. Mutates stops; appends RouteDays to the plan."""
    start, end = rep.start, rep.end or rep.start
    live = [s for s in stops if s.status == STATUS_RT_PLANNED]
    for s in live:
        if haversine_km(start, (s.lat, s.lng)) > options.max_radius_km:
            s.mark(STATUS_RT_OUT_OF_RANGE,
                   f"more than {options.max_radius_km:.0f} km from the start - "
                   f"check the pin", "Too far from the rep's start point")
    live = [s for s in live if s.status == STATUS_RT_PLANNED]
    if live:
        nearest = min(haversine_km(start, (s.lat, s.lng)) for s in live)
        if nearest > ROUTE_FAR_START_KM:
            plan.warnings.append(
                f"{rep.name}: the start point is {nearest:.0f} km from the "
                f"nearest due customer - check the start address.")
    if not live:
        return

    points = [start, end] + [(s.lat, s.lng) for s in live]
    km, mins = matrices(points, options.road_factor, options.speed_kmh,
                        options.min_leg_minutes)
    node = {id(s): i + 2 for i, s in enumerate(live)}
    stop_at = {i + 2: s for i, s in enumerate(live)}
    keys = {node[id(s)]: s.key for s in live}
    prio = {node[id(s)]: s.priority for s in live}
    S, E = 0, 1
    service, limit = options.service_minutes, options.max_minutes

    for s in live:
        n = node[id(s)]
        if mins[S][n] + service + mins[n][E] > limit:
            s.mark(STATUS_RT_OUT_OF_RANGE,
                   "too far for a day trip from the start point",
                   "Too far for a day trip")

    first = [s for s in live if s.status == STATUS_RT_PLANNED and s.occurrence == 1]
    later = [s for s in live if s.status == STATUS_RT_PLANNED and s.occurrence > 1]

    # More first visits than the week can hold at all: shed the least urgent.
    capacity = len(dates) * options.max_stops
    if len(first) > capacity:
        first.sort(key=lambda s: (-s.priority, s.key))
        for s in first[capacity:]:
            s.mark(STATUS_RT_OVER_CAPACITY, "the week is full",
                   "Week is full - lower priority than what was planned")
        first = first[:capacity]

    nodes = [node[id(s)] for s in first]
    order = two_opt(nearest_neighbour(mins, S, nodes, keys), mins, S, E)
    while True:
        segments = split_tour(order, mins, S, E, service, limit,
                              options.max_stops, len(dates), options.balance)
        if segments is not None or not order:
            segments = segments or []
            break
        victim = _drop_one(order, prio, mins, S, E, keys)
        order.remove(victim)
        stop_at[victim].mark(STATUS_RT_OVER_CAPACITY, "the week is full",
                             "Week is full - lower priority than what was planned")
        order = two_opt(order, mins, S, E, max_passes=5)

    on_progress({"phase": "plan", "index": 0, "total": 0, "name": rep.name,
                 "status": "", "message": f"{rep.name}: {len(order)} stop(s) "
                                          f"in {len(segments)} day(s)"})

    chosen = assign_to_dates(
        segments, dates,
        lambda seg, d: sum(_penalty(stop_at[n], d) for n in seg))
    days = {}
    for seg, d in zip(segments, chosen):
        day = RouteDay(rep.user_id, rep.name, d, start, end)
        day.stops = [stop_at[n] for n in seg]
        days[d] = day

    # Second and later visits to the same customer go in afterwards, on a day
    # far enough from its other visits - a tour would put the two (identical)
    # coordinates side by side and book both on the same day.
    #
    # Each needs the visit before it: an earlier visit is satisfied when it is
    # placed in a day OR already covered by an open scheduled visit. Without
    # this, a customer whose first visit was dropped for capacity got its
    # second booked anyway - insertion is not bound by the split - taking a
    # slot from a more urgent first visit.
    satisfied = {}
    for x in stops:
        if x.status == STATUS_RT_ALREADY:
            satisfied[x.customer_id] = satisfied.get(x.customer_id, 0) + 1
    for day in days.values():
        for x in day.stops:
            satisfied[x.customer_id] = satisfied.get(x.customer_id, 0) + 1
    for s in sorted(later, key=lambda s: (s.occurrence, -s.priority, s.key)):
        if satisfied.get(s.customer_id, 0) < s.occurrence - 1:
            which = ("the customer's first visit" if s.occurrence == 2
                     else f"visit {s.occurrence - 1} to this customer")
            s.mark(STATUS_RT_OVER_CAPACITY,
                   f"not booked: {which} this week was not planned",
                   "An earlier visit to this customer was not planned")
            continue
        n = node[id(s)]
        best = None
        for d in dates:
            if d < s.earliest:
                continue
            day = days.get(d)
            # The dates this customer is already booked on, from the days
            # themselves - stop.day is not set until the days are timed.
            others = [dd.date for dd in days.values()
                      for x in dd.stops if x.customer_id == s.customer_id]
            if any(abs((d - o).days) < s.min_gap_days for o in others):
                continue
            seq = [node[id(x)] for x in day.stops] if day else []
            if len(seq) >= options.max_stops:
                continue
            pos, added = cheapest_insertion(seq, n, mins, S, E)
            total = day_minutes(seq, mins, S, E, service) + added + service
            if total > limit + 1e-9:
                continue
            cost = added + _penalty(s, d)
            if best is None or cost < best[0] - 1e-9:
                best = (cost, d, pos)
        if best is None:
            s.mark(STATUS_RT_OVER_CAPACITY,
                   "no day with room far enough from its other visit(s)",
                   "No day with room for a repeat visit")
            continue
        _cost, d, pos = best
        if d not in days:
            days[d] = RouteDay(rep.user_id, rep.name, d, start, end)
        days[d].stops.insert(pos, s)
        satisfied[s.customer_id] = satisfied.get(s.customer_id, 0) + 1

    for d in sorted(days):
        day = days[d]
        seq = [node[id(x)] for x in day.stops]
        seq = two_opt(seq, mins, S, E)
        day.stops = [stop_at[n] for n in seq]
        _time_day(day, km, mins, node, options, S, E, tz)
        plan.days.append(day)


def _time_day(day, km, mins, node, options, S, E, tz=None):
    """Arrival and departure for each stop, from the day's start time.

    tz: the instance's zone (None = this PC's).
    """
    t = localize(day.date, options.day_start, tz)
    day.leave = t
    here = S
    for i, s in enumerate(day.stops, 1):
        n = node[id(s)]
        s.leg_km = km[here][n] * options.road_factor
        s.leg_minutes = mins[here][n]
        day.km += s.leg_km
        day.drive_minutes += s.leg_minutes
        t += timedelta(minutes=s.leg_minutes)
        s.arrive = t
        t += timedelta(minutes=options.service_minutes)
        s.depart = t
        s.sequence = i
        s.day = day.date
        day.service_minutes += options.service_minutes
        here = n
    tail_km, tail_min = km[here][E] * options.road_factor, mins[here][E]
    day.km += tail_km
    day.drive_minutes += tail_min
    day.finish = t + timedelta(minutes=tail_min)


def build_route_plan(data, reps, options, instance="", on_progress=_noop,
                     should_cancel=_never_cancel):
    """Plan every included rep's window from loaded data. NO network."""
    dates = options.dates()
    plan = RoutePlan(instance, options, dates)
    plan.warnings.extend(data.warnings)
    if not dates:
        plan.warnings.append("No working days in the chosen window.")
        return plan
    included = [r for r in reps if r.include]
    for r in included:
        if not r.start:
            plan.warnings.append(f"{r.name}: no start address - not planned.")
    routable = [r for r in included if r.start]

    stops = _due_stops(data, routable, options, dates, plan)
    routed = set()
    for i, rep in enumerate(routable, 1):
        if should_cancel():
            plan.cancelled = True
            break
        mine = [s for s in stops if s.user_id == rep.user_id]
        _route_rep(rep, mine, options, dates, plan, on_progress, tz=data.tz)
        routed.add(rep.user_id)
        on_progress({"phase": "plan", "index": i, "total": len(routable),
                     "name": rep.name, "status": "", "message": ""})
    if plan.cancelled:
        # Their stops are still at the initial "planned" with no day; leaving
        # them so would count them as planned and print them in the itinerary.
        for s in stops:
            if s.user_id not in routed and s.status == STATUS_RT_PLANNED:
                s.mark(STATUS_RT_NOT_ROUTED,
                       "planning was cancelled before this rep was routed",
                       "Planning was cancelled")
        plan.warnings.append(
            f"Planning was cancelled after {len(routed)} of {len(routable)} "
            f"rep(s) - this plan is incomplete.")
    plan.stops.extend(stops)

    # A customer two reps both visit on the same day is worth knowing about.
    seen = {}
    for s in plan.planned():
        seen.setdefault((s.customer_id, s.day), []).append(s)
    for group in seen.values():
        reps_here = {s.rep_name for s in group}
        if len(reps_here) > 1:
            for s in group:
                others = ", ".join(sorted(reps_here - {s.rep_name}))
                s.notes = _append_note(s.notes, f"also planned for {others} "
                                                f"the same day")
    plan.stops.sort(key=lambda s: (str(s.rep_name), s.day or date.max,
                                   s.sequence or 0, str(s.name)))
    plan.days.sort(key=lambda d: (str(d.rep_name), d.date))
    return plan


def write_report(report_rows, path, fieldnames=ROUTE_REPORT_FIELDNAMES):
    """Write report rows to a CSV (defaults to the route columns)."""
    return reports.write_report(report_rows, path, fieldnames)
