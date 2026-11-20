# Analysis: what the problem is, and a feasibility matrix that holds up

Notes from a first pass over the brief and the official fleet-kit, so the whole team starts from the same picture.

## 1. What we are actually building

`planner.py`: reads `scenario.json` and writes `plan.json`, which gives every aircraft we use a list of timed waypoints with `observe` / `engage` actions. The organisers run it on **unseen** scenarios (L1 to L4, up to 100 aircraft), **60 s per run**, standard library only. The ranking is the **sum of raw scores**, with ties broken by run time. Hand-in deadline: **22 Nov, midday**.

```
score = Σ points of served contacts − 0.5 · energy (Wh) − 10 · effectors consumed
```

**Any rule violation makes the whole scenario score 0.** Being valid comes first, being clever second.

- A **hostile** contact is served when a scout observes it (20 s dwell within 150 m), and then, at least 10 s later and inside its time window, **exactly** the `engage_needs` mix of light and heavy effectors strikes it, all within 20 s of each other.
- An **unknown** contact is served by the scout observation alone.
- **Friendly and civilian** contacts give no points: keep 100 m away from them, and never strike a hostile within 200 m of one (R7).
- Effectors are one-way. Scouts must land back home.

The scenario is complete and exact: nothing is hidden or random. The brief's part 2 (link-loss policy) needs a `fleetkit.linkloss` package that **has not shipped**: `cli.py` checks for it and says "ships separately".

## 2. It is an allocation and scheduling problem, not pathfinding

Legs are straight lines. Zones are cylinders you can usually fly over (ceilings of about 56–68 m, while the altitude cap is 150 m). The hard part is **who serves what, in which order, and when**: a team orienteering problem with time windows, precedence (scout before strike) and synchronisation (multi-effector strikes).

## 3. Which resource is scarce depends on the level

Averaged over 10 seeds per level. "Need" is the total `engage_needs` across hostiles (an upper bound, because some hostiles are collateral-banned or unreachable):

| Level | Scouts | Light: have / need | Heavy: have / need | Hostile + unknown |
|---|---|---|---|---|
| L1 | 2 | 2 / 3.2 | 1 / 1.3 | 3 + 1 |
| L2 | 3 | 6 / 6.4 | 3 / 3.6 | 6 + 3 |
| L3 | 5 | **16 / 24.6** | 9 / 6.1 | 14 + 6 |
| L4 | 15 | **50 / 87.5** | 35 / 26.4 | 50 + 20 |

The types **can't be swapped**: R10 wants the exact mix (`validator.py`, `_r10`), so a spare heavy can't fill a light slot. At L3/L4, **lights are the bottleneck**. Scout time is the other hidden constraint, because every strike needs a timely confirmation first.

## 4. The effector × contact matrix (`matrix.py`)

```sh
python3 analysis/matrix.py path/to/scenario.json
```

Rows are hostile contacts and columns are effectors. Each cell is the cost of that one pairing *in isolation*: climb to 40 m, fly straight from base to the contact at best-range speed, `0.5·Wh + 10`. A cell shows one of:

- a number: feasible, at that cost
- `--`: can't arrive before the window closes, or not enough battery
- `X`: wrong effector type for this contact
- `BAN`: the contact is collateral-banned (R7)

On `examples/L2-7.json` the net values (points − cheapest cost) make the heavy allocation easy to read: C03 + C02 + C04 = 158.5 beats C03 + C06 = 111, so the baseline was **right** to skip C06 there.

**What the matrix ignores:** scouts, separation, zone detours, interactions between cells (an effector is used once). It's the **menu of options**, not a plan.

**Energy:** in selection, a strike's energy is worth about 1.5–7 points, against contacts worth 42–84, so it rarely changes a choice. In the score, it's about 17% of the baseline's total on L2-7, so it's worth minimising once the selection is fixed (best-range speed, shorter scout tours).

## 5. Is the matrix sound? (`check_matrix.py`)

```sh
python3 analysis/check_matrix.py L2:0-4 L3:0-2
```

For every matrix cell, the script builds a real isolated mini-plan (the best scout observes, this effector plus the nearest partners strike) using the baseline's route builders, and runs it through the **official validator**. Over 884 cells on 8 scenarios:

| Matrix said | Validator found a valid plan? | Count |
|---|---|---|
| feasible | yes | 671 (76%) |
| feasible | no | 80 (9%) |
| **infeasible** | **yes** | **0** |
| infeasible | no | 133 (15%) |

- **No false exclusions were observed in this sample.** The isolated-route checker is itself a heuristic, so this is not proof that every matrix rejection is sound. The planner uses estimated energy/time to rank candidates, not to exclude them.
- **It's slightly optimistic:** 100% right at L2, about 87% at L3. The misses were:
  - **64 on separation (R8).** These are 3–4-effector strikes where the test harness didn't deconflict hard enough. They're fixable, not unreachable. The first run had 246 of these; adding altitude-layer rotation cleared most of them.
  - **16 because no scout can confirm in time.** This is the matrix's **one real gap**. Next step: add a scout check per contact.
- **Its energy estimate is good:** real / estimate has a median of 1.03 and a p90 of about 1.28 (the high cases are zone detours).

## 6. Proposed approach: GRASP (greedy randomised adaptive search)

1. Build the matrix once (milliseconds), and add the scout-confirmation check.
2. **Randomised greedy:** at each step pick one of the top-k contacts by value per scarce effector (not always #1), build real routes for it, accept it if it stays valid.
3. **Adaptive:** after each pick, remove the used effectors, and move the assigned scout to that contact's position, time and remaining battery. Effectors never move before they're used, so only scout rows change.
4. Repeat until about 55 s, keep the best valid plan, and always start from the baseline's plan as the fallback.
5. Later, local search on the best plan: swap effectors, drop and re-add contacts, slow aircraft down to save energy.

**Speed is the blocker.** One baseline greedy run takes 0.23 s at L2, 1.5 s at L3 and **6.6 s at L4**: only about 8 random restarts at L4. The cause is that it re-validates the **whole** plan after every insertion, and R8 compares every pair of aircraft. **First engineering task:** incremental checking (only the new routes against the accepted ones), with one full official validation at the end as a safety net.

## 7. Implemented search planner

The executable planner is now at [`../planner.py`](../planner.py), with usage and design details in [`../README.md`](../README.md). It uses the matrix estimates for ranking and the baseline route builders to grow a bounded beam tree, maintaining a separate archive of the best N distinct timed plans. Skip branches preserve resources for later contacts.

Incremental validation runs the official local rules on changed routes, rechecks R4/R10 on the combined plan, and runs the official R8 pair checker for every pair involving a changed route. The winners still receive a full official validation. The default search stops at 55 seconds from startup, leaving room inside the 60-second limit to validate and write the result.

`check_matrix.py` remains an isolated-mission diagnostic. A successful cell does not establish compatibility with other contacts' aircraft.
