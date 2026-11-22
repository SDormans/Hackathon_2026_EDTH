# Phase 2 redo: scout tours (2026-11-22)

Base: master `6675630` (Phase 1 split). Each variant is one commit on its own branch, run with
`test_logger.py --workers 1 --seconds 45`; the reference and all three variants ran at the same time
(4 single-worker processes on 8 cores). None was kept: all three lose on the held-out seeds.

## L4 diagnosis (`analysis/scout_reach.py`)

Tuning L4 seeds: 1–9 non-banned targets per scenario that no scout can physically reach
(L4:15 9 targets/490 pts, L4:17 8/552 pts). Held-out L4: 1–11 (L4:37 11/792 pts). These are the
upper limit on what scout-tour moves alone cannot recover.

## Variants

| Variant | Change |
|---|---|
| v1 cheap insertion | beam expansion (`children(..., insert="cheap")`) tries the cheapest earlier tour position next to append; full insertion/reorder stays the fallback round |
| v2 wider fallback | `scout_fallback` 8 → 12 (candidates beyond the 4 nearest, ranked by window slack + added distance as before) |
| v3 tour polish | last 1.5 s of LNS: first-improvement single-visit relocations and segment reversals (2-opt, tours ≤ 8 visits) on the best plan's scout tours, each rebuilt tour re-checked by `IncrementalValidator.extend` |

Multi-contact observation was skipped. `fleetkit.validator.valid_observation` takes one
`wp.target` per observe waypoint, and the scorer credits only that target, so one visit cannot
count for two contacts. Back-to-back observe waypoints at a shared point were already tried and
lost (`history/2026-11-21-phase1-scout-tours.md`).

Both new moves were tested against the official scorer: every cheap-insertion child (L3:5) and every
polished state (L2:3, L3:5, starting from perturbed tour orders) was officially valid, with the same
score and served set as the incremental validator. `tests/test_exactness.py` already covers
insertion, reorder and 2-opt/or-opt agreement.

## Tuning (L1–L4 × 0–2, 15–20)

| Run | L1 | L2 | L3 | L4 | Total | Δ | Invalid | Slowest s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| reference | 922.40 | 2500.81 | 4729.97 | 12455.17 | 20608.35 | – | 0 | 45.87 |
| v1 | 922.35 | 2501.84 | 4734.84 | 12360.25 | 20519.28 | −89.07 | 0 | 45.83 |
| v2 | 922.42 | 2499.91 | 4731.65 | 12423.33 | 20577.31 | −31.05 | 0 | 45.79 |
| v3 | 922.40 | 2500.72 | 4727.17 | 12495.78 | 20646.07 | +37.71 | 0 | 44.30 |

## Held-out (L1–L4 × 30–45)

| Run | L1 | L2 | L3 | L4 | Total | Δ | Invalid | Slowest s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| reference | 1566.38 | 4076.87 | 8280.99 | 21191.26 | 35115.51 | – | 0 | 46.07 |
| v1 | 1566.10 | 4080.26 | 8286.64 | 21166.79 | 35099.80 | −15.70 | 0 | 46.12 |
| v2 | 1566.38 | 4076.92 | 8276.54 | 21044.12 | 34963.97 | −151.54 | 0 | 46.25 |
| v3 | 1566.38 | 4076.80 | 8289.96 | 21139.29 | 35072.43 | −43.07 | 0 | 44.66 |

## Conclusion

All three reverted (never merged). v3's polish found improvements in only 5 of 36 tuning cases,
1.07 points in total; its tuning gain was noise, and its 1.5 s cost lost on held-out. v2 spends
beam time on far candidates that rarely survive. v1 is within noise (run-to-run spread on held-out
L4 is about ±100 points; see `history/2026-11-22-phase1-refactor.md`). The planner is unchanged.
