"""Link-loss policy tests; skipped when fleetkit has no linkloss package (AA-OpenProjects kit)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import planner  # noqa: F401  (fleetkit discovery)

try:
    from fleetkit.linkloss.events import make_events
    from fleetkit.linkloss.sim import simulate
    from fleetkit.linkloss.score import evaluate_cases, oracle_points
except ImportError:
    make_events = None
from fleetkit.baseline import greedy_plan
from fleetkit.generator import generate
from fleetkit.model import Scenario


@unittest.skipIf(make_events is None, "fleetkit.linkloss not available")
class LinkLossPolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import policy
        cls.policy = policy
        cls.cases = []
        for level, seed in [("L1", 3), ("L2", 1), ("L3", 2)]:
            sc = Scenario.from_dict(generate(level, seed))
            plan = greedy_plan(sc)
            cls.cases.append((sc, plan, make_events(sc, plan, seed + 1)))

    def test_safe_and_better_than_broadcast(self):
        from fleetkit.linkloss.policies import BroadcastSensePolicy
        oracles = [oracle_points(*c) for c in self.cases]
        mine = evaluate_cases(self.cases, self.policy.Policy, seeds=2, oracles=oracles)
        floor = evaluate_cases(self.cases, BroadcastSensePolicy, seeds=2, oracles=oracles)
        self.assertEqual(mine["failures"], 0)
        self.assertGreater(mine["s_loss"], floor["s_loss"])
        self.assertLess(mine["ms_per_call"], 50.0)

    def test_tight_short_range_setting_beats_broadcast(self):
        from fleetkit.linkloss.policies import BroadcastSensePolicy
        setting = (250.0, 50.0)
        mine = evaluate_cases(self.cases, self.policy.Policy, setting, seeds=2)
        floor = evaluate_cases(self.cases, BroadcastSensePolicy, setting, seeds=2)
        self.assertLessEqual(mine["failures"], floor["failures"])
        self.assertGreater(mine["s_loss"], floor["s_loss"])

    def test_messages_fit_size_limit(self):
        import json
        sizes = []
        orig = self.policy.Policy.step

        def step(policy_self, obs):
            action, msg = orig(policy_self, obs)
            if msg is not None:
                sizes.append(len(json.dumps(msg, separators=(",", ":")).encode()))
            return action, msg
        self.policy.Policy.step = step
        try:
            for sc, plan, ev in self.cases:
                simulate(sc, plan, ev, self.policy.Policy, run_seed=1)
        finally:
            self.policy.Policy.step = orig
        self.assertTrue(sizes)
        self.assertLessEqual(max(sizes), 64)


if __name__ == "__main__":
    unittest.main()
