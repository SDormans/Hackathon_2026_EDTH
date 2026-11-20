"""Batch selection, failure accounting, subprocess limits and parallel CLI checks."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'analysis'))
import benchmark_search as bench
from fleetkit.generator import generate
from fleetkit.model import Scenario, load_json, save_json
from fleetkit.scorer import evaluate


class SelectionTests(unittest.TestCase):
    def test_seed_ranges_and_case_deduplication(self):
        self.assertEqual(bench.parse_seeds('0-2,7,2,-2--1'), [0, 1, 2, 7, -2, -1])
        self.assertEqual(bench.select_cases(levels='L1,L2', seeds='1-2'),
                         [('L1', 1), ('L1', 2), ('L2', 1), ('L2', 2)])
        self.assertEqual(bench.select_cases(['L2:7', 'L2:7']), [('L2', 7)])
        self.assertEqual(len(bench.select_cases()), 12)

    def test_bad_selection(self):
        for text in ['3-1', 'x', '1,,2']:
            with self.assertRaises(ValueError):
                bench.parse_seeds(text)
        with self.assertRaises(ValueError):
            bench.select_cases(['L5:7'])
        with self.assertRaises(ValueError):
            bench.select_cases(['L1:0'], levels='L2')


class SummaryTests(unittest.TestCase):
    def row(self, base, search, search_status='ok', setup='ok'):
        return dict(generation_status=setup, baseline_score=base, search_score=search,
                    baseline_seconds=1, search_seconds=2, improvement=search-base,
                    baseline={'status': 'ok'}, search={'status': search_status})

    def test_failure_scores_count_and_setup_is_excluded(self):
        summary = bench.summarize([self.row(10, 20), self.row(10, 10),
                                   self.row(10, 0, 'timeout'), self.row(500, 500, setup='error')])
        self.assertEqual(summary['baseline_score'], 30)
        self.assertEqual(summary['search_score'], 30)
        self.assertEqual((summary['wins'], summary['ties'], summary['losses']), (1, 1, 1))
        self.assertEqual(summary['search_timeouts'], 1)
        self.assertEqual(summary['search_failures'], 1)
        self.assertEqual(summary['setup_failures'], 1)
        self.assertEqual(summary['search_valid'], 2)
        self.assertEqual(summary['improvement_pct'], 0)
        self.assertIsNone(bench.percent(5, 0))

    def test_gain_uses_summed_scores(self):
        s = bench.summarize([self.row(1, 2), self.row(100, 100)])
        self.assertAlmostEqual(s['improvement_pct'], 100/101)
        self.assertEqual(bench.summarize([])['compared'], 0)


class SubprocessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = generate('L1', 0)
        cls.sc = Scenario.from_dict(cls.data)

    def run_script(self, code, timeout=2):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = folder / 'plan.json'
            script = folder / 'candidate.py'
            script.write_text(code)
            return bench.run_planner([sys.executable, str(script), str(path)], self.sc, path,
                                     folder / 'run.log', timeout, dict(os.environ))

    def test_crash_missing_and_malformed_output(self):
        self.assertEqual(self.run_script('raise RuntimeError("broken")')['status'], 'crash')
        self.assertEqual(self.run_script('pass')['status'], 'no_plan')
        r = self.run_script('import sys\nopen(sys.argv[1], "w").write("{")')
        self.assertEqual(r['status'], 'bad_json')
        self.assertEqual(r['score'], 0)

    def test_invalid_plan(self):
        result = self.run_script('import sys,json\njson.dump({"seed":-999,"routes":[]},open(sys.argv[1],"w"))')
        self.assertEqual(result['status'], 'invalid')
        self.assertEqual(result['score'], 0)

    def test_timeout_terminates_child(self):
        result = self.run_script('import time\ntime.sleep(10)', timeout=.1)
        self.assertEqual(result['status'], 'timeout')
        self.assertEqual(result['score'], 0)
        self.assertLess(result['seconds'], 2)

    def test_valid_plan(self):
        result = self.run_script(f'import sys,json\njson.dump({{"seed":{self.sc.seed},"routes":[]}},open(sys.argv[1],"w"))')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['score'], 0)


class BatchIntegrationTests(unittest.TestCase):
    def run_batch(self, output, *extra):
        return subprocess.run([sys.executable, str(ROOT / 'analysis/benchmark_search.py'),
                               '--levels', 'L1', '--seeds', '0-3', '--seconds', '.25',
                               '--workers', '2', '--output', str(output), *extra],
                              cwd=ROOT, capture_output=True, text=True, timeout=20)

    def test_parallel_batch_writes_inspectable_reports_and_plans(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'batch'
            result = self.run_batch(output)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            summary = load_json(output / 'summary.json')
            self.assertTrue(summary['complete'])
            self.assertEqual(summary['total']['search_valid'], 4)
            self.assertEqual(summary['settings']['workers'], 2)
            self.assertIn('TOTAL', result.stdout)
            self.assertEqual(len((output / 'results.jsonl').read_text().splitlines()), 4)
            self.assertEqual(len((output / 'results.csv').read_text().splitlines()), 5)
            report = (output / 'report.html').read_text()
            self.assertIn('Parallel runs share CPU', report)
            self.assertNotIn('http-equiv="refresh"', report)
            for row in load_json(output / 'results.json'):
                folder = output / row['folder']
                sc = Scenario.from_dict(load_json(folder / 'scenario.json'))
                for name in ['greedy.plan.json', 'search.plan.json']:
                    self.assertTrue(evaluate(sc, load_json(folder / name)).valid)
            # Output directories cannot silently overwrite a previous run.
            again = self.run_batch(output)
            self.assertEqual(again.returncode, 2)
            self.assertIn('already exists', again.stderr)

    def test_one_failed_planner_does_not_stop_other_cases(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            candidate = folder / 'candidate.py'
            candidate.write_text('''import json,sys
scenario=json.load(open(sys.argv[1]))
if scenario['seed']==1: raise RuntimeError('intentional failure')
json.dump({'seed':scenario['seed'],'routes':[]},open(sys.argv[2],'w'))
''')
            output = folder / 'batch'
            proc = self.run_batch(output, '--planner', str(candidate))
            self.assertEqual(proc.returncode, 1, proc.stdout+proc.stderr)
            summary = load_json(output / 'summary.json')
            self.assertTrue(summary['complete'])
            self.assertEqual(summary['completed'], 4)
            self.assertEqual(summary['total']['search_failures'], 1)
            self.assertEqual(summary['total']['search_valid'], 3)
            rows = load_json(output / 'results.json')
            failed = next(r for r in rows if r['seed']==1)
            self.assertEqual(failed['search']['status'], 'crash')
            self.assertEqual(failed['search_score'], 0)
            self.assertLess(failed['improvement'], 0)


if __name__ == '__main__':
    unittest.main()
