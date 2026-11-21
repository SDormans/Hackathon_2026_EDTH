# Scouting Bottleneck — Findings & Improvement Plan  
  
**Repo:** `SDormans/Hackathon_2026_EDTH` (branch `scouting`)  
**Date:** 2026-11-21  
**Source data:** `analysis/results/l4-unserved-diagnosis/` (L4, seeds 0–2, 55 s, one worker)  
  
## 1. Impact summary  
  
L4 baseline: **1798.76 pts** vs greedy 1354.26. Of **154 missed targets worth 8,542 pts**, scout-related failures are the largest single lever:  
  
| Reason | Contacts | Points | Category |  
|---|---|---|---|  
| `scout_route` | 46 | 2,665 | Scout construction |  
| `collateral_ban` | 36 | 2,460 | Unavailable (banned) |  
| `outranked` | 42 | 2,117 | Beam pruning |  
| `scout_rule:R1` | 21 | 775 | Scout construction |  
| `time_cap` | 3 | 209 | Time limit |  
| `scout_rule:R3` | 3 | 163 | Scout construction |  
| `scout_rule:R8` | 3 | 153 | Scout construction |  
  
- **Scout failures total: 73 contacts / ~3,756 pts** — more than `outranked` and `time_cap` combined.  
- `collateral_ban` points are unreachable by any plan; they inflate "available points" totals.  
- A direct-flight check (`python3 analysis/scout_reach.py analysis/results/l4-unserved-diagnosis`) shows only **8 non-banned targets** across all three scenarios are physically unreachable even flying straight from base at full speed, ignoring zones.  
- **Conclusion:** losses come from how tours are built, not from physics.  
  
## 2. Root causes in the code  
  
### 2.1 Append-only candidate generation (main search)  
`Search.slots()` in `search_planner.py` (~L288–295): during beam search (`insert=False`) it only tries **appending** the new observation to the end of the **4 nearest scouts'** tours. If the tour tail doesn't fit the contact's window, the attempt fails as `scout_route` — even though inserting the stop earlier in the tour might succeed.  
  
### 2.2 Insertion exists but only in repair  
The `insert=True` path (~L296–304) ranks **every insertion position of every scout** by added distance and tries the top 8 — but it is only used by the end-of-run repair pass, not during the main search where most contacts are decided.  
  
### 2.3 Fixed tour order  
`_scout_route` in `fleetkit/baseline.py` (~L102–117) visits tasks strictly in list order. There is no re-sequencing of an existing tour to make room for a new stop, so greedy append order can paint itself into a corner.  
  
### 2.4 Full route rebuild per candidate  
`Search.observation()` (~L306–313) rebuilds the entire scout route (including `_detour` zone checks) for every candidate. `children()` (~L353–355) iterates slots × 2 altitude layers × 3 observation angles × 2 departure shifts → up to **48 route builds per slot** before validation even runs.  
  
### 2.5 Hardcoded scout fan-out  
`scouts[:4]` is fixed; when all 4 fail there is no fallback to more distant scouts, even for high-value contacts.  
  
## 3. Recommended improvements (by expected impact)  
  
| # | Change | Targets | Expected effect |  
|---|---|---|---|  
| 1 | Use insertion-ranked slots in the main search (append first, retry with `insert=True` on `scout_route`/`scout_rule` failure) | `scout_route` (2,665), `scout_rule:R1` (775) | Largest single win; reuses existing repair logic |  
| 2 | Cheap reachability pre-filter before `_scout_route`: earliest arrival from previous stop at max speed + climb + `obs_dwell_s` ≤ window end; also direct-from-home check (port `analysis/scout_reach.py` logic) | Runtime / all scout failures | Skips most of the 48 builds per slot → more beam width or deeper search in the same 55 s |  
| 3 | Tour reordering on failure: swap new task with each existing position / small 2-opt for short tours | `scout_route`, `scout_rule:R3/R8` | Recovers order-dependent failures |  
| 4 | Memoize `_scout_route` keyed on (scout id, task-point tuple, depart bucket, z) | Runtime | Avoids rebuilding identical tours across sibling branches |  
| 5 | Fallback beyond top-4 scouts (e.g., up to 8) when all fail | `scout_route` | Cheap recovery for high-value contacts |  
| 6 | After scout fixes: widen beam on later passes or retry `outranked` contacts in repair with freed scout capacity | `outranked` (2,117) | Second-largest lever |  
  
## 4. Expected outcome  
  
~3,700 pts are currently lost to scout construction while only ~8 targets are truly unreachable. Items 1–3 address the bulk of that gap; items 2 and 4 also free runtime that can be converted into beam width (indirectly reducing `outranked`).  
  
## 5. Verification  
  
- Unit tests: `python3 -m unittest discover -s tests -v`  
- Benchmark: `python3 analysis/benchmark_search.py --levels L4 --seeds 0-2 --workers 1 --seconds 55`  
- Compare `scout_route` / `scout_rule:*` unserved counts and total score against `analysis/results/l4-unserved-diagnosis/`.  
- Official validation path (IncrementalValidator + final full validation) must remain unchanged.  
