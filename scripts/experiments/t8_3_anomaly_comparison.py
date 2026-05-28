"""
T8.3 — Anomaly Detection Comparison
CNN-AE vs IsolationForest vs ground truth (precision / recall / F1 table).

Requires a Parquet file that has:
  - Standard PLUDOS columns (accel_x/y/z, gyro_x/y/z, state, ...)
  - A 'ground_truth' column (int: 1 = anomalous packet, 0 = normal)
    This column must be added manually by annotating a real fault session.

Usage:
  python3 scripts/experiments/t8_3_anomaly_comparison.py \\
      --parquet /path/to/annotated.parquet \\
      [--state-dir /path/to/state]   # for Welford stats (optional)
      [--out comparison_table.csv]

Both backends are run on the MOVING rows only (state == 1).
"""
import argparse
import os
import sys


def _load_and_validate(parquet_path):
    try:
        import pandas as pd
    except ImportError:
        print("pandas not installed")
        sys.exit(1)

    df = pd.read_parquet(parquet_path)
    required = {"accel_x", "accel_y", "accel_z", "gyro_x", "gyro_y", "gyro_z", "state", "ground_truth"}
    missing = required - set(df.columns)
    if missing:
        print(f"Parquet missing columns: {missing}")
        print("Add a 'ground_truth' column (0=normal, 1=anomaly) from manual annotation.")
        sys.exit(1)

    moving = df[df["state"] == 1].copy()
    if len(moving) < 50:
        print(f"Only {len(moving)} MOVING rows — need at least 50.")
        sys.exit(1)

    print(f"Loaded {len(df)} rows, {len(moving)} MOVING, {moving['ground_truth'].sum()} anomalous.")
    return moving


def _run_isolation_forest(df):
    """IsolationForest labels — same logic as anomaly.py."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "client"))
    from anomaly import make_anomaly_labels_isolation_forest

    labels = make_anomaly_labels_isolation_forest(df)
    return labels.astype(int)


def _run_cnn(df, state_dir):
    """CNN autoencoder labels — same logic as client.py (T3.3)."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "client"))
    from anomaly_cnn import make_anomaly_labels_cnn

    labels = make_anomaly_labels_cnn(df, state_dir=state_dir)
    return labels.astype(int)


def _metrics(y_true, y_pred, name):
    from sklearn.metrics import precision_score, recall_score, f1_score, roc_auc_score
    tp = int(((y_pred == 1) & (y_true == 1)).sum())
    fp = int(((y_pred == 1) & (y_true == 0)).sum())
    fn = int(((y_pred == 0) & (y_true == 1)).sum())
    tn = int(((y_pred == 0) & (y_true == 0)).sum())
    prec = precision_score(y_true, y_pred, zero_division=0)
    rec  = recall_score(y_true, y_pred, zero_division=0)
    f1   = f1_score(y_true, y_pred, zero_division=0)
    try:
        auc = roc_auc_score(y_true, y_pred)
    except Exception:
        auc = float("nan")
    return {"Detector": name, "TP": tp, "FP": fp, "FN": fn, "TN": tn,
            "Precision": round(prec, 3), "Recall": round(rec, 3),
            "F1": round(f1, 3), "AUC": round(auc, 3)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parquet",   required=True, help="annotated Parquet file with ground_truth column")
    ap.add_argument("--state-dir", default="client/state", help="Welford stats directory")
    ap.add_argument("--out",       default="t8_3_comparison.csv")
    args = ap.parse_args()

    import pandas as pd

    df = _load_and_validate(args.parquet)
    y_true = df["ground_truth"].values

    results = []

    print("Running IsolationForest...")
    try:
        y_if = _run_isolation_forest(df)
        results.append(_metrics(y_true, y_if, "IsolationForest"))
    except Exception as exc:
        print(f"  IsolationForest failed: {exc}")

    print("Running CNN autoencoder...")
    try:
        y_cnn = _run_cnn(df, args.state_dir)
        results.append(_metrics(y_true, y_cnn, "CNN-AE"))
    except Exception as exc:
        print(f"  CNN-AE failed: {exc}")

    if not results:
        print("All detectors failed.")
        sys.exit(1)

    tbl = pd.DataFrame(results)
    print("\n" + tbl.to_string(index=False))
    tbl.to_csv(args.out, index=False)
    print(f"\nSaved: {args.out}")


if __name__ == "__main__":
    main()
