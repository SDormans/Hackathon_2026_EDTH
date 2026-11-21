# Scouting bottleneck: findings

Scope: why L4 plans leave targets unserved, which code causes it, and what to change first. Sections 1–4 describe the code before the change; section 5 reports the first implemented step.

Source data: the committed L4 diagnosis run in [results/l4-unserved-diagnosis/](results/l4-unserved-diagnosis/report.html) (seeds 0–2, 55 s search, one worker), summarised in [README.md](../README.md#why-contacts-go-unserved).

## 1. Diagnosis: L4 unserved targets

The search scored 1798.76 against greedy's 1354.26 (+32.8%, 3 wins, 0 losses). It still missed 154 targets worth 8,542 points:

| Reason | Contacts | Points | Group |
|---|---|---|---|
| `scout_route` | 46 | 2,665 | Scout |
| `collateral_ban` | 36 | 2,460 | Unavailable (R7) |
| `outranked` | 42 | 2,117 | Beam |
| `scout_rule:R1` | 21 | 775 | Scout |
| `time_cap` | 3 | 209 | Time |
| `scout_rule:R3` | 3 | 163 | Scout |
| `scout_rule:R8` | 3 | 153 | Scout |

- **Scout failures are the largest lever: 73 contacts, ~3,756 points** (`scout_route` + `scout_rule:*`).
- Physics explains little of it. `analysis/scout_reach.py` finds only 8 non-banned targets (607 points: L4-0 3/228, L4-1 1/87, L4-2 4/292) that no scout can observe in time even flying straight from base. The remaining scout failures come from how tours are built.
- `outranked` (42 contacts, 2,117 points) is the second lever: a valid serve existed in some branch, but the beam kept other branches.
- Collateral bans (2,460 points) can never be served; they inflate "available points" totals.
- Time limits matter little (3 contacts). Each run completed 4–5 passes.

`scout_rule:R1` here is the horizon check in `IncrementalValidator.quick_reject`: a rebuilt tour ends after the scenario horizon. R3 is energy/return and R8 is separation.

## 2. Root causes in `search_planner.py`

### 2.1 Append-only slots, four nearest scouts (`Search.slots`)

In the main search (`insert=False`), `slots` returns one slot per scout: the **end** of its tour (`len(p.tasks[a.id])`). Scouts are ranked by distance from their last task (or home) to the contact and truncated with a hardcoded `scouts[:4]`.

Effects:

- A contact whose window falls between two existing tasks can only be appended. Appending after a later task usually misses its window (`scout_route`) or pushes the return leg past the horizon (`scout_rule:R1`).
- Scouts ranked 5th or lower are never tried, even when their tour has room.

### 2.2 Insertion mode only in repair

`slots(..., insert=True)` already ranks every tour position of every scout by added distance and returns the best 8. Only `Search.repair` uses it (`self.children(state, contact, local_stop, 4, True)`). Repair runs on archived plans in the last ~3 s reserve, after the beam has already committed to append-only tours. The main beam in `Search.search` calls `children(parent, contact, local_stop)` with the default `insert=False`.

### 2.3 Fixed tour order (`fleetkit/baseline.py` `_scout_route`)

`_scout_route` visits `tasks` strictly in list order and returns `None` as soon as one arrival plus `obs_dwell_s` exceeds that task's window. It never reorders tasks. One new task in the wrong position invalidates the whole tour, and the planner never tries another order of the existing tasks. The departure time comes only from the first task's window (`Search.observation`), so the tour is not retimed around a tight later window.

### 2.4 Repeated full route rebuilds (`Search.observation` / `Search._children`)

`_children` loops over `itertools.product(slots, SCOUT_LAYERS, (0, 60, -60), (0, 7))`: 2 altitudes × 3 observation angles × 2 departure shifts = **12 rebuilds per slot**. With 4 append slots that is **up to 48 rebuilds per `children` call** (96 with the 8 repair slots). Each rebuild calls `_scout_route` on the whole tour, recomputing detours for every leg, and runs `checker.extend` when the route builds. Most combinations fail for the same reason (the window or the horizon), so the time spent goes to repeated failures rather than new candidates.

### 2.5 Hardcoded `scouts[:4]`

The cut-off is a literal in `slots`, not a parameter, and has no fallback. If all four candidates fail, the contact is recorded as `scout_route` or `scout_rule:*` even if a fifth scout could serve it.

## 3. Ranked improvements

Points at stake are the L4 unserved points in the targeted reasons. They are upper bounds, not expected gains. Ranks 1, 2 and 5 are implemented (section 5); the rest are not benchmarked yet.

| Rank | Change | Where | Targets | Points at stake | Effort / risk |
|---|---|---|---|---|---|
| 1 | Use insertion slots in the main search (`insert=True`, or append plus the best few insertion slots) | `Search.search`, `Search.slots` | `scout_route`, `scout_rule:R1` | ~3,440 | Low code change. More slots per call, so pair it with rank 2 or 4 to keep pass count. Inserting can break earlier confirmations; the existing global confirmation recheck must stay. |
| 2 | Reachability pre-filter: drop (scout, slot) pairs that cannot reach the contact in its window by straight-line time before building routes | `Search.slots` / `_children` | CPU on `scout_route` failures | Time only (8 contacts / 607 pts are unreachable by any scout) | Low. Must be a lower bound, as in `scout_reach.py`, so it never rejects a feasible slot. |
| 3 | Tour reordering: after a failed insert, try a time-window-ordered or 2-opt reordered tour in a local wrapper | New helper around `_scout_route` (keep the official kit unchanged) | `scout_route`, `scout_rule:R1` | Same pool as rank 1 | Medium. More rebuilds per attempt; needs tests for confirmation and separation after reorders. |
| 4 | Route memoisation: cache `_scout_route` results by (scout, task tuple, depart, z) and stop the altitude/angle/shift loop once a slot fails on window or horizon | `Search.observation`, `_children` | CPU across all scout attempts | Time; more passes for `outranked` | Low–medium. Cache keys must include every input; bound memory. |
| 5 | Scout fallback beyond the top 4: when all four fail, try the remaining scouts (or make the limit a parameter) | `Search.slots` | `scout_route`, `scout_rule:*` | Part of ~3,756 | Low. Costs time only on contacts that already failed. |
| 6 | Widen the beam (or keep diverse branches) for `outranked` contacts | `Search.search`, `--beam-width` (default 6) | `outranked` | ~2,117 | Low code change, but a wider beam means fewer passes in 55 s. Do after ranks 2 and 4 free time. |

Not addressable by planning: `collateral_ban` (2,460 points, R7).

## 4. Verification

Run from the repository root with the bundled kit on `PYTHONPATH` (or `FLEETKIT_DIR`):

```sh
# Unit tests: search behaviour, incremental vs full validation, hand-in execution
python3 -m unittest discover -s tests -v

# L4 seeds 0-2 at full budget, one worker (same settings as the committed diagnosis)
python3 analysis/benchmark_search.py --levels L4 --seeds 0-2 --seconds 55 --workers 1

# Lower bound on physically unreachable targets for a benchmark directory
python3 analysis/scout_reach.py analysis/results/l4-unserved-diagnosis
```

Compare the `unserved` and `unserved_points` totals in the new run's `summary.json` with the table in section 1. Single L4 cases vary by a few percent between runs, so judge each change over several seed batches, compare against master under the same load, and report losses as well as wins.

## 5. Results: insertion slots with a timing bound

Implemented in `search_planner.py`:

- `Search.feasible_positions`: lower bound on scout timing per tour position (straight legs at the lowest layer, departure at 0, observation point within `OBS_OFFSET_M` of the contact; earliest observation ends forward, latest observation starts backward from the horizon). A position it rejects cannot pass `_scout_route` and R1; `test_time_window_bound_never_rejects_a_buildable_position` checks this on a generated L4 scenario.
- `Search.insertion_costs`: every bound-feasible position of **every** scout (no `scouts[:4]` cut), ranked by added distance. The main beam now tries the 6 cheapest (`children(..., slots=6)`), repair the 8 cheapest. Slots the bound rejects are never built; a contact with none is recorded as `scout_route` without any rebuild.
- `Search.regret` (regret-2 over scouts) orders repair within each class: contacts with one feasible scout first, contacts with none last.

Benchmark: L4 seeds 0–9, 55 s, one worker per batch; four batches (master and branch, seeds 0–4 and 5–9) ran concurrently, so both sides shared the same load.

| Case | Greedy | Master | Branch | Branch − master |
|---|---|---|---|---|
| L4:0 | 590.34 | 861.03 | 1119.93 | +258.90 |
| L4:1 | 432.46 | 618.51 | 956.93 | +338.42 |
| L4:2 | 331.46 | 518.50 | 716.11 | +197.61 |
| L4:3 | 532.90 | 796.39 | 1039.06 | +242.67 |
| L4:4 | 453.48 | 703.98 | 864.50 | +160.52 |
| L4:5 | 702.45 | 936.64 | 1149.01 | +212.37 |
| L4:6 | 314.70 | 605.64 | 785.42 | +179.78 |
| L4:7 | 389.54 | 659.83 | 913.51 | +253.68 |
| L4:8 | 533.71 | 898.51 | 1092.03 | +193.52 |
| L4:9 | 440.94 | 726.59 | 825.85 | +99.26 |
| **Total** | 4721.96 | 7325.62 | 9462.34 | **+2136.72 (+29.2%)** |

All 20 plans were valid; branch beat master on 10 of 10 cases.

Unserved points over the 10 cases:

| Reason | Master | Branch |
|---|---|---|
| Scout (`scout_route` + `scout_rule:*`) | 11,291 | 7,605 |
| `outranked` | 6,442 | 4,956 |
| `time_cap` | 57 | 533 |
| `collateral_ban` | 7,827 | 7,827 |

Limits: one run per case, so single-case differences include run-to-run noise of a few percent. Master stopped searching early on 5 cases (31–47 s); the branch used the full 55 s on every case. Regret ordering was not benchmarked separately from the insertion slots. Other levels (L1–L3) were not benchmarked for this change. `time_cap` grew because each expansion now builds more viable routes in the same time allowance.
