# Fleet search planner

A standard-library planner that turns the baseline's route candidates into a bounded search tree. It always keeps valid partial fleet plans, returns the best plan found, and can export the best N distinct plans and an interactive tree.

Run from this directory:

```sh
python3 planner.py path/to/scenario.json output/plan.json
```

The default search deadline is **55 seconds from startup**, including input loading. This leaves time inside the organisers' 60-second limit for full validation and writing the result. The required plan is written before optional diagnostics. Python imports and individual validator calls cannot be preempted: this is a cooperative deadline, not a guarantee against arbitrary machine stalls.

For a shorter run with five candidate plans and the tree:

```sh
python3 planner.py path/to/scenario.json output/plan.json \
  --seconds 5 --top-n 5 --beam-width 6 --artifacts output/search
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

For example, one test gives a 90-point contact both available light effectors, while two 70-point contacts each need one. Greedy commits both effectors to the 90-point contact. A skip branch keeps them available for the 140-point pair; the test confirms that the resulting valid plan also wins after energy and effector costs.

Incremental validation makes more candidate checks possible within the same deadline. The matrix helps spend those checks on promising groups; the official rules still decide validity. The benchmark below shows higher scores on all four sampled scenarios, including a **43.5% improvement on L4-0** with the full budget. These measurements do not establish that every mechanism contributes equally or that every unseen scenario will improve. The capped greedy warm start can also stop before completing the baseline, so there is no universal guarantee of matching its score.

## How the tree works

Each node holds routes, scout tasks, consumed effectors and a score. At each contact the search can skip it or serve it with different scouts, effector groups and flight settings. Serving a hostile constructs its observation and complete required strike group together. Scouts always have return-and-land legs. Extending a scout task list rebuilds its tour and rechecks earlier confirmations.

- `candidate_matrix.py` ranks effector/contact choices using the distance/energy estimates from the analysis matrix. Estimated time and energy do **not** exclude candidates. Only exact subtype availability and the official collateral restriction exclude groups. At most three shortlisted teams are tried, so the search remains heuristic.
- `search_planner.py` first follows the original greedy choices with faster validation, for up to a quarter of the budget (capped at 10 seconds). If interrupted, it retains the last accepted plan. It then searches beams using scarcity/value, window-close, points and seeded randomised orders.
- Every extension applies the official local rules to changed routes, rechecks confirmation and strike completeness globally, and checks separation between changed routes and all other routes. Unchanged pairs retain their parent's validation result. Energy and separation flight pieces are cached per branch.
- The beam keeps at most `--beam-width` distinct states at a depth. A separate archive keeps up to `--top-n` distinct valid plans across the entire search. The two settings are independent. Distinct means different assignments or timed waypoints, so solutions may serve the same contacts.
- Scout visits can go at any position of any scout's tour. A lower bound on timing (straight legs at the lowest layer, no detours) first drops positions that cannot meet the contact's window, the later tasks' windows or the horizon; the remaining positions are ranked by added tour time, including hovering while waiting for windows (`--scout-cost time`, the default), or by added distance (`--scout-cost distance`). Each beam expansion tries the 6 cheapest, repair the 8 cheapest.
- The search stops a short reserve (at most 3 seconds) before the deadline to **repair** the best archived plans: unserved unknowns, then hostiles, are inserted at the cheapest feasible tour positions, with the free effectors. Within each class, contacts with the highest regret (fewest or most unequal scout alternatives) go first. Each repaired plan is recorded and archived; searching resumes if time remains.
- **Large-neighbourhood search** uses the last `--lns-share` of the remaining time (default 0.5). Each iteration removes 2–6 served contacts from the current plan (random, related in space and time, lowest-value strikes, or one scout's whole tour), rebuilds the affected scout tours, drops their strikes and reinserts them with regret ordering together with other unserved contacts. Better plans are always kept; worse ones are accepted with simulated-annealing probability, with the temperature falling to zero at the deadline. Every candidate is checked by the incremental validator and archived.
- Route builders reuse memoised zone detours, and candidates whose waypoints fall outside the horizon or a contact's window are rejected before parsing. Duplicate plans are detected from per-route waypoint tuples.
- Expansion has local time caps, preventing an impossible contact from consuming the whole run. Children must improve the parent's actual score. All archive winners receive a full official validation and score before export; final validation stops starting new calls at 58 seconds. The valid empty plan is the last fallback.

The result is the best N plans **found**, not a proof of the global top N. Contact ordering, a bounded beam, fixed flight settings, shortlists and the improving-child restriction can all discard useful alternatives. The trace records accepted nodes and skip branches; failed route attempts are aggregated by rule, rather than stored individually. Trace growth is capped between passes to keep tiny scenarios from producing unlimited diagnostics.

## Checks and benchmarks

```sh
python3 -m unittest discover -s tests -v
python3 analysis/benchmark_search.py --seconds 5 --cases L1:0 L2:7 L3:1 L4:0
python3 analysis/benchmark_search.py --levels L1,L2,L3,L4 --seeds 0-4 --workers 4
```

Tests compare incremental validation with the full official validator on perturbed routes, check conflicts with existing aircraft and broken earlier confirmations, verify branch independence, and demonstrate a skip branch beating greedy allocation. A subprocess test copies only the hand-in modules and checks the CLI and exported plans.

### Batch benchmarks

The runner defaults to **12 scenarios** (L1–L4, seeds 0–2), a **55-second search budget** and one worker. Choose a seed range or explicit cases, and optionally run scenarios concurrently:

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

### Local benchmark results

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
| `scout_route` | No tour position passed the timing bound, or the baseline scout builder could not fit the visit into the windows. |
| `scout_rule:Rn` / `strike_rule:Rn` | The scout or strike routes failed official rule Rn. |
| `not_improving` | A valid serve existed but did not raise the score. |
| `outranked` | A valid serve existed in some branch, but that branch did not win. |

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

Scout failures account for 73 contacts and 3,756 points. A direct-flight check (`python3 analysis/scout_reach.py analysis/results/l4-unserved-diagnosis`) shows only 8 non-banned targets across the three scenarios that no scout can observe in time even flying straight from base at full speed (ignoring zones). Most scout failures therefore come from how tours are built (append-only, four nearest candidates), not from physics. `outranked` is the second lever: valid serves existed but the beam kept other branches. Time limits are a small factor (3 contacts). Collateral bans are unavailable points: they make "available points" totals look larger than what any plan can reach. Each L4 run completed 4–5 passes. The full run (report, scenarios, plans and logs) is committed in [analysis/results/l4-unserved-diagnosis/](analysis/results/l4-unserved-diagnosis/report.html).

**Insertion slots (L4 seeds 0–9, 55 s, two batches of five run concurrently with master).** Total 9462.34 vs master 7325.62 (+29.2%), 10 wins of 10 against master, all plans valid. Scout-related unserved points fell from 11,291 to 7,605 and `outranked` from 6,442 to 4,956. Details in [analysis/scouting-bottleneck-findings.md](analysis/scouting-bottleneck-findings.md#5-results-insertion-slots-with-a-timing-bound).

**Time ranking and large-neighbourhood search (same cases, 8 concurrent batches, 2–3 rounds).** Against the insertion-slot version (mean 9,476): time ranking alone 9,184 (−3.1%); time ranking with LNS share 0.5 9,917 (+4.7%); distance ranking with LNS share 0.5 10,302 (+8.7%, higher on all 10 cases). All plans valid. Details in [section 6 of the findings](analysis/scouting-bottleneck-findings.md#6-results-time-ranking-and-large-neighbourhood-search).

A generated L2-7 demo is available locally at `output/search/tree.html`, with its winning fleet animation at `output/flight.html`. Generated outputs are ignored by Git; use `--artifacts` to reproduce them.

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

With no options it runs L1-L4, seeds 0-2, at 55 seconds per case with one worker.
Each invocation creates a unique directory in `output/test-history/`; older runs are
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
