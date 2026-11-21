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
    context = failure_context(rec, result.failed)
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
        "event_learning": table, "failure_context": context, "seconds": round(time.monotonic() - started, 2),
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
    return out


def iter_collections(root=HISTORY):
    """Yields (directory, collection.json) for every stored collection, oldest first."""
    root = Path(root)
    for d in sorted(p for p in root.glob("*") if (p / "collection.json").exists()):
        yield d, json.loads((d / "collection.json").read_text())


def compare(root=HISTORY):
    """One line per collection and setting: how each policy version did, oldest first."""
    lines = ["| collection | policy | note | cases | setting | runs | failed | retention | msgs/ac/min | S_loss |",
             "|---|---|---|---:|---|---:|---:|---:|---:|---:|"]
    for d, col in iter_collections(root):
        for key, agg in sorted((col.get("by_setting") or {}).items()):
            lines.append(f"| {d.name} | {col['provenance']['policy']['short']} | {col.get('note') or ''} | "
                         f"{len(col['settings']['cases'])} | {key} | {agg['runs']} | {agg['failed']} | "
                         f"{agg['retention']:.3f} | {agg['messages_per_aircraft_per_min']:.2f} | {agg['s_loss']:.3f} |")
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
