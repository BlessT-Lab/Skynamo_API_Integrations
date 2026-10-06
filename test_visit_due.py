"""Offline tests for visit_due: when a customer is due, calendar arithmetic,
working days, and the API's undocumented time formats."""

from datetime import date, datetime, time, timedelta, timezone

from skynamo_geo.visit_due import (
    MAX_OCCURRENCES, FrequencyError, add_months, format_api_datetime, label,
    localize, next_due, occurrences, parse_api_datetime, parse_frequency,
    parse_holidays, parse_utc_offset, working_days,
)

D = date.fromisoformat

# --- calendar arithmetic -------------------------------------------------
assert add_months(D("2026-01-31"), 1) == D("2026-02-28")      # clamps
assert add_months(D("2028-01-31"), 1) == D("2028-02-29")      # leap year
assert add_months(D("2028-02-29"), -12) == D("2027-02-28")
assert add_months(D("2026-12-15"), 1) == D("2027-01-15")      # year rollover
assert add_months(D("2026-03-31"), -1) == D("2026-02-28")
assert add_months(D("2026-05-10"), 0) == D("2026-05-10")

# --- frequency records ---------------------------------------------------
assert parse_frequency({"frequency": 1, "cycle": 2, "period": "week"}) == \
    (1, 2, "week")
assert parse_frequency({"frequency": "2", "cycle": "1", "period": "Weeks"}) == \
    (2, 1, "week")                                             # tolerant
for bad in ({"frequency": 0, "cycle": 1, "period": "week"},
            {"frequency": 1, "cycle": 0, "period": "week"},
            {"frequency": 1, "cycle": 1, "period": "fortnight"},
            {"frequency": None, "cycle": 1, "period": "week"}):
    try:
        parse_frequency(bad)
        raise AssertionError(f"accepted {bad}")
    except FrequencyError:
        pass
assert label(1, 2, "week") == "once every 2 weeks"
assert label(2, 1, "month") == "twice a month"

# --- next due ------------------------------------------------------------
# once a week, seen Monday -> due the next Monday
assert next_due([D("2026-10-05")], 1, 1, "week") == D("2026-10-12")
# twice a week: due a week after the SECOND-newest visit, not 3.5 days after
assert next_due([D("2026-10-08"), D("2026-10-06")], 2, 1, "week") == \
    D("2026-10-13")
# fewer visits on record than the frequency asks for -> due now
assert next_due([D("2026-10-08")], 2, 1, "week") is None
assert next_due([], 1, 1, "month") is None
# months are calendar months
assert next_due([D("2026-01-31")], 1, 1, "month") == D("2026-02-28")
assert next_due([D("2026-03-15")], 1, 1, "year") == D("2027-03-15")

# --- occurrences in a planning window -------------------------------------
mon, fri = D("2026-10-12"), D("2026-10-16")

# Seen 10 days before the week starts, once a week -> one visit, 3 days overdue
occ = occurrences([D("2026-10-02")], 1, 1, "week", mon, fri)
assert len(occ) == 1 and occ[0][1] == mon and occ[0][2] == 3, occ

# Not due until after the window -> nothing to plan
assert occurrences([D("2026-10-11")], 1, 2, "week", mon, fri) == []

# Due mid-week -> planned on its due day, not overdue
occ = occurrences([D("2026-10-07")], 1, 1, "week", mon, fri)
assert occ == [(mon, D("2026-10-14"), 0)], occ

# Never visited, twice a week -> two visits, spaced (never Mon + Tue)
occ = occurrences([], 2, 1, "week", mon, fri)
assert len(occ) == 2, occ
assert occ[0][2] is None                         # never visited: unknown lag
assert (occ[1][1] - occ[0][1]).days >= 2, occ
assert occ[1][0] >= occ[0][1] + timedelta(days=2)

# A misconfigured record cannot flood the week
assert len(occurrences([], 50, 1, "week", mon, fri)) <= MAX_OCCURRENCES

# Every occurrence lies inside the window and is no earlier than its earliest
for history, f, c, p in (([], 3, 1, "week"), ([D("2026-09-01")], 1, 1, "month"),
                         ([D("2026-10-09"), D("2026-10-06")], 2, 1, "week")):
    for earliest, due, _over in occurrences(history, f, c, p, mon, fri):
        assert mon <= earliest <= due <= fri, (history, f, earliest, due)

# --- working days ----------------------------------------------------------
days = working_days(D("2026-10-09"), 5)          # a Friday
assert [d.isoformat() for d in days] == [
    "2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"]
days = working_days(mon, 3, holidays=[D("2026-10-13")])
assert D("2026-10-13") not in days and len(days) == 3
assert working_days(mon, 2, weekdays=(5,)) == [D("2026-10-17"), D("2026-10-24")]
assert working_days(mon, 5, weekdays=()) == []   # never loops forever

dates, bad = parse_holidays("2026-12-25, 2026-12-26; nonsense")
assert dates == [D("2026-12-25"), D("2026-12-26")] and bad == ["nonsense"]

# --- time zone strings (format undocumented) -------------------------------
two = timezone(timedelta(hours=2))
for text in ("+02:00", "02:00:00", "+0200", "UTC+2", "(UTC+02:00) Harare",
             "UTC +02:00", "2"):
    assert parse_utc_offset(text) == two, text
assert parse_utc_offset("-05:30") == timezone(-timedelta(hours=5, minutes=30))
assert parse_utc_offset("GMT-3") == timezone(-timedelta(hours=3))
assert parse_utc_offset("UTC") == timezone.utc
for junk in (None, "", "Harare", "+25:00"):
    assert parse_utc_offset(junk) is None, junk

# --- API datetime strings --------------------------------------------------
assert parse_api_datetime("2026-10-07T09:00:00") == datetime(2026, 10, 7, 9)
assert parse_api_datetime("2026-10-07T07:00:00Z") == \
    datetime(2026, 10, 7, 7, tzinfo=timezone.utc)
assert parse_api_datetime("2026-10-07T09:00:00+02:00").utcoffset() == \
    timedelta(hours=2)
# .NET's seven fractional digits
assert parse_api_datetime("2026-10-07T09:00:00.1234567+02:00").microsecond == \
    123456
assert parse_api_datetime("2026-10-07") == datetime(2026, 10, 7)
assert parse_api_datetime(None) is None and parse_api_datetime("x") is None

nine = localize(D("2026-10-07"), time(9, 0), two)
assert format_api_datetime(nine, "offset") == "2026-10-07T09:00:00+02:00"
assert format_api_datetime(nine, "naive") == "2026-10-07T09:00:00"
assert format_api_datetime(nine, "utc") == "2026-10-07T07:00:00Z"
# tz None resolves through the PC's own rules and is always aware
assert localize(D("2026-10-07"), time(9, 0), None).tzinfo is not None

print("All visit-due tests passed")
