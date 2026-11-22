"""Large neighbourhood search: pluggable destroy operators, repair and LNS moves.
"""
from __future__ import annotations

import itertools
import math
import time

from fleetkit.geometry import hdist
from fleetkit.model import HOSTILE, SCOUT, UNKNOWN
from config import CONFIG
from deadline import DeadlineReached
from scout_tours import SCOUT_LAYERS, scout_tour, trip_labels


DESTROY_OPERATORS = ("random", "related", "worst", "tour", "trip", "group", "strike")
# Moves that rebuild part of the plan themselves: Search.<op>_move(current, rng) returns
# (partial, removed contact ids) or None.
MOVE_OPERATORS = ("trip", "group", "strike")


# Destroy operators: (search, served, by_id, current, priorities, rng, k) -> contact ids to remove.
def destroy_random(search, served, by_id, current, priorities, rng, k):
    return rng.sample(served, k)


def destroy_related(search, served, by_id, current, priorities, rng, k):
    seed = by_id[rng.choice(served)]
    return sorted(served, key=lambda c: hdist(by_id[c].pos, seed.pos))[:k]


def destroy_worst(search, served, by_id, current, priorities, rng, k):
    return sorted(served, key=lambda c: priorities[c] * rng.uniform(0.8, 1.2))[:k]


def destroy_tour(search, served, by_id, current, priorities, rng, k):
    tours = [[t["cid"] for t in tasks if t["cid"] in current.served and t["cid"] in by_id]
             for tasks in current.tasks.values()]
    tours = [t for t in tours if t]
    if not tours:
        return destroy_random(search, served, by_id, current, priorities, rng, k)
    tour = rng.choice(tours)
    return rng.sample(tour, min(k, len(tour)))


DESTROY = {"random": destroy_random, "related": destroy_related, "worst": destroy_worst,
           "tour": destroy_tour, "trip": destroy_tour}


def best_disjoint(lists, floor=-math.inf, nodes=None):
    """Best combination of one trip per list with pairwise disjoint contact masks.

    lists: per scout [(value, mask)] sorted by value, best first (include (0, 0) to allow
    an empty trip). Depth-first over scouts in order of their best trip, pruned when the
    partial value plus every remaining scout's best trip cannot beat the best so far
    (initially `floor`). Returns (value, [index per list]) or None if nothing beats floor.
    """
    nodes = CONFIG.group_nodes if nodes is None else nodes
    n = len(lists)
    if n == 0 or any(not lst for lst in lists):
        return None
    order = sorted(range(n), key=lambda i: -lists[i][0][0])
    rest = [0.0] * (n + 1)
    for pos in range(n - 1, -1, -1):
        rest[pos] = rest[pos + 1] + max(0.0, lists[order[pos]][0][0])
    best = [floor, None]
    picks = [0] * n
    count = [0]

    def go(pos, value, mask):
        if pos == n:
            if value > best[0] + 1e-9:
                best[0], best[1] = value, list(picks)
            return
        for k, (v, m) in enumerate(lists[order[pos]]):
            count[0] += 1
            if value + v + rest[pos + 1] <= best[0] + 1e-9 or count[0] > nodes:
                return
            if m & mask == 0:
                picks[order[pos]] = k
                go(pos + 1, value + v, mask | m)

    go(0, 0.0, 0)
    return None if best[1] is None else (best[0], best[1])


def default_lns_share(scenario):
    n = sum(c.cls in (HOSTILE, UNKNOWN) for c in scenario.contacts)
    return CONFIG.lns_share_large if n >= CONFIG.lns_large_contacts else CONFIG.lns_share


class LNSMixin:
    """Search methods; mixed into search_planner.Search."""

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

    def trip_move(self, current, rng):
        """Destroy and rebuild one scout's whole tour from its best label-search trip.
        Returns (partial state, contact ids removed) or None when nothing changes."""
        scout = self.trip_scout(current, rng)
        if scout is None:
            return None
        sid = scout.id
        partial, removed = self.strip_scout(current, sid)
        if partial is None:
            return None
        others = {t["cid"] for s, tasks in current.tasks.items() if s != sid for t in tasks}
        route = current.routes.get(sid)
        z = max(w["z"] for w in route) if route else SCOUT_LAYERS[0]
        start = time.monotonic()
        trips = self.scout_trips(partial, scout, partial.used, others | set(partial.served), z)
        self.lns["trip_label_s"] += time.monotonic() - start
        trips = [tr for tr in trips if set(tr[1]) != removed]
        if not trips:
            return None
        pick = 0 if rng.random() < 0.6 or len(trips) == 1 else rng.randrange(1, min(5, len(trips)))
        stop = min(self.checker.stop, time.monotonic() + 0.5)
        partial, _ = self.realise_trip(partial, scout, trips[pick][1], z, stop)
        return partial, sorted(removed)

    def strike_move(self, current, rng):
        """Jointly re-plan a blocked hostile and one to three R8-conflicting strikes.

        Removing an effector route rather than its contact is important: the scout
        observation remains a valid, reusable confirmation while alternative teams
        and timings are explored.  The incremental checker then rechecks R4/R10 and
        every changed-route separation pair on each re-strike.
        """
        open_ = [c for c in self.sc.contacts if c.cls == HOSTILE and c.window is not None
                 and c.id not in current.served and c.id not in self.matrix.banned
                 and self.matrix.priority(c) > 0]
        if not open_:
            return None
        side = ("strike_rule:", "strike_route", "time_cap", "not_improving", "outranked")
        preferred = [c for c in open_ if any(k.startswith(side) for k in
                     self.attempts.get(c.id, {}).get("outcomes", {}))]
        target = rng.choice(preferred or open_)
        observed = self.observed_strike(current, target.id)
        stop = min(self.checker.stop, time.monotonic() + CONFIG.strike_move_s)
        # Most unserved contacts do not retain an observation: ordinary children are
        # atomic.  Build one candidate scout visit solely to obtain the prospective
        # strike trajectories for conflict discovery; after removal, children() does
        # the normal fully-validated scout-and-strike insertion.
        needs_observation = observed is None
        if observed is None:
            try:
                scout, _, scout_route = next((x for x in self.scout_options(current, target, True)
                                               if x[2] is not None), (None, None, None))
            except DeadlineReached:
                return None
            if scout_route is None:
                return None
            obs = [w["t"] + w.get("dwell_s", 0.0) for w in scout_route
                   if w.get("action") == "observe" and w.get("target") == target.id]
            if not obs:
                return None
            obs_end = max(obs)
        else:
            scout, scout_route, obs_end = observed
        conflicts = set()
        # Use all currently installed effectors as prospective candidates.  The
        # conflict-derived removals below make their actual availability explicit.
        for _, routes in self.strike_route_options(target, self.matrix.groups(target, set(), 3),
                                                    scout, scout_route, obs_end, stop):
            conflicts.update(self.strike_conflicts(current, routes))
            if len(conflicts) >= 3:
                break
        displaced = set()
        for aid in conflicts:
            route = current.parsed.get(aid)
            if route is not None:
                displaced.update(w.target for w in route.waypoints if w.action == "engage")
        displaced.discard(target.id)
        if not displaced:
            return None
        sizes = [n for n in CONFIG.strike_remove_sizes if n <= len(displaced)]
        if not sizes:
            return None
        count = rng.choice(sizes)
        # Prefer low-value displaced strikes when the scarce effector pool cannot
        # support every member of the group.
        removed = sorted(displaced, key=lambda cid: self.matrix.priority(self.contacts[cid]))[:count]
        partial = current
        for cid in removed:
            partial = self.drop_strike(partial, cid)
            if partial is None:
                return None
        if needs_observation:
            try:
                created = max((state for state, _ in self.children(partial, target, stop, 2, True)),
                              key=lambda s: s.score, default=None)
            except DeadlineReached:
                return None
            if created is None:
                return None
            partial = created
        group = [target.id] + removed
        orders = (group, removed + [target.id],
                  sorted(group, key=lambda cid: -self.matrix.priority(self.contacts[cid])))
        best = None
        for order in orders:
            trial = partial
            for cid in order:
                self.checker.check_time(stop)
                got = self.observed_strike(trial, cid)
                if got is None:
                    continue
                a, route, end = got
                children = self.strikes(trial, self.contacts[cid],
                                        self.matrix.groups(self.contacts[cid], trial.used),
                                        a, route, end, stop)
                child = max((state for state, _ in children), key=lambda s: s.score, default=None)
                if child is not None:
                    trial = child
            if best is None or trial.score > best.score:
                best = trial
        return best, group

    def group_scouts(self, state, n, rng):
        """n scouts for the group move: one picked like the trip move, then neighbours
        weighted by how close their tours (or bases) come to the chosen ones."""
        seed = self.trip_scout(state, rng)
        if seed is None:
            return []
        scouts = [a for a in self.sc.aircraft if a.type == SCOUT]

        def points(a):
            return [a.home[:2]] + [t["point"] for t in state.tasks.get(a.id, ())]
        chosen = [seed]
        near = points(seed)
        while len(chosen) < n:
            rest = [a for a in scouts if a not in chosen]
            if not rest:
                break
            weights = []
            for a in rest:
                d = min(hdist(p, q) for p in points(a) for q in near)
                weights.append(1.0 / (1.0 + d / 1000.0) ** 2)
            pick = rng.choices(rest, weights=weights)[0]
            chosen.append(pick)
            near += points(pick)
        return chosen

    def group_move(self, current, rng):
        """Jointly re-plan n scouts (n from GROUP_SIZES) from the best combination of their
        label-search trips over a shared contact pool. Returns (partial state, contact ids
        removed) or None when the estimate finds nothing better than the current trips."""
        n = rng.choice(CONFIG.group_sizes)
        key = str(n)
        self.lns["group_sizes"][key] = self.lns["group_sizes"].get(key, 0) + 1
        scouts = self.group_scouts(current, n, rng)
        if len(scouts) < 2:
            return None
        sids = [a.id for a in scouts]
        mine = {t["cid"] for sid in sids for t in current.tasks.get(sid, ())}
        others = {t["cid"] for s, tasks in current.tasks.items() if s not in sids for t in tasks}
        near = [p for a in scouts for p in [a.home[:2]] + [t["point"] for t in current.tasks.get(a.id, ())]]
        open_ = []
        for c in self.sc.contacts:
            if (c.cls in (HOSTILE, UNKNOWN) and c.window is not None and c.id not in current.served
                    and c.id not in others and c.id not in self.matrix.banned
                    and self.matrix.priority(c) > 0):
                d = min(hdist(c.pos, p) for p in near)
                if d < CONFIG.group_radius_m:
                    open_.append((d, c.id))
        pool = set(mine) | {cid for _, cid in sorted(open_)[:max(0, CONFIG.group_pool - len(mine))]}
        partial, removed = self.strip_scouts(current, sids)
        if partial is None:
            return None
        exclude = {c.id for c in self.sc.contacts if c.id not in pool} | set(partial.served)
        start = time.monotonic()
        bit = {cid: j for j, cid in enumerate(sorted(pool))}
        lists, layers, cur = [], [], 0.0
        for a in scouts:
            route = current.routes.get(a.id)
            z = max(w["z"] for w in route) if route else SCOUT_LAYERS[0]
            cands = self.trip_candidates(a, partial.used, exclude, z)
            order = tuple(t["cid"] for t in current.tasks.get(a.id, ()))
            trips = trip_labels(self.sc, a, cands, z, top=CONFIG.group_trips, orders=(order,), cover=True)
            have = [tr[0] for tr in trips if set(tr[1]) == set(order)]
            cur += max(have) if have and order else 0.0
            entries = [(tr[0], sum(1 << bit[c] for c in tr[1]), tr) for tr in trips if tr[0] > 0]
            entries.append((0.0, 0, None))
            entries.sort(key=lambda e: -e[0])
            lists.append(entries)
            layers.append(z)
        got = best_disjoint([[(v, m) for v, m, _ in lst] for lst in lists], cur)
        self.lns["group_label_s"] += time.monotonic() - start
        if got is None:
            return None
        picked = [lists[i][k][2] for i, k in enumerate(got[1])]
        new = {a.id: set(tr[1]) if tr else set() for a, tr in zip(scouts, picked)}
        if all(new[a.id] == {t["cid"] for t in current.tasks.get(a.id, ())} for a in scouts):
            return None
        visits = sorted(((f, a, cid, z) for a, tr, z in zip(scouts, picked, layers) if tr
                         for cid, f in zip(tr[1], tr[3])), key=lambda v: (v[0], v[2]))
        stop = min(self.checker.stop, time.monotonic() + CONFIG.group_move_s)
        partial, _ = self.realise_visits(partial, [(a, cid, z) for _, a, cid, z in visits], stop)
        self.lns["group_realised"] += 1
        return partial, sorted(removed)

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
        return DESTROY.get(op, destroy_random)(self, served, by_id, current, priorities, rng, k)

    def insert_best(self, partial, contact):
        """Best child inserting contact (0.08 s cap), or partial itself."""
        local_stop = min(self.checker.stop, time.monotonic() + 0.08)
        chosen = partial
        try:
            for child, _ in self.children(partial, contact, local_stop, insert=True):
                if chosen is partial or child.score > chosen.score:
                    chosen = child
        except DeadlineReached:
            self.checker.check_time()
        return chosen

    def exact_reinsert(self, base, subset, greedy, rng):
        """Best plan over insertion orders of the few contacts a repair placed or removed,
        sharing order prefixes, until EXACT_ORDERS orders or EXACT_REINSERT_S seconds."""
        self.lns["exact_steps"] += 1
        orders = list(itertools.permutations(subset))
        rng.shuffle(orders)
        best, cache = greedy, {(): base}
        saved = self.checker.stop
        self.checker.stop = min(saved, time.monotonic() + CONFIG.exact_reinsert_s)
        try:
            for order in orders[:CONFIG.exact_orders]:
                ids = tuple(c.id for c in order)
                for i in range(1, len(ids) + 1):
                    if ids[:i] not in cache:
                        cache[ids[:i]] = self.insert_best(cache[ids[:i - 1]], order[i - 1])
                self.lns["exact_orders"] += 1
                if cache[ids].score > best.score + 1e-9:
                    best = cache[ids]
        except DeadlineReached:
            pass
        finally:
            self.checker.stop = saved
        if best is not greedy:
            self.lns["exact_wins"] += 1
            self.lns["exact_gain"] += best.score - greedy.score
        self.checker.check_time()
        return best

    def lns_step(self, st, contacts, priorities):
        rng, by_id = self.lns_rng, st["by_id"]
        current, best = st["current"], st["best"]
        self.lns["iterations"] += 1
        served = sorted(c for c in current.served if c in by_id)
        roll = None if self.adaptive else rng.random()
        if self.adaptive:
            ops = [o for o in self.weights if (CONFIG.trip_search or o != "trip")
                   and (CONFIG.group_sizes or o != "group") and (CONFIG.strike_group or o != "strike")]
            op = rng.choices(ops, weights=[self.weights[o] for o in ops])[0]
        elif CONFIG.trip_search and roll < CONFIG.trip_share:
            op = "trip"
        elif CONFIG.group_sizes and roll < CONFIG.trip_share * bool(CONFIG.trip_search) + CONFIG.group_share:
            op = "group"
        elif roll < CONFIG.trip_share * bool(CONFIG.trip_search) + CONFIG.group_share * bool(CONFIG.group_sizes) + CONFIG.tour_share:
            op = "tour"
        elif CONFIG.strike_group and roll < (CONFIG.trip_share * bool(CONFIG.trip_search) + CONFIG.group_share * bool(CONFIG.group_sizes)
                                      + CONFIG.tour_share + CONFIG.strike_share):
            op = "strike"
        else:
            op = "related" if rng.random() < 0.5 else "random"
        removed, trip_centre = [], []
        partial = current
        rebuild = False
        if op == "tour" and rng.random() < CONFIG.tour_rebuild:
            sids = [sid for sid, tasks in current.tasks.items()
                    if any(t["cid"] in current.served and t["cid"] in by_id for t in tasks)]
            stripped = self.strip_scout(current, rng.choice(sids)) if sids else (None, ())
            if stripped[0] is not None:
                rebuild, partial = True, stripped[0]
                removed = sorted(c for c in stripped[1] if c in by_id and c not in partial.served)
        if op in MOVE_OPERATORS:
            self.lns[op + "_tried"] += 1
            try:
                moved = getattr(self, op + "_move")(current, rng)
            except DeadlineReached:
                # A move's own time cap ended it; only the LNS deadline ends the phase.
                self.checker.check_time()
                moved = None
            if moved is None:
                op = "related"
            else:
                partial, stripped = moved
                trip_centre = [by_id[c].pos for c in stripped if c in by_id]
                removed = [c for c in stripped if c in by_id and c not in partial.served]
        if served and not rebuild and op not in MOVE_OPERATORS:
            for cid in self.destroy(op, served, by_id, current, priorities, rng):
                smaller = self.remove(partial, cid)
                if smaller is not None:
                    partial = smaller
                    removed.append(cid)
        open_ = [c for c in contacts if c.id not in partial.served and c.id not in removed]
        centre = [by_id[c].pos for c in removed] or trip_centre or [rng.choice(contacts).pos]
        near = sorted(open_, key=lambda c: min(hdist(c.pos, q) for q in centre))[:6]
        extra = rng.sample(open_, min(3, len(open_)))
        pool = list({c.id: c for c in near + extra}.values())
        noise = {c.id: rng.uniform(0.6, 1.4) for c in pool}
        order = (sorted(pool, key=lambda c: -priorities[c.id] * noise[c.id])
                 + [by_id[c] for c in removed])
        base, placed = partial, []
        for contact in order:
            self.checker.check_time()
            child = self.insert_best(partial, contact)
            if child is not partial:
                partial = child
                placed.append(contact)
        subset = list({c.id: c for c in placed + [by_id[c] for c in removed]}.values())
        if 2 <= len(subset) <= CONFIG.exact_reinsert:
            partial = self.exact_reinsert(base, subset, partial, rng)
        tally = self.lns["ops"].setdefault("tour_rebuild" if rebuild else op,
                                           {"tried": 0, "accepted": 0, "improvements": 0,
                                            "gain": 0.0})
        tally["tried"] += 1
        delta = partial.score - current.score
        span = max(1e-6, self.stop - st["begin"])
        heat = st["temperature"] * max(0.0, self.stop - time.monotonic()) / span
        reward = 0.0
        if delta > 1e-9 or ((removed or op in MOVE_OPERATORS) and heat > 0 and rng.random() < math.exp(delta / heat)):
            self.lns["accepted"] += 1
            tally["accepted"] += 1
            if op in MOVE_OPERATORS:
                self.lns[op + "_accepted"] += 1
            reward = 1.0 if delta > 1e-9 else 0.3
            current = st["current"] = self.record(partial, current, {"kind": "lns", "op": op,
                                                                     "removed": removed},
                                                  len(partial.served))
            if current.score > best.score + 1e-9:
                self.lns["improvements"] += 1
                self.lns["gain"] += current.score - best.score
                tally["improvements"] += 1
                tally["gain"] += current.score - best.score
                if op in MOVE_OPERATORS:
                    self.lns[op + "_improvements"] += 1
                    self.lns[op + "_gain"] += current.score - best.score
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
