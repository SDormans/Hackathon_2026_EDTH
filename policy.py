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

Coordination, using what every drone knows of its peers (plan + announced states + news):

- TRUST_PLAN: two aircraft both on schedule are separated by the validated plan, so the
  separation check skips that pair. Second-guessing it (55 m margin, 75 m exemption) made
  strike partners at a shared target back off, hold and reverse into one another.
- EXACT_OBS_END: a scout reports its observation end from its route clock (rounded up), not
  the second it noticed it; effectors then need only 0.1 s of slack past confirm_margin_s, and
  no longer slow down just short of a plan that had 12 s between observation and strike.
- GROUP_SYNC: a confirmed effector holds short while its strike partners' predicted
  engagements are further behind it than the plan's own spread (or sync window - 6 s).
- SCOUT_WAIT: an unconfirmed effector waits for a scout it knows is late (from the scout's
  announced state) for as long as the contact's window allows, instead of a fixed 15 s.

Messages cost score, so the only messages are rate changes, "observed" notices and news of
a sensed danger (a reclassified contact or a pop-up zone). NEWS_MODE picks how news spreads:

- "off": never sent;
- "burst": each drone that learns a danger sends it NEWS_SENDS times, stopping early once two
  peers have echoed it back;
- "forward" (store and forward): each drone keeps every danger it knows and, per danger, the
  peers it knows have it (it heard the danger from them, or sent it FWD_TRIES times while they
  were close). Each second it predicts every peer from the plan and sends a danger only when a
  peer that may lack it is within its estimated reach. The reach is learnt from how far away
  peers are heard, since the radio range is not observed.

Wire format: one JSON string of concatenated records. The first character is the type and
fixes the length of what follows; fields are base-93 digits over printable ASCII without
'"' and '\\', so every character costs one byte.

    V tau(3) rate(1)          my route time (0.1 s) and rate: announced deviation
    O contact(c) end_t(2)     observation of contact ended at end_t (s)
    R contact(c)              contact reclassified (now friendly)
    Z x(2) y(2) radius(1)     pop-up zone: centre to 1 m, radius rounded up (top 200 m)

Contacts are numbered in the scenario's order, which every drone knows; c is 1 digit for up
to 93 contacts, else 2. A decoder stops at
the first record it does not understand, so a malformed message never raises.
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
NEWS_SENDS = 3          # burst mode: times a drone sends each danger it learns
NEWS_GAP_S = 2.0        # seconds between those sends
NEWS_ECHOES = 2         # stop sending a danger once this many peers have been heard sending it
NEWS_MODE = "forward"   # "off", "burst" or "forward" (store and forward)
FWD_TRIES = 3           # sends at close range before a peer is assumed to have the danger
FWD_GAP_S = 2.0         # seconds between those sends
FWD_FAR_TRIES = 6       # sends at far range (delivery uncertain), never counted as delivered
FWD_FAR_GAP_S = 5.0
FWD_CLOSE_SHARE = 0.5   # close = within this share of the estimated range (delivery ~95 %)
FWD_FAR_SHARE = 0.95    # far = up to this share of it
FWD_RANGE_MIN_M = 240.0  # range estimate before anything was heard (the smallest range in the grid)
MAX_TEXT = 62           # 64 bytes minus the two quotes of a JSON string
TRUST_PLAN = True       # two aircraft both on schedule are separated by the validated plan: skip the check
ON_PLAN_S = 0.25        # route-clock offset still counted as on schedule
GROUP_SYNC = True       # a confirmed effector holds short until its strike partners can engage within sync
SYNC_SLACK_S = 6.0      # sync window minus this is the spread aimed for
SCOUT_WAIT = True       # an unconfirmed effector waits for a late scout while its window allows
SCOUT_WAIT_PAD_S = 5.0
EXACT_OBS_END = True    # a scout reports its observation end from its route clock, not the step it noticed it
CONFIRM_SLACK_S = 0.1
SAFE_STEP = True        # every move must keep d_sep + STEP_MARGIN_M from every peer's reachable positions this step
STEP_MARGIN_M = 5.0
FORMATION_M = 120.0     # an on-schedule peer this close that must stop is expected to stop where it is
REVERSE_LOOK_S = 8.0    # a reversal must keep clear of on-schedule peers' next seconds
STALE_CAP_S = 3.0       # route-time drift assumed at most for a silent off-plan peer
ESCAPE_GAIN_M = 2.0     # with no clear move, leave a hold only for this much more distance
HEARTBEAT_NEAR_M = 300.0  # an off-plan aircraft re-announces its state every second while a peer is this close

# ---------------------------------------------------------------------- compact wire format
ALPHABET = "".join(chr(c) for c in range(32, 127) if chr(c) not in '"\\')
BASE = len(ALPHABET)    # 93
DIGIT = {ch: i for i, ch in enumerate(ALPHABET)}
COORD_OFFSET = 1000     # x, y in metres + offset, in two digits: -1000 m to 7648 m
RADIUS_OFFSET = 40      # radius - offset in one digit: 40 m to 132 m
ZONE_TOP_M = 200.0
ZONE_PAD_M = 1.5        # decoded zones are inflated to cover the rounding of the centre


def enc(value, width):
    """Non-negative integer as `width` base-93 characters (clamped to the range)."""
    value = max(0, min(int(value), BASE ** width - 1))
    out = []
    for _ in range(width):
        value, r = divmod(value, BASE)
        out.append(ALPHABET[r])
    return "".join(reversed(out))


def dec(text):
    value = 0
    for ch in text:
        value = value * BASE + DIGIT[ch]
    return value


def _fields(cw):
    return {"V": 4, "O": cw + 2, "R": cw, "Z": 5}


def encode_records(records, cw=1):
    """Records (tuples) to one message string; records that do not fit are left out.

    `cw` is the width of a contact number: 1 digit for up to 93 contacts, else 2. A record whose
    values fall outside the format's range is left out rather than clamped to a wrong value.
    """
    text = ""
    for rec in records:
        kind = rec[0]
        if kind == "V":
            part = "V" + enc(round(rec[1] * 10), 3) + enc(round((rec[2] + 1.0) * 20), 1)
        elif kind == "O" and rec[1] < BASE ** cw:
            part = "O" + enc(rec[1], cw) + enc(rec[2], 2)
        elif kind == "R" and rec[1] < BASE ** cw:
            part = "R" + enc(rec[1], cw)
        elif kind == "Z":
            x, y = round(rec[1]) + COORD_OFFSET, round(rec[2]) + COORD_OFFSET
            r = math.ceil(rec[3]) - RADIUS_OFFSET
            if not (0 <= x < BASE ** 2 and 0 <= y < BASE ** 2 and 0 <= r < BASE):
                continue
            part = "Z" + enc(x, 2) + enc(y, 2) + enc(r, 1)
        else:
            continue
        if len(text) + len(part) <= MAX_TEXT:
            text += part
    return text


def decode_records(text, cw=1):
    """Message string to records; stops at the first unknown or truncated record."""
    out, i = [], 0
    if not isinstance(text, str):
        return out
    fields = _fields(cw)
    while i < len(text):
        kind = text[i]
        n = fields.get(kind)
        body = text[i + 1:i + 1 + n] if n else ""
        if n is None or len(body) < n or any(ch not in DIGIT for ch in body):
            break
        if kind == "V":
            out.append(("V", dec(body[:3]) / 10.0, dec(body[3]) / 20.0 - 1.0))
        elif kind == "O":
            out.append(("O", dec(body[:cw]), dec(body[cw:])))
        elif kind == "R":
            out.append(("R", dec(body)))
        else:
            out.append(("Z", dec(body[:2]) - COORD_OFFSET, dec(body[2:4]) - COORD_OFFSET,
                        dec(body[4]) + RADIUS_OFFSET))
        i += 1 + n
    return out


def _d(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def _seg_dist(p1, q1, p2, q2):
    """Smallest distance between segments p1-q1 and p2-q2 (3D)."""
    d1 = [q1[i] - p1[i] for i in range(3)]
    d2 = [q2[i] - p2[i] for i in range(3)]
    r = [p1[i] - p2[i] for i in range(3)]
    a = sum(x * x for x in d1)
    e = sum(x * x for x in d2)
    f = sum(d2[i] * r[i] for i in range(3))
    if a <= 1e-12 and e <= 1e-12:
        return _d(p1, p2)
    if a <= 1e-12:
        s, u = 0.0, min(1.0, max(0.0, f / e))
    else:
        c = sum(d1[i] * r[i] for i in range(3))
        if e <= 1e-12:
            s, u = min(1.0, max(0.0, -c / a)), 0.0
        else:
            b = sum(d1[i] * d2[i] for i in range(3))
            den = a * e - b * b
            s = min(1.0, max(0.0, (b * f - c * e) / den)) if den > 1e-12 else 0.0
            u = (b * s + f) / e
            if u < 0.0:
                s, u = min(1.0, max(0.0, -c / a)), 0.0
            elif u > 1.0:
                s, u = min(1.0, max(0.0, (b - c) / a)), 1.0
    x = [p1[i] + d1[i] * s - p2[i] - d2[i] * u for i in range(3)]
    return math.sqrt(sum(v * v for v in x))


def _polyline(clock, lo, hi):
    """Planned positions over route times [lo, hi] (airborne part only), or None."""
    lo, hi = max(lo, clock.start), min(hi, clock.end)
    if hi <= clock.start + 1e-9 or lo >= clock.end - 1e-9 or hi < lo:
        return None
    taus = [lo] + clock.breakpoints(lo, hi) + [hi]
    return [clock.position(x) for x in taus]


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
        self.contact_ids = [c.id for c in scenario.contacts]          # shared numbering for the wire
        self.contact_index = {c: i for i, c in enumerate(self.contact_ids)}
        self.cw = 1 if len(self.contact_ids) <= BASE else 2
        self.news = {}                 # danger key -> {"rec", "left", "next", "echoes", "informed", ...}
        self.heard_max_d = 0.0         # farthest distance a peer was heard from (estimates the range)
        # observation end to engagement: reported ends are rounded up, so a small slack suffices
        self.margin = rules.confirm_margin_s + (CONFIRM_SLACK_S if EXACT_OBS_END else 1.5)
        self.own_obs_end = {wp.target: wp.t + wp.dwell_s for wp in self.route.waypoints if wp.action == "observe"}
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
        self.sync_s = rules.sync_window_s
        self.group = [aid for aid, (_, tgt) in self.peers.items() if self.target is not None and tgt == self.target]
        self.obs_wp = {}               # contact -> [(scout id, planned route time the observation ends)]
        for aid, r in routes.items():
            if aid != aircraft_id and aircraft[aid].type == SCOUT:
                for wp in r.waypoints:
                    if wp.action == "observe":
                        self.obs_wp.setdefault(wp.target, []).append((aid, wp.t + wp.dwell_s))
        # spread from my engagement to the latest partner's that is tolerated: what the plan has, or more
        planned = [self.peers[a][0].end - self.engage_t for a in self.group] if self.target else []
        self.sync_spread = min(self.sync_s - 0.5, max([self.sync_s - SYNC_SLACK_S] + [p + 0.5 for p in planned]))
        c = self.contacts.get(self.target) if self.target else None
        self.window_end = c.window[1] if (c is not None and c.window) else None
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
        t = obs["t"]
        for e in obs["sensed"]:
            if e["kind"] == "reclassify":
                self._learn_reclassified(e["contact"], t)
            elif e["kind"] == "popup_zone":
                self._learn_zone((e["center"][0], e["center"][1], e["radius_m"], e["top_m"]), t)
        for m in obs["inbox"]:
            where = self._peer_where(m["from"], t)
            if where is not None:                          # how far a message can come from
                self.heard_max_d = max(self.heard_max_d, _d(where, obs["pos"]))
            for rec in decode_records(m["payload"], self.cw):
                kind = rec[0]
                if kind == "V":
                    self.peer_state[m["from"]] = (rec[1], rec[2], m["sent_t"])
                elif kind == "O" and rec[1] < len(self.contact_ids):
                    c, end = self.contact_ids[rec[1]], rec[2]
                    if c not in self.confirmed or end < self.confirmed[c]:
                        self.confirmed[c] = end
                elif kind == "R" and rec[1] < len(self.contact_ids):
                    self._learn_reclassified(self.contact_ids[rec[1]], t, m["from"])
                elif kind == "Z":
                    self._learn_zone((rec[1], rec[2], rec[3] + ZONE_PAD_M, ZONE_TOP_M), t, m["from"])

    def _note_news(self, key, rec, t, sender):
        """Queues a danger for sending, or counts a peer's echo of one we already have."""
        item = self.news.get(key)
        if item is None:
            item = self.news[key] = {"rec": rec, "left": NEWS_SENDS if NEWS_MODE == "burst" else 0, "next": t,
                                     "echoes": set(), "informed": set(), "tries": {}, "far": {}, "next_to": {}}
        if sender is not None:
            item["echoes"].add(sender)
            item["informed"].add(sender)  # it sent the danger, so it has it
            if len(item["echoes"]) >= NEWS_ECHOES:
                item["left"] = 0          # peers have it and are spreading it

    def _peer_where(self, aid, t):
        """Predicted position of a peer that can still receive: airborne, or on the ground before take-off."""
        if aid not in self.peers:
            return None
        p = self._peer_pos(aid, t)
        if p is None:
            clock = self.peers[aid][0]
            if aid not in self.peer_state and t <= clock.start:
                p = clock.pos[0]
        return p

    def _forward(self, obs, records):
        """Store and forward: add each danger a peer within reach may lack (see NEWS_MODE).

        The range is estimated as the farthest distance a peer was heard from. A peer within half of
        it receives with about 95 % chance, so FWD_TRIES sends there count as delivered; farther out
        a few spaced sends are tried without counting them.
        """
        t, here = obs["t"], obs["pos"]
        rng = max(FWD_RANGE_MIN_M, self.heard_max_d)
        close_m, far_m = FWD_CLOSE_SHARE * rng, FWD_FAR_SHARE * rng
        dist = {}
        for item in self.news.values():
            targets = []
            for pid in self.peers:
                if pid in item["informed"] or t < item["next_to"].get(pid, -1e9):
                    continue
                if pid not in dist:
                    p = self._peer_where(pid, t)
                    dist[pid] = None if p is None else _d(p, here)
                d = dist[pid]
                if d is None or d > far_m:
                    continue
                if d <= close_m:
                    targets.append((pid, True))
                elif item["far"].get(pid, 0) < FWD_FAR_TRIES:
                    targets.append((pid, False))
            if not targets:
                continue
            records.append(item["rec"])
            for pid, close in targets:
                if close:
                    n = item["tries"][pid] = item["tries"].get(pid, 0) + 1
                    item["next_to"][pid] = t + FWD_GAP_S
                    if n >= FWD_TRIES:
                        item["informed"].add(pid)   # three sends at close range: assume it arrived
                else:
                    item["far"][pid] = item["far"].get(pid, 0) + 1
                    item["next_to"][pid] = t + FWD_FAR_GAP_S

    def _learn_reclassified(self, contact, t, sender=None):
        self.reclassified.add(contact)
        if contact in self.contact_index:
            self._note_news(("R", contact), ("R", self.contact_index[contact]), t, sender)

    def _learn_zone(self, z, t, sender=None):
        same = next((k for k in self.zones if abs(k[0] - z[0]) <= 3 and abs(k[1] - z[1]) <= 3
                     and abs(k[2] - z[2]) <= 3), None)
        if same is None:
            self.zones.append(z)
            same = z
        self._note_news(("Z", round(same[0]), round(same[1])), ("Z", same[0], same[1], same[2]), t, sender)

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
        if TRUST_PLAN and self._on_plan(t, tau, rate):
            near = [aid for aid in near if not self._peer_on_plan(aid, t)]
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

    # ------------------------------------------------------------------ coordination
    def _peer_eta(self, aid, tau_end, t):
        """Predicted real time a peer reaches route time `tau_end`, from its announced state (None if not moving)."""
        tau0, rate, t0 = self.peer_state.get(aid, (None, 1.0, None))
        if tau0 is None:
            return tau_end                       # on plan
        tau_now = tau0 + rate * (t - t0)
        if tau_now >= tau_end:
            return t
        if rate <= 0.0:
            return t + (tau_end - tau_now)       # held or turning: at best it resumes now
        return t + (tau_end - tau_now) / rate

    def _group_latest(self, t):
        """Latest predicted engagement among strike partners still expected to engage."""
        latest = -math.inf
        for aid in self.group:
            clock = self.peers[aid][0]
            tau0, rate, _ = self.peer_state.get(aid, (None, 1.0, None))
            if rate < 0.0:
                continue                         # turned back: not waited for
            eta = self._peer_eta(aid, clock.end, t)
            if eta >= t - 1.0 or tau0 is None:
                latest = max(latest, eta)
        return latest

    def _grace_until(self, t):
        """How long an unconfirmed effector waits: the fixed grace, or longer for a scout known to be late."""
        until = self.engage_t + HOLD_GRACE_S
        if not SCOUT_WAIT:
            return until
        for aid, tau_end in self.obs_wp.get(self.target, []):
            if aid in self.peer_state and self.peer_state[aid][1] >= 0.0:
                until = max(until, self._peer_eta(aid, tau_end, t) + self.margin + SCOUT_WAIT_PAD_S)
        if self.window_end is not None:
            until = min(until, max(self.engage_t + HOLD_GRACE_S, self.window_end))
        return until

    def _on_plan(self, t, tau, rate):
        return rate == 1.0 and abs(tau - t) <= ON_PLAN_S and not self.aborted

    def _peer_on_plan(self, aid, t):
        st = self.peer_state.get(aid)
        if st is None:
            return True
        tau0, rate, t0 = st
        return rate == 1.0 and abs(tau0 - t0) <= ON_PLAN_S

    # ------------------------------------------------------------------ decision
    def _options(self, obs):
        """Rates that serve the intent, in order of preference, and whether engaging is allowed."""
        tau, t = obs["tau"], obs["t"]
        if self._hits_zone(tau):
            self.aborted = True
        if self.target is not None and self.target in self.reclassified:
            self.aborted = True
        if self.target is not None and not self.aborted and self.target not in self.confirmed \
                and t > self._grace_until(t):
            self.aborted = True
        if self.aborted:
            return [-1.0, 0.0, 0.5, 1.0], False
        if self.target is not None and self.target in self.confirmed:
            if t + (self.engage_t - tau) < self.confirmed[self.target] + self.margin:
                return [1.0, 0.5, 0.0, -1.0], False   # confirmed too recently: not yet
            if GROUP_SYNC and self._group_latest(t) - (t + (self.engage_t - tau)) > self.sync_spread:
                return [1.0, 0.5, 0.0, -1.0], False   # partners are late: wait for them
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
        if SAFE_STEP:
            options = self._safe_options(t, tau, options, may_engage)
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

    # ------------------------------------------------------------------ one-step safety filter
    def _peer_reach(self, aid, t, step=1.0):
        """Planned positions a peer can occupy during [t, t + step], or None (not airborne)."""
        clock = self.peers[aid][0]
        st = self.peer_state.get(aid)
        if st is None or self._peer_on_plan(aid, t):
            c, k = t, 0.0
        else:
            tau0, rate, t0 = st
            # decisions after the announced one are unknown; changes are announced at once and repeated,
            # so a long silence means no change (or a lost aircraft) rather than unbounded drift
            c, k = tau0 + rate * (t - t0), min(max(0.0, t - t0 - 1.0), STALE_CAP_S) + 0.1
        return _polyline(clock, c - k - step, c + k + step)

    def _peer_nominal(self, aid, t):
        """Planned positions a peer occupies during [t, t + 1] if it keeps its announced state."""
        clock = self.peers[aid][0]
        st = self.peer_state.get(aid)
        if st is None or self._peer_on_plan(aid, t):
            return _polyline(clock, t, t + 1.0)
        tau0, rate, t0 = st
        c, k = tau0 + rate * (t - t0), min(max(0.0, t - t0 - 1.0), STALE_CAP_S) + 0.1
        return _polyline(clock, c + min(0.0, rate) - k, c + max(0.0, rate) + k)

    def _polyline_point(self, aid, t):
        clock = self.peers[aid][0]
        return [clock.position(min(max(t, clock.start), clock.end))]

    def _peer_blocked(self, aid, t, near):
        """Whether an on-schedule peer close to me must stop this step for an off-schedule aircraft."""
        key = (aid, t)
        if key in self._blocked:
            return self._blocked[key]
        clock = self.peers[aid][0]
        out = False
        here = self.clock.position(min(max(t, self.clock.start), self.clock.end))
        seg = _polyline(clock, t, t + 1.0)
        if seg is not None and _d(seg[0], here) <= FORMATION_M:
            need = self.sep - SEP_MARGIN_M + STEP_MARGIN_M
            tgt = self.peers[aid][1]
            for other, _ in near:
                if other == aid or self._peer_on_plan(other, t):
                    continue
                band = self._peer_reach(other, t, 0.0)
                if band is None:
                    continue
                otgt = self.peers[other][1]
                centre = self.contacts[tgt].pos if (tgt is not None and tgt == otgt) else None
                for a0, a1 in (list(zip(seg, seg[1:])) or [(seg[0], seg[0])]):
                    for b0, b1 in (list(zip(band, band[1:])) or [(band[0], band[0])]):
                        if centre is not None and all(_d(q, centre) <= self.exempt for q in (a0, a1, b0, b1)):
                            continue
                        if _seg_dist(a0, a1, b0, b1) < need:
                            out = True
                            break
                    if out:
                        break
                if out:
                    break
        self._blocked[key] = out
        return out

    def _step_worst(self, t, tau, rate, near, nominal=False):
        """Smallest distance between my move at `rate` this step and the peers' reachable positions."""
        mine = _polyline(self.clock, min(tau, tau + rate), max(tau, tau + rate))
        if mine is None or tau <= self.clock.start + 1e-9 and rate <= 0.0:
            return math.inf            # on the ground, or landed
        # one more step on schedule is covered by the validated plan, even when I mean to leave it
        trusted = TRUST_PLAN and rate == 1.0 and abs(tau - t) <= ON_PLAN_S
        worst = math.inf
        hold_on_plan = rate == 0.0 and abs(tau - t) <= ON_PLAN_S
        for aid, reach in near:
            if self._peer_on_plan(aid, t) and (trusted or hold_on_plan) and FORMATION_M > 0.0:
                blocked = self._peer_blocked(aid, t, near)
                if hold_on_plan and blocked:
                    continue           # held at the same route time as the peer: as planned
                if trusted and blocked:
                    reach = self._polyline_point(aid, t)   # it stops where it is now
                elif trusted:
                    continue
            elif trusted and self._peer_on_plan(aid, t):
                continue
            if trusted and not nominal:
                # on schedule has the right of way: an off-plan peer checks its own moves against mine,
                # so only where it may be now counts
                reach = self._peer_reach(aid, t, 0.0)
                if reach is None:
                    continue
            tgt = self.peers[aid][1]
            centre = self.contacts[tgt].pos if (tgt is not None and tgt == self.target) else None
            if REVERSE_LOOK_S > 0.0 and rate < 0.0 and not nominal and self._peer_on_plan(aid, t):
                # turning back must not leave me in the way of on-schedule traffic coming up behind
                ahead = _polyline(self.peers[aid][0], t + 1.0, t + 1.0 + REVERSE_LOOK_S)
                for b0, b1 in (list(zip(ahead, ahead[1:])) or [(ahead[0], ahead[0])]) if ahead else ():
                    if centre is None or not all(_d(q, centre) <= self.exempt for q in (mine[0], b0, b1)):
                        worst = min(worst, _seg_dist(mine[0], mine[0], b0, b1))
            mine_segs = list(zip(mine, mine[1:])) or [(mine[0], mine[0])]
            reach_segs = list(zip(reach, reach[1:])) or [(reach[0], reach[0])]
            for a0, a1 in mine_segs:
                for b0, b1 in reach_segs:
                    if centre is not None and all(_d(q, centre) <= self.exempt for q in (a0, a1, b0, b1)):
                        continue       # the usual exemption around a shared target
                    worst = min(worst, _seg_dist(a0, a1, b0, b1))
        return worst

    def _safe_options(self, t, tau, options, may_engage):
        """Options whose move this step keeps clear of every peer; else the one keeping most distance."""
        self._blocked = {}
        here = self.clock.position(tau)
        near, self.near_d = [], math.inf
        for aid in self.peers:
            reach = self._peer_reach(aid, t)
            if reach is None:
                continue
            d = min(_d(q, here) for q in reach)
            self.near_d = min(self.near_d, d)
            if d < NEAR_M:
                near.append((aid, reach))
        if not near:
            return options
        need = self.sep - SEP_MARGIN_M + STEP_MARGIN_M
        cand = options + [r for r in (0.0, 1.0, -1.0) if r not in options and self._allowed(tau, r, may_engage)]
        worst = {r: self._step_worst(t, tau, r, near) for r in cand}
        safe = [r for r in options if worst[r] >= need]
        if safe:
            return safe
        safe = [r for r in cand if worst[r] >= need]
        if safe:
            return safe[:1]
        # nothing is clear whatever the peers do: each peer answers for its own move, so take the move
        # that keeps most distance from where they go if they keep their announced states; prefer to
        # stay put (predictable, announced) unless a move keeps clearly more distance
        nominal = [(aid, self._peer_nominal(aid, t)) for aid, _ in near]
        nominal = [(aid, reach) for aid, reach in nominal if reach is not None]
        worst = {r: self._step_worst(t, tau, r, nominal, nominal=True) for r in cand}
        best = max(cand, key=lambda r: worst[r])
        return [best if worst[best] >= worst[0.0] + ESCAPE_GAIN_M else 0.0]

    def _obs_end_report(self, c, obs):
        """Whole second at or after the real end of my observation of `c` (it ended during the last step)."""
        t, tau = obs["t"], obs["tau"]
        end = t
        if EXACT_OBS_END and c in self.own_obs_end and self.rate == 1.0:
            # moving at full rate over the last step: the end lies (tau - tau_end) seconds back
            end = min(t, max(t - 1.0, t - (tau - self.own_obs_end[c])))
        return int(math.ceil(end - 1e-6))

    def _message(self, obs, tau, rate):
        t = obs["t"]
        records = []
        if self.ac.type == SCOUT:
            for c in obs["own_observed"]:
                if c not in self.reported:
                    self.reported[c] = self._obs_end_report(c, obs)
                    if c in self.last_engage:
                        self.announce[c] = max(self.last_engage[c], t) + ANNOUNCE_AFTER_S
            live = sorted(c for c, until in self.announce.items() if t <= until)
            records += [("O", self.contact_index[c], self.reported[c]) for c in live[:2]]
        changed = self.sent is None and rate != 1.0 or self.sent is not None and rate != self.sent[0]
        off = abs(tau - t) > 0.25 or rate != 1.0
        gap = REPEAT_S if self.repeats < 1 else REFRESH_S
        if SAFE_STEP and getattr(self, "near_d", math.inf) < HEARTBEAT_NEAR_M:
            gap = 1.0                                   # peers nearby check their moves against my state
        due = self.sent is not None and off and t - self.last_sent >= gap
        if changed:
            self.sent, self.repeats = (rate, t), 0
        elif due:
            self.repeats += 1
        if changed or due:
            records.insert(0, ("V", tau, rate))         # own state first: separation depends on it
            self.last_sent = t
        if NEWS_MODE == "forward":
            self._forward(obs, records)
        elif NEWS_MODE == "burst":
            for item in self.news.values():
                if item["left"] > 0 and t >= item["next"]:
                    records.append(item["rec"])
                    item["left"] -= 1
                    item["next"] = t + NEWS_GAP_S
        return encode_records(records, self.cw) or None
