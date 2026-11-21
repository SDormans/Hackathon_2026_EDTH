"""Link-loss evaluation harness: leaderboard setting, sizing grid, CSV and SVG output.

Cases are (scenario, plan, events) for the 8 validation scenarios (greedy plans, as in
handin/check_policy.py) and a set of event seeds. The held-out event seeds (default 1001-1003)
were never used to tune policy.py; use other seeds (e.g. 501-502) while developing.

    python3 analysis/linkloss_eval.py run --policy policy.py --name phase5 --workers 8
    python3 analysis/linkloss_eval.py run --policy policy.py:HANDOVER=False --name no-handover
    python3 analysis/linkloss_eval.py run --policy return --name return
    python3 analysis/linkloss_eval.py report        # variant table, budgets that beat continue/return, SVG

`--policy` is a kit baseline name (continue, hold, return, broadcast) or a path to a policy file,
optionally followed by `:NAME=VALUE,...` to override module constants (Python literals).

Every cell is computed with `fleetkit.linkloss.score.evaluate_cases` (5 communication seeds),
split per case across worker processes and summed back; meets/frontier/sizing_score follow
`fleetkit.linkloss.score.sizing` exactly (`--check` recomputes one variant serially with `sizing()`).
The leaderboard numbers are the 1000x200 cell. Output (analysis/linkloss/):

    <name>.csv           one row per grid cell (range, capacity, retention, failures, ...)
    variants.csv         one summary row per variant (latest run wins)
    linkloss_sizing.svg  retention against capacity per range, one line per variant (from `report`)
"""
import argparse
import ast
import csv
import gzip
import hashlib
import importlib.util
import json
import os
import pickle
import sys
import time
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _kit  # noqa: E402,F401

from fleetkit.baseline import greedy_plan  # noqa: E402
from fleetkit.linkloss.events import make_events  # noqa: E402
from fleetkit.linkloss.policies import BASELINES  # noqa: E402
from fleetkit.linkloss.score import (CAPACITIES, LEADERBOARD, MU, RANGES, SEEDS, THETA,  # noqa: E402
                                     evaluate_cases, oracle_points, sizing)
from fleetkit.model import Scenario, load_json  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
VALIDATION = sorted((ROOT / "validation").glob("*.json"))
OUT = ROOT / "analysis" / "linkloss"
CACHE = ROOT / "output" / "linkloss_eval"
HELD_OUT = "1001-1003"


def parse_seeds(text):
    out = []
    for part in text.split(","):
        if "-" in part:
            lo, hi = part.split("-", 1)
            out += list(range(int(lo), int(hi) + 1))
        elif part:
            out.append(int(part))
    return out


# --------------------------------------------------------------------------- cases

def _build(job):
    path, seed = job
    sc = Scenario.from_dict(load_json(str(path)))
    plan = greedy_plan(sc)
    try:
        ev = make_events(sc, plan, seed)
    except RuntimeError as exc:
        return path.stem, seed, None, str(exc)
    return path.stem, seed, (sc, plan, ev, oracle_points(sc, plan, ev)), None


def build_cases(event_seeds, workers):
    """Returns [(label, scenario, plan, events, v_oracle)], cached under output/ by inputs."""
    key = hashlib.sha256(json.dumps([[p.name, p.read_bytes().hex()[:64]] for p in VALIDATION]
                                    + [event_seeds, str(_kit.KIT)]).encode()).hexdigest()[:12]
    cache = CACHE / f"cases-{key}.pkl.gz"
    if cache.exists():
        with gzip.open(cache, "rb") as fh:
            return pickle.load(fh)
    jobs = [(p, s) for p in VALIDATION for s in event_seeds]
    with Pool(workers) as pool:
        built = pool.map(_build, jobs)
    cases = []
    for name, seed, case, err in built:
        if case is None:
            print(f"  skipped {name} events {seed}: {err}", file=sys.stderr)
            continue
        cases.append((f"{name}:e{seed}",) + case)
    CACHE.mkdir(parents=True, exist_ok=True)
    with gzip.open(cache, "wb") as fh:
        pickle.dump(cases, fh)
    return cases


# --------------------------------------------------------------------------- policies

_FACTORIES = {}


def load_factory(spec):
    """Policy class from a baseline name or `path[:NAME=VALUE,...]`, loaded as its own module."""
    if spec in _FACTORIES:
        return _FACTORIES[spec]
    if spec in BASELINES:
        factory = BASELINES[spec]
    else:
        path, _, overrides = spec.partition(":")
        path = os.path.abspath(path)
        sys.path.insert(0, os.path.dirname(path))
        name = "policy_" + hashlib.sha1(spec.encode()).hexdigest()[:10]
        module_spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        for item in filter(None, overrides.split(",")):
            k, v = item.split("=", 1)
            if not hasattr(module, k):
                raise SystemExit(f"{path} has no constant {k}")
            setattr(module, k, ast.literal_eval(v))
        factory = module.Policy
    _FACTORIES[spec] = factory
    return factory


# --------------------------------------------------------------------------- evaluation

_CASES = None


def _init(cases):
    global _CASES
    _CASES = cases


def _cell_case(job):
    spec, i, setting, seeds = job
    label, sc, plan, ev, v = _CASES[i]
    res = evaluate_cases([(sc, plan, ev)], load_factory(spec), setting, seeds, oracles=[v])
    return setting, i, res


def evaluate_grid(spec, cases, workers, seeds=SEEDS, ranges=RANGES, capacities=CAPACITIES):
    """`sizing()` semantics, parallel over (cell, case); returns (cells, sizing_score, frontier, per_case)."""
    settings = [(r, c) for r in ranges for c in capacities]
    jobs = [(spec, i, s, seeds) for s in settings for i in range(len(cases))]
    with Pool(workers, initializer=_init, initargs=(cases,)) as pool:
        results = pool.map(_cell_case, jobs, chunksize=1)
    cells, per_case = {}, []
    for s in settings:
        parts = [(i, res) for (ss, i, res) in results if ss == s]
        runs = sum(r["runs"] for _, r in parts)
        ret = sum(r["retention"] * r["runs"] for _, r in parts) / runs
        msgs = sum(r["messages_per_aircraft_per_min"] * r["runs"] for _, r in parts) / runs
        calls_ms = [r["ms_per_call"] for _, r in parts]
        fails = sum(r["failures"] for _, r in parts)
        examples = [f"{cases[i][0]}: {e.split(': ', 1)[-1]}" for i, r in parts for e in r["failure_examples"]]
        cells[s] = {"retention": ret, "failures": fails, "runs": runs, "messages_per_aircraft_per_min": msgs,
                    "s_loss": ret - MU * msgs, "ms_per_call": max(calls_ms) if calls_ms else 0.0,
                    "meets": ret >= THETA and fails == 0, "failure_examples": examples}
        for i, r in parts:
            per_case.append((s, cases[i][0], r["retention"], r["failures"], r["messages_per_aircraft_per_min"]))
    meets = sum(1 for c in cells.values() if c["meets"])
    frontier = {}
    for r in ranges:
        ok = [c for c in capacities if cells[(r, c)]["meets"]]
        frontier[r] = min(ok) if ok else None
    return cells, meets / len(cells), frontier, per_case


def frontier_text(frontier):
    return " ".join(f"{r:g}:{'-' if c is None else f'{c:g}'}" for r, c in frontier.items())


def write_variant(name, spec, event_seeds, cases, cells, score, frontier, per_case, seconds):
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / f"{name}.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["range_m", "capacity_Bps", "retention", "failures", "runs", "msgs_per_ac_min", "s_loss",
                    "meets", "ms_per_call", "failure_examples"])
        for (r, c), x in cells.items():
            w.writerow([f"{r:g}", f"{c:g}", f"{x['retention']:.4f}", x["failures"], x["runs"],
                        f"{x['messages_per_aircraft_per_min']:.3f}", f"{x['s_loss']:.4f}", int(x["meets"]),
                        f"{x['ms_per_call']:.3f}", " | ".join(x["failure_examples"][:6])])
    with open(OUT / f"{name}-cases.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["range_m", "capacity_Bps", "case", "retention", "failures", "msgs_per_ac_min"])
        for (r, c), label, ret, f, m in per_case:
            w.writerow([f"{r:g}", f"{c:g}", label, f"{ret:.4f}", f, f"{m:.3f}"])
    lb = cells[LEADERBOARD]
    row = {"variant": name, "policy": spec, "event_seeds": ",".join(map(str, event_seeds)), "cases": len(cases),
           "lb_retention": f"{lb['retention']:.4f}", "lb_failures": lb["failures"], "lb_runs": lb["runs"],
           "lb_msgs_per_ac_min": f"{lb['messages_per_aircraft_per_min']:.3f}", "lb_s_loss": f"{lb['s_loss']:.4f}",
           "grid_failures": sum(x["failures"] for x in cells.values()),
           "grid_runs": sum(x["runs"] for x in cells.values()),
           "sizing_score": f"{score:.3f}", "frontier": frontier_text(frontier), "seconds": f"{seconds:.0f}"}
    path = OUT / "variants.csv"
    rows = []
    if path.exists():
        with open(path, newline="") as fh:
            rows = [r for r in csv.DictReader(fh) if not (r["variant"] == name and r["event_seeds"] == row["event_seeds"])]
    rows.append(row)
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(row))
        w.writeheader()
        w.writerows(rows)
    return row


def cmd_run(a):
    seeds = parse_seeds(a.event_seeds)
    t0 = time.time()
    cases = build_cases(seeds, a.workers)
    print(f"{len(cases)} cases ({len(VALIDATION)} scenarios x event seeds {a.event_seeds}), "
          f"built in {time.time() - t0:.0f} s", flush=True)
    load_factory(a.policy)   # fail early on a bad spec
    ranges = [float(x) for x in a.ranges.split(",")] if a.ranges else RANGES
    caps = [float(x) for x in a.capacities.split(",")] if a.capacities else CAPACITIES
    t1 = time.time()
    cells, score, frontier, per_case = evaluate_grid(a.policy, cases, a.workers, a.comm_seeds, ranges, caps)
    secs = time.time() - t1
    if LEADERBOARD in cells and not a.ranges and not a.capacities and a.comm_seeds == SEEDS:
        row = write_variant(a.name, a.policy, seeds, cases, cells, score, frontier, per_case, secs)
        print(json.dumps(row, indent=1))
    for (r, c), x in cells.items():
        print(f"  {r:6g} m {c:5g} B/s  ret {x['retention']:.4f}  fail {x['failures']:3d}/{x['runs']}  "
              f"msgs {x['messages_per_aircraft_per_min']:6.2f}  s_loss {x['s_loss']:.4f}  {'meets' if x['meets'] else ''}"
              + (f"  e.g. {x['failure_examples'][0]}" if x["failure_examples"] else ""))
    print(f"sizing_score {score:.3f}  frontier {frontier_text(frontier)}  ({secs:.0f} s)")
    if a.check:
        ref = sizing([c[1:4] for c in cases], load_factory(a.policy), a.comm_seeds, ranges, caps)
        same = abs(ref["sizing_score"] - score) < 1e-12 and all(
            abs(ref["cells"][f"{r:g}x{c:g}"]["retention"] - cells[(r, c)]["retention"]) < 1e-9
            and ref["cells"][f"{r:g}x{c:g}"]["failures"] == cells[(r, c)]["failures"] for r, c in cells)
        print("check against fleetkit sizing():", "identical" if same else "DIFFERENT")
        if not same:
            sys.exit(1)


# --------------------------------------------------------------------------- report

COLORS = ["#888888", "#d62728", "#1f77b4", "#2ca02c", "#9467bd", "#ff7f0e", "#8c564b", "#e377c2", "#17becf"]


def load_cells(name):
    with open(OUT / f"{name}.csv", newline="") as fh:
        return {(float(r["range_m"]), float(r["capacity_Bps"])): r for r in csv.DictReader(fh)}


def svg_plot(variants, path):
    """Retention against capacity, one panel per range; a dashed line marks THETA; hollow marks a failed cell."""
    pw, ph, ml, mt, gap = 230, 200, 45, 40, 25
    width = ml + len(RANGES) * (pw + gap) + 150
    height = mt + ph + 60
    caps = list(CAPACITIES)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
             f'font-family="sans-serif" font-size="11"><rect width="100%" height="100%" fill="white"/>',
             f'<text x="{ml}" y="16" font-size="13">Link-loss retention per sizing cell '
             f'(held-out cases; hollow = a run failed; dashed = {THETA})</text>']
    y0 = 0.0
    for k, r in enumerate(RANGES):
        x0 = ml + k * (pw + gap)
        sx = lambda i: x0 + 10 + i * (pw - 20) / (len(caps) - 1)  # noqa: E731
        sy = lambda v: mt + ph - (v - y0) / (1 - y0) * ph  # noqa: E731
        parts.append(f'<rect x="{x0}" y="{mt}" width="{pw}" height="{ph}" fill="none" stroke="#ccc"/>')
        parts.append(f'<text x="{x0 + pw / 2}" y="{mt - 6}" text-anchor="middle">R_max {r:g} m</text>')
        parts.append(f'<line x1="{x0}" x2="{x0 + pw}" y1="{sy(THETA):.1f}" y2="{sy(THETA):.1f}" '
                     f'stroke="#999" stroke-dasharray="4 3"/>')
        for i, c in enumerate(caps):
            parts.append(f'<text x="{sx(i):.1f}" y="{mt + ph + 14}" text-anchor="middle">{c:g}</text>')
        if k == 0:
            for v in (0, 0.25, 0.5, 0.75, 1.0):
                parts.append(f'<text x="{x0 - 4}" y="{sy(v) + 4:.1f}" text-anchor="end">{v:g}</text>')
        for j, (name, cells) in enumerate(variants):
            col = COLORS[j % len(COLORS)]
            pts = [(sx(i), sy(float(cells[(r, c)]["retention"])), int(cells[(r, c)]["failures"]))
                   for i, c in enumerate(caps) if (r, c) in cells]
            parts.append('<polyline fill="none" stroke="%s" stroke-width="1.5" points="%s"/>'
                         % (col, " ".join(f"{x:.1f},{y:.1f}" for x, y, _ in pts)))
            for x, y, f in pts:
                parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" stroke="{col}" '
                             f'fill="{"white" if f else col}"/>')
    parts.append(f'<text x="{ml + len(RANGES) * (pw + gap) / 2}" y="{mt + ph + 32}" text-anchor="middle">'
                 f'capacity (B/s per receiver)</text>')
    lx = ml + len(RANGES) * (pw + gap)
    for j, (name, _) in enumerate(variants):
        col = COLORS[j % len(COLORS)]
        parts.append(f'<rect x="{lx}" y="{mt + 16 * j}" width="10" height="10" fill="{col}"/>'
                     f'<text x="{lx + 14}" y="{mt + 9 + 16 * j}">{name}</text>')
    parts.append("</svg>")
    Path(path).write_text("\n".join(parts) + "\n")


def cmd_report(a):
    with open(OUT / "variants.csv", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r["event_seeds"] == ",".join(map(str, parse_seeds(a.event_seeds)))]
    names = [r["variant"] for r in rows] if not a.variants else a.variants.split(",")
    rows = {r["variant"]: r for r in rows}
    cells = {n: load_cells(n) for n in names}
    refs = [n for n in ("continue", "return") if n in cells]
    print("| Variant | Retention | Failures | Msgs/ac/min | S_loss | Grid failures | Sizing | Frontier | Beats continue+return from |")
    print("|---|---:|---:|---:|---:|---:|---:|---|---|")
    for n in names:
        r = rows[n]
        beats = []
        for rng in RANGES:
            ok = [c for c in CAPACITIES if refs and float(cells[n][(rng, c)]["s_loss"]) > max(
                float(cells[x][(rng, c)]["s_loss"]) for x in refs) and int(cells[n][(rng, c)]["failures"]) == 0]
            beats.append(f"{rng:g}:{min(ok):g}" if ok else f"{rng:g}:-")
        print(f"| {n} | {float(r['lb_retention']):.3f} | {r['lb_failures']}/{r['lb_runs']} | "
              f"{float(r['lb_msgs_per_ac_min']):.2f} | {float(r['lb_s_loss']):.3f} | {r['grid_failures']}/{r['grid_runs']} | "
              f"{float(r['sizing_score']):.2f} | {r['frontier']} | {' '.join(beats)} |")
    if a.sizing_from:
        src = load_cells(a.sizing_from)
        with open(ROOT / "analysis" / "linkloss_sizing.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["variant", "range_m", "capacity_Bps", "retention", "failures", "runs", "msgs_per_ac_min",
                        "s_loss", "meets", "frontier_capacity"])
            fr = {}
            for (rng, c), x in src.items():
                if x["meets"] == "1":
                    fr[rng] = min(fr.get(rng, c), c)
            for (rng, c), x in src.items():
                w.writerow([a.sizing_from, f"{rng:g}", f"{c:g}", x["retention"], x["failures"], x["runs"],
                            x["msgs_per_ac_min"], x["s_loss"], x["meets"], f"{fr[rng]:g}" if rng in fr else ""])
    svg_plot([(n, cells[n]) for n in names], ROOT / "analysis" / "linkloss_sizing.svg")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--policy", required=True)
    r.add_argument("--name", required=True)
    r.add_argument("--event-seeds", default=HELD_OUT)
    r.add_argument("--comm-seeds", type=int, default=SEEDS)
    r.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    r.add_argument("--ranges", default="", help="subset, e.g. 250,500 (not written to variants.csv)")
    r.add_argument("--capacities", default="")
    r.add_argument("--check", action="store_true", help="recompute serially with fleetkit sizing() and compare")
    p = sub.add_parser("report")
    p.add_argument("--event-seeds", default=HELD_OUT)
    p.add_argument("--variants", default="")
    p.add_argument("--sizing-from", default="", help="variant whose grid becomes analysis/linkloss_sizing.csv")
    a = ap.parse_args()
    cmd_run(a) if a.cmd == "run" else cmd_report(a)


if __name__ == "__main__":
    main()
