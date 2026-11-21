"""Deadline-bounded beam search using official baseline routes and rule checks.

Nodes are valid, complete-to-home partial fleet plans. No route is edited in place.
The matrix ranks candidates; only official rule checks establish validity.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import itertools
import math
import random
import time

from fleetkit import baseline
from fleetkit.baseline import (_Planner, _effector_route, _scout_route, EFFECTOR_LAYERS,
                               OBS_OFFSET_M, SCOUT_LAYERS, SPEED_FACTOR)
from fleetkit.energy import best_range_speed, route_energy_wh
from fleetkit.geometry import EPS, hdist
from fleetkit.model import HOSTILE, SCOUT, UNKNOWN, parse_plan
from fleetkit.scorer import evaluate
from fleetkit import validator as rules
from candidate_matrix import CandidateMatrix


class DeadlineReached(Exception):
    pass


# How far one attempt to serve a contact got, from least to most progress.
# An attempt is one call to Search.children for one parent node.
STAGES = ("no_options", "no_team", "scout_route", "scout_rule", "strike_route",
          "strike_rule", "not_improving", "accepted")


def outcome_stage(outcome):
    return outcome.split(":", 1)[0]


def unserved_reason(contact, banned, stats):
    """One primary reason why a contact is missing from the winning plan.

    collateral_ban / no_window: excluded before the search.
    never_tried: no attempt ran (the pass order or the deadline never reached it).
    outranked: some branch served it validly, but that branch did not win.
    Otherwise the most common attempt outcome, e.g. "scout_rule:R4", "time_cap".
    """
    if contact.id in banned:
        return "collateral_ban"
    if contact.window is None:
        return "no_window"
    if stats is None or (not stats["attempts"] and not stats["warm_accepted"]):
        return "never_tried"
    outcomes = stats["outcomes"]
    if stats["warm_accepted"] or outcomes.get("accepted"):
        return "outranked"
    if not outcomes:
        return "never_tried"
    # Most frequent outcome; ties go to the attempt that got furthest.
    order = {name: i for i, name in enumerate(STAGES)}
    return max(outcomes, key=lambda o: (outcomes[o], order.get(outcome_stage(o), -1), o))


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
    def __init__(self, scenario, stop):
        self.sc = scenario
        self.aircraft = scenario.aircraft_by_id()
        self.contacts = scenario.contact_by_id()
        self.stop = stop
        self.calls = 0
        self.seconds = 0.0
        self.rejections = {}
        self.last_rule = None

    def check_time(self, stop=None):
        if time.monotonic() >= min(self.stop, stop if stop is not None else self.stop):
            raise DeadlineReached

    def reject(self, violations):
        key = violations[0].rule
        self.last_rule = key
        self.rejections[key] = self.rejections.get(key, 0) + 1
        return None

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
        plan = {"seed": self.sc.seed, "routes": [
            {"aircraft": aid, "waypoints": route} for aid, route in extra.items()]}
        changed, errors = parse_plan(plan)
        if errors or changed is None:
            raise ValueError("route builder produced malformed waypoints: " + str(errors))
        for check in (rules._r1, rules._r2, rules._r3, rules._r5, rules._r6,
                      rules._r7, rules._r9):
            violations = check(self.sc, changed, plan) if check is rules._r1 else check(self.sc, changed)
            if violations:
                return self.reject(violations)
        parsed = {**parent.parsed, **changed}
        for check in (rules._r4, rules._r10):
            violations = check(self.sc, parsed)
            if violations:
                return self.reject(violations)
        new_items = {aid: rules.separation_item(aid, route, self.aircraft[aid])
                     for aid, route in changed.items()}
        old_items = {aid: item for aid, item in parent.separation.items() if aid not in changed}
        pairs = itertools.chain(itertools.combinations(new_items.values(), 2),
                                itertools.product(new_items.values(), old_items.values()))
        for a, b in pairs:
            self.check_time()
            if a is not None and b is not None:
                violation = rules.separation_violation(self.sc.rules, self.contacts, a, b)
                if violation:
                    return self.reject([violation])
        energies = dict(parent.energies)
        energies.update({aid: route_energy_wh(route.waypoints, self.aircraft[aid])
                         for aid, route in changed.items()})
        keys = {**parent.keys, **{aid: route_key(route) for aid, route in extra.items()}}
        return self.state({**parent.routes, **extra}, parent.tasks, parsed,
                          {**old_items, **new_items}, energies, keys)

    def without(self, parent, aids, tasks):
        """Parent minus whole routes. Callers drop every engagement of a contact together
        and only scouts with no remaining observations, so no rule can newly fail."""
        def keep(d):
            return {k: v for k, v in d.items() if k not in aids}
        return self.state(keep(parent.routes), tasks, keep(parent.parsed), keep(parent.separation),
                          keep(parent.energies), keep(parent.keys))

    def state(self, routes, tasks, parsed, separation, energies, keys):
        served = set()
        used = set()
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
        return State(routes, tasks, frozenset(used), parsed, separation, energies,
                     frozenset(served), score, keys=keys)


class WarmPlanner(_Planner):
    """The original greedy choices, with incremental checks and a deadline."""
    def __init__(self, scenario, checker, state, stop):
        super().__init__(scenario)
        self.checker, self.state, self.stop = checker, state, stop
        self.last = None

    def _try(self, extra):
        self.checker.check_time(self.stop)
        self.last = self.checker.extend(self.state, extra)
        return self.last.score if self.last is not None else None

    def _add_observation(self, *args):
        self.checker.check_time(self.stop)
        return super()._add_observation(*args)


class Search:
    def __init__(self, scenario, seconds=55.0, top_n=5, beam_width=6, start=None, lns_share=0.5,
                 scout_cost="time"):
        if seconds < 0 or not 1 <= top_n <= 100 or not 1 <= beam_width <= 100:
            raise ValueError("seconds must be nonnegative; top_n and beam_width must be 1..100")
        if not 0 <= lns_share <= 0.9:
            raise ValueError("lns_share must be 0..0.9")
        if scout_cost not in ("time", "distance"):
            raise ValueError("scout_cost must be time or distance")
        self.lns_share = lns_share
        # Primary key for ranking scout insertion positions; the other breaks ties.
        self.scout_cost = scout_cost
        self.lns_stats = {"iterations": 0, "accepted": 0, "improved": 0}
        self.sc = scenario
        self.start = time.monotonic() if start is None else start
        # Includes input reading when the CLI passes its process start time.
        self.stop = self.start + min(seconds, 55.0)
        self.top_n, self.width = top_n, beam_width
        self.checker = IncrementalValidator(scenario, self.stop)
        self.root = State({}, {a.id: [] for a in scenario.aircraft if a.type == SCOUT},
                          frozenset(), {}, {}, {}, frozenset(), 0.0)
        self.trace = [{"id": 0, "parent": None, "decision": "start", "score": 0.0,
                       "served": [], "depth": 0}]
        self.archive = [self.root]
        self.signatures = {self.root.signature()}
        self.matrix = None
        self.passes = 0
        # Per-contact attempt accounting for "why was this contact not served".
        self.attempts = {}
        self.repaired = set()

    def stats(self, cid):
        if cid not in self.attempts:
            self.attempts[cid] = {"attempts": 0, "warm_accepted": 0, "outcomes": {}, "rules": {}}
        return self.attempts[cid]

    def record(self, state, parent, decision, depth):
        # Trace stores metadata, not full copies of every discarded plan.
        state = replace(state, node_id=len(self.trace))
        self.trace.append({"id": state.node_id, "parent": parent.node_id,
                           "decision": decision, "score": round(state.score, 6),
                           "served": sorted(state.served), "depth": depth})
        if len(self.archive) < self.top_n or state.score > self.archive[-1].score:
            signature = state.signature()
            if signature not in self.signatures:
                self.archive.append(state)
                self.archive.sort(key=lambda s: -s.score)
                self.archive = self.archive[:self.top_n]
                self.signatures = {s.signature() for s in self.archive}
        return state

    def warm_start(self):
        stop = min(self.stop, self.start + min(10.0, max(0.0, (self.stop-self.start)*0.25)))
        p = WarmPlanner(self.sc, self.checker, self.root, stop)
        contacts = (sorted((c for c in self.sc.contacts if c.cls == HOSTILE), key=lambda c: -c.points)
                    + sorted((c for c in self.sc.contacts if c.cls == UNKNOWN), key=lambda c: -c.points))
        for depth, c in enumerate(contacts, 1):
            self.checker.check_time(stop)
            accepted = p.serve_hostile(c) if c.cls == HOSTILE else p.serve_unknown(c)
            if accepted:
                self.stats(c.id)["warm_accepted"] += 1
                state = replace(p.last, tasks=dict(p.tasks))
                p.state = self.record(state, p.state, {"kind": "greedy", "contact": c.id}, depth)

    def insertion_costs(self, p, contact):
        """(added flight time, added distance, scout id, tour position, scout) for every
        position of every scout tour that passes the time-window bound, cheapest first."""
        ranked = []
        for a in p._scout_candidates(contact, limit=len(p.tasks)):
            for k, seconds, metres in self.feasible_positions(a, p.tasks[a.id], contact):
                ranked.append((seconds, metres, a.id, k, a))
        if self.scout_cost == "distance":
            ranked.sort(key=lambda r: (r[1], r[0], r[2], r[3]))
        else:
            ranked.sort(key=lambda r: r[:4])
        return ranked

    def feasible_positions(self, scout, tasks, contact):
        """Tour positions where the contact could still fit, by a lower bound on timing:
        straight legs at the lowest layer, departing at 0, the observation point within
        OBS_OFFSET_M of the contact. A rejected position cannot pass _scout_route and R1.
        Returns (position, added flight time, added distance); time includes hovering while
        waiting for windows, which costs energy like flying."""
        dwell, horizon = self.sc.rules.obs_dwell_s, self.sc.horizon_s
        speed = min(SPEED_FACTOR * scout.v_h_mps, best_range_speed(scout.power))
        z = min(SCOUT_LAYERS)
        climb = z / (SPEED_FACTOR * scout.v_up_mps) + 0.5
        descent = z / (SPEED_FACTOR * scout.v_down_mps) + 0.5
        home, pos = scout.home, contact.pos
        points = [t["point"] for t in tasks]
        # ends[i]: earliest end of observation i; latest[i]: latest start of observation i.
        ends, t, cur = [], climb, home
        for task, point in zip(tasks, points):
            t = max(t + hdist(cur, point) / speed, task["window"][0]) + dwell
            ends.append(t)
            cur = point
        latest, nxt = [0.0] * len(tasks), horizon - descent
        for i in range(len(tasks) - 1, -1, -1):
            after = points[i+1] if i + 1 < len(tasks) else home
            latest[i] = nxt = min(tasks[i]["window"][1], nxt - hdist(points[i], after) / speed) - dwell
        lo, hi = contact.window
        stops = [(t["point"], t["window"]) for t in tasks]
        before_s = self.tour_seconds(speed, climb, descent, home, stops)
        out = []
        for k in range(len(tasks) + 1):
            before = points[k-1] if k else home
            after = points[k] if k < len(tasks) else home
            start = max((ends[k-1] if k else climb)
                        + max(0.0, hdist(before, pos) - OBS_OFFSET_M) / speed, lo)
            if start + dwell > hi + EPS:
                continue
            arrive = start + dwell + max(0.0, hdist(pos, after) - OBS_OFFSET_M) / speed
            if k < len(tasks):
                if max(arrive, tasks[k]["window"][0]) > latest[k] + EPS:
                    continue
            elif arrive + descent > horizon + EPS:
                continue
            seconds = self.tour_seconds(speed, climb, descent, home,
                                        stops[:k] + [(pos, contact.window)] + stops[k:]) - before_s
            out.append((k, seconds, hdist(before, pos) + hdist(pos, after) - hdist(before, after)))
        return out

    def tour_seconds(self, speed, climb, descent, home, stops):
        """Estimated airborne time of a tour on straight legs: takeoff is delayed so the
        first window is not waited for on the ground."""
        if not stops:
            return 0.0
        dwell = self.sc.rules.obs_dwell_s
        travel = climb + hdist(home, stops[0][0]) / speed
        t = max(travel, stops[0][1][0])
        depart = t - travel
        t += dwell
        for (a, _), (b, window) in zip(stops, stops[1:]):
            t = max(t + hdist(a, b) / speed, window[0]) + dwell
        return t + hdist(stops[-1][0], home) / speed + descent - depart

    def observation(self, p, scout, contact, z, angle, shift, k):
        """baseline._Planner._add_observation, inserting the task at tour position k."""
        task = {"cid": contact.id, "point": p._obs_point(scout, contact, angle), "window": contact.window}
        tasks = p.tasks[scout.id][:k] + [task] + p.tasks[scout.id][k:]
        route = self.scout_tour(scout, tasks, z, shift)
        return (tasks, route) if route is not None else None

    def scout_tour(self, scout, tasks, z, shift=0.0):
        speed = min(SPEED_FACTOR * scout.v_h_mps, best_range_speed(scout.power))
        travel = hdist(scout.home, tasks[0]["point"]) / speed + z / scout.v_up_mps + 3.0
        return _scout_route(scout, tasks, max(0.0, tasks[0]["window"][0] - travel) + shift, z, self.sc)

    def children(self, parent, contact, stop, limit=2, slots=6):
        """Bounded candidate generation, recording how far each attempt got."""
        if contact.id in parent.served:
            return
        stats = self.stats(contact.id)
        stats["attempts"] += 1
        progress = {"stage": -1, "outcome": "no_options"}
        outcome = None
        try:
            yield from self._children(parent, contact, stop, limit, stats, progress, slots)
            outcome = progress["outcome"]
        except DeadlineReached:
            outcome = "accepted" if progress["outcome"] == "accepted" else "time_cap"
            raise
        finally:
            if outcome is None:  # Closed early by the consumer.
                outcome = progress["outcome"]
            stats["outcomes"][outcome] = stats["outcomes"].get(outcome, 0) + 1

    @staticmethod
    def _progress(stats, progress, outcome):
        stage = STAGES.index(outcome_stage(outcome))
        if ":" in outcome:
            stats["rules"][outcome] = stats["rules"].get(outcome, 0) + 1
        if stage >= progress["stage"]:
            progress["stage"], progress["outcome"] = stage, outcome

    def _children(self, parent, contact, stop, limit, stats, progress, slots):
        """Bounded candidate generation: alternate scouts, teams and flight settings."""
        groups = self.matrix.groups(contact, parent.used) if contact.cls == HOSTILE else [()]
        if not groups:
            self._progress(stats, progress, "no_team")
            return
        p = _Planner(self.sc)
        p.tasks = parent.tasks
        count = 0
        seen = set()
        positions = [(a, k) for *_, k, a in self.insertion_costs(p, contact)[:slots]]
        if not positions:
            self._progress(stats, progress, "scout_route")
            return
        # Round-robin teams before spending time on fine timing adjustments.
        for (scout, k), z, obs_angle, shift in itertools.product(positions,
                                                                SCOUT_LAYERS, (0.0, 60.0, -60.0),
                                                                (0.0, 7.0)):
            self.checker.check_time(stop)
            built = self.observation(p, scout, contact, z, obs_angle, shift, k)
            if built is None:
                self._progress(stats, progress, "scout_route")
                continue
            tasks, route = built
            scout_extra = {scout.id: route}
            scout_state = self.checker.extend(parent, scout_extra)
            if scout_state is None:
                self._progress(stats, progress, f"scout_rule:{self.checker.last_rule}")
                continue
            if contact.cls == UNKNOWN:
                options = [(scout_state, ())]
            else:
                obs_end = max(w["t"] + w.get("dwell_s", 0) for w in route
                              if w.get("target") == contact.id and w.get("action") == "observe")
                options = self.strikes(parent, contact, groups, scout, route, obs_end, stop,
                                       stats, progress)
            for candidate, team in options:
                if candidate.score <= parent.score:
                    self._progress(stats, progress, "not_improving")
                    continue
                signature = candidate.signature()
                if signature in seen:
                    continue
                seen.add(signature)
                candidate.tasks = {**parent.tasks, scout.id: tasks}
                decision = {"kind": "serve", "contact": contact.id, "scout": scout.id,
                            "effectors": [a.id for a in team]}
                self._progress(stats, progress, "accepted")
                yield candidate, decision
                count += 1
                if count >= limit:
                    return

    def strikes(self, parent, contact, groups, scout, scout_route, obs_end, stop,
                stats=None, progress=None):
        base = max(obs_end + self.sc.rules.confirm_margin_s + 2.0, contact.window[0])
        # Try cheap alternatives to the nearest team. At most one child per team
        # for each scout setting, avoiding a beam filled with timing variants.
        for team in groups:
            found = False
            spacing = min(5.0, self.sc.rules.sync_window_s / max(1, len(team)-1))
            for delay, angle, rotation in itertools.product((0.0, 8.0, 20.0, 45.0, 90.0),
                                                             (0.0, 35.0, -35.0), range(3)):
                self.checker.check_time(stop)
                arrive = base + delay
                if arrive + spacing*(len(team)-1) > contact.window[1] + EPS:
                    break
                extra = {scout.id: scout_route}
                for i, eff in enumerate(team):
                    z = EFFECTOR_LAYERS[(i + rotation) % len(EFFECTOR_LAYERS)]
                    route, earliest = _effector_route(eff, contact, arrive + spacing*i, z, angle, self.sc)
                    if route is None:
                        break
                    extra[eff.id] = route
                if len(extra) != len(team)+1:
                    if stats is not None:
                        self._progress(stats, progress, "strike_route")
                    continue
                candidate = self.checker.extend(parent, extra)
                if candidate is None and stats is not None:
                    self._progress(stats, progress, f"strike_rule:{self.checker.last_rule}")
                if candidate is not None:
                    yield candidate, team
                    found = True
                if found:
                    break

    def repair(self, state, depth):
        """Greedy fill of what the search left unserved: unknowns join any scout tour at
        their cheapest positions, then hostiles retry with the free effectors. Within each
        class, contacts with the fewest scout alternatives (highest regret) go first."""
        improved = True
        while improved:
            improved = False
            contacts = [c for c in self.sc.contacts if c.cls in (HOSTILE, UNKNOWN)
                        and c.window is not None and c.id not in self.matrix.banned
                        and c.id not in state.served]
            p = _Planner(self.sc)
            p.tasks = state.tasks
            regret = {c.id: self.regret(p, c) for c in contacts}
            contacts.sort(key=lambda c: (c.cls != UNKNOWN, -regret[c.id], -self.matrix.priority(c), c.id))
            for contact in contacts:
                self.checker.check_time()
                local_stop = min(self.stop, time.monotonic() + 0.25)
                best = None
                try:
                    for child, decision in self.children(state, contact, local_stop, 4, 8):
                        if best is None or child.score > best[0].score:
                            best = (child, decision)
                except DeadlineReached:
                    self.checker.check_time()
                if best is not None:
                    depth += 1
                    state = self.record(best[0], state, {**best[1], "kind": "repair"}, depth)
                    self.repaired.add(state.signature())
                    improved = True
        return state

    def regret(self, p, contact):
        """Regret-2: extra flight time if the cheapest scout is unavailable; infinite with
        one feasible scout, -1 with none (tried last)."""
        best = {}
        for seconds, _, aid, _, _ in self.insertion_costs(p, contact):
            best.setdefault(aid, seconds)
        costs = sorted(best.values())
        if not costs:
            return -1.0
        return costs[1] - costs[0] if len(costs) > 1 else float("inf")

    def search(self, contacts, priorities, rng):
        """Beam passes over varied contact orders until the checker deadline.
        Returns False once the trace cap is reached."""
        while contacts:
            self.checker.check_time()
            # Different orders explore different scout tours and allocations.
            if self.passes % 4 == 0:
                order = sorted(contacts, key=lambda c: (-priorities[c.id], c.id))
            elif self.passes % 4 == 1:
                order = sorted(contacts, key=lambda c: (c.window[1], -priorities[c.id], c.id))
            elif self.passes % 4 == 2:
                order = sorted(contacts, key=lambda c: (-c.points, c.id))
            else:
                noise = {c.id: rng.uniform(0.65, 1.35) for c in contacts}
                order = sorted(contacts, key=lambda c: (-priorities[c.id]*noise[c.id], c.id))
            beam = [self.root]
            for depth, contact in enumerate(order, 1):
                self.checker.check_time()
                next_beam = []
                for parent in beam:
                    self.checker.check_time()
                    skipped = self.record(parent, parent, {"kind": "skip", "contact": contact.id}, depth)
                    next_beam.append(skipped)
                    remaining = max(1, (len(order)-depth+1)*len(beam))
                    allowance = min(0.12, max(0.005, (self.stop-time.monotonic()) / (remaining*1.5)))
                    local_stop = min(self.stop, time.monotonic()+allowance)
                    try:
                        for child, decision in self.children(parent, contact, local_stop):
                            next_beam.append(self.record(child, parent, decision, depth))
                    except DeadlineReached:
                        self.checker.check_time()  # Local cap expires without ending search.
                unique = {}
                for state in sorted(next_beam, key=lambda s: -s.score):
                    unique.setdefault(state.signature(), state)
                beam = list(unique.values())[:self.width]
            self.passes += 1
            # Bound trace size for long runs on tiny or impossible scenarios.
            if len(self.trace) >= 20000:
                return False
        return True

    def remove(self, state, cids):
        """State without the given served contacts: their strike routes are dropped and
        their scouts' tours rebuilt, first keeping the old takeoff time and altitude.
        None if a rebuilt tour fails every variant."""
        drop = {aid for aid, route in state.routes.items()
                if any(w.get("action") == "engage" and w.get("target") in cids for w in route)}
        tasks, rebuild = dict(state.tasks), []
        for aid, tour in state.tasks.items():
            kept = [t for t in tour if t["cid"] not in cids]
            if len(kept) == len(tour):
                continue
            tasks[aid] = kept
            if kept:
                rebuild.append(aid)
            else:
                drop.add(aid)
        new = self.checker.without(state, drop, tasks)
        for aid in rebuild:
            scout, old = self.checker.aircraft[aid], state.routes[aid]
            variants = [(old[1]["z"], old[0]["t"])] + [
                (z, None, shift) for z in (old[1]["z"],) + tuple(l for l in SCOUT_LAYERS if l != old[1]["z"])
                for shift in (0.0, 7.0, 15.0)]
            for variant in variants:
                self.checker.check_time()
                if variant[1] is None:
                    route = self.scout_tour(scout, tasks[aid], variant[0], variant[2])
                else:
                    route = _scout_route(scout, tasks[aid], variant[1], variant[0], self.sc)
                extended = self.checker.extend(new, {aid: route}) if route is not None else None
                if extended is not None:
                    new = extended
                    break
            else:
                return None
        new.tasks = tasks
        return new

    def destroy(self, state, rng):
        """2-5 served contacts chosen at random, by proximity in space and time to a
        random seed, by lowest net value of hostile strikes, or one scout's whole tour."""
        served = sorted(state.served)
        if not served:
            return set()
        k = min(len(served), rng.randint(2, 5))
        contacts = self.checker.contacts
        op = rng.randrange(4)
        if op == 1:
            seed = contacts[rng.choice(served)]
            served.sort(key=lambda cid: hdist(contacts[cid].pos, seed.pos)
                        + 20.0 * abs(contacts[cid].window[0] - seed.window[0]))
            return set(served[:k])
        if op == 2:
            strikers = {}
            for aid, route in state.routes.items():
                for w in route:
                    if w.get("action") == "engage":
                        strikers.setdefault(w["target"], []).append(aid)
            rules = self.sc.rules
            value = {cid: (contacts[cid].points - rules.c_eff * len(aids)
                           - rules.lambda_e * sum(state.energies[a] for a in aids))
                     * rng.uniform(0.8, 1.2) for cid, aids in strikers.items()}
            if value:
                return set(sorted(value, key=value.get)[:k])
        if op == 3:
            tours = [tour for tour in state.tasks.values() if len(tour) >= 2]
            if tours:
                cids = [t["cid"] for t in rng.choice(tours) if t["cid"] in state.served]
                if cids:
                    return set(cids[:6])
        return set(rng.sample(served, k))

    def reinsert(self, state, removed, contacts, priorities):
        """Regret-ordered greedy reinsertion of the removed contacts and the six
        highest-regret other unserved contacts."""
        p = _Planner(self.sc)
        p.tasks = state.tasks
        open_ = [c for c in contacts if c.id not in state.served]
        regret = {c.id: self.regret(p, c) for c in open_}
        open_.sort(key=lambda c: (-regret[c.id], -priorities[c.id], c.id))
        others = [c for c in open_ if c.id not in removed][:6]
        for contact in [c for c in open_ if c.id in removed or c in others]:
            self.checker.check_time()
            best = None
            try:
                for child, _ in self.children(state, contact, min(self.stop, time.monotonic() + 0.1), 3):
                    if best is None or child.score > best.score:
                        best = child
            except DeadlineReached:
                self.checker.check_time()
            if best is not None:
                state = best
        return state

    def lns(self, contacts, priorities, rng):
        """Large neighbourhood search from the best plan with simulated-annealing
        acceptance; temperature falls linearly to zero at the deadline."""
        current = best = self.archive[0]
        begin = time.monotonic()
        span = max(1e-9, self.stop - begin)
        stale = 0
        while True:
            self.checker.check_time()
            removed = self.destroy(current, rng)
            if not removed:
                return
            self.lns_stats["iterations"] += 1
            partial = self.remove(current, removed)
            if partial is None:
                continue
            candidate = self.reinsert(partial, removed, contacts, priorities)
            if candidate.signature() == current.signature():
                stale += 1
            else:
                temperature = 0.02 * abs(best.score) * max(0.0, self.stop - time.monotonic()) / span
                delta = candidate.score - current.score
                if delta > 0 or (temperature > 0 and rng.random() < math.exp(delta / temperature)):
                    self.lns_stats["accepted"] += 1
                    current = self.record(candidate, current, {"kind": "lns", "removed": sorted(removed)},
                                          len(candidate.served))
                    stale = 0 if current.score > best.score else stale + 1
                    if current.score > best.score:
                        self.lns_stats["improved"] += 1
                        best = current
                else:
                    stale += 1
            if stale >= 30:
                current, stale = self.archive[0], 0

    def repair_archive(self):
        """Repair archived plans, best first, each at most once."""
        while True:
            pending = [s for s in self.archive if s.signature() not in self.repaired]
            if not pending:
                return
            self.repaired.add(pending[0].signature())
            depth = max((n["depth"] for n in self.trace if n["id"] == pending[0].node_id), default=0)
            self.repair(pending[0], depth)

    def run(self):
        try:
            self.warm_start()
        except DeadlineReached:
            pass
        self.matrix = CandidateMatrix(self.sc)
        contacts = [c for c in self.sc.contacts if c.cls in (HOSTILE, UNKNOWN)
                    and c.window is not None and c.id not in self.matrix.banned]
        priorities = {c.id: self.matrix.priority(c) for c in contacts}
        rng = random.Random(self.sc.seed)
        # The beam and repair get the first part of the remaining time, LNS the rest.
        deadline = self.stop
        self.stop = self.stop - self.lns_share * max(0.0, self.stop - time.monotonic())
        try:
            self.beam_and_repair(contacts, priorities, rng)
        finally:
            self.stop = self.checker.stop = deadline
        if contacts:
            try:
                self.lns(contacts, priorities, rng)
            except DeadlineReached:
                pass
        # Official full validation is the authority; archive can include partial
        # plans because every scout already has its return-and-land leg.
        verified = []
        for state in self.archive:
            if time.monotonic() >= self.start + 58.0:
                break
            result = evaluate(self.sc, state.plan(self.sc.seed))
            if result.valid:
                verified.append((state, result))
        if not verified:
            verified = [(self.root, evaluate(self.sc, self.root.plan(self.sc.seed)))]
        verified.sort(key=lambda pair: -pair[1].score)
        return verified

    def beam_and_repair(self, contacts, priorities, rng):
        # Alternate: search until a reserve before the deadline, then repair the best
        # unrepaired plans. The reserve adapts to how long repairs take.
        reserve = min(3.0, 0.1 * max(0.0, self.stop - time.monotonic()))
        more = bool(contacts)
        while time.monotonic() < self.stop:
            # Too little time left to repair again: the last search uses all of it.
            final = time.monotonic() >= self.stop - reserve
            self.checker.stop = self.stop if final else self.stop - reserve
            try:
                if more:
                    more = self.search(contacts, priorities, rng)
            except DeadlineReached:
                pass
            self.checker.stop = self.stop
            if final:
                break
            began = time.monotonic()
            try:
                self.repair_archive()
            except DeadlineReached:
                break
            if not more:
                break
            reserve = min(reserve, max(0.5, 1.5 * (time.monotonic() - began)))

    def diagnostics(self, served):
        """Per-contact accounting against the winner's served set."""
        banned = self.matrix.banned if self.matrix is not None else CandidateMatrix(self.sc).banned
        served = set(served)
        contacts, summary, points = {}, {}, {}
        for c in self.sc.contacts:
            if c.cls not in (HOSTILE, UNKNOWN):
                continue
            stats = self.attempts.get(c.id)
            entry = {"cls": c.cls, "points": c.points, "served": c.id in served,
                     "attempts": stats["attempts"] if stats else 0,
                     "outcomes": dict(stats["outcomes"]) if stats else {},
                     "rules": dict(stats["rules"]) if stats else {}}
            if c.id not in served:
                reason = unserved_reason(c, banned, stats)
                entry["reason"] = reason
                summary[reason] = summary.get(reason, 0) + 1
                points[reason] = points.get(reason, 0) + c.points
            contacts[c.id] = entry
        return {"contacts": contacts,
                "unserved": dict(sorted(summary.items(), key=lambda kv: -points[kv[0]])),
                "unserved_points": dict(sorted(points.items(), key=lambda kv: -kv[1]))}

    def report(self, verified):
        diagnostics = self.diagnostics(verified[0][1].served if verified else [])
        return {"seed": self.sc.seed, "elapsed_s": time.monotonic()-self.start,
                "search_budget_s": max(0, self.stop-self.start), "passes": self.passes,
                "candidate_checks": self.checker.calls,
                "candidate_check_seconds": self.checker.seconds,
                "rejections": self.checker.rejections,
                "beam_width": self.width, "requested_top_n": self.top_n, "lns": self.lns_stats,
                "unserved": diagnostics["unserved"],
                "unserved_points": diagnostics["unserved_points"],
                "contacts": diagnostics["contacts"],
                "solutions": [{"rank": i, "node": state.node_id, "score": result.score,
                               "served": result.served} for i, (state, result) in enumerate(verified, 1)],
                "nodes": self.trace}
