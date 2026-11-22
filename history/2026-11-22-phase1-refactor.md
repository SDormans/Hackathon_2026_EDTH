# Phase 1 refactor gate (2026-11-22)

Change: `search_planner.py` split into `config.py`, `deadline.py`, `route_geometry.py`,
`effector_teams.py`, `scout_tours.py`, `incremental_validation.py`, `diagnostics.py`,
`beam.py`, `lns.py`; tunables in `config.CONFIG`; pluggable `DESTROY`, move operators and
`SCOUT_MOVES`; dead code removed (`old_planner_scouting.py`, unused `_Planner`, duplicate `ops` key).

A = master `1be71e7`, B = this branch. `test_logger.py --workers 1 --seconds 45`; A and B ran
at the same time (plus up to four other single-worker runs on 8 cores).

## Tuning (L1–L4 × 0–2, 15–20)

| Level | A | B | B−A | Invalid A/B | Slowest A/B s |
|---|---:|---:|---:|---:|---:|
| L1 | 922.42 | 922.40 | −0.02 | 0/0 | 45.08/45.12 |
| L2 | 2501.02 | 2500.82 | −0.20 | 0/0 | 45.24/45.25 |
| L3 | 4725.54 | 4727.75 | +2.21 | 0/0 | 45.49/45.34 |
| L4 | 12312.10 | 12373.75 | +61.65 | 0/0 | 45.86/45.82 |
| Total | 20461.07 | 20524.72 | +63.65 | 0/0 | 45.86/45.82 |

L2:17 is valid in both runs; the Phase 0 baseline failure on that case (1175 s) was a VM suspension.

## Held-out (L1–L4 × 30–45)

| Level | A | B | B−A | Invalid A/B | Slowest A/B s |
|---|---:|---:|---:|---:|---:|
| L1 | 1566.38 | 1566.38 | +0.00 | 0/0 | 45.13/45.11 |
| L2 | 4077.03 | 4077.00 | −0.03 | 0/0 | 45.36/45.48 |
| L3 | 8294.94 | 8289.13 | −5.81 | 0/0 | 45.41/45.39 |
| L4 | 21331.34 | 21220.33 | −111.01 | 0/0 | 45.99/46.20 |
| Total | 35269.68 | 35152.84 | −116.85 | 0/0 | 45.99/46.20 |

Largest held-out case changes: L4:34 −114.2, L4:40 −49.5, L4:36 +76.2, L4:43 +25.5.

## Is the difference behavior or noise?

- Under a deterministic fake clock, A and B produce byte-identical plans (8 scenarios across
  L1–L4, plus L4:34 at 12 s and 30 s budgets).
- CPU time for the identical L4:34 30 s fake-clock workload, three interleaved repeats:
  A 11.31/11.96/11.75 s (mean 11.67), B 10.77/11.20/13.02 s (mean 11.66). No throughput cost.
- So the ±0.3% totals come from wall-clock-dependent search paths under shared CPU, not from
  the refactor. Gate result: behavior-preserving; 0 invalid; runtime unchanged.
