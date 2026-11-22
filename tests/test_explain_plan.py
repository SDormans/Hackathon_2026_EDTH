"""The offline explanation sidecar covers every hostile/unknown contact of a plan."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'analysis'))
import explain_plan
from fleetkit.baseline import greedy_plan
from fleetkit.generator import generate
from fleetkit.model import HOSTILE, UNKNOWN, Scenario


class ExplainPlanTests(unittest.TestCase):
    def test_one_line_per_contact_and_served_matches_plan(self):
        sc = Scenario.from_dict(generate('L2', 3))
        plan = greedy_plan(sc)
        rows = explain_plan.explain(sc, plan)
        expected = [c.id for c in sc.contacts if c.cls in (HOSTILE, UNKNOWN)]
        self.assertEqual([r['contact'] for r in rows], expected)
        targets = {w['target'] for r in plan['routes'] for w in r['waypoints']
                   if w.get('action') == ('engage')}
        for r in rows:
            self.assertTrue(r['why'])
            if r['cls'] == HOSTILE:
                self.assertEqual(r['served'], r['contact'] in targets)
            if not r['served']:
                self.assertIn('reason', r)

    def test_reason_categories(self):
        self.assertEqual(explain_plan.category('scout_rule:R8'), 'unreachable')
        self.assertEqual(explain_plan.category('collateral_ban'), 'collateral')
        self.assertEqual(explain_plan.category('time_cap'), 'window')
        self.assertEqual(explain_plan.category('no_team'), 'no effectors')


if __name__ == '__main__':
    unittest.main()
