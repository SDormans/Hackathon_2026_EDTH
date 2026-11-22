# Phase 0 baseline, tuning seeds, 2026-11-22

Summary of `test_logger.py` run `20261122T125237.411320Z-534c61ad` on master `1be71e7` (raw run folder not committed):

```sh
python3 test_logger.py --workers 1 --seconds 45 --levels L1,L2,L3,L4 --seeds 0-2,15-20 --note baseline
```

| Level | Greedy | Ours | W/T/L | Valid | Slowest planner process s |
|---|---:|---:|---:|---:|---:|
| L1 | 896.05 | 922.42 | 8/0/1 | 9/9 | 45.09 |
| L2 | 2138.77 | 2222.71 | 8/0/1 | 8/9 | 45.22 |
| L3 | 3492.54 | 4730.79 | 9/0/0 | 9/9 | 45.33 |
| L4 | 5041.70 | 12484.94 | 9/0/0 | 9/9 | 45.94 |
| Total | 11569.06 | 20360.86 | 34/0/2 | 35/36 | 45.94 |

The one failure, L2:17, is a runner timeout recorded at 1175 s wall: the VM was suspended during that case (other cases took ≤ 45.9 s). The Phase 1 A/B rerun covers L2:17 again. Losses to greedy: L1:20 (63.5 vs 113.8) and the L2:17 failure.

`python3 -m unittest discover -s tests -v`: 106 tests OK. `python3 -m fleetkit validation-set` reproduces the eight committed `validation/` scenarios byte for byte.
