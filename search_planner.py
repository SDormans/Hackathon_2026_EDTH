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
                               SCOUT_LAYERS, SPEED_FACTOR)
from fleetkit.energy import best_range_speed, route_energy_wh
from fleetkit.geometry import EPS, hdist
from fleetkit.model import HOSTILE, SCOUT, UNKNOWN, parse_plan
from fleetkit.scorer import evaluate
from fleetkit import validator as rules
from candidate_matrix import CandidateMatrix


class DeadlineReached(Exception):
    pass


DESTROY_OPERATORS = ("random", "related", "worst", "tour")


def scout_tour(scenario, scout, tasks, z, shift=0.0, depart=None):
    """Baseline scout tour for an ordered task list; same departure rule as the baseline."""
    if depart is None:
        speed = min(SPEED_FACTOR * scout.v_h_mps, best_range_speed(scout.power))
        travel = hdist(scout.home, tasks[0]["point"]) / speed + z / scout.v_up_mps + 3.0
        depart = max(0.0, tasks[0]["window"][0] - travel) + shift
    return _scout_route(scout, tasks, depart, z, scenario)


def insertion_positions(home, tasks, pos, limit):
    """Tour positions ordered by added horizontal distance (cheapest insertion first)."""
    points = [home[:2]] + [t["point"] for t in tasks] + [home[:2]]
    cost = [(hdist(points[i], pos) + hdist(pos, points[i+1]) - hdist(points[i], points[i+1]), i)
            for i in range(len(tasks) + 1)]
    return [i for _, i in sorted(cost)[:limit]]


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

    @staticmethod
    def without(parent, removed):
        """Parent minus whole routes; extend() must still recheck R4/R10 globally."""
        def keep(d):
            return {k: v for k, v in d.items() if k not in removed}
        return replace(parent, routes=keep(parent.routes), parsed=keep(parent.parsed),
                       separation=keep(parent.separation), energies=keep(parent.energies))

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
        keys = {**parent.keys, **{aid: route_key(route) for aid, route in extra.items()}}
        return State({**parent.routes, **extra}, parent.tasks, frozenset(used), parsed,
                     {**old_items, **new_items}, energies, frozenset(served), score, keys=keys)


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
    def __init__(self, scenario, seconds=55.0, top_n=5, beam_width=6, start=None, lns_share=0.4,
                 interleave=False, adaptive=False, restarts=True):
        if seconds < 0 or not 1 <= top_n <= 100 or not 1 <= beam_width <= 100:
            raise ValueError("seconds must be nonnegative; top_n and beam_width must be 1..100")
        if not 0 <= lns_share <= 1:
            raise ValueError("lns_share must be 0..1")
        self.sc = scenario
        self.start = time.monotonic() if start is None else start
        # Includes input reading when the CLI passes its process start time.
        self.stop = self.start + min(seconds, 55.0)
        self.top_n, self.width = top_n, beam_width
        # The beam ends early so the final share of the budget repairs its best plan.
        self.beam_stop = self.stop - lns_share * max(0.0, self.stop - self.start)
        self.checker = IncrementalValidator(scenario, self.stop)
        self.aircraft = scenario.aircraft_by_id()
        self.contacts = scenario.contact_by_id()
        self.lns = {"iterations": 0, "accepted": 0, "improvements": 0, "gain": 0.0}
        self.lns_share = lns_share
        self.interleave, self.adaptive, self.restarts = interleave, adaptive, restarts
        if interleave:
            # Beam passes alternate with LNS bursts; a short final tail is LNS only.
            self.beam_stop = self.stop - min(lns_share, 0.1) * max(0.0, self.stop - self.start)
        self.lns_rng = random.Random(scenario.seed + 7919)
        self.lns_state = None
        self.weights = {op: 1.0 for op in DESTROY_OPERATORS}
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

    def children(self, parent, contact, stop, limit=2, insert=False):
        """Bounded candidate generation, recording how far each attempt got.

        insert=False appends the visit to a scout tour (beam search); insert=True
        also tries the cheapest earlier tour positions (LNS repair).
        """
        if contact.id in parent.served:
            return
        stats = self.stats(contact.id)
        stats["attempts"] += 1
        progress = {"stage": -1, "outcome": "no_options"}
        outcome = None
        try:
            yield from self._children(parent, contact, stop, limit, stats, progress, insert)
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

    def scout_options(self, parent, contact, insert):
        """(scout, tasks, route or None) for each scout, tour position and flight setting."""
        p = _Planner(self.sc)
        scouts = p._scout_candidates(contact, limit=len(parent.tasks))
        if insert:
            def reach(a):
                points = [a.home] + [t["point"] for t in parent.tasks[a.id]]
                return min(hdist(q, contact.pos) for q in points)
            scouts.sort(key=reach)
        else:
            scouts.sort(key=lambda a: hdist(parent.tasks[a.id][-1]["point"] if parent.tasks[a.id]
                                           else a.home, contact.pos))
        for scout in scouts[:4]:
            current = parent.tasks[scout.id]
            positions = (insertion_positions(scout.home, current, contact.pos, 3) if insert
                         else [len(current)])
            for pos, z, obs_angle, shift in itertools.product(positions, SCOUT_LAYERS,
                                                               (0.0, 60.0, -60.0), (0.0, 7.0)):
                task = {"cid": contact.id, "point": p._obs_point(scout, contact, obs_angle),
                        "window": contact.window}
                tasks = current[:pos] + [task] + current[pos:]
                yield scout, tasks, scout_tour(self.sc, scout, tasks, z, shift)

    def _children(self, parent, contact, stop, limit, stats, progress, insert=False):
        """Bounded candidate generation: alternate scouts, teams and flight settings."""
        groups = self.matrix.groups(contact, parent.used) if contact.cls == HOSTILE else [()]
        if not groups:
            self._progress(stats, progress, "no_team")
            return
        count = 0
        seen = set()
        # Round-robin teams before spending time on fine timing adjustments.
        for scout, tasks, route in self.scout_options(parent, contact, insert):
            self.checker.check_time(stop)
            if route is None:
                self._progress(stats, progress, "scout_route")
                continue
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
        their cheapest positions, then hostiles retry with the free effectors."""
        improved = True
        while improved:
            improved = False
            contacts = [c for c in self.sc.contacts if c.cls in (HOSTILE, UNKNOWN)
                        and c.window is not None and c.id not in self.matrix.banned
                        and c.id not in state.served]
            contacts.sort(key=lambda c: (c.cls != UNKNOWN, -self.matrix.priority(c), c.id))
            for contact in contacts:
                self.checker.check_time()
                local_stop = min(self.checker.stop, time.monotonic() + 0.25)
                best = None
                try:
                    for child, decision in self.children(state, contact, local_stop, 4, True):
                        if best is None or child.score > best[0].score:
                            best = (child, decision)
                except DeadlineReached:
                    self.checker.check_time()
                if best is not None:
                    depth += 1
                    state = self.record(best[0], state, {**best[1], "kind": "repair"}, depth)
                    improved = True
        return state

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
            pass_start = time.monotonic()
            beam = [self.root]
            for depth, contact in enumerate(order, 1):
                self.checker.check_time()
                next_beam = []
                for parent in beam:
                    self.checker.check_time()
                    skipped = self.record(parent, parent, {"kind": "skip", "contact": contact.id}, depth)
                    next_beam.append(skipped)
                    remaining = max(1, (len(order)-depth+1)*len(beam))
                    allowance = min(0.12, max(0.005, (self.checker.stop-time.monotonic()) / (remaining*1.5)))
                    local_stop = min(self.checker.stop, time.monotonic()+allowance)
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
            if self.interleave and self.lns_share < 1:
                burst = (time.monotonic() - pass_start) * self.lns_share / (1 - self.lns_share)
                self.improve(contacts, priorities, time.monotonic() + burst)
            # Bound trace size for long runs on tiny or impossible scenarios.
            if len(self.trace) >= 20000:
                return False
        return True

    def run(self):
        contacts, priorities = self.search_phase()
        self.improve_phase(contacts, priorities)
        return self.finalize()

    def search_phase(self):
        """Warm start plus beam passes until the beam deadline.
        Returns the searchable contacts and their priorities."""
        try:
            self.warm_start()
        except DeadlineReached:
            pass
        self.matrix = CandidateMatrix(self.sc)
        contacts = [c for c in self.sc.contacts if c.cls in (HOSTILE, UNKNOWN)
                    and c.window is not None and c.id not in self.matrix.banned]
        priorities = {c.id: self.matrix.priority(c) for c in contacts}
        rng = random.Random(self.sc.seed)
        self.checker.stop = min(self.beam_stop, self.stop)
        try:
            self.search(contacts, priorities, rng)
        except DeadlineReached:
            pass
        self.checker.stop = self.stop
        return contacts, priorities

    def improve_phase(self, contacts, priorities):
        """Greedy repair of the best beam plan, then LNS until the deadline."""
        # Greedy repair of the best plan gets at most a quarter of the LNS share.
        self.checker.stop = self.beam_stop + 0.25 * max(0.0, self.stop - self.beam_stop)
        try:
            if contacts:
                best = self.archive[0]
                depth = max((n["depth"] for n in self.trace if n["id"] == best.node_id), default=0)
                self.repair(best, depth)
        except DeadlineReached:
            pass
        self.checker.stop = self.stop
        try:
            self.improve(contacts, priorities)
        except DeadlineReached:
            pass

    def finalize(self):
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

    def remove(self, state, cid):
        """State without contact cid, or None if no valid removal was found.

        Drops the strike team and the scout task, rebuilding the tour from its
        original departure so earlier visits keep their times and later ones can
        only move earlier. A hostile whose rebuilt tour fails keeps its harmless
        observation and loses only the strike.
        """
        team = {aid for aid, route in state.parsed.items()
                if any(w.action == "engage" and w.target == cid for w in route.waypoints)}
        attempts = []
        for sid, tasks in state.tasks.items():
            if not any(t["cid"] == cid for t in tasks):
                continue
            rest = [t for t in tasks if t["cid"] != cid]
            if not rest:
                attempts.append((team | {sid}, {}, {**state.tasks, sid: rest}))
                break
            route = state.routes.get(sid)
            if route is None:
                break
            for depart in (route[0]["t"], None):
                tour = scout_tour(self.sc, self.aircraft[sid], rest, route[1]["z"], depart=depart)
                if tour is not None:
                    attempts.append((team, {sid: tour}, {**state.tasks, sid: rest}))
            break
        if self.contacts[cid].cls == HOSTILE and team:
            attempts.append((team, {}, state.tasks))
        for removed, extra, tasks in attempts:
            candidate = self.checker.extend(self.checker.without(state, removed), extra)
            if candidate is not None and cid not in candidate.served:
                candidate.tasks = tasks
                return candidate
        return None

    def improve(self, contacts, priorities, stop=None):
        """Large neighbourhood search: remove a few served contacts, then reinsert.

        Destroy picks random, spatially related, low-value or same-tour served
        contacts (only the first two unless adaptive). Repair tries the removed
        contacts, their nearest unserved neighbours and a few random unserved ones,
        inserting each scout visit at its cheapest tour positions. Worse plans are
        sometimes accepted (annealing); accepted plans enter the archive.
        State persists across calls, so interleaved bursts continue one search.
        """
        if not contacts:
            return
        stop = self.stop if stop is None else min(stop, self.stop)
        st = self.lns_state
        if st is None:
            st = self.lns_state = {
                "by_id": {c.id: c for c in contacts}, "best": self.archive[0],
                "current": self.archive[0], "begin": time.monotonic(),
                "temperature": max(1.0, 0.01 * abs(self.archive[0].score)),
                "stale": 0, "since_best": 0}
        elif self.archive[0].score > st["best"].score + 1e-9:
            st["best"] = st["current"] = self.archive[0]
            st["stale"] = st["since_best"] = 0
        saved, self.checker.stop = self.checker.stop, stop
        try:
            # Small scenarios converge quickly; stopping early lowers the runtime tie-break.
            while st["since_best"] < 400:
                self.checker.check_time()
                self.lns_step(st, contacts, priorities)
        except DeadlineReached:
            if time.monotonic() >= self.stop:
                raise
        finally:
            self.checker.stop = saved

    def destroy(self, op, served, by_id, current, priorities, rng):
        k = rng.randint(1, min(4, len(served)))
        if op == "related":
            seed = by_id[rng.choice(served)]
            return sorted(served, key=lambda c: hdist(by_id[c].pos, seed.pos))[:k]
        if op == "worst":
            return sorted(served, key=lambda c: priorities[c] * rng.uniform(0.8, 1.2))[:k]
        if op == "tour":
            tours = [[t["cid"] for t in tasks if t["cid"] in current.served and t["cid"] in by_id]
                     for tasks in current.tasks.values()]
            tours = [t for t in tours if t]
            if tours:
                tour = rng.choice(tours)
                return rng.sample(tour, min(k, len(tour)))
        return rng.sample(served, k)

    def lns_step(self, st, contacts, priorities):
        rng, by_id = self.lns_rng, st["by_id"]
        current, best = st["current"], st["best"]
        self.lns["iterations"] += 1
        served = sorted(c for c in current.served if c in by_id)
        if self.adaptive:
            ops = list(self.weights)
            op = rng.choices(ops, weights=[self.weights[o] for o in ops])[0]
        else:
            op = "related" if rng.random() < 0.5 else "random"
        removed = []
        partial = current
        if served:
            for cid in self.destroy(op, served, by_id, current, priorities, rng):
                smaller = self.remove(partial, cid)
                if smaller is not None:
                    partial = smaller
                    removed.append(cid)
        open_ = [c for c in contacts if c.id not in partial.served and c.id not in removed]
        centre = [by_id[c].pos for c in removed] or [rng.choice(contacts).pos]
        near = sorted(open_, key=lambda c: min(hdist(c.pos, q) for q in centre))[:6]
        extra = rng.sample(open_, min(3, len(open_)))
        pool = list({c.id: c for c in near + extra}.values())
        noise = {c.id: rng.uniform(0.6, 1.4) for c in pool}
        order = (sorted(pool, key=lambda c: -priorities[c.id] * noise[c.id])
                 + [by_id[c] for c in removed])
        for contact in order:
            self.checker.check_time()
            local_stop = min(self.checker.stop, time.monotonic() + 0.08)
            chosen_child = None
            try:
                for child, _ in self.children(partial, contact, local_stop, insert=True):
                    if chosen_child is None or child.score > chosen_child.score:
                        chosen_child = child
            except DeadlineReached:
                self.checker.check_time()
            if chosen_child is not None:
                partial = chosen_child
        delta = partial.score - current.score
        span = max(1e-6, self.stop - st["begin"])
        heat = st["temperature"] * max(0.0, self.stop - time.monotonic()) / span
        reward = 0.0
        if delta > 1e-9 or (removed and heat > 0 and rng.random() < math.exp(delta / heat)):
            self.lns["accepted"] += 1
            reward = 1.0 if delta > 1e-9 else 0.3
            current = st["current"] = self.record(partial, current, {"kind": "lns", "op": op,
                                                                     "removed": removed},
                                                  len(partial.served))
            if current.score > best.score + 1e-9:
                self.lns["improvements"] += 1
                self.lns["gain"] += current.score - best.score
                st["best"], st["stale"], st["since_best"] = current, 0, 0
                reward = 3.0
        if self.adaptive:
            self.weights[op] = max(0.05, 0.8 * self.weights[op] + 0.2 * reward)
        if reward == 3.0:
            return
        st["stale"] += 1
        st["since_best"] += 1
        if st["stale"] >= 25:
            st["stale"] = 0
            st["current"] = rng.choice(self.archive) if self.restarts else st["best"]

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
                "beam_width": self.width, "requested_top_n": self.top_n,
                "lns": {**self.lns, "variant": {"interleave": self.interleave,
                                                "adaptive": self.adaptive,
                                                "restarts": self.restarts},
                        **({"weights": dict(self.weights)} if self.adaptive else {})},
                "unserved": diagnostics["unserved"],
                "unserved_points": diagnostics["unserved_points"],
                "contacts": diagnostics["contacts"],
                "solutions": [{"rank": i, "node": state.node_id, "score": result.score,
                               "served": result.served} for i, (state, result) in enumerate(verified, 1)],
                "nodes": self.trace}
