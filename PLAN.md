# Team plan: Fleet Orchestration (EDTH Paris)

Deadline: **22 Nov, midday**. Base: Nick's beam-search planner (`planner.py` + `search_planner.py`).

## Rule 1: never score 0
A crash, a timeout (> 60 s) or one broken rule makes that scenario score 0.
Safety comes before cleverness.

## Where we stand
- Greedy baseline on the 8 validation scenarios: **2664.5** total
- Our search planner: *(to be measured)*
- In L4 the baseline wins about 1,000 of about 3,700 available points and uses only 5 of 15 scouts.

## What we found
- **Takeoff collisions.** All aircraft sit on pads ~11 m apart, but separation (R8) needs 40 m.
  Aircraft climbing out at the same time break R8. It is the top reason candidate plans get rejected.
- **Idle scouts.** Unknown contacts need only a scout pass (nearly free points), but most scouts never fly.
- **Cost of a strike.** Each effector costs 10 points plus energy, so a low-value hostile
  needing 3 effectors earns almost nothing. Rank targets by points minus cost.

## Tasks (in order)

| # | Task | Why | Owner | Status |
|---|------|-----|-------|--------|
| 1 | Safety: stop at ~45–50 s (now up to 58 s), build the hand-in folder, run `check_handin.py` on many seeds | Avoid timeouts on a slower grading machine | ? | todo |
| 2 | Takeoff scheduling: own takeoff time, heading and altitude per aircraft | Fix R8 at the base; likely the biggest gain | Daaksh | todo |
| 3 | Scouts: plan unknown contacts, insert each new visit at its best position in the tour | 10 of 15 scouts idle in L4 | ? | todo |
| 4 | Polish: swap/drop moves after the search, wait on the ground instead of hovering, fly at best-range speed | Small gains everywhere | ? | if time |

## How we accept a change
1. Run the benchmark on the 8 validation scenarios **and** random new seeds.
2. Merge only if the total score goes up **and** every plan passes the official validator.
3. Don't tune to specific seeds: the final ranking uses unseen scenarios.

## How we work
- One person per task, each on their own branch.
- Quick review before merging into `master`.
- Small docs-only changes may go straight to `master` (pull first).
