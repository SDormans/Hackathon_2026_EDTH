# Plan fragility vs link-loss retention (2026-11-22)

Question: do plans with fewer tight scout→effector couplings, fewer near pairs and longer
confirm→strike gaps keep more points after the link is cut? Result: **no**. The tie-breaker
(step 3) was not built.

Setup: `analysis/fragility_correlation.py`, tuning cases L2:0-2, L3:0-2, L4:0-2, L4:15 (none held out).
For each case the planner ran with beam widths 6, 3 and 12 at 20 s, 4 workers. The ranked archive
(`--artifacts`) gave 127 distinct valid plans (5-15 per case). Each plan got its own events
(`make_events(sc, plan, seed + 1)`, as `check_policy.py`) and was scored with `policy.py`
over 5 comm seeds at 1000x200 (LEADERBOARD), 500x100 and 250x50. Correlation is
within-scenario Spearman (ranks centred per case). 95% CI comes from a case bootstrap
(500 resamples).

| Metric vs retention | 1000x200 (LEADERBOARD) | 500x100 | 250x50 |
|---|---:|---:|---:|
| `plan_fragility` (composite) | -0.01 [-0.26, +0.28] | -0.05 [-0.34, +0.30] | -0.05 [-0.35, +0.26] |
| tight couplings (slack < 10 s) | -0.20 [-0.52, +0.17] | -0.09 [-0.29, +0.13] | -0.00 [-0.32, +0.32] |
| near pairs (< d_sep + 20 m) | +0.17 [+0.02, +0.32] | +0.06 [-0.22, +0.39] | -0.01 [-0.29, +0.28] |
| mean confirm→strike gap | -0.05 [-0.30, +0.19] | -0.04 [-0.27, +0.18] | -0.02 [-0.34, +0.27] |
| plan score (reference) | +0.19 [-0.04, +0.43] | +0.11 [-0.12, +0.31] | +0.06 [-0.25, +0.42] |

A sturdier plan would show a negative rho for the first three rows and a positive one for the gap.

- The composite has no correlation at any setting.
- Tight couplings point the expected way only at the leaderboard setting, and the CI spans 0.
- Near pairs point the wrong way: more near pairs come with higher retention.
- PLAN score predicts retention at least as well as any fragility term.

Within-case spread was large, so the cases do discriminate. Examples (retention range at
LEADERBOARD over the archive): L3-1 0.00–1.00, L4-1 0.00–1.00, L4-2 0.40–1.00. Retention depends
on which event set is drawn for a plan and on the policy's R8 behaviour, not on these plan
features.

The official link-loss evaluation uses the organisers' reference plans (`LINKLOSS.md`), so
planner output can't change the official link-loss score in any case.
