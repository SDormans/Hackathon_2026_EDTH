# Scouting bottleneck: findings

Scope: why L4 plans leave targets unserved, which code causes it, and what to change first. Documentation only; no planner code was changed for this report.

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

Points at stake are the L4 unserved points in the targeted reasons. They are upper bounds, not expected gains; no change below has been benchmarked yet.

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
