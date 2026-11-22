"""Link-loss policy rules: proceed only on a held confirmation, targeted O notices, no R4 breaks."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'analysis'))
import linkloss_knowledge as lk
from fleetkit.linkloss.policies import load_policy
from fleetkit.linkloss.sim import simulate
from fleetkit.model import parse_plan

POLICY = str(ROOT / 'policy.py')


class PolicyRuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cases = [lk.load_case(lk.prepare_case(level, seed, ev, Path(cls.tmp.name) / f'{level}-{seed}-{ev}.json.gz'))
                     for level, seed, ev in (('L2', 7, 1), ('L2', 7, 2), ('L1', 3, 1))]
        cls.factory = load_policy(POLICY)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _effector(self, sc, plan):
        for aid in sorted(parse_plan(plan)[0]):
            p = self.factory(sc, plan, aid)
            if p.target is not None and p.window_end is not None:
                return p
        self.skipTest('no effector with a windowed target')

    def test_never_engages_without_a_completed_observation(self):
        for sc, plan, events in self.cases:
            for setting in ((250.0, 50.0), (1000.0, 200.0)):
                for seed in range(3):
                    r = simulate(sc, plan, events, self.factory, setting=setting, run_seed=seed)
                    self.assertFalse([f for f in r.failed if f.startswith('R4')], (setting, seed, r.failed))
                    self.assertFalse([f for f in r.failed if 'policy error' in f], (setting, seed, r.failed))

    def test_hold_short_never_outlasts_the_window(self):
        sc, plan, _ = self.cases[0]
        p = self._effector(sc, plan)
        lead = p.step.__globals__['HOLD_LEAD_S']
        p.window_end = p.engage_t + 4.0
        p.peer_state = {aid: (0.0, 1.0, 0.0) for aid, _ in p.obs_wp.get(p.target, [])}  # scouts very late
        self.assertLessEqual(p._grace_until(p.engage_t), max(p.engage_t, p.window_end - lead) + 1e-9)

    def test_observation_notice_goes_only_to_the_strike_effectors(self):
        sc, plan, _ = self.cases[0]
        aid = next(a for a in sorted(parse_plan(plan)[0]) if a.startswith('S'))
        p = self.factory(sc, plan, aid)
        c = sorted(p.strikers)[0]
        p._note_obs(c, 100.0, 0.0)
        self.assertEqual(set(p.news[('O', c)]['audience']), set(p.strikers[c]) - {p.id})


    def test_nominal_check_keeps_on_schedule_peers_moving(self):
        # the no-safe-move fallback ranks options by the peers' nominal motion; a formation peer
        # guessed blocked must not be assumed to stop there (L4-402, 500 m: S05/S13 at 39 m)
        sc, plan, _ = self.cases[0]
        ids = sorted(parse_plan(plan)[0])
        p = self.factory(sc, plan, ids[0])
        peer = next(a for a in ids[1:] if p.peers[a][1] is None or p.peers[a][1] != p.target)
        t = (p.clock.start + p.clock.end) / 2.0
        here = p.clock.position(t)
        far = [tuple(c + 5000.0 for c in here)]
        p._peer_on_plan = lambda aid, now: True
        p._peer_blocked = lambda aid, now, near: True
        p._polyline_point = lambda aid, now: [here]
        self.assertGreater(p._step_worst(t, t, 1.0, [(peer, far)], nominal=True), 1000.0)

    def test_no_clear_move_fallback_looks_ahead_before_stopping(self):
        # L3-301 e2001, 2000 m: H04 stopped at its engage gate because a stop and a slow move tied over
        # one step, and the on-plan H08 behind it closed to 25.6 m before hearing the stop
        sc, plan, _ = self.cases[0]
        p = self.factory(sc, plan, sorted(parse_plan(plan)[0])[0])
        t = (p.clock.start + p.clock.end) / 2.0
        here = p.clock.position(t)
        p._peer_reach = lambda aid, now: [here]
        p._peer_nominal = lambda aid, now: [here]
        p._allowed = lambda tau, r, may: r != 1.0
        p._step_worst = lambda now, tau, r, near, nominal=False: {0.5: 57.3, 0.0: 57.3, -1.0: 39.3}[r] if nominal else 0.0
        p._ahead_worst = lambda now, tau, r, near: {0.5: 36.0, 0.0: 19.0, -1.0: 25.8}[r]
        module = type(p)._safe_options.__globals__
        old = module['NOMINAL_AHEAD']
        try:
            self.assertEqual(p._safe_options(t, t, [0.5, 0.0, -1.0], False), [0.5])
            module['NOMINAL_AHEAD'] = False
            self.assertEqual(p._safe_options(t, t, [0.5, 0.0, -1.0], False), [0.0])
        finally:
            module['NOMINAL_AHEAD'] = old


class HarnessCliTests(unittest.TestCase):
    def test_eval_harness_exposes_run_and_report(self):
        for cmd in ('run', 'report'):
            r = subprocess.run([sys.executable, str(ROOT / 'analysis' / 'linkloss_eval.py'), cmd, '-h'],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(f'linkloss_eval.py {cmd}', r.stdout)


if __name__ == '__main__':
    unittest.main()
