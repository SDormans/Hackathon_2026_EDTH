"""What one drone knew at one moment of a recorded link-loss run (read-only).

Replays the fact log written by analysis/linkloss_knowledge.py up to time t, and adds the
drone's own state, the messages it sent and heard that second, and where it believed its close
peers were against where they really were.

    python analysis/drone_knowledge.py RUN.json.gz H05 612            # one drone at t = 612 s
    python analysis/drone_knowledge.py RUN.json.gz H05 600-620        # one line per second
    python analysis/drone_knowledge.py RUN.json.gz --drones           # list the drones of a run
    python analysis/drone_knowledge.py COLL_A COLL_B L3-3_1000x200_c1 H05 612
        # the same drone at the same moment in two collections (e.g. policy.py vs policy_v2.py)
"""
import argparse
import gzip
import json
from pathlib import Path


def load(path):
    with gzip.open(path, "rt") as fh:
        return json.load(fh)


def knowledge_at(run, drone, t):
    """Returns {fact: (value, source, from, learned_t)} for `drone` at time `t`."""
    known = {}
    for f in run["facts"]:
        if f["drone"] != drone or f["t"] > t:
            continue
        if f.get("value") in (None, False) and f["fact"] in known and f["fact"].startswith("self:"):
            known.pop(f["fact"])
            continue
        known[f["fact"]] = (f.get("value"), f.get("source"), f.get("from"), f["t"])
    return known


def state_at(run, drone, t):
    for s in run["steps"]:
        if s["drone"] == drone and s["t"] == t:
            return s
    return None


def peers_at(run, drone, t):
    return [n for n in run["near"] if n["drone"] == drone and n["t"] == t]


def describe(run, drone, t):
    lines = []
    s = state_at(run, drone, t)
    k = knowledge_at(run, drone, t)
    head = f"{drone} at t = {t:g} s"
    if s:
        head += (f"  pos {[round(v) for v in s['pos']]}  tau {s['tau']:.1f}  mode {s['mode']}  "
                 f"energy {s.get('energy')} Wh  action {s['action']}")
    else:
        head += "  (not flying: on the ground, engaged, landed or lost)"
    lines.append(head)
    if s and s.get("sent"):
        lines.append(f"  sent   {s['sent']!r}")
    heard = (s or {}).get("heard") or (s or {}).get("inbox")
    if heard:
        lines.append(f"  heard  {heard}")
    groups = {"obs": "observations it knows are done", "rc": "contacts it knows turned friendly",
              "zone": "pop-up zones it knows", "peer": "peers' announced state (route time, rate, sent at)",
              "self": "its own decisions"}
    for prefix, title in groups.items():
        items = sorted((f, v) for f, v in k.items() if f.split(":", 1)[0] == prefix)
        if not items:
            continue
        lines.append(f"  {title}:")
        for fact, (value, source, frm, learned) in items:
            origin = source + (f" from {frm}" if frm else "")
            lines.append(f"    {fact:<14} {json.dumps(value):<26} {origin:<16} since t = {learned:g}")
    near = peers_at(run, drone, t)
    if near:
        lines.append("  close peers (believed vs real distance, m):")
        for n in sorted(near, key=lambda x: x["real_dist"]):
            bel = "unknown / thinks gone" if n["believed_dist"] is None else f"{n['believed_dist']:.0f}"
            err = "" if n["belief_error"] is None else f"  error {n['belief_error']:.0f}"
            lines.append(f"    {n['peer']:<5} real {n['real_dist']:>6.0f}  believed {bel}{err}")
    return "\n".join(lines)


def parse_times(text):
    if "-" in text:
        lo, hi = text.split("-", 1)
        return [float(x) for x in range(int(float(lo)), int(float(hi)) + 1)]
    return [float(text)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("args", nargs="+")
    ap.add_argument("--drones", action="store_true", help="list the drones of a run")
    a = ap.parse_args()
    if a.drones:
        run = load(a.args[0])
        print(" ".join(sorted({s["drone"] for s in run["steps"]})))
        return
    if len(a.args) == 3:
        run_path, drone, times = a.args
        run = load(run_path)
        print(run["summary"]["run"], "| failed:", (run["summary"]["failed"] or ["no"])[0])
        for t in parse_times(times):
            print(describe(run, drone, t))
        return
    if len(a.args) == 5:
        coll_a, coll_b, name, drone, times = a.args
        for coll in (coll_a, coll_b):
            run = load(Path(coll) / "runs" / f"{name}.json.gz")
            policy = json.loads((Path(coll) / "collection.json").read_text()).get("note") or coll
            print(f"===== {policy} | {name} | retention {run['summary']['retention']} | failed: "
                  f"{(run['summary']['failed'] or ['no'])[0]}")
            for t in parse_times(times):
                print(describe(run, drone, t))
        return
    ap.error("give RUN DRONE T, or COLL_A COLL_B RUN_NAME DRONE T")


if __name__ == "__main__":
    main()
