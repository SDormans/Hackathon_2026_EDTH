"""usage: python3 analysis/matrix.py scenario.json

Rough effector x contact cost matrix: straight line, cruise at 40 m, best-range speed (capped at v_max).

Ignores zones/separation/scout timing -- it's a first-look estimate, not a plan.
"""
import math
import sys

sys.path.insert(0, __import__("os").path.dirname(__file__))
import _kit  # noqa: E402,F401  (makes fleetkit importable)
from fleetkit.model import Scenario, load_json  # noqa: E402

s = Scenario.from_dict(load_json(sys.argv[1]))
R = s.rules
CRUISE_Z = 40.0
lam, c_eff = R.lambda_e, R.c_eff

hostiles = [c for c in s.contacts if c.cls == "hostile"]
protected = [c for c in s.contacts if c.cls in ("friendly", "civilian")]
effectors = [a for a in s.aircraft if a.type == "effector"]


def one_way(a, c):
    d = math.hypot(c.pos[0] - a.home[0], c.pos[1] - a.home[1])
    v = min(a.v_h_mps, math.sqrt(a.power.p0_w / a.power.k_v))
    t_climb = CRUISE_Z / a.v_up_mps
    t = t_climb + d / v
    e = (a.power.p0_w + a.power.k_v * v * v) * (d / v) / 3600 + a.power.e_climb_wh_per_m * CRUISE_Z \
        + a.power.p0_w * t_climb / 3600
    t_fast = t_climb + d / a.v_h_mps   # earliest possible arrival
    return t, t_fast, e


print(f"cell = cost (λ·Wh + c_eff) if reachable before window closes, '--' = can't, 'X' = wrong type\n")
print("contact  pts  needs        window        collat " + " ".join(f"{a.id:>6}" for a in effectors))
for c in hostiles:
    banned = any(math.hypot(c.pos[0] - p.pos[0], c.pos[1] - p.pos[1]) < R.collateral_radius_m for p in protected)
    needs = "+".join(f"{n}{k[0].upper()}" for k, n in c.needs.items())
    row = []
    for a in effectors:
        if c.needs.get(a.subtype, 0) == 0:
            row.append("     X"); continue
        t, t_fast, e = one_way(a, c)
        ok = e <= a.usable_wh and t_fast <= c.window[1]
        row.append(f"{lam * e + c_eff:6.1f}" if ok else "    --")
    print(f"{c.id:7} {c.points:4.0f}  {needs:6} {c.window[0]:6.0f}-{c.window[1]:<6.0f} {'BAN' if banned else '   ':>6} " + " ".join(row))
