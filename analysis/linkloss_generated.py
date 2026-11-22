"""Scores link-loss policies on generated scenarios, greedy plans and verified events.

    python3 analysis/linkloss_generated.py --levels L1,L2,L3,L4 --seeds 0-4 --policies policy.py,broadcast,hold
"""
import argparse
import json
import sys
import time
from pathlib import Path

import _kit  # noqa: F401  (fleetkit discovery)
from fleetkit.baseline import greedy_plan
from fleetkit.generator import generate
from fleetkit.linkloss.events import make_events
from fleetkit.linkloss.policies import load_policy
from fleetkit.linkloss.score import evaluate_cases, oracle_points, sizing
from fleetkit.model import Scenario, load_json


def seeds(text):
    lo, _, hi = text.partition("-")
    return list(range(int(lo), int(hi or lo) + 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", default="L2,L3")
    ap.add_argument("--seeds", default="0-2")
    ap.add_argument("--policies", default=str(Path(__file__).resolve().parent.parent / "policy.py") + ",broadcast")
    ap.add_argument("--comm-seeds", type=int, default=2)
    ap.add_argument("--plans", help="directory with <level>-<seed>.json plans instead of greedy")
    ap.add_argument("--sizing", action="store_true")
    a = ap.parse_args()
    cases = []
    for lv in a.levels.split(","):
        for s in seeds(a.seeds):
            sc = Scenario.from_dict(generate(lv, s))
            plan = load_json(f"{a.plans}/{lv}-{s}.json") if a.plans else greedy_plan(sc)
            try:
                cases.append((sc, plan, make_events(sc, plan, s + 1)))
            except RuntimeError as exc:
                print(f"{lv}-{s}: skipped ({exc})", file=sys.stderr)
    oracles = [oracle_points(*c) for c in cases]
    for spec in a.policies.split(","):
        factory = load_policy(spec)
        start = time.time()
        if a.sizing:
            res = sizing(cases, factory, seeds=a.comm_seeds)
            print(spec, "sizing", round(res["sizing_score"], 3), "frontier", res["frontier"],
                  f"{time.time() - start:.0f}s")
            for name, cell in res["cells"].items():
                if not cell["meets"]:
                    print(f"    {name}: retention {cell['retention']:.3f} failed {cell['failures']}/{cell['runs']}",
                          cell["failure_examples"][:2])
            continue
        res = evaluate_cases(cases, factory, seeds=a.comm_seeds, oracles=oracles)
        print(f"{Path(spec).name:12s} S_loss {res['s_loss']:.3f} retention {res['retention']:.3f} "
              f"failed {res['failures']}/{res['runs']} msgs/ac/min {res['messages_per_aircraft_per_min']:.2f} "
              f"ms/call {res['ms_per_call']:.2f} ({time.time() - start:.0f}s)")
        for ex in res["failure_examples"]:
            print("   ", ex)


if __name__ == "__main__":
    main()
