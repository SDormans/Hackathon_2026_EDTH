"""Adaptive Large Neighbourhood Search on top of the beam search.

The beam search builds a good valid plan; ALNS then repeatedly removes a few served
contacts (destroy), re-inserts contacts with best-position scout insertion (repair),
accepts by simulated annealing, and adapts destroy-method weights to what works.
Every candidate goes through the same IncrementalValidator; the official scorer still
decides the final plan in Search.finalize().
"""
from __future__ import annotations

from dataclasses import replace
import math
import random
import time

from fleetkit.baseline import SCOUT_LAYERS, SPEED_FACTOR, _scout_route
from fleetkit.energy import best_range_speed
from fleetkit.geometry import hdist
from fleetkit.model import HOSTILE, SCOUT, UNKNOWN
from fleetkit import validator as rules
from search_planner import DeadlineReached, Search, State

OBS_OFFSET_M = 100.0
DESTROYS = ("random", "worst", "scout", "related", "room")


class AlnsSearch(Search):
    """Beam search for `beam_share` of the budget, ALNS on the best plan for the rest."""

    def __init__(self, *args, beam_share=0.45, **kwargs):
        super().__init__(*args, **kwargs)
        self.full_stop = self.stop
        self.beam_stop = self.start + beam_share * (self.full_stop - self.start)
        self.scouts = [a for a in self.sc.aircraft if a.type == SCOUT]
        self.acft = self.sc.aircraft_by_id()
        self.cmap = self.sc.contact_by_id()
        self.rng = random.Random(self.sc.seed * 7919 + 17)
        self.alns_stats = {"iterations": 0, "improved": 0, "new_best": 0,
                           "weights": {}, "start_score": None, "best_score": None}

    def run(self):
        self.stop = self.checker.stop = self.beam_stop
        self.search_phase()
        self.stop = self.checker.stop = self.full_stop
        try:
            self.alns()
        except DeadlineReached:
            pass
        return self.finalize()

    # ------------------------------------------------------------------ helpers
    def _score(self, parsed, energies):
        served, used = set(), set()
        for aid, route in parsed.items():
            for wp in route.waypoints:
                if wp.action == "engage":
                    served.add(wp.target)
                    used.add(aid)
                elif wp.action == "observe":
                    c = self.cmap[wp.target]
                    if c.cls == UNKNOWN and rules.valid_observation(wp, c, self.sc):
                        served.add(c.id)
        r = self.sc.rules
        score = (sum(self.cmap[c].points for c in served) - r.lambda_e * sum(energies.values())
                 - r.c_eff * len(used))
        return frozenset(served), frozenset(used), score

    def _without(self, st, aids):
        keep = lambda d: {k: v for k, v in d.items() if k not in aids}
        parsed, energies = keep(st.parsed), keep(st.energies)
        served, used, score = self._score(parsed, energies)
        return State(keep(st.routes), dict(st.tasks), used, parsed, keep(st.separation),
                     energies, served, score)

    def _strikers(self, st):
        out = {}
        for aid, wps in st.routes.items():
            if self.acft[aid].type != SCOUT and wps and wps[-1].get("action") == "engage":
                out.setdefault(wps[-1]["target"], []).append(aid)
        return out

    def _owner(self, st):
        return {t["cid"]: sid for sid, tasks in st.tasks.items() for t in tasks}

    def _scout_z(self, st, sid):
        wps = st.routes.get(sid)
        return (wps[1]["z"],) if wps and len(wps) > 1 else SCOUT_LAYERS

    def _departures(self, scout, tasks, z):
        speed = min(SPEED_FACTOR * scout.v_h_mps, best_range_speed(scout.power))
        travel = hdist(scout.home, tasks[0]["point"]) / speed + z / scout.v_up_mps + 3.0
        base = max(0.0, tasks[0]["window"][0] - travel)
        return (base, base + 7.0, 0.0) if base > 0.0 else (0.0,)

    def _build_scout(self, scout, tasks, z):
        for depart in self._departures(scout, tasks, z):
            route = _scout_route(scout, tasks, depart, z, self.sc)
            if route is not None:
                yield route

    def _obs_point(self, scout, contact, angle):
        vx, vy = scout.home[0] - contact.pos[0], scout.home[1] - contact.pos[1]
        n = math.hypot(vx, vy) or 1.0
        a = math.radians(angle)
        ux, uy = vx / n, vy / n
        ux, uy = ux * math.cos(a) - uy * math.sin(a), ux * math.sin(a) + uy * math.cos(a)
        return contact.pos[0] + ux * OBS_OFFSET_M, contact.pos[1] + uy * OBS_OFFSET_M

    # ------------------------------------------------------------------ destroy
    def destroy(self, st, cids):
        """Remove contacts (their strikes and scout visits); rebuild touched scout tours."""
        strikers, owner = self._strikers(st), self._owner(st)
        removed = set(cids)
        while True:
            drop = {a for c in removed for a in strikers.get(c, [])}
            tasks = dict(st.tasks)
            rebuilt = {}
            for sid in {owner[c] for c in removed if c in owner}:
                left = [t for t in st.tasks[sid] if t["cid"] not in removed]
                tasks[sid] = left
                if not left:
                    drop.add(sid)
                else:
                    rebuilt[sid] = left
            base = self._without(st, drop)
            base = replace(base, tasks=tasks)
            if not rebuilt:
                return base, removed
            ok = True
            cur = base
            for sid, left in rebuilt.items():
                scout = self.acft[sid]
                found = None
                for z in self._scout_z(st, sid):
                    for route in self._build_scout(scout, left, z):
                        nxt = self.checker.extend(cur, {sid: route})
                        if nxt is not None:
                            found = nxt
                            break
                    if found:
                        break
                if found is None:
                    ok = False
                    removed.update(t["cid"] for t in left)  # give up this scout's whole tour
                    break
                cur = replace(found, tasks=tasks)
            if ok:
                return cur, removed

    def pick_destroy(self, st, op, k):
        served = sorted(st.served)
        if not served:
            return []
        if op == "random":
            return self.rng.sample(served, min(k, len(served)))
        if op == "worst":
            def net(cid):
                c = self.cmap[cid]
                cost = sum(self.matrix.costs.get((a, cid), 10.0) for a in self._strikers(st).get(cid, []))
                return (c.points - cost) * self.rng.uniform(0.8, 1.2)
            return sorted(served, key=net)[:k]
        if op == "scout":
            busy = [sid for sid, t in st.tasks.items() if t]
            if not busy:
                return self.rng.sample(served, min(k, len(served)))
            sid = self.rng.choice(busy)
            return [t["cid"] for t in st.tasks[sid]][:max(k, 8)]
        if op == "related":
            seed = self.cmap[self.rng.choice(served)]
            return sorted(served, key=lambda c: hdist(self.cmap[c].pos, seed.pos)
                          + 5.0 * abs(self.cmap[c].window[0] - seed.window[0]))[:k]
        # room: free the neighbourhood of a valuable unserved contact
        pool = [c for c in self.candidates if c.id not in st.served]
        if not pool:
            return self.rng.sample(served, min(k, len(served)))
        target = max(self.rng.sample(pool, min(4, len(pool))), key=lambda c: self.priorities[c.id])
        return sorted(served, key=lambda c: hdist(self.cmap[c].pos, target.pos))[:k]

    # ------------------------------------------------------------------ repair
    def insert(self, st, contact, stop):
        """Best valid insertion of one contact: all near scouts, every tour position."""
        groups = self.matrix.groups(contact, st.used) if contact.cls == HOSTILE else [()]
        if not groups:
            return None
        def reach(s):
            pts = [s.home] + [t["point"] for t in st.tasks[s.id]]
            return min(hdist(p, contact.pos) for p in pts)
        best, found = None, 0
        for scout in sorted(self.scouts, key=reach)[:6]:
            tasks = st.tasks[scout.id]
            for z in self._scout_z(st, scout.id):
                for angle in (0.0, 60.0, -60.0):
                    task = {"cid": contact.id, "point": self._obs_point(scout, contact, angle),
                            "window": contact.window}
                    for pos in range(len(tasks) + 1):
                        self.checker.check_time(stop)
                        new_tasks = tasks[:pos] + [task] + tasks[pos:]
                        for route in self._build_scout(scout, new_tasks, z):
                            scout_state = self.checker.extend(st, {scout.id: route})
                            if scout_state is None:
                                continue
                            if contact.cls == UNKNOWN:
                                options = [scout_state]
                            else:
                                obs_end = max(w["t"] + w.get("dwell_s", 0) for w in route
                                              if w.get("target") == contact.id and w.get("action") == "observe")
                                options = [cand for cand, _ in self.strikes(st, contact, groups, scout,
                                                                           route, obs_end, stop)]
                            for cand in options:
                                if cand.score > st.score and (best is None or cand.score > best.score):
                                    best = replace(cand, tasks={**st.tasks, scout.id: new_tasks})
                                    found += 1
                            break  # one departure per position is enough
                        if found >= 3:
                            return best
        return best

    def repair(self, st, removed):
        pool = [self.cmap[c] for c in removed]
        extra = [c for c in self.candidates if c.id not in st.served and c.id not in removed]
        extra.sort(key=lambda c: -self.priorities[c.id] * self.rng.uniform(0.6, 1.4))
        pool += extra[:6]
        pool.sort(key=lambda c: -self.priorities[c.id] * self.rng.uniform(0.7, 1.3))
        for contact in pool:
            if contact.id in st.served:
                continue
            local = min(self.full_stop, time.monotonic() + 0.25)
            try:
                nxt = self.insert(st, contact, local)
            except DeadlineReached:
                self.checker.check_time()  # re-raise only if the global deadline passed
                nxt = None
            if nxt is not None:
                st = nxt
        return st

    # ------------------------------------------------------------------ main loop
    def alns(self):
        from candidate_matrix import CandidateMatrix
        if self.matrix is None:
            self.matrix = CandidateMatrix(self.sc)
        self.candidates = [c for c in self.sc.contacts if c.cls in (HOSTILE, UNKNOWN)
                           and c.window is not None and c.id not in self.matrix.banned]
        self.priorities = {c.id: self.matrix.priority(c) for c in self.candidates}
        best = cur = max(self.archive, key=lambda s: s.score)
        self.alns_stats["start_score"] = best.score
        weights = {op: 1.0 for op in DESTROYS}
        t0, span = time.monotonic(), max(1e-6, self.full_stop - time.monotonic())
        temp0 = max(2.0, 0.01 * abs(best.score))
        try:
            self._loop(best, cur, weights, t0, span, temp0)
        finally:
            self.alns_stats["weights"] = {k: round(v, 2) for k, v in weights.items()}

    def _loop(self, best, cur, weights, t0, span, temp0):
        while True:
            self.checker.check_time()
            frac = (time.monotonic() - t0) / span
            temp = temp0 * (0.05 ** frac)
            ops = list(weights)
            op = self.rng.choices(ops, weights=[weights[o] for o in ops])[0]
            k = self.rng.randint(2, 5)
            cids = self.pick_destroy(cur, op, k)
            if not cids:
                break
            reduced, removed = self.destroy(cur, cids)
            cand = self.repair(reduced, removed)
            self.alns_stats["iterations"] += 1
            reward = 0
            if cand.score > best.score + 1e-6:
                best = cur = cand
                reward = 3
                self.alns_stats["new_best"] += 1
                self.record(best, self.root, {"kind": "alns", "op": op}, 0)
            elif cand.score > cur.score + 1e-6:
                cur, reward = cand, 2
                self.alns_stats["improved"] += 1
            elif self.rng.random() < math.exp((cand.score - cur.score) / temp):
                cur, reward = cand, 1
            weights[op] = max(0.1, 0.8 * weights[op] + 0.2 * reward)
            if self.rng.random() < 0.02:
                cur = best  # occasional restart from the best plan

    def report(self, verified):
        out = super().report(verified)
        self.alns_stats["best_score"] = verified[0][1].score if verified else None
        out["alns"] = self.alns_stats
        return out
