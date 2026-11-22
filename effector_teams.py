"""Effector strike routes and the search methods that assign strike teams.
"""
from __future__ import annotations

import itertools
import math

from fleetkit import baseline
from fleetkit.baseline import _effector_route, EFFECTOR_LAYERS, SPEED_FACTOR
from fleetkit.energy import best_range_speed
from fleetkit.geometry import EPS, hdist, segment_enters_cylinder
from fleetkit.model import SCOUT
from fleetkit import validator as rules
from config import CONFIG
from route_geometry import _cached_detour, climb_path, pad_conflict, pad_neighbours


# Effector cruise layer sets, tried in order for a strike team; member i flies at
# layers[i % len(layers)]. First the whole team at the 30 m floor: team members arrive a
# few seconds apart, in trail, and less climb saves energy (e_climb x 10-100 m each). Then
# the kit's three rotations of 40/85/130 m when the low team breaks separation.
EFFECTOR_Z = ((30.0,),) + tuple(tuple(EFFECTOR_LAYERS[(i + r) % len(EFFECTOR_LAYERS)]
                                      for i in range(len(EFFECTOR_LAYERS)))
                                for r in range(len(EFFECTOR_LAYERS)))


def effector_layer_sets():
    """Layer sets tried for a strike team, in order."""
    return (((CONFIG.ground_z,),) + EFFECTOR_Z) if CONFIG.ground_strikes else EFFECTOR_Z


_effector_shapes = {}


def effector_route(eff, contact, arrive, z, angle, scenario):
    """The kit's _effector_route, climbing along the first leg (DIAGONAL_CLIMB).

    The flight path depends only on (effector, contact, layer, angle); it is built once
    and only the times are placed for each arrival."""
    if z == CONFIG.ground_z:
        if angle != 0.0:
            return None, arrive
        route = ground_route(eff, contact, arrive, scenario)
        if route is not None:
            return route
        z = 30.0
    if not CONFIG.diagonal_climb:
        return _effector_route(eff, contact, arrive, z, angle, scenario)
    key = (id(scenario), eff.id, contact.id, z, angle, CONFIG.effector_cruise)
    shape = _effector_shapes.get(key)
    if shape is None or shape[0] is not scenario:
        if len(_effector_shapes) > 200000:
            _effector_shapes.clear()
        shape = _effector_shapes[key] = (scenario, _effector_shape(eff, contact, z, angle, scenario))
    shape = shape[1]
    if shape is None:
        return None, arrive
    if shape == "kit":
        return _effector_route(eff, contact, arrive, z, angle, scenario)
    home, inner, a_pt, e_pt, t_cruise, t_final = shape
    earliest = t_cruise + t_final
    if arrive < earliest:
        return None, earliest
    t_a = arrive - t_final
    t_o = t_a - t_cruise
    wps = [baseline._wp(t_o, home)]
    for frac, point in inner:
        wps.append(baseline._wp(t_o + t_cruise * frac, point))
    wps.append(baseline._wp(t_a, a_pt))
    wps.append(baseline._wp(arrive, e_pt, action="engage", target=contact.id))
    return wps, earliest


_ground_clear = {}


def ground_route(eff, contact, arrive, scenario):
    """(route, earliest arrival) for the two-waypoint route home -> engagement at the
    contact, flown at ground level; None when the straight line is not clear (R6)."""
    key = (id(scenario), eff.id, contact.id)
    hit = _ground_clear.get(key)
    if hit is None or hit[0] is not scenario:
        home, end = eff.home, contact.pos
        cylinders = baseline._cylinders(scenario)
        clear = not any(segment_enters_cylinder(home, end, c, r + CONFIG.ground_clearance_m, top)
                        for c, r, top in cylinders)
        hit = _ground_clear[key] = (scenario, clear)
    if not hit[1]:
        return None
    earliest = hdist(eff.home, contact.pos) / (CONFIG.effector_cruise * eff.v_h_mps)
    if arrive < earliest:
        return None, earliest
    return [baseline._wp(arrive - earliest, eff.home),
            baseline._wp(arrive, contact.pos, action="engage", target=contact.id)], earliest


def _effector_shape(eff, contact, z, angle, scenario):
    """(home, [(share of cruise time, point)], approach, engage point, cruise s, final s),
    None when no path exists, or "kit" for the kit's vertical climb."""
    home = eff.home
    reach = min(baseline.APPROACH_M, 0.5 * hdist(home, contact.pos))
    ux, uy = baseline._toward_home(contact, home, angle)
    a_pt = (contact.pos[0] + ux * reach, contact.pos[1] + uy * reach, z)
    e_pt = (contact.pos[0], contact.pos[1], baseline.ENGAGE_Z)
    path = _cached_detour([(home[0], home[1], z), a_pt], scenario)
    if path is None:
        return None
    v_h = CONFIG.effector_cruise * eff.v_h_mps
    t_final = max(hdist(a_pt, e_pt) / v_h,
                  (z - baseline.ENGAGE_Z) / (SPEED_FACTOR * eff.v_down_mps)) + 0.2
    v_cruise = min(v_h, best_range_speed(eff.power))
    total = baseline._length(path)
    path = climb_path(scenario, path, v_cruise * (z / (SPEED_FACTOR * eff.v_up_mps) + 0.5))
    if path is None:
        return "kit"
    inner = [(baseline._length(path[:k + 1]) / total, path[k]) for k in range(1, len(path) - 1)]
    return home, inner, a_pt, e_pt, total / v_cruise, t_final


def strike_deadlines(scenario, aircraft, routes):
    """Latest observation finish for each struck contact (its first engagement - margin)."""
    out = {}
    for aid, route in routes.items():
        if aircraft[aid].type == SCOUT:
            continue
        for w in route:
            if w.get("action") == "engage":
                cid = w["target"]
                out[cid] = min(out.get(cid, math.inf), w["t"] - scenario.rules.confirm_margin_s)
    return out
# The warm start (kit planner) builds effector routes through the module too.
baseline._effector_route = effector_route


class StrikeMixin:
    """Search methods; mixed into search_planner.Search."""

    def strikes(self, parent, contact, groups, scout, scout_route, obs_end, stop,
                stats=None, progress=None):
        base = max(obs_end + self.sc.rules.confirm_margin_s + 2.0, contact.window[0])
        # Try cheap alternatives to the nearest team. At most one child per team
        # for each scout setting, avoiding a beam filled with timing variants.
        for team in groups:
            found = False
            spacing = min(5.0, self.sc.rules.sync_window_s / max(1, len(team)-1))
            delays = CONFIG.strike_delays if CONFIG.pad_takeoffs else (0.0, 8.0, 20.0, 45.0, 90.0)
            for delay, angle, layers in itertools.product(delays, (0.0, 35.0, -35.0),
                                                             effector_layer_sets()):
                self.checker.check_time(stop)
                arrive = base + delay
                if arrive + spacing*(len(team)-1) > contact.window[1] + EPS:
                    break
                extra = {scout.id: scout_route}
                for i, eff in enumerate(team):
                    z = layers[i % len(layers)]
                    route, earliest = effector_route(eff, contact, arrive + spacing*i, z, angle, self.sc)
                    if route is None:
                        break
                    extra[eff.id] = route
                if len(extra) != len(team)+1:
                    if stats is not None:
                        self._progress(stats, progress, "strike_route")
                    continue
                if CONFIG.pad_takeoffs and pad_conflict(self.sc, self.aircraft, parent.routes,
                                                 {aid: r for aid, r in extra.items() if aid != scout.id}):
                    # Neighbouring pads climbing together: R8 without a validator call.
                    if stats is not None:
                        self._progress(stats, progress, "strike_rule:R8")
                    continue
                candidate = self.checker.extend(parent, extra)
                if candidate is None and stats is not None:
                    self._progress(stats, progress, f"strike_rule:{self.checker.last_rule}")
                if candidate is not None:
                    yield candidate, team
                    found = True
                if found:
                    break

    def strike_route_options(self, contact, groups, scout, scout_route, obs_end, stop):
        """A small, concrete sample of the routes ``strikes`` will try.

        This is deliberately route based: the strike LNS move must identify R8
        conflicts from the validator's separation items, not from pad or target
        distance heuristics.  It does not validate or mutate a state.
        """
        base = max(obs_end + self.sc.rules.confirm_margin_s + 2.0, contact.window[0])
        count = 0
        for team in groups:
            spacing = min(5.0, self.sc.rules.sync_window_s / max(1, len(team) - 1))
            for delay, angle, layers in itertools.product(CONFIG.strike_delays, (0.0, 35.0, -35.0),
                                                            effector_layer_sets()):
                self.checker.check_time(stop)
                arrive = base + delay
                if arrive + spacing * (len(team) - 1) > contact.window[1] + EPS:
                    break
                extra = {}
                for i, eff in enumerate(team):
                    route, _ = effector_route(eff, contact, arrive + spacing * i,
                                              layers[i % len(layers)], angle, self.sc)
                    if route is None:
                        break
                    extra[eff.id] = route
                if len(extra) == len(team):
                    yield team, extra
                    count += 1
                    if count >= CONFIG.strike_conflict_routes:
                        return

    def strike_conflicts(self, state, routes):
        """Return existing aircraft which really violate R8/pad spacing with routes.

        The caller supplies only prospective effector routes.  Every candidate is
        parsed through the same route-info builder as incremental validation and
        compared with every unchanged route, including scouts.
        """
        conflicts = set()
        infos = {}
        for aid, route in routes.items():
            info = self.checker.route_info(aid, route)
            if info[2]:
                continue
            infos[aid] = info
        for aid, info in infos.items():
            for oid, old in state.infos.items():
                if oid == aid or oid in infos:
                    continue
                item = old[3]
                if item is not None and info[3] is not None and rules.separation_violation(
                        self.sc.rules, self.contacts, info[3][:5], item[:5]):
                    conflicts.add(oid)
            # The cheap pad test catches the same takeoff geometry before a full R8
            # trajectory check, and identifies the concrete neighbouring aircraft.
            for oid in pad_neighbours(self.sc, self.aircraft, aid):
                old = state.routes.get(oid)
                if old and pad_conflict(self.sc, self.aircraft, state.routes, {aid: routes[aid]}):
                    conflicts.add(oid)
        return conflicts

    def observed_strike(self, state, cid):
        """(scout, route, observation finish) for an already observed hostile."""
        for sid, tasks in state.tasks.items():
            if not any(t["cid"] == cid for t in tasks):
                continue
            route = state.routes.get(sid)
            if route is None:
                continue
            times = [w["t"] + w.get("dwell_s", 0.0) for w in route
                     if w.get("action") == "observe" and w.get("target") == cid]
            if times:
                return self.aircraft[sid], route, max(times)
        return None

    def drop_strike(self, state, cid):
        """Drop just cid's effector routes, retaining its harmless scout observation."""
        team = {aid for aid, route in state.parsed.items()
                if any(w.action == "engage" and w.target == cid for w in route.waypoints)}
        if not team:
            return state
        return self.checker.extend(self.checker.without(state, team), {})

    def effector_earliest(self, eff, contact):
        key = ("eff_earliest", eff.id, contact.id)
        hit = self._trip_cache.get(key)
        if hit is None:
            route, earliest = effector_route(eff, contact, 0.0, EFFECTOR_Z[0][0], 0.0, self.sc)
            hit = self._trip_cache[key] = 0.0 if route is not None else earliest
        return hit
