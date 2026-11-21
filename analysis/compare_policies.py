"""Paired comparison of link-loss policies on the same scenarios, events and communication seeds.

    FLEETKIT_DIR=/path/to/fleet-kit python analysis/compare_policies.py policy.py policy_v2.py \
        --levels L1,L2,L3,L4 --seeds 20-29 --settings 1000x200,500x200,250x200,250x50 --comm-seeds 2

The kit must include `fleetkit.linkloss` (the updated official kit). Plans are the kit's greedy
plans and events use seed + 1, as in handin/check_policy.py.
"""
import argparse
import os
import sys
import time
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _kit  # noqa: E402,F401

from fleetkit.baseline import greedy_plan  # noqa: E402
from fleetkit.generator import generate  # noqa: E402
from fleetkit.linkloss.events import make_events  # noqa: E402
from fleetkit.linkloss.policies import load_policy  # noqa: E402
from fleetkit.linkloss.score import MU, oracle_points, retention  # noqa: E402
from fleetkit.linkloss.sim import simulate  # noqa: E402
from fleetkit.model import Scenario  # noqa: E402


def parse_seeds(text):
    if "-" in text:
        lo, hi = text.split("-", 1)
        return list(range(int(lo), int(hi) + 1))
    return [int(x) for x in text.split(",")]


def run_case(job):
    level, seed, policies, settings, comm_seeds = job
    sc = Scenario.from_dict(generate(level, seed))
    plan = greedy_plan(sc)
    try:
        events = make_events(sc, plan, seed + 1)
        v_oracle = oracle_points(sc, plan, events)
    except Exception as exc:  # no usable events for this case
        return level, seed, None, f"{type(exc).__name__}: {exc}"
    factories = {p: load_policy(p) for p in policies}
    rows = []
    for setting in settings:
        for k in range(comm_seeds):
            for p in policies:
                r = simulate(sc, plan, events, factories[p], setting=setting, run_seed=k)
                rows.append(dict(policy=p, setting=setting, comm=k, failed=r.failed[:1],
                                 ret=retention(r.points, bool(r.failed), v_oracle),
                                 msgs=r.messages_per_aircraft_per_min,
                                 ms=1000.0 * r.policy_seconds / max(1, r.policy_calls)))
    return level, seed, rows, [e["kind"] for e in events["events"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("policies", nargs="+")
    ap.add_argument("--levels", default="L1,L2,L3,L4")
    ap.add_argument("--seeds", default="20-29")
    ap.add_argument("--settings", default="1000x200,500x200,250x200,250x50")
    ap.add_argument("--comm-seeds", type=int, default=2)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--json", help="also write every paired run to this JSON file")
    a = ap.parse_args()
    policies = [str(Path(p).resolve()) for p in a.policies]
    names = {p: Path(p).name for p in policies}
    settings = [tuple(float(v) for v in s.split("x")) for s in a.settings.split(",")]
    jobs = [(lv, s, policies, settings, a.comm_seeds) for lv in a.levels.split(",") for s in parse_seeds(a.seeds)]
    began = time.time()
    with Pool(a.workers) as pool:
        results = pool.map(run_case, jobs)
    rows, skipped = [], []
    for level, seed, rs, info in results:
        if rs is None:
            skipped.append(f"{level}-{seed}: {info}")
            continue
        for r in rs:
            r["case"] = f"{level}-{seed}"
            rows.append(r)

    print(f"{len({r['case'] for r in rows})} cases, {len(skipped)} skipped, {time.time() - began:.0f} s\n")
    head = f"{'setting':>10}  {'policy':<16} {'retention':>9} {'failed':>8} {'msg/ac/min':>10} {'S_loss':>7} {'meets':>5} {'ms/call':>7}"
    print(head)
    print("-" * len(head))
    for setting in settings:
        for p in policies:
            sel = [r for r in rows if r["policy"] == p and r["setting"] == setting]
            ret = sum(r["ret"] for r in sel) / len(sel)
            msgs = sum(r["msgs"] for r in sel) / len(sel)
            fails = sum(bool(r["failed"]) for r in sel)
            ms = sum(r["ms"] for r in sel) / len(sel)
            meets = "yes" if ret >= 0.9 and fails == 0 else "no"
            print(f"{setting[0]:>5g}x{setting[1]:<4g}  {names[p]:<16} {ret:>9.3f} {fails:>4}/{len(sel):<3} "
                  f"{msgs:>10.1f} {ret - MU * msgs:>7.3f} {meets:>5} {ms:>7.2f}")
        print()

    if len(policies) == 2:
        base, new = policies
        key = lambda r: (r["case"], r["setting"], r["comm"])  # noqa: E731
        b = {key(r): r for r in rows if r["policy"] == base}
        n = {key(r): r for r in rows if r["policy"] == new}
        better = [(k, b[k]["ret"], n[k]["ret"]) for k in b if n[k]["ret"] > b[k]["ret"] + 1e-9]
        worse = [(k, b[k]["ret"], n[k]["ret"]) for k in b if n[k]["ret"] < b[k]["ret"] - 1e-9]
        print(f"paired runs: {names[new]} better in {len(better)}, worse in {len(worse)}, "
              f"same in {len(b) - len(better) - len(worse)} of {len(b)}")
        for label, lst in (("worse", worse), ("better", better)):
            for k, rb, rn in sorted(lst, key=lambda x: x[2] - x[1])[:12]:
                why = n[k]["failed"] or b[k]["failed"]
                print(f"  {label}: {k[0]} {k[1][0]:g}x{k[1][1]:g} seed {k[2]}: {rb:.3f} -> {rn:.3f}"
                      + (f"   ({why[0][:90]})" if why else ""))
    for s in skipped:
        print("skipped", s)
    if a.json:
        import json
        out = dict(policies=[names[p] for p in policies], levels=a.levels, seeds=a.seeds,
                   settings=[list(s) for s in settings], comm_seeds=a.comm_seeds, workers=a.workers,
                   seconds=round(time.time() - began, 1), skipped=skipped,
                   rows=[dict(r, policy=names[r["policy"]], setting=list(r["setting"])) for r in rows])
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json).write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
