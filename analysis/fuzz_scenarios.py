"""Fuzz the planner: small random perturbations of base scenarios, each planned and
validated with the official scorer. Any crash, timeout or invalid plan is a bug.

Perturbations (one to three per case): contact windows shifted/stretched by 10-60 s,
zone radii scaled by +-10%, zone centres nudged up to 100 m, horizon scaled by +-5%,
aircraft energy (capacity) and speeds scaled by +-10%.

    python3 analysis/fuzz_scenarios.py --levels L1,L2,L3,L4 --seed 50 --cases 50 \
        --seconds 15 --full-cases 3 --workers 4
"""
import argparse
import copy
import json
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analysis"))
import _kit  # noqa: E402,F401  (local fleetkit discovery)
from fleetkit.generator import generate  # noqa: E402
from robust_sweep import run_case  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402


def jitter_windows(d, rng):
    for c in d["contacts"]:
        w = c.get("window_s")
        if w and rng.random() < 0.5:
            lo = w[0] + rng.choice((-1, 1)) * rng.uniform(10, 60)
            hi = w[1] + rng.choice((-1, 1)) * rng.uniform(10, 60)
            lo = max(0.0, lo)
            c["window_s"] = [round(lo, 1), round(max(lo, hi), 1)]
    return "windows"


def scale_zones(d, rng):
    for z in d["zones"]:
        z["radius_m"] = round(z["radius_m"] * rng.uniform(0.9, 1.1), 1)
    return "zone-radius"


def nudge_zones(d, rng):
    for z in d["zones"]:
        z["center"] = [round(v + rng.uniform(-100, 100), 1) for v in z["center"]]
    return "zone-center"


def shift_horizon(d, rng):
    d["horizon_s"] = round(d["horizon_s"] * rng.uniform(0.95, 1.05), 1)
    return "horizon"


def tweak_aircraft(d, rng):
    for a in d["aircraft"]:
        if rng.random() < 0.5:
            a["capacity_wh"] = round(a["capacity_wh"] * rng.uniform(0.9, 1.1), 2)
        if rng.random() < 0.5:
            f = rng.uniform(0.9, 1.1)
            for key in ("v_h_mps", "v_up_mps", "v_down_mps"):
                a[key] = round(a[key] * f, 2)
    return "aircraft"


PERTURBATIONS = (jitter_windows, scale_zones, nudge_zones, shift_horizon, tweak_aircraft)


def perturb(base, rng):
    d = copy.deepcopy(base)
    applied = [p(d, rng) for p in rng.sample(PERTURBATIONS, rng.randint(1, 3))]
    return d, applied


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--levels", default="L1,L2,L3,L4")
    parser.add_argument("--seed", type=int, default=50, help="generator seed of the base scenarios")
    parser.add_argument("--cases", type=int, default=50, help="perturbed cases per level")
    parser.add_argument("--fuzz-seed", type=int, default=0)
    parser.add_argument("--seconds", type=float, default=15.0)
    parser.add_argument("--full-cases", type=int, default=3,
                        help="cases per level also run at --full-seconds")
    parser.add_argument("--full-seconds", type=float, default=45.0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out", type=Path, default=ROOT / "output" / "fuzz")
    args = parser.parse_args()
    scen_dir = args.out / "scenarios"
    plan_dir = args.out / "plans"
    scen_dir.mkdir(parents=True, exist_ok=True)
    plan_dir.mkdir(parents=True, exist_ok=True)
    jobs = []
    for level in args.levels.split(","):
        base = generate(level, args.seed)
        rng = random.Random(f"fuzz-{args.fuzz_seed}-{level}-{args.seed}")
        for k in range(args.cases):
            d, applied = perturb(base, rng)
            name = f"{level}-{args.seed}-f{args.fuzz_seed}-{k:03d}"
            path = scen_dir / f"{name}.json"
            path.write_text(json.dumps(d))
            jobs.append((path, args.seconds, applied))
            if k < args.full_cases:
                full = scen_dir / f"{name}-full.json"
                full.write_text(json.dumps(d))
                jobs.append((full, args.full_seconds, applied))
    # Longest jobs first keeps the pool busy.
    jobs.sort(key=lambda j: -j[1])
    results = []
    with ThreadPoolExecutor(args.workers) as pool:
        futures = [(pool.submit(run_case, p, s, 60.0, plan_dir), applied)
                   for p, s, applied in jobs]
        for f, applied in futures:
            r = f.result()
            r["perturbations"] = applied
            results.append(r)
            print(f"{r['case']:28s} {r['seconds']:4.0f}s {r['status']:8s} wall {r.get('wall_s', 0):6.2f} s "
                  f"routes {r.get('routes', '-'):>3} score {r.get('score', '-'):>9} "
                  f"{','.join(applied)} {r['detail']}", flush=True)
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    slowest = max(results, key=lambda r: r.get("wall_s", 0))
    print(f"\n{len(results)} cases: {counts}; empty plans {sum(1 for r in results if r.get('empty'))}; "
          f"slowest {slowest['case']} ({slowest['seconds']:g} s budget) {slowest.get('wall_s')} s")
    (args.out / "results.json").write_text(json.dumps(results, indent=1))
    return 0 if all(r["status"] == "ok" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
