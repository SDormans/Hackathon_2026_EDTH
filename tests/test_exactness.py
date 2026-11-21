"""Incremental validator vs the official fleetkit validator on each scout-tour move type.

Every perturbed scout route (insertion, reorder, 2-opt/relocation) and every injected
violation (R4, R6, R8, energy R3, horizon R1) must get the same verdict from
IncrementalValidator.extend as from fleetkit.scorer.evaluate on the full plan."""
import copy
from dataclasses import replace
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import planner  # noqa: F401  (fleetkit discovery)
from search_planner import Search, scout_tour
from fleetkit.generator import generate
from fleetkit.model import Scenario
from fleetkit.scorer import evaluate


def two_opt_orders(n):
    """Index orders near range(n): single-visit relocations (or-opt), then segment
    reversals (2-opt)."""
    base = list(range(n))
    seen = {tuple(base)}
    moves = []
    for i in range(n):
        for j in range(n):
            order = base[:i] + base[i + 1:]
            order.insert(j, i)
            moves.append(order)
    for i in range(n - 1):
        for j in range(i + 2, n + 1):
            moves.append(base[:i] + base[i:j][::-1] + base[j:])
    for order in moves:
        if tuple(order) not in seen:
            seen.add(tuple(order))
            yield order


def full_plan(state, seed, sid, route):
    plan = state.plan(seed)
    plan["routes"] = [r for r in plan["routes"] if r["aircraft"] != sid]
    plan["routes"].append({"aircraft": sid, "waypoints": route})
    return plan


class ExactnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sc = Scenario.from_dict(generate("L3", 1))
        search = Search(cls.sc, seconds=6.0, lns_share=0.0)
        cls.best = search.run()[0][0]
        cls.search = search
        cls.search.checker.stop = time.monotonic() + 600
        scouts = {a.id for a in cls.sc.aircraft if a.type == "scout"}
        cls.sid = max((s for s in cls.best.tasks if s in scouts),
                      key=lambda s: len(cls.best.tasks[s]))
        cls.tasks = cls.best.tasks[cls.sid]
        cls.scout = cls.sc.aircraft_by_id()[cls.sid]
        cls.z = max(w["z"] for w in cls.best.routes[cls.sid])
        assert len(cls.tasks) >= 3, "need a multi-visit scout tour"

    def agree(self, route, sc=None, parent=None, checker=None, sid=None, expect=None):
        sc, sid = sc or self.sc, sid or self.sid
        parent = parent or self.best
        checker = checker or self.search.checker
        official = evaluate(sc, full_plan(parent, sc.seed, sid, route))
        checker.last_rule = None
        actual = checker.extend(parent, {sid: route})
        self.assertEqual(actual is not None, official.valid,
                         [str(v) for v in official.violations][:3])
        rules = {v.rule for v in official.violations}
        if actual is not None:
            self.assertAlmostEqual(actual.score, official.score, places=6)
            self.assertEqual(actual.served, frozenset(official.served))
        else:
            self.assertIn(checker.last_rule, rules)
        if expect:
            self.assertIn(expect, rules)
            self.assertIsNone(actual)
        return official.valid

    def tours(self, orders):
        by_cid = {t["cid"]: t for t in self.tasks}
        for order in orders:
            tour = scout_tour(self.sc, self.scout, [by_cid[c] for c in order], self.z)
            if tour is not None:
                yield copy.deepcopy(tour)

    def removal_orders(self, keeps_prefix):
        """Remove each visit and re-add it: insertion positions keep the other visits'
        order (keeps_prefix True), reordered tours do not."""
        contacts = self.sc.contact_by_id()
        for task in self.tasks:
            rest = [t for t in self.tasks if t is not task]
            for order, keeps in Search.tour_orders(self.scout, rest, contacts[task["cid"]], True):
                if keeps == keeps_prefix:
                    yield order

    def check_moves(self, orders):
        verdicts = [self.agree(tour) for tour in self.tours(orders)]
        self.assertTrue(verdicts)
        return verdicts

    def test_insertion_moves_agree(self):
        self.check_moves(self.removal_orders(True))

    def test_reorder_moves_agree(self):
        self.check_moves(self.removal_orders(False))

    def test_two_opt_moves_agree(self):
        cids = [t["cid"] for t in self.tasks]
        orders = [[cids[i] for i in o] for o in two_opt_orders(len(cids))]
        self.check_moves(orders)

    def valid_moved_routes(self):
        cids = [t["cid"] for t in self.tasks]
        orders = [cids] + [[cids[i] for i in o] for o in two_opt_orders(len(cids))]
        out = []
        for tour in self.tours(orders):
            if self.search.checker.extend(self.best, {self.sid: tour}) is not None:
                out.append(tour)
        self.assertTrue(out)
        return out[:4]

    def test_injected_r4_agrees(self):
        struck = {wp.get("target") for r in self.best.routes.values() for wp in r
                  if wp.get("action") == "engage"}
        elsewhere = {wp.get("target") for a, r in self.best.routes.items() if a != self.sid
                     for wp in r if wp.get("action") == "observe"}
        only_here = struck - elsewhere
        injected = 0
        for route in self.valid_moved_routes():
            if not only_here & {wp.get("target") for wp in route}:
                continue
            for wp in route:
                wp.pop("action", None)
                wp.pop("target", None)
            self.agree(route, expect="R4")
            injected += 1
        self.assertGreater(injected, 0)

    def test_injected_r6_agrees(self):
        zone = max(self.sc.zones, key=lambda z: z.top_m)
        for route in self.valid_moved_routes():
            a, b = route[1], route[2]
            mid = dict(x=zone.center[0], y=zone.center[1], z=min(a["z"], zone.top_m - 5.0),
                       t=(a["t"] + a.get("dwell_s", 0.0) + b["t"]) / 2)
            route.insert(2, mid)
            self.agree(route, expect="R6")

    def test_injected_r8_agrees(self):
        sc = copy.deepcopy(self.sc)
        sc.aircraft.append(replace(self.scout, id="twin"))
        for route in self.valid_moved_routes():
            search = Search(sc, seconds=600)
            routes = {**self.best.routes, self.sid: route}
            parent = search.checker.extend(search.root, routes)
            self.assertIsNotNone(parent)
            twin = copy.deepcopy(route)
            for wp in twin[1:]:
                wp.pop("action", None)
                wp.pop("target", None)
            self.agree(twin, sc=sc, parent=parent, checker=search.checker, sid="twin",
                       expect="R8")

    def test_injected_energy_agrees(self):
        need = self.best.energies[self.sid]
        frac = 100.0 * 0.5 * need / self.scout.usable_wh
        sc = copy.deepcopy(self.sc)
        sc.aircraft = [replace(a, battery_pct=a.battery_pct * frac / 100.0) if a.id == self.sid
                       else a for a in sc.aircraft]
        search = Search(sc, seconds=600)
        here = {wp.get("target") for wp in self.best.routes[self.sid]}
        others = {a: r for a, r in self.best.routes.items() if a != self.sid
                  and not any(wp.get("action") == "engage" and wp.get("target") in here
                              for wp in r)}
        parent = search.checker.extend(search.root, others)
        self.assertIsNotNone(parent)
        for route in self.valid_moved_routes():
            self.agree(route, sc=sc, parent=parent, checker=search.checker, expect="R3")

    def test_injected_horizon_agrees(self):
        for route in self.valid_moved_routes():
            route[-1]["t"] = self.sc.horizon_s + 30.0
            self.agree(route, expect="R1")


if __name__ == "__main__":
    unittest.main()
