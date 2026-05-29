"""
PLUDOS anomaly labelling — pure inference module, no Flower dependency.

Exports:
  load_buffered_data()   — load + concat recent Parquet files; return (X, y, labeller)
  label_packets(df)      — standalone shim: label a pre-loaded DataFrame
  _make_anomaly_labels() — dispatch to active backend (ANOMALY_MODEL)
  _make_anomaly_labels_if() — IsolationForest path; also CNN fallback target

Standalone use:
  from anomaly import label_packets

Federated use (client.py):
  from anomaly import load_buffered_data, _make_anomaly_labels
"""

import logging
import os
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config — same env vars as client.py; read independently so this module
# has no import dependency on client.py (and no flwr import).
# ---------------------------------------------------------------------------

TEST_MODE = os.getenv("TEST_MODE") == "1"
BUFFER_DIR = os.getenv("BUFFER_DIR", "./ram_buffer" if TEST_MODE else "/app/ram_buffer")

MAX_PARQUET_FILES = int(os.getenv("MAX_PARQUET_FILES", "20"))

# Anomaly model selector — controls pseudo-label generation for XGBoost training.
ANOMALY_MODEL = os.getenv("ANOMALY_MODEL", "isolation_forest_xgb")

# Faults on these shuttles are rare — keep the assumed anomaly fraction low so
# IsolationForest does not over-flag normal MOVING vibration. Tune via env.
IF_CONTAMINATION     = float(os.getenv("IF_CONTAMINATION",    "0.02"))
IF_MIN_MOVING_SAMPLES = int(os.getenv("IF_MIN_MOVING_SAMPLES", "50"))

# accel_z threshold — legacy backend only (ANOMALY_MODEL=threshold). Must match .env.example.
ANOMALY_THRESHOLD_G = float(os.getenv("ANOMALY_THRESHOLD_G", "0.8"))

# Persisted CNN state (T3.1/T3.2); must survive container restarts.
STATE_DIR = Path(os.getenv("STATE_DIR", "./state" if TEST_MODE else "/app/state"))

# ---------------------------------------------------------------------------
# IsolationForest feature set — vibration and shock channels only.
# IDLE packets are excluded from the IF fit (no bearing load at rest).
# accel_mag, gyro_mag and rolling_accel_std_10 are derived at train time by
# _derive_features (the gateway stores raw signal only — store-raw, derive-here).
# ---------------------------------------------------------------------------

_IF_FEATURES = [
    "accel_mag",            # total shock magnitude
    "rolling_accel_std_10", # sustained vibration (bearing wear signature)
    "gyro_x",               # torsional vibration (motor/bearing)
    "gyro_mag",             # overall rotation magnitude
    "accel_z",              # vertical channel — floor surface + bearing noise
]

# Raw columns persisted by the gateway (must match data-engine.py _PARQUET_COLS).
_RAW_FEATURES = [
    "accel_x", "accel_y", "accel_z",
    "gyro_x", "gyro_y", "gyro_z",
    "temp_c", "humidity_pct",
    "seq_gap", "state",
]


def _derive_features(df: pd.DataFrame) -> pd.DataFrame:
    """Compute engineered features from raw signal at train time (store-raw pattern).

    The gateway persists only raw samples; classic ML (IsolationForest, XGBoost)
    needs hand-engineered features, so they are computed here once per FL round.
    The CNN path consumes raw axes directly and ignores these columns.
    Mutates and returns df with accel_mag, gyro_mag and rolling_accel_std_10 added.
    """
    df["accel_mag"] = (df["accel_x"]**2 + df["accel_y"]**2 + df["accel_z"]**2).pow(0.5)
    df["gyro_mag"]  = (df["gyro_x"]**2  + df["gyro_y"]**2  + df["gyro_z"]**2).pow(0.5)
    # 1-second rolling std (10 packets at 10 Hz MOVING) — surface/vibration proxy.
    # min_periods=2 (std needs ≥2 points); fill the leading NaN with 0.
    df["rolling_accel_std_10"] = (
        df["accel_mag"].rolling(10, min_periods=2).std().fillna(0.0)
    )
    return df


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_buffered_data() -> tuple[np.ndarray, np.ndarray, str]:
    """
    Load and concatenate recent mission Parquet files from the RAM buffer.

    Loads up to MAX_PARQUET_FILES most recent files. Returns (X, y, labeller_name).
    """
    logger.info("Scanning RAM buffer for telemetry Parquet files...")

    if not os.path.exists(BUFFER_DIR):
        raise FileNotFoundError(f"CRITICAL: Buffer directory {BUFFER_DIR} not found.")

    files = sorted(
        [f for f in os.listdir(BUFFER_DIR) if f.endswith(".parquet")],
        key=lambda f: os.path.getmtime(os.path.join(BUFFER_DIR, f)),
    )
    if not files:
        raise FileNotFoundError(
            "CRITICAL: No Parquet files found in buffer. "
            "Ensure at least one shuttle mission has completed before triggering an FL round."
        )

    recent = files[-MAX_PARQUET_FILES:]
    frames = [pd.read_parquet(os.path.join(BUFFER_DIR, f)) for f in recent]
    df = pd.concat(frames, ignore_index=True)
    logger.info(
        "Loaded %d samples from %d Parquet file(s): %s … %s",
        len(df), len(recent), recent[0], recent[-1],
    )

    # Backfill any missing raw column with NaN so old/partial Parquet files still load.
    for col in _RAW_FEATURES:
        if col not in df.columns:
            df[col] = np.nan

    # Sort by (shuttle_id, seq) so the rolling-window derivation is time-ordered.
    if "shuttle_id" in df.columns and "seq" in df.columns:
        df = df.sort_values(["shuttle_id", "seq"]).reset_index(drop=True)

    # store-raw, derive-at-train-time: build engineered features from raw signal.
    df = _derive_features(df)

    # Feature matrix = raw signal + train-time-derived features. The CNN labeller
    # reads the raw axes directly; IsolationForest/XGBoost use the derived columns.
    feature_cols = _RAW_FEATURES + ["accel_mag", "gyro_mag", "rolling_accel_std_10"]
    df_clean = df[feature_cols].dropna()
    X_train  = df_clean.values

    # T3.4: decide labeller once per round; log it explicitly.
    y_train, labeller = _make_anomaly_labels(df_clean)
    logger.info("[ANOMALY] labeller=%s for this round (T3.4)", labeller)
    return X_train, y_train, labeller


# ---------------------------------------------------------------------------
# Anomaly labellers
# ---------------------------------------------------------------------------

def _make_anomaly_labels(df_clean: pd.DataFrame) -> tuple[np.ndarray, str]:
    """
    Dispatch to the active anomaly labelling backend (ANOMALY_MODEL env var).

    cnn_autoencoder      — 1D-CNN reconstruction error (T3.3); falls back to IF
    isolation_forest_xgb — per-sample IsolationForest (default)
    threshold            — legacy accel_z > ANOMALY_THRESHOLD_G rule
    """
    if ANOMALY_MODEL == "cnn_autoencoder":
        from anomaly_cnn import make_anomaly_labels_cnn
        return make_anomaly_labels_cnn(
            df_clean, STATE_DIR, _make_anomaly_labels_if, IF_CONTAMINATION
        )
    if ANOMALY_MODEL == "threshold":
        return (df_clean["accel_z"] > ANOMALY_THRESHOLD_G).astype(int).values, "threshold"
    return _make_anomaly_labels_if(df_clean)


def _make_anomaly_labels_if(df_clean: pd.DataFrame) -> tuple[np.ndarray, str]:
    """IsolationForest path; also serves as fallback from CNN."""
    y = np.zeros(len(df_clean), dtype=int)
    moving_mask = df_clean["state"].astype(int) == 1
    n_moving = moving_mask.sum()

    if n_moving < IF_MIN_MOVING_SAMPLES:
        logger.warning(
            "[ANOMALY] only %d MOVING samples (need %d) — falling back to threshold label",
            n_moving, IF_MIN_MOVING_SAMPLES,
        )
        return (df_clean["accel_z"] > ANOMALY_THRESHOLD_G).astype(int).values, "threshold"

    available = [c for c in _IF_FEATURES if c in df_clean.columns]
    X_moving = df_clean.loc[moving_mask, available].values

    iso = IsolationForest(
        contamination=IF_CONTAMINATION,
        n_estimators=100,
        random_state=42,
        n_jobs=-1,
    )
    moving_labels = (iso.fit_predict(X_moving) == -1).astype(int)
    y[moving_mask.values] = moving_labels

    n_anomalous = moving_labels.sum()
    logger.info(
        "[ANOMALY] IsolationForest: %d/%d MOVING packets flagged anomalous (%.1f%%)",
        n_anomalous, n_moving, 100.0 * n_anomalous / n_moving,
    )
    return y, "isolation_forest_xgb"


def label_packets(df_clean: pd.DataFrame) -> np.ndarray:
    """
    Standalone shim: label a pre-loaded, pre-cleaned DataFrame.
    Returns integer labels (0=normal, 1=anomaly). No Flower, no InfluxDB.
    """
    labels, _ = _make_anomaly_labels(df_clean)
    return labels
