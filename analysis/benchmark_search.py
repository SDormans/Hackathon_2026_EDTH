"""Compare the submitted entry point against greedy, using the official scorer.

python3 analysis/benchmark_search.py --seconds 5 --cases L1:0 L2:7 L3:1 L4:0
Generation and baseline timing are outside the planner's measured wall time.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import _kit
from fleetkit.baseline import greedy_plan
from fleetkit.generator import generate
from fleetkit.model import Scenario, save_json
from fleetkit.scorer import evaluate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=5)
    parser.add_argument('--cases', nargs='+', default=['L1:0', 'L2:7', 'L3:1', 'L4:0'])
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(_kit.KIT))
    for case in args.cases:
        level, seed = case.split(':')
        data = generate(level, int(seed))
        sc = Scenario.from_dict(data)
        start = time.monotonic()
        base = evaluate(sc, greedy_plan(sc))
        base_seconds = time.monotonic()-start
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            save_json(data, folder / 'scenario.json')
            start = time.monotonic()
            proc = subprocess.run([sys.executable, str(root / 'planner.py'),
                                   str(folder / 'scenario.json'), str(folder / 'plan.json'),
                                   '--seconds', str(args.seconds)],
                                  cwd=folder, env=env, timeout=60, capture_output=True, text=True)
            took = time.monotonic()-start
            if proc.returncode:
                raise RuntimeError(proc.stderr)
            plan = json.loads((folder / 'plan.json').read_text())
            result = evaluate(sc, plan)
            if not result.valid:
                raise RuntimeError(result.violations)
            print(json.dumps(dict(case=case, baseline_score=base.score,
                                  baseline_seconds=base_seconds, search_score=result.score,
                                  search_seconds=took, improvement=result.score-base.score,
                                  served=result.served)), flush=True)


if __name__ == '__main__':
    main()
