"""Search states and incremental rule checks (official rules on changed routes only).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import itertools
import math
import time

from fleetkit.energy import route_energy_wh
from fleetkit.geometry import EPS, dist3
from fleetkit.model import EFFECTOR, HOSTILE, SCOUT, UNKNOWN, parse_plan
from fleetkit import validator as rules
from deadline import DeadlineReached


@dataclass
class State:
    routes: dict
    tasks: dict
    used: frozenset
    parsed: dict
    separation: dict
    energies: dict
    served: frozenset
    score: float
    node_id: int = 0
    keys: dict = field(default_factory=dict)
    # IncrementalValidator.route_info entry of every route (avoids re-hashing keys).
    infos: dict = field(default_factory=dict)

    def plan(self, seed):
        return {"seed": seed, "routes": [
            {"aircraft": aid, "waypoints": self.routes[aid]} for aid in sorted(self.routes)]}

    def signature(self):
        return tuple(sorted(self.keys.items()))


def route_key(route):
    """Hashable identity of a waypoint list; routes are never edited after construction."""
    return tuple((w["t"], w["x"], w["y"], w["z"], w.get("dwell_s", 0.0), w.get("action"),
                  w.get("target")) for w in route)


class IncrementalValidator:
    """Reuse valid unchanged routes, including their cached R8 flight pieces.

    Local rules run on replaced/new routes. R4 and R10 run on the entire plan:
    rebuilding a scout tour can invalidate an earlier engagement's confirmation.
    R8 checks every pair containing a changed route. Unchanged pairs were already
    validated in the parent. Scoring uses the same energy and observation helpers.
    """
    def __init__(self, scenario, stop, sep_buffer=0.0):
        self.sc = scenario
        # R8 pair checks use d_sep_m + sep_buffer so flown trajectories keep margin after a cut.
        self.sep_rules = replace(scenario.rules, d_sep_m=scenario.rules.d_sep_m + sep_buffer)
        self.aircraft = scenario.aircraft_by_id()
        self.contacts = scenario.contact_by_id()
        self.stop = stop
        self.calls = 0
        self.seconds = 0.0
        self.rejections = {}
        self.last_rule = None
        # Routes are never edited after construction, so everything derived from one
        # route (parsed form, local rule result, R8 pieces, energy) and every R8 pair
        # result is a pure function of the routes: sibling branches retry the same scout
        # route with ~100 strike variants, and passes rebuild the same routes.
        self._routes = {}   # (aid, route_key) -> (rid, Route, violations, item, energy)
        self._pairs = {}    # (rid, rid) -> R8 violation or None
        self.cache_hits = 0

    def route_info(self, aid, waypoints, key=None):
        k = (aid, route_key(waypoints) if key is None else key)
        hit = self._routes.get(k)
        if hit is not None:
            self.cache_hits += 1
            return hit
        if len(self._routes) > 200000:
            self._routes.clear()
            self._pairs.clear()
        plan = {"seed": self.sc.seed, "routes": [{"aircraft": aid, "waypoints": waypoints}]}
        changed, errors = parse_plan(plan)
        if errors or changed is None:
            raise ValueError("route builder produced malformed waypoints: " + str(errors))
        route, violations = changed[aid], None
        for check in (rules._r1, rules._r2, rules._r3, rules._r5, rules._r6,
                      rules._r7, rules._r9):
            found = check(self.sc, changed, plan) if check is rules._r1 else check(self.sc, changed)
            if found:
                violations = found
                break
        item = energy = None
        obs, eng = [], []
        if violations is None:
            ac = self.aircraft[aid]
            item = rules.separation_item(aid, route, ac)
            energy = route_energy_wh(route.waypoints, ac)
            if item is not None:
                # Bounding box of the flight (pieces are straight, so the waypoints bound
                # it) and its time span: a quick exact pre-filter for R8 pairs.
                pts = [w.pos for w in route.waypoints]
                item = item + ((min(p[0] for p in pts), max(p[0] for p in pts),
                                min(p[1] for p in pts), max(p[1] for p in pts),
                                min(p[2] for p in pts), max(p[2] for p in pts)),)
            # What R4, R10 and scoring read from this route.
            for wp in route.waypoints:
                c = self.contacts.get(wp.target)
                if wp.action == "observe" and c is not None and rules.valid_observation(wp, c, self.sc):
                    obs.append((c.id, wp.t + wp.dwell_s, ac.type == SCOUT, c.cls == UNKNOWN))
                elif wp.action == "engage":
                    strike = c is not None and c.cls == HOSTILE and ac.type == EFFECTOR
                    eng.append((wp.target, wp.t, strike, ac.subtype or "?",
                                strike and dist3(wp.pos, c.pos) <= self.sc.rules.R_eng_m + EPS))
        hit = self._routes[k] = (len(self._routes), route, violations, item, energy, obs, eng)
        return hit

    def global_rules(self, infos):
        """R4 then R10 over the whole plan from per-route summaries; the same verdicts as
        rules._r4 and rules._r10. Returns a violation list or None."""
        margin = self.sc.rules.confirm_margin_s
        first_obs = {}
        groups = {}
        for info in infos:
            for cid, end, by_scout, _ in info[5]:
                if by_scout and end < first_obs.get(cid, math.inf):
                    first_obs[cid] = end
            for cid, t, strike, sub, close in info[6]:
                if strike:
                    groups.setdefault(cid, []).append((t, sub, close))
        for cid, group in groups.items():
            for t, _, _ in group:
                if not first_obs.get(cid, math.inf) <= t - margin + EPS:
                    return [rules.Violation("R4", "engagement without a confirmed observation",
                                            contact=cid)]
        for cid, group in groups.items():
            counts = {}
            for _, sub, _ in group:
                counts[sub] = counts.get(sub, 0) + 1
            want = {k: v for k, v in self.contacts[cid].needs.items() if v > 0}
            times = [t for t, _, _ in group]
            if (counts != want or max(times) - min(times) > self.sc.rules.sync_window_s + EPS
                    or not all(close for _, _, close in group)):
                return [rules.Violation("R10", "engagement group", contact=cid)]
        return None

    def apart(self, a, b):
        """True when two separation items can never come within d_sep: their time spans
        do not overlap, or their bounding boxes are at least d_sep apart on one axis."""
        if min(a[3], b[3]) - max(a[2], b[2]) <= 1e-12:
            return True
        d = self.sep_rules.d_sep_m
        p, q = a[5], b[5]
        return (p[0] - q[1] >= d or q[0] - p[1] >= d or p[2] - q[3] >= d or q[2] - p[3] >= d
                or p[4] - q[5] >= d or q[4] - p[5] >= d)

    def pair_violation(self, a, b):
        """R8 for two (rid, item) routes, memoised by route ids."""
        key = (a[0], b[0]) if a[0] < b[0] else (b[0], a[0])
        if key in self._pairs:
            self.cache_hits += 1
            return self._pairs[key]
        if a[1] is None or b[1] is None or self.apart(a[1], b[1]):
            violation = None
        else:
            violation = rules.separation_violation(self.sep_rules, self.contacts, a[1][:5], b[1][:5])
        if len(self._pairs) > 2000000:
            self._pairs.clear()
        self._pairs[key] = violation
        return violation

    def check_time(self, stop=None):
        if time.monotonic() >= min(self.stop, stop if stop is not None else self.stop):
            raise DeadlineReached

    def reject(self, violations):
        key = violations[0].rule
        self.last_rule = key
        self.rejections[key] = self.rejections.get(key, 0) + 1
        return None

    @staticmethod
    def without(parent, removed):
        """Parent minus whole routes; extend() must still recheck R4/R10 globally."""
        def keep(d):
            return {k: v for k, v in d.items() if k not in removed}
        return replace(parent, routes=keep(parent.routes), parsed=keep(parent.parsed),
                       separation=keep(parent.separation), energies=keep(parent.energies),
                       infos=keep(parent.infos), keys=keep(parent.keys))

    def extend(self, parent, extra):
        self.check_time()
        start = time.monotonic()
        self.calls += 1
        try:
            return self._extend(parent, extra)
        finally:
            self.seconds += time.monotonic() - start

    def quick_reject(self, extra):
        """Exact subset of R1 (horizon) and R9 (windows) on raw waypoints, before parsing."""
        horizon = self.sc.horizon_s
        for route in extra.values():
            for wp in route:
                start = wp["t"]
                end = start + wp.get("dwell_s", 0.0)
                if start < -EPS or end > horizon + EPS:
                    return "R1"
                contact = self.contacts.get(wp.get("target"))
                if contact is not None and contact.window is not None and wp.get("action"):
                    lo, hi = contact.window
                    if start < lo - EPS or (end if wp["action"] == "observe" else start) > hi + EPS:
                        return "R9"
        return None

    def _extend(self, parent, extra):
        rule = self.quick_reject(extra)
        if rule:
            self.last_rule = rule
            self.rejections[rule] = self.rejections.get(rule, 0) + 1
            return None
        keys = {aid: route_key(route) for aid, route in extra.items()}
        infos = {aid: self.route_info(aid, route, keys[aid]) for aid, route in extra.items()}
        for info in infos.values():
            if info[2]:
                return self.reject(info[2])
        changed = {aid: info[1] for aid, info in infos.items()}
        parsed = {**parent.parsed, **changed}
        every = {**parent.infos, **infos}
        if len(every) != len(set(parent.parsed) | set(infos)):
            every = None
        if every is not None:
            violations = self.global_rules(every.values())
            if violations:
                return self.reject(violations)
        else:
            for check in (rules._r4, rules._r10):
                violations = check(self.sc, parsed)
                if violations:
                    return self.reject(violations)
        new = [(info[0], info[3]) for info in infos.values()]
        old = []
        for aid, item in parent.separation.items():
            if aid in changed:
                continue
            info = parent.infos.get(aid)
            # Parent routes normally come through here; otherwise check without the memo.
            old.append((info[0], item) if info is not None and info[3] is item else (-1, item))
        for a, b in itertools.chain(itertools.combinations(new, 2), itertools.product(new, old)):
            self.check_time()
            if a[0] < 0 or b[0] < 0:
                violation = (None if a[1] is None or b[1] is None else
                             rules.separation_violation(self.sep_rules, self.contacts,
                                                        a[1][:5], b[1][:5]))
            else:
                violation = self.pair_violation(a, b)
            if violation:
                return self.reject([violation])
        new_items = {aid: info[3] for aid, info in infos.items()}
        old_items = {aid: item for aid, item in parent.separation.items() if aid not in changed}
        energies = dict(parent.energies)
        energies.update({aid: info[4] for aid, info in infos.items()})
        served = set()
        used = set()
        if every is not None:
            for aid, info in every.items():
                for cid, _, _, unknown in info[5]:
                    if unknown:
                        served.add(cid)
                for cid, *_ in info[6]:
                    served.add(cid)
                    used.add(aid)
        else:
            for aid, route in parsed.items():
                for wp in route.waypoints:
                    if wp.action == "engage":
                        served.add(wp.target)
                        used.add(aid)
                    elif wp.action == "observe":
                        c = self.contacts[wp.target]
                        if c.cls == UNKNOWN and rules.valid_observation(wp, c, self.sc):
                            served.add(c.id)
        score = (sum(self.contacts[cid].points for cid in served)
                 - self.sc.rules.lambda_e * sum(energies.values())
                 - self.sc.rules.c_eff * len(used))
        keys = {**parent.keys, **keys}
        return State({**parent.routes, **extra}, parent.tasks, frozenset(used), parsed,
                     {**old_items, **new_items}, energies, frozenset(served), score, keys=keys,
                     infos={**parent.infos, **infos})
