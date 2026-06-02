# Pose Realtime Experiments

This directory is copy-branch-only experiment code for evaluating low-latency
pose extraction alternatives without touching the main portable pipeline.

Current experiment:

- `benchmark_oracle_crop_pose.py`
  - builds "oracle" per-frame fencer boxes from the current low-latency
    YOLO26x jump-safe tracker
  - runs a candidate single-person pose backend on those crops
  - reports runtime and keypoint error against the oracle tracks

The goal is to answer:

- how fast can a crop-based pose model run on the Jetson Orin Nano
- whether wrists/elbows/knees/ankles stay accurate enough for referee logic

