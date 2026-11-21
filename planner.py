"""Hand-in entry point: python3 planner.py scenario.json plan.json.

Standard library only. Optional search diagnostics: --artifacts output/search.
"""
import argparse
import json
from pathlib import Path
import sys
import time

# Start before input parsing/imports so they count against the search budget.
START = time.monotonic()
try:
    import fleetkit
except ModuleNotFoundError:
    # Local checkout convenience. The official runner supplies fleetkit through
    # PYTHONPATH, so a hand-in needs only this file and its three sibling modules.
    import os
    configured = os.environ.get("FLEETKIT_DIR")
    if configured:
        sys.path.insert(0, configured)
    else:
        root = Path(__file__).resolve().parent
        kits = sorted(root.glob("AA-Hackathon*/**/fleet-kit/fleetkit/__init__.py"))
        if not kits:
            raise SystemExit("Set PYTHONPATH or FLEETKIT_DIR to the folder containing fleetkit/")
        sys.path.insert(0, str(kits[0].parent.parent))

from fleetkit.model import Scenario, load_json, save_json
from search_planner import SCOUT_COSTS, Search


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario")
    parser.add_argument("plan")
    parser.add_argument("--seconds", type=float, default=55.0,
                        help="search deadline including startup, capped at 55 seconds")
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--beam-width", type=int, default=6)
    parser.add_argument("--lns-share", type=float, default=0.5,
                        help="share of the search time for large neighbourhood search, 0..0.9")
    parser.add_argument("--scout-cost", choices=SCOUT_COSTS, default="mixed",
                        help="primary cost for ranking scout insertion positions")
    parser.add_argument("--time-weight", type=float, default=0.75,
                        help="weight of time in --scout-cost mixed, 0 (distance) .. 1 (time)")
    parser.add_argument("--artifacts", type=Path,
                        help="optional directory for ranked plans, tree.json and interactive tree.html")
    args = parser.parse_args()
    if not 0 <= args.seconds <= 55 or not 1 <= args.top_n <= 100 or not 1 <= args.beam_width <= 100:
        parser.error("seconds must be 0..55; top-n and beam-width must be 1..100")
    if not 0 <= args.lns_share <= 0.9:
        parser.error("lns-share must be 0..0.9")
    if not 0 <= args.time_weight <= 1:
        parser.error("time-weight must be 0..1")
    scenario = Scenario.from_dict(load_json(args.scenario))
    search = Search(scenario, args.seconds, args.top_n, args.beam_width, START, args.lns_share,
                    args.scout_cost, args.time_weight)
    verified = search.run()
    # Required output is written before optional diagnostics.
    save_json(verified[0][0].plan(scenario.seed), args.plan)
    report = search.report(verified)
    if args.artifacts:
        from tree_view import save_tree
        args.artifacts.mkdir(parents=True, exist_ok=True)
        for rank, (state, result) in enumerate(verified, 1):
            save_json(state.plan(scenario.seed), args.artifacts / f"plan-{rank:02d}.json")
        save_json(report, args.artifacts / "tree.json")
        save_tree(report, args.artifacts / "tree.html")
    print(json.dumps({key: report[key] for key in
                      ("elapsed_s", "passes", "candidate_checks", "rejections",
                       "unserved", "unserved_points", "solutions")}), file=sys.stderr)


if __name__ == "__main__":
    main()
