"""Checks the rough matrix against the real validator.

For every (hostile contact, effector of a needed subtype) cell: the matrix predicts feasible or not.
Then we try to build a real isolated mini-plan -- one scout observing, this effector (plus the nearest
partners if the contact needs several) striking after confirmation -- and run the official validator.

usage: python3 analysis/check_matrix.py L2:0-4 L3:0-2 ...
"""
import math
import subprocess
import sys
import tempfile
import time
from collections import Counter

sys.path.insert(0, __import__("os").path.dirname(__file__))
from _kit import KIT  # noqa: E402
from fleetkit.baseline import EFFECTOR_LAYERS, SCOUT_LAYERS, _effector_route, _Planner  # noqa: E402
from fleetkit.energy import route_energy_wh  # noqa: E402
from fleetkit.model import HOSTILE, SCOUT, Scenario, load_json, parse_plan  # noqa: E402
from fleetkit.scorer import evaluate  # noqa: E402

CRUISE_Z = 40.0


def matrix_cell(s, a, c):
    """Same estimate as matrix.py: (feasible, est_wh, banned)."""
    R = s.rules
    banned = any(math.hypot(c.pos[0] - p.pos[0], c.pos[1] - p.pos[1]) < R.collateral_radius_m
                 for p in s.contacts if p.cls in ("friendly", "civilian"))
    d = math.hypot(c.pos[0] - a.home[0], c.pos[1] - a.home[1])
    v = min(a.v_h_mps, math.sqrt(a.power.p0_w / a.power.k_v))
    t_climb = CRUISE_Z / a.v_up_mps
    e = (a.power.p0_w + a.power.k_v * v * v) * (d / v) / 3600 + a.power.e_climb_wh_per_m * CRUISE_Z \
        + a.power.p0_w * t_climb / 3600
    t_fast = t_climb + d / a.v_h_mps
    ok = (not banned) and e <= a.usable_wh and t_fast <= c.window[1]
    return ok, e, banned


def simulate_cell(s, a, c):
    """Tries to build a real valid mini-plan serving c with effector a. Returns (ok, reason, real_wh)."""
    p = _Planner(s)
    effs = [a]
    for sub, n in c.needs.items():
        need = n - (1 if a.subtype == sub else 0)
        free = sorted((x for x in s.aircraft if x.type != SCOUT and x.subtype == sub and x.id != a.id),
                      key=lambda x: math.hypot(x.home[0] - c.pos[0], x.home[1] - c.pos[1]))
        if len(free) < need:
            return False, "not enough partners", None
        effs += free[:need]
    scouts = sorted((x for x in s.aircraft if x.type == SCOUT),
                    key=lambda x: math.hypot(x.home[0] - c.pos[0], x.home[1] - c.pos[1]))
    any_scout = False
    last_violation = "no scout route"
    for scout in scouts:
        for z in SCOUT_LAYERS:
            for angle in (0.0, 60.0, -60.0):
                built = p._add_observation(scout, c, z, angle, 0.0)
                if built is None:
                    continue
                _, sroute = built
                obs_end = max(w["t"] + w.get("dwell_s", 0.0) for w in sroute if w.get("target") == c.id)
                any_scout = True
                lo, hi = c.window
                base = max(obs_end + s.rules.confirm_margin_s + 2.0, lo)
                for delay in (0.0, 8.0, 20.0, 45.0, 90.0):
                    arrive = base + delay
                    if arrive + 5.0 * len(effs) > hi:
                        break
                    for eang, rot in [(ea, ro) for ea in (0.0, 35.0, -35.0) for ro in range(3)]:
                        routes = {scout.id: sroute}
                        ok = True
                        for i, e in enumerate(effs):
                            r, _ = _effector_route(e, c, arrive + 5.0 * i, EFFECTOR_LAYERS[(i + rot) % 3], eang, s)
                            if r is None:
                                ok = False
                                last_violation = "effector too slow for window after confirm"
                                break
                            routes[e.id] = r
                        if not ok:
                            continue
                        plan = {"seed": s.seed, "routes": [{"aircraft": k, "waypoints": w} for k, w in routes.items()]}
                        res = evaluate(s, plan)
                        if res.valid and c.id in res.served:
                            parsed, _ = parse_plan(plan)
                            return True, "ok", route_energy_wh(parsed[a.id].waypoints, a)
                        if res.violations:
                            last_violation = res.violations[0].rule + " " + res.violations[0].message[:50]
    return False, (last_violation if any_scout else "no scout can observe in window"), None


def main():
    conf = Counter()
    reasons = Counter()
    errs = []
    for spec in sys.argv[1:]:
        lvl, rng = spec.split(":")
        lo, hi = map(int, rng.split("-"))
        for seed in range(lo, hi + 1):
            with tempfile.NamedTemporaryFile(suffix=".json") as f:
                subprocess.run(["python3", "-m", "fleetkit", "generate", lvl, str(seed), "-o", f.name],
                               cwd=KIT, check=True, capture_output=True)
                s = Scenario.from_dict(load_json(f.name))
            t0 = time.time()
            local = Counter()
            for c in s.contacts:
                if c.cls != HOSTILE:
                    continue
                for a in s.aircraft:
                    if a.type == SCOUT or c.needs.get(a.subtype, 0) == 0:
                        continue
                    m_ok, m_wh, banned = matrix_cell(s, a, c)
                    s_ok, why, real_wh = simulate_cell(s, a, c)
                    key = ("M+" if m_ok else "M-") + ("S+" if s_ok else "S-")
                    local[key] += 1
                    if m_ok and not s_ok:
                        reasons[why.split(" ")[0] if why.startswith("R") else why] += 1
                    if not m_ok and s_ok:
                        reasons["UNDER-estimate (matrix said no, sim found a plan)"] += 1
                    if s_ok:
                        errs.append(real_wh / m_wh)
            conf.update(local)
            print(f"{lvl}-{seed}: {dict(local)}  ({time.time() - t0:.1f}s)", flush=True)
    total = sum(conf.values())
    print("\n=== confusion (M = matrix says feasible, S = real validator found a plan) ===")
    for k in ("M+S+", "M+S-", "M-S+", "M-S-"):
        print(f"  {k}: {conf[k]:5d}  ({100 * conf[k] / max(total, 1):.1f}%)")
    print("\n=== why matrix-feasible cells failed for real ===")
    for k, v in reasons.most_common():
        print(f"  {v:5d}  {k}")
    if errs:
        errs.sort()
        print(f"\n=== energy: real / matrix estimate, over {len(errs)} working cells ===")
        print(f"  median {errs[len(errs) // 2]:.2f}  p10 {errs[len(errs) // 10]:.2f}  p90 {errs[9 * len(errs) // 10]:.2f}")


main()
