"""Offline property tests for routing: distances, tour building, the day
split, and date assignment. There is no live oracle for "the best route", so
these pin the properties that make it trustworthy instead - 2-opt never makes a
tour longer, the split is optimal (checked against brute force), every day
respects its limits, and the same input always gives the same answer."""

import itertools
import random

from skynamo_geo.routing import (
    assign_to_dates, cheapest_insertion, day_minutes, haversine_km, matrices,
    nearest_neighbour, path_cost, split_tour, two_opt,
)

# --- haversine against known distances ----------------------------------
assert abs(haversine_km((0, 0), (0, 1)) - 111.19) < 0.05      # 1 deg at equator
cape_town, joburg = (-33.9249, 18.4241), (-26.2041, 28.0473)
assert abs(haversine_km(cape_town, joburg) - 1263) < 15
assert haversine_km(joburg, joburg) == 0.0
assert abs(haversine_km(cape_town, joburg) - haversine_km(joburg, cape_town)) < 1e-9

# --- matrices: symmetric, zero diagonal, minimum leg ----------------------
pts = [(-26.10, 28.05), (-26.10, 28.05), (-26.20, 28.10)]
km, mins = matrices(pts, road_factor=1.3, speed_kmh=50, min_leg_minutes=3)
assert all(km[i][i] == 0 and mins[i][i] == 0 for i in range(3))
assert km[0][2] == km[2][0] and mins[0][2] == mins[2][0]
assert mins[0][1] == 3, "same building is not zero minutes apart"
assert abs(mins[0][2] - km[0][2] * 1.3 / 50 * 60) < 1e-9


def random_case(rng, n):
    """A start, an end and n stops scattered over ~60 km."""
    pts = [(-26.0 + rng.uniform(-0.3, 0.3), 28.0 + rng.uniform(-0.3, 0.3))
           for _ in range(n + 2)]
    _km, m = matrices(pts, 1.3, 50, 0)
    return m, 0, 1, list(range(2, n + 2))


# --- tours: 2-opt never lengthens, always a permutation, deterministic ----
rng = random.Random(20261006)
for case in range(200):
    m, start, end, nodes = random_case(rng, rng.randint(2, 18))
    nn = nearest_neighbour(m, start, nodes)
    assert sorted(nn) == nodes
    improved = two_opt(nn, m, start, end)
    assert sorted(improved) == nodes, "2-opt must return a permutation"
    assert path_cost(improved, m, start, end) <= path_cost(nn, m, start, end) + 1e-9, \
        f"case {case}: 2-opt lengthened the tour"
    # the same nodes in any arrival order give the same tour
    shuffled = nodes[:]
    rng.shuffle(shuffled)
    assert nearest_neighbour(m, start, shuffled) == nn

# identical coordinates terminate and tie-break on the supplied keys
same = [(-26.0, 28.0)] * 6
_k, m = matrices(same, 1.3, 50, 0)
order = nearest_neighbour(m, 0, [5, 4, 3, 2], keys={2: "d", 3: "c", 4: "b", 5: "a"})
assert order == [5, 4, 3, 2], order
assert sorted(two_opt(order, m, 0, 1)) == [2, 3, 4, 5]

# --- the day split ---------------------------------------------------------
def brute_split(order, m, start, end, service, max_min, max_stops, n_days,
                balance):
    """Every way of cutting a fixed order into <= n_days pieces."""
    n = len(order)
    best = None
    for cuts in itertools.product([0, 1], repeat=n - 1):
        segs, cur = [], [order[0]]
        for node, cut in zip(order[1:], cuts):
            if cut:
                segs.append(cur)
                cur = [node]
            else:
                cur.append(node)
        segs.append(cur)
        if len(segs) > n_days:
            continue
        total = 0.0
        ok = True
        for s in segs:
            t = day_minutes(s, m, start, end, service)
            if t > max_min + 1e-9 or len(s) > max_stops:
                ok = False
                break
            total += t + balance * t * t / max_min
        if ok and (best is None or total < best):
            best = total
    return best


def split_total(segs, m, start, end, service, max_min, balance):
    return sum(day_minutes(s, m, start, end, service)
               + balance * day_minutes(s, m, start, end, service) ** 2 / max_min
               for s in segs)


rng = random.Random(7)
checked = 0
for case in range(150):
    n = rng.randint(1, 8)
    m, start, end, nodes = random_case(rng, n)
    order = two_opt(nearest_neighbour(m, start, nodes), m, start, end)
    service = rng.choice([15, 30, 45])
    max_min = rng.choice([180, 300, 480])
    max_stops = rng.choice([3, 5, 8])
    n_days = rng.randint(1, 5)
    balance = rng.choice([0.0, 0.5])
    got = split_tour(order, m, start, end, service, max_min, max_stops, n_days,
                     balance)
    want = brute_split(order, m, start, end, service, max_min, max_stops,
                       n_days, balance)
    if want is None:
        assert got is None, f"case {case}: split found a plan brute force could not"
        continue
    assert got is not None, f"case {case}: split missed a feasible plan"
    # same nodes, same order, nothing lost or duplicated
    assert [x for s in got for x in s] == order
    assert len(got) <= n_days and all(got)
    for s in got:
        assert len(s) <= max_stops
        assert day_minutes(s, m, start, end, service) <= max_min + 1e-9
    assert abs(split_total(got, m, start, end, service, max_min, balance)
               - want) < 1e-6, f"case {case}: split is not optimal"
    checked += 1
assert checked > 50, "too few feasible cases to mean anything"

assert split_tour([], m, 0, 1, 30, 480, 10, 5) == []

# A stop that alone is too far for a day makes the split infeasible.
far = [(-26.0, 28.0), (-26.0, 28.0), (-33.9, 18.4)]   # 1,200 km away
_k, m = matrices(far, 1.3, 50, 0)
assert split_tour([2], m, 0, 1, 30, 480, 10, 5) is None

# Balance spreads work: with it on, the longest day is no longer than without.
rng = random.Random(3)
m, start, end, nodes = random_case(rng, 12)
order = two_opt(nearest_neighbour(m, start, nodes), m, start, end)
plain = split_tour(order, m, start, end, 30, 600, 12, 4, balance=0.0)
even = split_tour(order, m, start, end, 30, 600, 12, 4, balance=1.0)
longest = lambda segs: max(day_minutes(s, m, start, end, 30) for s in segs)
assert longest(even) <= longest(plain) + 1e-6

# --- insertion and date assignment ------------------------------------------
line = [(0.0, 0.0), (0.0, 0.2), (0.0, 0.1), (0.0, 0.05)]
_k, m = matrices(line, 1.0, 60, 0)
pos, added = cheapest_insertion([2], 3, m, 0, 1)
assert pos == 0 and added < 1e-6, "a point on the way costs nothing"

days = ["mon", "tue", "wed"]
segs = ["A", "B"]
pref = {("A", "wed"): 0, ("B", "mon"): 0}
picked = assign_to_dates(segs, days, lambda s, d: pref.get((s, d), 10))
assert picked == ["wed", "mon"], picked
assert assign_to_dates([], days, lambda s, d: 0) == []

print("All routing tests passed")
