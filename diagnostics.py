"""Unserved-contact accounting, plan fragility and the search report.
"""
from __future__ import annotations

import math
import time

from fleetkit.geometry import close_intervals, overlapping_pieces
from fleetkit.model import HOSTILE, SCOUT, UNKNOWN, parse_plan
from fleetkit import validator as rules
from candidate_matrix import CandidateMatrix
from config import CONFIG


# How far one attempt to serve a contact got, from least to most progress.
# An attempt is one call to Search.children for one parent node.
STAGES = ("no_options", "no_team", "scout_window", "scout_route", "scout_rule", "strike_route",
          "strike_rule", "not_improving", "accepted")


def outcome_stage(outcome):
    return outcome.split(":", 1)[0]


def unserved_reason(contact, banned, stats):
    """One primary reason why a contact is missing from the winning plan.

    collateral_ban / no_window: excluded before the search.
    never_tried: no attempt ran (the pass order or the deadline never reached it).
    outranked: some branch served it validly, but that branch did not win.
    Otherwise the most common attempt outcome, e.g. "scout_rule:R4", "time_cap".
    """
    if contact.id in banned:
        return "collateral_ban"
    if contact.window is None:
        return "no_window"
    if stats is None or (not stats["attempts"] and not stats["warm_accepted"]):
        return "never_tried"
    outcomes = stats["outcomes"]
    if stats["warm_accepted"] or outcomes.get("accepted"):
        return "outranked"
    if not outcomes:
        return "never_tried"
    # Most frequent outcome; ties go to the attempt that got furthest.
    order = {name: i for i, name in enumerate(STAGES)}
    return max(outcomes, key=lambda o: (outcomes[o], order.get(outcome_stage(o), -1), o))


def fragility_parts(plan, scenario):
    """Link-loss fragility of a plan: what a delay or a lost message is most likely to break.

    tight: engage waypoints whose confirm slack (engage t - completed observation end -
    confirm_margin_s, earliest observation) is below FRAGILITY_TIGHT_S;
    near_pairs: aircraft pairs ever closer than d_sep_m + FRAGILITY_SEP_MARGIN_M (effectors
    striking the same contact are exempt); mean_gap_s: mean engage t - observation end.
    """
    routes, _ = parse_plan(plan)
    routes = routes or {}
    aircraft, contacts, r = scenario.aircraft_by_id(), scenario.contact_by_id(), scenario.rules
    obs_end = {}
    for aid, route in routes.items():
        if aircraft.get(aid) is None or aircraft[aid].type != SCOUT:
            continue
        for wp in route.waypoints:
            c = contacts.get(wp.target)
            if wp.action == "observe" and c is not None and rules.valid_observation(wp, c, scenario):
                end = wp.t + wp.dwell_s
                obs_end[c.id] = min(end, obs_end.get(c.id, math.inf))
    gaps = []
    for aid, route in routes.items():
        for wp in route.waypoints:
            if wp.action == "engage" and wp.target in obs_end:
                gaps.append(wp.t - obs_end[wp.target])
    tight = sum(g - r.confirm_margin_s < CONFIG.fragility_tight_s for g in gaps)
    near_m = r.d_sep_m + CONFIG.fragility_sep_margin_m
    items = []
    for aid in sorted(routes):
        ac = aircraft.get(aid)
        item = rules.separation_item(aid, routes[aid], ac) if ac is not None else None
        if item is None:
            continue
        pts = [p for piece in item[1] for p in (piece[2], piece[3])]
        box = (min(p[0] for p in pts), max(p[0] for p in pts), min(p[1] for p in pts),
               max(p[1] for p in pts), min(p[2] for p in pts), max(p[2] for p in pts))
        items.append((item, box))
    near = 0
    for i, (a, ba) in enumerate(items):
        for b, bb in items[i + 1:]:
            if min(a[3], b[3]) <= max(a[2], b[2]) or (a[4] is not None and a[4] == b[4]):
                continue
            if (ba[0] - near_m > bb[1] or bb[0] - near_m > ba[1] or ba[2] - near_m > bb[3]
                    or bb[2] - near_m > ba[3] or ba[4] - near_m > bb[5] or bb[4] - near_m > ba[5]):
                continue
            if any(close_intervals(pa, pb, lo, hi, near_m) is not None
                   for pa, pb, lo, hi in overlapping_pieces(a[1], b[1])):
                near += 1
    return {"tight": tight, "near_pairs": near, "engages": len(gaps),
            "mean_gap_s": sum(gaps) / len(gaps) if gaps else None}


def plan_fragility(plan, scenario):
    """Lower is sturdier: tight couplings + near pairs - capped mean confirm->strike gap share."""
    f = fragility_parts(plan, scenario)
    gap = min(f["mean_gap_s"], CONFIG.fragility_gap_cap_s) / CONFIG.fragility_gap_cap_s if f["engages"] else 1.0
    return f["tight"] + f["near_pairs"] - gap


class DiagnosticsMixin:
    """Search methods; mixed into search_planner.Search."""

    def diagnostics(self, served):
        """Per-contact accounting against the winner's served set."""
        banned = self.matrix.banned if self.matrix is not None else CandidateMatrix(self.sc).banned
        served = set(served)
        contacts, summary, points = {}, {}, {}
        for c in self.sc.contacts:
            if c.cls not in (HOSTILE, UNKNOWN):
                continue
            stats = self.attempts.get(c.id)
            entry = {"cls": c.cls, "points": c.points, "served": c.id in served,
                     "attempts": stats["attempts"] if stats else 0,
                     "outcomes": dict(stats["outcomes"]) if stats else {},
                     "beam_pruned": stats.get("beam_pruned", 0) if stats else 0,
                     "rules": dict(stats["rules"]) if stats else {}}
            if c.id not in served:
                reason = unserved_reason(c, banned, stats)
                entry["reason"] = reason
                summary[reason] = summary.get(reason, 0) + 1
                points[reason] = points.get(reason, 0) + c.points
            contacts[c.id] = entry
        return {"contacts": contacts,
                "unserved": dict(sorted(summary.items(), key=lambda kv: -points[kv[0]])),
                "unserved_points": dict(sorted(points.items(), key=lambda kv: -kv[1]))}

    def fragility(self, state):
        plan = state.plan(self.sc.seed)
        return {**fragility_parts(plan, self.sc), "score": plan_fragility(plan, self.sc)}

    def report(self, verified):
        diagnostics = self.diagnostics(verified[0][1].served if verified else [])
        return {"seed": self.sc.seed, "elapsed_s": time.monotonic()-self.start,
                "search_budget_s": max(0, self.stop-self.start), "passes": self.passes,
                "candidate_checks": self.checker.calls,
                "candidate_check_seconds": self.checker.seconds,
                "rejections": self.checker.rejections,
                "beam_width": self.width, "requested_top_n": self.top_n,
                "scouting": {"scout_speed": self.scout_speed, "obs_stretch": self.obs_stretch}, "sep_buffer_m": self.sep_buffer,
                "lns": {**self.lns, "variant": {"interleave": self.interleave,
                                                "adaptive": self.adaptive,
                                                "restarts": self.restarts},
                        **({"weights": dict(self.weights)} if self.adaptive else {})},
                "trip_plan": dict(self.trip_stats),
                "errors": list(self.errors),
                "unserved": diagnostics["unserved"],
                "unserved_points": diagnostics["unserved_points"],
                "contacts": diagnostics["contacts"],
                "fragility": self.fragility(verified[0][0]) if verified else None,
                "solutions": [{"rank": i, "node": state.node_id, "score": result.score,
                               "served": result.served} for i, (state, result) in enumerate(verified, 1)],
                "nodes": self.trace}
