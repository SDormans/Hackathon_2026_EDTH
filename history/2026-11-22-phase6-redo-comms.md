# Phase 6 redo: retention and comms (2026-11-22)

No policy change kept. `policy.py` is unchanged from master (88a4369).

## Setup

`analysis/linkloss_eval.py run` on the 8 validation scenarios with greedy plans, 5 comm seeds,
leaderboard cell (1000 m, 200 B/s) plus the 20-cell sizing grid (2400 runs). Event seeds 1001-1003
(Phase 5), 2001-2003 and 3001-3003 (new, evaluation only). Per-cell CSVs in `analysis/linkloss/p6-*.csv`,
summary rows in `analysis/linkloss/variants.csv`.

## Variants, event seeds 2001-2003

| Variant | LB retention | LB fail | Msgs/ac/min | S_loss | Grid fail | Sizing score |
|---|---:|---:|---:|---:|---:|---:|
| blind continue | 0.0000 | 120/120 | 0.00 | 0.0000 | 2400 | 0.00 |
| return | 0.4036 | 5/120 | 0.00 | 0.4036 | 100 | 0.00 |
| hold | 0.4036 | 0/120 | 0.00 | 0.4036 | 0 | 0.00 |
| safe + V deltas only (`NEWS_MODE="off"`, `O_FORWARD=False`) | 0.9937 | 0/120 | 20.57 | 0.9526 | 0 | 0.75 |
| safe + deltas + news (`policy.py`) | 0.9909 | 0/120 | 15.06 | 0.9608 | 5 | 0.50 |
| + look-ahead in no-clear-move fallback | 0.9905 | 0/120 | 14.73 | 0.9610 | 0 | 0.75 |

Hold and return keep only the points earned before the loss; the 250 m row fails retention (< 0.9)
for every safe variant. `policy.py`'s 5 grid failures are one run (L3-301 e2001 comm 0) in all five
2000 m cells: H04 stops at its engage gate (strike not yet allowed), the on-plan H08 75 m behind on
the same approach hears the stop 3 s later and closes to 25.6 m. In the no-clear-move fallback,
stop and half speed tie over one step (57.3 m) and ties hold.

## Rejected fix: look-ahead in the no-clear-move fallback

Two lines in `_safe_options`: rank fallback options by min(one-step, 3-step look-ahead) distance.
Fixes the run above, but on the other seed sets:

| Event seeds | Policy | LB retention | LB fail | Grid fail | Sizing score |
|---|---|---:|---:|---:|---:|
| 1001-1003 | `policy.py` (Phase 5) | 0.9908 | 0/120 | 0 | 1.00 |
| 1001-1003 | + look-ahead | 0.9908 | 0/120 | 1 | 0.95 |
| 3001-3003 | `policy.py` | 0.9925 | 0/120 | 0 | 1.00 |
| 3001-3003 | + look-ahead | 0.9675 | 3/120 | 41 | 0.20 |

Moving instead of holding when nothing is clear walks into peers that act on the hold
(e.g. L3-301 e3002: L02/S04 at 33 m, 3 of 5 comm seeds at 1000 m, 200 B/s; 0 of 5 on master).
Reverted in this branch (commit + revert).

## Checks

- Deltas only: over 24 runs (e1001-1003, 1000 m / 200 B/s), on-schedule aircraft sent 0 heartbeats:
  1,029 O, 1,701 R, 748 Z news records and 115 V (the step they leave the plan), in 78,171 on-schedule
  steps. Off-schedule aircraft send V every step (15,766); the separation checks need it.
- Effector proceed rule (unchanged): observation ended by `t_loss` per plan -> engage on the plan;
  later -> needs the scout's O report or own observation, else approach and hold short of the engage
  point until `window_end - HOLD_LEAD_S`, then return. Unit-tested in `tests/test_linkloss_policy.py`.
- Handover: not added. A taker deviates from the validated plan, and peers can only learn of it
  through lossy, delayed V messages; the e2001 and e3002 runs show 2-3 s of warning is not enough at
  closing speed. No separation guarantee, so no handover (as in the Phase 6 brief).

## Sizing curve

`analysis/linkloss_sizing.csv` adds `pooled-e1001-3003` rows (`policy.py`, 9 event seeds, 360 runs
per cell). Every cell has retention >= 0.93; 250-1000 m meet the bar down to 50 B/s; 2000 m fails
on the single e2001 run. Minimum viable budget: 50 B/s per receiver at 250-1000 m.

| Range | Retention at 50 / 200 B/s | Failures | Msgs/ac/min | Meets |
|---|---:|---:|---:|---|
| 250 m | 0.9323 / 0.9323 | 0 | 19.3 | yes |
| 500 m | 0.9797 / 0.9801 | 0 | 16.5 | yes |
| 1000 m | 0.9906 / 0.9914 | 0 | 15.8 | yes |
| 2000 m | 0.9934 / 0.9934 | 1 | 14.9 | no |

## Gate

Not met: the policy is not zero-violation on all fresh seeds (1 run in 360 at 2000 m), and no
variant improved retention without violations on every seed set. Planner scores are unaffected
(policy only).
