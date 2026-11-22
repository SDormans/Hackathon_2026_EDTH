"""Robustness sweep: run planner.py on scenario files and validate every output.

Each scenario runs in its own process with a hard wall-clock timeout. A case fails on a
nonzero exit (crash), a wall time above --limit seconds (timeout), a missing or invalid
plan (official fleetkit scorer), or an internal error the planner caught and recovered from
(it then exports a fallback plan; still a bug). Empty plans (no routes) are valid but reported.

    python3 analysis/robust_sweep.py scenarios/robust/*.json --seconds 45 --workers 4
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analysis"))
from _kit import KIT  # noqa: E402  (local fleetkit discovery)

sys.path.insert(0, str(KIT))
from fleetkit.model import Scenario, load_json  # noqa: E402
from fleetkit.scorer import evaluate  # noqa: E402


def run_case(scenario_path, seconds, limit, out_dir, extra=()):
    """Runs the planner on one scenario; returns a result dictionary."""
    scenario_path = Path(scenario_path)
    plan_path = Path(out_dir) / f"{scenario_path.stem}.plan.json"
    if plan_path.exists():
        plan_path.unlink()
    cmd = [sys.executable, str(ROOT / "planner.py"), str(scenario_path), str(plan_path),
           "--seconds", str(seconds), *extra]
    started = time.monotonic()
    result = {"case": scenario_path.stem, "seconds": seconds, "status": "ok", "detail": ""}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=limit + 15,
                              env={"PYTHONPATH": str(KIT), "PATH": "/usr/bin:/bin"})
    except subprocess.TimeoutExpired:
        result.update(wall_s=round(time.monotonic() - started, 2), status="timeout",
                      detail=f"killed after {limit + 15:g} s")
        return result
    wall = time.monotonic() - started
    result["wall_s"] = round(wall, 2)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        result.update(status="crash", detail=f"exit {proc.returncode}: {' | '.join(tail)}")
        return result
    if wall > limit:
        result.update(status="timeout", detail=f"wall {wall:.1f} s > {limit:g} s")
    errors = None
    for line in reversed((proc.stderr or "").strip().splitlines()):
        if line.startswith("{"):
            try:
                errors = json.loads(line).get("errors")
            except ValueError:
                pass
            break
    if result["status"] != "ok":
        pass
    elif errors is None or "Traceback" in (proc.stderr or ""):
        # The planner caught an internal error (or printed no report): recovered, but a bug.
        tail = [l for l in (proc.stderr or "").splitlines() if l.strip()][-2:]
        result.update(status="recovered", detail=" | ".join(tail)[:300])
    elif errors:
        result.update(status="recovered", detail=errors[0].strip().splitlines()[-1][:300])
    if not plan_path.exists():
        result.update(status="crash", detail="exit 0 but no plan file")
        return result
    try:
        plan = load_json(plan_path)
        evaluation = evaluate(Scenario.from_dict(load_json(scenario_path)), plan)
    except Exception as exc:  # unreadable plan counts as invalid
        result.update(status="invalid", detail=f"{type(exc).__name__}: {exc}")
        return result
    routes = plan.get("routes") if isinstance(plan, dict) else None
    result["routes"] = len(routes) if isinstance(routes, list) else 0
    result["empty"] = result["routes"] == 0
    result["score"] = round(evaluation.score, 3) if evaluation.valid else 0.0
    if not evaluation.valid:
        result.update(status="invalid",
                      detail="; ".join(str(v) for v in evaluation.violations[:3]))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenarios", nargs="+")
    parser.add_argument("--seconds", type=float, default=45.0)
    parser.add_argument("--limit", type=float, default=60.0, help="wall-clock limit (s)")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out", type=Path, help="plan directory (default: a temp dir)")
    parser.add_argument("--json", type=Path, help="write results as JSON")
    args = parser.parse_args()
    out = args.out or Path(tempfile.mkdtemp(prefix="robust-"))
    out.mkdir(parents=True, exist_ok=True)
    results = []
    with ThreadPoolExecutor(args.workers) as pool:
        futures = [pool.submit(run_case, s, args.seconds, args.limit, out) for s in args.scenarios]
        for f in futures:
            r = f.result()
            results.append(r)
            print(f"{r['case']:28s} {r['status']:8s} wall {r.get('wall_s', 0):6.2f} s  "
                  f"routes {r.get('routes', '-'):>3}  score {r.get('score', '-'):>9}  {r['detail']}",
                  flush=True)
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    empty = sum(1 for r in results if r.get("empty"))
    slowest = max(results, key=lambda r: r.get("wall_s", 0))
    print(f"\n{len(results)} cases: {counts}; empty plans {empty}; "
          f"slowest {slowest['case']} {slowest.get('wall_s')} s")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(results, indent=1))
    return 0 if all(r["status"] == "ok" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
