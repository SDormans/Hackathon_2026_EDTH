# Scout speed feature flags

Two `planner.py` options that change how scout tours are flown. They affect scout routes only.
Beam search, repair and LNS are unchanged, and LNS uses the new tours automatically: it
rebuilds tours through `scout_tour()` and reinserts visits through `children(..., insert=True)`.

| Option | Values | What it does |
|---|---|---|
| `--scout-speed` | `eco`, `fast`, `hybrid` | Scout cruise speed |
| `--obs-stretch` / `--no-obs-stretch` | on / off | Observation distance at the 70 m layer |

- `eco`: the kit's speed, `min(0.95 · v_h, best-range speed)`. Same routes as before.
- `fast`: every scout leg at `0.98 · v_h`.
- `hybrid`: the kit's speed, switching to `fast` only when the eco tour would miss a window,
  the return home by the horizon (R1) or the battery (R3).
- `--obs-stretch`: observe from as far as the 3D observation range allows at the scout's
  altitude, instead of the kit's fixed 100 m.

**Defaults reproduce the old behaviour:** `--scout-speed eco --no-obs-stretch`. Graders run
`planner.py` without options, so the defaults in `planner.py` decide what gets graded.

```sh
python3 planner.py scenario.json plan.json                                      # old behaviour
python3 planner.py scenario.json plan.json --scout-speed fast --obs-stretch     # faster scouts
python3 planner.py scenario.json plan.json --scout-speed hybrid --obs-stretch
```

The settings used are recorded in the report (`tree.json`, key `"scouting"`).

## Why

On L4 seeds 0–2, about 50 contacts were lost on the scout side (`scout_route`, `scout_rule:R1`,
`scout_rule:R3`). Checking each one against a dedicated idle scout flying straight out and back:

- about 12 cannot be reached by any scout in time or within battery;
- about 12 are reachable only at a higher cruise speed;
- the rest fail inside busy tours on R1 (home by 900 s) or R3 (battery), both driven by airtime.

**Speed.** Energy is mostly `p0 × airtime`, and energy per metre is flat near the best-range
speed. On L4, `0.98 · v_h` is on average 5–13% faster than the kit's speed (up to 39% for some
scouts) for +0–1.5% energy per metre. For scouts whose best-range speed is above `0.95 · v_h`,
the kit's speed is both slower and more expensive.

**Observation distance.** Contacts are at z = 0 and the observation range (150 m) is a 3D
distance. At the 70 m layer the scout can observe from about 125 m away (√(142² − 70²), with an
8 m margin) instead of 100 m. At 110 m the kit's 100 m is already near the limit. If a tour
built with stretched points is rebuilt at 110 m, the points are pulled back into range, never
closer than the kit's 100 m.

## Safety

- `0.98 · v_h` keeps a 2% margin under the R2 speed limit. Climb and descent rates are unchanged.
- Every candidate still goes through the same rule checks, and the final plan through the
  official validator.
- Tests (`python3 -m unittest discover -s tests`):
  - with the default options, scout tours and observation points are identical to the kit's
    (500 random tours);
  - `fast` tours never finish later than `eco` tours, pass R2, R5, R6 and R9, and every
    observation counts, including stretched points flown at 110 m.

## Benchmarking

`analysis/benchmark_search.py` takes a repeatable `--planner-arg` that is passed to the planner
and recorded in `run.json` and on the report's "Source:" line:

```sh
python3 analysis/benchmark_search.py --levels L1,L2,L3,L4 --seeds 15-17 --workers 1 --seconds 45 \
  --output output/benchmarks/s15-scout-default
python3 analysis/benchmark_search.py --levels L1,L2,L3,L4 --seeds 15-17 --workers 1 --seconds 45 \
  --planner-arg=--scout-speed=fast --planner-arg=--obs-stretch --output output/benchmarks/s15-scout-fast
python3 analysis/benchmark_search.py --levels L1,L2,L3,L4 --seeds 15-17 --workers 1 --seconds 45 \
  --planner-arg=--scout-speed=hybrid --planner-arg=--obs-stretch --output output/benchmarks/s15-scout-hybrid
```

## Results so far

On the `scouting` branch, L1–L4 seeds 0–2, 55 s, 4 workers, with `fast` and stretch on:

| | master | scouting patch | + faster scouts |
|---|---:|---:|---:|
| Total | 5052.85 | 5751.72 | 5984.27 |
| L4 | 2318.87 | 2985.79 | 3201.16 |

All 12 plans were valid. On L4, `scout_route` misses dropped from 31 to 16 contacts; R3 and R1
rose slightly (9 → 13, 7 → 11) as contacts now get further before failing.

Still to check:

- held-out seeds with `--workers 1`; a single L4 case varies by up to ~15% between runs;
- runtime: the slowest run took 57.15 s (55.85 s before) with 4 workers; recheck with one worker
  and lower `--seconds` if it stays above 56 s;
- `hybrid` against `fast`; it has not been benchmarked at full budget.