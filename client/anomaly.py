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
import re
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

# Single source of truth for the ISM330 ±2 g LSB scale (DS13281); drain captures
# store raw int16 and are converted to g here.
from drain_receiver import ACCEL_G_PER_LSB

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config — same env vars as client.py; read independently so this module
# has no import dependency on client.py (and no flwr import).
# ---------------------------------------------------------------------------

TEST_MODE = os.getenv("TEST_MODE") == "1"
BUFFER_DIR = os.getenv("BUFFER_DIR", "./ram_buffer" if TEST_MODE else "/app/ram_buffer")

MAX_PARQUET_FILES = int(os.getenv("MAX_PARQUET_FILES", "20"))

# Anomaly model selector — controls pseudo-label generation for XGBoost training.
# Default is the 1D-CNN autoencoder; it falls back to IsolationForest when torch
# is unavailable or there are too few MOVING samples (see _make_anomaly_labels).
ANOMALY_MODEL = os.getenv("ANOMALY_MODEL", "cnn_autoencoder")

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
    # Rolling std over 10 packets (≈0.2 s at 50 Hz MOVING) — surface/vibration proxy.
    # min_periods=2 (std needs ≥2 points); fill the leading NaN with 0.
    df["rolling_accel_std_10"] = (
        df["accel_mag"].rolling(10, min_periods=2).std().fillna(0.0)
    )
    return df


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

# Daily-consolidated rollups are named YYYY-MM-DD.parquet (data-engine
# _consolidate_day) — skip so a row is never double-counted (once in its live
# mission_s* file, again in the day rollup).
_DAILY_CONSOLIDATED_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.parquet$")


# Keep live mission telemetry AND cap_accel_* drain captures (ADR-021); drop
# schema-incompatible files. cap_accel_* is adapted to the live schema by
# _adapt_cap_accel below. cap_gyro_* is excluded on purpose: it is a separate
# file at a different ODR (416 vs 3332 Hz) and the accel-only training path
# (DESIGN_COUNCIL item 2, fork 1) does not align the two streams — revisit when
# gyro alignment lands. Daily rollups are skipped to avoid double-counting rows
# already present in their per-mission file.
def _is_trainable_parquet(name: str) -> bool:
    if name.startswith("cap_") and not name.startswith("cap_accel_"):
        return False
    if _DAILY_CONSOLIDATED_RE.match(name):
        return False
    return True


# Adapt a cap_accel_* drain capture to the live telemetry schema so the training
# loader can consume it (DESIGN_COUNCIL item 2, accel-only fork). Conversions:
#   - raw int16 axes (x/y/z at ISM330 ±2 g FS) → g via ACCEL_G_PER_LSB
#   - is_idle_snapshot → state (idle=0, moving=1)
#   - sample_index → seq (per-file monotonic sort key); seq_gap → 0 (contiguous)
#   - gyro_*, humidity_pct, and MOVING temp_c are absent in this file → 0.0
#     placeholders. This keeps rows past dropna() AND holds the XGBoost feature
#     dimension constant across rounds. These channels carry no signal in the
#     accel-only path; they gain meaning only once gyro alignment lands (fork 2/3).
def _adapt_cap_accel(df: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame()
    out["shuttle_id"]   = df["shuttle_id"].astype(int)
    out["seq"]          = df["sample_index"].astype(int)
    out["seq_gap"]      = 0
    out["state"]        = np.where(df["is_idle_snapshot"], 0, 1).astype(int)
    out["accel_x"]      = df["x"].astype("float32") * ACCEL_G_PER_LSB
    out["accel_y"]      = df["y"].astype("float32") * ACCEL_G_PER_LSB
    out["accel_z"]      = df["z"].astype("float32") * ACCEL_G_PER_LSB
    out["gyro_x"]       = 0.0
    out["gyro_y"]       = 0.0
    out["gyro_z"]       = 0.0
    # temp_c is stamped only on idle snapshots (NaN for MOVING); fill so MOVING
    # rows survive dropna(). humidity_pct is never in the drain schema.
    out["temp_c"]       = (df["temp_c"].astype("float32").fillna(0.0)
                           if "temp_c" in df.columns else 0.0)
    out["humidity_pct"] = 0.0
    return out


def load_buffered_data() -> tuple[np.ndarray, np.ndarray, str]:
    """
    Load and concatenate recent mission Parquet files from the RAM buffer.

    Loads up to MAX_PARQUET_FILES most recent files. Returns (X, y, labeller_name).
    """
    logger.info("Scanning RAM buffer for telemetry Parquet files...")

    if not os.path.exists(BUFFER_DIR):
        raise FileNotFoundError(f"CRITICAL: Buffer directory {BUFFER_DIR} not found.")

    files = sorted(
        [f for f in os.listdir(BUFFER_DIR)
         if f.endswith(".parquet") and _is_trainable_parquet(f)],
        key=lambda f: os.path.getmtime(os.path.join(BUFFER_DIR, f)),
    )
    if not files:
        raise FileNotFoundError(
            "CRITICAL: No trainable Parquet files in buffer (live mission_* and "
            "cap_accel_* drain captures are accepted; cap_gyro_* and daily-consolidated "
            "rollups are skipped). "
            "Ensure at least one shuttle mission has completed before triggering an FL round."
        )

    recent = files[-MAX_PARQUET_FILES:]
    frames = []
    for f in recent:
        raw = pd.read_parquet(os.path.join(BUFFER_DIR, f))
        # cap_accel_* uses the raw drain schema — adapt it to the live schema first.
        frames.append(_adapt_cap_accel(raw) if f.startswith("cap_accel_") else raw)
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

    # Guard: fail loudly rather than train XGBoost on an empty frame and save a
    # garbage 0-sample model. Triggers if the loaded files lack the live
    # telemetry schema (raw accel/gyro axes) and every row drops out at dropna().
    if df_clean.empty:
        raise ValueError(
            "CRITICAL: 0 trainable samples after dropping rows missing raw features. "
            "Loaded Parquet files lack the live telemetry schema (accel/gyro axes). "
            "FL training skipped — no valid mission data available."
        )

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

    cnn_autoencoder      — 1D-CNN reconstruction error (T3.3, default); falls back to IF
    isolation_forest_xgb — per-sample IsolationForest
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
