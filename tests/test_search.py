"""Run with python3 -m unittest discover -s tests -v (standard library only)."""
import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Entry point also supplies the local fleetkit discovery used by these tests.
import planner
from candidate_matrix import CandidateMatrix
import search_planner
from search_planner import IncrementalValidator, Search, unserved_reason
from fleetkit.baseline import greedy_plan
from fleetkit.generator import generate
from fleetkit.model import Scenario, save_json
from fleetkit.scorer import evaluate
import fleetkit


def scenario():
    """Serving A consumes both lights; skipping A allows the better B+C pair."""
    aircraft = []
    for kind, subtype, count in [('scout', None, 2), ('effector', 'light', 2)]:
        for i in range(count):
            aircraft.append(dict(id=f'{kind}{i}', type=kind, subtype=subtype,
                                 home=[0, len(aircraft)*60, 0], v_h_mps=30,
                                 v_up_mps=10, v_down_mps=15, capacity_wh=200,
                                 battery_pct=100, power=dict(p0_w=80, k_v=.1,
                                                            e_climb_wh_per_m=.01)))
    contacts = [dict(id=cid, **{'class': 'hostile'}, pos=pos, points=points,
                     engage_needs={'light': need}, window_s=[100, 800])
                for cid, pos, points, need in [('A', [1500, 0, 0], 90, 2),
                                               ('B', [1300, 500, 0], 70, 1),
                                               ('C', [1300, -500, 0], 70, 1)]]
    return Scenario.from_dict(dict(seed=1, level='test', horizon_s=900,
                                   aircraft=aircraft, contacts=contacts, zones=[]))


class IncrementalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sc = scenario()
        cls.plan = greedy_plan(cls.sc)
        assert evaluate(cls.sc, cls.plan).valid
        cls.routes = {r['aircraft']: r['waypoints'] for r in cls.plan['routes']}

    def checker(self, sc=None):
        search = Search(sc or self.sc, seconds=10)
        return search, search.checker

    def test_incremental_score_matches_official(self):
        search, checker = self.checker()
        state = checker.extend(search.root, self.routes)
        result = evaluate(self.sc, self.plan)
        self.assertIsNotNone(state)
        self.assertAlmostEqual(state.score, result.score)
        self.assertEqual(state.served, frozenset(result.served))

    def test_changed_routes_match_full_validator(self):
        search, checker = self.checker()
        parent = checker.extend(search.root, self.routes)
        rng = random.Random(42)
        for _ in range(120):
            aid = rng.choice(list(self.routes))
            route = copy.deepcopy(self.routes[aid])
            if rng.random() < .5:
                shift = rng.uniform(-60, 60)
                for wp in route:
                    wp['t'] += shift
            else:
                wp = rng.choice(route[1:])
                wp[rng.choice(['x', 'y', 'z'])] += rng.uniform(-150, 150)
            extra = {aid: route}
            full = parent.plan(self.sc.seed)
            for r in full['routes']:
                if r['aircraft'] == aid:
                    r['waypoints'] = route
            official = evaluate(self.sc, full)
            actual = checker.extend(parent, extra)
            self.assertEqual(actual is not None, official.valid)
            if actual is not None:
                self.assertAlmostEqual(actual.score, official.score)

    def test_cached_detour_matches_official(self):
        sc = Scenario.from_dict(generate('L3', 1))
        rng = random.Random(3)
        for _ in range(200):
            pts = [(rng.uniform(-500, 6000), rng.uniform(-500, 6000), rng.choice([40.0, 70.0, 130.0]))
                   for _ in range(2)]
            expected = search_planner._official_detour(list(pts), sc)
            self.assertEqual(search_planner._cached_detour(list(pts), sc), expected)
            self.assertEqual(search_planner._cached_detour(list(pts), sc), expected)

    def test_quick_reject_matches_full_validator(self):
        search, checker = self.checker()
        aid = next(iter(self.routes))
        for field, shift in (('t', -10000.0), ('t', 10000.0)):
            route = copy.deepcopy(self.routes[aid])
            for wp in route:
                wp[field] += shift
            rule = checker.quick_reject({aid: route})
            self.assertIsNotNone(rule)
            checker.last_rule = None
            self.assertIsNone(checker.extend(search.root, {aid: route}))
            self.assertEqual(checker.last_rule, rule)
        self.assertIsNone(checker.quick_reject(self.routes))

    def test_rebuilt_scout_must_keep_previous_confirmations(self):
        search, checker = self.checker()
        parent = checker.extend(search.root, self.routes)
        aid = next(a.id for a in self.sc.aircraft if a.type == 'scout' and a.id in self.routes)
        route = copy.deepcopy(self.routes[aid])
        for wp in route:
            wp.pop('action', None)
            wp.pop('target', None)
        self.assertIsNone(checker.extend(parent, {aid: route}))
        self.assertIn('R4', checker.rejections)

    def test_new_aircraft_conflicts_with_existing_route(self):
        sc = copy.deepcopy(self.sc)
        aid = next(a.id for a in sc.aircraft if a.type == 'scout' and a.id in self.routes)
        original = sc.aircraft_by_id()[aid]
        sc.aircraft.append(replace(original, id='duplicate-scout'))
        search, checker = self.checker(sc)
        parent = checker.extend(search.root, self.routes)
        self.assertIsNone(checker.extend(parent, {'duplicate-scout': self.routes[aid]}))
        self.assertIn('R8', checker.rejections)

    def test_signature_identifies_routes(self):
        search, checker = self.checker()
        a = checker.extend(search.root, self.routes)
        b = checker.extend(search.root, copy.deepcopy(self.routes))
        self.assertEqual(a.signature(), b.signature())
        aid = next(iter(self.routes))
        route = copy.deepcopy(self.routes[aid])
        route[-1]['t'] += 1.0
        c = checker.extend(a, {aid: route})
        if c is not None:
            self.assertNotEqual(a.signature(), c.signature())
        self.assertNotEqual(search.root.signature(), a.signature())

    def test_branches_do_not_mutate_parent(self):
        search, checker = self.checker()
        before = copy.deepcopy(search.root)
        child = checker.extend(search.root, self.routes)
        self.assertEqual(search.root, before)
        self.assertIsNotNone(child)
        self.assertEqual(search.root.routes, {})

    def test_matrix_respects_used_resources_and_exact_mix(self):
        matrix = CandidateMatrix(copy.deepcopy(self.sc))
        c = self.sc.contacts[0]
        group = matrix.groups(c, set())[0]
        self.assertEqual(len(group), 2)
        self.assertEqual(matrix.groups(c, {group[0].id}), [])
        # An optimistic cost estimate must not become a feasibility exclusion.
        for a in matrix.effectors:
            a.capacity_wh = .001
        self.assertTrue(matrix.groups(c, set()))


class SearchTests(unittest.TestCase):
    def test_skip_branch_finds_better_combination(self):
        sc = scenario()
        base = evaluate(sc, greedy_plan(sc))
        search = Search(sc, seconds=1.0, top_n=3, beam_width=6)
        verified = search.run()
        self.assertGreater(verified[0][1].score, base.score)
        self.assertEqual(set(verified[0][1].served), {'B', 'C'})
        self.assertEqual(len(verified), 3)
        self.assertEqual(len({s.signature() for s, _ in verified}), 3)
        scores = [r.score for _, r in verified]
        self.assertEqual(scores, sorted(scores, reverse=True))
        for state, _ in verified:
            self.assertTrue(evaluate(sc, state.plan(sc.seed)).valid)
        self.assertTrue(any(n['decision'].get('kind') == 'skip'
                            for n in search.trace if isinstance(n['decision'], dict)))
        for n in search.trace[1:]:
            self.assertLess(n['parent'], n['id'])

    def test_repair_adds_unserved_unknown(self):
        sc = scenario()
        plan = greedy_plan(sc)
        sc.contacts.append(replace(sc.contacts[1], id='U', cls='unknown', points=30,
                                   needs={}, pos=(900.0, 200.0, 0.0)))
        search = Search(sc, seconds=5)
        search.matrix = CandidateMatrix(sc)
        state = search.checker.extend(search.root, {r['aircraft']: r['waypoints'] for r in plan['routes']})
        self.assertNotIn('U', state.served)
        repaired = search.repair(state, 0)
        self.assertIn('U', repaired.served)
        self.assertGreater(repaired.score, state.score)
        result = evaluate(sc, repaired.plan(sc.seed))
        self.assertTrue(result.valid)
        self.assertAlmostEqual(result.score, repaired.score)

    def test_expired_budget_returns_valid_empty_plan(self):
        sc = scenario()
        search = Search(sc, seconds=0)
        verified = search.run()
        self.assertEqual(verified[0][1].score, 0)
        self.assertTrue(evaluate(sc, verified[0][0].plan(sc.seed)).valid)

    def test_no_serviceable_contacts(self):
        sc = scenario()
        sc.contacts = []
        result = Search(sc, seconds=.1).run()
        self.assertTrue(result[0][1].valid)
        self.assertEqual(result[0][1].score, 0)

    def test_standalone_handin_and_tree_artifacts(self):
        # Copy only hand-in modules, with fleetkit supplied by the runner.
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            for name in ['planner.py', 'search_planner.py', 'candidate_matrix.py', 'tree_view.py']:
                shutil.copy2(ROOT / name, work / name)
            sc = generate('L1', 7)
            save_json(sc, work / 'scenario.json')
            env = dict(os.environ, PYTHONPATH=str(Path(fleetkit.__file__).resolve().parent.parent))
            start = time.monotonic()
            result = subprocess.run([sys.executable, 'planner.py', 'scenario.json', 'plan.json',
                                     '--seconds', '.5', '--top-n', '3', '--artifacts', 'artifacts'],
                                    cwd=work, env=env, timeout=5, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLess(time.monotonic()-start, 5)
            parsed = Scenario.from_dict(sc)
            plan = json.loads((work / 'plan.json').read_text())
            self.assertTrue(evaluate(parsed, plan).valid)
            report = json.loads((work / 'artifacts/tree.json').read_text())
            self.assertTrue((work / 'artifacts/tree.html').exists())
            self.assertTrue(report['solutions'])
            for p in (work / 'artifacts').glob('plan-*.json'):
                self.assertTrue(evaluate(parsed, json.loads(p.read_text())).valid)



def diagnostic_scenario():
    """One contact per failure reason, next to the servable B and C."""
    sc = scenario()
    data = dict(seed=1, level='test', horizon_s=900, zones=[], aircraft=[
        dict(id=a.id, type=a.type, subtype=a.subtype, home=list(a.home), v_h_mps=a.v_h_mps,
             v_up_mps=a.v_up_mps, v_down_mps=a.v_down_mps, capacity_wh=a.capacity_wh,
             battery_pct=a.battery_pct, power=dict(p0_w=a.power.p0_w, k_v=a.power.k_v,
                                                  e_climb_wh_per_m=a.power.e_climb_wh_per_m))
        for a in sc.aircraft])
    # A spare light, so EARLY reaches the scout stage instead of running out of team.
    spare = dict(data['aircraft'][-1], id='effector2', home=[0, 240, 0])
    data['aircraft'].append(spare)
    hostile = lambda cid, pos, needs, window, points=60: dict(
        id=cid, **{'class': 'hostile'}, pos=pos, points=points, engage_needs=needs, window_s=window)
    data['contacts'] = [
        hostile('B', [1300, 500, 0], {'light': 1}, [100, 800], 70),
        hostile('C', [1300, -500, 0], {'light': 1}, [100, 800], 70),
        # A civilian inside the collateral radius: R7 forbids engaging it.
        hostile('BAN', [-1500, 0, 0], {'light': 1}, [100, 800]),
        dict(id='CIV', **{'class': 'civilian'}, pos=[-1450, 0, 0], window_s=None),
        # No heavy effector exists.
        hostile('HEAVY', [1300, 0, 0], {'heavy': 1}, [100, 800]),
        # Closes before any scout can get there.
        hostile('EARLY', [2500, 2500, 0], {'light': 1}, [0, 20]),
    ]
    return Scenario.from_dict(data)


class DiagnosticsTests(unittest.TestCase):
    def stats(self, outcomes, attempts=None, warm=0):
        return {'attempts': sum(outcomes.values()) if attempts is None else attempts,
                'warm_accepted': warm, 'outcomes': outcomes, 'rules': {}}

    def test_reason_classification(self):
        c = scenario().contacts[0]
        self.assertEqual(unserved_reason(c, {c.id}, None), 'collateral_ban')
        self.assertEqual(unserved_reason(replace(c, window=None), set(), None), 'no_window')
        self.assertEqual(unserved_reason(c, set(), None), 'never_tried')
        self.assertEqual(unserved_reason(c, set(), self.stats({}, 0)), 'never_tried')
        self.assertEqual(unserved_reason(c, set(), self.stats({'accepted': 1, 'time_cap': 9})),
                         'outranked')
        self.assertEqual(unserved_reason(c, set(), self.stats({}, 0, warm=1)), 'outranked')
        self.assertEqual(unserved_reason(c, set(), self.stats({'time_cap': 3, 'strike_rule:R8': 1})),
                         'time_cap')
        # Ties go to the attempt that got furthest.
        self.assertEqual(unserved_reason(c, set(), self.stats({'scout_route': 2, 'strike_rule:R8': 2})),
                         'strike_rule:R8')

    def test_every_unserved_contact_has_its_reason(self):
        sc = diagnostic_scenario()
        search = Search(sc, seconds=3.0)
        verified = search.run()
        result = verified[0][1]
        self.assertTrue(result.valid)
        report = search.report(verified)
        contacts = report['contacts']
        self.assertEqual(set(contacts), {'B', 'C', 'BAN', 'HEAVY', 'EARLY'})
        self.assertEqual({cid for cid, e in contacts.items() if e['served']}, set(result.served))
        self.assertEqual(set(result.served), {'B', 'C'})
        reasons = {cid: e['reason'] for cid, e in contacts.items() if not e['served']}
        self.assertEqual(reasons['BAN'], 'collateral_ban')
        self.assertEqual(reasons['HEAVY'], 'no_team')
        self.assertTrue(reasons['EARLY'].startswith('scout_'), reasons['EARLY'])
        self.assertEqual(sum(report['unserved'].values()), 3)
        self.assertEqual(sum(report['unserved_points'].values()), 180)

    def test_expired_budget_reports_never_tried(self):
        sc = diagnostic_scenario()
        search = Search(sc, seconds=0)
        report = search.report(search.run())
        reasons = {cid: e.get('reason') for cid, e in report['contacts'].items()}
        self.assertEqual(reasons['BAN'], 'collateral_ban')
        self.assertEqual({r for cid, r in reasons.items() if cid != 'BAN'}, {'never_tried'})

    def test_generated_scenario_accounting_is_complete(self):
        sc = Scenario.from_dict(generate('L2', 3))
        search = Search(sc, seconds=2.0)
        verified = search.run()
        report = search.report(verified)
        served = set(verified[0][1].served)
        targets = {c.id for c in sc.contacts if c.cls in ('hostile', 'unknown')}
        self.assertEqual(set(report['contacts']), targets)
        for cid, entry in report['contacts'].items():
            self.assertEqual(entry['served'], cid in served)
            self.assertEqual('reason' in entry, cid not in served)
            self.assertEqual(sum(entry['outcomes'].values()), entry['attempts'])
        self.assertEqual(sum(report['unserved'].values()), len(targets - served))
        self.assertEqual(set(report['unserved']), set(report['unserved_points']))


if __name__ == '__main__':
    unittest.main()
