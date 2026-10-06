"""Route construction - pure functions over index matrices, stdlib only.

Everything here works on integer node indices into a minutes matrix, so it
knows nothing about customers, reps or the API and can be tested exhaustively.
Callers put the rep's start and end points into the same matrix as the stops.

The approach for one rep's week:

  1. one tour through every stop, start -> end: nearest neighbour, then 2-opt
  2. split that tour into at most N consecutive days by dynamic programming
     (a Beasley split) - each day must fit its hour and stop limits

A polar "sweep" around the start point was rejected: it only works when the
start sits in the middle of the territory. A rep who starts from home off to
one side sees every customer inside a narrow angle, the sweep cuts long thin
wedges, and every day drives the full distance out and back. Splitting a tour
keeps neighbouring customers together wherever the start is.

Distances are straight-line (haversine) scaled by a road-winding factor. That
gets the ORDER close to what roads would give at no cost; absolute drive times
are estimates and are labelled as such.
"""

import itertools
import math

EARTH_RADIUS_KM = 6371.0088
_EPS = 1e-9


def haversine_km(a, b):
    """Great-circle distance in km between two (lat, lng) points."""
    lat1, lng1 = map(math.radians, a)
    lat2, lng2 = map(math.radians, b)
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2)
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def matrices(points, road_factor, speed_kmh, min_leg_minutes=0.0):
    """(km, minutes) matrices for a list of (lat, lng) points.

    Travel minutes = straight-line km x road_factor / speed, with a floor so
    two stops in the same building are not zero minutes apart. A node to
    itself is always 0.
    """
    n = len(points)
    km = [[0.0] * n for _ in range(n)]
    mins = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            d = haversine_km(points[i], points[j])
            m = max(min_leg_minutes, d * road_factor / speed_kmh * 60.0)
            km[i][j] = km[j][i] = d
            mins[i][j] = mins[j][i] = m
    return km, mins


def path_cost(order, cost, start, end):
    """Total cost of start -> order... -> end."""
    if not order:
        return cost[start][end]
    total = cost[start][order[0]] + cost[order[-1]][end]
    for a, b in zip(order, order[1:]):
        total += cost[a][b]
    return total


def nearest_neighbour(cost, start, nodes, keys=None):
    """Greedy tour over `nodes` from `start`. Deterministic.

    Ties (equal cost - stops at the same coordinates) break on keys[node],
    then on the node index, so the same input always gives the same tour
    whatever order it arrived in.
    """
    keys = keys or {}
    remaining = set(nodes)
    order, here = [], start
    while remaining:
        nxt = min(remaining,
                  key=lambda n: (cost[here][n], keys.get(n, n), n))
        order.append(nxt)
        remaining.discard(nxt)
        here = nxt
    return order


def two_opt(order, cost, start, end, max_passes=50):
    """Improve a tour by reversing segments. Never returns a longer tour.

    The endpoints are fixed (and may differ). A reversal is accepted only if
    it improves by more than a tiny epsilon, so equal-cost swaps cannot loop;
    passes are capped so it always finishes. Assumes symmetric costs, which
    haversine-derived costs are.
    """
    path = [start] + list(order) + [end]
    n = len(path)
    for _ in range(max_passes):
        improved = False
        for i in range(1, n - 2):
            for j in range(i + 1, n - 1):
                a, b = path[i - 1], path[i]
                c, d = path[j], path[j + 1]
                delta = (cost[a][c] + cost[b][d]) - (cost[a][b] + cost[c][d])
                if delta < -_EPS:
                    path[i:j + 1] = reversed(path[i:j + 1])
                    improved = True
        if not improved:
            break
    return path[1:-1]


def day_minutes(nodes, mins, start, end, service_minutes):
    """Minutes for one day: travel start -> stops -> end, plus time on site."""
    return path_cost(nodes, mins, start, end) + service_minutes * len(nodes)


def split_tour(order, mins, start, end, service_minutes, max_minutes,
               max_stops, n_days, balance=0.0):
    """Cut a tour into at most n_days consecutive day segments, optimally.

    A segment's cost is its day_minutes plus `balance * minutes^2 /
    max_minutes`, which still sums per day (so the dynamic programme stays
    exact) and stops one 9-hour day sitting next to a 2-hour one. Every
    segment must fit max_minutes and max_stops.

    Returns a list of segments (each a list of nodes, all non-empty, tour
    order preserved), or None when the stops cannot fit in n_days.
    """
    n = len(order)
    if n == 0:
        return []
    inf = float("inf")
    # cost[i][j]: segment order[i:j] (j exclusive), computed incrementally.
    seg = {}
    for i in range(n):
        inner = 0.0
        for j in range(i, n):
            if j > i:
                inner += mins[order[j - 1]][order[j]]
            count = j - i + 1
            if count > max_stops:
                break
            fixed = mins[start][order[i]] + inner + service_minutes * count
            if fixed > max_minutes + _EPS:
                break                       # only grows with j
            total = fixed + mins[order[j]][end]
            if total > max_minutes + _EPS:
                continue
            seg[(i, j + 1)] = total + balance * total * total / max_minutes

    best = [[inf] * (n + 1) for _ in range(n_days + 1)]
    back = [[None] * (n + 1) for _ in range(n_days + 1)]
    best[0][0] = 0.0
    for k in range(1, n_days + 1):
        for j in range(1, n + 1):
            for i in range(0, j):
                c = seg.get((i, j))
                if c is None or best[k - 1][i] == inf:
                    continue
                v = best[k - 1][i] + c
                if v < best[k][j] - _EPS:
                    best[k][j] = v
                    back[k][j] = i
    k_best = min(range(1, n_days + 1), key=lambda k: best[k][n])
    if best[k_best][n] == inf:
        return None
    segments, j = [], n
    for k in range(k_best, 0, -1):
        i = back[k][j]
        segments.append(order[i:j])
        j = i
    return list(reversed(segments))


def cheapest_insertion(day_nodes, node, mins, start, end):
    """(position, added_minutes) for inserting `node` into a day's order."""
    path = [start] + list(day_nodes) + [end]
    best = None
    for pos in range(len(path) - 1):
        a, b = path[pos], path[pos + 1]
        added = mins[a][node] + mins[node][b] - mins[a][b]
        if best is None or added < best[1] - _EPS:
            best = (pos, added)
    return best


def assign_to_dates(segments, dates, score, max_exhaustive=7):
    """Choose which date each segment runs on, minimising the summed score.

    score(segment, date) -> number. With up to `max_exhaustive` dates every
    ordered choice is tried (7 dates -> 5,040), which is exact. Beyond that it
    keeps tour order on consecutive dates, which is a sensible default for a
    tour that already flows around the territory.

    Returns a list of dates, one per segment.
    """
    k = len(segments)
    if k == 0:
        return []
    if k > len(dates):
        raise ValueError("more day segments than dates")
    if len(dates) > max_exhaustive:
        return list(dates[:k])
    best, best_dates = None, None
    for chosen in itertools.permutations(dates, k):
        total = sum(score(s, d) for s, d in zip(segments, chosen))
        if best is None or total < best - _EPS:
            best, best_dates = total, chosen
    return list(best_dates)
