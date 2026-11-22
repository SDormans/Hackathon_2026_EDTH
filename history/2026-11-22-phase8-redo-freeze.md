# Phase 8 redo: final checks (2026-11-22)

Planner at master `88a4369` (Phase 3 beam bound merged; Phase 6 made no code change).

## Final held-out batch

`python3 test_logger.py run --seeds 30-45 --seconds 45 --workers 1` (L1-L4 x 30-45, 64 cases).

| Level | Greedy | Ours | Gain | W/T/L | Valid |
|---|---:|---:|---:|---:|---:|
| L1 | 1322.35 | 1566.38 | +18.5 % | 16/0/0 | 16/16 |
| L2 | 3574.34 | 4076.59 | +14.1 % | 16/0/0 | 16/16 |
| L3 | 6160.20 | 8294.00 | +34.6 % | 16/0/0 | 16/16 |
| L4 | 7977.91 | 21331.69 | +167.4 % | 16/0/0 | 16/16 |
| Total | 19034.80 | 35268.66 | +85.3 % | 64/0/0 | 64/64 |

0 invalid, 0 failures, slowest planner run 45.84 s (gate < 58 s). For reference, the Phase 2
held-out reference was 35115.51 and Phase 3's paired real-clock runs gave 34993.14 (master) vs
35020.58 (bound); real-clock totals move by about 0.5 % with CPU load.

Unserved contacts: collateral_ban 225 (15766 pts), outranked 129 (6790), scout_window 103 (5412,
100 on L4), no_team 24 (1241), time_cap 16 (744), strike_route 3 (222).

## Clean copy

The 14 hand-in files copied into one flat temp folder, `env -i` with only the official kit on
`PYTHONPATH`, `planner.py <scenario> <plan> --scout-speed fast --obs-stretch` (the
`submit_to_board.py` call):

| Scenario | Exit | Wall s | Valid | Score |
|---|---:|---:|---|---:|
| L1-101 | 0 | 2.54 | valid | 72.05 |
| L3-301 | 0 | 45.27 | valid | 725.14 |
| L4-401 | 0 | 45.56 | valid | 1613.86 |
| L4-402 | 0 | 45.55 | valid | 1177.92 |

`load_policy('policy.py')` loads from the same folder.

## Board submission

Not done. No token was provided, and neither board server answers from the VM (the default
trycloudflare tunnel returns no response; `192.168.2.19:8080` is on the event LAN).

## Gates

| Gate | State |
|---|---|
| Held-out 0 invalid, max < 58 s | met (0, 45.84 s) |
| Clean copy with board flags | met |
| Link-loss 0 violations on all event seeds | not met: 1 run in 360 at 2000 m (L3-301 e2001), see Phase 6 |
| Board vs local comparison | not done (no token, server unreachable) |

Not tagged: the freeze tag waits for the open gates and the merges.
