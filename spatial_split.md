# Improvement plan: spatial split of the search (Hackathon_2026_EDTH)

## Goal

Raise L4 scores by splitting one large search into several smaller regional searches. Each one runs in its own process, then the results are merged into one plan that the official scorer validates. Smaller problems give more beam passes and more ALNS iterations in the same 55 s.

## Facts the design must respect (L4 seeds 0–2)

- **One shared base.** All 100 aircraft (15 scouts, 50 light, 35 heavy) start in a 100 m × 100 m square at (150–250, 150–250). Every route starts and ends there, so all regions share the takeoff and landing airspace.
- **Targets in one quadrant.** The 70 hostile/unknown targets lie at 3°–87° bearing and 1.6–9 km from the base, so sectors split by bearing from the base are natural.
- **Global rules.** R8 (40 m separation) applies to every aircraft pair. R4 (confirm before engage) and R10 (all-or-nothing) are checked on the whole plan. R7 (collateral) depends on every friendly and civilian contact.
- **Resources.** The aircraft pool is shared, so each aircraft must belong to exactly one region.
- **Grading machine.** Its core count is unknown. Starting workers by `fork` works only on Linux. The process has 60 s in total.

## Design

1. **Partition.** Split targets into *k* bearing sectors (start with k = 2–4), balanced by points rather than count. Targets within a margin (e.g. 300 m or 5°) of a sector boundary go into a *border set* that the global phase handles.
2. **Allocate aircraft.** Give each sector scouts and effectors in proportion to its demand: points, plus the `engage_needs` per subtype. Use the `CandidateMatrix` cost estimates. Keep a reserve of about 10–20% of each subtype for the global phase.
3. **Deconflict the base.** Give each sector a separate departure slot, altitude layer or departure heading, so that routes from different sectors cannot break R8 near the base. This overlaps with PLAN.md task 2 (takeoff scheduling); build it once and reuse it.
4. **Sub-solve.** For each sector, build a sub-`Scenario` containing:
   - its aircraft and targets;
   - **all** friendly/civilian contacts (needed for R7 and protect radii);
   - all zones, rules, the horizon and the seed.

   Run `Search` (beam, repair and ALNS) in a forked worker with a deadline of about 35 s. Send back plain waypoint dicts, not `State` objects.
5. **Merge.** Combine the sector routes and validate them with `IncrementalValidator.extend(root, routes)`, which runs global R4/R10 and R8 on all cross-sector pairs. If that is rejected, find the conflicting pairs, drop the lower-value route of each pair (with its served contacts), and retry.
6. **Global polish.** In the remaining time (about 15 s), run `repair` on the merged plan using the reserve aircraft and the border set, then ALNS on the full plan.
7. **Fallback.** The main process runs the normal unsplit search at the same time, as variant 0 in `parallel_search.py`. The final plan is the best result that passes the official scorer. With one core, or without `fork`, skip the split.

## Steps

| # | Task | Files | Done when |
|---|---|---|---|
| 0 | Diagnostic: targets and points per sector; where R8 rejections happen (near the base vs in the field) | `analysis/region_stats.py` | The report shows most cross-sector conflicts are within about 500 m of the base. If not, rethink step 3. |
| 1 | Sector partition and border set | `spatial_split.py` | Unit test: every target is in exactly one sector or the border set; points are balanced within ±20% |
| 2 | Aircraft allocation with reserve | `spatial_split.py`, `candidate_matrix.py` | Test: every aircraft is assigned once; each sector's `engage_needs` can be met |
| 3 | Base deconfliction (slots, layers or headings per sector) | `spatial_split.py`, shared with PLAN task 2 | Test: routes from two sectors built under the scheme pass R8 together |
| 4 | Sub-scenario builder and worker run | `spatial_split.py`, reuse `parallel_search.py` | Test: a sub-scenario keeps all protected contacts and zones; the worker returns valid plans before its deadline |
| 5 | Merge with conflict resolution | `spatial_split.py`, `search_planner.py` | Test: an injected cross-sector R8 conflict is resolved; the merged plan passes `evaluate` |
| 6 | Global repair and ALNS on the merged plan | `search_planner.py` | The merged plan's score never falls below the best sector merge |
| 7 | CLI flags (`--split k`, `--split-share`), single-core fallback, final official validation | `planner.py` | `--workers 1` is identical to the current planner; hand-in subprocess test passes |
| 8 | Benchmark and README results | `README.md`, `PLAN.md` | See acceptance below |

## Benchmark and acceptance

- Run `python3 analysis/benchmark_search.py --levels L4 --seeds 0-9 --seconds 55 --workers 1`, then L1–L3 seeds 0–4. Use one benchmark worker, because the planner uses all cores itself.
- Compare against the current `scouting` head (from a `git worktree`) under the same load. Repeat each batch at least twice, because single L4 cases vary by a few percent.
- Tune k ∈ {2, 3, 4}, the reserve fraction and the sub-solve share on seeds 0–9 only, then confirm on unseen seeds (e.g. 20–29).
- Accept only if the total score rises, every plan is valid, there are no timeouts, and the slowest run finishes under 57 s (PLAN.md acceptance rule).

## Risks

| Risk | Mitigation |
|---|---|
| Cross-sector R8 conflicts at the base erase the gains | Do step 0 first; base deconfliction in step 3; conflict-dropping merge in step 5 |
| Fixed aircraft split leaves one sector short | Reserve pool plus global repair; size allocation from demand |
| Border targets lost | Border set handled in the global phase |
| Little gain on L1–L3 (small problems) | Enable the split only above a target count, e.g. 40 targets |
| Grader has 1 core or no `fork` | Automatic fallback to the unsplit search |
| Worker overrun | Hard worker deadline of about 35 s; main process collects by 50 s and validates by 58 s |
