"""Link-loss benchmark on fixed scenarios and plans: every event seed x setting x comm seed.

    python3 analysis/linkloss_bench.py --plans output/ll/plans --event-seeds 1-10 \
        --settings leaderboard --policy policy.py --workers 8 --out output/ll/lb.json
    python3 analysis/linkloss_bench.py ... --settings sizing       # the 20-cell grid

Scenarios are read from validation/<name>.json and plans from <plans>/<name>.json. Events are
drawn with the kit's make_events (cached in <plans>/events/). Each run uses the kit's simulate,
retention and S_loss exactly as fleetkit.linkloss.score.evaluate_cases does.
"""
import argparse
import json
import os
import sys
import time
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _kit  # noqa: E402,F401

from fleetkit.linkloss.events import make_events  # noqa: E402
from fleetkit.linkloss.policies import load_policy  # noqa: E402
from fleetkit.linkloss.score import CAPACITIES, LEADERBOARD, MU, RANGES, SEEDS, THETA, retention  # noqa: E402
from fleetkit.linkloss.sim import simulate  # noqa: E402
from fleetkit.model import Scenario, load_json, save_json  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def seeds(text):
    out = []
    for part in text.split(","):
        if "-" in part:
            lo, hi = part.split("-", 1)
            out += list(range(int(lo), int(hi) + 1))
        else:
            out.append(int(part))
    return out


def events_job(job):
    name, plans, seed = job
    path = Path(plans) / "events" / f"{name}-e{seed}.json"
    if path.exists():
        return name, seed, load_json(path)
    sc = Scenario.from_dict(load_json(ROOT / "validation" / f"{name}.json"))
    try:
        ev = make_events(sc, load_json(Path(plans) / f"{name}.json"), seed)
    except Exception as exc:
        return name, seed, {"error": f"{type(exc).__name__}: {exc}"}
    path.parent.mkdir(parents=True, exist_ok=True)
    save_json(ev, path)
    return name, seed, ev


def run_job(job):
    name, plans, seed, events, policy, setting, comm = job
    sc = Scenario.from_dict(load_json(ROOT / "validation" / f"{name}.json"))
    plan = load_json(Path(plans) / f"{name}.json")
    factory = load_policy(policy)
    r = simulate(sc, plan, events, factory, setting=setting, run_seed=comm)
    return dict(case=name, ev=seed, setting=list(setting), comm=comm, failed=r.failed[:2],
                points=r.points, oracle=events["oracle_points"],
                ret=retention(r.points, bool(r.failed), events["oracle_points"]),
                msgs=r.messages_per_aircraft_per_min, calls=r.policy_calls, secs=r.policy_seconds,
                lost=r.lost, kinds=[e["kind"] for e in events["events"]])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plans", default="output/ll/plans")
    ap.add_argument("--cases", default=",".join(sorted(p.stem for p in (ROOT / "validation").glob("*.json"))))
    ap.add_argument("--event-seeds", default="1-10")
    ap.add_argument("--settings", default="leaderboard", help="leaderboard, sizing, or RxC,RxC")
    ap.add_argument("--comm-seeds", type=int, default=SEEDS)
    ap.add_argument("--policy", default=str(ROOT / "policy.py"))
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    ap.add_argument("--out")
    a = ap.parse_args()
    if a.settings == "leaderboard":
        settings = [LEADERBOARD]
    elif a.settings == "sizing":
        settings = [(r, c) for r in RANGES for c in CAPACITIES]
    else:
        settings = [tuple(float(v) for v in s.split("x")) for s in a.settings.split(",")]
    names = a.cases.split(",")
    start = time.time()
    with Pool(a.workers) as pool:
        evs = pool.map(events_job, [(n, a.plans, s) for n in names for s in seeds(a.event_seeds)])
        good = [(n, s, e) for n, s, e in evs if "error" not in e]
        for n, s, e in evs:
            if "error" in e:
                print(f"events {n} seed {s}: {e['error']}")
        jobs = [(n, a.plans, s, e, os.path.abspath(a.policy), st, k)
                for st in settings for n, s, e in good for k in range(a.comm_seeds)]
        rows = []
        for i, row in enumerate(pool.imap_unordered(run_job, jobs, chunksize=1), 1):
            rows.append(row)
            if i % 200 == 0:
                print(f"  {i}/{len(jobs)} runs, {time.time() - start:.0f} s", flush=True)
    print(f"{len(rows)} runs in {time.time() - start:.0f} s, policy {a.policy}")
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(rows))
    report(rows, settings, names)


def summarise(rows):
    n = len(rows)
    ret = sum(r["ret"] for r in rows) / n
    msgs = sum(r["msgs"] for r in rows) / n
    calls = sum(r["calls"] for r in rows)
    return dict(runs=n, retention=ret, failures=sum(bool(r["failed"]) for r in rows), msgs=msgs,
                s_loss=ret - MU * msgs, ms=1000.0 * sum(r["secs"] for r in rows) / calls if calls else 0.0)


def report(rows, settings, names):
    for st in settings:
        sub = [r for r in rows if tuple(r["setting"]) == tuple(st)]
        print(f"\nsetting {st[0]:g} m x {st[1]:g} B/s")
        print(f"{'case':8} {'runs':>5} {'retention':>9} {'fail':>5} {'msg/ac/min':>10} {'S_loss':>7} {'ms/call':>7}")
        for n in names + ["ALL"]:
            rs = sub if n == "ALL" else [r for r in sub if r["case"] == n]
            if not rs:
                continue
            s = summarise(rs)
            print(f"{n:8} {s['runs']:>5} {s['retention']:>9.3f} {s['failures']:>5} {s['msgs']:>10.2f} "
                  f"{s['s_loss']:>7.3f} {s['ms']:>7.3f}")
        for r in sub:
            if r["failed"]:
                print(f"  FAIL {r['case']} ev{r['ev']} comm{r['comm']}: {r['failed'][0]}")
    if len(settings) > 1:
        meets = 0
        print("\nsizing grid: retention (* = meets: >= 0.9 and no failure)")
        caps = sorted({c for _, c in settings})
        print("range  " + "".join(f"{c:>9g}" for c in caps))
        for rg in sorted({r for r, _ in settings}):
            line = ""
            for c in caps:
                cell = [r for r in rows if tuple(r["setting"]) == (rg, c)]
                if not cell:
                    line += f"{'-':>9}"
                    continue
                s = summarise(cell)
                ok = s["retention"] >= THETA and s["failures"] == 0
                meets += ok
                line += f"{s['retention']:>8.3f}{'*' if ok else ' '}"
            print(f"{rg:>5g}  {line}")
        print(f"sizing_score {meets / len(settings):.2f}")


if __name__ == "__main__":
    main()
