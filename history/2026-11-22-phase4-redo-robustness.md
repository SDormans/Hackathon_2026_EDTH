# Phase 4 redo: robustness on the split planner (2026-11-22)

Code: master `970bdce` (Phase 1 split; Phase 2 changed nothing). Harness: `analysis/robust_sweep.py` and
`analysis/fuzz_scenarios.py`; each case runs `planner.py` in its own process with a 60 s wall limit and
the official scorer validates the exported plan. A case fails on a crash, a timeout, a missing or invalid
plan, or a caught internal error.

| Set | Cases | Budget | Workers | Failures | Empty plans | Slowest s |
|---|---:|---|---:|---:|---:|---:|
| Generated L1–L4 × seeds 50–70 | 84 | 45 s | 4 | 0 | 0 | 46.33 (L4-58) |
| Degenerate + fuzz fixtures (`tests/fixtures/robust/`) | 21 | 45 s | 3 | 0 | 12 (expected) | 45.41 |
| Fuzz, base seed 50 (50 per level + 3 full-budget per level) | 212 | 15 s / 45 s | 4 | 0 | 0 | 45.93 |
| Total | 317 | | | **0** | | 46.33 |

The degenerate fixtures cover 0 contacts (L1 and L4 fleets), all contacts collateral-banned,
impossible windows, a 1-scout/1-effector fleet, 100- and 150-aircraft fleets, 60 s and 0 s
horizons, map-covering zones (flyable and not), a zone over the base, no aircraft, scouts only,
effectors only, unmet engage needs, missing windows and empty batteries. `analysis/degenerate_scenarios.py`
regenerates all 18 generated fixtures byte for byte.

Fuzz perturbations (one to three per case): windows shifted/stretched 10–60 s, zone radii ±10 %,
zone centres up to 100 m, horizon ±5 %, aircraft energy and speeds ±10 %.

No robustness bug was found, so no code or regression test was added; `tests/test_robustness.py`
already runs every fixture and injects a crash and a hang.

Commands (scenarios/robust/ and output/ are ignored):

```sh
for L in L1 L2 L3 L4; do for s in $(seq 50 70); do
  python3 -m fleetkit generate $L $s -o scenarios/robust/$L-$s.json; done; done
python3 analysis/robust_sweep.py scenarios/robust/*.json --seconds 45 --workers 4
python3 analysis/robust_sweep.py tests/fixtures/robust/*.json --seconds 45 --workers 3
python3 analysis/fuzz_scenarios.py --levels L1,L2,L3,L4 --seed 50 --cases 50 --seconds 15 --full-cases 3 --workers 4
```
