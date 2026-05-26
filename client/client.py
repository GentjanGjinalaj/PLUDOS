"""
PLUDOS AI Worker: Federated Learning Client
-------------------------------------------
Runs on the Jetson Orin Nano. Responsibilities:
1. Load all recent mission Parquet files from the RAM buffer (concatenated).
2. Train an XGBoost model locally on the NVIDIA GPU.
3. Profile energy consumption per FL phase via AlumetProfiler.
4. Stream energy telemetry to InfluxDB (fl_energy, fl_phases measurements).
5. Evaluate the server's global model on a local held-out test set.

n_estimators is set by the server each round via fit_config() based on the
previous round's measured energy — this closes the energy-aware FL loop (ADR-014).
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
from pathlib import Path

from influxdb_client import InfluxDBClient, Point, WritePrecision  # type: ignore
from influxdb_client.client.write_api import SYNCHRONOUS           # type: ignore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==========================================
# 1. ENVIRONMENT & HARDWARE CONFIGURATION
# ==========================================
TEST_MODE  = os.getenv("TEST_MODE") == "1"
BUFFER_DIR = "./ram_buffer" if TEST_MODE else "/app/ram_buffer"
DEVICE     = "cpu" if TEST_MODE else "cuda"

# Phase 2 relay file path — written by alumet-relay probe.py (now dormant).
# Leave empty to fall back to tegrastats. Will be replaced by a Prometheus
# query once alumet-relay --output prometheus metric names are confirmed on hardware.
ALUMET_RELAY_METRICS_FILE = os.getenv("ALUMET_RELAY_METRICS_FILE", "")

# Maximum Parquet files to concatenate per FL round. Ensures mid-mission buffer-
# pressure flushes are included rather than training on just the latest tail file.
MAX_PARQUET_FILES = int(os.getenv("MAX_PARQUET_FILES", "20"))

# Anomaly classification threshold for the Z-axis accelerometer channel (g).
# Default 2.0g: gravity alone reads ~1.0g, so 2.0g catches genuine shocks only.
# 0.8g (old default) was below gravity — flagged every sample as anomalous.
# IMPORTANT: uncalibrated — must be validated against labelled fault data from
# Savoye before any thesis accuracy claim. See docs/future_options.md §3.3.
ANOMALY_THRESHOLD_G = float(os.getenv("ANOMALY_THRESHOLD_G", "2.0"))

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
    # Returns zeros on any failure — caller always gets a safe dict.
    try:
        r = subprocess.run(
            ["tegrastats", "--interval", "100", "--count", "1"],
            capture_output=True, text=True, timeout=3,
        )
        line = r.stdout.strip()
        gpu = int(m.group(1)) if (m := re.search(r"VDD_GPU\S*\s+(\d+)mW", line)) else 0
        cpu = int(m.group(1)) if (m := re.search(r"VDD_CPU\S*\s+(\d+)mW", line)) else 0
        soc = int(m.group(1)) if (m := re.search(r"VDD_SOC\S*\s+(\d+)mW", line)) else 0
        return {"gpu": gpu / 1000.0, "cpu": cpu / 1000.0, "total": (gpu + cpu + soc) / 1000.0}
    except Exception:
        return {"gpu": 0.0, "cpu": 0.0, "total": 0.0}


def _read_relay_metrics() -> dict[str, float] | None:
    # Reads INA3221 data from the shared file written by alumet-relay probe.py.
    # probe.py is currently dormant; returns None until revived or replaced by
    # a Prometheus endpoint query (alumet --output prometheus).
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

        point = (
            Point("fl_phases")
            .tag("device",   self.device_name)
            .tag("fl_round", str(self.round_num))
            .tag("phase",    phase)
            .tag("nvpmodel", self.nvpmodel)
            .field("fl_round_int", self._round_int)
            .field("duration_ms",  duration_ms)
            .field("energy_j",     delta_e)
            .field("avg_power_w",  avg_power)
            .time(time.time_ns(), WritePrecision.NS)
        )
        try:
            self.write_api.write(bucket=self.bucket, record=point)
            logger.info(
                "[ALUMET] phase=%-12s dur=%.0fms energy=%.3fJ avg=%.2fW",
                phase, duration_ms, delta_e, avg_power,
            )
        except Exception as exc:
            logger.error("[ALUMET] fl_phases write failed (phase=%s): %s", phase, exc)

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
            else:
                # Phase 2: INA3221 via relay file; Phase 1 fallback is tegrastats.
                pw = _read_relay_metrics() or _read_tegrastats()

            # Atomic float update — safe under CPython GIL for this access pattern.
            self._energy_j += pw["total"] * elapsed

            point = (
                Point("fl_energy")
                .tag("device",   self.device_name)
                .tag("fl_round", str(self.round_num))
                .tag("nvpmodel", self.nvpmodel)
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

    # Anomaly label: Z-axis > ANOMALY_THRESHOLD_G — high vertical shock.
    y_train = (df.loc[df_clean.index, "accel_z"] > ANOMALY_THRESHOLD_G).astype(int).values

    return X_train, y_train


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

            preds    = (booster.predict(xgb.DMatrix(X_test)) > 0.5).astype(int)
            accuracy = float((preds == y_test).mean())
            logger.info("[EVAL] round=%s test_samples=%d accuracy=%.3f",
                        config.get("server_round", "?"), len(X_test), accuracy)
            # Heartbeat on successful round completion — server trigger reads this
            # to know the gateway is alive and what state its buffer is in.
            _write_gw_status_heartbeat(last_round=config.get("server_round"))
            return 0.0, len(X_test), {"accuracy": accuracy}
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
