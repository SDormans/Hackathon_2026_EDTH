"""One "why" line per contact of a plan: who serves it and why, or why it was skipped.

Reads the scenario, the plan and (optionally) the planner's stderr log, whose first line is the
search report with per-contact diagnostics (`contacts[cid].reason`, attempts and outcomes from
`Search.attempts`). The planner itself is not changed; this is an offline sidecar.

    python3 planner.py scenario.json plan.json 2> plan.log
    python3 analysis/explain_plan.py scenario.json plan.json --log plan.log -o plan.why.json
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))
import _kit  # noqa: E402,F401

from candidate_matrix import CandidateMatrix  # noqa: E402
from fleetkit.model import HOSTILE, UNKNOWN, Scenario, load_json  # noqa: E402

CATEGORY = {"collateral_ban": "collateral", "no_window": "window", "scout_window": "window",
            "time_cap": "window", "never_tried": "not reached", "outranked": "outranked",
            "no_team": "no effectors", "no_options": "unreachable", "not_improving": "outranked"}
TEXT = {
    "collateral": "a friendly or civilian contact is inside the collateral radius, so striking it is banned",
    "window": "no scout/strike timing fits its time window with the remaining aircraft",
    "unreachable": "every attempted route broke an official rule",
    "outranked": "it was served validly in some branches, but plans that used those aircraft elsewhere scored higher",
    "not reached": "the search deadline came before an attempt",
    "not worth it": "points do not cover the cheapest strike group's estimated effector and energy cost",
    "no effectors": "no free strike group of the required subtypes (all committed to other contacts)",
}


def category(reason):
    if reason in CATEGORY:
        return CATEGORY[reason]
    if reason.startswith(("scout_route", "strike_route", "scout_rule", "strike_rule")):
        return "unreachable"
    return reason


def read_report(path):
    if not path:
        return {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return {}


def explain(scenario, plan, report=None):
    matrix = CandidateMatrix(scenario)
    diag = (report or {}).get("contacts", {})
    kinds = {a.id: a.subtype or a.type for a in scenario.aircraft}
    observe, engage = {}, {}
    for route in plan.get("routes", []):
        for w in route["waypoints"]:
            if w.get("action") == "observe":
                observe.setdefault(w["target"], []).append((w["t"], route["aircraft"]))
            elif w.get("action") == "engage":
                engage.setdefault(w["target"], []).append((w["t"], route["aircraft"]))
    out = []
    for c in scenario.contacts:
        if c.cls not in (HOSTILE, UNKNOWN):
            continue
        entry = {"contact": c.id, "cls": c.cls, "points": c.points}
        d = diag.get(c.id, {})
        if d.get("attempts") is not None:
            entry["attempts"] = d["attempts"]
        scouts = sorted(observe.get(c.id, []))
        strikers = sorted(engage.get(c.id, []))
        if c.cls == UNKNOWN and scouts:
            t, s = scouts[0]
            entry.update(served=True, scouts=[s], why=f"observed by {s} at t={t:.0f}s: an unknown needs only a scout pass, "
                                                       f"so it was inserted at the cheapest position of {s}'s tour")
        elif c.cls == HOSTILE and strikers:
            ids = [a for _, a in strikers]
            cost = sum(matrix.costs.get((a, c.id), 0.0) for a in ids)
            best = matrix.groups(c, set(), 1)
            cheapest = bool(best) and sorted(a.id for a in best[0]) == sorted(ids)
            mix = ", ".join(f"{a} ({kinds[a]})" for a in ids)
            scout = f"confirmed by {scouts[0][1]} at t={scouts[0][0]:.0f}s, " if scouts else ""
            entry.update(served=True, scouts=[s for _, s in scouts[:1]], effectors=ids, est_cost=round(cost, 1),
                         why=f"{scout}struck by {mix} at t={strikers[0][0]:.0f}s; needs {dict(c.needs)}, "
                             f"estimated cost {cost:.1f} vs {c.points:g} points"
                             + ("; the cheapest group by the candidate matrix" if cheapest else
                                "; cheaper effectors were committed elsewhere or broke a rule"))
        else:
            reason = d.get("reason")
            if reason is None:
                if c.id in matrix.banned:
                    reason = "collateral_ban"
                elif c.window is None:
                    reason = "no_window"
            cat = category(reason) if reason else None
            if cat is None and c.cls == HOSTILE:
                prio = matrix.priority(c)
                cat = "no effectors" if prio == float("-inf") else "not worth it" if prio <= 0 else None
            if cat is None:
                cat = "not in plan"
            text = TEXT.get(cat, "not in the winning plan")
            if reason and cat == "unreachable":
                text += f" (most often {reason})"
            entry.update(served=False, reason=cat, detail=reason, why=f"skipped ({cat}): {text}")
        out.append(entry)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenario")
    ap.add_argument("plan")
    ap.add_argument("--log", help="planner stderr log (search report on its first JSON line)")
    ap.add_argument("-o", "--output", help="write the explanations as JSON here")
    a = ap.parse_args()
    sc = Scenario.from_dict(load_json(a.scenario))
    rows = explain(sc, load_json(a.plan), read_report(a.log))
    for r in rows:
        print(f"{r['contact']} {r['cls']:8s} {r['points']:5.0f}  {r['why']}")
    if a.output:
        Path(a.output).write_text(json.dumps(rows, indent=1) + "\n")


if __name__ == "__main__":
    main()
