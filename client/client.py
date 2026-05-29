"""
PLUDOS AI Worker: Federated Learning Client (T5.3 — Flower optional layer)
---------------------------------------------------------------------------
Runs on the Jetson Orin Nano. Responsibilities:
1. Load all recent mission Parquet files from the RAM buffer (concatenated).
2. Generate anomaly labels via the selected backend (ANOMALY_MODEL env var) —
   delegated to anomaly.py (no Flower dependency there).
3. Train an XGBoost classifier on those labels (federated via Flower in
   federated mode; local retrain loop in standalone mode).
4. Profile energy consumption per FL phase via AlumetProfiler.
5. Stream energy telemetry to InfluxDB (fl_energy, fl_phases measurements).
6. Evaluate the server's global model on a local held-out test set.

Deployment mode (PLUDOS_MODE env var):
  federated  — default; Flower ClientApp registered with the SuperLink.
  standalone — local retrain loop every STANDALONE_RETRAIN_INTERVAL_S seconds;
               no Flower, writes model to LOCAL_MODEL_PATH.
  headless   — data-engine only (data-engine.py handles this; ai-worker not started).

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
import urllib.request
from pathlib import Path

from influxdb_client import InfluxDBClient, Point, WritePrecision  # type: ignore
from influxdb_client.client.write_api import SYNCHRONOUS           # type: ignore

# Pure inference module — no Flower dependency (T5.3).
from anomaly import load_buffered_data, _make_anomaly_labels

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==========================================
# 1. ENVIRONMENT & HARDWARE CONFIGURATION
# ==========================================
TEST_MODE  = os.getenv("TEST_MODE") == "1"
BUFFER_DIR = os.getenv("BUFFER_DIR", "./ram_buffer" if TEST_MODE else "/app/ram_buffer")


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

# Deployment mode — controls whether Flower or the local retrain loop runs.
#   federated  — default: register with SuperLink, Flower manages rounds.
#   standalone — local retrain loop every STANDALONE_RETRAIN_INTERVAL_S; no Flower.
#   headless   — data-engine only; ai-worker should not be started in this mode.
PLUDOS_MODE = os.getenv("PLUDOS_MODE", "federated")

# Retraining cadence in standalone mode (default 30 min).
STANDALONE_RETRAIN_INTERVAL_S = int(os.getenv("STANDALONE_RETRAIN_INTERVAL_S", "1800"))

# Alumet-relay Prometheus endpoint — scraped by AlumetProfiler for real INA3221 power.
# pludos-alumet-relay publishes port 9095; pludos-data-engine uses network_mode: host
# so localhost:9095 inside the container reaches the host's published port directly.
ALUMET_PROMETHEUS_URL = os.getenv("ALUMET_PROMETHEUS_URL", "http://localhost:9095/metrics")

# Controls which energy source is accepted during FL rounds.
#   "alumet"     — hard requirement; round aborts with ERROR if Alumet scrape fails or returns 0.
#   "tegrastats"  — use tegrastats only (debug/no-Alumet-relay mode).
#   "auto"        — legacy: relay → alumet → tegrastats fallback, tagged in InfluxDB.
ENERGY_SOURCE_REQUIRED = os.getenv("ENERGY_SOURCE_REQUIRED", "alumet")

# Maximum Parquet files per FL round — also used by the standalone retrain loop.
# Defined here for use in _write_gw_status_heartbeat(); anomaly.py reads the same env var.
MAX_PARQUET_FILES = int(os.getenv("MAX_PARQUET_FILES", "20"))

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
        # T4.2: monotonic timestamp of last [ENERGY][DEGRADED] warn — throttles to 60 s.
        self._last_degraded_warn_t: float = 0.0

        # Active phase snapshots: phase_name → (start_monotonic, energy_j_at_start)
        self._phase_snapshots: dict[str, tuple[float, float]] = {}

        # fl_round_int for Flux range queries — 0 if round_num is non-numeric.
        self._round_int: int = int(round_num) if str(round_num).isdigit() else 0

        influx_url    = os.getenv("INFLUXDB_URL",    "http://127.0.0.1:8086")
        influx_token  = os.getenv("INFLUXDB_TOKEN",  "pludos-dev-token")
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
                pw = _read_alumet_prometheus()
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
                # T4.2: warn every 60 s so ops knows tegrastats is the active source.
                if time.monotonic() - self._last_degraded_warn_t >= 60.0:
                    logger.warning("[ENERGY][DEGRADED] source=tegrastats (ENERGY_SOURCE_REQUIRED=%s)",
                                   ENERGY_SOURCE_REQUIRED)
                    self._last_degraded_warn_t = time.monotonic()
            else:  # auto — legacy fallback chain, tagged for Grafana visibility
                pw = _read_alumet_prometheus()
                if pw is not None:
                    self._energy_source = "alumet"
                else:
                    pw = _read_tegrastats()
                    self._energy_source = "tegrastats_fallback"
                    # T4.2: throttled DEGRADED warn; not a round-abort (auto mode).
                    if time.monotonic() - self._last_degraded_warn_t >= 60.0:
                        logger.warning("[ENERGY][DEGRADED] alumet unavailable — falling back to tegrastats")
                        self._last_degraded_warn_t = time.monotonic()

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
# load_buffered_data / _make_anomaly_labels* / label_packets imported from anomaly.py


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
    influx_token  = os.getenv("INFLUXDB_TOKEN",  "pludos-dev-token")
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


def _write_train_metrics(
    round_num, n_estimators: int, total_trees: int,
    train_logloss: float, n_samples: int, anomaly_rate: float, labeller: str,
) -> None:
    # Write per-round XGBoost metrics for T8.1 convergence study. Best-effort.
    influx_url   = os.getenv("INFLUXDB_URL",    "http://127.0.0.1:8086")
    influx_token = os.getenv("INFLUXDB_TOKEN",  "pludos-dev-token")
    influx_org   = os.getenv("INFLUXDB_ORG",    "pludos")
    influx_bucket= os.getenv("INFLUXDB_BUCKET", "alumet_energy")
    try:
        client = InfluxDBClient(url=influx_url, token=influx_token, org=influx_org)
        try:
            point = (
                Point("fl_train_metrics")
                .tag("gateway_id", GATEWAY_ID)
                .tag("fl_round",   str(round_num))
                .tag("labeller",   labeller)
                .field("n_estimators",  n_estimators)
                .field("total_trees",   total_trees)
                .field("train_logloss", train_logloss)
                .field("n_samples",     n_samples)
                .field("anomaly_rate",  anomaly_rate)
                .time(time.time_ns(), WritePrecision.NS)
            )
            client.write_api(write_options=SYNCHRONOUS).write(bucket=influx_bucket, record=point)
            logger.info(
                "[TRAIN_METRICS] round=%s n_est=%d total_trees=%d logloss=%.4f samples=%d anomaly_rate=%.3f",
                round_num, n_estimators, total_trees, train_logloss, n_samples, anomaly_rate,
            )
        finally:
            client.close()
    except Exception as exc:
        logger.warning("[TRAIN_METRICS] write failed: %s", exc)


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
            X_train, y_train, labeller = load_buffered_data()
            profiler.end_phase("load")

            # Phase: training — GPU bound, dominant energy consumer.
            # T3.6: warm-start from server's merged global model received in evaluate()
            # last round. Continues tree growth rather than retraining from scratch.
            xgb_model_arg = str(LOCAL_MODEL_PATH) if LOCAL_MODEL_PATH.exists() else None
            if xgb_model_arg:
                logger.info("[WARMSTART] continuing from global model (%s)", LOCAL_MODEL_PATH)
            logger.info(
                "Training XGBoost (device=%s, n_estimators=%d, labeller=%s) for round %s",
                DEVICE, n_estimators, labeller, round_num,
            )
            profiler.begin_phase("train")
            # Artificial sleep in TEST_MODE ensures enough InfluxDB points for Grafana.
            if TEST_MODE:
                time.sleep(1.5)
            model = xgb.XGBClassifier(n_estimators=n_estimators, tree_method="hist", device=DEVICE,
                                      eval_metric="logloss")
            model.fit(X_train, y_train, xgb_model=xgb_model_arg,
                      eval_set=[(X_train, y_train)], verbose=False)
            total_trees  = model.get_booster().num_boosted_rounds()
            evals        = model.evals_result()
            train_logloss = evals.get("validation_0", {}).get("logloss", [float("nan")])[-1]
            logger.info("[WARMSTART] local booster: %d boosted rounds, logloss=%.4f",
                        total_trees, train_logloss)
            profiler.end_phase("train")
        finally:
            # Always record round_total — even if load or train raised.
            # This ensures fl_phases gets a data point for budget adaptation.
            profiler.end_phase("round_total")
            profiler.stop()

        if model is None:
            raise RuntimeError(f"Round {round_num}: training did not complete; check buffer.")

        # Write XGBoost training metrics to InfluxDB for T8.1 convergence study.
        _write_train_metrics(round_num, n_estimators, total_trees, train_logloss,
                             len(X_train), float(y_train.mean()) if len(y_train) else 0.0, labeller)

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
            X, y, _ = load_buffered_data()
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
                logger.info("[MODEL] global model saved to %s (%d rounds)",
                            LOCAL_MODEL_PATH, booster.num_boosted_rounds())
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


def _wait_for_alumet(timeout_s: int = 30) -> bool:
    # Poll ALUMET_PROMETHEUS_URL until a 200 OK is received or timeout expires.
    # Returns True if healthy, False if the endpoint never responds in time.
    deadline = time.monotonic() + timeout_s
    warned   = False
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(ALUMET_PROMETHEUS_URL, timeout=1) as r:
                if r.status == 200:
                    logger.info("[ENERGY] alumet ready at %s", ALUMET_PROMETHEUS_URL)
                    return True
        except Exception:
            pass
        if not warned:
            logger.warning("[ENERGY] alumet not ready; waiting up to %ds (%s)",
                           timeout_s, ALUMET_PROMETHEUS_URL)
            warned = True
        time.sleep(2)
    return False


def client_fn(context: fl.common.Context):
    # Startup heartbeat bootstraps the FL trigger: round 1 can fire even before
    # any FL has occurred, as long as one parquet exists in the buffer.
    try:
        _write_gw_status_heartbeat(last_round=None)
    except Exception as exc:
        # Never block joining a round on a heartbeat failure.
        logger.warning("[GW_STATUS] startup heartbeat skipped: %s", exc)

    # T4.1: gate on alumet readiness when it is the required source. Skip in
    # TEST_MODE (no relay running) and "auto"/"tegrastats" (fallbacks accepted).
    if not TEST_MODE and ENERGY_SOURCE_REQUIRED not in ("auto", "tegrastats"):
        if not _wait_for_alumet(30):
            raise RuntimeError(
                "[ENERGY] alumet unavailable after 30 s — refusing to register with Flower"
            )

    return PLUDOSClient().to_client()


app = fl.client.ClientApp(client_fn=client_fn)


# ==========================================
# 4. STANDALONE MODE (T5.4)
# ==========================================

def _run_standalone_loop() -> None:
    # Local retrain loop for standalone mode: no Flower, no server required.
    # Retrains XGBoost every STANDALONE_RETRAIN_INTERVAL_S seconds on the
    # most recent buffer data and persists the model to LOCAL_MODEL_PATH.
    logger.info(
        "[STANDALONE] mode=standalone interval=%ds model=%s",
        STANDALONE_RETRAIN_INTERVAL_S, LOCAL_MODEL_PATH,
    )
    while True:
        try:
            X, y, labeller = load_buffered_data()
            model = xgb.XGBClassifier(
                n_estimators=N_ESTIMATORS_DEFAULT,
                tree_method="hist",
                device=DEVICE,
            )
            model.fit(X, y)
            LOCAL_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
            model.get_booster().save_model(str(LOCAL_MODEL_PATH))
            logger.info(
                "[STANDALONE] retrained labeller=%s samples=%d saved=%s",
                labeller, len(X), LOCAL_MODEL_PATH,
            )
        except FileNotFoundError as exc:
            logger.warning("[STANDALONE] no data yet: %s — retrying in %ds",
                           exc, STANDALONE_RETRAIN_INTERVAL_S)
        except Exception as exc:
            logger.error("[STANDALONE] retrain failed: %s", exc)
        time.sleep(STANDALONE_RETRAIN_INTERVAL_S)


if __name__ == "__main__":
    if PLUDOS_MODE == "standalone":
        _run_standalone_loop()
    else:
        # federated: Flower manages execution via the ClientApp registered above.
        logger.info("[CLIENT] PLUDOS_MODE=%s — waiting for Flower SuperNode", PLUDOS_MODE)
