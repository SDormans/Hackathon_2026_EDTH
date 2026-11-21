# Scouting Bottleneck — Consolidated Findings & Improvement Plan

**Repository:** `SDormans/Hackathon_2026_EDTH`  
**Branch:** `scouting`  
**Date:** 2026-11-21  
**Source data:** `analysis/results/l4-unserved-diagnosis/` (L4, seeds 0–2, 55-second search, one worker)

## Purpose

This document consolidates the two scouting-bottleneck analyses. It retains the diagnosis, code-level findings, measurements, and verification approach from Document 1, while incorporating the stronger implementation refinements from Document 2.

The objective is to recover targets currently missed by scout-tour construction while keeping the planner within its 55-second search budget. This is a documentation and implementation plan; no planner code is changed here.

---

## 1. Diagnosis: L4 unserved targets

The search scored **1,798.76 points**, compared with greedy's **1,354.26** (+32.8%; 3 wins, 0 losses). It nevertheless missed **154 targets worth 8,542 points**.

| Reason | Contacts | Points | Category |
|---|---:|---:|---|
| `scout_route` | 46 | 2,665 | Scout construction |
| `collateral_ban` | 36 | 2,460 | Unavailable (R7) |
| `outranked` | 42 | 2,117 | Beam pruning |
| `scout_rule:R1` | 21 | 775 | Scout construction |
| `time_cap` | 3 | 209 | Time limit |
| `scout_rule:R3` | 3 | 163 | Scout construction |
| `scout_rule:R8` | 3 | 153 | Scout construction |

Key observations:

- Scout-related failures account for **73 contacts and approximately 3,756 points** (`scout_route` plus `scout_rule:*`). This is the largest actionable group.
- The direct-flight reachability analysis in `analysis/scout_reach.py` identifies only **8 non-banned targets worth 607 points** that no scout can observe in time even when flying straight from base:
  - L4-0: 3 targets / 228 points
  - L4-1: 1 target / 87 points
  - L4-2: 4 targets / 292 points
- Therefore, most scout failures are caused by tour construction and candidate generation rather than physical reachability.
- `outranked` accounts for 42 contacts / 2,117 points. A valid serve existed in at least one search branch, but that branch did not survive beam pruning.
- `collateral_ban` accounts for 36 contacts / 2,460 points. These targets cannot be served under the applicable ban rule and should not be treated as recoverable planning opportunities.
- The time cap accounts for only 3 contacts / 209 points. Each run completed 4–5 passes.
- `scout_rule:R1` is the horizon check in `IncrementalValidator.quick_reject`; R3 concerns energy/return constraints; R8 concerns separation.

**Conclusion:** The largest opportunity is to improve how scout tours are constructed. Search efficiency improvements may then create additional budget for recovering `outranked` contacts.

---

## 2. Root causes

### 2.1 Append-only candidate generation

In the main search, `Search.slots()` in `search_planner.py` uses `insert=False`. It creates one candidate slot per scout: the end of that scout's current tour.

The scouts are ranked by distance from their last task (or home) to the new contact, and the candidate list is truncated using the hardcoded `scouts[:4]`.

Consequences:

- A contact whose observation window falls between two existing tasks can only be appended to the end of a tour.
- Appending after a later task may miss the contact's time window (`scout_route`) or push the return leg beyond the scenario horizon (`scout_rule:R1`).
- Scouts ranked fifth or lower are never considered, even if their tours could serve the contact.

### 2.2 Insertion is available only during repair

The existing `slots(..., insert=True)` path considers insertion positions throughout every scout's tour. It ranks them by added distance and returns the best eight.

However, this path is used only by `Search.repair`, which operates on archived plans during approximately the final three seconds of the search.

The main beam search calls `children(parent, contact, local_stop)` with the default `insert=False`. As a result, insertion is unavailable during the part of the search where most target decisions are made.

### 2.3 Fixed tour order

`_scout_route` in `fleetkit/baseline.py` visits tasks strictly in list order. It returns `None` when an arrival plus `obs_dwell_s` exceeds a task's observation window.

It does not reorder existing tasks or retime a tour around a tight later window. Consequently, adding a task in an unsuitable position can invalidate an otherwise feasible tour.

The departure time is derived from the first task's window in `Search.observation`, which further limits the ability to adapt a tour to later time windows.

### 2.4 Repeated full route rebuilds

`Search._children` explores combinations of:

- 2 altitude layers
- 3 observation angles
- 2 departure shifts (`0`, `60`, `-60` is three values; see note below)
- 2 additional parameter values (`0`, `7`)

The current loop is:

```python
itertools.product(slots, SCOUT_LAYERS, (0, 60, -60), (0, 7))
```

This is **2 × 3 × 2 = 12 combinations per slot** only if the loop has three dimensions. The actual expression has four dimensions: **2 × 3 × 2 × 2 = 24 combinations per slot**.

Therefore, with four append slots, the loop can attempt up to **96 combinations per `children()` call**; with eight insertion slots, up to **192 combinations**. Each candidate can rebuild the full tour, including detour calculations, before validation.

> **Measurement note:** Confirm the exact number of `Search.observation` / `_scout_route` calls with instrumentation before using these theoretical combination counts as runtime measurements. Some combinations may be rejected or short-circuited before a route build.

Many combinations can fail for the same time-window or horizon reason. This spends search time rebuilding routes that are unlikely to produce a new feasible candidate.

### 2.5 Hardcoded scout fan-out

The `scouts[:4]` limit is hardcoded in `Search.slots()`. If the four nearest scouts cannot serve a contact, there is no fallback to more distant scouts, even when one of them could serve it.

---

## 3. Ranked improvements

The implementation strategy below combines Document 1's detailed diagnosis and prioritization with Document 2's append-first, insertion-fallback approach and more specific reachability filtering.

Points at stake are the L4 unserved points in the targeted categories. They are **upper bounds, not expected gains**. The improvements have not yet been benchmarked.

| Rank | Improvement | Where | Main targets | Points / benefit at stake | Effort / risk |
|---:|---|---|---|---|---|
| 1 | Append first, then try insertion candidates when append produces no valid child | `Search.search`, `Search.slots`, `Search._children` | `scout_route`, `scout_rule:R1` | Part of approximately 3,440 points | Low–medium. Limits extra work compared with insertion for every candidate. |
| 2 | Add a safe, slot-specific reachability pre-filter | `Search.slots` / `Search._children` | Scout failures and runtime | Saves route builds; 8 targets / 607 points are physically unreachable | Low–medium. Must use a conservative lower bound and never reject a feasible slot. |
| 3 | Memoize route builds and avoid redundant candidate combinations | `Search.observation`, `Search._children` | Runtime across scout attempts | More search work within the same 55-second budget | Low–medium. Cache keys must include all route-affecting inputs; memory must be bounded. |
| 4 | Try limited tour reordering after a failed insertion | Helper around `_scout_route` | `scout_route`, `scout_rule:R1/R3/R8` | Part of approximately 3,756 points | Medium. More route builds; confirmation and separation must be revalidated. |
| 5 | Add a fallback beyond the four nearest scouts | `Search.slots` | `scout_route`, `scout_rule:*` | Part of approximately 3,756 points | Low. Apply selectively after nearer candidates fail to control runtime. |
| 6 | Preserve more diverse beam branches or widen the beam after runtime improvements | `Search.search`, `--beam-width` | `outranked` | 2,117 points | Low code change, but a wider beam can reduce the number of passes in 55 seconds. |

### 3.1 Append-first, insertion-fallback

Use the existing append path as the inexpensive first attempt, but make insertion available during the main search when appending cannot produce a valid child.

Suggested flow:

1. Generate the normal append candidates.
2. Evaluate those candidates using the existing route construction and validation path.
3. Determine whether any valid append child was produced for the contact.
4. If no valid append child exists, generate insertion-ranked slots using the existing `insert=True` logic.
5. Evaluate a limited number of the best insertion candidates.
6. Keep the existing global confirmation recheck and final full validation.

This reuses existing insertion logic without multiplying the cost of every ordinary search expansion.

**Important trade-off:** Retrying insertion only when all append candidates fail can miss an insertion that would produce a better plan when an append candidate is valid but strategically poor. Start with the fallback approach for a controlled change, then benchmark whether selective insertion should also be explored for high-value contacts or weak append outcomes.

Do not remove or weaken confirmation rechecks. Inserting a task can change the timing of previously planned observations.

### 3.2 Safe slot-specific reachability pre-filter

Before rebuilding a full tour, reject a `(scout, slot)` pair only when a conservative lower-bound calculation proves that the contact cannot be reached in time.

For an insertion slot, estimate the earliest possible arrival from the preceding task (or home/base for the first slot), using optimistic travel assumptions such as maximum permitted speed and the minimum required climb time.

A candidate is potentially feasible only if:

```text
earliest_arrival + obs_dwell_s <= observation_window_end
```

The calculation must also respect the relevant departure/window constraints. It should ignore detours only when doing so makes the estimate optimistic: a straight-line lower bound can prove impossibility, but must not be used to claim that a route is feasible.

Also retain the direct-from-base reachability check from `analysis/scout_reach.py` to identify targets that are physically unreachable by every scout.

**Safety requirement:** The pre-filter must have no false negatives. If feasibility is uncertain, keep the candidate and let the official route builder and validator decide.

### 3.3 Route memoization and redundant-combination reduction

Cache `_scout_route` results using a key containing every input that can affect the route. A possible starting key is:

```text
(scout_id, task_point_tuple, departure_time, altitude)
```

Include additional parameters if route construction depends on them. Do not use a coarse departure-time bucket unless tests prove that the bucket cannot merge materially different routes.

Also investigate short-circuiting combinations when a failure is independent of altitude, observation angle, or departure shift. Only skip combinations when the reason for failure is demonstrably invariant under the skipped parameter.

Bound the cache or scope it to a search pass so that memory use remains predictable.

### 3.4 Limited tour reordering

After an insertion attempt fails, try a small number of alternative task orders rather than exhaustively permuting the whole tour.

Possible first experiments:

- Move the newly inserted task across nearby positions.
- Try a time-window-oriented ordering for short tours.
- Apply a limited 2-opt-style change when it can plausibly improve timing or distance.

Keep this logic in a local helper around `_scout_route`; do not modify the official kit's route implementation.

Every reordered tour must go through the normal route checks, incremental validation, confirmation recheck, and final full validation. In particular, verify energy/return constraints (R3), separation (R8), and confirmation timing.

### 3.5 Fallback beyond the top four scouts

Keep the current nearest-four behaviour as the first attempt. If those candidates produce no valid child, try additional scouts, for example up to eight.

Prefer a selective fallback rather than increasing the scout fan-out for every contact. Consider contact value and remaining search budget when deciding whether to spend additional route-build time.

### 3.6 Beam diversity and `outranked` recovery

After scout construction and runtime improvements are benchmarked, revisit beam pruning.

Options include:

- Increasing `--beam-width` if the runtime savings support it.
- Preserving branches with different scout assignments or tour structures.
- Retrying selected `outranked` contacts during repair when scout capacity becomes available.

The default beam width is 6. A wider beam is not automatically better under a fixed 55-second budget: compare both score and completed search passes.

---

## 4. Separate the two kinds of improvement

Evaluate improvements in two groups so that their effects are not confused.

### Recovering missed targets

- Insertion fallback
- Tour reordering
- Additional scout fallback

These changes aim to make previously infeasible-looking contacts serviceable.

### Improving search efficiency

- Slot-specific reachability filtering
- Route memoization
- Safe short-circuiting of redundant candidate combinations

These changes aim to reduce wasted computation. They may not directly recover a target, but can allow more search passes or a wider/more diverse beam within the same time budget.

A runtime improvement should not be described as a score improvement until benchmarks show that it increases final scores or reduces unserved points.

---

## 5. Expected outcome and limitations

Approximately **3,756 points** are currently missed due to scout-related reasons, but this is an upper bound on the opportunity, not a forecast.

The direct-flight analysis identifies only 8 non-banned targets / 607 points as physically unreachable under its assumptions. This supports prioritizing tour construction over changes to scout physics.

The `collateral_ban` group (2,460 points) is not addressable through planning changes while the ban rule remains in force.

The `outranked` group (2,117 points) is a separate search-quality problem. It may become easier to address if scout improvements reduce route-building cost, but this must be demonstrated by benchmark results.

The key objective is:

> **Increase successfully served contacts and final score without reducing the amount of useful search completed within the 55-second budget.**

---

## 6. Verification

Run from the repository root with the bundled kit on `PYTHONPATH` (or configure `FLEETKIT_DIR`).

### Unit tests

```sh
python3 -m unittest discover -s tests -v
```

### L4 benchmark

```sh
python3 analysis/benchmark_search.py --levels L4 --seeds 0-2 --seconds 55 --workers 1
```

### Physical reachability diagnosis

```sh
python3 analysis/scout_reach.py analysis/results/l4-unserved-diagnosis
```

### Compare results

Compare the new run's `summary.json` with `analysis/results/l4-unserved-diagnosis/`, focusing on:

- Total score and score relative to greedy.
- `unserved` and `unserved_points`.
- `scout_route` and `scout_rule:*` counts and points.
- `outranked` counts and points.
- Number of completed search passes.
- Runtime and route-build counts, if instrumentation is added.
- Wins and losses per seed, not only the aggregate.

Single L4 cases can vary by a few percent between runs. Evaluate changes over multiple seed batches and compare against the unchanged `master` baseline under the same machine load and settings.

### Validation invariants

- Keep `IncrementalValidator` and final full validation as the authoritative validation path.
- Preserve the global confirmation recheck, especially after insertions and reordering.
- Ensure the pre-filter is conservative and cannot reject a feasible candidate.
- Verify energy/return and separation constraints after route changes.
- Report both score changes and search-throughput changes.

---

## 7. Recommended implementation sequence

1. Add instrumentation for route-build counts, candidate counts, and failure reasons.
2. Implement append-first, insertion-fallback behind a controlled change.
3. Add the conservative slot-specific reachability pre-filter.
4. Add route memoization and only proven-safe short-circuits.
5. Benchmark and inspect whether the additional runtime headroom is real.
6. Add limited tour reordering and scout fallback, measuring each independently.
7. Revisit beam width/diversity to address `outranked` contacts.
8. Run the full unit-test and multi-seed benchmark suite after each meaningful change.

**Do not combine all changes into one unmeasured patch.** Implement and benchmark them incrementally so that regressions, runtime costs, and actual score gains can be attributed to the responsible change.
