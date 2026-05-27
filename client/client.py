"""
PLUDOS AI Worker: Federated Learning Client
-------------------------------------------
Runs on the Jetson Orin Nano. Responsibilities:
1. Load all recent mission Parquet files from the RAM buffer (concatenated).
2. Generate anomaly labels via the selected backend (ANOMALY_MODEL env var):
     isolation_forest_xgb (default) — IsolationForest on MOVING packets → pseudo-labels
     lstm_autoencoder               — LSTM autoencoder on sliding windows of MOVING
                                      packets; reconstruction error = anomaly score
     threshold                       — legacy accel_z > ANOMALY_THRESHOLD_G rule
3. Train an XGBoost classifier on those labels (federated via Flower).
4. Profile energy consumption per FL phase via AlumetProfiler.
5. Stream energy telemetry to InfluxDB (fl_energy, fl_phases measurements).
6. Evaluate the server's global model on a local held-out test set.

n_estimators is set by the server each round via fit_config() based on the
previous round's measured energy — this closes the energy-aware FL loop (ADR-014).

Anomaly model switching:
  ANOMALY_MODEL=lstm_autoencoder — sequence-level LSTM reconstruction error (this branch)
  ANOMALY_MODEL=isolation_forest_xgb — per-sample IsolationForest (default, main branch)
  ANOMALY_MODEL=threshold — legacy accel_z threshold rule
"""

import flwr as fl
import xgboost as xgb
import numpy as np
import time
import logging
import os
import pandas as pd
import re
import socket
import subprocess
import threading
import random
import urllib.request
from pathlib import Path
from sklearn.ensemble import IsolationForest

from influxdb_client import InfluxDBClient, Point, WritePrecision  # type: ignore
from influxdb_client.client.write_api import SYNCHRONOUS           # type: ignore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==========================================
# 1. ENVIRONMENT & HARDWARE CONFIGURATION
# ==========================================
TEST_MODE  = os.getenv("TEST_MODE") == "1"
BUFFER_DIR = "./ram_buffer" if TEST_MODE else "/app/ram_buffer"


def _detect_xgb_device() -> str:
    # Try CUDA only if a GPU device node is present (Jetson: nvhost-ctrl-gpu;
    # discrete: nvidia0). Falls back to CPU silently rather than letting XGBoost
    # emit a noisy warning. Podman 3.4.x on JetPack 5.x doesn't support CDI,
    # so GPU device passthrough requires Podman ≥ 4.1 — tracked as a follow-up.
    if TEST_MODE:
        return "cpu"
    gpu_present = (
        os.path.exists("/dev/nvidia0") or
        os.path.exists("/dev/nvhost-ctrl-gpu")
    )
    if not gpu_present:
        return "cpu"
    try:
        import xgboost as xgb
        import numpy as np
        xgb.XGBClassifier(device="cuda", n_estimators=1).fit(
            np.array([[1.0, 2.0], [3.0, 4.0]]), np.array([0, 1])
        )
        return "cuda"
    except Exception:
        return "cpu"


DEVICE = _detect_xgb_device()
logger.info("[CONFIG] XGBoost device: %s", DEVICE)

# Alumet-relay Prometheus endpoint — scraped by AlumetProfiler for real INA3221 power.
# pludos-alumet-relay publishes port 9095; pludos-data-engine uses network_mode: host
# so localhost:9095 inside the container reaches the host's published port directly.
ALUMET_PROMETHEUS_URL = os.getenv("ALUMET_PROMETHEUS_URL", "http://localhost:9095/metrics")

# Legacy file-based relay path (probe.py, now dormant — superseded by Prometheus scrape).
ALUMET_RELAY_METRICS_FILE = os.getenv("ALUMET_RELAY_METRICS_FILE", "")

# Controls which energy source is accepted during FL rounds.
#   "alumet"     — hard requirement; round aborts with ERROR if Alumet scrape fails or returns 0.
#   "tegrastats"  — use tegrastats only (debug/no-Alumet-relay mode).
#   "auto"        — legacy: relay → alumet → tegrastats fallback, tagged in InfluxDB.
ENERGY_SOURCE_REQUIRED = os.getenv("ENERGY_SOURCE_REQUIRED", "alumet")

# Maximum Parquet files to concatenate per FL round. Ensures mid-mission buffer-
# pressure flushes are included rather than training on just the latest tail file.
MAX_PARQUET_FILES = int(os.getenv("MAX_PARQUET_FILES", "20"))

# Anomaly model selector — controls pseudo-label generation for XGBoost training.
#   "lstm_autoencoder" (default) — LSTM autoencoder on 50-packet sliding windows of
#       MOVING data; reconstruction error = anomaly score. Catches temporal patterns
#       (abnormal ramp-up, sustained jitter) that per-sample methods miss.
#       Falls back to isolation_forest_xgb if torch is unavailable or data is sparse.
#   "isolation_forest_xgb" — IsolationForest on per-packet vibration features.
#       Faster (~1 s vs ~10 s per round), no torch dependency.
#   "threshold" — legacy: accel_z > ANOMALY_THRESHOLD_G. Debug/comparison only.
ANOMALY_MODEL = os.getenv("ANOMALY_MODEL", "isolation_forest_xgb")

# Isolation Forest: fraction of MOVING packets expected to be anomalous.
# 0.05 = conservative prior (5% of warehouse shuttle samples are shock/wear events).
# Tune upward if the shuttle is known to be in poor condition.
IF_CONTAMINATION = float(os.getenv("IF_CONTAMINATION", "0.05"))

# Minimum MOVING packets required to trust the Isolation Forest fit.
# Below this, fall back to the threshold label to avoid fitting noise.
IF_MIN_MOVING_SAMPLES = int(os.getenv("IF_MIN_MOVING_SAMPLES", "50"))

# Legacy threshold label fallback (used when ANOMALY_MODEL=threshold or IF data too sparse).
# Default 2.0g: gravity alone reads ~1.0g, so 2.0g catches genuine shocks only.
ANOMALY_THRESHOLD_G = float(os.getenv("ANOMALY_THRESHOLD_G", "2.0"))

# LSTM autoencoder constants — only used when ANOMALY_MODEL=lstm_autoencoder.
# Window of 50 packets = 5 s at 10 Hz MOVING TX rate; covers one acceleration ramp-up.
LSTM_WINDOW_SIZE         = int(os.getenv("LSTM_WINDOW_SIZE",         "50"))
# Hidden dimension: 32 is enough to capture shuttle motion structure; larger = slower.
LSTM_HIDDEN_DIM          = int(os.getenv("LSTM_HIDDEN_DIM",          "32"))
# 30 epochs: ~4 batches/epoch at 3000 MOVING packets (stride 25) = 120 steps — enough
# for convergence without being slow. 20 was borderline; keep ≥25 in practice.
LSTM_EPOCHS              = int(os.getenv("LSTM_EPOCHS",              "30"))
LSTM_BATCH_SIZE          = int(os.getenv("LSTM_BATCH_SIZE",          "32"))
# Need at least 4 non-overlapping windows (2×window_size with 50% stride overlap) to
# train a meaningful autoencoder; below this, fall back to IsolationForest.
LSTM_MIN_MOVING_SAMPLES  = int(os.getenv("LSTM_MIN_MOVING_SAMPLES",  "200"))

# Where to persist the received global model for standalone (no-server) inference.
# Lives inside BUFFER_DIR so it survives on the host bind-mount across container
# restarts. Relative to the container working dir, resolved at runtime.
LOCAL_MODEL_PATH = Path(os.getenv("LOCAL_MODEL_PATH",
                                   os.path.join(os.getenv("BUFFER_DIR", "/app/ram_buffer"),
                                                "model", "latest.ubj")))

# Default n_estimators when no server override is present. The server adapts this
# each round via fit_config() based on the previous round's energy (ADR-014).
N_ESTIMATORS_DEFAULT = int(os.getenv("FL_N_ESTIMATORS_DEFAULT", "10"))

# Gateway identity used in gw_status heartbeat — same env var as data-engine.py's
# stm_mission tag so the FL trigger can correlate readings from the same Jetson.
GATEWAY_ID = os.getenv("JETSON_HOSTNAME", socket.gethostname())

# Timestamp (epoch seconds) of the previous heartbeat — used to derive
# `missions_since_last_round` as the count of parquet files newer than this.
# Module scope: persists across Flower's repeated client_fn() invocations within
# one ai-worker process.
_last_heartbeat_ts: float = 0.0


# ==========================================
# 2. ALUMET ENERGY PROFILING API
# ==========================================

def _read_nvpmodel() -> str:
    # "NV Power Mode: MAXN_SUPER\n..." — extract last token of first line.
    try:
        out = subprocess.check_output(["nvpmodel", "-q"], timeout=2).decode()
        return out.splitlines()[0].split()[-1]
    except Exception:
        return "unknown"


def _read_tegrastats() -> dict[str, float]:
    # One-shot tegrastats sample; parses VDD_GPU, VDD_CPU, VDD_SOC rails.
    # tegrastats on JetPack 5.x has no --count flag — start it, read one line,
    # kill it. Binary is bind-mounted from the host at /usr/bin/tegrastats.
    # Returns zeros on any failure — caller always gets a safe dict.
    try:
        proc = subprocess.Popen(
            ["tegrastats", "--interval", "100"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        try:
            line = proc.stdout.readline()  # type: ignore[union-attr]
        finally:
            proc.kill()
            proc.wait()
        gpu = int(m.group(1)) if (m := re.search(r"VDD_GPU\S*\s+(\d+)mW", line)) else 0
        cpu = int(m.group(1)) if (m := re.search(r"VDD_CPU\S*\s+(\d+)mW", line)) else 0
        soc = int(m.group(1)) if (m := re.search(r"VDD_SOC\S*\s+(\d+)mW", line)) else 0
        return {"gpu": gpu / 1000.0, "cpu": cpu / 1000.0, "total": (gpu + cpu + soc) / 1000.0}
    except Exception:
        return {"gpu": 0.0, "cpu": 0.0, "total": 0.0}


def _read_relay_metrics() -> dict[str, float] | None:
    # Legacy: reads from the shared file written by alumet-relay probe.py (dormant).
    if not ALUMET_RELAY_METRICS_FILE or not os.path.exists(ALUMET_RELAY_METRICS_FILE):
        return None
    try:
        import json
        with open(ALUMET_RELAY_METRICS_FILE) as f:
            data = json.load(f)
        return {
            "gpu":   float(data.get("power_gpu_w",   0.0)),
            "cpu":   float(data.get("power_cpu_w",   0.0)),
            "total": float(data.get("power_total_w", 0.0)),
        }
    except Exception:
        return None


def _read_alumet_prometheus() -> dict[str, float] | None:
    """
    Scrape the local alumet-relay Prometheus endpoint for Jetson INA3221 power.

    Parses input_current_alumet (mA) and input_voltage_alumet (mV) per channel.
    Power = current_mA * voltage_mV / 1_000_000 W.
    Channel mapping: VDD_IN → total, *CPU* → cpu, *SOC* or *GPU* → gpu.
    Returns None if the endpoint is unreachable (alumet-relay not running).
    """
    try:
        with urllib.request.urlopen(ALUMET_PROMETHEUS_URL, timeout=1) as r:
            text = r.read().decode()
    except Exception:
        return None

    currents: dict[str, float] = {}
    voltages: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        # Extract channel label from Prometheus label set, then the value.
        m = re.search(r'input_current_alumet\{[^}]*ina_channel_label="([^"]+)"', line)
        if m:
            try:
                currents[m.group(1)] = float(line.rsplit(None, 1)[-1])
            except ValueError:
                pass
        m = re.search(r'input_voltage_alumet\{[^}]*ina_channel_label="([^"]+)"', line)
        if m:
            try:
                voltages[m.group(1)] = float(line.rsplit(None, 1)[-1])
            except ValueError:
                pass

    if not currents:
        return None

    def _power_w(hint: str) -> float:
        # Find the first channel whose name contains hint (case-insensitive).
        for ch, ma in currents.items():
            if hint.lower() in ch.lower():
                mv = voltages.get(ch, 5000.0)
                return ma * mv / 1_000_000.0
        return 0.0

    total = _power_w("VDD_IN") or _power_w("IN")
    cpu   = _power_w("CPU")
    gpu   = _power_w("SOC") or _power_w("GPU")

    # If VDD_IN not found, sum all channels as best-effort total.
    if total == 0.0 and currents:
        total = sum(
            ma * voltages.get(ch, 5000.0) / 1_000_000.0
            for ch, ma in currents.items()
        )

    logger.debug("[ALUMET] Prometheus: cpu=%.2fW gpu=%.2fW total=%.2fW channels=%s",
                 cpu, gpu, total, list(currents.keys()))
    return {"gpu": gpu, "cpu": cpu, "total": total}


class AlumetProfiler:
    """
    Background energy profiler for FL rounds.

    Runs a 10 Hz sampling thread for the duration of each FL round.
    Writes two InfluxDB measurements:

      fl_energy  — continuous 10 Hz power samples (power_gpu_w, power_cpu_w,
                   power_total_w, energy_j, fl_round_int) tagged by device/fl_round/nvpmodel.

      fl_phases  — one summary point per named phase (load, train, round_total)
                   with duration_ms, energy_j, avg_power_w, fl_round_int.

    fl_round is stored as both a string tag (for grouping) and an integer field
    (fl_round_int) to support Flux range queries across rounds.
    """

    def __init__(self, round_num: int | str) -> None:
        self.round_num   = round_num
        self.is_running  = False
        self.thread: threading.Thread | None = None
        self.nvpmodel    = "test" if TEST_MODE else _read_nvpmodel()
        self.device_name = socket.gethostname()

        # Shared energy accumulator — written by polling thread, read by begin/end_phase.
        # Float assignment is atomic under CPython's GIL; precision loss is negligible
        # for 10 Hz sampling.
        self._energy_j: float = 0.0

        # Set True by _poll_metrics when ENERGY_SOURCE_REQUIRED="alumet" and scrape
        # fails or returns zero. Checked in end_phase to abort the round cleanly.
        self._source_failed: bool = False
        # Last-used energy source; written by polling thread, used for InfluxDB tagging.
        self._energy_source: str = "unknown"

        # Active phase snapshots: phase_name → (start_monotonic, energy_j_at_start)
        self._phase_snapshots: dict[str, tuple[float, float]] = {}

        # fl_round_int for Flux range queries — 0 if round_num is non-numeric.
        self._round_int: int = int(round_num) if str(round_num).isdigit() else 0

        influx_url    = os.getenv("INFLUXDB_URL",    "http://127.0.0.1:8086")
        influx_token  = os.getenv("INFLUXDB_TOKEN",  "pludos-secret-token")
        influx_org    = os.getenv("INFLUXDB_ORG",    "pludos")
        influx_bucket = os.getenv("INFLUXDB_BUCKET", "alumet_energy")

        self.bucket    = influx_bucket
        self.client    = InfluxDBClient(url=influx_url, token=influx_token, org=influx_org)
        self.write_api = self.client.write_api(write_options=SYNCHRONOUS)

    def start(self) -> None:
        """Start the background 10 Hz sampling thread."""
        self.is_running = True
        logger.info("[ALUMET] Starting profiler for FL Round %s", self.round_num)
        self.thread = threading.Thread(target=self._poll_metrics, daemon=True)
        self.thread.start()

    def begin_phase(self, phase: str) -> None:
        """Record the start of a named phase; used with end_phase to compute energy delta."""
        self._phase_snapshots[phase] = (time.monotonic(), self._energy_j)

    def end_phase(self, phase: str) -> None:
        # Compute duration and energy delta since begin_phase, write fl_phases point.
        if phase not in self._phase_snapshots:
            return
        start_t, start_e = self._phase_snapshots.pop(phase)
        now         = time.monotonic()
        duration_ms = (now - start_t) * 1000.0
        delta_e     = self._energy_j - start_e
        # Avoid divide-by-zero on very short phases.
        avg_power   = delta_e / max(now - start_t, 1e-6)

        # If the required alumet source failed, write NaN so the server treats this
        # round as "unknown energy" rather than "low energy" (T0.4 guard on server side).
        energy_field = float("nan") if self._source_failed and ENERGY_SOURCE_REQUIRED == "alumet" else delta_e
        source_tag   = self._energy_source

        point = (
            Point("fl_phases")
            .tag("device",   self.device_name)
            .tag("fl_round", str(self.round_num))
            .tag("phase",    phase)
            .tag("nvpmodel", self.nvpmodel)
            .tag("source",   source_tag)
            .field("fl_round_int", self._round_int)
            .field("duration_ms",  duration_ms)
            .field("energy_j",     energy_field)
            .field("avg_power_w",  avg_power)
            .time(time.time_ns(), WritePrecision.NS)
        )
        try:
            self.write_api.write(bucket=self.bucket, record=point)
            logger.info(
                "[ALUMET] phase=%-12s dur=%.0fms energy=%.3fJ avg=%.2fW source=%s",
                phase, duration_ms, delta_e, avg_power, source_tag,
            )
        except Exception as exc:
            logger.error("[ALUMET] fl_phases write failed (phase=%s): %s", phase, exc)

        if self._source_failed and ENERGY_SOURCE_REQUIRED == "alumet":
            # Signal the polling thread to stop, then abort the round.
            self.is_running = False
            raise RuntimeError(
                f"[ENERGY] alumet unavailable for round {self.round_num}; round aborted"
            )

    def _poll_metrics(self) -> None:
        # 10 Hz polling loop. Reads INA3221 relay file if available, falls back
        # to tegrastats (Phase 1). Accumulates energy_j for phase delta computation.
        last_t = time.monotonic()
        while self.is_running:
            now     = time.monotonic()
            elapsed = now - last_t
            last_t  = now

            if TEST_MODE:
                pw = {
                    "gpu":   random.uniform(20.0, 40.0),
                    "cpu":   random.uniform(3.0,  8.0),
                    "total": random.uniform(25.0, 50.0),
                }
                self._energy_source = "test"
            elif ENERGY_SOURCE_REQUIRED == "alumet":
                # alumet required: fail the round if unreachable or reports zero power.
                pw = _read_relay_metrics() or _read_alumet_prometheus()
                if pw is None or pw.get("total", 0.0) == 0.0:
                    if not self._source_failed:
                        logger.error("[ENERGY] alumet unavailable, refusing to report fake energy")
                        self._source_failed = True
                    self._energy_source = "failed"
                    pw = {"gpu": 0.0, "cpu": 0.0, "total": 0.0}
                else:
                    self._energy_source = "alumet"
            elif ENERGY_SOURCE_REQUIRED == "tegrastats":
                pw = _read_tegrastats()
                self._energy_source = "tegrastats"
            else:  # auto — legacy fallback chain, tagged for Grafana visibility
                pw = _read_relay_metrics() or _read_alumet_prometheus()
                if pw is not None:
                    self._energy_source = "alumet"
                else:
                    pw = _read_tegrastats()
                    self._energy_source = "tegrastats_fallback"
                    if not self._source_failed:
                        logger.warning(
                            "[ENERGY][DEGRADED] alumet unavailable — falling back to tegrastats"
                        )
                        self._source_failed = True

            # Atomic float update — safe under CPython GIL for this access pattern.
            self._energy_j += pw["total"] * elapsed

            point = (
                Point("fl_energy")
                .tag("device",   self.device_name)
                .tag("fl_round", str(self.round_num))
                .tag("nvpmodel", self.nvpmodel)
                .tag("source",   self._energy_source)
                .field("fl_round_int",  self._round_int)
                .field("power_gpu_w",   pw["gpu"])
                .field("power_cpu_w",   pw["cpu"])
                .field("power_total_w", pw["total"])
                .field("energy_j",      self._energy_j)
                .time(time.time_ns(), WritePrecision.NS)
            )
            try:
                self.write_api.write(bucket=self.bucket, record=point)
                logger.info(
                    "[ALUMET] gpu=%.2fW cpu=%.2fW total=%.2fW energy=%.3fJ",
                    pw["gpu"], pw["cpu"], pw["total"], self._energy_j,
                )
            except Exception as exc:
                logger.error("[ALUMET] fl_energy write failed: %s", exc)

            time.sleep(0.1)

    def stop(self) -> None:
        """Stop the sampling thread and release the InfluxDB connection pool."""
        self.is_running = False
        if self.thread:
            self.thread.join()
        # Release HTTP connection pool — each FL round creates a new client.
        self.client.close()
        logger.info("[ALUMET] Round %s complete. total_energy=%.3fJ",
                    self.round_num, self._energy_j)


# ==========================================
# 3. FEDERATED LEARNING LOGIC
# ==========================================

def load_buffered_data() -> tuple[np.ndarray, np.ndarray]:
    """
    Load and concatenate recent mission Parquet files from the RAM buffer.

    Loads up to MAX_PARQUET_FILES most recent files to ensure data from
    mid-mission buffer-pressure flushes is included — not just the latest
    tail file, which may be a partial mission.
    """
    logger.info("Scanning RAM buffer for telemetry Parquet files...")

    if not os.path.exists(BUFFER_DIR):
        raise FileNotFoundError(f"CRITICAL: Buffer directory {BUFFER_DIR} not found.")

    # Sort by modification time so daily (YYYY-MM-DD.parquet) and intra-day mission
    # files are ordered chronologically regardless of their different name formats.
    files = sorted(
        [f for f in os.listdir(BUFFER_DIR) if f.endswith(".parquet")],
        key=lambda f: os.path.getmtime(os.path.join(BUFFER_DIR, f)),
    )
    if not files:
        raise FileNotFoundError(
            "CRITICAL: No Parquet files found in buffer. "
            "Ensure at least one shuttle mission has completed before triggering an FL round."
        )

    # Take only the most recent N files to bound memory usage.
    recent = files[-MAX_PARQUET_FILES:]
    frames = [pd.read_parquet(os.path.join(BUFFER_DIR, f)) for f in recent]
    df = pd.concat(frames, ignore_index=True)
    logger.info(
        "Loaded %d samples from %d Parquet file(s): %s … %s",
        len(df), len(recent), recent[0], recent[-1],
    )

    # Backward-compatible backfill for columns added in schema upgrades.
    # Old Parquet files missing a column get a neutral value so training doesn't crash.
    _backfill_f16 = {
        "gyro_x": 0, "gyro_y": 0, "gyro_z": 0, "gyro_mag": 0,
        "accel_jerk": 0,
        "horizontal_accel": 0,   # √(ax²+ay²); 0 = no horizontal motion assumed
        "tilt_angle_deg":   0,   # 0° = flat; safe neutral for old files
        "gyro_jerk":        0,
        "rolling_accel_mean_10": 0,
        "rolling_accel_std_10":  0,
    }
    for col, val in _backfill_f16.items():
        if col not in df.columns:
            df[col] = np.float16(val)
    if "accel_mag" not in df.columns:
        df["accel_mag"] = (df["accel_x"]**2 + df["accel_y"]**2 + df["accel_z"]**2).pow(0.5)
    if "seq_gap" not in df.columns:
        df["seq_gap"] = np.int16(0)
    if "energy_j" not in df.columns:
        df["energy_j"] = np.float32(0)
    if "mission_elapsed_s" not in df.columns:
        df["mission_elapsed_s"] = np.float32(0)
    for col in ("moving_run_id", "pause_count", "is_long_pause"):
        if col not in df.columns:
            df[col] = np.int8(0)
    for col in ("pause_duration_s", "moving_run_dur_s"):
        if col not in df.columns:
            df[col] = np.float32(0)

    # ZUPT speed and displacement.
    # New Parquet files (data-engine schema ≥ v3.1): pre-computed with correct measured dt.
    # Old files: compute here with dt_s = 0.1 s (10 Hz MOVING TX rate).
    if "speed_ms" not in df.columns:
        dt_s = 0.1   # 10 Hz MOVING rate; old constant was 0.02 (50 Hz, wrong since commit 3e99444)
        df["speed_ms"]       = 0.0
        df["displacement_m"] = 0.0
        for sid, group in df.groupby("shuttle_id", sort=False):
            idx     = group.sort_values("seq").index
            states  = group.loc[idx, "state"].values.astype(int)
            ax_vals = group.loc[idx, "accel_x"].values
            ay_vals = group.loc[idx, "accel_y"].values
            speeds, disps = [], []
            vel, disp, prev = 0.0, 0.0, int(states[0])
            for i in range(len(states)):
                s = int(states[i])
                if s == 1 and prev == 0:
                    vel = 0.0
                if s == 1 and not (np.isnan(ax_vals[i]) or np.isnan(ay_vals[i])):
                    a_h  = float(np.sqrt(ax_vals[i]**2 + ay_vals[i]**2))
                    vel  = max(0.0, vel + a_h * 9.81 * dt_s)
                    disp += vel * dt_s
                else:
                    vel = 0.0
                speeds.append(round(vel, 3))
                disps.append(round(disp, 3))
                prev = s
            df.loc[idx, "speed_ms"]       = speeds
            df.loc[idx, "displacement_m"] = disps
    elif "displacement_m" not in df.columns:
        df["displacement_m"] = np.float32(0)

    # Column names match data-engine.py _PARQUET_COLS — must stay in sync.
    feature_cols = [
        "accel_x", "accel_y", "accel_z", "accel_mag",
        "accel_jerk", "horizontal_accel", "tilt_angle_deg",
        "gyro_x", "gyro_y", "gyro_z", "gyro_mag", "gyro_jerk",
        "rolling_accel_mean_10", "rolling_accel_std_10",
        "seq_gap",
        "energy_j",
        "mission_elapsed_s",
        "speed_ms", "displacement_m",
        "moving_run_id", "pause_duration_s", "moving_run_dur_s",
        "pause_count", "is_long_pause",
        "state",
    ]
    # Drop rows where any feature is NaN (sensor failure packets).
    df_clean = df[feature_cols].dropna()
    X_train = df_clean.values

    y_train = _make_anomaly_labels(df_clean)
    return X_train, y_train


# Features used by Isolation Forest — vibration and shock channels only.
# IDLE packets are excluded from the IF fit (no bearing load at rest).
_IF_FEATURES = [
    "accel_mag",            # total shock magnitude
    "rolling_accel_std_10", # sustained vibration (bearing wear signature)
    "gyro_x",               # torsional vibration (motor/bearing)
    "gyro_mag",             # overall rotation magnitude
    "accel_z",              # vertical channel — floor surface + bearing noise
]

# Features used by the LSTM autoencoder — raw 6-DOF motion plus magnitude.
# Raw axes are preferred over derived stats so the autoencoder learns temporal
# patterns in the signal itself, not features already computed across time.
_LSTM_FEATURES = [
    "accel_x", "accel_y", "accel_z",
    "gyro_x",  "gyro_y",  "gyro_z",
    "accel_mag",
]


def _make_anomaly_labels(df_clean: pd.DataFrame) -> np.ndarray:
    """
    Dispatch to the active anomaly labelling backend (ANOMALY_MODEL env var).

    lstm_autoencoder     — sequence-level LSTM reconstruction error (this branch)
    isolation_forest_xgb — per-sample IsolationForest (default)
    threshold            — legacy accel_z > ANOMALY_THRESHOLD_G rule
    """
    if ANOMALY_MODEL == "lstm_autoencoder":
        return _make_anomaly_labels_lstm(df_clean)
    if ANOMALY_MODEL == "threshold":
        return (df_clean["accel_z"] > ANOMALY_THRESHOLD_G).astype(int).values
    # Default: isolation_forest_xgb
    return _make_anomaly_labels_if(df_clean)


def _make_anomaly_labels_lstm(df_clean: pd.DataFrame) -> np.ndarray:
    """
    LSTM autoencoder anomaly labelling for XGBoost training.

    Trains a small seq2seq LSTM autoencoder on sliding windows of MOVING
    packets. Windows with high reconstruction error are anomalous — they
    represent motion patterns the model has not learned to compress, which
    on warehouse shuttles corresponds to vibration or shock signatures.

    Falls back to IsolationForest if there are fewer than LSTM_MIN_MOVING_SAMPLES
    MOVING packets (too few windows to train a useful autoencoder).

    Returns the same binary label array shape as _make_anomaly_labels().
    """
    # Lazy import — torch is only required on this branch.
    try:
        import torch
        import torch.nn as nn
    except ImportError:
        logger.error("[ANOMALY] torch not installed — falling back to IsolationForest")
        return _make_anomaly_labels_if(df_clean)

    moving_mask = df_clean["state"].astype(int) == 1
    n_moving = int(moving_mask.sum())

    if n_moving < LSTM_MIN_MOVING_SAMPLES:
        logger.warning(
            "[ANOMALY] LSTM: only %d MOVING samples (need %d) — falling back to IsolationForest",
            n_moving, LSTM_MIN_MOVING_SAMPLES,
        )
        return _make_anomaly_labels_if(df_clean)

    available = [c for c in _LSTM_FEATURES if c in df_clean.columns]
    # Parquet files are written sorted by sequence_monotonic (data-engine flush), and
    # load_buffered_data concatenates them in mtime order, so MOVING rows are already
    # in chronological per-mission blocks — no additional sort needed here.
    df_moving = df_clean.loc[moving_mask]
    X_moving = df_moving[available].values.astype(np.float32)   # (N, D)

    # Build overlapping windows with 50% stride — more training signal than non-overlapping.
    stride = max(1, LSTM_WINDOW_SIZE // 2)
    starts = list(range(0, n_moving - LSTM_WINDOW_SIZE + 1, stride))
    if not starts:
        logger.warning("[ANOMALY] LSTM: not enough packets for even one window — falling back")
        return _make_anomaly_labels_if(df_clean)

    windows = np.stack([X_moving[s:s + LSTM_WINDOW_SIZE] for s in starts])  # (W, T, D)

    # Normalize each feature to zero mean / unit std across the full window set.
    mean = windows.mean(axis=(0, 1), keepdims=True)
    std  = windows.std(axis=(0, 1), keepdims=True) + 1e-8
    X_norm = (windows - mean) / std

    tensor = torch.tensor(X_norm)   # (W, T, D)
    n_features = tensor.shape[2]

    # Seq2seq LSTM autoencoder: encoder compresses to hidden state,
    # decoder reconstructs the sequence from that context vector.
    class _LSTMAutoencoder(nn.Module):
        def __init__(self, n_feat: int, hidden: int) -> None:
            super().__init__()
            self.encoder = nn.LSTM(n_feat,  hidden, batch_first=True)
            self.decoder = nn.LSTM(hidden,  n_feat,  batch_first=True)

        def forward(self, x: "torch.Tensor") -> "torch.Tensor":
            # Encode: collapse sequence to (batch, 1, hidden).
            _, (h, _) = self.encoder(x)
            # Expand hidden state across all time steps so the decoder
            # sees the same context at every position.
            ctx = h.permute(1, 0, 2).expand(-1, x.size(1), -1).contiguous()
            out, _ = self.decoder(ctx)
            return out

    model = _LSTMAutoencoder(n_features, LSTM_HIDDEN_DIM)
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
    criterion = nn.MSELoss()

    dataset = torch.utils.data.TensorDataset(tensor)
    loader  = torch.utils.data.DataLoader(
        dataset, batch_size=LSTM_BATCH_SIZE, shuffle=True, drop_last=False,
    )

    logger.info(
        "[ANOMALY] LSTM: training autoencoder — %d windows, T=%d, D=%d, hidden=%d, epochs=%d",
        len(starts), LSTM_WINDOW_SIZE, n_features, LSTM_HIDDEN_DIM, LSTM_EPOCHS,
    )
    model.train()
    for epoch in range(LSTM_EPOCHS):
        epoch_loss = 0.0
        for (batch,) in loader:
            optimizer.zero_grad()
            loss = criterion(model(batch), batch)
            loss.backward()
            optimizer.step()
            epoch_loss += loss.item()
        if (epoch + 1) % 5 == 0:
            logger.info("[ANOMALY] LSTM epoch %02d/%d loss=%.4f",
                        epoch + 1, LSTM_EPOCHS, epoch_loss / len(loader))

    # Compute per-window MSE reconstruction error.
    model.eval()
    with torch.no_grad():
        recon  = model(tensor)
        errors = ((tensor - recon) ** 2).mean(dim=(1, 2)).numpy()   # (W,)

    # Accumulate per-packet scores by averaging over all windows that include it.
    # Uses the same stride as the window construction loop.
    packet_scores = np.zeros(n_moving, dtype=np.float32)
    packet_counts = np.zeros(n_moving, dtype=np.float32)
    for i, s in enumerate(starts):
        packet_scores[s:s + LSTM_WINDOW_SIZE] += errors[i]
        packet_counts[s:s + LSTM_WINDOW_SIZE] += 1.0

    # Packets not covered by any window (tail < LSTM_WINDOW_SIZE) stay score=0.
    covered = packet_counts > 0
    avg_scores = np.where(covered, packet_scores / np.maximum(packet_counts, 1), 0.0)

    # Flag the top IF_CONTAMINATION fraction of covered packets as anomalous.
    threshold = np.percentile(avg_scores[covered], 100.0 * (1.0 - IF_CONTAMINATION))
    moving_labels = (avg_scores >= threshold).astype(int)

    n_anomalous = int(moving_labels.sum())
    logger.info(
        "[ANOMALY] LSTM: %d/%d MOVING packets flagged anomalous (%.1f%%), threshold=%.6f",
        n_anomalous, n_moving, 100.0 * n_anomalous / n_moving, threshold,
    )

    # df_moving preserves df_clean's order (no sort), so moving_labels[i] maps to
    # the i-th True position in moving_mask. Use the boolean mask for assignment —
    # df_moving.index contains original (pre-dropna) row numbers which exceed len(y).
    y = np.zeros(len(df_clean), dtype=int)
    y[moving_mask.values] = moving_labels
    return y


def _make_anomaly_labels_if(df_clean: pd.DataFrame) -> np.ndarray:
    """IsolationForest path extracted for reuse as a fallback from LSTM."""
    y = np.zeros(len(df_clean), dtype=int)
    moving_mask = df_clean["state"].astype(int) == 1
    n_moving = moving_mask.sum()

    if n_moving < IF_MIN_MOVING_SAMPLES:
        logger.warning(
            "[ANOMALY] only %d MOVING samples (need %d) — falling back to threshold label",
            n_moving, IF_MIN_MOVING_SAMPLES,
        )
        return (df_clean["accel_z"] > ANOMALY_THRESHOLD_G).astype(int).values

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
    return y


def _write_gw_status_heartbeat(last_round: int | None = None) -> None:
    """
    Emit a gw_status InfluxDB point so the server-side fl-trigger watcher can
    detect when this gateway is alive and has new data ready for an FL round.

    Best-effort: any failure is logged and swallowed so the FL round is never
    interrupted by a transient InfluxDB outage. Runs on the caller's thread —
    not a daemon — because the caller is already off the asyncio loop (Flower
    evaluate / client_fn).
    """
    global _last_heartbeat_ts

    # Snapshot the buffer cheaply: filename count + a one-shot read of the most
    # recent file's shuttle_id column for the distinct-shuttle approximation.
    parquet_files_available = 0
    missions_since_last_round = 0
    shuttle_count = 0
    try:
        if os.path.isdir(BUFFER_DIR):
            entries = [f for f in os.listdir(BUFFER_DIR) if f.endswith(".parquet")]
            parquet_files_available = len(entries)
            # Count files whose mtime is strictly newer than the last heartbeat —
            # bootstraps to "all files" on the very first call (_last_heartbeat_ts=0).
            missions_since_last_round = sum(
                1 for f in entries
                if os.path.getmtime(os.path.join(BUFFER_DIR, f)) > _last_heartbeat_ts
            )
            # Cheap distinct-shuttle count: read only the shuttle_id column from
            # the newest parquet file. Filename pattern is "mission_<ts>.parquet"
            # without a shuttle ID, so we have to peek inside.
            if entries:
                # Filename pattern is "mission_<ts>.parquet" with no shuttle ID,
                # so peek inside the newest file's shuttle_id column —
                # written by data-engine.py _flush().
                newest = max(entries, key=lambda f: os.path.getmtime(os.path.join(BUFFER_DIR, f)))
                df = pd.read_parquet(
                    os.path.join(BUFFER_DIR, newest),
                    columns=["shuttle_id"],
                )
                shuttle_count = int(df["shuttle_id"].nunique())
    except Exception as exc:
        logger.warning("[GW_STATUS] buffer scan failed: %s", exc)

    influx_url    = os.getenv("INFLUXDB_URL",    "http://127.0.0.1:8086")
    influx_token  = os.getenv("INFLUXDB_TOKEN",  "pludos-secret-token")
    influx_org    = os.getenv("INFLUXDB_ORG",    "pludos")
    influx_bucket = os.getenv("INFLUXDB_BUCKET", "alumet_energy")

    try:
        client = InfluxDBClient(url=influx_url, token=influx_token, org=influx_org)
        try:
            point = (
                Point("gw_status")
                .tag("gateway_id", GATEWAY_ID)
                .field("shuttle_count",             shuttle_count)
                .field("missions_since_last_round", missions_since_last_round)
                .field("parquet_files_available",   parquet_files_available)
                # last_round is -1 when emitted at client startup (no round yet).
                .field("last_round", int(last_round) if last_round is not None else -1)
                .time(time.time_ns(), WritePrecision.NS)
            )
            client.write_api(write_options=SYNCHRONOUS).write(
                bucket=influx_bucket, record=point
            )
            logger.info(
                "[GW_STATUS] gateway=%s parquet=%d missions_new=%d shuttles=%d round=%s",
                GATEWAY_ID, parquet_files_available, missions_since_last_round,
                shuttle_count, last_round,
            )
        finally:
            client.close()
        _last_heartbeat_ts = time.time()
    except Exception as exc:
        # Heartbeat is best-effort; never fail the FL round on InfluxDB issues.
        logger.warning("[GW_STATUS] write failed: %s", exc)


class PLUDOSClient(fl.client.NumPyClient):
    """Flower framework wrapper for PLUDOS edge node participation in FL rounds."""

    def get_parameters(self, config):
        return []

    def fit(self, parameters, config):
        """
        Triggered by the central server each FL round.
        n_estimators is read from config — set by the server based on the
        previous round's energy measurement (ADR-014 energy-aware adaptation).
        Profiles energy across three phases: load, train, round_total.
        """
        round_num    = config.get("server_round", "unknown")
        # Server overrides n_estimators based on previous round's energy budget.
        n_estimators = int(config.get("n_estimators", N_ESTIMATORS_DEFAULT))

        profiler = AlumetProfiler(round_num)
        profiler.start()
        profiler.begin_phase("round_total")
        model = None

        try:
            # Phase: data loading — I/O bound, typically short.
            profiler.begin_phase("load")
            X_train, y_train = load_buffered_data()
            profiler.end_phase("load")

            # Phase: training — GPU bound, dominant energy consumer.
            logger.info(
                "Training XGBoost (device=%s, n_estimators=%d) for round %s",
                DEVICE, n_estimators, round_num,
            )
            profiler.begin_phase("train")
            # Artificial sleep in TEST_MODE ensures enough InfluxDB points for Grafana.
            if TEST_MODE:
                time.sleep(1.5)
            model = xgb.XGBClassifier(n_estimators=n_estimators, tree_method="hist", device=DEVICE)
            model.fit(X_train, y_train)
            profiler.end_phase("train")
        finally:
            # Always record round_total — even if load or train raised.
            # This ensures fl_phases gets a data point for budget adaptation.
            profiler.end_phase("round_total")
            profiler.stop()

        if model is None:
            raise RuntimeError(f"Round {round_num}: training did not complete; check buffer.")

        # Serialise booster trees to raw JSON bytes for Flower transport.
        booster     = model.get_booster()
        raw_booster = booster.save_raw("json")
        model_bytes = np.frombuffer(raw_booster, dtype=np.uint8)

        logger.info("Round %s complete — %d training samples, %d estimators",
                    round_num, len(X_train), n_estimators)
        return [model_bytes], len(X_train), {}

    def evaluate(self, parameters, config):
        """
        Evaluate the server's global model on a local held-out test set (P2-6 fix).
        Uses an 80/20 time-ordered split — no shuffle to preserve time-series integrity.
        Returns dummy metrics if no global model is available yet (round 1).
        """
        try:
            X, y = load_buffered_data()
            if len(X) < 20 or not parameters:
                # Too few samples or no global model sent yet.
                return 0.0, len(X), {"accuracy": 0.0}

            # 80/20 time-ordered split — last 20% held out for evaluation.
            split  = int(len(X) * 0.8)
            X_test = X[split:]
            y_test = y[split:]
            if len(X_test) == 0:
                return 0.0, 0, {"accuracy": 0.0}

            # Deserialise the server's merged booster from the NumPy parameter array.
            booster = xgb.Booster()
            booster.load_model(bytearray(parameters[0].tobytes()))

            # Persist global model locally for standalone inference (no server needed).
            # Saved to BUFFER_DIR/model/latest.ubj which is a host bind-mount — survives
            # container restarts. Best-effort: never fail the round on a save error.
            try:
                LOCAL_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
                booster.save_model(str(LOCAL_MODEL_PATH))
                logger.info("[MODEL] global model saved to %s", LOCAL_MODEL_PATH)
            except Exception as save_exc:
                logger.warning("[MODEL] failed to save global model locally: %s", save_exc)

            y_prob   = booster.predict(xgb.DMatrix(X_test))
            preds    = (y_prob > 0.5).astype(int)
            accuracy = float((preds == y_test).mean())
            # Binary cross-entropy (log-loss) — lets Flower track model improvement.
            eps  = 1e-7
            loss = float(-np.mean(
                y_test * np.log(y_prob + eps) + (1 - y_test) * np.log(1 - y_prob + eps)
            ))
            logger.info("[EVAL] round=%s test_samples=%d accuracy=%.3f loss=%.4f",
                        config.get("server_round", "?"), len(X_test), accuracy, loss)
            # Heartbeat on successful round completion — server trigger reads this
            # to know the gateway is alive and what state its buffer is in.
            _write_gw_status_heartbeat(last_round=config.get("server_round"))
            return loss, len(X_test), {"accuracy": accuracy}
        except Exception as exc:
            logger.warning("[EVAL] evaluation failed: %s", exc)
            return 0.0, 1, {"accuracy": 0.0}


def client_fn(context: fl.common.Context):
    # Startup heartbeat bootstraps the FL trigger: round 1 can fire even before
    # any FL has occurred, as long as one parquet exists in the buffer.
    try:
        _write_gw_status_heartbeat(last_round=None)
    except Exception as exc:
        # Never block joining a round on a heartbeat failure.
        logger.warning("[GW_STATUS] startup heartbeat skipped: %s", exc)
    return PLUDOSClient().to_client()


app = fl.client.ClientApp(client_fn=client_fn)
