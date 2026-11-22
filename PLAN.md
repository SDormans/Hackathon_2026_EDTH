# Team plan: Fleet Orchestration (EDTH Paris)

Deadline: **22 Nov, midday**. Base: Nick's beam-search planner (`planner.py` + `search_planner.py`).

## Rule 1: never score 0
A crash, a timeout (> 60 s) or one broken rule makes that scenario score 0.
Safety comes before cleverness.

## Where we stand
- Greedy baseline on the 8 validation scenarios: **2664.5** total
- Our search planner: *(to be measured)*
- In L4 the baseline wins about 1,000 of about 3,700 available points and uses only 5 of 15 scouts.
- Phase 2 beam (held-out L1–L4 seeds 30–45, 45 s, one worker): **35151.40** vs 34900.15 before (35044.05 for the same base code in the scout-tour phase run). All 64 plans valid, slowest run 45.9 s. Kept: diversity slots and beam width 9. Dropped: bound ranking and non-improving children ([README](README.md#phase-2-beam-changes-held-out-seeds-3045-45-s)).

## What we found
- **Takeoff collisions.** All aircraft sit on pads ~11 m apart, but separation (R8) needs 40 m.
  Aircraft climbing out at the same time break R8. It is the top reason candidate plans get rejected.
- **Idle scouts.** Unknown contacts need only a scout pass (nearly free points), but most scouts never fly.
- **Cost of a strike.** Each effector costs 10 points plus energy, so a low-value hostile
  needing 3 effectors earns almost nothing. Rank targets by points minus cost.

## Tasks (in order)

| # | Task | Why | Owner | Status |
|---|------|-----|-------|--------|
| 1 | Safety: stop at ~45–50 s (now up to 58 s), build the hand-in folder, run `check_handin.py` on many seeds | Avoid timeouts on a slower grading machine | ? | done: 45 s search, validation stops at 50 s, slowest held-out L4 run 45.9 s |
| 2 | Takeoff scheduling: own takeoff time, heading and altitude per aircraft | Fix R8 at the base; likely the biggest gain | Daaksh | done: pad-aware takeoffs (`PAD_TAKEOFFS`). Remaining R8 is mostly en route; per-member takeoff slots, layer pairs and wider spacing all lost (README) |
| 3 | Scouts: plan unknown contacts, insert each new visit at its best position in the tour | 10 of 15 scouts idle in L4 | ? | done: insertion in beam children and LNS, 8-scout fallback. 6 first scouts and extra departure shifts lost. Scout-tour phase: cheapest-insertion beam expansion and slack-ranked fanout 6/10 won on tuning but lost on held-out seeds 30–45 (−83.69), shared observation points lost; all reverted (README). Only 3 L4 scout-rule failures remain; most misses are `scout_window` and `outranked` |
| 4 | Polish: swap/drop moves after the search, wait on the ground instead of hovering, fly at best-range speed | Small gains everywhere | ? | open: effectors already back-timed (no hover), scouts stretch legs instead of hovering, cruise at 0.995 v_h. Relocation/2-opt polish of scout orders lost (−48.19 on tuning seeds) and was reverted |
| 5 | Beam: keep diverse states, rank by optimistic bound, allow cheaper non-improving children, sweep width | `outranked` contacts: valid serves cut by the beam | Devin | done: `BEAM_DIVERSE = 3` and width 9 kept (+251 held-out); bound ranking and relaxed children lost or never fired (README) |
| 6 | LNS phase 3: conflict-targeted destroy, whole-tour rebuild, exact reinsertion order, `lns_share` split | Repair is where L4 gains come from | ? | done: kept the size-dependent `lns_share` (0.6 with ≥ 40 hostile+unknown contacts) and the whole-tour rebuild (`tour` 10% of the mix, half of picks rebuild). On beam width 9: held-out 35111.07 → 35415.98 (+304.9, 64/64 valid). `blocker` and exact reinsertion lost and were reverted ([history](history/2026-11-22-phase3.md)) |

## Decisions
- **Single process.** The grader's core count is unknown. `multiprocessing` startup and pickling `Scenario`/`State` would use seconds of the 45 s budget. A spawn failure on the grader would score 0. The expected gain does not justify the risk, so the planner stays single-process.
- **Deadline margin.** Under CPU throttling (1.6–2.6× fewer candidate checks) the slowest held-out L4 case still finished in about 46 s with a valid plan. The 50 s `validate_stop` cutoff stays.

## How we accept a change
1. Run the benchmark on the 8 validation scenarios **and** random new seeds.
2. Merge only if the total score goes up **and** every plan passes the official validator.
3. Don't tune to specific seeds: the final ranking uses unseen scenarios.

## How we work
- One person per task, each on their own branch.
- Quick review before merging into `master`.
- Small docs-only changes may go straight to `master` (pull first).
