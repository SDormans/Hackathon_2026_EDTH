"""Robustness: the planner exits 0 and exports an officially valid plan for any input.

Fixtures under tests/fixtures/robust/ are degenerate scenarios (analysis/degenerate_scenarios.py)
and fuzz cases (analysis/fuzz_scenarios.py) that failed before their fix.
"""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import planner  # noqa: E402,F401  (local fleetkit discovery)
from search_planner import Search  # noqa: E402
from fleetkit.model import Scenario, load_json  # noqa: E402
from fleetkit.scorer import evaluate  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "robust"
SECONDS = 5


def run_planner(scenario, out_dir, seconds=SECONDS):
    plan = Path(out_dir) / f"{Path(scenario).stem}.plan.json"
    started = time.monotonic()
    proc = subprocess.run([sys.executable, str(ROOT / "planner.py"), str(scenario), str(plan),
                           "--seconds", str(seconds)], capture_output=True, text=True,
                          timeout=75, cwd=ROOT)
    return proc, plan, time.monotonic() - started


def report_errors(stderr):
    for line in reversed(stderr.strip().splitlines()):
        if line.startswith("{"):
            return json.loads(line).get("errors")
    return None


class FixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.fixtures = sorted(FIXTURES.glob("*.json"))
        with ThreadPoolExecutor(4) as pool:
            cls.runs = dict(zip(cls.fixtures, pool.map(lambda f: run_planner(f, cls.tmp.name),
                                                       cls.fixtures)))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_fixtures_present(self):
        names = {f.stem for f in self.fixtures}
        for required in ("a-no-contacts", "b1-collateral-banned", "c-tiny-fleet", "d1-fleet-100",
                         "e1-horizon-60s", "f1-zones-everywhere", "j-missing-windows"):
            self.assertIn(required, names)

    def test_every_fixture_gives_a_valid_plan(self):
        for fixture, (proc, plan, wall) in self.runs.items():
            with self.subTest(fixture=fixture.stem):
                self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
                self.assertNotIn("Traceback", proc.stderr)
                self.assertEqual(report_errors(proc.stderr), [])
                self.assertTrue(plan.exists())
                result = evaluate(Scenario.from_dict(load_json(fixture)), load_json(plan))
                self.assertTrue(result.valid, [str(v) for v in result.violations[:3]])
                # Short budget plus final validation; the hard limit is 60 s.
                self.assertLess(wall, SECONDS + 20)


def _subprocess_main(body, scenario, plan):
    """Runs planner.main() in a child process after `body` patches the planner module."""
    script = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(ROOT)!r})
        import planner
        {textwrap.indent(textwrap.dedent(body), '        ').strip()}
        sys.argv = ['planner.py', {str(scenario)!r}, {str(plan)!r}, '--seconds', '5']
        planner.main()
    """)
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          timeout=60, cwd=ROOT)


class SafetyNetTests(unittest.TestCase):
    scenario = FIXTURES / "c-tiny-fleet.json"

    def assert_empty_valid(self, proc, plan):
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        data = load_json(plan)
        self.assertEqual(data["routes"], [])
        self.assertTrue(evaluate(Scenario.from_dict(load_json(self.scenario)), data).valid)

    def test_search_crash_exports_empty_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "plan.json"
            proc = _subprocess_main("""
                class Boom:
                    def __init__(self, *a, **k):
                        raise ZeroDivisionError('injected')
                planner.Search = Boom
            """, self.scenario, plan)
            self.assert_empty_valid(proc, plan)
            self.assertIn("injected", proc.stderr)

    def test_watchdog_writes_empty_plan_before_hard_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "plan.json"
            started = time.monotonic()
            proc = _subprocess_main("""
                planner.HARD_LIMIT_S = 2.0
                class Hang:
                    def __init__(self, *a, **k):
                        pass
                    def run(self):
                        time.sleep(30)
                planner.Search = Hang
            """, self.scenario, plan)
            self.assertLess(time.monotonic() - started, 15)
            self.assert_empty_valid(proc, plan)
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_watchdog_write_does_not_wait_for_a_stalled_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "plan.json"
            writer = planner.PlanWriter(plan, 7)
            writer.lock.acquire()  # a main-thread write that never finishes
            started = time.monotonic()
            self.assertTrue(writer.write_empty(wait=0.2))
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(load_json(plan), {"seed": 7, "routes": []})

    def test_phase_error_keeps_best_archived_plan(self):
        sc = Scenario.from_dict(load_json(self.scenario))
        search = Search(sc, seconds=3)

        def broken(*args):
            raise IndexError("injected")
        search.improve_phase = broken
        verified = search.run()
        self.assertEqual(len(search.errors), 1)
        self.assertIn("injected", search.errors[0])
        state, result = verified[0]
        self.assertTrue(evaluate(sc, state.plan(sc.seed)).valid)
        self.assertGreater(result.score, 0)


if __name__ == "__main__":
    unittest.main()
