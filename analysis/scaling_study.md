# Scaling study (30 / 60 / 100 aircraft)

`python3 analysis/scaling_study.py --sizes 30,60,100 --seeds 0-2 --seconds 45`, one planner process at a time, on master `0acfa7c` on an 8-core cloud VM (it ran beside one other planner job).
Scenarios scale the L4 generator counts to the requested fleet size; raw rows are in [scaling_study.csv](scaling_study.csv).

| Aircraft | Contacts | Greedy total | Planner total | Gain | Valid | Max planner runtime (s) | Max greedy runtime (s) |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 30 | 27 | 1255.04 | 1609.72 | +28.3% | 3/3 | 45.34 | 4.56 |
| 60 | 54 | 1919.45 | 2913.31 | +51.8% | 3/3 | 46.01 | 13.38 |
| 100 | 90 | 1666.92 | 4321.04 | +159.2% | 3/3 | 45.88 | 6.18 |

The planner's runtime is set by the 45 s deadline, not by fleet size: it was 45.3–46.0 s at every size, and one 30-aircraft case finished early at 27 s. All 9 plans pass official validation. In this sample the gain over greedy grows with fleet size. Three seeds per size are a sample, not a guarantee.
