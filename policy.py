"""Link-loss policy: safe by default, quiet unless a peer needs to know.

Failure modes the policy guards against (each one sets retention to 0):
- engaging a contact that turned friendly: effectors sense their own target within 300 m,
  always before they reach it, and turn back;
- entering a pop-up zone: every aircraft checks its remaining route against the zones it
  knows each second and turns back before it would cross one;
- engaging without a completed observation (R4): an effector engages only when it knows the
  observation is done. Observations finished before the cut are known from the plan (nothing
  can go wrong before it). Later ones are announced by the scout that made them; an effector
  that has not heard one holds short of its target, then turns back;
- separation (R8) once aircraft leave the plan: every aircraft predicts its peers from the
  plan (route clock = real time until they say otherwise) and from their announced
  (route time, rate). Each second it picks, among the moves that serve its intent, the one
  that keeps the most room over the next few seconds, and announces its rate when it changes.

Messages cost score, so the only messages are rate changes and "observed" notices.
"""
import math

from fleetkit.geometry import segment_enters_cylinder
from fleetkit.linkloss import RouteClock
from fleetkit.model import EFFECTOR, SCOUT, parse_plan

HOLD_LEAD_S = 3.0       # an unconfirmed effector stops this much route time before engaging
HOLD_GRACE_S = 15.0     # and waits at most this long past its planned engagement
ANNOUNCE_AFTER_S = HOLD_GRACE_S + 10.0  # announced while late effectors may still wait
HORIZON_S = 5.0         # look-ahead of the separation check
SAMPLES = 10            # positions checked over the look-ahead
SEP_MARGIN_M = 15.0
NEAR_M = 400.0          # peers further than this cannot meet within the look-ahead
REPEAT_S = 4.0
REFRESH_S = 8.0         # an off-schedule aircraft re-announces its state this often
EXEMPT_MARGIN_M = 25.0  # use the shared-target exemption only well inside its radius
SLACK_S = 1.5           # timing uncertainty of an announced peer state          # a changed rate is repeated once after this long (lost messages)


def _d(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


class Policy:
    def __init__(self, scenario, plan, aircraft_id):
        routes, _ = parse_plan(plan)
        aircraft = scenario.aircraft_by_id()
        rules = scenario.rules
        self.id = aircraft_id
        self.ac = aircraft[aircraft_id]
        self.route = routes[aircraft_id]
        self.clock = RouteClock(self.route)
        self.sep = rules.d_sep_m + SEP_MARGIN_M
        self.exempt = rules.exempt_radius_m - EXEMPT_MARGIN_M
        self.contacts = scenario.contact_by_id()
        self.margin = rules.confirm_margin_s + 1.5   # observation end to engagement, with slack
        self.target = self._target(self.route, self.ac)
        self.engage_t = self.route.waypoints[-1].t if self.target else None
        self.peers = {}
        for aid, r in routes.items():
            if aid != aircraft_id:
                self.peers[aid] = (RouteClock(r), self._target(r, aircraft[aid]))
        self.peer_state = {}           # aid -> (tau at t0, rate, t0)
        self.obs_end, self.last_engage = {}, {}
        for aid, r in routes.items():
            for wp in r.waypoints:
                if wp.action == "observe" and aircraft[aid].type == SCOUT:
                    self.obs_end.setdefault(wp.target, []).append(wp.t + wp.dwell_s)
                elif wp.action == "engage" and aircraft[aid].type == EFFECTOR:
                    self.last_engage[wp.target] = max(self.last_engage.get(wp.target, 0.0), wp.t)
        self.confirmed = None
        self.reclassified, self.zones = set(), []
        self.announce, self.reported = {}, {}
        self.aborted = False
        self.rate = 1.0
        self.sent = None               # (rate, time) of the last rate change sent
        self.last_sent, self.repeats = -1e9, 0

    @staticmethod
    def _target(route, ac):
        last = route.waypoints[-1]
        return last.target if (ac.type == EFFECTOR and last.action == "engage") else None

    # ------------------------------------------------------------------ knowledge
    def _learn(self, obs):
        if self.confirmed is None:
            # contact -> time its observation ended (planned ends before the cut are exact)
            self.confirmed = {c: min(ends) for c, ends in self.obs_end.items() if min(ends) <= obs["t_loss"]}
        for e in obs["sensed"]:
            if e["kind"] == "reclassify":
                self.reclassified.add(e["contact"])
            elif e["kind"] == "popup_zone":
                z = (e["center"][0], e["center"][1], e["radius_m"], e["top_m"])
                if z not in self.zones:
                    self.zones.append(z)
        for m in obs["inbox"]:
            p = m["payload"]
            for c, end in p.get("o", ()):
                if c not in self.confirmed or end < self.confirmed[c]:
                    self.confirmed[c] = end
            if "r" in p:
                tau, rate = p["r"]
                self.peer_state[m["from"]] = (tau, rate, m["sent_t"])

    def _hits_zone(self, tau_from, tau_to=None):
        if not self.zones:
            return False
        k = self.clock.next_waypoint(tau_from)
        pts = [self.clock.position(tau_from)] + self.clock.pos[k:]
        if tau_to is not None:
            pts = [self.clock.position(tau_from), self.clock.position(tau_to)]
        return any(segment_enters_cylinder(a, b, (z[0], z[1]), z[2], z[3])
                   for z in self.zones for a, b in zip(pts, pts[1:]))

    # ------------------------------------------------------------------ separation
    def _peer_pos(self, aid, t, rate_override=None):
        clock, _ = self.peers[aid]
        tau0, rate, t0 = self.peer_state.get(aid, (None, 1.0, None))
        if rate_override is not None and tau0 is not None:
            rate = rate_override
        tau = t if tau0 is None else tau0 + rate * (t - t0)
        if tau >= clock.end - 1e-9 or tau <= clock.start + 1e-9:
            return None            # engaged, landed or still on the ground
        return clock.position(tau)

    def _room(self, t, tau, rate):
        """Smallest predicted distance to any peer over the look-ahead for a constant rate."""
        here = self.clock.position(tau)
        near = []
        for aid in self.peers:
            p = self._peer_pos(aid, t)
            if p is None:
                clock = self.peers[aid][0]
                if aid not in self.peer_state and t <= clock.start <= t + HORIZON_S + SLACK_S:
                    p = clock.pos[0]   # still on the ground, takes off within the look-ahead
            if p is not None and _d(p, here) < NEAR_M:
                near.append(aid)
        best = math.inf
        for i in range(1, SAMPLES + 1):
            dt = HORIZON_S * i / SAMPLES
            my_tau = min(max(tau + rate * dt, self.clock.start), self.clock.end)
            if my_tau <= self.clock.start + 1e-9:
                continue           # landed
            me = self.clock.position(my_tau)
            for aid in near:
                # An off-plan peer with a lower id has the right of way: it may move any way.
                known = aid in self.peer_state
                rates = (None, -1.0, 0.0, 1.0) if (known and aid < self.id) else (None,)
                # An announced state may be a step or so stale (a lost or late message).
                shifts = (-SLACK_S, 0.0, SLACK_S) if known else (0.0,)
                for ro, sh in ((ro, sh) for ro in rates for sh in shifts):
                    p = self._peer_pos(aid, t + dt + sh, ro)
                    if p is None:
                        continue
                    tgt = self.peers[aid][1]
                    if tgt is not None and tgt == self.target:
                        c = self.contacts[tgt].pos
                        if _d(me, c) <= self.exempt and _d(p, c) <= self.exempt:
                            continue   # the usual exemption around a shared target
                    best = min(best, _d(me, p))
        return best

    # ------------------------------------------------------------------ decision
    def _options(self, obs):
        """Rates that serve the intent, in order of preference, and whether engaging is allowed."""
        tau, t = obs["tau"], obs["t"]
        if self._hits_zone(tau):
            self.aborted = True
        if self.target is not None and self.target in self.reclassified:
            self.aborted = True
        if self.target is not None and not self.aborted and self.target not in self.confirmed                 and t > self.engage_t + HOLD_GRACE_S:
            self.aborted = True
        if self.aborted:
            return [-1.0, 0.0, 0.5, 1.0], False
        if self.target is not None and self.target in self.confirmed:
            if t + (self.engage_t - tau) < self.confirmed[self.target] + self.margin:
                return [1.0, 0.5, 0.0, -1.0], False   # confirmed too recently: not yet
            return [1.0, 0.5, 0.0, -1.0], True
        if self.target is not None:
            return [1.0, 0.5, 0.0, -1.0], False       # unconfirmed: approach, never engage
        return [1.0, 0.5, 0.0, -1.0], True

    def _allowed(self, tau, rate, may_engage):
        """Hard limits: never enter a known zone, never engage when not allowed."""
        if rate == 0.0:
            return True
        end = tau + rate * (HORIZON_S + 1.0)
        if rate > 0 and self.target is not None and not may_engage and end >= self.engage_t - 0.5:
            return False
        if self.zones:
            lo, hi = (tau, end) if rate > 0 else (end, tau)
            lo, hi = max(lo, self.clock.start), min(hi, self.clock.end)
            taus = [lo] + [x for x in self.clock.breakpoints(lo, hi)] + [hi]
            pts = [self.clock.position(x) for x in taus]
            if any(segment_enters_cylinder(a, b, (z[0], z[1]), z[2], z[3])
                   for z in self.zones for a, b in zip(pts, pts[1:])):
                return False
        return True

    def step(self, obs):
        self._learn(obs)
        t, tau = obs["t"], obs["tau"]
        options, may_engage = self._options(obs)
        options = [r for r in options if self._allowed(tau, r, may_engage)] or [0.0]
        choice, best_room = options[0], -1.0
        for r in options:
            room = self._room(t, tau, r)
            if room >= self.sep:
                choice = r
                break
            if room > best_room:
                choice, best_room = r, room
        self.rate = choice
        action = ({"kind": "continue"} if choice == 1.0 else {"kind": "hold"} if choice == 0.0
                  else {"kind": "return"} if choice == -1.0 else {"kind": "speed", "scale": choice})
        return action, self._message(obs, tau, choice)

    def _message(self, obs, tau, rate):
        t = obs["t"]
        msg = {}
        if self.ac.type == SCOUT:
            for c in obs["own_observed"]:
                if c not in self.reported:
                    self.reported[c] = int(math.ceil(t))   # it ended during the last step
                    if c in self.last_engage:
                        self.announce[c] = self.last_engage[c] + ANNOUNCE_AFTER_S
            live = sorted(c for c, until in self.announce.items() if t <= until)
            if live:
                msg["o"] = [[c, self.reported[c]] for c in live[:2]]
        changed = self.sent is None and rate != 1.0 or self.sent is not None and rate != self.sent[0]
        off = abs(tau - t) > 0.25 or rate != 1.0
        due = self.sent is not None and off and t - self.last_sent >= (REPEAT_S if self.repeats < 1 else REFRESH_S)
        if changed:
            self.sent, self.repeats = (rate, t), 0
        elif due:
            self.repeats += 1
        if changed or due:
            msg["r"] = [round(tau, 1), rate]
            self.last_sent = t
        return msg or None
