# Scaling study (30 / 60 / 100 aircraft)

`python3 analysis/scaling_study.py --sizes 30,60,100 --seeds 0-2 --seconds 45`, one planner process at a time, on final master `4b83149` on an 8-core cloud VM (it ran beside one other planner job).
Scenarios scale the L4 generator counts to the requested fleet size; raw rows are in [scaling_study.csv](scaling_study.csv).

| Aircraft | Contacts | Greedy total | Planner total | Gain | Valid | Max planner runtime (s) | Max greedy runtime (s) |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 30 | 27 | 1255.04 | 1616.36 | +28.8% | 3/3 | 45.31 | 4.63 |
| 60 | 54 | 1919.45 | 2909.49 | +51.6% | 3/3 | 45.91 | 15.21 |
| 100 | 90 | 1666.92 | 4280.44 | +156.8% | 3/3 | 45.69 | 6.20 |

The planner's runtime is set by the 45 s deadline, not by fleet size: it was 45.3–45.9 s at every size. All 9 plans pass official validation. In this sample the gain over greedy grows with fleet size. Three seeds per size are a sample, not a guarantee.
