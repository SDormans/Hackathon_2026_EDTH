"""Does plan fragility predict link-loss retention? Several valid plans per scenario, each
scored by search_planner.plan_fragility and by the link-loss harness (policy.py, LEADERBOARD).

    python3 analysis/fragility_correlation.py --cases L3:0 L4:1 --beam-widths 6,3 --seconds 20 --workers 4

Plans: the planner's ranked archive (--artifacts) for each beam width, deduplicated, kept only
when the official scorer accepts them. Events: make_events(scenario, plan, seed + 1), as
check_policy.py. Correlations are within-scenario (Spearman on ranks centred per scenario).
"""
import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys

import _kit
from fleetkit.generator import generate
from fleetkit.linkloss.events import make_events
from fleetkit.linkloss.policies import load_policy
from fleetkit.linkloss.score import LEADERBOARD, MU, oracle_points, retention
from fleetkit.linkloss.sim import simulate
from fleetkit.model import Scenario, load_json, save_json
from fleetkit.scorer import evaluate

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import search_planner  # noqa: E402


def plan_case(task):
    level, seed, width, seconds, out = task
    folder = Path(out) / f"{level}-{seed}" / f"w{width}"
    folder.mkdir(parents=True, exist_ok=True)
    scn = folder.parent / "scenario.json"
    if not scn.exists():
        save_json(generate(level, seed), scn)
    env = dict(os.environ, PYTHONPATH=str(_kit.KIT))
    subprocess.run([sys.executable, str(ROOT / "planner.py"), str(scn), str(folder / "plan.json"),
                    "--seconds", str(seconds), "--beam-width", str(width), "--artifacts", str(folder / "art")],
                   env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=70)
    return sorted(str(p) for p in (folder / "art").glob("plan-*.json"))


def score_plan(task):
    level, seed, path, policy, comm_seeds, settings = task
    sc = Scenario.from_dict(generate(level, seed))
    plan = load_json(path)
    res = evaluate(sc, plan)
    if not res.valid:
        return None
    row = {"case": f"{level}-{seed}", "plan": path, "plan_score": res.score,
           **search_planner.fragility_parts(plan, sc), "fragility": search_planner.plan_fragility(plan, sc)}
    try:
        ev = make_events(sc, plan, seed + 1)
    except RuntimeError as exc:
        return {**row, "skipped": str(exc)}
    oracle = oracle_points(sc, plan, ev)
    factory = load_policy(policy)
    for setting in settings:
        pts, rets, msgs, fails = [], [], [], 0
        for k in range(comm_seeds):
            run = simulate(sc, plan, ev, factory, setting=setting, run_seed=k)
            pts.append(0.0 if run.failed else run.points)
            rets.append(retention(run.points, bool(run.failed), oracle))
            msgs.append(run.messages_per_aircraft_per_min)
            fails += bool(run.failed)
        tag = "" if setting == LEADERBOARD else f"_{setting[0]:g}x{setting[1]:g}"
        ret = statistics.mean(rets)
        row.update({f"retention{tag}": ret, f"retained_points{tag}": statistics.mean(pts),
                    f"failures{tag}": fails, f"s_loss{tag}": ret - MU * statistics.mean(msgs)})
    return {**row, "oracle_points": oracle, "runs": comm_seeds}


def ranks(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2.0
        i = j + 1
    return out


def within_spearman(rows, x, y):
    """Pearson on per-scenario centred ranks; ignores scenarios without variation in x or y."""
    xs, ys = [], []
    for case in sorted({r["case"] for r in rows}):
        grp = [r for r in rows if r["case"] == case and r.get(x) is not None and r.get(y) is not None]
        if len(grp) < 2:
            continue
        rx, ry = ranks([r[x] for r in grp]), ranks([r[y] for r in grp])
        mx, my = statistics.mean(rx), statistics.mean(ry)
        xs += [v - mx for v in rx]
        ys += [v - my for v in ry]
    sxx, syy = sum(v * v for v in xs), sum(v * v for v in ys)
    if not sxx or not syy:
        return None, len(xs)
    return sum(a * b for a, b in zip(xs, ys)) / (sxx * syy) ** 0.5, len(xs)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", nargs="+", required=True)
    ap.add_argument("--beam-widths", default="6,3")
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--policy", default=str(ROOT / "policy.py"))
    ap.add_argument("--comm-seeds", type=int, default=3)
    ap.add_argument("--extra-settings", default="", help="also score at RANGExCAPACITY settings, e.g. 250x50")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--output", default=str(ROOT / "output" / "fragility-correlation"))
    a = ap.parse_args(argv)
    cases = [(c.split(":")[0], int(c.split(":")[1])) for c in a.cases]
    widths = [int(w) for w in a.beam_widths.split(",")]
    with ProcessPoolExecutor(a.workers) as pool:
        tasks = [(l, s, w, a.seconds, a.output) for l, s in cases for w in widths]
        plans = {}
        for (l, s, w, _, _), paths in zip(tasks, pool.map(plan_case, tasks)):
            for p in paths:
                digest = hashlib.sha256(Path(p).read_bytes()).hexdigest()
                plans.setdefault((l, s, digest), p)
        print(f"{len(plans)} distinct plans", flush=True)
        settings = [LEADERBOARD] + [tuple(float(v) for v in x.split("x")) for x in a.extra_settings.split(",") if x]
        jobs = [(l, s, p, a.policy, a.comm_seeds, settings) for (l, s, _), p in sorted(plans.items())]
        rows = [r for r in pool.map(score_plan, jobs) if r is not None and "skipped" not in r]
    Path(a.output, "rows.json").write_text(json.dumps(rows, indent=1) + "\n")
    ys = [k for k in rows[0] if k.startswith(("retention", "retained_points", "failures", "s_loss"))] if rows else []
    print(f"{'case':8} {'score':>8} {'frag':>7} {'tight':>5} {'near':>4} {'gap':>6}  " + " ".join(f"{y[:18]:>18}" for y in ys))
    for r in sorted(rows, key=lambda r: (r["case"], -r["plan_score"])):
        print(f"{r['case']:8} {r['plan_score']:8.1f} {r['fragility']:7.2f} {r['tight']:5} {r['near_pairs']:4} "
              f"{(r['mean_gap_s'] or 0):6.1f}  " + " ".join(f"{r[y]:18.3f}" for y in ys))
    print("\nwithin-scenario Spearman (negative fragility-retention = sturdier plans retain more):")
    for x in ("fragility", "tight", "near_pairs", "mean_gap_s", "plan_score"):
        for y in ys:
            rho, n = within_spearman(rows, x, y)
            print(f"  {x:>10} vs {y:<28} rho {'n/a' if rho is None else f'{rho:+.3f}'}  (n={n})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
