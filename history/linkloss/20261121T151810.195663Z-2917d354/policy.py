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

Messages cost score, so the only messages are rate changes, "observed" notices and news of
a sensed danger (a reclassified contact or a pop-up zone). News is shared and relayed so the
drone it affects hears it long before it would sense it at 300 m: each drone that learns a
danger sends it NEWS_SENDS times, and stops early once two peers have echoed it back (they
have it and are spreading it: an implicit acknowledgement, at no extra message cost).

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
NEWS_SENDS = 0          # times a drone sends each danger it learns; 0 = off (3 measured worse: more R8, see README)
NEWS_GAP_S = 2.0        # seconds between those sends
NEWS_ECHOES = 2         # stop sending a danger once this many peers have been heard sending it
MAX_TEXT = 62           # 64 bytes minus the two quotes of a JSON string

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
        self.news = {}                 # danger key -> {"rec", "left", "next", "echoes"}
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
        t = obs["t"]
        for e in obs["sensed"]:
            if e["kind"] == "reclassify":
                self._learn_reclassified(e["contact"], t)
            elif e["kind"] == "popup_zone":
                self._learn_zone((e["center"][0], e["center"][1], e["radius_m"], e["top_m"]), t)
        for m in obs["inbox"]:
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
            item = self.news[key] = {"rec": rec, "left": NEWS_SENDS, "next": t, "echoes": set()}
        if sender is not None:
            item["echoes"].add(sender)
            if len(item["echoes"]) >= NEWS_ECHOES:
                item["left"] = 0          # peers have it and are spreading it

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
        records = []
        if self.ac.type == SCOUT:
            for c in obs["own_observed"]:
                if c not in self.reported:
                    self.reported[c] = int(math.ceil(t))   # it ended during the last step
                    if c in self.last_engage:
                        self.announce[c] = self.last_engage[c] + ANNOUNCE_AFTER_S
            live = sorted(c for c, until in self.announce.items() if t <= until)
            records += [("O", self.contact_index[c], self.reported[c]) for c in live[:2]]
        changed = self.sent is None and rate != 1.0 or self.sent is not None and rate != self.sent[0]
        off = abs(tau - t) > 0.25 or rate != 1.0
        due = self.sent is not None and off and t - self.last_sent >= (REPEAT_S if self.repeats < 1 else REFRESH_S)
        if changed:
            self.sent, self.repeats = (rate, t), 0
        elif due:
            self.repeats += 1
        if changed or due:
            records.insert(0, ("V", tau, rate))         # own state first: separation depends on it
            self.last_sent = t
        for item in self.news.values():
            if item["left"] > 0 and t >= item["next"]:
                records.append(item["rec"])
                item["left"] -= 1
                item["next"] = t + NEWS_GAP_S
        return encode_records(records, self.cw) or None
