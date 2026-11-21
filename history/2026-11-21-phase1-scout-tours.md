# Scout-tour phase: benchmark summary, 2026-11-21

Summary of the `test_logger.py` runs for the scout-tour phase. The raw history folders are not committed. Every run used one worker on one 8-CPU machine, and candidates ran from detached worktrees. Runs listed together ran at the same time. Scores are official raw scores, and every plan in every run was valid.

```sh
# Tuning: 36 cases
python3 test_logger.py --workers 1 --seconds 45 --levels L1,L2,L3,L4 --seeds 0-2,15-20 --planner <worktree>/planner.py
# Held-out: 64 cases, run once per candidate
python3 test_logger.py --workers 1 --seconds 45 --levels L1,L2,L3,L4 --seeds 30-45 --planner <worktree>/planner.py --note phase1-heldout
```

## Step 0: L4 diagnosis on current master (seeds 0–2, 55 s)

Run `20261121T220039.325772Z-bf9eac16`: our total is 3888.04 and greedy scores 1354.26. All 3 plans were valid, and the slowest planner process took 55.94 s.

| Reason | README diagnosis (older code) | Current master |
|---|---:|---:|
| `scout_route` | 46 contacts (2665 pts) | 0 |
| `scout_rule:*` | 27 (1091) | 3 (228): 2× R3, 1× R4 |
| Scout failures total | 73 (3756) | 3 (228) |
| `scout_window` | — | 20 (1156) |
| `outranked` | 42 (2117) | 14 |
| `time_cap` | 3 (209) | 3 |
| `collateral_ban` | 36 (2460) | 36 (2460) |

`analysis/scout_reach.py analysis/results/l4-unserved-diagnosis` still finds 8 physically unreachable targets (607 pts), matching the README. Those 8 targets are part of the 20 `scout_window` misses. The README's count of 73 scout failures does not reproduce on current master: later scout insertion work, the 8-scout fallback and time-ranked candidates already removed most of them.

## Tuning seeds

| Run | Change | Run id | Total | L1 | L2 | L3 | L4 | vs previous | Slowest s |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| master `f737a2a` | reference | `20261121T220039.325153Z-83402707` | 20366.11 | 922.42 | 2501.91 | 4722.92 | 12218.87 | | 45.87 |
| 1 `4179f5c` | beam expansion also tries the 2 cheapest insertion positions | `20261121T220256.821137Z-947d2b84` | 20523.23 | 922.40 | 2502.04 | 4727.22 | 12371.57 | +157.12 | 46.07 |
| 2 `eacb0f1` | scout fanout 6, fallback 10; rank by added distance plus a 30 s tight-slack penalty | `20261121T220404.468330Z-aae7d68f` | 20575.53 | 922.40 | 2497.16 | 4722.57 | 12433.39 | +52.30 | 45.82 |
| 3 `af8aec3` | two contacts observed from one shared point, one observe waypoint each | `20261121T220540.585671Z-c91604d3` | 20450.07 | 922.40 | 2495.58 | 4721.50 | 12310.59 | −125.46, reverted | 46.23 |
| 4 `e55ee0d` on 3 | relocation and 2-opt polish of scout orders on each new best plan | `20261121T220651.165289Z-f5139695` | 20456.15 | 922.40 | 2495.28 | 4710.37 | 12328.10 | (base reverted) | 45.81 |
| 2, rerun | reference for 4 | `20261121T222641.469505Z-5e13d5b0` | 20524.17 | 922.40 | 2496.84 | 4725.48 | 12379.45 | | 45.82 |
| 4 on 2 | same polish, Step 3 reverted | `20261121T222641.532723Z-3ea7c1c6` | 20475.98 | 922.40 | 2497.33 | 4728.03 | 12328.23 | −48.19, reverted | 45.76 |

Two runs of the Step 2 code differ by 51 points, so the gains from Steps 1 and 2 are close to run-to-run noise. Every version loses L1:20 to greedy (63.46 vs 113.84).

## Held-out seeds 30–45 (winning combination: Steps 1 + 2)

| Run | Run id | Total | L1 | L2 | L3 | L4 | Valid | Slowest s |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| master `f737a2a` | `20261121T222641.518466Z-f3cf98ec` | 35044.05 | 1561.38 | 4054.16 | 8252.65 | 21175.86 | 64/64 | 45.85 |
| Steps 1 + 2 (`d29eac5` planner) | `20261121T224625.682551Z-3fb24009` | 34960.36 | 1561.29 | 4066.72 | 8286.54 | 21045.80 | 64/64 | 45.97 |
| Change | | −83.69 | −0.09 | +12.56 | +33.89 | −130.06 | | |

The gate required a higher held-out total, with no invalid plans and a slowest run of at most 57 s. The combination met the validity and runtime limits but scored 83.69 points lower, so it failed the gate. Steps 1 and 2 were reverted. The planner is unchanged from master; the new exactness tests stay.

## Scout failures on held-out (`test_logger.py contacts`)

| Class | Run | Served | Scout failures | `scout_window` | Other scout reasons |
|---|---|---:|---:|---:|---|
| hostile | master | 713/1168 | 7 (383 pts) | 75 (5022) | R3 3, R1 1, R8 1, R4 1, route 1 |
| hostile | Steps 1 + 2 | 718/1168 | 6 (350 pts) | 77 (5224) | R8 2, R3 1, route 3 |
| unknown | master | 421/480 | 0 | 28 (527) | |
| unknown | Steps 1 + 2 | 424/480 | 1 (18 pts) | 31 (561) | route 1 |

The combination served 5 more hostiles and 3 more unknowns but still scored less in total, mostly on L4. Its hostile `time_cap` count rose from 9 to 20. This is probably because each beam node tries more insertion positions within the same per-node time.
