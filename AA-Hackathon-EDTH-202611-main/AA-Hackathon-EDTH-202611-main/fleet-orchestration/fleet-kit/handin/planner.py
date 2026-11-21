"""Your hand-in. We run:  python3 planner.py scenario.json plan.json

Read the scenario from argv[1], write a plan to argv[2]. Replace the baseline below with your own planner.
Keep it within 60 seconds per scenario (a run that crashes, times out or writes an invalid plan scores 0).
"""
import sys

from fleetkit.baseline import greedy_plan
from fleetkit.model import Scenario, load_json, save_json


def main() -> None:
    scenario = Scenario.from_dict(load_json(sys.argv[1]))
    plan = greedy_plan(scenario)  # TODO: your planner here
    save_json(plan, sys.argv[2])


if __name__ == "__main__":
    main()
