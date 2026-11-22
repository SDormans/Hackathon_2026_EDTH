# Phase 5 redo: link-loss policy audit on the split planner

`policy.py` was audited against the official API (`fleetkit/linkloss/{sim,policies,score,events,oracle}.py`).
It has no planner imports and already implements `TRUST_PLAN`, V/O/R/Z delta messages and
store-and-forward news, so it was verified, not rewritten. One bug fix was kept.

## Official evaluation (organiser plans, `analysis/linkloss_eval.py`)

8 validation scenarios x event seeds 1001-1003 = 24 cases; leaderboard 1000 m / 200 B/s x 5 comm seeds;
sizing grid 4 ranges x 5 rates x 5 comm seeds.

```sh
python3 analysis/linkloss_eval.py run --policy policy.py --name redo --workers 8
```

| Policy | LB retention | LB failures | Grid failures | Sizing score | Frontier |
|---|---:|---:|---:|---:|---|
| master (`redo`) | 0.9908 | 0/120 | 0/2400 | 1.000 | 250 m / 50 B/s |
| fix (`redo-pA`) | 0.9908 | 0/120 | 0/2400 | 1.000 | 250 m / 50 B/s |

## Our own planner's plans (`analysis/linkloss_bench.py`)

Plans from `planner.py` (45 s) for the same 8 scenarios, all officially valid
(L1-101 72.05, L2-201 294.67, L2-202 265.86, L3-301 725.13, L3-302 417.32, L3-303 446.89,
L4-401 1616.96, L4-402 1164.53; slowest 45.6 s).

```sh
python3 analysis/linkloss_bench.py --plans <plans> --event-seeds 1001-1003 --settings sizing --policy policy.py --workers 8
python3 analysis/linkloss_bench.py --plans <plans> --event-seeds 1001-1010 --settings leaderboard --policy policy.py --workers 8
```

| Policy | LB retention | LB failures | 250 m | 500 m | 1000 m | 2000 m | Sizing score |
|---|---:|---:|---|---|---|---|---:|
| master | 0.984 | 0/400 | 0.929, 2 fail | 0.964, 1 fail | 0.983 | 0.984 | 0.50 |
| fix | 0.984 | 0/400 | 0.929, 2 fail | 0.971 | 0.983 | 0.984 | 0.75 |

(Retention is identical across rates per range; failures are counted per (case, comm seed) run.)

### Fixed: L4-402 ev1001 comm0, 500 m - R8 S05/S13 at 39.0 m, t = 890.8 s

When no option is safe, the policy picks the one keeping most room against the peers' *nominal*
motion (`_step_worst(..., nominal=True)`). The formation branch still ran in that mode and replaced
an on-schedule peer it guessed "blocked" by its current position, so S05 held expecting S13 to stop;
S13 kept flying its schedule. Fix: skip the formation branch when `nominal=True`.
Regression test: `test_nominal_check_keeps_on_schedule_peers_moving`.

### Open: L3-301 ev1003, 250 m (both at every rate)

- **comm0, R6 [L03] pop-up zone, t = 397 s.** L03 is holding off-schedule when the adversarial
  `make_events` zone appears around its position; it is inside from the first step, so no action set
  (no altitude action) avoids it.
- **comm3, R8 L02/L03 at 26.2 m, t = 469.6 s.** Both off-schedule (L02 returning at -1, L03 creeping
  at 0.5) and closing at about 46 m/s. L03 broadcast V every second, but L02 heard nothing until 469
  (range edge plus loss at 250 m); L03's belief of L02 dated from t = 400. Distance went 243 m -> 26 m
  in 5 s, i.e. less than one reliable exchange at this range.
- Tried and rejected: `REFRESH_S` 8 -> 4 (no change on either run).

## Gate

- Official cases (the ones the board scores): 0 violations over all scenarios, event seeds, comm seeds and
  every sizing cell including 250 m / 50 B/s. **Met.**
- Our planner's plans: 2 of 600 runs fail at 250 m (all rates), 0 elsewhere. **Not met** at 250 m; the
  remaining two failures are documented above.

Full suite: 107 tests. Under parallel benchmark load `TripTests.test_trip_route_is_valid_within_battery_and_horizon`
failed once (6 s search result depends on CPU time); it passes alone. Pre-existing, not related to this change.
