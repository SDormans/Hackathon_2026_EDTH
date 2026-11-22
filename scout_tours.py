"""Scout tours: observation points, tour building, reachability bounds, trip labels
and scout-insertion moves.
"""
from __future__ import annotations

from dataclasses import replace
import heapq
import itertools
import math
import time

from fleetkit import baseline
from fleetkit.baseline import SCOUT_LAYERS, SPEED_FACTOR
from fleetkit.energy import best_range_speed
from fleetkit.geometry import EPS, hdist
from fleetkit.model import HOSTILE, SCOUT, UNKNOWN
from config import CONFIG
from deadline import DeadlineReached
from route_geometry import _cached_detour, clear_leg, climb_path, takeoff_shift


# The kit's scout layers (70, 110 m) plus a low one. At 30 m the 3D observation range allows
# observing from ~139 m instead of ~123 m, and the climb is shorter, so early windows are
# reached sooner. Official check_handin, L1-L4 seeds 35-39: 9913.7 -> 10089.6 (+1.8%),
# L4 +3.0%; with seeds 30-34 and the two validation L4s, L4 won 10 of 12 cases.
SCOUT_LAYERS = tuple(SCOUT_LAYERS) + (30.0,)

_tours = {}
# "fast": every scout leg at SCOUT_CRUISE * v_h. "eco": the kit's speed. "hybrid": the kit's
# speed, falling back to fast when the eco tour misses a window, the horizon or the battery.
# Set per run by Search(scout_speed=...); planner.py --scout-speed.
SCOUT_SPEEDS = ("fast", "eco", "hybrid")


def eco_speed(scout):
    return min(SPEED_FACTOR * scout.v_h_mps, best_range_speed(scout.power))


def fast_speed(scout):
    return max(eco_speed(scout), CONFIG.scout_cruise * scout.v_h_mps)


def obs_offset(scenario, contact, z):
    """Horizontal observation offset toward home for a scout at altitude z."""
    if not CONFIG.obs_stretch:
        return baseline.OBS_OFFSET_M
    reach = scenario.rules.R_obs_m - CONFIG.obs_margin_m
    dz = z - contact.pos[2]
    if reach <= abs(dz):
        return baseline.OBS_OFFSET_M
    return max(baseline.OBS_OFFSET_M, math.sqrt(reach * reach - dz * dz))


def obs_point(scenario, scout, contact, angle, z):
    ux, uy = baseline._toward_home(contact, scout.home, angle)
    off = obs_offset(scenario, contact, z)
    return contact.pos[0] + ux * off, contact.pos[1] + uy * off


def build_scout_route(scout, tasks, depart, z, scenario, speed, diagonal=None):
    """The kit's _scout_route with an explicit cruise speed (same waypoints at the kit speed
    without DIAGONAL_CLIMB). Diagonal: the first leg climbs and the last descends."""
    if diagonal is None:
        diagonal = CONFIG.diagonal_climb and bool(tasks)
    rules_ = scenario.rules
    detour, length, wp = baseline._detour, baseline._length, baseline._wp
    home = scout.home
    above = (home[0], home[1], z)
    t = depart
    wps = [wp(t, home)]
    if not diagonal:
        t += z / (SPEED_FACTOR * scout.v_up_mps) + 0.5
        wps.append(wp(t, above))
    cur = above
    for n, task in enumerate(tasks):
        px, py = task["point"]
        cpos = task.get("cpos")
        if cpos is not None:
            # A point stretched for 70 m may be out of range when the tour flies at 110 m.
            reach = rules_.R_obs_m - CONFIG.obs_margin_m
            h = math.sqrt(max(0.0, reach * reach - (z - cpos[2]) ** 2))
            d = math.hypot(px - cpos[0], py - cpos[1])
            if d > h:
                px, py = cpos[0] + (px - cpos[0]) * h / d, cpos[1] + (py - cpos[1]) * h / d
        target = (px, py, z)
        path = detour([cur, target], scenario)
        if path is None:
            return None
        total = length(path)
        if diagonal and n == 0:
            path = climb_path(scenario, path, speed * (z / (SPEED_FACTOR * scout.v_up_mps) + 0.5))
            if path is None:
                return build_scout_route(scout, tasks, depart, z, scenario, speed, False)
        lo, hi = task["window"]
        arrive = max(t + total / speed, lo)
        if arrive + rules_.obs_dwell_s > hi:
            return None
        for k in range(1, len(path) - 1):
            wps.append(wp(t + (arrive - t) * length(path[:k + 1]) / total, path[k]))
        wps.append(wp(arrive, target, action="observe", target_id=task["cid"],
                      dwell_s=rules_.obs_dwell_s))
        t = arrive + rules_.obs_dwell_s
        cur = target
    path = detour([cur, above], scenario)
    if path is None:
        return None
    total = length(path)
    arrive = t + total / speed
    if diagonal:
        path = climb_path(scenario, path, speed * (z / (SPEED_FACTOR * scout.v_down_mps) + 0.5),
                          end=True)
        if path is None:
            return build_scout_route(scout, tasks, depart, z, scenario, speed, False)
    for k in range(1, len(path) - 1):
        wps.append(wp(t + (arrive - t) * length(path[:k + 1]) / total, path[k]))
    if diagonal:
        wps.append(wp(arrive, home))
    else:
        wps.append(wp(arrive, above))
        wps.append(wp(arrive + z / (SPEED_FACTOR * scout.v_down_mps) + 0.5, home))
    for w in wps:
        if "target_id" in w:
            w["target"] = w.pop("target_id")
    return wps


def raw_energy_wh(route, scout):
    """route_energy_wh on raw waypoint dicts (same formula as fleetkit.energy)."""
    p = scout.power
    total = 0.0
    for a, b in zip(route, route[1:]):
        dep = a["t"] + a.get("dwell_s", 0.0)
        d = b["t"] - dep
        if d > 0:
            v = math.hypot(b["x"] - a["x"], b["y"] - a["y"]) / d
            total += (p.p0_w + p.k_v * v * v) * d / 3600.0 + p.e_climb_wh_per_m * max(0.0, b["z"] - a["z"])
        total += p.p0_w * b.get("dwell_s", 0.0) / 3600.0
    return total


def _depart(scout, tasks, z, speed, shift):
    climb = 0.0 if CONFIG.diagonal_climb else z / scout.v_up_mps
    travel = hdist(scout.home, tasks[0]["point"]) / speed + climb + 3.0
    return max(0.0, tasks[0]["window"][0] - travel) + shift


def _build_tour(scenario, scout, tasks, z, shift, depart):
    if CONFIG.scout_speed_mode == "eco":
        speeds = (eco_speed(scout),)
    elif CONFIG.scout_speed_mode == "fast":
        speeds = (fast_speed(scout),)
    else:
        speeds = (eco_speed(scout), fast_speed(scout))
    route = None
    for speed in speeds:
        start = _depart(scout, tasks, z, speed, shift) if depart is None else depart
        route = build_scout_route(scout, tasks, start, z, scenario, speed)
        if route is not None and len(speeds) > 1 and speed != speeds[-1]:
            if (route[-1]["t"] > scenario.horizon_s + EPS
                    or raw_energy_wh(route, scout) > scout.usable_wh + EPS):
                continue
        if route is not None:
            return route
    return route


def scout_tour(scenario, scout, tasks, z, shift=0.0, depart=None):
    """Scout tour for an ordered task list; same departure rule as the baseline.

    Memoised: sibling branches rebuild identical tours many times. Routes are never edited
    after construction, so the waypoint dicts are shared; the list itself is a copy.
    """
    key = (id(scenario), scout.id, z, shift, depart, CONFIG.scout_speed_mode, CONFIG.obs_stretch, CONFIG.diagonal_climb,
           tuple((t["cid"], tuple(t["point"]), tuple(t["window"])) for t in tasks))
    hit = _tours.get(key)
    if hit is None or hit[0] is not scenario:
        if len(_tours) > 100000:
            _tours.clear()
        hit = _tours[key] = (scenario, _build_tour(scenario, scout, tasks, z, shift, depart))
    return None if hit[1] is None else list(hit[1])


def scout_reachable(scenario, scout, tasks, pos, z, prev_route=None):
    """False only when the tour must fail: the scout tour builder misses the window of
    tasks[pos], or the scout cannot be home before the horizon (R1).

    Lower bounds only: the builder flies at most `speed` along paths no shorter than straight
    lines, departs at t >= 0, and cannot observe before a window opens. prev_route, the
    parent's route for the same earlier tasks, tightens the bound on the previous visit.
    """
    rules_ = scenario.rules
    speed = eco_speed(scout) if CONFIG.scout_speed_mode == "eco" else fast_speed(scout)
    # Diagonal: the climb and descent overlap the cruise legs (lower bound 0).
    climb = 0.0 if CONFIG.diagonal_climb else z / (SPEED_FACTOR * scout.v_up_mps) + 0.5
    descend = 0.0 if CONFIG.diagonal_climb else z / (SPEED_FACTOR * scout.v_down_mps) + 0.5
    task = tasks[pos]
    lo, hi = task["window"]
    # Stretched observation points may be pulled in by up to ~35 m at 110 m altitude.
    slack = 2 * 35.0 / speed if CONFIG.obs_stretch else 0.0
    arrive = climb + hdist(scout.home, task["point"]) / speed - slack
    if pos > 0:
        prev = tasks[pos - 1]
        prev_done = prev["window"][0] + rules_.obs_dwell_s
        if prev_route is not None:
            for w in prev_route:
                if w.get("action") == "observe" and w.get("target") == prev["cid"]:
                    prev_done = max(prev_done, w["t"] + w.get("dwell_s", 0.0) - CONFIG.reach_slack_s)
                    break
        arrive = max(arrive, prev_done + hdist(prev["point"], task["point"]) / speed - slack)
    finish = max(arrive, lo) + rules_.obs_dwell_s
    if finish > hi + EPS:
        return False
    # Every remaining path back home is at least the straight line.
    return (finish + hdist(task["point"], scout.home) / speed - slack + descend
            <= scenario.horizon_s + EPS)


def scout_wh_per_m(scout):
    """Cruise energy per metre (Wh) at the speed scout tours fly."""
    v = eco_speed(scout) if CONFIG.scout_speed_mode == "eco" else fast_speed(scout)
    return (scout.power.p0_w + scout.power.k_v * v * v) / v / 3600.0


def tour_estimate(scenario, scout, legs, z):
    """Straight-leg replay of scout_tour: (finish times, landing time, Wh), or None as
    soon as a visit cannot finish inside its window.

    `legs` = [(point, window, reach)] in visit order; `reach` shortens the legs into and
    out of a point that is not chosen yet (it lies within `reach` of its contact). Same
    departure rule, climb, dwell and cruise speed as scout_tour at layer z (hybrid: the
    fast speed), without detours, so every time is a lower bound on the real tour.
    """
    rules_, power = scenario.rules, scout.power
    speed = eco_speed(scout) if CONFIG.scout_speed_mode == "eco" else fast_speed(scout)
    # Diagonal: the climb and descent overlap the cruise legs (lower bound: no extra time).
    climb = 0.0 if CONFIG.diagonal_climb else z / (SPEED_FACTOR * scout.v_up_mps) + 0.5
    home = scout.home[:2]
    first = max(0.0, hdist(home, legs[0][0]) - legs[0][2])
    lead = 0.0 if CONFIG.diagonal_climb else z / scout.v_up_mps
    t = max(0.0, legs[0][1][0] - (first / speed + lead + 3.0)) + climb
    wh = power.p0_w * climb / 3600.0 + power.e_climb_wh_per_m * z
    here, slack, finishes = home, 0.0, []
    for point, (lo, hi), reach in legs:
        d = max(0.0, hdist(here, point) - reach - slack)
        arrive = max(t + d / speed, lo)
        if arrive + rules_.obs_dwell_s > hi + EPS:
            return None
        if arrive > t:
            v = d / (arrive - t)
            wh += (power.p0_w + power.k_v * v * v) * (arrive - t) / 3600.0
        t = arrive + rules_.obs_dwell_s
        wh += power.p0_w * rules_.obs_dwell_s / 3600.0
        finishes.append(t)
        here, slack = point, reach
    back = max(0.0, hdist(here, home) - slack) / speed
    descent = 0.0 if CONFIG.diagonal_climb else z / (SPEED_FACTOR * scout.v_down_mps) + 0.5
    wh += ((power.p0_w + power.k_v * speed * speed) * back + power.p0_w * descent) / 3600.0
    return finishes, t + back + descent, wh


def tour_fits(scenario, scout, current, contact, order, z, deadlines):
    """False only when the tour visiting `order` (contact ids) at layer z must fail:
    a window or a strike's confirmation deadline (R4) missed, landing after the horizon
    (R1), or the battery exceeded (R3, 1% margin for the estimate)."""
    by_id = {t["cid"]: t for t in current}
    legs = []
    for cid in order:
        if cid == contact.id:
            legs.append((contact.pos[:2], tuple(contact.window), obs_offset(scenario, contact, z)))
        else:
            t = by_id[cid]
            lo, hi = t["window"]
            legs.append((t["point"], (lo, min(hi, deadlines.get(cid, math.inf))), 0.0))
    est = tour_estimate(scenario, scout, legs, z)
    return (est is not None and est[1] <= scenario.horizon_s + EPS
            and est[2] <= 1.01 * scout.usable_wh)


_legs = {}


def leg_length(scenario, a, b, z, exact=True):
    """Horizontal length of the scout leg a-b at layer z as the tour builder flies it
    (straight, or the kit's detour around zones); inf when no detour exists.
    exact=False: the straight length (a lower bound) without computing detours."""
    if not exact:
        return hdist(a, b)
    key = (id(scenario), a[0], a[1], b[0], b[1], z)
    hit = _legs.get(key)
    if hit is None or hit[0] is not scenario:
        p, q = (a[0], a[1], z), (b[0], b[1], z)
        if clear_leg(scenario, p, q):
            d = hdist(p, q)
        else:
            path = _cached_detour([p, q], scenario)
            d = math.inf if path is None else baseline._length(path)
        if len(_legs) > 200000:
            _legs.clear()
        hit = _legs[key] = (scenario, d)
    return hit[1]


def trip_labels(scenario, scout, cands, z, keep=None, top=30, orders=(), cover=False,
                skip=0, rows=None, exact=True):
    """Best single-sortie trips for `scout` by resource-constrained label setting.

    cands: [(cid, point, (lo, hi), net value, latest finish)]. A label is a chain of visits
    ending at one candidate: (finish time, Wh so far, net value, visited mask). Legs are
    straight, at the scout's cruise speed, with tour_estimate's departure rule, waiting and
    energy model (lower bounds of scout_tour). Labels are dropped when dominated at the same
    contact (earlier or equal finish, less Wh, more value, subset of visits) or when the
    contact already keeps `keep` better ones. `orders`: extra visit orders (contact ids) to
    evaluate the same way, e.g. the current tour.

    Returns up to `top` trips [(value, order, Wh, finishes)] with distinct contact sets,
    best first; value = sum of net values - lambda_e * scout energy (home to home).
    cover: also the best trip found that includes each candidate (beyond `top`), so
    trip selection across scouts can trade contacts. skip: bit mask of candidates to
    leave out. rows: a dict reused across calls with the same cands (leg lengths).
    exact=False: straight legs, no detours (faster; realisation checks the real tour).
    """
    keep = CONFIG.trip_labels if keep is None else keep
    rules_, p = scenario.rules, scout.power
    speed = eco_speed(scout) if CONFIG.scout_speed_mode == "eco" else fast_speed(scout)
    home, dwell, lam = scout.home[:2], rules_.obs_dwell_s, rules_.lambda_e
    cap, horizon = CONFIG.trip_battery * scout.usable_wh, scenario.horizon_s
    cruise_w = p.p0_w + p.k_v * speed * speed
    descent = 0.0 if CONFIG.diagonal_climb else z / (SPEED_FACTOR * scout.v_down_mps) + 0.5
    lead = 0.0 if CONFIG.diagonal_climb else z / scout.v_up_mps
    climb = 0.0 if CONFIG.diagonal_climb else z / (SPEED_FACTOR * scout.v_up_mps) + 0.5
    start_wh = p.e_climb_wh_per_m * z + p.p0_w * climb / 3600.0
    n = len(cands)
    if rows is not None and "back" in rows:
        back_d = rows["back"]
    else:
        back_d = [leg_length(scenario, c[1], home, z, exact) for c in cands]
    back_t = [d / speed + descent for d in back_d]
    back_wh = [(cruise_w * d / speed + p.p0_w * descent) / 3600.0 for d in back_d]
    dwell_wh = p.p0_w * dwell / 3600.0
    index = {c[0]: j for j, c in enumerate(cands)}

    def leg(t, wh, d, j):
        _, _, (lo, _), _, latest = cands[j]
        arrive = max(t + d / speed, lo)
        finish = arrive + dwell
        if finish > latest + EPS or finish + back_t[j] > horizon + EPS:
            return None
        dt = arrive - t
        if dt > 0:
            v = d / dt
            wh += (p.p0_w + p.k_v * v * v) * dt / 3600.0
        wh += dwell_wh
        if wh + back_wh[j] > cap:
            return None
        return finish, wh

    def first(j):
        d = rows["out"][j]
        if d == math.inf:
            return None
        lo = cands[j][2][0]
        # _depart times the straight line; a detour only makes the arrival later.
        depart = max(0.0, lo - (hdist(home, cands[j][1]) / speed + lead + 3.0)) + climb
        return leg(depart, start_wh, d, j)

    rows = {} if rows is None else rows

    def dist(j):
        row = rows.get(j)
        if row is None:
            row = rows[j] = [leg_length(scenario, cands[j][1], b[1], z, exact) for b in cands]
        return row
    labels = []          # [finish, wh, net, mask, node, pred, alive]
    buckets = [[] for _ in range(n)]
    heap = []

    def push(finish, wh, net, mask, j, pred):
        value = net - lam * wh
        bucket = buckets[j]
        for o in bucket:
            if (o[0] <= finish + EPS and o[1] <= wh + 1e-9 and o[2] - lam * o[1] >= value - 1e-9
                    and o[3] & ~mask == 0):
                return
        kept = []
        for o in bucket:
            if (finish <= o[0] + EPS and wh <= o[1] + 1e-9 and value >= o[2] - lam * o[1] - 1e-9
                    and mask & ~o[3] == 0):
                o[6] = False
            else:
                kept.append(o)
        if len(kept) >= keep:
            worst = min(kept, key=lambda o: o[2] - lam * o[1])
            if worst[2] - lam * worst[1] >= value:
                buckets[j] = kept
                return
            worst[6] = False
            kept.remove(worst)
        label = [finish, wh, net, mask, j, pred, True]
        kept.append(label)
        buckets[j] = kept
        labels.append(label)
        heapq.heappush(heap, (finish, len(labels) - 1))

    if "back" not in rows:
        rows["back"] = [leg_length(scenario, c[1], home, z, exact) for c in cands]
        rows["out"] = [leg_length(scenario, home, c[1], z, exact) for c in cands]
    for j in range(n):
        if skip >> j & 1:
            continue
        got = first(j)
        if got is not None:
            push(got[0], got[1], cands[j][3], 1 << j, j, None)
    while heap:
        _, i = heapq.heappop(heap)
        label = labels[i]
        if not label[6]:
            continue
        t, wh, net, mask, j = label[:5]
        row = dist(j)
        for k in range(n):
            if (mask | skip) >> k & 1 or row[k] == math.inf:
                continue
            got = leg(t, wh, row[k], k)
            if got is not None:
                push(got[0], got[1], net + cands[k][3], mask | 1 << k, k, label)

    def chain(label):
        out = []
        while label is not None:
            out.append(label)
            label = label[5]
        return out[::-1]

    trips = {}
    covering = {}       # candidate index -> (value, mask) of the best trip including it
    for label in labels:
        value = label[2] - lam * (label[1] + back_wh[label[4]])
        best = trips.get(label[3])
        if best is None or value > best[0]:
            path = chain(label)
            trips[label[3]] = (value, tuple(cands[o[4]][0] for o in path), label[1] + back_wh[label[4]],
                               tuple(o[0] for o in path))
        if cover:
            mask, j = label[3], 0
            while mask:
                if mask & 1 and (j not in covering or value > covering[j][0]):
                    covering[j] = (value, label[3])
                mask >>= 1
                j += 1
    for order in orders:
        if not order or any(cid not in index for cid in order):
            continue
        js = [index[cid] for cid in order]
        got, net, finishes = first(js[0]), cands[js[0]][3], []
        for a, b in zip([None] + js, js):
            if a is not None and got is not None:
                got = None if dist(a)[b] == math.inf else leg(got[0], got[1], dist(a)[b], b)
                net += cands[b][3]
            if got is None:
                break
            finishes.append(got[0])
        if got is None:
            continue
        mask = sum(1 << j for j in js)
        value = net - lam * (got[1] + back_wh[js[-1]])
        if mask not in trips or value > trips[mask][0]:
            trips[mask] = (value, tuple(order), got[1] + back_wh[js[-1]], tuple(finishes))
    out = sorted(trips.values(), key=lambda tr: (-tr[0], tr[1]))[:top]
    if cover:
        have = {frozenset(tr[1]) for tr in out}
        for _, mask in sorted(covering.values(), key=lambda vm: -vm[0]):
            tr = trips[mask]
            if frozenset(tr[1]) not in have:
                have.add(frozenset(tr[1]))
                out.append(tr)
    return out


def pack_trips(trips, stop=None):
    """Weighted set packing: at most one trip per scout, no contact in two trips, maximal
    total value. trips = [(value, scout id, contact ids, payload)] with value > 0.

    Greedy by value and by value per contact, then swaps until none helps: a trip outside
    the packing replaces every chosen trip it conflicts with, the scouts left without a
    trip are refilled greedily from their own trips, and the swap is kept when the total
    grows. Returns (value, chosen trips).
    """
    trips = [tr for tr in trips if tr[0] > 0]
    trips.sort(key=lambda tr: (-tr[0], tr[1], tuple(sorted(tr[2]))))
    by_scout = {}
    for tr in trips:
        by_scout.setdefault(tr[1], []).append(tr)

    def fill(chosen):
        scouts = {tr[1] for tr in chosen}
        taken = set()
        for tr in chosen:
            taken |= tr[2]
        free = sorted((tr for sid, own in by_scout.items() if sid not in scouts for tr in own),
                      key=lambda tr: -tr[0])
        for tr in free:
            if tr[1] not in scouts and not (taken & tr[2]):
                chosen.append(tr)
                scouts.add(tr[1])
                taken |= tr[2]
        return chosen

    def total(chosen):
        return sum(tr[0] for tr in chosen)

    best = []
    for key in (None, lambda tr: -tr[0] / max(1, len(tr[2]))):
        order = trips if key is None else sorted(trips, key=key)
        chosen, scouts, taken = [], set(), set()
        for tr in order:
            if tr[1] not in scouts and not (taken & tr[2]):
                chosen.append(tr)
                scouts.add(tr[1])
                taken |= tr[2]
        improved = True
        while improved:
            improved = False
            current = total(chosen)
            for tr in trips:
                if stop is not None and time.monotonic() >= stop:
                    break
                if tr in chosen:
                    continue
                keep = [c for c in chosen if c[1] != tr[1] and not (c[2] & tr[2])]
                if total(keep) + tr[0] + sum(by_scout[c[1]][0][0] for c in chosen
                                             if c not in keep) <= current + 1e-9:
                    continue  # even each freed scout's best trip could not pay for it
                trial = fill(keep + [tr])
                value = total(trial)
                if value > current + 1e-9:
                    chosen, current, improved = trial, value, True
        if total(chosen) > total(best):
            best = chosen
    return total(best), best


def insertion_positions(home, tasks, pos, limit):
    """Tour positions ordered by added horizontal distance (cheapest insertion first)."""
    points = [home[:2]] + [t["point"] for t in tasks] + [home[:2]]
    cost = [(hdist(points[i], pos) + hdist(pos, points[i+1]) - hdist(points[i], points[i+1]), i)
            for i in range(len(tasks) + 1)]
    return [i for _, i in sorted(cost)[:limit]]


class ScoutTripMixin:
    """Search methods; mixed into search_planner.Search."""

    # --- Scout trip label search -------------------------------------------------------

    def trip_candidates(self, scout, used, exclude, z, deadlines=None):
        """Label-search candidates for one scout: contacts with a net value above zero
        (unknown: points; hostile: points minus its cheapest free team), their observation
        point at layer z, window and latest observation finish (hostiles: early enough to
        confirm and strike, and before any existing strike's deadline)."""
        rules_ = self.sc.rules
        deadlines = deadlines or {}
        out = []
        for c in self.sc.contacts:
            if (c.cls not in (HOSTILE, UNKNOWN) or c.window is None or c.id in exclude
                    or c.id in self.matrix.banned):
                continue
            lo, hi = c.window
            latest = hi
            if c.cls == HOSTILE:
                groups = self.matrix.groups(c, used, 1)
                if not groups:
                    continue
                team = groups[0]
                net = c.points - sum(self.matrix.costs[a.id, c.id] for a in team)
                spacing = min(5.0, rules_.sync_window_s / max(1, len(team) - 1)) * (len(team) - 1)
                ready = max(lo, max(self.effector_earliest(a, c) for a in team))
                if ready + spacing > hi + EPS:
                    continue
                latest = hi - spacing - rules_.confirm_margin_s - 2.0 - CONFIG.trip_strike_slack
            else:
                net = c.points
            latest = min(latest, deadlines.get(c.id, math.inf))
            if net <= 0 or latest < lo + rules_.obs_dwell_s:
                continue
            out.append((c.id, obs_point(self.sc, scout, c, 0.0, z), (lo, hi), net, latest))
        return out

    def scout_trips(self, state, scout, used, exclude, z, top=30):
        """trip_labels for one scout against `state`, with its current tour as an extra
        order so the result is never worse than what the scout already flies (by the same
        estimate)."""
        cands = self.trip_candidates(scout, used, exclude, z)
        current = tuple(t["cid"] for t in state.tasks.get(scout.id, ()))
        return trip_labels(self.sc, scout, cands, z, top=top, orders=(current,))

    def trip_scout(self, state, rng):
        """A scout for the trip move, favouring high battery use and unserved positive-net
        contacts near its tour or base."""
        unserved = [c for c in self.sc.contacts if c.cls in (HOSTILE, UNKNOWN)
                    and c.window is not None and c.id not in state.served
                    and c.id not in self.matrix.banned and self.matrix.priority(c) > 0]
        scouts, weights = [], []
        for sid in sorted(state.tasks):
            scout = self.aircraft[sid]
            frac = state.energies.get(sid, 0.0) / max(1e-9, scout.usable_wh)
            points = [scout.home[:2]] + [t["point"] for t in state.tasks[sid]]
            near = sum(1 for c in unserved if min(hdist(q, c.pos) for q in points) < 1500.0)
            scouts.append(scout)
            weights.append(0.3 + frac + 0.2 * min(5, near))
        return rng.choices(scouts, weights=weights)[0] if scouts else None

    def strip_scout(self, state, sid):
        """State without scout sid's tour and the strikes its observations confirm."""
        return self.strip_scouts(state, (sid,))

    def strip_scouts(self, state, sids):
        """State without the tours of scouts `sids` and the strikes their observations
        confirm. Returns (state or None, removed contact ids)."""
        cids = {t["cid"] for sid in sids for t in state.tasks.get(sid, ())}
        team = {aid for aid, route in state.parsed.items()
                if self.aircraft[aid].type != SCOUT
                and any(w.action == "engage" and w.target in cids for w in route.waypoints)}
        partial = self.checker.extend(self.checker.without(state, team | set(sids)), {})
        if partial is None:
            return None, cids
        partial.tasks = {**state.tasks, **{sid: [] for sid in sids}}
        return partial, cids

    def realise_trip(self, state, scout, order, z, stop):
        """Append the trip's visits one at a time (see realise_visits). Returns (state,
        realised contact ids)."""
        return self.realise_visits(state, [(scout, cid, z) for cid in order], stop)

    def realise_visits(self, state, visits, stop, on_visit=None):
        """Append visits [(scout, contact id, preferred layer)] in order through _serve
        (strikes included), retrying layers, observation angles and takeoff shifts; visits
        that cannot be realised are skipped. A scout keeps the layer its tour already flies.
        on_visit(previous, new, scout, cid) is called after each realised visit.
        Returns (state, realised contact ids); stops quietly at `stop`."""
        done = []
        try:
            for scout, cid, z in visits:
                self.checker.check_time(stop)
                child = self.realise_visit(state, scout, cid, z, stop)
                if child is not None:
                    if on_visit is not None:
                        child = on_visit(state, child, scout, cid)
                    state = child
                    done.append(cid)
        except DeadlineReached:
            self.checker.check_time()
        return state, done

    def realise_visit(self, state, scout, cid, z, stop):
        """Best child appending contact cid to the scout's tour, or None."""
        contact = self.contacts[cid]
        if cid in state.served:
            return None
        groups = self.matrix.groups(contact, state.used) if contact.cls == HOSTILE else [()]
        if not groups:
            return None
        route = state.routes.get(scout.id)
        layer = max(w["z"] for w in route) if route else z
        tasks = list(state.tasks.get(scout.id, ()))
        layers = [layer] + [l for l in SCOUT_LAYERS if l != layer]
        speed = eco_speed(scout) if CONFIG.scout_speed_mode != "fast" else fast_speed(scout)

        def options():
            for zz, angle, sh in itertools.product(layers, (0.0, 60.0, -60.0), (0.0, 7.0)):
                task = {"cid": cid, "point": obs_point(self.sc, scout, contact, angle, zz),
                        "window": contact.window, "cpos": contact.pos}
                tt = tasks + [task]
                if CONFIG.pad_takeoffs:
                    sh += takeoff_shift(self.sc, self.aircraft, state.routes, scout,
                                        _depart(scout, tt, zz, speed, 0.0))
                yield scout, tt, scout_tour(self.sc, scout, tt, zz, sh)

        # The first visits of a trip may cost more scout energy than they earn; the trip
        # is judged as a whole by the caller.
        base = replace(state, score=-math.inf)
        progress = {"stage": -1, "outcome": "no_options"}
        best = None
        for child, _ in self._serve(base, contact, options(), groups, stop, 2,
                                    self.stats(cid), progress, {"count": 0, "seen": set()}):
            if best is None or child.score > best.score:
                best = child
        return best

    def trip_plan(self, stop):
        """Warm start from a packing of label-search trips of all scouts.

        Candidate trips per scout and layer: the best ones, the best one including each
        contact (cover) and the best one without each contact of its best trip; then
        TRIP_PLAN_ROUNDS rounds of re-planning every scout without the contacts the other
        chosen trips hold, re-packing after each. Every realised visit is recorded; the
        result is then greedily repaired."""
        start = time.monotonic()
        unique = {}
        scouts = [a for a in self.sc.aircraft if a.type == SCOUT]
        layers = (SCOUT_LAYERS[0], min(SCOUT_LAYERS))
        cands = {(a.id, z): self.trip_candidates(a, frozenset(), set(), z)
                 for a in scouts for z in layers}

        rows = {key: {} for key in cands}

        def add(scout, z, exclude=(), top=CONFIG.trip_plan_trips, cover=True):
            self.checker.check_time(stop)
            pool = cands[scout.id, z]
            skip = sum(1 << j for j, c in enumerate(pool) if c[0] in exclude)
            found = trip_labels(self.sc, scout, pool, z, keep=CONFIG.trip_plan_labels, top=top,
                                cover=cover, skip=skip, rows=rows[scout.id, z],
                                exact=CONFIG.trip_plan_exact)
            for value, order, wh, finishes in found:
                key = (scout.id, frozenset(order))
                if key not in unique or value > unique[key][0]:
                    unique[key] = (value, scout.id, frozenset(order), (z, order, finishes))
            return found

        for scout in scouts:
            for z in layers:
                found = add(scout, z)
                if found:
                    # The best trip without each of its most valuable contacts.
                    net = {c[0]: c[3] for c in cands[scout.id, z]}
                    for cid in sorted(found[0][1], key=lambda c: -net[c])[:CONFIG.trip_plan_drops]:
                        add(scout, z, {cid}, top=3, cover=False)
        pack_stop = lambda: min(stop, time.monotonic() + 0.3)
        value, chosen = pack_trips(list(unique.values()), pack_stop())
        rounds = 0
        for _ in range(CONFIG.trip_plan_rounds):
            for scout in scouts:
                held = {cid for tr in chosen if tr[1] != scout.id for cid in tr[2]}
                for z in layers:
                    add(scout, z, held, top=10)
            value, chosen = pack_trips(list(unique.values()), pack_stop())
            rounds += 1
        self.trip_stats.update(candidates=len(unique), chosen=len(chosen), rounds=rounds,
                               estimate=round(value, 3),
                               label_s=round(time.monotonic() - start, 4))
        visits = sorted(((f, self.aircraft[sid], cid, z)
                         for _, sid, _, (z, order, finishes) in chosen
                         for cid, f in zip(order, finishes)), key=lambda v: (v[0], v[2]))
        depth = [0]

        def on_visit(previous, child, scout, cid):
            depth[0] += 1
            return self.record(child, previous, {"kind": "trip_plan", "contact": cid,
                                                 "scout": scout.id}, depth[0])

        state, done = self.realise_visits(self.root, [(a, cid, z) for _, a, cid, z in visits],
                                          stop, on_visit)
        self.trip_stats.update(visits=len(visits), realised=len(done),
                               realised_score=round(state.score, 3))
        saved, self.checker.stop = self.checker.stop, min(self.checker.stop, stop)
        try:
            state = self.repair(state, depth[0])
        except DeadlineReached:
            pass
        finally:
            self.checker.stop = saved
        self.trip_stats.update(score=round(state.score, 3),
                               seconds=round(time.monotonic() - start, 3))
        return state
