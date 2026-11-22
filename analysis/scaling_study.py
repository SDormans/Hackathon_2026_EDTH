"""Planner score and runtime against fleet size: 30, 60 and 100 aircraft.

The kit has four fixed levels; this adds levels N30/N60/N100 to the generator's table (in this
process only, the kit files are not changed). N100 is L4 (15 scouts, 50 light, 35 heavy); smaller
fleets scale every L4 count by n/100 (heavy effectors take the rounding remainder) and the area by sqrt(n/100), so density stays comparable.
Each case runs planner.py in its own process (60 s hard timeout) and the greedy baseline, and
both are scored by the official scorer.

    python3 analysis/scaling_study.py --sizes 30,60,100 --seeds 0-2 --seconds 45
"""
import argparse
import csv
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _kit  # noqa: E402

from fleetkit import generator  # noqa: E402
from fleetkit.baseline import greedy_plan  # noqa: E402
from fleetkit.model import Scenario, load_json, save_json  # noqa: E402
from fleetkit.scorer import evaluate  # noqa: E402

COUNTS = ("scouts", "light", "heavy", "hostile", "unknown", "friendly", "civilian", "zones")


def level_for(n):
    base = generator.LEVELS["L4"]
    f = n / 100.0
    level = dict(base)
    for key in COUNTS:
        level[key] = max(1, int(base[key] * f + 0.5))
    level["heavy"] = n - level["scouts"] - level["light"]
    level["area"] = round(base["area"] * math.sqrt(f))
    lo, hi = base["clusters"]
    level["clusters"] = (max(1, round(lo * f)), max(2, round(hi * f)))
    return level


def parse_seeds(text):
    if "-" in text:
        lo, hi = text.split("-", 1)
        return list(range(int(lo), int(hi) + 1))
    return [int(x) for x in text.split(",")]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sizes", default="30,60,100")
    ap.add_argument("--seeds", default="0-2")
    ap.add_argument("--seconds", type=float, default=45.0)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--work", default=str(ROOT / "output" / "scaling"))
    ap.add_argument("--output", default=str(ROOT / "analysis" / "scaling_study.csv"))
    a = ap.parse_args()
    work = Path(a.work)
    work.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONPATH": str(_kit.KIT)}
    rows = []
    for n in [int(x) for x in a.sizes.split(",")]:
        name = f"N{n}"
        generator.LEVELS[name] = level_for(n)
        for seed in parse_seeds(a.seeds):
            data = generator.generate(name, seed)
            sc = Scenario.from_dict(data)
            case = work / f"{name}-{seed}"
            case.mkdir(exist_ok=True)
            save_json(data, case / "scenario.json")
            t0 = time.monotonic()
            greedy = evaluate(sc, greedy_plan(sc))
            greedy_s = time.monotonic() - t0
            t0 = time.monotonic()
            status = "ok"
            try:
                with open(case / "search.log", "w") as log:
                    proc = subprocess.run([sys.executable, str(ROOT / "planner.py"), str(case / "scenario.json"),
                                           str(case / "plan.json"), "--seconds", str(a.seconds)],
                                          stdout=subprocess.DEVNULL, stderr=log, env=env, timeout=a.timeout)
                if proc.returncode:
                    status = f"exit {proc.returncode}"
            except subprocess.TimeoutExpired:
                status = "timeout"
            ours_s = time.monotonic() - t0
            ours = evaluate(sc, load_json(case / "plan.json")) if status == "ok" else None
            valid = bool(ours and ours.valid)
            row = {"aircraft": len(sc.aircraft), "contacts": len(sc.contacts), "seed": seed,
                   "greedy_score": round(greedy.score, 2), "greedy_s": round(greedy_s, 2),
                   "score": round(ours.score, 2) if valid else 0.0, "valid": int(valid),
                   "runtime_s": round(ours_s, 2), "status": status}
            rows.append(row)
            print(json.dumps(row), flush=True)
    with open(a.output, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {a.output}")


if __name__ == "__main__":
    main()
