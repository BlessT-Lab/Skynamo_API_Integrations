"""When is a customer due a visit? Pure date logic - no I/O.

Skynamo's visit frequency reads "f visits every c periods" (frequency=1,
cycle=2, period=week is "once every 2 weeks"). That is a COUNT-IN-A-WINDOW
rule, not a fixed interval: with W = c periods, a customer is due on day d when
fewer than f visits fall in the window (d - W, d]. "Twice a week" therefore
means "fewer than two visits in the last seven days", not "every 3.5 days" -
the latter double-books a customer seen on Monday and Tuesday last week.

With the visit dates sorted newest first (v1 >= v2 >= ...), that reduces to

    next_due = v_f + W        (or "due now" with fewer than f on record)

Months and years use calendar arithmetic - "once a month" since 31 January is
due on 28/29 February, not 30.44 days later.

The window is ROLLING, not calendar-bucketed (each calendar month). That is a
decided assumption, recorded in CLAUDE.md: it needs no anchor date, and it is
how a person reads "once every two weeks". If Skynamo's own compliance reports
are found to bucket by calendar month, this is the one place to change.
"""

import calendar
import math
import re
from datetime import date, datetime, timedelta, timezone

PERIODS = ("week", "month", "year")
# Hard stop on how many visits one pair can need inside one plan, so a
# misconfigured "50 per week" record cannot flood a rep's week.
MAX_OCCURRENCES = 7


class FrequencyError(ValueError):
    """A visit-frequency record that cannot be planned from."""


def add_months(d, months):
    """`d` moved by `months` calendar months, clamping the day to the month.

    31 Jan + 1 month = 28 Feb (29 in a leap year); 29 Feb - 12 months = 28 Feb.
    """
    total = d.year * 12 + (d.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def parse_frequency(record):
    """(frequency, cycle, period) from a /visitfrequencies record, validated.

    Raises FrequencyError for anything that cannot be planned from - a zero
    count or cycle, or a period other than week/month/year - so the caller can
    report it against the record instead of planning nonsense.
    """
    try:
        frequency = int(record.get("frequency"))
        cycle = int(record.get("cycle"))
    except (TypeError, ValueError):
        raise FrequencyError("frequency and cycle must be whole numbers")
    period = str(record.get("period") or "").strip().lower()
    if period.endswith("s"):
        period = period[:-1]
    if frequency < 1 or cycle < 1:
        raise FrequencyError(f"{frequency} per {cycle} {period} is not a schedule")
    if period not in PERIODS:
        raise FrequencyError(f"unknown period {record.get('period')!r}")
    return frequency, cycle, period


def shift(d, cycle, period, sign=1):
    """`d` moved by one window (cycle periods), forwards or backwards."""
    if period == "week":
        return d + timedelta(days=7 * cycle * sign)
    months = cycle * (12 if period == "year" else 1)
    return add_months(d, months * sign)


def window_days(cycle, period):
    """Approximate length of one window in days, for spacing and priority."""
    if period == "week":
        return 7 * cycle
    return round(cycle * (365.25 if period == "year" else 30.44))


def min_gap_days(frequency, cycle, period):
    """Fewest days between two visits to one customer inside one window.

    Half the even spacing (window / frequency), at least a day - so "twice a
    week" is never Monday and Tuesday. The one definition: the planner places
    repeat visits by it, and occurrences() spaces them by it.
    """
    return max(1, math.ceil(window_days(cycle, period) / (2 * frequency)))


def label(frequency, cycle, period):
    """'once every 2 weeks', 'twice a month', '3 times every 6 months'."""
    times = {1: "once", 2: "twice"}.get(frequency, f"{frequency} times")
    if cycle == 1:
        return f"{times} a {period}"
    return f"{times} every {cycle} {period}s"


def next_due(visits_desc, frequency, cycle, period):
    """The date the next visit is due, or None when it is due now.

    visits_desc: visit dates, newest first. Fewer than `frequency` visits on
    record means the window is already short - due now.
    """
    if len(visits_desc) < frequency:
        return None
    return shift(visits_desc[frequency - 1], cycle, period)


def occurrences(visits_desc, frequency, cycle, period, window_start, window_end):
    """Visits a pair needs inside [window_start, window_end].

    Returns a list of (earliest, due, overdue_days) - one per required visit,
    each assumed to happen on its due day when working out the next. A visit
    due before the window is due on its first day and carries how many days
    overdue it already is. Occurrences after the first are spaced at least
    half a window / frequency apart, so "twice a week" is never Monday and
    Tuesday.
    """
    history = sorted(visits_desc, reverse=True)
    gap = min_gap_days(frequency, cycle, period)
    out = []
    previous = None
    while len(out) < MAX_OCCURRENCES:
        due = next_due(history, frequency, cycle, period)
        if due is None:
            overdue = None          # never visited enough to say
            due = window_start
        else:
            overdue = (window_start - due).days if due < window_start else 0
            due = max(due, window_start)
        if due > window_end:
            break
        earliest = window_start if previous is None else max(
            window_start, previous + timedelta(days=gap))
        due = max(due, earliest)
        if due > window_end:
            break
        out.append((earliest, due, overdue))
        history = sorted(history + [due], reverse=True)
        previous = due
    return out


def working_days(start, count, weekdays=(0, 1, 2, 3, 4), holidays=()):
    """The next `count` dates from `start` that are worked.

    weekdays: Monday=0 .. Sunday=6. holidays: dates to skip. Returns fewer
    than `count` only when no weekday is worked at all.
    """
    weekdays = set(weekdays)
    holidays = set(holidays)
    if not weekdays or count <= 0:
        return []
    out, d = [], start
    while len(out) < count:
        if d.weekday() in weekdays and d not in holidays:
            out.append(d)
        d += timedelta(days=1)
    return out


def parse_holidays(text):
    """Dates from free text: '2026-12-25, 2026-12-26'. Returns (dates, bad)."""
    dates, bad = [], []
    for part in re.split(r"[,;\s]+", text or ""):
        part = part.strip()
        if not part:
            continue
        try:
            dates.append(date.fromisoformat(part))
        except ValueError:
            bad.append(part)
    return dates, bad


# ---------------------------------------------------------------------------
# Time zones and the API's datetime strings
# ---------------------------------------------------------------------------

_OFFSET_RE = re.compile(r"([+-])?\s*(\d{1,2})(?::?(\d{2}))?")


def parse_utc_offset(text):
    """A fixed timezone from Configuration.time_zone.utc_offset, or None.

    The format is undocumented, so this accepts the shapes such a field
    commonly takes: '+02:00', '-05:30', '02:00:00', '+0200', 'UTC+2',
    '(UTC+02:00) Harare', 'GMT-3'. A bare number with no sign is positive.
    """
    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None
    upper = raw.upper()
    if upper in ("UTC", "GMT", "Z"):
        return timezone.utc
    # Prefer the part after UTC/GMT when present, e.g. "(UTC+02:00) Harare".
    marker = re.search(r"(?:UTC|GMT)\s*([+-].*)", upper)
    candidate = marker.group(1) if marker else raw
    match = _OFFSET_RE.search(candidate)
    if not match:
        return None
    sign, hours, minutes = match.groups()
    hours, minutes = int(hours), int(minutes or 0)
    if hours > 14 or minutes > 59:
        return None
    delta = timedelta(hours=hours, minutes=minutes)
    return timezone(-delta if sign == "-" else delta)


def localize(day, clock, tz):
    """An aware datetime for a wall-clock time on a day.

    tz None means "this PC's time zone", resolved through the operating
    system's own rules so daylight saving is right on each date - deliberately
    not zoneinfo, which needs the tzdata package on Windows and may not be in
    the frozen exe.
    """
    naive = datetime.combine(day, clock)
    if tz is None:
        return naive.astimezone()
    return naive.replace(tzinfo=tz)


def parse_api_datetime(text):
    """A datetime from an API string, or None.

    Tolerates 'Z', explicit offsets, no offset, and the 7-digit fractional
    seconds .NET produces (datetime.fromisoformat takes at most 6).
    """
    if not text:
        return None
    s = str(text).strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        try:
            return datetime.fromisoformat(s[:10])
        except ValueError:
            return None


def format_api_datetime(dt, style="offset"):
    """An API datetime string for an aware datetime.

    style 'offset' -> 2026-10-07T09:00:00+02:00
          'naive'  -> 2026-10-07T09:00:00 (wall clock, no zone)
          'utc'    -> 2026-10-07T07:00:00Z
    Which one the server reads correctly is settled by diag_route_planning.py
    and recorded as SCHEDULED_VISIT_DATETIME_STYLE.
    """
    dt = dt.replace(microsecond=0)
    if style == "naive":
        return dt.replace(tzinfo=None).isoformat()
    if style == "utc":
        return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat() + "Z"
    return dt.isoformat()
