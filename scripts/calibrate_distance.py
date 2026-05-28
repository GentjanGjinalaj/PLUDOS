#!/usr/bin/env python3
"""
Calibrate DISTANCE_SCALE_FACTOR from a known-distance Parquet run.

Usage:
  python calibrate_distance.py PARQUET_FILE --distance KNOWN_M [--hpf-window N]

Mirrors the ZUPT integration in data-engine.py _compute_derived() and computes a
global scale factor: DISTANCE_SCALE_FACTOR = known_distance_m / integrated_distance_m.

Apply by multiplying data-engine's distance_m_cum by this factor.
Outputs: RECOMMENDED DISTANCE_SCALE_FACTOR=<value>
"""
import argparse
import math
import sys

import pandas as pd

STATE_MOVING = 1


def _integrate_distance(df: pd.DataFrame, hpf_window: int) -> float:
    """ZUPT integration — mirrors data-engine.py _compute_derived distance block."""
    ax_m  = df.loc[df["state"] == STATE_MOVING, "accel_x"].dropna()
    ay_m  = df.loc[df["state"] == STATE_MOVING, "accel_y"].dropna()
    var_x = float(ax_m.var()) if len(ax_m) > 1 else 0.0
    var_y = float(ay_m.var()) if len(ay_m) > 1 else 0.0
    track_a = (
        df["accel_x"].values.astype(float) if var_x >= var_y
        else df["accel_y"].values.astype(float)
    )

    rm  = pd.Series(track_a).rolling(hpf_window, min_periods=1).mean().values
    hpf = track_a - rm

    # Reconstruct dt_s from the timestamp column (seconds between consecutive packets).
    if "timestamp" in df.columns:
        timestamps = pd.to_datetime(df["timestamp"], utc=True)
    elif "timestamp_ms" in df.columns:
        timestamps = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    else:
        print("ERROR: Parquet must have a 'timestamp' or 'timestamp_ms' column")
        sys.exit(1)
    dt_s = timestamps.diff().dt.total_seconds().fillna(0.1).values

    states = df["state"].values.astype(int)
    d, vel = 0.0, 0.0
    for i in range(len(states)):
        if int(states[i]) == STATE_MOVING:
            h = float(hpf[i])
            if not math.isnan(h):
                vel += h * 9.81 * float(dt_s[i])
                d   += abs(vel) * float(dt_s[i])
        else:
            vel = 0.0   # ZUPT reset
    return d


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("parquet",             help="Parquet file from a known-distance run")
    parser.add_argument("--distance", type=float, required=True, help="Actual run distance in metres")
    parser.add_argument("--hpf-window", type=int, default=20, help="HPF running-mean window in packets (default 20)")
    args = parser.parse_args()

    df = pd.read_parquet(args.parquet)

    if "state" not in df.columns:
        print("ERROR: Parquet file missing 'state' column")
        sys.exit(1)

    n_moving = int((df["state"] == STATE_MOVING).sum())
    if n_moving == 0:
        print("ERROR: no MOVING packets in file — check that file contains a motion run")
        sys.exit(1)

    integrated = _integrate_distance(df, args.hpf_window)
    if integrated < 1e-6:
        print("ERROR: integrated distance is essentially zero — check accel signal quality")
        sys.exit(1)

    scale = args.distance / integrated
    print(f"MOVING packets  : {n_moving}")
    print(f"Known distance  : {args.distance:.3f} m")
    print(f"Integrated dist : {integrated:.3f} m")
    print(f"RECOMMENDED DISTANCE_SCALE_FACTOR={scale:.4f}")


if __name__ == "__main__":
    main()
