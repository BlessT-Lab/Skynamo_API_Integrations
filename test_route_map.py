"""Offline tests for route outputs: navigation links and the HTML page.

The page must stay self-contained (no scripts, stylesheets, images or other
external requests - only plain links out to Google Maps and Waze), must escape
everything that came from the instance, and must not print a rep's start
address - for a rep starting from home, that is their home.
"""

import os
import re
import shutil
import tempfile
from datetime import date, datetime, time, timedelta
from urllib.parse import parse_qs, urlparse

from skynamo_geo.route_engine import RouteDay, RouteOptions, RoutePlan, RouteStop
from skynamo_geo.route_map import (
    GMAPS_MAX_WAYPOINTS, build_route_html, gmaps_links, route_svg, stop_link,
    waze_link,
)

# --- links ---------------------------------------------------------------
path = [(-26.0 + i * 0.01, 28.0, f"{i} Main Rd", False) for i in range(25)]
links = gmaps_links(path)                      # 25 stops
assert len(links) == 3, len(links)
parts = [parse_qs(urlparse(u).query) for u in links]
for p in parts:
    assert len(p.get("waypoints", [""])[0].split("|")) <= GMAPS_MAX_WAYPOINTS
    assert p["travelmode"] == ["driving"] and p["api"] == ["1"]
# the first link starts from wherever the phone is: no origin at all, so a
# rep's start point (often their home) is never written into a link
assert "origin" not in parts[0], parts[0]
# consecutive links share an endpoint, and together cover the day once
for a, b in zip(parts, parts[1:]):
    assert a["destination"] == b["origin"]
assert parts[0]["waypoints"][0].startswith("-26.000000")
assert parts[-1]["destination"][0].startswith("-25.760000")
covered = []
for i, p in enumerate(parts):
    covered += ([p["origin"][0]] if i else []) + (
        p["waypoints"][0].split("|") if "waypoints" in p else [])
covered.append(parts[-1]["destination"][0])
assert covered == [f"{lat:.6f},{lng:.6f}" for lat, lng, _a, _x in path], \
    "every stop visited exactly once, in order"
assert gmaps_links([]) == []
one = parse_qs(urlparse(gmaps_links(path[:1])[0]).query)
assert "origin" not in one and "waypoints" not in one   # straight to it

# an approximate pin navigates by its address, an exact one by coordinates
assert "q=12+Main+Rd" in waze_link(-26.1, 28.0, "12 Main Rd", approximate=True)
assert "ll=-26.100000%2C28.000000" in waze_link(-26.1, 28.0, "12 Main Rd")
assert "query=12+Main+Rd" in stop_link(-26.1, 28.0, "12 Main Rd", True)
assert "query=-26.100000%2C28.000000" in stop_link(-26.1, 28.0, None, True)

# --- a small plan -----------------------------------------------------------
monday = date(2026, 10, 12)
opts = RouteOptions(monday, n_days=2)
plan = RoutePlan("acme", opts, [monday, monday + timedelta(days=1)])
HOME = (-26.123457, 28.987654)                 # a rep's start: their home
day = RouteDay(11, "Anna <script>alert(1)</script>", monday, HOME, HOME)
day.leave = datetime.combine(monday, time(8, 0))
for i in range(3):
    s = RouteStop(11, day.rep_name, {
        "id": i, "code": f"C{i}", "name": f"Shop {i} <img src=x onerror=1>",
        "location": {"latitude": -26.1 + i * 0.02, "longitude": 28.0,
                     "accuracy": 25, "is_approximate": i == 2}})
    s.day, s.sequence = monday, i + 1
    s.arrive = day.leave + timedelta(minutes=20 + 45 * i)
    s.depart = s.arrive + timedelta(minutes=30)
    s.leg_km = 3.0
    s.notes = "pin is approximate" if i == 2 else ""
    day.stops.append(s)
day.km, day.drive_minutes, day.service_minutes = 12.0, 40, 90
day.finish = day.stops[-1].depart + timedelta(minutes=15)
plan.days.append(day)
plan.stops.extend(day.stops)
gone = RouteStop(11, day.rep_name, {"id": 9, "code": "C9", "name": "Far away",
                                    "location": {}})
gone.mark("out-of-range", "more than 300 km from the start")
plan.stops.append(gone)
plan.warnings.append("Anna: the start point is 140 km from the nearest customer.")

tmp = tempfile.mkdtemp(prefix="route_map_")
try:
    out = build_route_html(plan, os.path.join(tmp, "nested", "plan.html"),
                           title="Week of 12 Oct")
    html_out = open(out, encoding="utf-8").read()

    # self-contained: nothing is fetched when the page opens
    for banned in ("<script", "<link", "<img", "url(", "@import", "<iframe"):
        assert banned not in html_out.lower(), banned
    hrefs = re.findall(r'href="([^"]+)"', html_out)
    assert hrefs, "itinerary should carry navigation links"
    assert all(h.startswith(("https://www.google.com/maps/", "https://waze.com/"))
               for h in hrefs), hrefs
    # inside a real tag only - the escaped canary below reads "src=" as text
    srcs = re.findall(r'<[a-zA-Z][^>]*\ssrc\s*=', html_out)
    assert not srcs, f"no element may load a source: {srcs}"

    # instance text is escaped, never live markup
    assert "&lt;script&gt;" in html_out and "&lt;img" in html_out

    # the rep's start is labelled, not addressed - and its coordinates are
    # in no link (nor anywhere else on the page), in any precision
    assert ">Start<" in html_out
    for text in ("26.12345", "28.98765", "26.1235", "28.9877"):
        assert text not in html_out, f"start coordinates leaked: {text}"
    # ...and the approximate pin is drawn with a dashed ring
    assert "stroke-dasharray" in html_out

    # what is not planned is listed with its reason
    assert "Not planned" in html_out and "more than 300 km" in html_out
    assert "140 km" in html_out
    assert "straight-line" in html_out, "estimates are labelled as estimates"
    assert html_out.count('class="day"') == 1
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# an empty plan renders without raising
empty = RoutePlan("acme", opts, [monday])
tmp = tempfile.mkdtemp(prefix="route_map_")
try:
    assert "Route plan" in open(build_route_html(
        empty, os.path.join(tmp, "e.html")), encoding="utf-8").read()
finally:
    shutil.rmtree(tmp, ignore_errors=True)
assert route_svg([]) == ""

print("All route map tests passed")
