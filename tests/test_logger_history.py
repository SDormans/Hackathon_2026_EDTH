"""History preservation and comparable-run integration checks."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LoggerTests(unittest.TestCase):
    def test_history_comparison_and_failure_preservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            history = Path(tmp) / 'history'
            def run(*extra):
                return subprocess.run([sys.executable, str(ROOT / 'test_logger.py'),
                    '--history', str(history), '--cases', 'L1:0', '--seconds', '0.1', *extra],
                    capture_output=True, text=True, timeout=30)
            first = run('--note', 'before')
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            original = next(history.glob('*/result.json'))
            contents = original.read_bytes()
            second = run('--note', 'after')
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertEqual(original.read_bytes(), contents)
            saved = [json.loads(p.read_text()) for p in sorted(history.glob('*/result.json'))]
            self.assertEqual(len(saved), 2)
            self.assertEqual(saved[1]['comparison']['previous_run_id'], saved[0]['run_id'])
            self.assertEqual(len(saved[1]['results']), 1)
            self.assertEqual(saved[1]['schema_version'], 2)
            self.assertIn('contacts', saved[1]['results'][0]['diagnostics'])
            different = run('--seconds', '0.2')
            self.assertEqual(different.returncode, 0, different.stderr)
            latest = json.loads(sorted(history.glob('*/result.json'))[-1].read_text())
            self.assertIsNone(latest['comparison'])
            broken = Path(tmp) / 'broken.py'
            broken.write_text('raise RuntimeError("test crash")')
            failed = run('--planner', str(broken))
            self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
            latest = json.loads(sorted(history.glob('*/result.json'))[-1].read_text())
            self.assertEqual(latest['status'], 'failed')
            self.assertEqual(latest['benchmark']['total']['search_failures'], 1)
            self.assertEqual(latest['results'][0]['search_score'], 0)
            export = subprocess.run([sys.executable, str(ROOT / 'test_logger.py'), 'summary',
                '--history', str(history)], capture_output=True, text=True, timeout=10)
            self.assertEqual(len([json.loads(line) for line in export.stdout.splitlines()]), 4)
            contacts = subprocess.run([sys.executable, str(ROOT / 'test_logger.py'), 'contacts',
                '--history', str(history)], capture_output=True, text=True, timeout=10)
            self.assertEqual(contacts.returncode, 0, contacts.stderr)
            report = json.loads(contacts.stdout)
            self.assertEqual(report['cases_with_detail'], 3)
            self.assertEqual(report['cases_without_detail'], 1)


if __name__ == '__main__':
    unittest.main()
