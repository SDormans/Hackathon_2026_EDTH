"""Records what every drone knows, second by second, during link-loss runs.

Offline analysis only: the recorder wraps each aircraft's policy, never changes an action or a
message, and writes per run what each drone knew and where each piece came from:

    self      its own state and decisions (position, rate, abort)
    plan      taken from the shared plan at the cut (observations completed before it)
    sensed    an event within 300 m of it
    heard     a peer's message
    inferred  derived by the policy with no new input (e.g. a timeout)

It also records each drone's belief of where its peers are (for policies with `_peer_pos`)
against where they really were, for every pair closer than --near metres, and when each
affected drone learned each event against when it needed to know.

    python3 analysis/linkloss_knowledge.py --levels L2,L3 --seeds 0-2                 # ad hoc, under output/
    python3 analysis/linkloss_knowledge.py --history --note "v2 news sharing" \
        --levels L1,L2,L3,L4 --seeds 0-4 --settings 1000x200,250x50 --workers 6      # kept in history/linkloss/
    python3 analysis/linkloss_knowledge.py compare                                    # every stored collection
    python3 analysis/linkloss_knowledge.py diff BEFORE_DIR AFTER_DIR                  # run by run: fixed, broken

Each separation failure gets a cause (base_traffic, shared_target_edge, lost_message,
silent_deviation, wrong_prediction, boxed_in, same_time_move, unexplained), with evidence, and
the report counts them. For policies with `_room`, steps near other drones also record the
predicted room of each move (`room`), which is what the boxed_in check reads.

A collection (one invocation) is one directory, so teammates' collections never conflict in Git:

    collection.json        settings, note, status, policy SHA-256, kit hash, Git state, per-setting results
    policy.py              the exact policy source that produced the data
    runs.jsonl             one line per run: case, setting, retention, S_loss, messages, failure
    report.md              the aggregate printed at the end
    cases/<case>.json.gz   {"scenario", "plan", "events"}: replay any run exactly
    runs/<run>.json.gz     {"schema", "summary", "facts", "steps", "near"}:
        summary  result, failures, event learning table (learned vs needed), failure context
        facts    one row each time a drone learns or changes a fact: t, drone, fact, value, source, from
        steps    one row per drone per second: own state, action, message sent, messages heard
        near     believed against real peer position whenever two drones are close

Read them back with `load_run(path)`, `load_case(path)` and `iter_collections()`.
"""
import argparse
import concurrent.futures as cf
import datetime
import gzip
import hashlib
import json
import math
import platform
import re
import secrets
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _kit  # noqa: E402,F401  makes fleetkit importable

from fleetkit.baseline import greedy_plan  # noqa: E402
from fleetkit.generator import generate  # noqa: E402
from fleetkit.linkloss.events import make_events  # noqa: E402
from fleetkit.linkloss.policies import load_policy  # noqa: E402
from fleetkit.linkloss.sim import RouteClock, simulate  # noqa: E402
from fleetkit.model import Scenario, load_json, parse_plan, save_json  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / "history" / "linkloss"   # Git-tracked knowledge store
SCHEMA = 1
MU = 0.002                                # message cost in S_loss, as fleetkit.linkloss.score
NEAR_M = 200.0
ROOM_RATES = (1.0, 0.5, 0.0, -1.0)        # moves whose predicted room is recorded near other drones
ROOM_NEAR_M = 150.0
BELIEF_TOL_M = 15.0                       # belief error that counts as "wrong" in the cause analysis


# --------------------------------------------------------------------------- knowledge extraction

def _zone_key(x, y):
    return f"zone:{round(x)},{round(y)}"


def extract(policy):
    """Returns the policy's current knowledge as {fact: value}.

    A policy may define `knowledge()` returning that dictionary itself; otherwise the attributes
    used by our policy.py and the shipped baselines are read when present.
    """
    if hasattr(policy, "knowledge"):
        return dict(policy.knowledge())
    facts = {}
    for c in getattr(policy, "reclassified", ()) or ():
        facts[f"rc:{c}"] = True
    for z in getattr(policy, "zones", ()) or ():
        facts[_zone_key(z[0], z[1])] = [round(z[2]), round(z[3])]
    for c, end in (getattr(policy, "confirmed", None) or {}).items():
        facts[f"obs:{c}"] = round(end, 1)
    for aid, state in (getattr(policy, "peer_state", None) or {}).items():
        facts[f"peer:{aid}"] = [round(v, 1) for v in state]
    if getattr(policy, "aborted", False):
        facts["self:aborted"] = True
    if hasattr(policy, "rate"):
        facts["self:rate"] = policy.rate
    return facts


def _event_fact(e):
    if e["kind"] == "reclassify":
        return f"rc:{e['contact']}"
    if e["kind"] == "popup_zone":
        return _zone_key(*e["center"])
    return None


def message_facts(policy, payload):
    """Fact keys a message carries, using the policy module's own decoder when it has one.

    Our policy.py sends compact strings (`decode_records`, contacts numbered in scenario order);
    other policies send JSON, which is searched for contact ids as a best effort.
    """
    step = getattr(type(policy), "step", None)
    decode = getattr(step, "__globals__", {}).get("decode_records")   # load_policy does not register the module
    ids = getattr(policy, "contact_ids", None)
    if callable(decode) and ids is not None and isinstance(payload, str):
        out = set()
        for rec in decode(payload, getattr(policy, "cw", 1)):
            if rec[0] == "R" and rec[1] < len(ids):
                out.add(f"rc:{ids[rec[1]]}")
            elif rec[0] == "O" and rec[1] < len(ids):
                out.add(f"obs:{ids[rec[1]]}")
            elif rec[0] == "Z":
                out.add(_zone_key(rec[1], rec[2]))
        return out
    try:
        text = json.dumps(payload, separators=(",", ":"))
    except (TypeError, ValueError):
        return set()
    return {"*" + text}


def _mentions(facts, fact):
    if fact in facts:
        return True
    name = fact.split(":", 1)[1]
    return any(f.startswith("*") and name in f for f in facts)


class Recorder:
    """Collects the rows of one run. Shared by the wrappers of one simulation (offline only)."""

    def __init__(self):
        self.facts, self.steps, self.beliefs = [], [], []
        self.last_pos = {}                     # drone -> (t, pos) of its latest step

    def close_to(self, t, aid, pos, radius=ROOM_NEAR_M):
        """True if another drone was within radius at its latest recorded step (at most 1 s old)."""
        self.last_pos[aid] = (t, pos)
        return any(o != aid and t - ot <= 1.0 and _dist(op, pos) <= radius
                   for o, (ot, op) in self.last_pos.items())


class Recorded:
    """Wraps one aircraft's policy: passes obs and results through unchanged, records knowledge."""

    def __init__(self, inner, aid, rec):
        self.inner, self.aid, self.rec = inner, aid, rec
        self.known = {}
        self.first = True

    def step(self, obs):
        action, message = self.inner.step(obs)
        t = obs["t"]
        sensed_facts = {_event_fact(e) for e in obs["sensed"]} - {None}
        senders = [m["from"] for m in obs["inbox"]]
        carried = [(m["from"], message_facts(self.inner, m["payload"])) for m in obs["inbox"]]
        now = extract(self.inner)
        for fact, value in now.items():
            if fact in self.known and self.known[fact] == value:
                continue
            carriers = [who for who, facts in carried if _mentions(facts, fact)]
            if fact.startswith("self:"):
                source, src = "self", None
            elif fact in sensed_facts:
                source, src = "sensed", None
            elif fact.startswith("peer:") and fact[5:] in senders:
                source, src = "heard", fact[5:]
            elif carriers:
                source, src = "heard", ",".join(carriers)
            elif self.first:
                source, src = "plan", None
            elif senders:
                source, src = "heard", ",".join(senders)
            else:
                source, src = "inferred", None      # derived by the policy without new input
            row = {"t": t, "drone": self.aid, "fact": fact, "value": value, "source": source}
            if src:
                row["from"] = src
            self.rec.facts.append(row)
        self.known = now
        self.first = False
        step = {"t": t, "drone": self.aid, "pos": [round(v, 1) for v in obs["pos"]], "tau": round(obs["tau"], 1),
                "mode": obs["mode"], "energy": round(obs["energy_left_wh"], 1), "action": action}
        if message is not None:
            step["sent"] = message
        if obs["inbox"]:
            step["heard"] = [{"from": m["from"], "sent_t": m["sent_t"], "payload": m["payload"]} for m in obs["inbox"]]
        room = getattr(self.inner, "_room", None)
        if callable(room) and self.rec.close_to(t, self.aid, obs["pos"]):
            try:   # read-only: _room predicts, it changes no state
                step["room"] = {f"{r:g}": round(room(t, obs["tau"], r), 1) for r in ROOM_RATES}
            except Exception:
                pass
        self.rec.steps.append(step)
        peer_pos = getattr(self.inner, "_peer_pos", None)
        peers = getattr(self.inner, "peers", None)
        if callable(peer_pos) and peers:
            bel = {}
            for pid in peers:
                try:
                    p = peer_pos(pid, t)
                except Exception:  # a belief we cannot read is simply not recorded
                    p = None
                bel[pid] = None if p is None else [round(v, 1) for v in p]
            self.rec.beliefs.append((t, self.aid, bel))
        return action, message


# --------------------------------------------------------------------------- per-run analysis

def _dist(a, b):
    return math.sqrt(sum((a[k] - b[k]) ** 2 for k in range(3)))


def _hdist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _actual_positions(steps):
    by_t = {}
    for s in steps:
        by_t.setdefault(s["t"], {})[s["drone"]] = s["pos"]
    return by_t


def near_rows(rec, near_m):
    """Believed against real position of each peer, whenever the two are within near_m."""
    actual = _actual_positions(rec.steps)
    rows = []
    for t, aid, bel in rec.beliefs:
        here = actual.get(t, {}).get(aid)
        if here is None:
            continue
        for pid, real in actual.get(t, {}).items():
            if pid == aid:
                continue
            d = _dist(here, real)
            if d > near_m:
                continue
            b = bel.get(pid)
            rows.append({"t": t, "drone": aid, "peer": pid, "real_dist": round(d, 1),
                         "believed_dist": None if b is None else round(_dist(here, b), 1),
                         "belief_error": None if b is None else round(_dist(b, real), 1)})
    return rows


def _zone_entry(clock, z, t_from):
    """First planned time at or after t_from at which the route is inside the zone, or None."""
    t = max(t_from, clock.start)
    while t <= clock.end:
        p = clock.position(t)
        if _hdist(p, z["center"]) <= z["radius_m"] and p[2] <= z["top_m"]:
            return t
        t += 1.0
    return None


def event_table(scenario, plan, events, facts):
    """For each event and each drone: when it learned it and how, and whether it was affected."""
    routes, _ = parse_plan(plan)
    clocks = {aid: RouteClock(r) for aid, r in routes.items()}
    targets = {aid: (r.waypoints[-1].target if r.waypoints[-1].action == "engage" else None) for aid, r in routes.items()}
    first = {}
    for f in facts:
        first.setdefault((f["drone"], f["fact"]), f)
    rows = []
    for e in events["events"]:
        fact = _event_fact(e)
        if fact is None:
            rows.append({"event": e["kind"], "aircraft": e["aircraft"], "t": e["t"]})
            continue
        for aid in routes:
            if e["kind"] == "reclassify":
                need = clocks[aid].end if targets[aid] == e["contact"] else None
            else:
                need = _zone_entry(clocks[aid], e, e["t"])
            got = first.get((aid, fact))
            row = {"event": fact, "t": e["t"], "drone": aid, "affected": need is not None,
                   "needed_by_plan_t": need, "learned_t": got["t"] if got else None,
                   "source": got["source"] if got else None, "from": got.get("from") if got else None}
            if need is not None and got:
                row["slack_s"] = round(need - got["t"], 1)
            rows.append(row)
    return rows


def _reach_delay(delays, n, share):
    """Seconds after the event until `share` of the n drones knew it, or None if never."""
    need = math.ceil(share * n)
    return delays[need - 1] if need <= len(delays) else None


def news_distribution(events, table, steps):
    """How far and how fast each danger spread through the fleet.

    The audience of an event is every drone airborne when it happened. Per event: coverage (share
    of the audience that ever knew it), seconds until 50 / 90 / 100 % knew, how many learned by
    sensing and by hearing, and the drones still airborne at the end that never knew (missed;
    drones that engaged, landed or were lost first are not counted as missed).
    """
    active = {}
    for st in steps:
        active.setdefault(st["t"], set()).add(st["drone"])
    last_seen = {}
    for st in steps:
        last_seen[st["drone"]] = max(last_seen.get(st["drone"], st["t"]), st["t"])
    end_t = max(active) if active else 0.0
    out = []
    for e in events["events"]:
        fact = _event_fact(e)
        if fact is None:
            continue
        t0 = min((t for t in active if t >= e["t"]), default=None)
        if t0 is None:
            continue
        audience = active[t0]
        rows = {r["drone"]: r for r in table if r.get("event") == fact and r["drone"] in audience}
        delays = sorted(r["learned_t"] - e["t"] for r in rows.values() if r["learned_t"] is not None)
        n = len(audience)
        missed = sorted(d for d in audience if rows.get(d, {}).get("learned_t") is None and last_seen.get(d) == end_t)
        src = {}
        for r in rows.values():
            if r["learned_t"] is not None:
                src[r["source"]] = src.get(r["source"], 0) + 1
        out.append({"event": fact, "t": e["t"], "audience": n, "knew": len(delays),
                    "coverage": round(len(delays) / n, 4) if n else None,
                    "t50_s": _reach_delay(delays, n, 0.5), "t90_s": _reach_delay(delays, n, 0.9),
                    "t100_s": _reach_delay(delays, n, 1.0), "by_source": src, "missed": missed})
    return out


SEP_RE = re.compile(r"R8 \(separation\): (\S+) and (\S+) come within ([\d.]+) m.*?t = ([\d.]+) s")


def failure_context(rec, failed, window=15):
    """For each separation failure: both drones' actions, beliefs of each other and messages near it."""
    out = []
    actual = _actual_positions(rec.steps)
    bel_at = {(t, aid): b for t, aid, b in rec.beliefs}
    steps_at = {(s["t"], s["drone"]): s for s in rec.steps}
    for reason in failed:
        m = SEP_RE.search(reason)
        if not m:
            out.append({"reason": reason})
            continue
        a, b, dmin, tf = m.group(1), m.group(2), float(m.group(3)), float(m.group(4))
        timeline = []
        for t in sorted(actual):
            if not (tf - window <= t <= tf + 2):
                continue
            row = {"t": t}
            for me, other in ((a, b), (b, a)):
                s = steps_at.get((t, me))
                if s is None:
                    continue
                bel = (bel_at.get((t, me)) or {}).get(other, "n/a")
                real = actual[t].get(other)
                row[me] = {"action": s["action"].get("kind"), "tau": s["tau"],
                           "sent": s.get("sent"), "heard": [h["from"] for h in s.get("heard", [])],
                           "believes_other_at": bel, "other_really_at": real,
                           "belief_error": round(_dist(bel, real), 1) if isinstance(bel, list) and real else None}
            timeline.append(row)
        out.append({"reason": reason, "pair": [a, b], "min_dist": dmin, "t": tf, "timeline": timeline})
    return out


CAUSES = {
    "base_traffic": "near the base: one drone taking off or landing next to another",
    "shared_target_edge": "two effectors of the same target at the edge of the exemption radius",
    "lost_message": "a drone sent its change, the other never heard it, and its belief was wrong",
    "silent_deviation": "a drone left its schedule without announcing it, and the other's belief was wrong",
    "wrong_prediction": "the other's messages arrived, but its belief was still wrong",
    "boxed_in": "beliefs were right, but no move kept the required room",
    "same_time_move": "beliefs were right; both drones changed their move in the same second",
    "unexplained": "beliefs were right and a safe move existed",
}


def classify_failures(rec, contexts, scenario, plan, sep_m):
    """Adds a `cause` (a key of CAUSES) and `evidence` to each separation failure context.

    Checked in order: where it happened (base, shared target), then whether either drone's
    belief of the other was wrong in the last seconds and why, then whether any move was safe.
    """
    routes, _ = parse_plan(plan)
    home = {aid: r.waypoints[0].pos for aid, r in routes.items()}
    target = {aid: (r.waypoints[-1].target if r.waypoints[-1].action == "engage" else None) for aid, r in routes.items()}
    contacts = scenario.contact_by_id()
    exempt = scenario.rules.exempt_radius_m
    steps_at = {(st["t"], st["drone"]): st for st in rec.steps}
    sent_by = {}
    for st in rec.steps:
        if "sent" in st:
            sent_by.setdefault(st["drone"], []).append(st["t"])
    for ctx in contexts:
        if "pair" not in ctx:
            ctx["cause"] = "not_separation"
            continue
        a, b, tf = ctx["pair"][0], ctx["pair"][1], ctx["t"]
        tl = ctx["timeline"]
        last = tl[-1] if tl else {}
        pa = (last.get(a) or {}).get("other_really_at") or None
        pb = (last.get(b) or {}).get("other_really_at") or None
        # where: the base (either home), or both near a shared target
        if pa and pb and min(_hdist(pb, home[a]), _hdist(pb, home[b]), _hdist(pa, home[a]), _hdist(pa, home[b])) < 150:
            ctx["cause"], ctx["evidence"] = "base_traffic", f"within 150 m of a home at t = {tf:.0f} s"
            continue
        ta, tb = target.get(a), target.get(b)
        if ta and ta == tb and ta in contacts and pa and pb:
            c = contacts[ta].pos
            da, db = _dist(pb, c), _dist(pa, c)
            if min(da, db) <= exempt + 30 and max(da, db) >= exempt - 30:
                ctx["cause"] = "shared_target_edge"
                ctx["evidence"] = f"{ta}: {a} at {db:.0f} m, {b} at {da:.0f} m, exemption {exempt:g} m"
                continue
        # beliefs in the last 6 s before the failure
        wrong = []
        for row in tl:
            if row["t"] < tf - 6:
                continue
            for me, other in ((a, b), (b, a)):
                e = (row.get(me) or {}).get("belief_error")
                if e is not None and e > BELIEF_TOL_M:
                    wrong.append((row["t"], me, other, e))
        if wrong:
            t_w, me, other, err = max(wrong, key=lambda w: w[3])
            sent = [t for t in sent_by.get(other, []) if tf - 20 <= t <= t_w]
            heard = {h["sent_t"] for t in range(int(tf) - 20, int(t_w) + 2)
                     for h in (steps_at.get((float(t), me)) or {}).get("heard", []) if h["from"] == other}
            missed = [t for t in sent if t not in heard]
            if missed:
                ctx["cause"] = "lost_message"
                ctx["evidence"] = f"{me} never heard {other}'s message of t = {missed[-1]:.0f} s; belief off by {err:.0f} m"
            elif not sent:
                ctx["cause"] = "silent_deviation"
                ctx["evidence"] = f"{other} sent nothing in the 20 s before; {me}'s belief off by {err:.0f} m"
            else:
                ctx["cause"] = "wrong_prediction"
                ctx["evidence"] = f"{me} heard {other} but its belief was off by {err:.0f} m at t = {t_w:.0f} s"
            continue
        # beliefs right: was any move safe at the last decisions?
        boxed = []
        for t in (tf - 2, tf - 1, float(int(tf))):
            for me in (a, b):
                room = (steps_at.get((float(int(t)), me)) or {}).get("room")
                if room and max(room.values()) < sep_m:
                    boxed.append((int(t), me, max(room.values())))
        if boxed:
            t_b, me, best = boxed[-1]
            ctx["cause"], ctx["evidence"] = "boxed_in", f"{me} at t = {t_b} s: best move kept {best:.0f} m (needs {sep_m:.0f})"
            continue
        changes = []
        for me in (a, b):
            prev = None
            for t in range(int(tf) - 6, int(tf) + 1):
                k = ((steps_at.get((float(t), me)) or {}).get("action") or {}).get("kind")
                if prev is not None and k != prev:
                    changes.append((t, me))
                prev = k
        same = sorted({t for t, me in changes if me == a} & {t for t, me in changes if me == b})
        if same:
            ctx["cause"], ctx["evidence"] = "same_time_move", f"both changed their move at t = {same[-1]} s"
        else:
            ctx["cause"], ctx["evidence"] = "unexplained", "beliefs within tolerance and a safe move existed"
    return contexts


def cause_counts(summaries):
    counts = {}
    for s in summaries:
        for ctx in s.get("failure_context") or []:
            key = ctx.get("cause") or "not_separation"
            if key == "not_separation":
                key = (ctx.get("reason") or "?").split(" ")[0]
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


# --------------------------------------------------------------------------- running

def parse_seeds(text):
    out = []
    for part in text.split(","):
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.extend(range(int(lo), int(hi) + 1))
        else:
            out.append(int(part))
    return out


def parse_settings(text):
    out = []
    for part in text.split(","):
        r, c = part.lower().split("x")
        out.append((float(r), float(c)))
    return out


def save_gz(path, obj):
    """Writes JSON to a gzip file (fixed mtime, so identical content gives identical bytes)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(obj, separators=(",", ":")).encode()
    with open(path, "wb") as fh, gzip.GzipFile(fileobj=fh, mode="wb", mtime=0) as gz:
        gz.write(raw)


def load_gz(path):
    with gzip.open(path, "rb") as fh:
        return json.loads(fh.read())


def prepare_case(level, seed, event_seed, case_path):
    """Writes scenario, greedy plan and events for one case (the same draw as check_policy.py)."""
    data = generate(level, seed)
    sc = Scenario.from_dict(data)
    plan = greedy_plan(sc)
    events = make_events(sc, plan, seed + 1 if event_seed is None else event_seed)
    save_gz(case_path, {"scenario": data, "plan": plan, "events": events})
    return case_path


def load_case(case_path):
    """Returns (Scenario, plan, events) of a stored case."""
    d = load_gz(case_path)
    return Scenario.from_dict(d["scenario"]), d["plan"], d["events"]


def load_run(run_path):
    """Returns a stored run: {"summary", "facts", "steps", "near"}."""
    return load_gz(run_path)


def _write_jsonl(path, rows):
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")


def run_one(task):
    case_path, policy_spec, setting, comm_seed, run_path, near_m = task
    run_path = Path(run_path)
    sc, plan, events = load_case(case_path)
    inner_factory = load_policy(policy_spec)
    rec = Recorder()

    def factory(scenario, plan_, aid):
        return Recorded(inner_factory(scenario, plan_, aid), aid, rec)

    started = time.monotonic()
    result = simulate(sc, plan, events, factory, setting=setting, run_seed=comm_seed)
    v_oracle = events.get("oracle_points") or 0.0
    retention = 0.0 if result.failed or not v_oracle else min(1.0, result.points / v_oracle)
    near = near_rows(rec, near_m)
    table = event_table(sc, plan, events, rec.facts)
    spread = news_distribution(events, table, rec.steps)
    context = failure_context(rec, result.failed)
    probe = inner_factory(sc, plan, next(iter(sorted(parse_plan(plan)[0]))))
    classify_failures(rec, context, sc, plan, getattr(probe, "sep", sc.rules.d_sep_m))
    errors = [r["belief_error"] for r in near if r["belief_error"] is not None]
    name = run_path.name.split(".")[0]
    summary = {
        "run": name, "case": f"{sc.level}-{sc.seed}", "setting": list(setting), "comm_seed": comm_seed,
        "t_loss": events["t_loss"], "events": events["events"],
        "failed": result.failed, "retention": round(retention, 4), "points": result.points, "oracle_points": v_oracle,
        "served": result.served, "lost": result.lost, "messages": result.messages,
        "messages_per_aircraft_per_min": round(result.messages_per_aircraft_per_min, 3),
        "s_loss": round(retention - MU * result.messages_per_aircraft_per_min, 4),
        "facts_recorded": len(rec.facts), "near_samples": len(near),
        "max_belief_error_near_m": max(errors) if errors else None,
        "news_distribution": spread, "event_learning": table, "failure_context": context, "seconds": round(time.monotonic() - started, 2),
    }
    save_gz(run_path, {"schema": SCHEMA, "summary": summary, "facts": rec.facts, "steps": rec.steps, "near": near})
    return summary


# --------------------------------------------------------------------------- provenance and the history store

def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _git(*args):
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def provenance(policy_spec):
    """What produced a collection: policy source hash, kit link-loss hash, Git state, Python."""
    kit_files = sorted((_kit.KIT / "fleetkit" / "linkloss").glob("*.py"))
    kit_hash = _sha256(b"".join(f.name.encode() + f.read_bytes() for f in kit_files))
    if Path(policy_spec).exists():
        data = Path(policy_spec).read_bytes()
        try:
            shown = str(Path(policy_spec).resolve().relative_to(ROOT))
        except ValueError:
            shown = Path(policy_spec).name
        pol = {"spec": shown, "sha256": _sha256(data), "short": _sha256(data)[:12]}
    else:
        pol = {"spec": policy_spec, "sha256": None, "short": policy_spec}
    status = _git("status", "--porcelain")
    return {"policy": pol, "kit_linkloss_sha256": kit_hash,
            "git": {"commit": _git("rev-parse", "HEAD"), "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
                    "dirty": bool(status) if status is not None else None},
            "python": platform.python_version(), "platform": platform.platform(terse=True)}


def by_setting(summaries):
    """Aggregate per communication setting: runs, failures, retention, messages, S_loss."""
    groups = {}
    for s in summaries:
        groups.setdefault(f"{s['setting'][0]:g}x{s['setting'][1]:g}", []).append(s)
    out = {}
    for key, rows in groups.items():
        ret = statistics.mean(r["retention"] for r in rows)
        msgs = statistics.mean(r["messages_per_aircraft_per_min"] for r in rows)
        out[key] = {"runs": len(rows), "failed": sum(bool(r["failed"]) for r in rows), "retention": round(ret, 4),
                    "messages_per_aircraft_per_min": round(msgs, 3), "s_loss": round(ret - MU * msgs, 4)}
        out[key].update(spread_stats([d for r in rows for d in r.get("news_distribution", [])]))
    return out


def spread_stats(dists):
    """Aggregate of news_distribution rows: mean coverage, share of events reaching 90 % / 100 %,
    median seconds to 90 %, and drones still flying at the end that never knew."""
    if not dists:
        return {"news_events": 0}
    t90 = [d["t90_s"] for d in dists if d["t90_s"] is not None]
    return {"news_events": len(dists),
            "coverage": round(statistics.mean(d["coverage"] for d in dists), 4),
            "reach90_share": round(sum(d["t90_s"] is not None for d in dists) / len(dists), 4),
            "reach100_share": round(sum(d["t100_s"] is not None for d in dists) / len(dists), 4),
            "t90_median_s": statistics.median(t90) if t90 else None,
            "missed_drones": sum(len(d["missed"]) for d in dists)}


def iter_collections(root=HISTORY):
    """Yields (directory, collection.json) for every stored collection, oldest first."""
    root = Path(root)
    for d in sorted(p for p in root.glob("*") if (p / "collection.json").exists()):
        yield d, json.loads((d / "collection.json").read_text())


def compare(root=HISTORY):
    """One line per collection and setting: how each policy version did, oldest first."""
    lines = ["| collection | policy | note | cases | setting | runs | failed | retention | msgs/ac/min | S_loss "
             "| coverage | reach 90% | t90 (s) | missed |",
             "|---|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]

    def num(v, f):
        return "" if v is None else format(v, f)

    for d, col in iter_collections(root):
        rows = sorted((col.get("by_setting") or {}).items(), key=lambda kv: tuple(float(x) for x in kv[0].split("x")))
        for key, agg in rows:
            lines.append(f"| {d.name} | {col['provenance']['policy']['short']} | {col.get('note') or ''} | "
                         f"{len(col['settings']['cases'])} | {key} | {agg['runs']} | {agg['failed']} | "
                         f"{agg['retention']:.3f} | {agg['messages_per_aircraft_per_min']:.2f} | {agg['s_loss']:.3f} | "
                         f"{num(agg.get('coverage'), '.3f')} | {num(agg.get('reach90_share'), '.2f')} | "
                         f"{num(agg.get('t90_median_s'), '.0f')} | {num(agg.get('missed_drones'), 'd')} |")
    return "\n".join(lines)


def _runs(collection_dir):
    path = Path(collection_dir) / "runs.jsonl"
    if not path.exists():
        raise SystemExit(f"{collection_dir}: no runs.jsonl (is it a finished collection?)")
    return {r["run"]: r for r in map(json.loads, path.read_text().splitlines()) if r}


def diff(before_dir, after_dir):
    """Run by run, what changed from one collection to another (same cases and settings)."""
    a, b = _runs(before_dir), _runs(after_dir)
    common = sorted(set(a) & set(b))
    if not common:
        raise SystemExit("the two collections share no runs (same cases, settings and comm seeds?)")
    fixed = [r for r in common if a[r]["failed"] and not b[r]["failed"]]
    broken = [r for r in common if not a[r]["failed"] and b[r]["failed"]]
    better = [r for r in common if r not in fixed and b[r]["retention"] > a[r]["retention"] + 1e-6]
    worse = [r for r in common if r not in broken and b[r]["retention"] < a[r]["retention"] - 1e-6]
    mean = lambda d, k: statistics.mean(d[r][k] for r in common)
    lines = [f"{len(common)} common runs ({len(set(a) ^ set(b))} only in one collection)",
             f"S_loss {mean(a, 's_loss'):.3f} -> {mean(b, 's_loss'):.3f}   retention {mean(a, 'retention'):.3f} -> "
             f"{mean(b, 'retention'):.3f}   failed {sum(bool(a[r]['failed']) for r in common)} -> "
             f"{sum(bool(b[r]['failed']) for r in common)}   msgs/ac/min {mean(a, 'messages_per_aircraft_per_min'):.2f} -> "
             f"{mean(b, 'messages_per_aircraft_per_min'):.2f}", ""]
    for title, rows in (("fixed (failed before, not now)", fixed), ("newly broken", broken),
                        ("better retention", better), ("worse retention", worse)):
        lines.append(f"{title}: {len(rows)}")
        for r in rows:
            why = (b[r]["failed"] or a[r]["failed"] or [""])[0][:90]
            lines.append(f"  {r}: {a[r]['retention']:.3f} -> {b[r]['retention']:.3f}  {why}")
    return "\n".join(lines)


def report(summaries):
    lines = []
    n = len(summaries)
    fails = [s for s in summaries if s["failed"]]
    lines.append(f"runs {n}, failed {len(fails)}, mean retention {statistics.mean(s['retention'] for s in summaries):.3f}, "
                 f"messages/aircraft/min {statistics.mean(s['messages_per_aircraft_per_min'] for s in summaries):.2f}")
    lines.append("")
    lines.append("| run | retention | msgs/ac/min | max belief error near (m) | failure |")
    lines.append("|---|---:|---:|---:|---|")
    for s in summaries:
        err = "" if s["max_belief_error_near_m"] is None else f"{s['max_belief_error_near_m']:.0f}"
        lines.append(f"| {s['run']} | {s['retention']:.3f} | {s['messages_per_aircraft_per_min']:.1f} | {err} | "
                     f"{(s['failed'][0] if s['failed'] else '')[:110]} |")
    causes = cause_counts(summaries)
    if causes:
        lines.append("")
        lines.append("Failure causes (separation failures are classified; others by rule):")
        for key, n_c in causes.items():
            lines.append(f"  {n_c:3d}  {key:<20} {CAUSES.get(key, '')}")
        for s in summaries:
            for ctx in s.get("failure_context") or []:
                if ctx.get("evidence"):
                    lines.append(f"       {s['run']}: {ctx['cause']}: {ctx['evidence']}")
    lines.append("")
    lines.append("News distribution (audience = drones airborne at the event):")
    groups = {}
    for s in summaries:
        groups.setdefault(f"{s['setting'][0]:g}x{s['setting'][1]:g}", []).extend(s.get("news_distribution", []))
    for key, dists in sorted(groups.items(), key=lambda kv: tuple(float(x) for x in kv[0].split("x"))):
        st = spread_stats(dists)
        if st["news_events"]:
            t90 = "never" if st["t90_median_s"] is None else f"{st['t90_median_s']:.0f} s"
            lines.append(f"  {key:>9}: {st['news_events']} events, coverage {st['coverage']:.3f}, reach 90% "
                         f"{st['reach90_share']:.2f}, reach 100% {st['reach100_share']:.2f}, median t90 {t90}, "
                         f"missed {st['missed_drones']}")
    affected = [r for s in summaries for r in s["event_learning"] if r.get("affected")]
    lines.append("")
    lines.append("Event learning, affected drones only (needed = planned time it had to know by):")
    if affected:
        by_src = {}
        for r in affected:
            by_src[r["source"] or "never"] = by_src.get(r["source"] or "never", 0) + 1
        late = [r for r in affected if r.get("slack_s") is not None and r["slack_s"] < 0]
        slacks = [r["slack_s"] for r in affected if r.get("slack_s") is not None]
        lines.append(f"  {len(affected)} drone-events; learned by {by_src}; learned after needed: {len(late)}; "
                     f"median slack {statistics.median(slacks):.0f} s" if slacks else f"  {len(affected)} drone-events; {by_src}")
    else:
        lines.append("  none")
    return "\n".join(lines)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["diff"]:
        if len(argv) != 3:
            raise SystemExit("usage: linkloss_knowledge.py diff BEFORE_COLLECTION AFTER_COLLECTION")
        print(diff(argv[1], argv[2]))
        return 0
    if argv[:1] == ["compare"]:
        root = Path(argv[1]) if len(argv) > 1 else HISTORY
        print(compare(root))
        return 0
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--policy", default=str(ROOT / "policy.py"), help="path to a policy.py or a baseline name")
    ap.add_argument("--levels", default="L2,L3")
    ap.add_argument("--seeds", default="0-1")
    ap.add_argument("--cases", nargs="*", help="explicit cases such as L4:13 (overrides --levels/--seeds)")
    ap.add_argument("--event-seed", type=int, help="events seed (default: scenario seed + 1, as check_policy.py)")
    ap.add_argument("--settings", default="1000x200", help="comma-separated RANGExCAPACITY, e.g. 1000x200,250x50")
    ap.add_argument("--comm-seeds", type=int, default=2)
    ap.add_argument("--near", type=float, default=NEAR_M, help="record peer beliefs when drones are this close (m)")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--history", action="store_true", help=f"store the collection in {HISTORY.relative_to(ROOT)}/ (Git-tracked)")
    ap.add_argument("--note", default="", help="what this collection is for, kept in collection.json")
    ap.add_argument("--output", help="new collection directory (default: output/linkloss-knowledge/<id>, or the history store)")
    a = ap.parse_args(argv)

    started = datetime.datetime.now(datetime.timezone.utc)
    cid = started.strftime("%Y%m%dT%H%M%S.%fZ") + "-" + secrets.token_hex(4)
    out = Path(a.output) if a.output else (HISTORY if a.history else ROOT / "output" / "linkloss-knowledge") / cid
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"output directory {out} is not empty")
    out.mkdir(parents=True, exist_ok=True)
    policy = a.policy if a.policy in ("continue", "hold", "return", "broadcast") else str(Path(a.policy).resolve())
    cases = [(c.split(":")[0], int(c.split(":")[1])) for c in a.cases] if a.cases else \
        [(lvl, s) for lvl in a.levels.split(",") for s in parse_seeds(a.seeds)]
    settings = parse_settings(a.settings)
    collection = {"schema": SCHEMA, "collection": out.name, "started_at": started.isoformat(), "status": "running",
                  "note": a.note, "settings": {"cases": [f"{l}:{s}" for l, s in cases], "event_seed": a.event_seed,
                                               "settings": [list(x) for x in settings], "comm_seeds": a.comm_seeds,
                                               "near_m": a.near, "workers": a.workers, "plans": "greedy baseline"},
                  "provenance": provenance(policy)}
    (out / "collection.json").write_text(json.dumps(collection, indent=1) + "\n")
    if Path(policy).exists():
        (out / "policy.py").write_bytes(Path(policy).read_bytes())    # the exact code that produced this data
    tasks = []
    for level, seed in cases:
        case_path = prepare_case(level, seed, a.event_seed, out / "cases" / f"{level}-{seed}.json.gz")
        for r, c in settings:
            for k in range(a.comm_seeds):
                name = f"{level}-{seed}_{r:g}x{c:g}_c{k}"
                tasks.append((str(case_path), policy, (r, c), k, str(out / "runs" / f"{name}.json.gz"), a.near))
    print(f"{len(tasks)} runs -> {out}", flush=True)
    summaries = []

    def done(s):
        summaries.append(s)
        print(f"  {s['run']}: retention {s['retention']:.3f}{'  FAILED ' + s['failed'][0][:90] if s['failed'] else ''}",
              flush=True)

    if a.workers > 1:
        with cf.ProcessPoolExecutor(a.workers) as pool:
            for s in pool.map(run_one, tasks):
                done(s)
    else:
        for task in tasks:
            done(run_one(task))
    _write_jsonl(out / "runs.jsonl", [{k: v for k, v in s.items() if k not in ("event_learning", "failure_context")}
                                      for s in summaries])
    text = report(summaries)
    (out / "report.md").write_text(text + "\n")
    collection.update(status="complete", finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                      by_setting=by_setting(summaries))
    (out / "collection.json").write_text(json.dumps(collection, indent=1) + "\n")
    print("\n" + text)
    print(f"\ncollection: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
