"""The knowledge recorder must not change a run, and must attribute facts to their source."""
import json
import re
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'analysis'))
import linkloss_knowledge as lk
from fleetkit.linkloss.policies import load_policy
from fleetkit.linkloss.sim import simulate


class RecorderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.case = lk.prepare_case('L2', 7, 1, Path(cls.tmp.name) / 'case.json.gz')

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _load(self):
        return lk.load_case(self.case)

    def test_recording_does_not_change_the_run(self):
        sc, plan, events = self._load()
        for spec in (str(ROOT / 'policy.py'), 'broadcast'):
            factory = load_policy(spec)
            plain = simulate(sc, plan, events, factory, run_seed=0)
            rec = lk.Recorder()
            wrapped = simulate(sc, plan, events, lambda s, p, a: lk.Recorded(factory(s, p, a), a, rec), run_seed=0)
            self.assertEqual((plain.failed, plain.points, plain.messages),
                             (wrapped.failed, wrapped.points, wrapped.messages), spec)
            self.assertTrue(rec.steps and rec.facts)

    def test_run_is_stored_and_attributes_sources(self):
        sharing = Path(self.tmp.name) / 'policy_sharing.py'      # news sharing is off by default
        src = (ROOT / 'policy.py').read_text()
        sharing.write_text(re.sub(r'^NEWS_MODE = .*$', 'NEWS_MODE = "forward"', src, flags=re.M))
        run_path = Path(self.tmp.name) / 'runs' / 'L2-7_1000x200_c0.json.gz'
        summary = lk.run_one((str(self.case), str(sharing), (1000.0, 200.0), 0, str(run_path), 200.0))
        run = lk.load_run(run_path)
        self.assertEqual(set(run), {'schema', 'summary', 'facts', 'steps', 'near'})
        self.assertEqual(run['summary']['retention'], summary['retention'])
        sources = {f['source'] for f in run['facts']}
        self.assertLessEqual(sources, {'self', 'plan', 'sensed', 'heard', 'inferred'})
        self.assertIn('sensed', sources)
        heard_rc = [f for f in run['facts'] if f['fact'].startswith('rc:') and f['source'] == 'heard']
        self.assertTrue(heard_rc, 'policy.py shares reclassifications')
        drones = {st['drone'] for st in run['steps']}
        for f in heard_rc:     # one or more carriers (two peers can send it in the same second)
            self.assertTrue(f.get('from') and set(f['from'].split(',')) <= drones, f)
        self.assertTrue([r for r in summary['event_learning'] if r.get('affected')])

    def test_collection_is_stored_with_provenance_and_compared(self):
        out = Path(self.tmp.name) / 'store'
        self.assertEqual(lk.main(['--cases', 'L1:0', '--comm-seeds', '1', '--note', 'test',
                                  '--output', str(out / 'c1')]), 0)
        col = json.loads((out / 'c1' / 'collection.json').read_text())
        self.assertEqual(col['status'], 'complete')
        self.assertEqual(col['note'], 'test')
        self.assertEqual(len(col['provenance']['policy']['sha256']), 64)
        self.assertEqual((out / 'c1' / 'policy.py').read_bytes(), (ROOT / 'policy.py').read_bytes())
        self.assertIn('1000x200', col['by_setting'])
        self.assertEqual(len(list((out / 'c1' / 'runs').glob('*.json.gz'))), 1)
        self.assertIn('| c1 |', lk.compare(out))


class WireFormatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location('team_policy_codec', ROOT / 'policy.py')
        cls.p = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.p)

    def test_round_trip_and_size(self):
        p = self.p
        recs = [('V', 363.5, 0.5), ('V', 12.3, -1.0), ('O', 4, 481), ('R', 9), ('Z', 2440, 900, 83)]
        text = p.encode_records(recs)
        self.assertEqual(p.decode_records(text), recs)
        self.assertLessEqual(len(json.dumps(text, separators=(',', ':')).encode()), 64)
        self.assertNotIn('"', text)
        self.assertNotIn('\\', text)

    def test_wide_contact_numbers(self):
        p = self.p
        recs = [('R', 120), ('O', 200, 33)]
        self.assertEqual(p.decode_records(p.encode_records(recs, cw=2), cw=2), recs)
        self.assertEqual(p.encode_records([('R', 120)], cw=1), '', 'out of range is left out, never clamped')

    def test_bad_input_never_raises(self):
        p = self.p
        for bad in ('Q12', 'Z1', 'R', 42, None, {'r': [1, 2]}, 'R\u00e9'):
            self.assertEqual(p.decode_records(bad), [])
        self.assertEqual(p.encode_records([('Z', 99999, 0, 80)]), '', 'zone outside the coordinate range')
        self.assertLessEqual(len(p.encode_records([('Z', 100, 100, 80)] * 20)), p.MAX_TEXT)


    def test_separation_failure_is_parsed(self):
        m = lk.SEP_RE.search('R8 (separation): H02 and L03 come within 1.3 m of each other (limit 40 m) around t = 440.0 s')
        self.assertEqual(m.groups(), ('H02', 'L03', '1.3', '440.0'))


if __name__ == '__main__':
    unittest.main()
