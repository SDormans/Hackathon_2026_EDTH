# Phase 3 redo: beam ranking with an optimistic remainder bound

Base: master after PR #25. Benchmarks: `test_logger.py --workers 1 --seconds 45`, two batches run side by
side (same load for both arms).

## Change kept: `CONFIG.beam_bound = 0.15`

`Search.bound_rank(order)` ranks beam states by

```
score + beam_bound * (free effectors / all effectors) * (positive points of contacts at depth..end)
```

so a state that has spent fewer effectors for the same score survives pruning. Free share is cached per
`used` set; the remainder is a suffix sum over the beam's contact order. `beam_bound = 0` restores the old
score-only ranking.

## Tuning seeds (L1-L4 x 0-2, 15-20; 36 cases)

| Variant | L1 | L2 | L3 | L4 | Total | Δ | Invalid | Slowest s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| master | 922.40 | 2500.82 | 4726.61 | 12313.45 | 20463.28 | - | 0 | 46.31 |
| beam_bound 0.05 | 922.40 | 2500.81 | 4715.15 | 12321.41 | 20459.76 | -3.52 | 0 | 46.49 |
| beam_bound 0.15 | 922.42 | 2500.43 | 4727.10 | 12323.12 | 20473.07 | +9.79 | 0 | 46.20 |
| beam_diverse_extra | 922.40 | 2501.49 | 4715.40 | 12261.92 | 20401.22 | -62.06 | 0 | 46.30 |

Only 0.15 went to held-out.

## Held-out seeds (L1-L4 x 30-45; 64 cases), evaluate only

| Variant | L1 | L2 | L3 | L4 | Total | Δ | W/T/L | Invalid | Slowest s |
|---|---:|---:|---:|---:|---:|---:|---|---:|---:|
| master | 1566.38 | 4077.11 | 8264.04 | 21085.61 | 34993.14 | - | - | 0 | 46.48 |
| beam_bound 0.15 | 1566.37 | 4076.58 | 8270.03 | 21107.60 | 35020.58 | +27.44 | 12/39/13 | 0 | 46.42 |

The real-clock gain is below the ~117-point timing noise seen between identical code in Phase 1, so the
same held-out cases were also run under a fake clock (each time call advances 0.1 ms, 45 s budget;
deterministic, so search quality without CPU-timing noise):

| Fake clock | L1 | L2 | L3 | L4 | Total | Δ | W/T/L | Identical plans |
|---|---:|---:|---:|---:|---:|---:|---|---:|
| master | 1566.38 | 4071.95 | 8229.79 | 20376.85 | 34244.96 | - | - | - |
| beam_bound 0.15 | 1566.38 | 4074.94 | 8229.46 | 20460.01 | 34330.79 | +85.83 | 4/59/1 | 56/64 |

Both held-out measures go up; kept.

## Deadline safety

- Clean copy (14 hand-in files, only official `fleetkit` on `PYTHONPATH`, `--scout-speed fast --obs-stretch`),
  L4-31: 45.77 s wall, valid, `policy.py` loads.
- About 2x CPU throttling (planner and a busy loop pinned to the same core), L4-31: 46.34 s wall,
  22.98 s CPU, valid.

## Not changed

`beam_diverse_extra` lost on tuning. LNS resource-conflict destroy, tour rebuilds, exact reinsertion and
size-dependent LNS shares are already on master from the earlier Phase 3 (`history/2026-11-22-phase3.md`).
