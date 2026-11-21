"""Link-loss policy: keep flying the plan, stop only for dangers and conflicts, talk only off the plan.

Every aircraft knows the whole plan, so a peer that is still on its plan needs no message: its
position is the planned one. Aircraft that leave the plan (hold, return, run late) broadcast
their route-clock state, and avoid every peer themselves. Scouts announce completed observations
of contacts that effectors will engage; an effector only engages after a confirmation it knows of.
"""
import math

from fleetkit.geometry import dist3, segment_enters_cylinder
from fleetkit.linkloss.sim import RouteClock
from fleetkit.model import EFFECTOR, SCOUT, parse_plan

LOOK = (0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0)   # seconds ahead checked for separation
MARGIN_M = 10.0
ZONE_MARGIN_M = 5.0
CONFIRM_SENDS = 6
STATE_REPEATS = 3                               # sends after each change of route-clock rate
HEARTBEAT_S = 10.0                              # resend period while off the plan and alone
NEAR_M = 400.0                                  # off the plan, send every second with a peer this close
RELAY_SENDS = 2                                 # sends of each newly learned event
WAIT_S = 30.0                                   # an effector waits this long for a late confirmation

_STATIC = {}


def _static(scenario, plan):
    """Plan facts every aircraft can derive alone, computed once per plan."""
    key = (id(scenario), id(plan))
    if key in _STATIC:
        return _STATIC[key]
    routes, _ = parse_plan(plan)
    aircraft = scenario.aircraft_by_id()
    clocks = {a: RouteClock(r) for a, r in routes.items()}
    target, confirm_end = {}, {}
    for aid, r in routes.items():
        last = r.waypoints[-1]
        if aircraft[aid].type == EFFECTOR and last.action == "engage":
            target[aid] = last.target
    engaged = set(target.values())
    for aid, r in routes.items():
        if aircraft[aid].type != SCOUT:
            continue
        for k, wp in enumerate(r.waypoints):
            if wp.action == "observe" and wp.target in engaged:
                confirm_end.setdefault(wp.target, []).append(clocks[aid].dep[k])
    vmax = max(a.v_h_mps + a.v_up_mps for a in scenario.aircraft)
    data = {"clocks": clocks, "target": target, "engaged": engaged, "confirm_end": confirm_end,
            "aircraft": aircraft, "contacts": scenario.contact_by_id(), "vmax": vmax}
    _STATIC.clear()
    _STATIC[key] = data
    return data


def _tau_at(clock, tau0, rate, dt):
    """Route-clock value after dt seconds at a rate, or None once the aircraft has ended."""
    tau = tau0 + rate * dt
    if rate > 0 and tau >= clock.end:
        return None
    if rate < 0 and tau <= clock.start:
        return None
    if tau <= clock.start:
        return False   # on the ground
    return tau


class Policy:
    def __init__(self, scenario, plan, aircraft_id):
        self.id = aircraft_id
        self.st = _static(scenario, plan)
        self.rules = scenario.rules
        self.clock = self.st["clocks"][aircraft_id]
        self.ac = self.st["aircraft"][aircraft_id]
        self.target = self.st["target"].get(aircraft_id)
        self.beliefs = {}       # peer -> (tau, t, rate) from its last state message
        self.confirmed = {}     # contact -> latest known observation end (real time)
        self.zones, self.reclassified = [], set()
        self.aborted = False
        self.prev = None        # (t, tau, rate) of the previous step
        self.seen = set()
        self.to_announce = []   # [items, sends left]
        self.waited = 0.0
        self.t_loss = None
        self.sent_rate = 1.0
        self.state_left = 0
        self.last_state_t = -math.inf

    # ---- knowledge -------------------------------------------------------------------------
    def _learn(self, obs):
        t = obs["t"]
        if self.t_loss is None:
            self.t_loss = obs["t_loss"]
            for c, ends in self.st["confirm_end"].items():
                done = [e for e in ends if e <= self.t_loss - 1e-6]
                if done:
                    self.confirmed[c] = min(done)
        for e in obs["sensed"]:
            if e["kind"] == "reclassify":
                self._add_reclass(e["contact"])
            elif e["kind"] == "popup_zone":
                self._add_zone(e["center"][0], e["center"][1], e["radius_m"], e["top_m"])
        for m in obs["inbox"]:
            p = m["payload"]
            if isinstance(p, list):
                self._parse(p, m)
        if self.ac.type == SCOUT:
            for c in obs["own_observed"]:
                if c in self.seen:
                    continue
                self.seen.add(c)
                if c in self.st["engaged"] and c not in self.confirmed:
                    end = t
                    if self.prev is not None and self.prev[2] > 0:
                        pt, ptau, prate = self.prev
                        deps = [self.clock.dep[k] for k, wp in enumerate(self.clock.route.waypoints)
                                if wp.action == "observe" and wp.target == c and ptau - 1e-9 <= self.clock.dep[k]]
                        if deps:
                            end = min(t, pt + (min(deps) - ptau) / prate)
                    self.confirmed[c] = end
                    self.to_announce.append([[c, math.ceil(end * 10)], CONFIRM_SENDS])

    def _add_reclass(self, c):
        if c not in self.reclassified:
            self.reclassified.add(c)
            self.to_announce.append([["!" + c], RELAY_SENDS])

    def _add_zone(self, x, y, r, top=200.0):
        if any(abs(z[0][0] - x) < 2 and abs(z[0][1] - y) < 2 for z in self.zones):
            return
        self.zones.append(((float(x), float(y)), float(r), float(top)))
        self.to_announce.append([["z", round(x), round(y), math.ceil(r)], RELAY_SENDS])

    def _parse(self, p, m):
        """Payload: optional [tau*10, rate*10] state, then items "C1",end*10 | "!C1" | "z",x,y,r."""
        num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
        i = 0
        if len(p) >= 2 and num(p[0]) and num(p[1]):
            self.beliefs[m["from"]] = (p[0] / 10.0, m["sent_t"], p[1] / 10.0)
            i = 2
        while i < len(p):
            v = p[i]
            if v == "z" and i + 3 < len(p) and all(num(x) for x in p[i + 1:i + 4]):
                self._add_zone(p[i + 1], p[i + 2], p[i + 3] + 1.0)
                i += 4
            elif isinstance(v, str) and v.startswith("!"):
                self._add_reclass(v[1:])
                i += 1
            elif isinstance(v, str) and i + 1 < len(p) and num(p[i + 1]):
                end = p[i + 1] / 10.0
                self.confirmed[v] = min(self.confirmed.get(v, math.inf), end)
                i += 2
            else:
                break

    # ---- prediction ------------------------------------------------------------------------
    def _peers(self, t, pos, on_plan):
        """Predicted positions of nearby peers at the LOOK offsets: {peer: [pos or None, ...]}."""
        reach = 2.0 * self.st["vmax"] * LOOK[-1] + self.rules.d_sep_m + MARGIN_M
        out = {}
        for pid, clock in self.st["clocks"].items():
            if pid == self.id:
                continue
            b = self.beliefs.get(pid)
            if b is None:
                if on_plan:
                    continue
                hyps = [(t, 1.0)]
            else:
                hyps = [(b[0] + b[2] * (t - b[1]), b[2])]
            for h, (tau0, rate) in enumerate(hyps):
                tau0 = max(clock.start, min(clock.end, tau0))
                if rate > 0 and tau0 >= clock.end or rate < 0 and tau0 <= clock.start:
                    continue
                now = clock.position(tau0)
                if abs(now[0] - pos[0]) > reach or abs(now[1] - pos[1]) > reach:
                    continue
                seq = []
                for dt in LOOK:
                    tau = _tau_at(clock, tau0, rate, dt)
                    seq.append(clock.position(tau) if tau else None)
                if any(seq):
                    out[(pid, h)] = seq
        return out

    def _peer_near(self, t, pos):
        for pid, clock in self.st["clocks"].items():
            if pid == self.id:
                continue
            b = self.beliefs.get(pid)
            tau = t if b is None else b[0] + b[2] * (t - b[1])
            if tau <= clock.start or tau >= clock.end:
                continue
            q = clock.position(tau)
            if abs(q[0] - pos[0]) < NEAR_M and abs(q[1] - pos[1]) < NEAR_M and dist3(q, pos) < NEAR_M:
                return True
        return False

    def _clearance(self, tau, rate, peers):
        """(seconds until separation would break, smallest margin to separation plus MARGIN_M)."""
        sep = self.rules.d_sep_m
        need = sep + MARGIN_M
        # the shared-target exemption only holds for a group flying its plan into the strike
        tgt = self.st["contacts"].get(self.target) if self.target and rate == 1.0 else None
        worst, first = math.inf, math.inf
        for i, dt in enumerate(LOOK):
            mine = _tau_at(self.clock, tau, rate, dt)
            if not mine:
                if mine is None:
                    break
                continue
            p = self.clock.position(mine)
            for key, seq in peers.items():
                q = seq[i]
                if q is None:
                    continue
                if tgt is not None and key[0] not in self.beliefs and self.st["target"].get(key[0]) == self.target:
                    r = self.rules.exempt_radius_m - 5.0
                    if dist3(p, tgt.pos) < r and dist3(q, tgt.pos) < r:
                        continue
                d = dist3(p, q)
                if d - need < worst:
                    worst = d - need
                if d < sep + 2.0 and dt < first:
                    first = dt
        return first, worst

    def _zone_ahead(self, tau, rate=1.0, horizon=None):
        if not self.zones or rate == 0.0:
            return False
        clock = self.clock
        span = LOOK[-1] + 2.0 if horizon is None else horizon
        if rate > 0:
            lo, hi = tau, min(clock.end, tau + span)
        else:
            lo, hi = max(clock.start, tau - span), tau
        if hi <= lo:
            return False
        taus = [lo] + clock.breakpoints(lo, hi) + [hi]
        pts = [clock.position(x) for x in taus]
        return any(segment_enters_cylinder(a, b, c, r + ZONE_MARGIN_M, top)
                   for c, r, top in self.zones for a, b in zip(pts, pts[1:]))

    # ---- decision --------------------------------------------------------------------------
    def step(self, obs):
        t, tau = obs["t"], obs["tau"]
        self._learn(obs)
        on_plan = obs["mode"] == "route" and abs(tau - t) < 1e-6
        if not self.aborted and (self._zone_ahead(tau, 1.0, self.clock.end - tau) or (self.target and self.target in self.reclassified)):
            self.aborted = True
        wanted = -1.0 if self.aborted or obs["mode"] == "return" else 1.0
        if wanted > 0 and self.target and tau + 1.0 >= self.clock.end:
            te = t + (self.clock.end - tau)
            end = self.confirmed.get(self.target)
            if end is None or end > te - self.rules.confirm_margin_s - 0.05:
                self.waited += 1.0
                wanted = -1.0 if self.waited > WAIT_S else 0.0
                if wanted < 0:
                    self.aborted = True
        options = [wanted] + [r for r in (0.0, -1.0, 1.0) if r != wanted]
        if self.aborted:
            options = [r for r in options if r <= 0]
        elif wanted == 0.0:
            options = [r for r in options if r <= 0]
        safe = [r for r in options if not self._zone_ahead(tau, r)]
        options = safe or [0.0]
        rate, best = options[0], None
        peer_cache = {}
        for r in options:
            plan_like = on_plan and r == 1.0
            if plan_like not in peer_cache:
                peer_cache[plan_like] = self._peers(t, obs["pos"], plan_like)
            c = self._clearance(tau, r, peer_cache[plan_like])
            if c[1] >= 0:
                rate = r
                break
            if best is None or c > best:
                rate, best = r, c
        message = []
        off_plan = not (on_plan and rate == 1.0)
        if rate != self.sent_rate:
            self.sent_rate, self.state_left = rate, STATE_REPEATS
        if off_plan and (self.state_left > 0 or t - self.last_state_t >= HEARTBEAT_S
                         or self._peer_near(t, obs["pos"])):
            message += [round(tau * 10), round(rate * 10)]
            self.state_left = max(0, self.state_left - 1)
            self.last_state_t = t
        if self.to_announce:
            item = self.to_announce[0]
            message += item[0]
            item[1] -= 1
            if item[1] <= 0:
                self.to_announce.pop(0)
        self.prev = (t, tau, rate)
        if rate == 1.0:
            return {"kind": "continue"}, message or None
        if rate == 0.0:
            return {"kind": "hold"}, message or None
        return {"kind": "return"}, message or None
