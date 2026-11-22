"""Deadline-bounded beam search using official baseline routes and rule checks.

Nodes are valid, complete-to-home partial fleet plans. No route is edited in place.
The matrix ranks candidates; only official rule checks establish validity.
"""
from __future__ import annotations

from dataclasses import replace
import random
import time
import traceback

from fleetkit.model import HOSTILE, SCOUT, UNKNOWN
from fleetkit.scorer import evaluate
from candidate_matrix import CandidateMatrix
from beam import BeamMixin
from config import CONFIG
from deadline import DeadlineReached
from diagnostics import DiagnosticsMixin
from effector_teams import StrikeMixin
from incremental_validation import IncrementalValidator, State
from lns import DESTROY_OPERATORS, LNSMixin, default_lns_share
from scout_tours import SCOUT_SPEEDS, ScoutTripMixin

# Re-exports for tests and analysis scripts that import from search_planner.
from beam import diverse_beam  # noqa: F401
from diagnostics import fragility_parts, plan_fragility, unserved_reason  # noqa: F401
from effector_teams import EFFECTOR_Z, _effector_route, effector_layer_sets, effector_route, ground_route  # noqa: F401
from route_geometry import _cached_detour, _official_detour, pad_conflict, takeoff_need, takeoff_shift  # noqa: F401
from scout_tours import SCOUT_LAYERS, obs_point, pack_trips, raw_energy_wh, scout_tour, trip_labels  # noqa: F401
from lns import best_disjoint  # noqa: F401


class Search(BeamMixin, StrikeMixin, ScoutTripMixin, LNSMixin, DiagnosticsMixin):
    def __init__(self, scenario, seconds=45.0, top_n=5, beam_width=9, start=None, lns_share=None,
                 interleave=False, adaptive=False, restarts=True, widen=False,
                 scout_speed="fast", obs_stretch=True, sep_buffer=0.0):
        if seconds < 0 or not 1 <= top_n <= 100 or not 1 <= beam_width <= 100:
            raise ValueError("seconds must be nonnegative; top_n and beam_width must be 1..100")
        if lns_share is None:
            lns_share = default_lns_share(scenario)
        if not 0 <= lns_share <= 1:
            raise ValueError("lns_share must be 0..1")
        if scout_speed not in SCOUT_SPEEDS:
            raise ValueError(f"scout_speed must be one of {SCOUT_SPEEDS}")
        if sep_buffer < 0:
            raise ValueError("sep_buffer must be nonnegative")
        # Scout tour settings are module-wide (tour cache and helpers); one Search per run.
        CONFIG.scout_speed_mode, CONFIG.obs_stretch = scout_speed, bool(obs_stretch)
        self.scout_speed, self.obs_stretch = scout_speed, bool(obs_stretch)
        self.sc = scenario
        self.start = time.monotonic() if start is None else start
        # Includes input reading when the CLI passes its process start time.
        self.stop = self.start + min(seconds, 55.0)
        # Final official validation starts no new call after this; 50 s at the default budget.
        # Never after 56.5 s: planner.py's watchdog exports the empty plan at 57 s.
        self.validate_stop = min(self.start + 56.5, self.stop + 5.0)
        self.top_n, self.width = top_n, beam_width
        # The beam ends early so the final share of the budget repairs its best plan.
        self.beam_stop = self.stop - lns_share * max(0.0, self.stop - self.start)
        self.sep_buffer = float(sep_buffer)
        self.checker = IncrementalValidator(scenario, self.stop, self.sep_buffer)
        self.aircraft = scenario.aircraft_by_id()
        self.contacts = scenario.contact_by_id()
        self.lns = {"iterations": 0, "accepted": 0, "improvements": 0, "gain": 0.0,
                    "trip_tried": 0, "trip_accepted": 0, "trip_improvements": 0,
                    "trip_gain": 0.0, "trip_label_s": 0.0,
                    "group_tried": 0, "group_realised": 0, "group_accepted": 0,
                    "group_improvements": 0, "group_gain": 0.0, "group_label_s": 0.0,
                    "group_sizes": {}, "ops": {},
                    "strike_tried": 0, "strike_accepted": 0, "strike_improvements": 0,
                    "strike_gain": 0.0,
                    "exact_steps": 0, "exact_orders": 0, "exact_wins": 0, "exact_gain": 0.0}
        self._trip_cache = {}
        self.trip_stats = {}
        self.lns_share = lns_share
        self.interleave, self.adaptive, self.restarts = interleave, adaptive, restarts
        # Later passes keep a wider beam: more outranked branches survive to the end.
        self.widen = widen
        if interleave:
            # Beam passes alternate with LNS bursts; a short final tail is LNS only.
            self.beam_stop = self.stop - min(lns_share, 0.1) * max(0.0, self.stop - self.start)
        self.lns_rng = random.Random(scenario.seed + 7919)
        self.lns_state = None
        self.weights = {op: 1.0 for op in DESTROY_OPERATORS}
        self.root = State({}, {a.id: [] for a in scenario.aircraft if a.type == SCOUT},
                          frozenset(), {}, {}, {}, frozenset(), 0.0)
        self.trace = [{"id": 0, "parent": None, "decision": "start", "score": 0.0,
                       "served": [], "depth": 0}]
        self.archive = [self.root]
        self.signatures = {self.root.signature()}
        self.matrix = None
        self.passes = 0
        # Per-contact attempt accounting for "why was this contact not served".
        self.attempts = {}
        # Tracebacks of errors caught by run(); a robust run has none.
        self.errors = []

    def stats(self, cid):
        if cid not in self.attempts:
            self.attempts[cid] = {"attempts": 0, "warm_accepted": 0, "outcomes": {}, "rules": {},
                                  "beam_pruned": 0}
        return self.attempts[cid]

    def record(self, state, parent, decision, depth):
        # Trace stores metadata, not full copies of every discarded plan.
        state = replace(state, node_id=len(self.trace))
        self.trace.append({"id": state.node_id, "parent": parent.node_id,
                           "decision": decision, "score": round(state.score, 6),
                           "served": sorted(state.served), "depth": depth})
        if len(self.archive) < self.top_n or state.score > self.archive[-1].score:
            signature = state.signature()
            if signature not in self.signatures:
                self.archive.append(state)
                self.archive.sort(key=lambda s: -s.score)
                self.archive = self.archive[:self.top_n]
                self.signatures = {s.signature() for s in self.archive}
        return state

    def run(self):
        # An unexpected error ends the search but not the run: finalize() exports the best
        # archived plan that the official scorer accepts, else the empty plan.
        try:
            contacts, priorities = self.search_phase()
            self.improve_phase(contacts, priorities)
        except Exception:
            self.errors.append(traceback.format_exc())
        try:
            return self.finalize()
        except Exception:
            self.errors.append(traceback.format_exc())
            return [(self.root, evaluate(self.sc, self.root.plan(self.sc.seed)))]

    def search_phase(self):
        """Warm start plus beam passes until the beam deadline.
        Returns the searchable contacts and their priorities."""
        try:
            self.warm_start()
        except DeadlineReached:
            pass
        self.matrix = CandidateMatrix(self.sc)
        contacts = [c for c in self.sc.contacts if c.cls in (HOSTILE, UNKNOWN)
                    and c.window is not None and c.id not in self.matrix.banned]
        priorities = {c.id: self.matrix.priority(c) for c in contacts}
        rng = random.Random(self.sc.seed)
        self.checker.stop = min(self.beam_stop, self.stop)
        if CONFIG.trip_plan and contacts:
            share = CONFIG.trip_plan_share * max(0.0, self.beam_stop - self.start)
            try:
                self.trip_plan(min(self.checker.stop, time.monotonic() + share))
            except DeadlineReached:
                pass
        try:
            self.search(contacts, priorities, rng)
        except DeadlineReached:
            pass
        self.checker.stop = self.stop
        return contacts, priorities

    def improve_phase(self, contacts, priorities):
        """Greedy repair of the best beam plan, then LNS until the deadline."""
        # Greedy repair of the best plan gets at most a quarter of the LNS share.
        self.checker.stop = self.beam_stop + 0.25 * max(0.0, self.stop - self.beam_stop)
        try:
            if contacts:
                best = self.archive[0]
                depth = max((n["depth"] for n in self.trace if n["id"] == best.node_id), default=0)
                self.repair(best, depth)
        except DeadlineReached:
            pass
        self.checker.stop = self.stop
        try:
            self.improve(contacts, priorities)
        except DeadlineReached:
            pass

    def finalize(self):
        # Official full validation is the authority; archive can include partial
        # plans because every scout already has its return-and-land leg.
        verified = []
        for state in self.archive:
            if time.monotonic() >= self.validate_stop:
                break
            result = evaluate(self.sc, state.plan(self.sc.seed))
            if result.valid:
                verified.append((state, result))
        if not verified:
            verified = [(self.root, evaluate(self.sc, self.root.plan(self.sc.seed)))]
        verified.sort(key=lambda pair: -pair[1].score)
        return verified
