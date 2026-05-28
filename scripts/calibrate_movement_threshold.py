#!/usr/bin/env python3
"""
Calibrate MOVEMENT_THRESHOLD_G2 from a captured IDLE/MOVING Parquet pair.

Usage:
  python calibrate_movement_threshold.py IDLE.parquet MOVING.parquet

Algorithm: threshold = mean(idle_mag2) + 5 * std(idle_mag2)
Outputs:   RECOMMENDED MOVEMENT_THRESHOLD_G2=<value>
"""
import sys

import numpy as np
import pandas as pd


def _mag2(df: pd.DataFrame) -> np.ndarray:
    """Squared acceleration magnitude from accel_x/y/z columns."""
    return (
        df["accel_x"].astype(float) ** 2
        + df["accel_y"].astype(float) ** 2
        + df["accel_z"].astype(float) ** 2
    ).values


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: calibrate_movement_threshold.py IDLE.parquet MOVING.parquet")
        sys.exit(1)

    idle_path, moving_path = sys.argv[1], sys.argv[2]
    idle_df   = pd.read_parquet(idle_path)
    moving_df = pd.read_parquet(moving_path)

    # Filter to the expected state if the column is present.
    if "state" in idle_df.columns:
        idle_df = idle_df[idle_df["state"] == 0]
    if "state" in moving_df.columns:
        moving_df = moving_df[moving_df["state"] == 1]

    idle_mag2   = _mag2(idle_df.dropna(subset=["accel_x", "accel_y", "accel_z"]))
    moving_mag2 = _mag2(moving_df.dropna(subset=["accel_x", "accel_y", "accel_z"]))

    if len(idle_mag2) < 10:
        print(f"ERROR: only {len(idle_mag2)} IDLE samples — need at least 10")
        sys.exit(1)

    threshold = float(idle_mag2.mean() + 5.0 * idle_mag2.std())

    print(f"IDLE  samples : {len(idle_mag2)}")
    print(f"MOVING samples: {len(moving_mag2)}")
    print(f"IDLE  mag2 mean={idle_mag2.mean():.4f}  std={idle_mag2.std():.4f}")
    if len(moving_mag2) > 0:
        sep = (moving_mag2.mean() - idle_mag2.mean()) / (idle_mag2.std() + 1e-9)
        print(f"MOVING mag2 mean={moving_mag2.mean():.4f}  separation={sep:.1f}σ")
    print(f"RECOMMENDED MOVEMENT_THRESHOLD_G2={threshold:.4f}")


if __name__ == "__main__":
    main()
