"""Beam search: warm start, bounded child generation and greedy repair.
"""
from __future__ import annotations

from dataclasses import replace
import itertools
import time

from fleetkit.baseline import _Planner
from fleetkit.geometry import hdist
from fleetkit.model import EFFECTOR, HOSTILE, UNKNOWN
from config import CONFIG
from deadline import DeadlineReached
from diagnostics import STAGES, outcome_stage
from effector_teams import strike_deadlines
from route_geometry import takeoff_shift
from scout_tours import (SCOUT_LAYERS, _depart, eco_speed, fast_speed, insertion_positions, obs_point,
                         scout_reachable, scout_tour, scout_wh_per_m, tour_fits)


def diverse_beam(ranked, width, diverse=None, extra=None):
    """Best states by score plus up to `diverse` lower-ranked states whose consumed aircraft
    (used set), then served set, is not yet represented in the kept beam."""
    diverse = CONFIG.beam_diverse if diverse is None else diverse
    extra = CONFIG.beam_diverse_extra if extra is None else extra
    top = width if extra else max(1, width - diverse)
    beam, rest = ranked[:top], ranked[top:]
    used = {state.used for state in beam}
    served = {state.served for state in beam}
    slots = diverse if extra else width - top
    picked, ids = [], set()
    for novel in (lambda st: st.used not in used, lambda st: st.served not in served):
        for state in rest:
            if len(picked) >= slots:
                break
            if id(state) in ids or not novel(state):
                continue
            picked.append(state)
            ids.add(id(state))
            used.add(state.used)
            served.add(state.served)
    if not extra:
        # Unfilled diverse slots go back to the next states by score.
        picked += [st for st in rest if id(st) not in ids][:slots - len(picked)]
    picked.sort(key=lambda st: -st.score)
    return beam + picked



# Scout insertion moves: (search, parent, contact, insert) -> lazy (scout, tasks, route)
# options, or None to skip. _children tries them in order and stops at the first round
# that yields a child.
def nearest_scouts(search, parent, contact, insert):
    """The SCOUT_FANOUT best-ranked scouts, appending (or inserting when insert)."""
    return search.scout_options(parent, contact, insert)


def nearest_scouts_reordered(search, parent, contact, insert):
    """Append mode only: the same scouts with insertion and reordering."""
    return None if insert else search.scout_options(parent, contact, True, skip_append=True)


def fallback_scouts(search, parent, contact, insert):
    """The next SCOUT_FALLBACK - SCOUT_FANOUT scouts, with insertion."""
    return search.scout_options(parent, contact, True,
                                slice(CONFIG.scout_fanout, CONFIG.scout_fallback))


SCOUT_MOVES = (nearest_scouts, nearest_scouts_reordered, fallback_scouts)

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


class BeamMixin:
    """Search methods; mixed into search_planner.Search."""

    def warm_start(self):
        stop = min(self.stop, self.start + min(10.0, max(0.0, (self.stop-self.start)*0.25)))
        p = WarmPlanner(self.sc, self.checker, self.root, stop)
        # Contacts without a window can never be served (R9); the kit's greedy assumes one.
        timed = [c for c in self.sc.contacts if c.window is not None]
        contacts = (sorted((c for c in timed if c.cls == HOSTILE), key=lambda c: -c.points)
                    + sorted((c for c in timed if c.cls == UNKNOWN), key=lambda c: -c.points))
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

    def ranked_scouts(self, parent, contact, insert):
        """Scouts by distance from their tour (insert) or tour end (append) to the contact.

        If the first SCOUT_FANOUT are all busy, the nearest idle scout takes the last slot:
        all scouts may share a base, so an idle one is often the only one that fits.
        """
        scouts = _Planner(self.sc)._scout_candidates(contact, limit=len(parent.tasks))
        if CONFIG.time_rank:
            return self._time_ranked(parent, contact, insert, scouts)
        if insert:
            def reach(a):
                points = [a.home] + [t["point"] for t in parent.tasks[a.id]]
                return min(hdist(q, contact.pos) for q in points)
            scouts.sort(key=reach)
        else:
            scouts.sort(key=lambda a: hdist(parent.tasks[a.id][-1]["point"] if parent.tasks[a.id]
                                           else a.home, contact.pos))
        head = scouts[:CONFIG.scout_fanout]
        if head and all(parent.tasks[a.id] for a in head):
            idle = next((a for a in scouts[CONFIG.scout_fanout:] if not parent.tasks[a.id]), None)
            if idle is not None:
                scouts = head[:-1] + [idle] + [a for a in scouts[CONFIG.scout_fanout - 1:] if a is not idle]
        return scouts

    def _time_ranked(self, parent, contact, insert, scouts):
        """Scouts with a visit order that fits (tour_fits at the lowest layer), cheapest
        added distance first; one idle scout that fits among the first SCOUT_FANOUT."""
        deadlines = strike_deadlines(self.sc, self.aircraft, parent.routes)
        z = min(SCOUT_LAYERS)
        ranked = []
        for scout in scouts:
            current = parent.tasks[scout.id]
            cids = tuple(t["cid"] for t in current)
            points = [scout.home[:2]] + [t["point"] for t in current] + [scout.home[:2]]
            best = None
            for i in (range(len(current) + 1) if insert else [len(current)]):
                order = cids[:i] + (contact.id,) + cids[i:]
                if not tour_fits(self.sc, scout, current, contact, order, z, deadlines):
                    continue
                added = (hdist(points[i], contact.pos) + hdist(contact.pos, points[i + 1])
                         - hdist(points[i], points[i + 1]))
                best = added if best is None else min(best, added)
            if best is not None:
                if CONFIG.scout_energy_rank:
                    best *= scout_wh_per_m(scout)
                ranked.append((best, scout.id, scout))
        ranked.sort(key=lambda r: r[:2])
        out = [r[2] for r in ranked]
        head = out[:CONFIG.scout_fanout]
        if head and all(parent.tasks[a.id] for a in head):
            idle = next((a for a in out[CONFIG.scout_fanout:] if not parent.tasks[a.id]), None)
            if idle is not None:
                out = head[:-1] + [idle] + [a for a in out[CONFIG.scout_fanout - 1:] if a is not idle]
        return out

    @staticmethod
    def tour_orders(scout, current, contact, insert, skip_append=False):
        """Visit orders (contact ids) to try, with whether earlier visits keep the parent's order.

        Append mode: the new visit goes last. Insert mode: the cheapest insertion positions
        (every position for short tours), then deadline-ordered and start-ordered tours.
        """
        cids = tuple(t["cid"] for t in current)
        orders, seen = [], set()

        def add(order, keeps_prefix):
            if order not in seen:
                seen.add(order)
                orders.append((order, keeps_prefix))

        if not insert:
            add(cids + (contact.id,), True)
            return orders
        if skip_append:
            seen.add(cids + (contact.id,))
        k = len(current) + 1 if len(current) <= CONFIG.full_insert_tasks else 3
        for pos in insertion_positions(scout.home, current, contact.pos, k):
            add(cids[:pos] + (contact.id,) + cids[pos:], True)
        if 1 <= len(current) <= CONFIG.reorder_tasks:
            windows = {t["cid"]: tuple(t["window"]) for t in current}
            windows[contact.id] = tuple(contact.window)
            add(tuple(sorted(windows, key=lambda c: (windows[c][1], windows[c][0], c))), False)
            add(tuple(sorted(windows, key=lambda c: (windows[c][0], windows[c][1], c))), False)
        return orders

    def scout_options(self, parent, contact, insert, ranks=None,
                      skip_append=False):
        """(scout, tasks, route or None) for each scout, visit order and flight setting.

        Orders that cannot reach the new visit in its window, or get home before the
        horizon, yield None without building a route.
        """
        ranks = slice(0, CONFIG.scout_fanout) if ranks is None else ranks
        deadlines = strike_deadlines(self.sc, self.aircraft, parent.routes) if CONFIG.time_rank else {}
        for scout in self.ranked_scouts(parent, contact, insert)[ranks]:
            current = parent.tasks[scout.id]
            by_id = {t["cid"]: t for t in current}
            prev_route = parent.routes.get(scout.id)
            for order, keeps_prefix in self.tour_orders(scout, current, contact, insert, skip_append):
                pos = order.index(contact.id)
                fits = {z: not CONFIG.time_rank or tour_fits(self.sc, scout, current, contact, order, z,
                                                      deadlines) for z in SCOUT_LAYERS}
                for z, obs_angle, shift in itertools.product(SCOUT_LAYERS, (0.0, 60.0, -60.0),
                                                             (0.0, 7.0)):
                    if not fits[z]:
                        yield scout, None, None
                        continue
                    task = {"cid": contact.id,
                            "point": obs_point(self.sc, scout, contact, obs_angle, z),
                            "window": contact.window, "cpos": contact.pos}
                    tasks = [task if cid == contact.id else by_id[cid] for cid in order]
                    if CONFIG.pad_takeoffs:
                        # Delay takeoff past neighbours on nearby pads (most scout R8 conflicts).
                        speed = eco_speed(scout) if CONFIG.scout_speed_mode != "fast" else fast_speed(scout)
                        shift += takeoff_shift(self.sc, self.aircraft, parent.routes, scout,
                                               _depart(scout, tasks, z, speed, 0.0))
                    if not scout_reachable(self.sc, scout, tasks, pos, z,
                                           prev_route if keeps_prefix else None):
                        yield scout, tasks, None
                        continue
                    yield scout, tasks, scout_tour(self.sc, scout, tasks, z, shift)

    def _children(self, parent, contact, stop, limit, stats, progress, insert=False):
        """Bounded candidate generation: alternate scouts, teams and flight settings.

        Rounds stop at the first one that yields a child: the nearest scouts as asked
        (append or insert), then (append mode) the same scouts with insertion and
        reordering, then the next SCOUT_FALLBACK - SCOUT_FANOUT scouts with insertion.
        """
        groups = self.matrix.groups(contact, parent.used) if contact.cls == HOSTILE else [()]
        if not groups:
            self._progress(stats, progress, "no_team")
            return
        rounds = [r for r in (move(self, parent, contact, insert) for move in SCOUT_MOVES)
                  if r is not None]
        state = {"count": 0, "seen": set()}
        if CONFIG.time_rank and not any(self.ranked_scouts(parent, contact, True)):
            # No scout has any visit order that fits, at any tour position.
            self._progress(stats, progress, "scout_window")
            return
        for options in rounds:
            for item in self._serve(parent, contact, options, groups, stop, limit, stats,
                                    progress, state):
                yield item
                if state["count"] >= limit:
                    return
            if state["count"]:
                return

    def _serve(self, parent, contact, scout_options, groups, stop, limit, stats, progress, state):
        seen = state["seen"]
        # Round-robin teams before spending time on fine timing adjustments.
        for scout, tasks, route in scout_options:
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
                state["count"] += 1
                yield candidate, decision
                if state["count"] >= limit:
                    return

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

    def bound_rank(self, order):
        """Beam key: score plus CONFIG.beam_bound times the share of effectors still free
        times the points of the contacts after the current depth (an optimistic remainder)."""
        weight = CONFIG.beam_bound
        if not weight:
            return lambda state, depth: state.score
        effectors = [a.id for a in self.aircraft.values() if a.type == EFFECTOR]
        if not effectors:
            return lambda state, depth: state.score
        after = [0.0] * (len(order) + 1)
        for i in range(len(order) - 1, -1, -1):
            after[i] = after[i + 1] + max(0.0, order[i].points)
        cache = {}

        def rank(state, depth):
            free = cache.get(state.used)
            if free is None:
                free = cache[state.used] = sum(1 for a in effectors if a not in state.used) / len(effectors)
            return state.score + weight * free * after[depth]
        return rank

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
            rank = self.bound_rank(order)
            beam = [self.root]
            for depth, contact in enumerate(order, 1):
                self.checker.check_time()
                next_beam, served = [], []
                for parent in beam:
                    self.checker.check_time()
                    skipped = self.record(parent, parent, {"kind": "skip", "contact": contact.id}, depth)
                    next_beam.append(skipped)
                    remaining = max(1, (len(order)-depth+1)*len(beam))
                    allowance = min(0.12, max(0.005, (self.checker.stop-time.monotonic()) / (remaining*1.5)))
                    local_stop = min(self.checker.stop, time.monotonic()+allowance)
                    try:
                        for child, decision in self.children(parent, contact, local_stop):
                            served.append(self.record(child, parent, decision, depth))
                            next_beam.append(served[-1])
                    except DeadlineReached:
                        self.checker.check_time()  # Local cap expires without ending search.
                unique = {}
                for state in sorted(next_beam, key=lambda s: -rank(s, depth)):
                    unique.setdefault(state.signature(), state)
                width = self.width
                if self.widen and self.passes >= 4:
                    width = min(100, self.width + self.width // 2)
                beam = diverse_beam(list(unique.values()), width)
                kept = {state.signature() for state in beam}
                # Valid serving children cut by the beam width ("outranked" in the beam).
                self.stats(contact.id)["beam_pruned"] += sum(
                    1 for state in served if state.signature() not in kept)
            self.passes += 1
            if self.interleave and self.lns_share < 1:
                burst = (time.monotonic() - pass_start) * self.lns_share / (1 - self.lns_share)
                self.improve(contacts, priorities, time.monotonic() + burst)
            # Bound trace size for long runs on tiny or impossible scenarios.
            if len(self.trace) >= 20000:
                return False
        return True
