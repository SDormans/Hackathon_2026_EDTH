# Fleet search planner

A standard-library planner that turns the baseline's route candidates into a bounded search tree, then spends the last part of the budget repairing the best plan with large neighbourhood search (LNS). It always keeps valid partial fleet plans, returns the best plan found, and can export the best N distinct plans and an interactive tree.

Run from this directory:

```sh
python3 planner.py path/to/scenario.json output/plan.json
```

The default search deadline is **45 seconds from startup**, including input loading. This leaves 15 seconds of the organisers' 60-second limit for full validation, writing the result and a slower grading machine (`--seconds` up to 55 for local experiments). The required plan is written before optional diagnostics. Python imports and individual validator calls cannot be preempted: this is a cooperative deadline, not a guarantee against arbitrary machine stalls.

For a shorter run with five candidate plans and the tree:

```sh
python3 planner.py path/to/scenario.json output/plan.json \
  --seconds 5 --top-n 5 --beam-width 9 --lns-share 0.4 --artifacts output/search
```

Open `output/search/tree.html` in a browser. Drag to pan, scroll to zoom, click nodes to inspect decisions, and click a ranked solution to highlight its path. The directory also contains `tree.json` with parent links, decisions, scores and rejection counts, plus `plan-01.json`, `plan-02.json`, etc. These are ordinary plans accepted by the official validator. There may be fewer than N if the run finds fewer distinct valid plans.

To watch the winning fleet fly, use the official viewer:

```sh
PYTHONPATH=path/to/fleet-kit python3 -m fleetkit view \
  path/to/scenario.json output/plan.json -o output/flight.html
```

## Why it can beat greedy

The baseline processes hostiles in descending point value, chooses the nearest required effectors, and accepts the first valid improvement. It then adds observations of unknown contacts. An early choice can consume aircraft that would earn more points in a later combination, and the baseline does not revisit that choice.

The search reuses the baseline's route builders and explores alternatives at those decision points:

| Decision | Greedy baseline | Search planner |
|---|---|---|
| Contact selection | Commits to the first improving assignment in points order | Keeps serve and skip branches, allowing resources to be saved for later contacts |
| Effector allocation | Chooses the nearest available group of the required subtypes | Tries up to three groups ranked by estimated energy and effector cost |
| Scout and flight settings | Stops at the first valid improvement | Can retain multiple assignments and flight settings across branches |
| Contact order | Hostiles by points, then unknowns by points | Explores scarcity/value, window-close, points and seeded randomised orders |
| Candidate checking | Revalidates the entire plan for each proposal | Reuses unchanged-route checks and checks every separation pair involving a changed route |
| Revisiting choices | Never removes an accepted assignment | LNS removes a few served contacts and reinserts contacts, so resources move to better combinations |
| Scout tours | Appends each visit to the end of a tour | LNS also tries the cheapest earlier positions in each tour |

For example, one test gives a 90-point contact both available light effectors, while two 70-point contacts each need one. Greedy commits both effectors to the 90-point contact. A skip branch keeps them available for the 140-point pair; the test confirms that the resulting valid plan also wins after energy and effector costs.

Incremental validation makes more candidate checks possible within the same deadline. The matrix helps spend those checks on promising groups; the official rules still decide validity. The benchmark below shows higher scores on all four sampled scenarios, including a **43.5% improvement on L4-0** with the full budget. These measurements do not establish that every mechanism contributes equally or that every unseen scenario will improve. The capped greedy warm start can also stop before completing the baseline, so there is no universal guarantee of matching its score.

## How the tree works

Each node holds routes, scout tasks, consumed effectors and a score. At each contact the search can skip it or serve it with different scouts, effector groups and flight settings. Serving a hostile constructs its observation and complete required strike group together. Scouts always have return-and-land legs. Extending a scout task list rebuilds its tour and rechecks earlier confirmations.

- `candidate_matrix.py` ranks effector/contact choices using the distance/energy estimates from the analysis matrix. Estimated time and energy do **not** exclude candidates. Only exact subtype availability and the official collateral restriction exclude groups. At most three shortlisted teams are tried, so the search remains heuristic.
- `search_planner.py` first follows the original greedy choices with faster validation, for up to a quarter of the budget (capped at 10 seconds). If interrupted, it retains the last accepted plan. It then searches beams using scarcity/value, window-close, points and seeded randomised orders.
- Every extension applies the official local rules to changed routes, rechecks confirmation and strike completeness globally, and checks separation between changed routes and all other routes. Unchanged pairs retain their parent's validation result. Energy and separation flight pieces are cached per branch.
- The beam keeps at most `--beam-width` distinct states at a depth. A separate archive keeps up to `--top-n` distinct valid plans across the entire search. The two settings are independent. Distinct means different assignments or timed waypoints, so solutions may serve the same contacts.
- When the beam phase ends, a greedy **repair** pass fills the best plan: unserved unknowns, then hostiles, are inserted at the cheapest scout-tour positions with the free effectors (at most 0.25 s per contact and a quarter of the LNS share). LNS then starts from the best archived plan.
- Route builders reuse memoised zone detours, and candidates whose waypoints fall outside the horizon or a contact's window are rejected before parsing. Duplicate plans are detected from per-route waypoint tuples.
- The last `--lns-share` of the budget runs LNS on the best plan. The default is 0.4, or 0.6 when the scenario has at least 40 hostile+unknown contacts (`LNS_SHARE`, `LNS_SHARE_LARGE`, `LNS_LARGE_CONTACTS`). Each iteration removes 1–4 served contacts, chosen at random or as neighbours of a random contact, rebuilding the affected scout tour from its original departure. In 10% of iterations (`TOUR_SHARE`) the removed contacts come from one scout tour, and half of those (`TOUR_REBUILD`) clear the whole tour and rebuild it by insertion. It then tries to reinsert the removed contacts, their six nearest unserved neighbours and three random unserved contacts. Each scout visit is tried at the three cheapest tour positions (added distance), and the best valid child per contact is kept. When a repair places at most 5 contacts (`EXACT_REINSERT`), up to 120 other insertion orders are tried within 0.5 s and the best result is kept. Worse plans are sometimes accepted, with a temperature of 1% of the best score falling to zero. After 25 iterations without a new best, the search restarts from a random plan in the top-N archive (`--no-restarts` returns to the best plan instead) and stops after 400 iterations without a new best. Accepted plans enter the archive, so they receive the same final official validation.
- Expansion has local time caps, preventing an impossible contact from consuming the whole run. Children must improve the parent's actual score. All archive winners receive a full official validation and score before export; final validation starts no new call 5 s after the search deadline (50 s at the default 45 s budget, at most 56.5 s). The valid empty plan is the last fallback.

The planner is deliberately **single-process**. The grading machine's core count is unknown. Starting `multiprocessing` workers and pickling `Scenario`/`State` objects would use seconds of the 45 s budget. A worker spawn failure on the grader would score 0. The expected gain does not justify that risk, so parallel search is not implemented.

The result is the best N plans **found**, not a proof of the global top N. Contact ordering, a bounded beam, fixed flight settings, shortlists and the improving-child restriction can all discard useful alternatives. The trace records accepted nodes and skip branches; failed route attempts are aggregated by rule, rather than stored individually. Trace growth is capped between passes to keep tiny scenarios from producing unlimited diagnostics.

## Checks and benchmarks

```sh
python3 -m unittest discover -s tests -v
python3 analysis/benchmark_search.py --seconds 5 --cases L1:0 L2:7 L3:1 L4:0
python3 analysis/benchmark_search.py --levels L1,L2,L3,L4 --seeds 0-4 --workers 4
```

Tests compare incremental validation with the full official validator on perturbed routes, check conflicts with existing aircraft and broken earlier confirmations, verify branch independence, and demonstrate a skip branch beating greedy allocation. A subprocess test copies only the hand-in modules and checks the CLI and exported plans.

### Robustness checks

The invariant: `planner.py` exits 0 within 60 s and exports a plan the official validator accepts (possibly empty) for any scenario. Three guards back this up beyond the search's own deadlines:

- `Search.run()` catches an unexpected error in the search phases, records its traceback in the report's `errors`, and still exports the best archived plan that passes official validation (else the empty plan).
- `planner.py` writes the empty plan if the scenario cannot be loaded or the search raises, and a watchdog thread writes the empty plan and exits at 57 s if no plan has been written. The plan file is written atomically (temporary file, then rename).
- Final validation starts no new call after 56.5 s (50 s at the default budget), before the watchdog.

```sh
# Generated sweep (scenarios/robust/ is ignored; regenerate it first)
for L in L1 L2 L3 L4; do for s in $(seq 50 60); do
  python3 -m fleetkit generate $L $s -o scenarios/robust/$L-$s.json; done; done
python3 analysis/robust_sweep.py scenarios/robust/*.json --seconds 45 --workers 4
# Degenerate scenarios (written to tests/fixtures/robust/) and fuzzing
python3 analysis/degenerate_scenarios.py
python3 analysis/robust_sweep.py tests/fixtures/robust/*.json --seconds 45 --workers 3
python3 analysis/fuzz_scenarios.py --levels L1,L2,L3,L4 --seed 50 --cases 50 --seconds 15 --workers 4
```

A case fails on a nonzero exit, a wall time above 60 s, a missing or invalid plan, or a caught internal error. `tests/test_robustness.py` runs every fixture at a 5 s budget and injects a crash and a hang to exercise the fallbacks.

Results (8-core VM, 3–4 parallel workers):

| Set | Cases | Budget | Failures | Empty plans | Slowest wall |
|---|---|---|---|---|---|
| Generated L1–L4, seeds 50–60 | 44 | 45 s | 0 | 0 | 45.9 s (L4) |
| Degenerate + fuzz fixtures | 21 | 45 s | 0 | 12 (expected) | 37.6 s |
| Fuzz, base seed 50 | 200 + 12 | 15 s / 45 s | 0 | 0 | 45.8 s |
| Fuzz, base seed 51 | 100 + 4 | 15 s / 45 s | 0 | 0 | 45.4 s |

The one bug found: hostile or unknown contacts without a time window crashed the warm start (the kit's greedy indexes the window). The warm start now skips them; R9 makes them unservable anyway. The generator never omits windows, so scores do not change.

### Batch benchmarks

The runner defaults to **12 scenarios** (L1–L4, seeds 0–2), a **45-second search budget** (the hand-in default) and one worker. Choose a seed range or explicit cases, and optionally run scenarios concurrently:

```sh
# 20 scenarios with four workers and the normal search budget
python3 analysis/benchmark_search.py --levels L1,L2,L3,L4 --seeds 0-4 --workers 4

# Reproducible sequential comparison: no competing benchmark workers
python3 analysis/benchmark_search.py --levels L1,L2,L3,L4 --seeds 0-4 --workers 1

# Quick smoke batch; short searches can score worse than greedy
python3 analysis/benchmark_search.py --levels L1,L2 --seeds 0-2,7 --seconds 5 --workers 2

# Select particular cases and a new output directory
python3 analysis/benchmark_search.py --cases L3:7 L4:13 --workers 2 \
  --output output/benchmarks/selected-cases
```

Each completed case prints greedy's score, our score, the gain, percentage, runtime and statuses. The final summary includes **total and per-level scores, wins/ties/losses, valid outputs, and failure counts**. The total is the sum of raw scores, matching the competition ranking; aggregate improvement is calculated from the totals.

Open the printed `report.html` path to inspect the same comparison in a browser. It refreshes every five seconds while the batch runs. Each run saves:

- `run.json` and `summary.json`: cases, budgets, worker count and aggregate results.
- `results.jsonl`: one flushed record per completion; `results.json` and `results.csv`: ordered results.
- A directory per case with `scenario.json`, `greedy.plan.json`, `search.plan.json`, and both process logs.

Greedy and our planner run separately on the **same generated input**, each with a **60-second hard process limit**. Both plans are checked by the official scorer. Crashes, timeouts, missing output, malformed JSON and invalid plans score zero and do not abort the remaining cases. Generation/setup errors are recorded and excluded from score comparisons. The runner exits nonzero if either planner or scenario setup failed; losing to greedy is a completed comparison, not an execution error.

Generation and the external scoring calls are outside each planner's measured runtime. Parallel workers share CPU, so their timing and time-bounded search scores can differ from a single-worker run. Use `--workers 1` when assessing performance under the competition's per-scenario budget. A custom candidate can be compared using `--planner path/to/planner.py`. Existing output directories are rejected to prevent overwriting results.

### Phase 3 LNS changes, held-out seeds 30–45, 45 s

Each change was one commit, benchmarked with `python test_logger.py --workers 1 --seconds 45` on the tuning seeds L1–L4 × 0–2, 15–20 (36 cases). Candidates that won tuning then ran on the held-out seeds L1–L4 × 30–45 (64 cases). Losers were reverted. Every batch ran at the same time as a reference run on one 8-CPU machine. Run ids, per-level scores, per-operator LNS counters and the throttling test are in [history/2026-11-22-phase3.md](history/2026-11-22-phase3.md). Every plan in every run was valid.

| Commit | Change | Tuning: reference → change | Held-out: reference → change | Kept |
|---|---|---:|---:|---|
| 35448b2 | Per-operator LNS counters (`lns.ops`) | stats only | | yes |
| 78daf53 / 06aed63 | `blocker` destroy op (15%): frees the effectors and tour slots of a top unserved contact | 20481.66 → 20408.16 (−73.5) | | no |
| 8a3f797 / 17245d3 | `tour` op in the default mix (10%); half of its picks clear a whole scout tour and rebuild it by insertion | 20397.01 → 20467.91 (+70.9) | 35012.57 → 34953.34 (−59.2) | no; re-applied on width 9 (e5f0e63) |
| b5350cb / a961e47 | Exact reinsertion: up to 120 insertion orders when a repair places ≤ 5 contacts, 0.5 s cap | 20467.91 → 20429.51 (−38.4) | | no; re-applied after the strike merge (below) |
| b271d6d | `lns_share` 0.6 with ≥ 40 hostile+unknown contacts, else 0.4 | 20452.19 → 20601.12 (+148.9) | 34893.80 → 35127.38 (+233.6) | yes |

Stack before beam width 9 (commit 17245d3, note `phase3-heldout`), 64 held-out cases beside master: **35127.38 vs 34893.80 (+233.58, +0.7%)**. L1 1561.38 (tie), L2 4054.27 vs 4054.22, L3 8266.33 vs 8262.77, L4 21245.39 vs 21015.43. It won 20 cases, lost 13 and tied 31, and all 64 plans were valid. The slowest planner process took 46.08 s unthrottled. Summed LNS counters were 32296 iterations, 15584 accepted moves, 760 improvements and +1834.6 gain. Gain by operator, in improvements / gain: `trip` 132 / 494.8, `group` 105 / 488.9, `related` 281 / 448.3, `random` 242 / 402.6.

- **`blocker`** had the lowest gain per try of all operators (54 points over 2223 tries). Freeing a blocked contact's resources rarely let it be reinserted at a profit.
- **Tour rebuild** gained more per try than `random` or `related`, but it took steps from them. Alone it lost on held-out seeds (−59.2) and was reverted. It was re-applied on beam width 9 (below), where it won.
- **Exact reinsertion** found a better order in 3791 of 5476 repairs, but each repair cost more. LNS ran half as many iterations (9300 vs 16151), and the total fell.
- **`lns_share`** on tuning seeds, step-2 code: 0.3 −108.0, 0.5 +6.7, 0.6 +131.8 and the size rule +148.9 against 0.4. On held-out seeds, 0.6 everywhere scored 35131.40 and the size rule 35214.37. Only L4 (70 hostile+unknown contacts) gets the larger share; L1–L3 have 4–20.
- **`since_best < 1500`** instead of 400 changed L1–L2 by +1.6 in total (3420.48 → 3422.04), so the early stop stays at 400. Except L1-20, every L1 case stops after 1.5–4.6 s, already at the same score.
- **Throttling:** the slowest held-out L4 case ran in 45.7–46.1 s wall time with valid plans under `cpulimit -l 50` and `-l 33` (1.6–2.6× fewer candidate checks than unthrottled). Final validation finished by 45.4 s, before the 50 s `validate_stop`, so the margin to 60 s is about 14 s and the cutoff was not changed.

Run-to-run noise is large: master scored 34803.60, 35012.57 and 34893.80 on the held-out seeds in separate batches, almost all of the spread on L4. Gains under about 200 points on held-out are not established by one batch.

**On beam width 9.** `master` then merged phase 2 (beam width 9), so the kept rule and the tour rebuild were re-run on held-out seeds 30–45 beside the new `master`, all at the same time (note `phase3-heldout w9`):

| Code | Total | L1 | L2 | L3 | L4 | Valid | Slowest s |
|---|---:|---:|---:|---:|---:|---:|---:|
| master a58eefb (width 9) | 35111.07 | 1566.17 | 4050.29 | 8260.28 | 21234.34 | 64/64 | 45.99 |
| + size rule | 35209.69 (+98.6) | 1566.17 | 4050.29 | 8263.15 | 21330.08 | 64/64 | 45.89 |
| + size rule + tour rebuild | **35415.98 (+304.9)** | 1566.17 | 4050.30 | 8262.83 | 21536.69 | 64/64 | 46.09 |

The tour rebuild with the size rule has now beaten the rule alone in both held-out batches that included both (+152.4, then +206.3), so it is kept. It won 24, lost 19 and tied 21 cases against `master`. Under `cpulimit -l 50` and `-l 33`, the two slowest L4 cases ran in 45.6–45.9 s wall time with valid plans.

**After the strike merge.** `master` then merged the `strike` operator and three hotfixes ([PR #13](https://github.com/SDormans/Hackathon_2026_EDTH/pull/13)–[#15](https://github.com/SDormans/Hackathon_2026_EDTH/pull/15)). `blocker` and exact reinsertion were re-applied unchanged and re-run, each group at the same time:

| Code | Tuning total | Held-out total | L1 | L2 | L3 | L4 | Valid | W/L/T vs master |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| master (strike on) | 20521.74 | 35330.53 | 1566.19 | 4050.35 | 8277.41 | 21436.58 | 64/64 | |
| strike off | 20538.12 (+16.4) | | | | | | | |
| + `blocker` | 20633.89 (+112.2) | 35251.22 (−79.3) | 1566.17 | 4047.94 | 8254.68 | 21382.43 | 64/64 | 17/27/20 |
| + exact reinsertion (kept) | 20574.82 (+53.1) | **35462.70 (+132.2)** | 1566.38 | 4077.10 | 8307.23 | 21511.99 | 64/64 | 29/19/16 |
| + both | | 35303.10 (−27.4) | 1566.36 | 4075.28 | 8298.58 | 21362.88 | 64/64 | 32/17/15 |

Exact reinsertion is kept: it gained on L2, L3 and L4. With `strike`, `trip`, `group` and `tour` taking 70% of the steps, the extra orders now raise improvements and gain (752 / +2008.7 vs 714 / +1959.5) even with 27% fewer LNS iterations; before `strike` it lost on tuning seeds. `blocker` lost on held-out seeds again and stays out. The gain is within the run-to-run noise above (up to about 200 points), so it rests on this one batch. Under `cpulimit -l 50` and `-l 33` (2.3–3.1× fewer candidate checks), the two slowest held-out L4 cases ran in 45.8–46.2 s wall time with valid plans.

### Held-out batch, seeds 70–74, 45 s

`python test_logger.py --workers 1 --seconds 45 --levels L1,L2,L3,L4 --seeds 70-74`: 20 cases not used for tuning (diagnosis used other seeds). Each candidate ran at the same time as a reference run of the previous commit; run totals and per-case scores are in [history/2026-11-21-heldout-70-74.md](history/2026-11-21-heldout-70-74.md). Every plan was valid in every run. The slowest full planner process took 45.9 s.

| Run | Total | L1 | L2 | L3 | L4 | vs reference |
|---|---:|---:|---:|---:|---:|---:|
| 035a098 (previous commit) | 10645.84 | 513.35 | 1482.90 | 2872.08 | 5777.51 | |
| 45 s default, validation cutoff at 50 s (kept) | 10637.52 | 513.35 | 1482.90 | 2868.77 | 5772.50 | −8.32 |
| Same code, five more reference runs | 10615.36–10650.79 | 513.35 | 1482.90 | 2864.01–2876.89 | 5755.10–5781.68 | |
| A: strike team members get their own takeoff slots | 10619.30 | 525.71 | 1483.80 | 2872.17 | 5737.62 | −22.18 |
| B: 6 first scouts instead of 4 | 10613.41 | 513.35 | 1482.90 | 2871.37 | 5745.80 | −28.07 |
| C: 30/75 m effector layer pair before the 40/85/130 m rotations | 10633.25 | 513.50 | 1484.94 | 2866.09 | 5768.71 | −6.24 |
| D: strike team spacing up to 8 s | 10597.15 | 525.71 | 1482.83 | 2862.86 | 5725.76 | −42.34 |
| E: A, only after every other strike setting failed | 10617.03 | 525.71 | 1482.91 | 2865.20 | 5743.21 | −33.76 |
| F: check last-conflicting aircraft first (~7% more checks per second) | 10589.70 | 513.35 | 1482.90 | 2872.79 | 5720.66 | −51.55 |
| G: extra 15 s scout departure shift | 10562.38 | 513.35 | 1482.91 | 2866.99 | 5699.14 | −52.98 |

The safety change was kept as a requirement. A–G were not committed. Repeated runs of the same code differ by up to about 35 points in total, mostly on L4. Every change to strike or scout options lost 20–50 points on L4, even when L1 gained 12 points (A, D, E fix L1-74). Remaining R8 rejections on a diagnostic L4 run were mostly en route: effectors of one team converging on their target at the same layer, and effectors crossing scouts. Takeoffs from neighbouring pads were about 20%.

### Scout-tour phase, tuning seeds 0–2 and 15–20, held-out seeds 30–45, 45 s

These four changes were each committed and benchmarked against the previous commit on 36 tuning cases (`--workers 1 --seconds 45 --levels L1,L2,L3,L4 --seeds 0-2,15-20`). Run ids, per-level scores and the scout diagnosis are in [history/2026-11-21-phase1-scout-tours.md](history/2026-11-21-phase1-scout-tours.md). Every plan was valid, and the slowest planner process took 46.2 s.

| Change | Tuning total | vs previous | Result |
|---|---:|---:|---|
| master `f737a2a` | 20366.11 | | |
| 1. Beam expansion also tries the 2 cheapest insertion positions (no reordered tours) | 20523.23 | +157.12 | won on tuning |
| 2. Scout fanout 6, fallback 10; candidates ranked by added distance plus a 30 s tight-slack penalty | 20575.53 | +52.30 | won on tuning |
| 3. Two contacts observed from one shared point, one observe waypoint each (allowed by `valid_observation` and R1) | 20450.07 | −125.46 | reverted |
| 4. Relocation and 2-opt polish of scout visit orders on each new best plan | 20475.98 | −48.19 vs a 20524.17 rerun of 2 | reverted |

The held-out evaluation of Steps 1 + 2 on 64 cases (seeds 30–45) did not pass the gate:

| Run | Total | L1 | L2 | L3 | L4 | Valid | Slowest s |
|---|---:|---:|---:|---:|---:|---:|---:|
| master | 35044.05 | 1561.38 | 4054.16 | 8252.65 | 21175.86 | 64/64 | 45.85 |
| Steps 1 + 2 | 34960.36 | 1561.29 | 4066.72 | 8286.54 | 21045.80 | 64/64 | 45.97 |

Steps 1 and 2 were therefore reverted, and the planner is unchanged from master. Two runs of the same Step 2 code differed by 51 points, so the tuning gains were within run-to-run noise. `tests/test_exactness.py` checks that `IncrementalValidator` and the official validator agree on insertion, reorder and 2-opt/or-opt scout moves. It also checks that both report injected R1 (horizon), R3 (energy), R4, R6 and R8 violations.

### Phase 2 beam changes, held-out seeds 30–45, 45 s

Each change was tuned on L1–L4 seeds 0–2 and 15–20 (36 cases). Changes that won were then checked once on held-out L1–L4 seeds 30–45 (64 cases), with `--workers 1` and 45 s. Run ids, tuning tables, per-case scores and the batch layout are in [history/2026-11-22-phase2-beam.md](history/2026-11-22-phase2-beam.md). Every plan was valid in every run. The slowest full planner process took 46.3 s.

| Change | Tuning total | Held-out total | L1 | L2 | L3 | L4 | Kept |
|---|---:|---:|---:|---:|---:|---:|---|
| Base (width 6) | 20576.39 / 20407.16 | 34900.15 | 1561.38 | 4053.90 | 8246.78 | 21038.09 | |
| Step 1: `BEAM_DIVERSE = 3` beam slots for states with a new used/served set | 20511.45 | 35086.54 | 1566.17 | 4054.41 | 8261.76 | 21204.21 | yes |
| Step 2: rank by score + optimistic bound, weight 1.0 (0.5) | 20438.70 (20564.53) | 35052.77 (35067.98) | 1566.17 | 4058.95 | 8265.67 | 21161.98 | no |
| Step 3: one non-improving child if its score + bound beats the parent's | 20517.91 | 35151.93 | 1566.17 | 4058.95 | 8267.72 | 21259.10 | no (never fires) |
| Step 4: width 9 on Step 1 (width 6 + `widen`, width 4 + `widen`) | 20394.59 (20545.11, 20474.47) | 35168.81 (35143.40, 35061.38) | 1566.17 | 4050.29 | 8275.44 | 21276.91 | yes, width 9 |
| **Final `phase2-heldout` run** (Step 1 + width 9, run alone) | | **35151.40** | 1566.17 | 4050.29 | 8280.52 | 21254.43 | |

Final branch vs base on held-out: **+251.25** (+0.7 %). The scout-tour phase measured the same base planner code on the same held-out seeds at 35044.05, so the gain over that run is +107.35. Runs of identical code differ by up to 169 points on tuning and up to 144 on held-out, mostly on L4. The gain is therefore only about one to two times the noise.

- **Step 0 accounting.** `outranked` now has a sub-count, `beam_pruned`: valid serving children that the beam width cut. On L4 seeds 0–2, 11 contacts (514 pts) were outranked, and 5 of them (227 pts) had valid children cut by the beam. Over the 64 held-out cases, the base had 131 outranked contacts (88 beam-cut) and the final run had 124 (95).
- **Step 2 lost** at both weights. The bound moves the beam toward states that keep resources free. Those states did not turn into better plans.
- **Step 3 never fires.** A non-improving child uses at least as many resources as its parent, so its optimistic bound is never higher. Of 4,078 non-improving children over 3 probe cases, none passed the gate. The looser gate (child score + bound > parent score) tied on tuning (20517.6) and was not run on held-out.
- **Width 9 won on held-out** in both sweeps, but lost on tuning L4 on the Step 1 code. It was picked on the held-out total, as the plan requires. `widen` (width × 1.5 from pass 4) was second in both held-out sweeps and stays off.

### LNS batch result

Both planners ran at full budget under the same load, four workers each at the same time, alongside greedy. Seeds 3–5 were not used during development. The validation scenarios ran 16 planners at the same time on eight cores; the greedy total comes from PLAN.md. `--lns-share 0.6` scored 4391.13 on the seeds 3–5 batch, within run-to-run noise of the default.

| Batch | Greedy | Beam only (master) | Beam + LNS | Gain over master |
|---|---:|---:|---:|---:|
| L1–L4, seeds 0–2 | 3622.86 | 4556.95 | 5208.17 | +14.3% |
| L4 only, seeds 0–2 | 1354.26 | 1899.92 | 2452.72 | +29.1% |
| L3–L4, seeds 3–5 | 2913.46 | 3881.74 | 4334.80 | +11.7% |
| 8 public validation scenarios | 2664.5 | 3674.65 | 4054.29 | +10.3% |

Beam + LNS matched or beat master on every case and greedy on all 18. Every plan was valid and the slowest run took 55.29 seconds. These are sampled scenarios, not a guarantee for unseen inputs.

### Consolidated branch result

The repair pass and LNS were merged with the upstream opt-in `--alns` phase, then rerun at 55 s with one worker. Each batch below ran concurrently with up to two other benchmark batches on eight cores.

| Batch | Greedy | Beam + repair (fork master) | Beam + repair + LNS (default) | `--alns` |
|---|---:|---:|---:|---:|
| L1–L4, seeds 0–2 | 3622.86 | 4655.96 | 5251.52 (+12.8%) | |
| L4 only, seeds 0–2 (after rebasing on upstream) | 1354.26 | 1998.04 | 2412.22 | 2224.16 |

Every plan was valid and the slowest run took 55.25 seconds. L4-2 scored 665.14 in the first batch and 562.88 after the rebase, so single L4 cases vary by up to about 15% between runs.

A second head-to-head at 45 s with one worker per planner (L1–L4, seeds 15–17, two planners at the same time) gave LNS 5623.67 and `--alns` 5435.37: LNS won all three L4 cases (2883.93 vs 2680.64) and the two tied on L1–L3. The `--alns` option was then removed, so LNS is the only improvement phase.

### Mixing the search strategies

Three variants were compared with the LNS described above (which then always restarted from the best plan):

- `--interleave`: alternate each beam pass with an LNS burst (burst length = pass time × share / (1 − share)), then a final LNS tail of 10% of the budget.
- `--adaptive`: adaptive LNS. Destroy operators `random`, `related`, `worst` (lowest-priority served contacts) and `tour` (contacts of one scout tour) are drawn by weight; weights move 20% towards a reward of 3 (new best), 1 (accepted improvement), 0.3 (accepted worse plan) or 0.
- `--restarts`: when stuck, restart from a random archived plan instead of the best.

All variants ran at full budget with two workers each, four planners at the same time on eight cores. None of these seeds were used during development.

| Batch | Restart from best | Interleave | Adaptive | Archive restarts | Restarts + adaptive | All three |
|---|---:|---:|---:|---:|---:|---:|
| L2–L4, seeds 6–8 | 4568.33 | 4535.74 | 4473.54 | **4619.43** | | |
| L3–L4, seeds 9–11 | 3515.57 | 3594.35 | 3634.68 | **3659.48** | | |
| L3–L4, seeds 12–14 | 4245.88 | | | **4265.76** | 4210.44 | 4219.51 |

Archive restarts scored highest in all three batches (12544.67 vs 12329.78 over the 21 cases, +1.7%) and are now the default. Per case they won 10, tied 2 and lost 9 against restarting from the best plan; the gain comes mostly from a few large L4 wins, so it is small relative to run-to-run noise. Combining restarts with the other variants scored lower than restarts alone. Every run in these batches was valid and finished inside the time limit.

### Parallel batch result

A full-budget batch over **L1–L4, seeds 0–2**, with **four concurrent workers**, completed with **12 wins, no ties or losses, and 12 valid outputs**. Neither planner crashed or timed out. These cases overlap earlier tests and are not a held-out evaluation.

| Level | Greedy total | Our total | Gain |
|---|---:|---:|---:|
| L1 | 291.65 | 332.34 | +14.0% |
| L2 | 731.36 | 840.72 | +15.0% |
| L3 | 1245.60 | 1467.69 | +17.8% |
| L4 | 1354.26 | 1837.14 | +35.7% |
| TOTAL | 3622.86 | 4477.89 | +23.6% |

The slowest candidate run finished in 55.23 seconds. Parallel CPU contention can change these scores; use one worker for a sequential comparison. The local report is `output/benchmarks/batch-12-parallel/report.html`. Reproduce the batch with:

```sh
python3 analysis/benchmark_search.py --levels L1,L2,L3,L4 --seeds 0-2 --workers 4 --seconds 55
```

### Repair and cheaper checks

Same inputs, master vs this change, both with two concurrent benchmark workers per batch running at the same time:

| Batch | Master | With repair | Change |
|---|---:|---:|---:|
| 8 public validation scenarios, 55 s | 3768.65 | 3790.20 | +0.6% |
| L1–L4 seeds 3–4, 55 s | 3164.20 | 3190.15 | +0.8% |
| L1–L4 seeds 0–2, 20 s (four workers, run separately) | 4417.21 | 4514.93 | +2.2% |

All plans were valid. Gains come from L4 (e.g. L4-401 1077.92 → 1118.71, L4-0 at 20 s 834.78 → 861.03); L1–L3 mostly tie but finish sooner. L4-402 regressed (613.23 → 593.99) and L4-2 at 20 s slightly (447.65 → 446.43): faster checks change which branches fit in the local time caps, so individual cases vary.

### Scout trip label search

On L4 the scouts are the bottleneck: they fly 9–16 km for 2–6 observations, and the choice of contacts each one chains matters more than how its route is shaped. `trip_labels` finds the best-value trips for one scout by resource-constrained label setting. A label is a chain of visits, with its finish time, Wh used, net value and visited set. Legs are straight lines using the `tour_estimate` speed, departure and energy model. Hostiles must also finish early enough for confirmation and their cheapest free team. Dominated labels are dropped, and each contact keeps at most 6. A run takes 1–8 ms per scout on L4. The search is used in two ways:

- **LNS operator `trip`** (`TRIP_SEARCH`, 20% of LNS steps): removes one scout's tour and the strikes it confirms, then rebuilds it visit by visit from a better trip through `_serve`, retrying layers, angles and takeoff shifts. It reports `trip_tried`/`trip_accepted`/`trip_improvements`/`trip_gain` in the `lns` block.
- **Trip-plan warm start** (`TRIP_PLAN`, at most 20% of the start-to-beam-end time): generates candidate trips for every scout at 70 m and 30 m. The candidates are the best trips, the best trip including each contact, the best trip without each of its two most valuable contacts, and two rounds of re-planning each scout around the others' chosen trips. Without that variety, every scout's top trips share the same few contacts and nothing can be traded. `pack_trips` then picks at most one trip per scout with no contact twice (weighted set packing: greedy, then swaps). The chosen plan is realised in observation-time order, recorded and repaired. Stats are in the `trip_plan` block.

Same-run comparisons, 17 scenarios (8 board validation, L4 seeds 9–14, L3 seeds 3–5), 4 workers, default 45 s, all plans valid:

| Run | base (035a098) | + trip LNS | + trip plan |
|---|---:|---:|---:|
| run 1 total | 13807.5 | 13889.4 | – |
| run 2 total | 13740.3 | 13750.5 | 13918.9 |
| run 2 L4 subtotal | 9907.7 | 9917.2 | 10074.5 |
| run 2 board-8 subtotal | 4790.5 | 4821.3 | 4823.6 |

Repeat runs of the same code differ by 0.4–1% in total; in a third run the committed version scored 14027.9. Step 2's gain over step 1 comes mostly from two large wins (L4-11 +104, L4-14 +53); it won 9 cases and lost 6. Under 4 parallel workers the trip plan reaches its time cap before realisation finishes on the largest cases. Two variants scored lower in the same run: capping generation at 40% of the share (−37) and generating at one layer only (−133).

#### Joint re-plan of n scouts (LNS operator `group`)

Moving one scout at a time stays in a local optimum. A model of the board L4 cases re-planned scouts one by one and reached the same contact set as the planner. Exchanging contacts between two scouts raised the model value by 60–80 per case. Most of that gain came from cheaper scout trips, not extra coverage. The `group` operator (`GROUP_SIZES`, `GROUP_SHARE` = 20% of LNS steps, alongside `trip`) works like this:

- It picks n scouts: one is chosen like the `trip` move, and the rest are neighbours weighted by tour/base distance.
- It pools their contacts with up to `GROUP_POOL` unserved positive-net contacts within 2 km of their tours.
- It removes those tours and the strikes they confirm (`strip_scouts`).
- It runs the label search for each scout over the pool, keeping the top 40 trips plus cover trips and the current tour as an extra order.
- `best_disjoint` picks the best set of disjoint trips with a depth-first search and an upper-bound prune. The move goes ahead only if that set beats the estimated value of the current trips.
- It realises the visits in observation-time order through `realise_visits`, taking at most 0.8 s per move. The normal LNS repair and acceptance follow.

Counters in the `lns` block: `group_tried`, `group_realised`, `group_accepted`, `group_improvements`, `group_gain`, `group_label_s` and `group_sizes`.

Same 17 scenarios, 4 versions per run, 4 workers, default 45 s. Base is master at de78902. All 136 plans were valid.

| Run | base | n = 2 | n = 3 | n random 2–4 |
|---|---:|---:|---:|---:|
| run 1 total | 14203.3 | 14431.3 | 14367.1 | 14423.9 |
| run 2 total | 14351.8 | 14337.1 | 14343.9 | 14368.6 |
| sum of both | 28555.1 | 28768.4 | 28711.0 | 28792.5 |
| L4 subtotal, sum | 20785.4 | 20975.5 | 20925.0 | 20987.4 |
| board-8 subtotal, sum | 9859.7 | 9870.7 | 9879.2 | 9938.9 |

Random n in 2–4 is the default. Its gain over base is +237 summed (+0.8%), but it comes almost entirely from run 1: run 2 is a tie within the 0.4–1% run-to-run noise. Against base, mix won 11 and lost 5 in run 1, and won 8 and lost 8 in run 2. Base itself moved by +149 between the runs. Per-case swings of ±40–60 on L4 go both ways; for example, L4-14 was +102 in run 1 and −64 in run 2. Over the two runs the group move improved the best plan 85 times (out of ~3500 tries). About 70% of tries pass the estimate gate and are realised, and LNS iterations drop by ~12% because a group move costs more than a `trip` move.

### Local benchmark results

### Joint re-plan of conflicting strikes (LNS operator `strike`)

The strike move samples concrete candidate effector routes for an unserved positive-net
hostile, identifies the existing aircraft that actually violate separation (including pad
takeoffs), drops one to three conflicting hostile strike teams while retaining their scout
observations, then retries the target and displaced strikes in several orders.  Every retry
uses the normal incremental extension, so R4, R8 and R10 are rechecked globally.  It reports
`strike_tried`, `strike_accepted`, `strike_improvements` and `strike_gain` in `lns`.

Two 17-scenario, 45-second, four-worker frozen-copy runs compared master with random 1--3
removed conflicts (`mix`), fixed sizes, and a 30% strike share.  All 204 candidate plans were
valid; the slowest wall time was 46.1 s.  The default `mix` gained in both runs and on L4, so it
is enabled at a 20% LNS share.

| Run | Base | mix (1--3) | L4 base | L4 mix |
|---|---:|---:|---:|---:|
| 1 | 14304.5 | 14372.4 | 10401.6 | 10477.6 |
| 2 | 14358.5 | 14447.0 | 10453.4 | 10551.3 |

The move won/lost/tied 7/7/3 cases in run 1 and 8/6/3 in run 2.  It improved the best plan only
six times across 3,477 attempts, so the +156.4 summed gain is small relative to the observed
run noise.  It did not recover C22 or C07 in these winners; C30, C06, C14, C50, C48 and C17 were
already served in the relevant board plans.  Fixed-size and higher-share variants were less
consistent (the 30% share won run 1 but lost run 2), so they are not defaults.

Measured on this machine with five-second search budgets. These are sampled scenarios, not a guarantee of performance on unseen inputs.

| Scenario | Greedy score | Search score | Planner wall time |
|---|---:|---:|---:|
| L1-0 | 123.82 | 130.86 | 5.06 s |
| L2-7 | 266.23 | 276.76 | 5.06 s |
| L3-1 | 463.88 | 475.75 | 5.10 s |
| L4-0 | 590.34 | 622.35 | 5.11 s |

A separate **55-second L4-0** run scored **847.12** and completed in **55.15 seconds**, including process startup, final validation and output. All reported plans passed the official validator. Timing and search outcomes can vary with machine load.

A later audit of 12 additional five-second runs produced 10 wins and two losses against greedy; the combined short-budget score was lower. Repeating the two regressions at 55 seconds produced a win on L4-1 (574.11 vs 432.46) and a tie on L4-13 (1045.75). Both completed within 55.26 seconds. Use the normal budget for quality comparisons; the small table above is not evidence of universal improvement.

### Why contacts go unserved

Every run records, for each hostile and unknown contact, how far each attempt to serve it got. Each contact missing from the winning plan gets one primary reason. The planner's stderr line includes `unserved` (count per reason), `unserved_points` and `rejections`, and `--artifacts` writes per-contact detail to `tree.json`. The benchmark sums these per level in its console summary and `report.html`.

| Reason | Meaning |
|---|---|
| `collateral_ban` | A friendly or civilian is inside the collateral radius. R7 forbids the strike, so these points are never available. |
| `no_window` | The contact has no time window. |
| `never_tried` | No attempt ran: no pass reached it before the deadline. |
| `time_cap` | Attempts ran out of their per-node time before finding a valid child. |
| `no_team` | Not enough free effectors of the needed subtypes in that branch. |
| `scout_window` | No scout has a visit order that fits the window, strike deadlines, battery and horizon (time ranking). |
| `scout_route` | The baseline scout builder could not fit the visit into the window when appended to the scout's tour. |
| `scout_rule:Rn` / `strike_rule:Rn` | The scout or strike routes failed official rule Rn. |
| `not_improving` | A valid serve existed but did not raise the score. |
| `outranked` | A valid serve existed in some branch, but that branch did not win. |

The per-contact `beam_pruned` count (in `tree.json`/diagnostics) records valid serving children cut by the beam width. Contact summaries report `outranked_beam_pruned`: outranked contacts with at least one such child.

The most common attempt outcome is the reason. Ties go to the attempt that got furthest.

**L4 diagnosis (seeds 0–2, 55 s, one worker; 1798.76 vs greedy 1354.26).** 154 targets were missed, worth 8,542 points:

| Reason | Contacts | Points |
|---|---|---|
| `scout_route` | 46 | 2665 |
| `collateral_ban` | 36 | 2460 |
| `outranked` | 42 | 2117 |
| `scout_rule:R1` | 21 | 775 |
| `time_cap` | 3 | 209 |
| `scout_rule:R3` | 3 | 163 |
| `scout_rule:R8` | 3 | 153 |

Scout failures account for 73 contacts and 3,756 points. These numbers come from older code. On master `f737a2a`, the same seeds score 3888.04, and only 3 contacts (228 points) fail on scouting: 2 at R3 and 1 at R4. Another 20 (1,156 points) miss on `scout_window`, including the 8 unreachable targets ([details](history/2026-11-21-phase1-scout-tours.md)). A direct-flight check (`python3 analysis/scout_reach.py analysis/results/l4-unserved-diagnosis`) shows only 8 non-banned targets across the three scenarios that no scout can observe in time even flying straight from base at full speed (ignoring zones). Most scout failures therefore come from how tours are built (append-only, four nearest candidates), not from physics. `outranked` is the second lever: valid serves existed but the beam kept other branches. Time limits are a small factor (3 contacts). Collateral bans are unavailable points: they make "available points" totals look larger than what any plan can reach. Each L4 run completed 4–5 passes. The full run (report, scenarios, plans and logs) is committed in [analysis/results/l4-unserved-diagnosis/](analysis/results/l4-unserved-diagnosis/report.html).

A generated L2-7 demo is available locally at `output/search/tree.html`, with its winning fleet animation at `output/flight.html`. Generated outputs are ignored by Git; use `--artifacts` to reproduce them.

### Where the L2/L3 gap to the board leader comes from

Board (sum per level): leader L2 562, L3 1587; us at 035a098 L2 ~547, L3 ~1552. Every non-banned unknown is served on all five L2/L3 board scenarios. The hostile set is the knapsack optimum over the light/heavy supply, and effector assignment is within 1 point of the optimal matching (straight-line energy). **The gap is energy only**, in two parts:

1. **Effector climb** (fixed: `GROUND_STRIKES`). R2 bounds the altitude only of waypoints after the first and before the last, so a two-waypoint effector route (home → engagement at the contact, R_eng 3D) can fly at z = 0. That avoids e_climb × 30 m per effector, worth 5–19 Wh per L3 scenario before the fix. Members whose straight ground line enters a zone or protected area fly the 30 m route.
2. **Scout tours** (partly fixed: `SCOUT_ENERGY_RANK`). Scouts differ by about ±30 % in energy per metre (L2-202: S01 7.9 J/m, S02/S03 10.4–10.7), but candidate scouts were ranked by added *distance*, so the cheapest scout was often left idle. Our scouts flew 24–29 km per L3 scenario. One tour through all observation disks without windows is 12–13 km. An offline VRPTW estimate (windows, R4 deadlines, horizon, battery) gives 51–69 Wh against our 71–82 Wh under the same model.

Hand-built plans that use the planner's own builders (VRPTW tours, strikes re-timed, official scorer) score L2-201 295.87, L2-202 266.06 (one scout instead of two), L3-301 715.06, L3-302 418.77 (two scouts instead of four) and L3-303 444.72. That is **L2 561.9** (leader 562) and **L3 1578.6** (leader 1587), so these two causes account for nearly the whole gap. What remains is scout tour construction: fewer and better-chosen scouts per scenario.

Benchmark, same run, four workers, 45 s, all plans valid:

| Batch | 035a098 | + ground strikes | + energy-ranked scouts |
|---|---:|---:|---:|
| 17 cases (board-8, L4 9–14, L3 3–5) | 13745.67 | 13826.45 | **13928.02** (+1.3 %; 14 W / 3 L) |
| board-8 | 4787.92 | 4803.09 | 4810.50 |
| board L2 / L3 | 547.27 / 1543.35 | 549.12 / 1556.95 | 554.69 / 1570.18 |
| L4 (8 cases) | 9922.37 | 9994.03 | 10052.34 |
| L3 seeds 3–15 (separate run) | 6594.65 | | 6660.01 (+1.0 %; 12 W / 1 L) |

In an earlier run, ground strikes alone gave 13763.04 → 13874.67 on the 17 cases and 6605.11 → 6647.56 on L3 seeds 3–15. Two changes were tested and dropped: trying the 30 m scout layer first (13816.67 against 13874.67 for ground strikes alone), and an LNS operator that removes a whole tour (no gain on L2-202 or L3-302).

## Leaderboard submission

`submit_to_board.py` runs the full pipeline in one command: generate the eight public validation scenarios, plan each one, print scores, and submit to the live board.

```sh
# Full pipeline — generates scenarios, plans, scores, and submits
python3 submit_to_board.py --token <TOKEN>

# Skip regenerating scenarios and replanning (reuse existing board-plans/)
python3 submit_to_board.py --token <TOKEN> --skip-generate --skip-plan

# Score locally without submitting
python3 submit_to_board.py --token <TOKEN> --skip-submit
```

The `--team` (default `ELARIS`) and `--server` (default `http://192.168.2.19:8080/`) flags can be overridden if needed. Plans are written to `board-plans/` and scenarios to `validation/`. The server keeps your best score per scenario, so resubmitting a worse plan never hurts.

## Hand-in

Copy `planner.py`, `search_planner.py`, `candidate_matrix.py` and `tree_view.py` together. The official runner supplies `fleetkit` via `PYTHONPATH`. Local runs discover the bundled kit; `FLEETKIT_DIR` can explicitly select another copy. No third-party packages are required, and no changes to the official kit are needed.

The original analysis and isolated-mission checker remain in [analysis/](analysis/README.md).

Development and agent guidance, including test and benchmark commands, is in [AGENTS.md](AGENTS.md).

### Persistent optimisation history

Run benchmarks through `test_logger.py` to keep an independent record of every batch:

```sh
python test_logger.py --cases L1:0 L2:7 L3:1 L4:0 --seconds 5 --note "Before optimisation"
python test_logger.py --cases L1:0 L2:7 L3:1 L4:0 --seconds 5 --note "After optimisation"
python test_logger.py summary
```

With no options it runs L1-L4, seeds 0-2, at 45 seconds per case with one worker.
Each invocation creates a unique directory in `history/`; older runs are
never overwritten. `result.json` includes official points, per-case and aggregate
planner runtimes, baseline gains, validity/failures, served contacts, search diagnostics,
settings, UTC timestamps, source SHA-256 hashes, Git state and machine/Python details.
The `benchmark/` subdirectory keeps scenarios, both plans, process logs, JSON/CSV
results and the HTML report. `--history` chooses another history directory.

The logger reports score and runtime changes against the latest completed batch
with identical cases, budgets, worker count, environment and official-kit hashes.
Source hashes distinguish code revisions even with uncommitted edits. Positive score
deltas and negative runtime deltas indicate improvement; percentage changes from
zero are null. Failed planners count as zero, and batches with setup errors are not
used for history comparisons. `summary` exports all run records as JSONL to stdout.
Runs terminated before final collection retain a `running` record and any benchmark
artifacts already written. Logging applies when launching through this script;
direct `planner.py` invocations do not enter this history. Use identical settings
and one worker for useful comparisons; machine load can still affect results.


History is intentionally not ignored: commit new run folders with `git add history`
to share them. The two original runs have been moved from `output/test-history/`
without changing their contents; embedded command paths describe their original
execution location. They lack per-contact diagnostics and cannot be backfilled.

New runs preserve `results[].diagnostics.contacts`, keyed by contact ID. Each
hostile/unknown contact includes `cls`, `points`, `served`, `attempts`, `outcomes`,
`rules`, and an unserved `reason`. Its enclosing case identifies the level and seed.
These details are also in benchmark JSON/JSONL results and the planner log.

```sh
python test_logger.py contacts
```

This prints per-class counts, reasons, lost points and scout-failure rates across
saved runs. Each benchmark summary also includes `total.contact_summary` and the
same breakdown per level. Scout failures mean the primary unserved reason is
`scout_route` or `scout_rule:*`; the rate denominator is all diagnosed contacts of
that class, including served contacts. Old runs, failed planners and custom planners
without diagnostics are counted as cases without detail, never as zero failures.
Repeated cases across runs count as repeated observations; use `--history` to select
a separate collection for a controlled comparison. Primary reasons summarise the
search attempts, not proof that a contact is physically impossible to serve.

## Link-loss policy (optional second part)

`policy.py` is the per-aircraft policy for the organisers' link-loss challenge (`LINKLOSS.md` in the official kit). It is scored separately from the planner and is handed in next to `planner.py`.

- **Reclassified target:** an effector senses its target within 300 m, before reaching it, and turns back.
- **Pop-up zone:** every move is checked against the zones an aircraft knows; it never enters one, including when turning back.
- **Confirm before engage (R4):** an effector engages only when it knows the observation ended at least 11.5 s earlier. Observations finished before the cut come from the plan; later ones come from the scout's message, which carries the end time. Otherwise it waits short of the target, then turns back.
- **Separation (R8):** each aircraft predicts its peers from the plan and from their announced (route time, rate), with ±1.5 s for stale messages and drones about to take off, and picks the move that keeps at least 55 m. Every move must also pass the reachable-set filter described under *Link-loss safety pass*.
- **Messages:** only rate changes (repeated, then refreshed every 8 s while off schedule) and scout observation notices.
- **Wire format:** one JSON string of type-coded records (`V` rate state, `O` observation, `R` reclassified, `Z` zone), base-93 printable ASCII, 1 byte per character; see the `policy.py` docstring. Same decisions as the dict format, fewer bytes; news-distribution columns (coverage, reach 90 %, t90, missed) are in `compare`.
- **News (store and forward, on):** `NEWS_MODE = "forward"`. Each drone keeps every sensed or heard danger (reclassified contact, pop-up zone) and the peers it knows have it (heard it from them, or sent it 3 times while they were within half its estimated range). Each second it predicts peers from the plan and sends a danger only when a peer that may lack it is in reach; the range is estimated from the farthest distance a peer was heard from. `"burst"` (send 3 times on learning) and `"off"` remain available.
- **Measured news distribution** (20 cases, L1-L4 seeds 0-4, one comm seed; drones airborne at the event that were still flying at the end and never knew it): off 12/16/25/19, burst 11/12/11/6, forward 7/3/2/1 at 250/500/1000/2000 m; capacity made no difference (messages are a few bytes). Messages about 8 per aircraft per minute in all three.
- **Score:** with 3 comm seeds at 4 ranges (240 runs each) failed runs were off 19, forward 22, and forward that ignores heard news 22; S_loss 0.865 / 0.855 / 0.852, a difference within noise. Acting on news neither causes nor prevents failures, and safe runs keep identical retention, because the policy's only use of news is aborting earlier. All failures are separation (R8), mostly at 250 m: that is the next thing to fix.

- **Coordination (on):** each drone now uses its picture of the fleet to stay on the plan rather than react to it. Pairs both on schedule trust the validated plan (`TRUST_PLAN`); scouts report the exact observation end (`EXACT_OBS_END`); a confirmed effector waits for late strike partners so the group still engages inside the sync window (`GROUP_SYNC`); an unconfirmed effector waits for a scout it knows is late while the window allows (`SCOUT_WAIT`). In safe runs before this, every contact lost against the oracle was a strike that missed sync because partners at the shared target backed off and reversed into one another.
- **Measured** (20 cases L1-L4 seeds 0-4, 250/500/1000/2000 m at 200 B/s, 3 comm seeds, 240 runs): S_loss 0.855 → 0.926, failed runs 22 → 12, retention of safe runs 0.960 → 0.992; at 1000 and 2000 m 0 failures and retention 0.999-1.000. Unseen seeds 20-29 (1000 and 250 m, 2 comm seeds, 160 runs): S_loss 0.732 → 0.833, failed 28 → 20. 17 runs were fixed and 7 newly broken, all R8 at 250-500 m; the remaining failures are mostly `lost_message` at 250 m. Tried and dropped: assuming an off-plan peer may have stopped anywhere since its last message (fixed 500 m, but frozen "ghosts" of engaged effectors boxed others in: 24 failures).

### Link-loss safety pass

Each move must now pass a **one-step reachable-set filter** (`SAFE_STEP`): every peer is predicted from the plan (on schedule) or from its last announced (route time, rate), widened for a lost later message (it may have stopped since, `HULL_S`; it may have changed its mind, `ANY_CAP_S`), and a move must keep d_sep + `STEP_MARGIN_M` from all of it. Off-schedule aircraft beacon their state every `BEACON_S` = 1 s. Two off-schedule aircraft resolve conflicts by id: the later id gives way to any move of the earlier one, which trusts that only when the later one has announced a hold. Braking or holding also keeps clear of on-schedule traffic behind (`SLOW_LOOK_S`). Safe options are ranked by the room they keep over the next `AHEAD_S` = 3 s. After `STUCK_S` = 30 s held against its wish, an aircraft prefers any safe move for `UNSTICK_S` = 10 s, which breaks deadlocks near the base.

Benchmark: `analysis/linkloss_bench.py` (our 45 s plans for the 8 validation scenarios in `output/ll/plans`, official `make_events` draws 1-10, comm seeds 0-4, 400 runs per setting); `analysis/linkloss_diag.py` replays one run second by second for a pair of aircraft.

| Setting | Policy | Retention | Failed runs | Msgs/ac/min | S_loss |
|---|---|---:|---:|---:|---:|
| 1000 m × 200 B/s | before (master) | 0.922 | 29/400 | - | - |
| 1000 m × 200 B/s | **now** | **0.991** | **0/400** | 15.9 | **0.959** |
| 250 m × 50 B/s | before (master) | - | 187/400 | - | - |
| 250 m × 50 B/s | **now** | **0.902** | **1/400** | 21.9 | **0.858** |

Per scenario, now (50 runs each):

| Scenario | Retention 1000×200 | Fail | Msgs/ac/min | S_loss | Retention 250×50 | Fail | Msgs/ac/min | S_loss |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| L1-101 | 1.000 | 0 | 12.5 | 0.975 | 1.000 | 0 | 14.0 | 0.972 |
| L2-201 | 1.000 | 0 | 9.7 | 0.981 | 1.000 | 0 | 10.4 | 0.979 |
| L2-202 | 1.000 | 0 | 16.1 | 0.968 | 0.784 | 0 | 26.0 | 0.732 |
| L3-301 | 0.988 | 0 | 21.3 | 0.945 | 0.829 | 0 | 28.7 | 0.771 |
| L3-302 | 0.972 | 0 | 23.3 | 0.926 | 0.931 | 1 | 28.9 | 0.873 |
| L3-303 | 0.988 | 0 | 16.1 | 0.956 | 0.982 | 0 | 19.4 | 0.944 |
| L4-401 | 0.983 | 0 | 13.9 | 0.955 | 0.867 | 0 | 21.5 | 0.824 |
| L4-402 | 0.996 | 0 | 14.6 | 0.967 | 0.824 | 0 | 26.3 | 0.771 |

Under 0.5 ms per call everywhere (0.26 ms at 1000 × 200).

**Sizing sweep** (all 20 range × capacity cells, 400 runs each): retention

| Range | 50 B/s | 100 | 200 | 400 | 800 |
|---|---:|---:|---:|---:|---:|
| 250 m | 0.902 (1 fail) | 0.902 (1) | 0.902 (1) | 0.902 (1) | 0.902 (1) |
| 500 m | 0.976 | 0.976 | 0.976 | 0.976 | 0.976 |
| 1000 m | 0.987 (2) | 0.991 | 0.991 | 0.991 | 0.991 |
| 2000 m | 0.990 (1) | 0.990 (1) | 0.990 (1) | 0.990 (1) | 0.990 (1) |

**sizing_score 0.45** (9 of 20 cells with retention ≥ 0.9 and no failure). Capacity hardly matters (messages are a few bytes); the failing cells share three runs:

- **L3-302 draw 6, comm 4 (250 m, R6):** unavoidable as far as we can tell. A pop-up zone (radius 115 m) appears at t = 565 s on top of an aircraft that had left its schedule: L05, held near the base by two peers, was already inside the zone's footprint when it appeared. In a variant that kept L05 moving it went from 181 m to 26 m from the zone centre between t = 559 and 565 and failed the same way. `make_events` only checks that aircraft flying the original schedule are clear of a new zone.
- **L3-302 draw 7, comm 3 (2000 m, R8 33.7 m) and L4-401 draw 4, comm 4 (1000 m × 50 B/s, R8 29 m):** a returning aircraft announced a hold a few seconds before touchdown and the message was lost; the peer extrapolated it as landed, dropped it and moved. A 3 s grace before dropping a "landed" peer (`LANDED_S = 3`) fixes both runs, but the full rerun broke 3 other runs at 250 m × 50 B/s (6 failures instead of 4 over 1000×200, 250×50, 1000×50, 2000×200), so it is not adopted.
- **L4-401 draw 4, comm 2 (1000 m × 50 B/s, R8 36.9 m):** two aborted aircraft dancing around each other near the base; hold announcements lost at 50 B/s.

Any change to the policy changes which messages are lost in every later second, so individual failing runs come and go between variants; compare totals over all draws, not single runs. Tried and dropped: `BEACON_S` 2 s (more R8 at 250 m), `SLOW_LOOK_S` = 4 (R6 deadlock at the base), treating silent holding peers as gone (fixed nothing, one new R8).

**Messages:** S_loss is 0.032 below retention at the leaderboard setting, which is exactly μ × 15.9 messages per aircraft per minute. Most of them are the 1 s off-schedule beacons that the separation filter relies on. Lowering `FWD_TRIES` 3 → 2 (fewer danger-news repeats) saved nothing (16.1 messages, S_loss 0.958) and added one R8 at 250 m × 50 B/s (2/400), so it was not adopted. Danger news is a small share of the traffic; cutting the beacons would trade the safety above for at most about 0.02 S_loss.

**Planner-side separation buffer.** `planner.py --sep-buffer M` (default 0) makes the search check R8 pairs against d_sep_m + M, so the plan itself leaves margin for trajectories flown after a cut; exported plans are still validated with the official scorer. Plan-score cost, L1-L4 seeds 0-1, 45 s, 4 workers, two rounds (totals per round):

| Buffer | Round 1 | Round 2 | Mean | vs 0 m |
|---|---:|---:|---:|---:|
| 0 m | 4751.9 | 4780.9 | 4766.4 | - |
| 10 m | 4580.5 | 4627.0 | 4603.7 | −162.6 (−3.4 %) |
| 15 m | 4609.4 | 4611.2 | 4610.3 | −156.1 (−3.3 %) |

All plans valid; the loss is almost all L4 (−130 to −200 per round), L1-L3 within 8 points. The link-loss benefit for the competition is zero: `LINKLOSS.md` says the final link-loss evaluation uses the organisers' own reference plans, not ours. So the default stays 0 and the margin stays policy-side (`SEP_MARGIN_M` = 15 m, `STEP_MARGIN_M`).
### Link-loss evaluation on held-out event seeds

`analysis/linkloss_eval.py` builds the cases (8 validation scenarios, greedy plans, held-out event seeds 1001-1003 never used for tuning; development used 501-512), runs `fleetkit.linkloss.score.evaluate_cases` with 5 comm seeds on the leaderboard cell (1000 m, 200 B/s) and the full `sizing()` grid (4 ranges x 5 capacities, 2400 runs), and writes `analysis/linkloss/*.csv`, `analysis/linkloss_sizing.csv` (retention per cell + frontier of the kept variant) and `analysis/linkloss_sizing.svg`.

```sh
python3 analysis/linkloss_eval.py run --policy policy.py --name wcap --workers 8
python3 analysis/linkloss_eval.py report --sizing-from wcap
```

| Variant | Retention (LB) | Failed runs (LB) | Msgs/ac/min | S_loss | Grid failures | sizing_score | Frontier | Beats continue and return from |
|---|---:|---:|---:|---:|---:|---:|---|---|
| continue | 0.000 | 120/120 | 0.00 | 0.000 | 2400/2400 | 0.00 | none | never |
| return | 0.399 | 20/120 | 0.00 | 0.399 | 400/2400 | 0.00 | none | never |
| hold | 0.473 | 0/120 | 0.00 | 0.473 | 0/2400 | 0.00 | none | 50 B/s at every range |
| Phase 5 | 0.967 | 2/120 | 9.56 | 0.948 | 174/2400 | 0.00 | none | never (fails a run in every row) |
| + `BOXED_NOMINAL` | 0.973 | 1/120 | 9.58 | 0.954 | 121/2400 | 0.00 | none | never |
| + `O_FORWARD` | 0.986 | 0/120 | 5.65 | 0.975 | 94/2400 | 0.50 | 1000:50 2000:50 | 50 B/s at 1000 and 2000 m |
| **+ `WINDOW_CAP` (kept)** | **0.986** | **0/120** | **5.65** | **0.975** | **94/2400** | **0.50** | **1000:50 2000:50** | **50 B/s at 1000 and 2000 m** |

A cell "meets" when it has 0 failures and beats continue and all-return; `hold` meets everywhere but keeps only 47 %. **The 0-violation gate is not met**: the kept variant has 0 failures at 1000 and 2000 m, but 17-18/120 runs fail at 250 m and 1-2/120 at 500 m, all separation (R8). No R4 failure occurred in any variant.

- **Step 1, messages:** nothing is sent while on plan. The only periodic traffic is the `V` refresh every `REFRESH_S` while off schedule. Removing it (development seeds, 250/500/1000 m) raised failures from 51/1/3 to 77/15/8 of 480; replacing it with sends to peers coming into reach (`V_FORWARD`) gave 57/3/3; both together, or a 2-4 s refresh, improved 250 m (30-31) but worsened 500 m (5-6). The 8 s refresh stays. The old O traffic (every second until the last strike + 25 s) was most messages; `O_FORWARD` sends each notice only to that contact's strike effectors, store-and-forward, and cut traffic from 9.6 to 5.6 msgs/ac/min while removing the last leaderboard failure.
- **Step 2, proceed rule:** an effector engages only on a confirmation it holds: from the plan when the observation ended before the cut, or from a heard O. Otherwise it holds short (grace, or a late scout's predicted observation) but never past window end minus `HOLD_LEAD_S`, then returns. Engaging on the scout's predicted state without an O was tried and broke R4 in about a quarter of runs (silent scout failures and unheard deviations), so it is not used. Waiting all the way to the window end added R8 conflicts (250 m: 50 -> 60 failures). `WINDOW_CAP` is neutral on held-out seeds.
- **Step 3, handover: not implemented.** A policy can only continue, hold, change speed or return along its own planned route (`LINKLOSS.md`), so no aircraft can fly to another's task. Only effectors that already strike the same contact could "take over", and they already engage independently once confirmed (`GROUP_SYNC`). No `A` record was added.
- **Step 4, tight budgets:** without messages the policy holds or returns, never strikes unconfirmed (0 R4 everywhere). Capacity hardly matters (messages are a few bytes); range does. The remaining 250 m failures are aircraft that are already boxed in: every move breaks 55 m against some possible peer state. `BOXED_NOMINAL` then picks the move with most room against announced states, which removed about 30 % of grid failures but not all. More `V` traffic trades 250 m failures for 500 m ones. Fixing the rest needs plans with more spacing or earlier, coordinated aborts.

Run the official check (with the updated official kit on `PYTHONPATH`):

```sh
python3 handin/check_policy.py --policy /path/to/policy.py --levels L1,L2,L3,L4 --seeds 0-1
```

Leaderboard setting (1000 m, 200 B/s), greedy plans, 40 unseen scenarios (L1–L4, seeds 20–29, 2 communication seeds each):

| Policy | S_loss | Retention | Failed runs |
|---|---:|---:|---:|
| hold | 0.361 | 0.361 | 4/80 |
| return | 0.366 | 0.366 | 2/80 |
| broadcast | 0.292 | 0.412 | 47/80 |
| **policy.py** | **0.799** | **0.817** | **9/80** |

About 9 messages per aircraft per minute and under 0.2 ms per call. The remaining failures are all separation (R8) between aircraft that leave the plan close together, mostly on L4.

### Plan fragility vs link-loss retention

`search_planner.plan_fragility(plan, scenario)` scores a plan as tight couplings + near pairs − (capped mean gap / 120 s). Tight couplings are engage waypoints with confirm slack under 10 s. Near pairs are aircraft pairs closer than `d_sep_m` + 20 m. The gap is the confirm→strike gap. `fragility_parts` returns the components. The planner's diagnostics and the benchmark summary log both (about 20–40 ms on an L4 plan).

`analysis/fragility_correlation.py` scored 127 archive plans on 10 tuning cases with `policy.py`. Within a scenario, fragility does not predict retention: rho −0.01 at 1000x200, −0.05 at 500x100 and 250x50, every CI spanning 0. Near pairs even correlate the wrong way (+0.17). Details are in `history/2026-11-22-fragility-correlation.md`. The link-loss-aware tie-breaker was therefore not built. The official link-loss score uses the organisers' reference plans anyway.

### Link-loss knowledge store

`analysis/linkloss_knowledge.py` records what every drone knew, second by second, and where each fact came from (`self`, `plan`, `sensed`, `heard` with the sender, `inferred`), plus believed versus real peer positions whenever two drones are within 200 m, event learning time against the time it was needed, and a timeline around each separation failure. The recorder never changes a run (tested).

```sh
python3 analysis/linkloss_knowledge.py --history --note "what changed" --policy my_policy.py \
  --levels L1,L2,L3,L4 --seeds 0-4 --settings 250x200,500x200,1000x200,2000x200 --comm-seeds 3 --workers 12
python3 analysis/linkloss_knowledge.py compare                 # every stored collection, per setting
python3 analysis/linkloss_knowledge.py diff BEFORE_DIR AFTER_DIR  # run by run: fixed, broken
```

Compare policies with at least 3 comm seeds: extra messages change the random delivery draws of the whole run, and with one seed off and store-and-forward looked like 6 against 15 failures, which 3 seeds showed to be 19 against 22 in 240 runs. Capacity made no difference so far, so one capacity per range is enough. A full 20-setting collection is about 25 MB: store in `history/` the collections worth keeping and use `output/` (the default without `--history`) for the rest.

**Using news in your own policy.** Everything is in `policy.py` and can be copied as is: the codec (`encode_records`, `decode_records`, contacts numbered in scenario order, `cw` digits wide), `_learn` (decodes the inbox into `reclassified`, `zones`, `confirmed`, `peer_state`), `_note_news` and `_forward` (store and forward), `_peer_where` (peer prediction from the plan, including drones on the ground). A policy that keeps the same attribute names (`reclassified`, `zones`, `confirmed`, `peer_state`, `aborted`, `rate`, `_peer_pos`, `peers`) is fully recorded by the knowledge store; others can define `knowledge()` returning `{fact: value}`.

`--history` stores the collection in Git-tracked `history/linkloss/<id>/` (one directory per collection, about 5-6 MB for 80 runs): `collection.json` (note, settings, policy SHA-256, kit hash, Git state, per-setting S_loss), the exact `policy.py`, `runs.jsonl`, `report.md`, `cases/*.json.gz` (scenario, plan, events) and `runs/*.json.gz` (`summary`, `facts`, `steps`, `near`). Load with `load_run`, `load_case`, `iter_collections`. Without `--history` it writes under ignored `output/`. Plans are the greedy baseline, as in `check_policy.py`.
