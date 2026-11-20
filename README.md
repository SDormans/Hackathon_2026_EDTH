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
- Expansion has local time caps, preventing an impossible contact from consuming the whole run. Children must improve the parent's actual score. All archive winners receive a full official validation and score before export; final validation stops starting new calls at 58 seconds. The valid empty plan is the last fallback.

The result is the best N plans **found**, not a proof of the global top N. Contact ordering, a bounded beam, fixed flight settings, shortlists and the improving-child restriction can all discard useful alternatives. The trace records accepted nodes and skip branches; failed route attempts are aggregated by rule, rather than stored individually. Trace growth is capped between passes to keep tiny scenarios from producing unlimited diagnostics.

## Checks and benchmarks

```sh
python3 -m unittest discover -s tests -v
python3 analysis/benchmark_search.py --seconds 5
python3 analysis/benchmark_search.py --seconds 55 --cases L4:0
```

Tests compare incremental validation with the full official validator on perturbed routes, check conflicts with existing aircraft and broken earlier confirmations, verify branch independence, and demonstrate a skip branch beating greedy allocation. A subprocess test copies only the hand-in modules and checks the CLI and exported plans.

The benchmark uses the official scorer and reports baseline and search scores and wall times. Scenario generation and the separate comparison baseline are outside the planner's measured runtime.

### Local benchmark results

Measured on this machine with five-second search budgets. These are sampled scenarios, not a guarantee of performance on unseen inputs.

| Scenario | Greedy score | Search score | Planner wall time |
|---|---:|---:|---:|
| L1-0 | 123.82 | 130.86 | 5.06 s |
| L2-7 | 266.23 | 276.76 | 5.06 s |
| L3-1 | 463.88 | 475.75 | 5.10 s |
| L4-0 | 590.34 | 622.35 | 5.11 s |

A separate **55-second L4-0** run scored **847.12** and completed in **55.15 seconds**, including process startup, final validation and output. All reported plans passed the official validator. Timing and search outcomes can vary with machine load.

A generated L2-7 demo is available locally at `output/search/tree.html`, with its winning fleet animation at `output/flight.html`. Generated outputs are ignored by Git; use `--artifacts` to reproduce them.

## Hand-in

Copy `planner.py`, `search_planner.py`, `candidate_matrix.py` and `tree_view.py` together. The official runner supplies `fleetkit` via `PYTHONPATH`. Local runs discover the bundled kit; `FLEETKIT_DIR` can explicitly select another copy. No third-party packages are required, and no changes to the official kit are needed.

The original analysis and isolated-mission checker remain in [analysis/](analysis/README.md).
