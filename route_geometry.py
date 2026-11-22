"""Shared route geometry: climbs, pad takeoffs and memoised kit detours.
"""
from __future__ import annotations

import math

from fleetkit import baseline
from fleetkit.baseline import SPEED_FACTOR
from fleetkit.geometry import hdist, segment_enters_cylinder
from config import CONFIG


_cyl = {}


def clear_leg(scenario, a, b):
    """True if the straight leg a-b enters no zone or protected area (R6)."""
    hit = _cyl.get(id(scenario))
    if hit is None or hit[0] is not scenario:
        hit = _cyl[id(scenario)] = (scenario, baseline._cylinders(scenario))
    return not any(segment_enters_cylinder(a, b, c, r, top) for c, r, top in hit[1])


def climb_path(scenario, path, distance, end=False):
    """path (horizontal at z, from over the pad when not end, to over the pad when end)
    with a waypoint `distance` along its first (last) leg, where the climb (descent) ends;
    None when the leg is shorter or the climbing piece enters a zone or protected area."""
    a, b = (path[-1], path[-2]) if end else (path[0], path[1])
    seg = hdist(a, b)
    if seg < distance + 1e-6:
        return None
    f = distance / seg
    mid = (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2])
    ground = (a[0], a[1], 0.0)
    if not (clear_leg(scenario, mid, ground) if end else clear_leg(scenario, ground, mid)):
        return None
    return path[:-1] + [mid, path[-1]] if end else [path[0], mid] + path[1:]


def takeoff_need(scenario, a, b, level=False):
    """Seconds two takeoffs from these pads must be apart, or None if far enough.
    level: both leave the pad climbing along a cruise leg, so the later one trails the
    earlier at cruise speed instead of rising beneath it at climb speed."""
    d_sep = scenario.rules.d_sep_m + 2.0
    gap = hdist(a.home, b.home)
    if gap >= d_sep:
        return None
    if level:
        rate = SPEED_FACTOR * min(a.v_h_mps, b.v_h_mps)
    else:
        rate = SPEED_FACTOR * min(a.v_up_mps, b.v_up_mps)
    return math.sqrt(max(0.0, d_sep * d_sep - gap * gap)) / rate + 1.0


def _diagonal(route):
    return len(route) > 1 and (route[1]["x"], route[1]["y"]) != (route[0]["x"], route[0]["y"])


_neighbours = {}


def pad_neighbours(scenario, aircraft, aid):
    """{other id: (gap needed vertically, gap needed level)} for pads closer than d_sep."""
    hit = _neighbours.get(id(scenario))
    if hit is None or hit[0] is not scenario:
        table = {}
        for a in aircraft.values():
            table[a.id] = {b.id: (takeoff_need(scenario, a, b), takeoff_need(scenario, a, b, True))
                           for b in aircraft.values()
                           if b.id != a.id and takeoff_need(scenario, a, b) is not None}
        hit = _neighbours[id(scenario)] = (scenario, table)
    return hit[1].get(aid, {})


def pad_conflict(scenario, aircraft, routes, extra):
    """True if a new route takes off too close in time to a neighbouring pad's takeoff."""
    for aid, route in extra.items():
        level = _diagonal(route)
        for oid, needs in pad_neighbours(scenario, aircraft, aid).items():
            other = extra[oid] if oid in extra else routes.get(oid)
            if not other:
                continue
            need = needs[1] if level and _diagonal(other) else needs[0]
            if abs(route[0]["t"] - other[0]["t"]) < need:
                return True
    return False


def takeoff_shift(scenario, aircraft, routes, scout, depart, limit=40.0, step=2.0):
    """Smallest delay so the scout's climb stays clear of takeoffs on neighbouring pads.
    Landings and later legs are left to the validator."""
    others = []
    for aid, needs in pad_neighbours(scenario, aircraft, scout.id).items():
        route = routes.get(aid)
        if not route:
            continue
        need = needs[1] if CONFIG.diagonal_climb and _diagonal(route) else needs[0]
        others.append((route[0]["t"], need))
    shift = 0.0
    while shift <= limit:
        if all(abs(depart + shift - t0) >= need for t0, need in others):
            return shift
        shift += step
    return 0.0


_official_detour = baseline._detour
_detours = {}


def _cached_detour(points, scenario):
    """Memoised baseline._detour: route builders repeat the same legs many times."""
    key = (id(scenario), tuple(points))
    if key not in _detours:
        if len(_detours) > 200000:
            _detours.clear()
        path = _official_detour(points, scenario)
        _detours[key] = (scenario, None if path is None else tuple(path))
    path = _detours[key][1]
    return None if path is None else list(path)


# The kit's route builders look _detour up in their module; results are unchanged.
baseline._detour = _cached_detour
