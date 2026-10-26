"""Route outputs that need no API: navigation links and a printable map.

The HTML is one self-contained file - inline SVG, inline CSS, no scripts and
no external requests - the same rule as dashboard.py, so it opens offline,
prints, and can be emailed. There is deliberately no background map: map
tiles are an external request (and a licensing question). The SVG draws the
route over the stops' true relative positions, which is what a rep needs to
see the shape of their day.

Google Maps directions links are free to generate (no key, no billing) but
take at most nine waypoints each, so a long day becomes several links that
share their endpoints. They hold the CUSTOMERS only: the first link has no
origin, so Google starts from wherever the phone is, and none ends at the
rep's start point - which is often their home, and a link is the part of this
page most likely to be forwarded. Waze links take a single destination, so
they are per stop. A stop whose pin is approximate navigates by its ADDRESS, not its
coordinates - otherwise the rep is driven to a suburb centroid.

This module must not import route_engine (the engine imports it for its CSV
rows); it reads plan objects by attribute.
"""

import html
import math
import os
from datetime import datetime
from urllib.parse import quote, urlencode

GMAPS_DIR_URL = "https://www.google.com/maps/dir/"
GMAPS_SEARCH_URL = "https://www.google.com/maps/search/"
GMAPS_MAX_WAYPOINTS = 9
WAZE_URL = "https://waze.com/ul"

DAY_COLOURS = ["#2f6fd0", "#d9822b", "#2e9e6a", "#b5459b", "#c0392b",
               "#16a5a5", "#8e6c3a"]
_INK, _MUTED, _GRID = "#1f2933", "#6b7280", "#e5e7eb"


def _esc(value):
    return html.escape("" if value is None else str(value), quote=True)


# ---------------------------------------------------------------------------
# Navigation links
# ---------------------------------------------------------------------------

def _where(lat, lng, address=None, approximate=False):
    """What to navigate to: the address for an approximate pin, else lat,lng."""
    if approximate and address:
        return address
    return f"{lat:.6f},{lng:.6f}"


def stop_link(lat, lng, address=None, approximate=False):
    """A Google Maps link to one place."""
    return GMAPS_SEARCH_URL + "?" + urlencode(
        {"api": 1, "query": _where(lat, lng, address, approximate)})


def waze_link(lat, lng, address=None, approximate=False):
    """A Waze link that starts navigation to one place."""
    if approximate and address:
        params = {"q": address, "navigate": "yes"}
    else:
        params = {"ll": f"{lat:.6f},{lng:.6f}", "navigate": "yes"}
    return WAZE_URL + "?" + urlencode(params)


def gmaps_links(stops, max_waypoints=GMAPS_MAX_WAYPOINTS):
    """Google Maps directions links for a day's stops, chunked.

    stops: the day's customers in order, as (lat, lng, address, approximate)
    tuples - never the rep's start or end point. The first link has no origin
    (Google uses the phone's location); each holds up to `max_waypoints`
    stops and a destination, and consecutive links share an endpoint, so
    following them one after another covers the day exactly once.
    """
    places = [None] + [_where(*p) for p in stops]   # None: "from here"
    if len(places) < 2:
        return []
    span = max_waypoints + 1
    links = []
    i = 0
    while i < len(places) - 1:
        chunk = places[i:i + span + 1]
        params = {"api": 1, "destination": chunk[-1], "travelmode": "driving"}
        if chunk[0] is not None:
            params["origin"] = chunk[0]
        if len(chunk) > 2:
            params["waypoints"] = "|".join(chunk[1:-1])
        links.append(GMAPS_DIR_URL + "?" + urlencode(params, quote_via=quote))
        i += span
    return links


# ---------------------------------------------------------------------------
# SVG
# ---------------------------------------------------------------------------

def _projector(points, width, height, pad):
    """Map (lat, lng) to SVG x/y: equirectangular, aspect kept, north up."""
    lats = [p[0] for p in points]
    lngs = [p[1] for p in points]
    mid = math.radians((min(lats) + max(lats)) / 2)
    scale_x = math.cos(mid) or 1e-6
    span_x = max((max(lngs) - min(lngs)) * scale_x, 1e-6)
    span_y = max(max(lats) - min(lats), 1e-6)
    k = min((width - 2 * pad) / span_x, (height - 2 * pad) / span_y)
    off_x = (width - span_x * k) / 2
    off_y = (height - span_y * k) / 2

    def project(lat, lng):
        x = off_x + (lng - min(lngs)) * scale_x * k
        y = off_y + (max(lats) - lat) * k
        return round(x, 1), round(y, 1)
    return project


def route_svg(days, width=640, height=360, numbered=True):
    """One SVG with each day's path in its own colour.

    days: list of (colour, start(lat,lng), stops, end(lat,lng)); stops are
    objects with .lat, .lng, .sequence, .approximate and .name.
    """
    points = []
    for _c, start, stops, end in days:
        points += [start, end] + [(s.lat, s.lng) for s in stops]
    if not points:
        return ""
    proj = _projector(points, width, height, pad=24)
    out = [f'<svg class="map" viewBox="0 0 {width} {height}" role="img" '
           f'width="100%" aria-label="Route map">',
           f'<rect x="0" y="0" width="{width}" height="{height}" '
           f'fill="#fbfbfc" stroke="{_GRID}"/>']
    for colour, start, stops, end in days:
        path = [proj(*start)] + [proj(s.lat, s.lng) for s in stops] + [proj(*end)]
        pts = " ".join(f"{x},{y}" for x, y in path)
        out.append(f'<polyline points="{pts}" fill="none" stroke="{colour}" '
                   f'stroke-width="2" stroke-linejoin="round" opacity="0.85"/>')
        for s in stops:
            x, y = proj(s.lat, s.lng)
            if s.approximate:
                out.append(f'<circle cx="{x}" cy="{y}" r="13" fill="none" '
                           f'stroke="{colour}" stroke-dasharray="3 3"/>')
            out.append(f'<circle cx="{x}" cy="{y}" r="9" fill="{colour}">'
                       f'<title>{_esc(s.name)}</title></circle>')
            if numbered:
                out.append(f'<text x="{x}" y="{y + 3.5}" text-anchor="middle" '
                           f'font-size="10" fill="#fff">{s.sequence}</text>')
    # start/end drawn last so they sit on top; labelled, never addressed
    seen = set()
    for _c, start, _stops, end in days:
        for point, label in ((start, "Start"), (end, "End")):
            x, y = proj(*point)
            if (x, y, label) in seen or (label == "End" and point == start):
                continue
            seen.add((x, y, label))
            out.append(f'<rect x="{x - 6}" y="{y - 6}" width="12" height="12" '
                       f'fill="{_INK}"/>'
                       f'<text x="{x + 9}" y="{y + 4}" font-size="11" '
                       f'fill="{_INK}">{label}</text>')
    out.append("</svg>")
    return "".join(out)


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

_CSS = """
body{font-family:Segoe UI,Arial,sans-serif;color:%(ink)s;margin:24px;max-width:960px}
h1{font-size:22px;margin:0 0 4px} h2{font-size:18px;margin:28px 0 6px}
h3{font-size:15px;margin:18px 0 6px} .muted{color:%(muted)s}
.note{color:%(muted)s;font-size:12px} .warn{color:#9a5b00}
table{border-collapse:collapse;width:100%%;font-size:12px;margin:6px 0}
th,td{border-bottom:1px solid %(grid)s;padding:4px 6px;text-align:left;vertical-align:top}
th{color:%(muted)s;font-weight:600} .num{text-align:right}
.kpis{display:flex;gap:12px;flex-wrap:wrap;margin:12px 0}
.kpi{border:1px solid %(grid)s;border-radius:8px;padding:8px 12px;min-width:110px}
.kpi b{display:block;font-size:20px} .day{page-break-inside:avoid}
.links a{margin-right:10px} svg.map{max-width:100%%;height:auto}
@media print{.rep{page-break-before:always} a{color:%(ink)s}}
""" % {"ink": _INK, "muted": _MUTED, "grid": _GRID}


def _clock(dt):
    return dt.strftime("%H:%M") if dt else ""


def _kpi(label, value):
    return f'<div class="kpi"><b>{_esc(value)}</b>{_esc(label)}</div>'


def _day_section(day, colour):
    links = gmaps_links([(s.lat, s.lng, s.address, s.approximate)
                         for s in day.stops])
    rows = []
    for s in day.stops:
        nav = (f'<a href="{_esc(stop_link(s.lat, s.lng, s.address, s.approximate))}">map</a> '
               f'<a href="{_esc(waze_link(s.lat, s.lng, s.address, s.approximate))}">waze</a>')
        rows.append(
            f"<tr><td class=num>{s.sequence}</td><td>{_clock(s.arrive)}</td>"
            f"<td>{_clock(s.depart)}</td><td>{_esc(s.name)}"
            f"<div class=note>{_esc(s.code)}</div></td>"
            f"<td class=num>{s.leg_km:.1f}</td><td>{_esc(s.notes)}</td>"
            f"<td>{nav}</td></tr>")
    link_html = " ".join(
        f'<a href="{_esc(u)}">Google Maps{" part " + str(i + 1) if len(links) > 1 else ""}</a>'
        for i, u in enumerate(links))
    return (
        f'<div class="day"><h3 style="color:{colour}">{_esc(day.date.strftime("%A %d %B"))}'
        f' &middot; {len(day.stops)} stop(s)</h3>'
        f'<p class="note">Leave {_clock(day.leave)}, back about '
        f'{_clock(day.finish)} &middot; {day.km:.0f} km driving '
        f'(straight-line estimate) &middot; {day.drive_minutes:.0f} min driving, '
        f'{day.service_minutes:.0f} min on site</p>'
        + route_svg([(colour, day.start, day.stops, day.end)], height=260)
        + f'<p class="links">{link_html}</p>'
        '<table><tr><th>#</th><th>Arrive</th><th>Leave</th><th>Customer</th>'
        '<th class=num>km</th><th>Notes</th><th>Navigate</th></tr>'
        + "".join(rows) + "</table></div>")


def build_route_html(plan, out_path, title="Route plan"):
    """Write the plan as one self-contained HTML file. Returns the path."""
    planned = [s for s in plan.stops if s.day is not None]
    unplanned = [s for s in plan.stops if s.day is None]
    total_km = sum(d.km for d in plan.days)
    reps = sorted({d.user_id: d.rep_name for d in plan.days}.items(),
                  key=lambda kv: (kv[1] or "", kv[0]))
    window = (f"{plan.dates[0]:%a %d %b} - {plan.dates[-1]:%a %d %b %Y}"
              if plan.dates else "")
    parts = [
        f"<h1>{_esc(title)}</h1>",
        f'<p class="muted">{_esc(window)} &middot; {_esc(plan.instance)} '
        f'&middot; generated {datetime.now():%Y-%m-%d %H:%M}</p>',
        '<p class="note">Distances and drive times are straight-line '
        "estimates scaled for roads - good for ordering stops, not a "
        "substitute for live traffic.</p>",
        '<div class="kpis">',
        _kpi("reps", len(reps)), _kpi("day routes", len(plan.days)),
        _kpi("visits planned", len(planned)),
        _kpi("not planned", len(unplanned)),
        _kpi("km (est.)", f"{total_km:,.0f}"),
        "</div>",
    ]
    for warning in plan.warnings:
        parts.append(f'<p class="note warn">{_esc(warning)}</p>')

    for user_id, rep_name in reps:
        days = sorted((d for d in plan.days if d.user_id == user_id),
                      key=lambda d: d.date)
        coloured = [(DAY_COLOURS[i % len(DAY_COLOURS)], d)
                    for i, d in enumerate(days)]
        parts.append(f'<div class="rep"><h2>{_esc(rep_name)}</h2>')
        legend = " ".join(
            f'<span style="color:{c}">&#9632; {_esc(d.date.strftime("%a"))}</span>'
            for c, d in coloured)
        parts.append(f'<p class="note">{legend}</p>')
        parts.append(route_svg([(c, d.start, d.stops, d.end) for c, d in coloured],
                               numbered=False))
        for colour, day in coloured:
            parts.append(_day_section(day, colour))
        parts.append("</div>")

    if unplanned:
        rows = "".join(
            f"<tr><td>{_esc(s.rep_name)}</td><td>{_esc(s.name)}"
            f"<div class=note>{_esc(s.code)}</div></td>"
            f"<td>{_esc(s.status)}</td><td>{_esc(s.notes)}</td></tr>"
            for s in sorted(unplanned, key=lambda s: (s.rep_name or "", s.status,
                                                     s.name or "")))
        parts.append('<div class="rep"><h2>Not planned</h2>'
                     '<table><tr><th>Rep</th><th>Customer</th><th>Status</th>'
                     f'<th>Why</th></tr>{rows}</table></div>')

    doc = ("<!DOCTYPE html><html><head><meta charset='utf-8'>"
           f"<title>{_esc(title)}</title><style>{_CSS}</style></head><body>"
           + "".join(parts) + "</body></html>")
    folder = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(folder, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
    return out_path
