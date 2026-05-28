"""
T8.4 — Standalone vs Federated: Time-to-Detect a Seeded Fault
Measures how many missions (and elapsed time) each mode needs to first flag
an anomalous shuttle after a physical fault is introduced.

Procedure:
  1. Start one Jetson in standalone mode, one in federated mode (or use one
     Jetson for both modes in sequence with the same fault session).
  2. Seed a fault (e.g. loosen a roller, add a washer to create vibration).
  3. Run the shuttle. Each mission triggers an FL/local-inference round.
  4. After the session, run this script against the two Parquet directories.

This script is a post-hoc analyser — it does NOT drive the hardware.
It reads anomaly_label (CNN or IF) columns from Parquet and finds the
first mission where the positive rate exceeds the detection threshold.

Usage:
  python3 scripts/experiments/t8_4_fault_detection.py \\
      --standalone /path/to/standalone/parquet/ \\
      --federated  /path/to/federated/parquet/  \\
      --fault-time "2026-05-28T14:00:00"        # when fault was introduced
      [--out t8_4_fault_detection.png]
"""
import argparse
import os
import sys

DETECTION_THRESHOLD = 0.10  # ≥10 % positive rate in a mission = "detected"


def _load_missions(parquet_dir):
    """Load all Parquet files in a directory, return list of (flush_time, df) sorted by time."""
    import pandas as pd
    import glob

    files = sorted(glob.glob(os.path.join(parquet_dir, "*.parquet")))
    if not files:
        print(f"No Parquet files in {parquet_dir}")
        return []
    missions = []
    for f in files:
        try:
            df = pd.read_parquet(f)
            moving = df[df["state"] == 1] if "state" in df.columns else df
            if len(moving) < 10:
                continue
            mtime = os.path.getmtime(f)
            missions.append((mtime, moving))
        except Exception as exc:
            print(f"  Skip {f}: {exc}")
    missions.sort(key=lambda x: x[0])
    return missions


def _positive_rate(df):
    """Fraction of MOVING rows labelled anomalous (0.0 if no label column)."""
    for col in ("anomaly_label", "anomaly"):
        if col in df.columns:
            return float(df[col].mean())
    return float("nan")


def _find_detection(missions, fault_ts):
    """Return (mission_index, elapsed_s) of first mission after fault with rate >= threshold."""
    import datetime
    fault_dt = datetime.datetime.fromisoformat(fault_ts).timestamp()
    post_fault = [(t, df) for t, df in missions if t >= fault_dt]
    for idx, (mtime, df) in enumerate(post_fault):
        rate = _positive_rate(df)
        if rate >= DETECTION_THRESHOLD:
            elapsed = mtime - fault_dt
            return idx + 1, elapsed, rate
    return None, None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--standalone",  required=True)
    ap.add_argument("--federated",   required=True)
    ap.add_argument("--fault-time",  required=True, help="ISO8601 timestamp when fault was seeded")
    ap.add_argument("--out",         default="t8_4_fault_detection.png")
    args = ap.parse_args()

    print(f"Loading standalone missions from {args.standalone}...")
    sa_missions = _load_missions(args.standalone)
    print(f"Loading federated missions from {args.federated}...")
    fd_missions = _load_missions(args.federated)

    results = {}
    for label, missions in [("Standalone", sa_missions), ("Federated", fd_missions)]:
        idx, elapsed, rate = _find_detection(missions, args.fault_time)
        if idx is None:
            print(f"{label}: fault NOT detected in {len(missions)} post-fault missions.")
            results[label] = {"missions_to_detect": None, "elapsed_s": None, "rate": None}
        else:
            print(f"{label}: detected in mission {idx}, {elapsed:.0f}s after fault (rate={rate:.2%})")
            results[label] = {"missions_to_detect": idx, "elapsed_s": elapsed, "rate": rate}

    try:
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(8, 5))
        labels = list(results.keys())
        missions_vals = [results[l]["missions_to_detect"] or 0 for l in labels]
        bars = ax.bar(labels, missions_vals, color=["steelblue", "darkorange"], alpha=0.8)
        ax.set_ylabel("Missions to detect fault")
        ax.set_title("T8.4 — Standalone vs Federated: Time-to-Detect")
        for bar, v in zip(bars, missions_vals):
            if v:
                elapsed = results[labels[bars.index(bar)]]["elapsed_s"]
                ax.text(bar.get_x() + bar.get_width()/2, v + 0.05, f"{elapsed:.0f}s",
                        ha="center", va="bottom", fontsize=10)
        plt.tight_layout()
        plt.savefig(args.out, dpi=150)
        print(f"Saved: {args.out}")
    except ImportError:
        print("matplotlib not installed — table only")


if __name__ == "__main__":
    main()
