"""Replays one link-loss run and prints a timeline for two aircraft around a separation failure.

    python3 analysis/linkloss_diag.py L3-301 4 1 L10 L13 [--policy policy.py] [--from 500 --to 540]
"""
import argparse
import importlib.util
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _kit  # noqa: E402,F401

from fleetkit.linkloss.sim import simulate  # noqa: E402
from fleetkit.model import Scenario, load_json  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def pos_at(pieces, t):
    for t0, t1, p0, p1 in pieces:
        if t0 - 1e-9 <= t <= t1 + 1e-9:
            f = 0.0 if t1 - t0 < 1e-12 else (t - t0) / (t1 - t0)
            return tuple(p0[k] + f * (p1[k] - p0[k]) for k in range(3))
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("case")
    ap.add_argument("ev", type=int)
    ap.add_argument("comm", type=int)
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--plans", default="output/ll/plans")
    ap.add_argument("--policy", default=str(ROOT / "policy.py"))
    ap.add_argument("--setting", default="1000x200")
    ap.add_argument("--from", dest="t_from", type=float)
    ap.add_argument("--to", dest="t_to", type=float)
    a = ap.parse_args()
    spec = importlib.util.spec_from_file_location("diag_policy", a.policy)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(Path(a.policy).resolve().parent))
    spec.loader.exec_module(mod)
    log = {}

    class Rec(mod.Policy):
        def step(self, obs):
            act, msg = super().step(obs)
            if self.id in (a.a, a.b):
                other = a.b if self.id == a.a else a.a
                log[(obs["t"], self.id)] = dict(tau=obs["tau"], rate=self.rate, ab=self.aborted, msg=msg,
                                                 believe=self.peer_state.get(other),
                                                 bpos=self._peer_pos(other, obs["t"]))
            return act, msg

    sc = Scenario.from_dict(load_json(ROOT / "validation" / f"{a.case}.json"))
    plan = load_json(Path(a.plans) / f"{a.case}.json")
    ev = load_json(Path(a.plans) / "events" / f"{a.case}-e{a.ev}.json")
    setting = tuple(float(v) for v in a.setting.split("x"))
    r = simulate(sc, plan, ev, Rec, setting=setting, run_seed=a.comm)
    print("t_loss", ev["t_loss"], "events", ev["events"])
    print("failed", r.failed)
    ca, cb = r.crafts[a.a], r.crafts[a.b]
    for c in (ca, cb):
        last = c.route.waypoints[-1]
        print(c.id, c.ac.type, "end", c.end_kind, c.ended, "last wp", last.action, last.target, last.t)
    ts = sorted({t for t, _ in log})
    lo = a.t_from if a.t_from is not None else ts[0]
    hi = a.t_to if a.t_to is not None else ts[-1]
    for t in ts:
        if not lo <= t <= hi:
            continue
        pa, pb = pos_at(ca.pieces, t), pos_at(cb.pieces, t)
        d = math.dist(pa, pb) if pa and pb else None
        row = [f"t={t:5.0f}", f"d={d:6.1f}" if d is not None else "d=   -  "]
        for aid in (a.a, a.b):
            e = log.get((t, aid))
            if e:
                bp = e["bpos"]
                other = pb if aid == a.a else pa
                err = math.dist(bp, other) if bp and other else None
                row.append(f"{aid}: tau={e['tau']:6.1f} r={e['rate']:4.1f} ab={int(e['ab'])} "
                           f"bel={e['believe']} err={'%.0f' % err if err is not None else '-'} msg={e['msg']}")
        print("  ".join(row))


if __name__ == "__main__":
    main()
