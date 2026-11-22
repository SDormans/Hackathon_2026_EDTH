"""Writes hand-built degenerate scenarios for the robustness tests.

Each scenario starts from a generated one (fixed level and seed) and is reshaped into an edge
case the generator never produces. Output: tests/fixtures/robust/<name>.json.

    python3 analysis/degenerate_scenarios.py [--out tests/fixtures/robust]
"""
import argparse
import copy
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analysis"))
import _kit  # noqa: E402,F401  (local fleetkit discovery)
from fleetkit.generator import generate  # noqa: E402

HOME = (150.0, 150.0)


def hostile(d):
    return [c for c in d["contacts"] if c["class"] == "hostile"]


def no_contacts(base):
    d = copy.deepcopy(base)
    d["contacts"] = []
    return d


def collateral_banned(base):
    """A civilian 150 m from every hostile (inside the 200 m collateral radius) and every
    unknown 30 m from a civilian (inside its 100 m protected cylinder)."""
    d = copy.deepcopy(base)
    d["contacts"] = [c for c in d["contacts"] if c["class"] in ("hostile", "unknown")]
    extra = []
    for c in list(d["contacts"]):
        offset = 150.0 if c["class"] == "hostile" else 30.0
        x, y = c["pos"][0] + offset, c["pos"][1]
        extra.append({"id": f"V{len(extra) + 1:02d}", "class": "civilian",
                      "pos": [round(x, 1), round(y, 1), 0.0], "window_s": None})
    d["contacts"] += extra
    return d


def impossible_windows(base):
    """Every window closes before any aircraft can arrive (5 s after take-off)."""
    d = copy.deepcopy(base)
    for c in d["contacts"]:
        if c.get("window_s"):
            c["window_s"] = [0.0, 5.0]
    return d


def tiny_fleet(base):
    d = copy.deepcopy(base)
    scout = next(a for a in d["aircraft"] if a["type"] == "scout")
    light = next(a for a in d["aircraft"] if a.get("subtype") == "light")
    d["aircraft"] = [scout, light]
    return d


def huge_fleet(base_fleet, base_contacts, size):
    """`size` aircraft on a pad grid 20 m apart, with the contacts of a smaller level."""
    d = copy.deepcopy(base_contacts)
    pool = base_fleet["aircraft"]
    cols = math.ceil(math.sqrt(size))
    aircraft = []
    counters = {}
    for i in range(size):
        a = copy.deepcopy(pool[i % len(pool)])
        prefix = {"scout": "S", "light": "L", "heavy": "H"}[a.get("subtype") or a["type"]]
        counters[prefix] = counters.get(prefix, 0) + 1
        a["id"] = f"{prefix}{counters[prefix]:03d}"
        a["home"] = [HOME[0] + (i % cols) * 20.0, HOME[1] + (i // cols) * 20.0, 0.0]
        aircraft.append(a)
    d["aircraft"] = aircraft
    return d


def short_horizon(base, horizon):
    d = copy.deepcopy(base)
    d["horizon_s"] = float(horizon)
    return d


def zones_everywhere(base, top):
    """Overlapping zones tile the map; only a disc around the pads stays open."""
    d = copy.deepcopy(base)
    area = 3600.0
    r, step = 300.0, 400.0
    zones = []
    n = int(area // step) + 2
    for i in range(n):
        for j in range(n):
            cx, cy = i * step, j * step
            if math.hypot(cx - HOME[0], cy - HOME[1]) < r + 250.0:
                continue
            zones.append({"id": f"Z{len(zones) + 1:03d}", "center": [cx, cy],
                          "radius_m": r, "top_m": top})
    d["zones"] = zones
    return d


def zone_over_base(base):
    d = copy.deepcopy(base)
    d["zones"] = d["zones"] + [{"id": "ZB", "center": list(HOME), "radius_m": 300.0, "top_m": 200.0}]
    return d


def no_aircraft(base):
    d = copy.deepcopy(base)
    d["aircraft"] = []
    return d


def only(base, kind):
    d = copy.deepcopy(base)
    d["aircraft"] = [a for a in d["aircraft"] if a["type"] == kind]
    return d


def unmet_needs(base):
    """Hostiles need more heavies than the fleet has, or nothing at all."""
    d = copy.deepcopy(base)
    for k, c in enumerate(hostile(d)):
        c["engage_needs"] = {} if k % 2 else {"heavy": 99}
    return d


def missing_windows(base):
    """Hostile and unknown contacts without a time window (the generator never does this)."""
    d = copy.deepcopy(base)
    for c in d["contacts"]:
        if c["class"] in ("hostile", "unknown"):
            c["window_s"] = None
    return d


def empty_batteries(base):
    d = copy.deepcopy(base)
    for a in d["aircraft"]:
        a["battery_pct"] = 0.0
    return d


def build():
    l1, l2, l4 = (generate(level, 50) for level in ("L1", "L2", "L4"))
    return {
        "a-no-contacts": no_contacts(l2),
        "b1-collateral-banned": collateral_banned(l2),
        "b2-impossible-windows": impossible_windows(l2),
        "c-tiny-fleet": tiny_fleet(l2),
        "d1-fleet-100": huge_fleet(l4, l2, 100),
        "d2-fleet-150": huge_fleet(l4, l2, 150),
        "e1-horizon-60s": short_horizon(l2, 60),
        "e2-horizon-0s": short_horizon(l2, 0),
        "f1-zones-everywhere": zones_everywhere(l2, 200.0),
        "f2-zones-everywhere-overflyable": zones_everywhere(l2, 120.0),
        "g-zone-over-base": zone_over_base(l1),
        "h1-no-aircraft": no_aircraft(l1),
        "h2-scouts-only": only(l2, "scout"),
        "h3-effectors-only": only(l2, "effector"),
        "i-unmet-needs": unmet_needs(l2),
        "j-missing-windows": missing_windows(l2),
        "k-empty-batteries": empty_batteries(l2),
        "l-no-contacts-l4": no_contacts(l4),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "tests" / "fixtures" / "robust")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for name, d in build().items():
        d["level"] = f"{d['level']}-{name}"
        (args.out / f"{name}.json").write_text(json.dumps(d, separators=(",", ":")))
        print(name, len(d["aircraft"]), "aircraft", len(d["contacts"]), "contacts",
              len(d["zones"]), "zones", "horizon", d["horizon_s"])


if __name__ == "__main__":
    main()
