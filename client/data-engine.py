"""
PLUDOS Edge Gateway: Data Engine (ADR-016 v3 — compact 24-byte stream)
-----------------------------------------------------------------------
Listens on a single raw UDP socket:
  - UDP 5683: PludosTelemetry packets from each STM32 shuttle
    (24 bytes — uint8 id + uint16 seq + uint32 tick + uint8 state +
     8×int16 sensors: accel xyz, gyro xyz, temp, humidity)
    Sensors scaled ×100 (g, dps, °C) or ×10 (%RH); 0x7FFF = unavailable.
    See `docs/wire_protocol.md §1`.

RAW-ONLY COLLECTION: the data engine is a pure raw collector. Parquet files
hold only non-recomputable signal — the raw accel/gyro/temp/humidity samples,
the state flag, a UTC timestamp and a packet-loss counter. All feature
engineering (magnitudes, jerk, tilt, rolling windows, distance, energy,
mission segmentation) is a TRAIN-TIME transform in `client/anomaly.py`, not a
per-packet gateway cost. This keeps Jetson CPU/SD usage minimal and leaves the
thesis free to pick any modelling direction from the raw signal later.

Every incoming packet is appended to that shuttle's in-memory buffer. A
mission is delimited by the `state` field: when a shuttle has been
streaming `state == MOVING` packets and then stays in `state == IDLE` for
`MISSION_END_IDLE_S` (default 30 s), the gateway flushes that shuttle's
buffer to a single Parquet file and writes a summary row to InfluxDB
`stm_mission`. A wall-clock time cap (`BUFFER_MAX_AGE_S`) force-flushes any
buffer that has been open too long, so no Parquet file can span hours if
mission-end detection fails to fire.

There is no CoAP and no second port. Packet loss is tolerated — the next
20 ms / 1 s sample arrives anyway. See `docs/decisions.md` ADR-015.
"""

import asyncio
import glob
import logging
import math
import os
import socket
import struct
import sys
import threading
import time
from datetime import datetime, timedelta

import pandas as pd
from influxdb_client import InfluxDBClient, Point, WritePrecision  # type: ignore
from influxdb_client.client.write_api import SYNCHRONOUS           # type: ignore

import drain_receiver  # high-rate capture drain receiver (ADR-020, UDP 5684)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("data-engine")

# ---------------------------------------------------------------------------
# Configuration — all values come from environment variables with safe defaults
# ---------------------------------------------------------------------------

# Deployment mode — mirrors client.py; headless skips InfluxDB mission writes.
#   standalone — Jetson-local InfluxDB; InfluxDB writes go to localhost.
#   headless   — collect only; Parquet is written but InfluxDB writes are skipped.
#   federated  — default; InfluxDB on the central server via Tailscale.
PLUDOS_MODE     = os.getenv("PLUDOS_MODE", "federated")

TEST_MODE       = os.getenv("TEST_MODE") == "1"
TELEMETRY_PORT  = int(os.getenv("TELEMETRY_PORT", os.getenv("COAP_PORT", "5683")))

# ---------------------------------------------------------------------------
# Buffer limits — hard-coded defaults vs RAM-percentage auto mode.
#
# Default strategy (SHUTTLE_LIMIT_MODE unset or "fixed"):
#   Hard packet counts sized for 50 Hz MOVING TX rate on the Jetson 8 GB.
#   At ~300 B Python dict overhead per packet:
#     SHUTTLE_SOFT_LIMIT=3000  → ~1 min of MOVING before proactive flush (≈0.9 MB RAM)
#     SHUTTLE_HARD_LIMIT=4500  → ~1.5 min before emergency flush          (≈1.4 MB RAM)
#     GATEWAY_HARD_LIMIT=100000→ safety ceiling across all shuttles       (≈30 MB RAM)
#   These defaults produce at most one Parquet write per ~1-minute mission
#   segment — far fewer eMMC writes than the old 1000/1500 limits.
#
# Auto strategy (SHUTTLE_LIMIT_MODE=auto):
#   Reads available RAM via psutil at startup and sizes limits as fractions
#   of available memory. Useful when deploying on non-Jetson hardware.
#   Falls back to fixed defaults if psutil is not installed.
#   Fractions: per-shuttle soft = 0.02% of avail RAM, hard = 1.5×soft,
#              gateway ceiling = 0.3% of avail RAM.
#
# Override any limit explicitly via env var regardless of mode.
# ---------------------------------------------------------------------------

_PACKET_BYTES_EST = 300  # conservative Python dict overhead per in-memory packet

def _compute_auto_limits() -> tuple[int, int, int]:
    """Derive buffer limits from available RAM (psutil). Returns (soft, hard, gateway)."""
    try:
        import psutil  # type: ignore[import-untyped]  # optional dep, container-only
        avail = psutil.virtual_memory().available
        soft    = max(3000, int(avail * 0.0002 / _PACKET_BYTES_EST))
        hard    = max(4500, int(soft * 1.5))
        gateway = max(100000, int(avail * 0.003 / _PACKET_BYTES_EST))
        logger.info(
            "[CONFIG] auto limits from %.1f GB available RAM: soft=%d hard=%d gateway=%d",
            avail / 1e9, soft, hard, gateway,
        )
        return soft, hard, gateway
    except ImportError:
        logger.warning("[CONFIG] psutil not installed — falling back to fixed defaults")
        return 3000, 4500, 100000

_LIMIT_MODE = os.getenv("SHUTTLE_LIMIT_MODE", "fixed").lower()
if _LIMIT_MODE == "auto" and "SHUTTLE_SOFT_LIMIT" not in os.environ:
    _auto_soft, _auto_hard, _auto_gw = _compute_auto_limits()
else:
    _auto_soft, _auto_hard, _auto_gw = 3000, 4500, 100000

SHUTTLE_SOFT_LIMIT = int(os.getenv("SHUTTLE_SOFT_LIMIT", str(_auto_soft)))
SHUTTLE_HARD_LIMIT = int(os.getenv("SHUTTLE_HARD_LIMIT", str(_auto_hard)))
GATEWAY_HARD_LIMIT = int(os.getenv("GATEWAY_HARD_LIMIT", str(_auto_gw)))

if SHUTTLE_HARD_LIMIT <= SHUTTLE_SOFT_LIMIT:
    raise ValueError(
        f"SHUTTLE_HARD_LIMIT ({SHUTTLE_HARD_LIMIT}) must be > "
        f"SHUTTLE_SOFT_LIMIT ({SHUTTLE_SOFT_LIMIT})"
    )

# Re-anchor NTP offset every N packets per shuttle to correct STM32 crystal drift.
NTP_REFRESH_INTERVAL = int(os.getenv("NTP_REFRESH_INTERVAL", "100"))
# Also re-anchor if more than this many seconds have elapsed since the last anchor,
# regardless of packet count. Prevents 16-minute drift gaps during IDLE at 0.1 Hz.
NTP_REFRESH_MAX_S = float(os.getenv("NTP_REFRESH_MAX_S", "60"))

# Mission-end detection — after this many seconds of state==IDLE following
# any state==MOVING run, flush the shuttle's buffer as one Parquet file.
MISSION_END_IDLE_S = float(os.getenv("MISSION_END_IDLE_S", "30"))

# Wall-clock time cap on any open buffer. Safety net against the case where
# mission-end detection fails to fire (e.g. spurious MOVING resetting the IDLE
# timer): no Parquet file may span more than this many seconds. Force-flushes
# as a mid-mission pressure flush without resetting mission state.
BUFFER_MAX_AGE_S = float(os.getenv("BUFFER_MAX_AGE_S", "300"))

# Per-second status log: roll up "tx rate" and last-seen sensor values rather
# than logging every packet (10 Hz × 100 shuttles would drown the terminal).
STATUS_LOG_PERIOD_S = float(os.getenv("STATUS_LOG_PERIOD_S", "1.0"))

# Beacon broadcast: announces the gateway IP on UDP so STM32s can auto-discover it.
BEACON_PORT       = int(os.getenv("BEACON_PORT", "5000"))
BEACON_INTERVAL_S = float(os.getenv("BEACON_INTERVAL_S", "10"))
GATEWAY_IP        = os.getenv("GATEWAY_IP", "")

# Multi-Jetson deployment: SHUTTLE_GROUP pins this gateway to a specific subset of
# shuttle IDs so STMs from another Jetson's group don't accidentally bond here
# when multiple Jetsons beacon on the same WiFi. Comma-separated list of 1-based
# IDs (e.g., "1,2"). Empty string = accept all (single-Jetson dev default).
_SHUTTLE_GROUP_RAW = os.getenv("SHUTTLE_GROUP", "").strip()
SHUTTLE_GROUP: set[int] = set()
if _SHUTTLE_GROUP_RAW:
    for tok in _SHUTTLE_GROUP_RAW.split(","):
        tok = tok.strip()
        if tok.isdigit():
            SHUTTLE_GROUP.add(int(tok))

# Parquet output directory: use tmpfs mount when inside container, local dir in test.
_DEFAULT_BUFFER_DIR   = "./ram_buffer"
_CONTAINER_BUFFER_DIR = "/app/ram_buffer"
BUFFER_DIR = (
    _DEFAULT_BUFFER_DIR
    if TEST_MODE or not os.path.isdir("/app")
    else _CONTAINER_BUFFER_DIR
)
try:
    os.makedirs(BUFFER_DIR, exist_ok=True)
except PermissionError:
    logger.warning("Cannot write to %s, falling back to %s", BUFFER_DIR, _DEFAULT_BUFFER_DIR)
    BUFFER_DIR = _DEFAULT_BUFFER_DIR
    os.makedirs(BUFFER_DIR, exist_ok=True)

# InfluxDB connection for mission-summary writes (stm_mission measurement).
_INFLUXDB_URL    = os.getenv("INFLUXDB_URL",    "http://127.0.0.1:8086")
_INFLUXDB_TOKEN  = os.getenv("INFLUXDB_TOKEN",  "")
_INFLUXDB_ORG    = os.getenv("INFLUXDB_ORG",    "pludos")
_INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET", "alumet_energy")

# Human-readable gateway tag written to every stm_mission InfluxDB point.
_GATEWAY_TAG = os.getenv("JETSON_HOSTNAME", socket.gethostname())

# State enum mirrors the STM32 firmware.
STATE_IDLE   = 0
STATE_MOVING = 1

# Universal wire sentinel (ADR-016 v3): 0x7FFF (32767) in any int16 field means
# that sensor was unavailable. Gateway converts to NaN before buffering.
_SENSOR_SENTINEL_INT = 32767

# Final Parquet columns — RAW ONLY (store-raw, derive-at-train-time).
# Only non-recomputable signal is persisted: raw sensor samples, the state
# flag, a UTC timestamp, and a packet-loss counter. Every engineered feature
# (magnitudes, jerk, tilt, rolling stats, distance, energy, segmentation) is
# derived at train time in client/anomaly.py — never on the gateway.
# Intermediate fields (tick_ms, seq_wire, timestamp_ms) are dropped at flush time.
_PARQUET_COLS = [
    "timestamp",    # pd.Timestamp UTC — anchored STM32 tick via per-shuttle NTP offset
    "shuttle_id",   # int8    — 1-based integer matching wifi_credentials.h SHUTTLE_ID
    "seq",          # int32   — uint16 wire counter unwrapped across rollovers; sort key
    "seq_gap",      # int16   — packets dropped before this row; 0=no loss (data-quality QA)
    "state",        # int8    — 0=IDLE, 1=MOVING
    # Accelerometer (ISM330DHCX, ±2 g FS) — physical units, float16 (NaN if unavailable)
    "accel_x",      # float16 g — X axis
    "accel_y",      # float16 g — Y axis
    "accel_z",      # float16 g — Z axis
    # Gyroscope (ISM330DHCX, ±250 dps FS) — physical units, float16
    "gyro_x",       # float16 dps — roll rate
    "gyro_y",       # float16 dps — pitch rate
    "gyro_z",       # float16 dps — yaw rate
    # Environment (HTS221)
    "temp_c",       # float16 °C — NaN when unavailable
    "humidity_pct", # float16 %  — HTS221 RH; NaN when unavailable
]

# ---------------------------------------------------------------------------
# Wire format — must match wire_protocol.md §1 exactly.
# ---------------------------------------------------------------------------

# 24-byte compact format (ADR-016 v3): id(1)+seq(2)+tick(4)+state(1)+8×int16(16)
# Sensors: accel xyz ×100 g, gyro xyz ×100 dps, temp ×100 °C, humidity ×10 %RH.
TELEMETRY_FMT  = "<BHIBhhhhhhhh"
TELEMETRY_SIZE = struct.calcsize(TELEMETRY_FMT)
assert TELEMETRY_SIZE == 24, f"telemetry fmt must be 24 bytes, got {TELEMETRY_SIZE}"

# ---------------------------------------------------------------------------
# Shuttle identity — maps the 1-byte wire ID to a human-readable name.
# Set SHUTTLE_NAMES="1:STM32-Alpha,2:STM32-Beta" in .env to override.
# ---------------------------------------------------------------------------

def _parse_shuttle_names(env_val: str) -> dict[int, str]:
    # Parse "1:Name-A,2:Name-B" → {1: "Name-A", 2: "Name-B"}
    result: dict[int, str] = {}
    for entry in env_val.split(","):
        entry = entry.strip()
        if ":" in entry:
            num, name = entry.split(":", 1)
            try:
                result[int(num)] = name.strip()
            except ValueError:
                pass
    return result

# Default is empty — unmapped IDs fall through to "shuttle-{n}" in _unpack_telemetry.
# Override with SHUTTLE_NAMES="1:shuttle-1,2:shuttle-2" in .env when custom labels are needed.
SHUTTLE_NAMES: dict[int, str] = _parse_shuttle_names(os.getenv("SHUTTLE_NAMES", ""))

# ---------------------------------------------------------------------------
# Mutable gateway state — per-shuttle dicts, single-threaded asyncio.
# ---------------------------------------------------------------------------

# All telemetry packets waiting for Parquet flush, keyed by shuttle_id.
_telemetry_buf: dict[str, list[dict]] = {}

# Monotonic time the current (non-empty) buffer was opened. Drives the
# BUFFER_MAX_AGE_S time cap; reset whenever a flush empties the buffer.
_buffer_open_wall: dict[str, float] = {}

# Wall-clock time of the first packet of the current mission per shuttle.
_mission_start_wall: dict[str, float] = {}

# Per-shuttle NTP offset (ms). Set on first packet; refreshed every NTP_REFRESH_INTERVAL.
_ntp_offsets: dict[str, int] = {}

# Per-shuttle packet counter driving the periodic NTP offset refresh.
_packet_counts: dict[str, int] = {}

# Wall-clock time (monotonic) of the most recent NTP re-anchor per shuttle.
# Used with NTP_REFRESH_MAX_S to cap drift during low-rate IDLE periods.
_ntp_anchor_wall: dict[str, float] = {}

# Per-shuttle wall-clock time of the last received packet.
_last_packet_wall: dict[str, float] = {}

# Per-shuttle wall-clock time the most recent state==MOVING packet was seen.
# None until the first MOVING packet for the current mission.
_last_moving_wall: dict[str, float | None] = {}

# Per-shuttle last seen sequence_id — used to detect uint16 wrap.
_last_seq_ids: dict[str, int] = {}

# Per-shuttle wrap count: incremented each time sequence_id wraps 65535 → 0.
_seq_wrap_counts: dict[str, int] = {}

# Per-shuttle latest sensor snapshot for the per-second status log.
_last_sample: dict[str, dict] = {}

# Per-shuttle running count of packets received in the current STATUS_LOG_PERIOD_S window.
_tx_rate_window: dict[str, int] = {}

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _finalize(df: pd.DataFrame) -> pd.DataFrame:
    """Add timestamp + packet-loss counter, cast to compact dtypes, return raw-only columns.
    df must be sorted by (shuttle_id, seq). No feature engineering — that is a
    train-time transform in client/anomaly.py (store-raw, derive-at-train-time)."""

    # Anchor STM32 relative tick to gateway NTP wall clock → proper UTC Timestamp.
    df["timestamp"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)

    # seq_gap: packets dropped before each row. diff()=1 means consecutive, so sub(1).
    # Clamp negative (reorder edge case) to 0. First row = 0 (no prior to compare).
    df["seq_gap"] = df["seq"].diff().sub(1).clip(lower=0).fillna(0).astype("int16")

    # Round to wire precision, then downcast sensors to float16 (2 B, finer than the
    # ×100 wire quantisation for accel; negligible loss for gyro/temp/humidity).
    sensor_cols = ("accel_x", "accel_y", "accel_z",
                   "gyro_x", "gyro_y", "gyro_z", "temp_c")
    df[list(sensor_cols)] = df[list(sensor_cols)].round(2)
    df["humidity_pct"]    = df["humidity_pct"].round(1)

    # Compact dtypes: int for identity/state/loss, float16 for sensors.
    df["shuttle_id"] = df["shuttle_id"].astype("int8")
    df["state"]      = df["state"].astype("int8")
    df["seq"]        = df["seq"].astype("int32")
    for col in sensor_cols:
        df[col] = df[col].astype("float16")
    df["humidity_pct"] = df["humidity_pct"].astype("float16")

    return df[_PARQUET_COLS]


def _flush(buf: list[dict], prefix: str) -> None:
    """Finalize raw columns, write clean Parquet atomically, clear buf."""
    if not buf:
        return
    df = pd.DataFrame(buf)
    df.sort_values(by=["shuttle_id", "seq"], inplace=True)
    df = _finalize(df)

    shuttle_id_val = int(df["shuttle_id"].iloc[0])
    shuttle_label  = f"shuttle-{shuttle_id_val}"
    # Include shuttle_id and millisecond timestamp in filename so concurrent flushes from
    # different shuttles (or mid-mission pressure flushes for the same shuttle) never collide.
    ts_ms     = int(time.time() * 1000)
    file_path = os.path.join(BUFFER_DIR, f"{prefix}_s{shuttle_id_val}_{ts_ms}.parquet")
    tmp_path  = file_path + ".tmp"
    # zstd compression: 40-60% smaller files than snappy with similar write speed.
    # level=3 balances compression ratio and CPU cost on the Jetson Cortex-A78.
    # PyArrow write is sync but only fires on flush — acceptable latency spike.
    df.to_parquet(tmp_path, engine="pyarrow", index=False,
                  compression="zstd", compression_level=3)
    os.replace(tmp_path, file_path)  # atomic rename: crash-safe on Linux
    logger.info("[%s] Flushed %d records → %s (zstd)", shuttle_label, len(df), file_path)
    buf.clear()


def _write_mission_summary(
    shuttle_id: str,
    packets: int,
    duration_ms: float,
) -> None:
    # Fire-and-forget background thread so the asyncio loop is never blocked by InfluxDB I/O.
    def _write() -> None:
        client = InfluxDBClient(url=_INFLUXDB_URL, token=_INFLUXDB_TOKEN, org=_INFLUXDB_ORG)
        try:
            point = (
                Point("stm_mission")
                .tag("shuttle_id", shuttle_id)
                .tag("gateway",    _GATEWAY_TAG)
                .field("packets",     packets)
                .field("duration_ms", duration_ms)
                .time(time.time_ns(), WritePrecision.NS)
            )
            client.write_api(write_options=SYNCHRONOUS).write(
                bucket=_INFLUXDB_BUCKET, record=point
            )
            logger.info(
                "[INFLUXDB] stm_mission shuttle=%s pkts=%d dur=%.0fms",
                shuttle_id, packets, duration_ms,
            )
        except Exception as exc:
            logger.warning("[INFLUXDB] stm_mission write failed (%s): %s", shuttle_id, exc)
        finally:
            client.close()

    threading.Thread(target=_write, daemon=True).start()


# Write one idle snapshot's accel/gyro waveform to Influx (stm_idle_wave), one point
# per sample in physical units, timed off the anchored mission t0 at the snapshot ODR.
def _write_idle_waveform(write_api, sid: str, summary: dict) -> None:
    accel = summary["accel_xyz"]
    gyro  = summary.get("gyro_xyz") or []
    t0_ms = int(summary["t0_wall_ms"])
    odr   = float(summary.get("odr_hz") or drain_receiver.IDLE_SNAP_ODR_HZ)
    step_ms = 1000.0 / odr if odr > 0 else 0.0
    a_lsb = drain_receiver.ACCEL_G_PER_LSB
    g_lsb = drain_receiver.GYRO_DPS_PER_LSB
    points = []
    for i, (ax, ay, az) in enumerate(accel):
        p = (Point("stm_idle_wave")
             .tag("shuttle_id", sid)
             .tag("gateway",    _GATEWAY_TAG)
             .field("ax_g", ax * a_lsb)
             .field("ay_g", ay * a_lsb)
             .field("az_g", az * a_lsb)
             .time(int((t0_ms + i * step_ms) * 1_000_000), WritePrecision.NS))
        if i < len(gyro):
            gx, gy, gz = gyro[i]
            p = (p.field("gx_dps", gx * g_lsb)
                  .field("gy_dps", gy * g_lsb)
                  .field("gz_dps", gz * g_lsb))
        points.append(p)
    if points:
        write_api.write(bucket=_INFLUXDB_BUCKET, record=points)


# Mirror one finalised high-rate drain (mission or idle snapshot) into InfluxDB as an
# stm_mission point so Grafana shows shuttle activity even though the live :5683 stream
# is off (ADR-021). High-rate waveforms stay in Parquet; only this summary goes to Influx.
def _write_drain_summary(summary: dict) -> None:
    # headless mode: collect to Parquet only, skip InfluxDB (mirrors _write_mission_summary).
    if PLUDOS_MODE == "headless":
        return
    sid = str(summary["shuttle_id"])

    # Fire-and-forget background thread so the asyncio loop is never blocked by InfluxDB I/O.
    def _write() -> None:
        client = InfluxDBClient(url=_INFLUXDB_URL, token=_INFLUXDB_TOKEN, org=_INFLUXDB_ORG)
        try:
            point = (
                Point("stm_mission")
                .tag("shuttle_id",  sid)
                .tag("gateway",     _GATEWAY_TAG)
                .tag("source",      "drain")
                .tag("kind",        "idle_snapshot" if summary["is_idle_snapshot"] else "mission")
                .field("mission_id",       int(summary["mission_id"]))
                .field("packets_total",    int(summary["packets_total"]))
                .field("packets_received", int(summary["packets_received"]))
                .field("packets_lost",     int(summary["packets_lost"]))
                .field("loss_pct",         float(summary["loss_pct"]))
                .field("accel_samples",    int(summary["accel_samples"]))
                .field("gyro_samples",     int(summary["gyro_samples"]))
                .field("complete",         bool(summary["complete"]))
                # Stamp the point at the anchored mission start (t0_wall_ms), NOT now —
                # drains arrive bursted, so wall-clock-of-arrival collapses spacing.
                .time(int(summary["t0_wall_ms"]) * 1_000_000, WritePrecision.NS)
            )
            # Vibration intensity (NaN for an empty stream — Influx rejects NaN floats).
            for fname, key in (("accel_rms_g", "accel_rms_g"),
                               ("accel_peak_g", "accel_peak_g"),
                               ("gyro_peak_dps", "gyro_peak_dps")):
                v = summary.get(key)
                if v is not None and not math.isnan(v):
                    point = point.field(fname, float(v))
            # Env stamp present on idle snapshots; absent (None) on high-rate missions.
            if summary.get("temp_c") is not None:
                point = point.field("temp_c", float(summary["temp_c"]))
            if summary.get("pressure_hpa") is not None:
                point = point.field("pressure_hpa", float(summary["pressure_hpa"]))

            write_api = client.write_api(write_options=SYNCHRONOUS)
            write_api.write(bucket=_INFLUXDB_BUCKET, record=point)

            # Idle snapshots are small — also write the per-sample accel/gyro waveform
            # (option B) so Grafana can plot rest vibration. High-rate missions stay
            # in Parquet only (too many samples for Influx).
            if summary.get("accel_xyz"):
                _write_idle_waveform(write_api, sid, summary)
            # recv/drain_loss describe the UDP drain from the STM, NOT the Influx write
            # (which either succeeds above or raises into the except below). Labelled
            # explicitly so the number isn't misread as a database write failure.
            logger.info(
                "[INFLUXDB] stm_mission(drain) written shuttle=%s m=%d kind=%s recv=%d/%d drain_loss=%.1f%%",
                sid, int(summary["mission_id"]),
                "idle" if summary["is_idle_snapshot"] else "mission",
                int(summary["packets_received"]), int(summary["packets_total"]),
                float(summary["loss_pct"]),
            )
        except Exception as exc:
            logger.warning("[INFLUXDB] stm_mission(drain) write failed (%s): %s", sid, exc)
        finally:
            client.close()

    threading.Thread(target=_write, daemon=True).start()


def _unpack_telemetry(raw: bytes) -> dict:
    """Unpack a 24-byte PludosTelemetry v3 packet. 0x7FFF (32767) in any int16 field → NaN."""
    sid_int, seq, tick, state, ax_r, ay_r, az_r, gx_r, gy_r, gz_r, temp_r, hum_r = \
        struct.unpack(TELEMETRY_FMT, raw)

    # 0x7FFF is the universal unavailable sentinel from firmware (out of range for all fields).
    nan = float("nan")
    ok_a = ax_r != _SENSOR_SENTINEL_INT
    ok_g = gx_r != _SENSOR_SENTINEL_INT
    ok_t = temp_r != _SENSOR_SENTINEL_INT
    ok_h = hum_r  != _SENSOR_SENTINEL_INT

    return {
        # --- Parquet columns (kept at flush) ---
        "shuttle_id":   sid_int,
        "state":        int(state),
        "accel_x":      ax_r / 100.0 if ok_a else nan,
        "accel_y":      ay_r / 100.0 if ok_a else nan,
        "accel_z":      az_r / 100.0 if ok_a else nan,
        "gyro_x":       gx_r / 100.0 if ok_g else nan,
        "gyro_y":       gy_r / 100.0 if ok_g else nan,
        "gyro_z":       gz_r / 100.0 if ok_g else nan,
        "temp_c":       temp_r / 100.0 if ok_t else nan,
        "humidity_pct": hum_r  / 10.0  if ok_h else nan,
        # --- Intermediate: consumed in datagram_received, dropped at flush ---
        "tick_ms":  tick,  # HAL_GetTick() ms since boot; anchored to UTC via NTP offset
        "seq_wire": seq,   # raw uint16 counter; used for wrap detection only
    }


def _reset_shuttle_state(shuttle_id: str) -> None:
    """Wipe all per-shuttle dicts — called on mission-end and ghost-shuttle cleanup."""
    _telemetry_buf.pop(shuttle_id, None)
    for store in (
        _buffer_open_wall, _mission_start_wall, _ntp_offsets,
        _last_packet_wall, _packet_counts, _last_seq_ids, _seq_wrap_counts,
        _last_moving_wall, _last_sample, _tx_rate_window,
    ):
        store.pop(shuttle_id, None)


def _maybe_flush_mission(shuttle_id: str, now: float) -> None:
    """If the shuttle has been IDLE for >= MISSION_END_IDLE_S after any MOVING run,
    flush its buffer to one Parquet file and reset all per-shuttle state."""
    last_moving = _last_moving_wall.get(shuttle_id)
    if last_moving is None:
        # No MOVING packet seen yet in this mission window — nothing to flush.
        return

    if (now - last_moving) < MISSION_END_IDLE_S:
        return

    pkts          = _packet_counts.get(shuttle_id, 0)
    started_wall  = _mission_start_wall.get(shuttle_id, now)
    duration_ms   = (now - started_wall) * 1000.0

    logger.info(
        "[%s] mission end (IDLE %.0fs) | pkts=%d | dur=%.0fms | flushing",
        shuttle_id, MISSION_END_IDLE_S, pkts, duration_ms,
    )

    # Pop the buffer before reset so _reset_shuttle_state's pop is a no-op.
    _flush(_telemetry_buf.pop(shuttle_id, []), "mission")
    # headless mode: skip InfluxDB write; Parquet is still written above.
    if PLUDOS_MODE != "headless":
        _write_mission_summary(shuttle_id, pkts, duration_ms)
    _reset_shuttle_state(shuttle_id)


# ---------------------------------------------------------------------------
# UDP protocol — handles PludosTelemetry on port 5683
# ---------------------------------------------------------------------------

class TelemetryProtocol(asyncio.DatagramProtocol):
    """Single-port asyncio datagram handler for PludosTelemetry from all shuttles."""

    def datagram_received(self, data: bytes, addr) -> None:
        if len(data) != TELEMETRY_SIZE:
            logger.debug(
                "Telemetry: bad size %d from %s (expected %d)", len(data), addr, TELEMETRY_SIZE
            )
            return

        try:
            pkt = _unpack_telemetry(data)
        except struct.error as exc:
            logger.warning("Telemetry unpack error from %s: %s", addr, exc)
            return

        # SHUTTLE_GROUP filter: defence-in-depth in case an STM32 bonded to the
        # wrong Jetson (or multiple Jetsons beacon on the same WiFi). Empty group
        # = accept all (single-Jetson dev default).
        sid_int = pkt["shuttle_id"]
        if SHUTTLE_GROUP and sid_int not in SHUTTLE_GROUP:
            logger.debug(
                "Telemetry: shuttle_id=%d not in SHUTTLE_GROUP=%s — dropping pkt from %s",
                sid_int, sorted(SHUTTLE_GROUP), addr,
            )
            return

        # shuttle_name: human-readable string key for all per-shuttle state dicts.
        # pkt["shuttle_id"] (integer) is the Parquet column; shuttle_name never appears in Parquet.
        shuttle_name = SHUTTLE_NAMES.get(sid_int, f"shuttle-{sid_int}")
        sequence_id  = pkt["seq_wire"]
        tick_ms      = pkt["tick_ms"]
        state        = pkt["state"]

        receipt_ms = int(time.time() * 1000)
        now        = time.monotonic()

        # First packet from this shuttle (since boot or since last mission flush):
        # establish the NTP offset and mark the mission start.
        if shuttle_name not in _ntp_offsets:
            _ntp_offsets[shuttle_name]        = receipt_ms - tick_ms
            _ntp_anchor_wall[shuttle_name]    = now
            _mission_start_wall[shuttle_name] = now
            logger.info(
                "[%s] NTP offset established: %d ms (state=%s)",
                shuttle_name, _ntp_offsets[shuttle_name],
                "MOVING" if state == STATE_MOVING else "IDLE",
            )

        # Drift correction: refresh when packet count hits a multiple of NTP_REFRESH_INTERVAL
        # OR when NTP_REFRESH_MAX_S seconds have elapsed (guards against IDLE at 0.1 Hz
        # where 100 packets = ~1000 s without the time-based trigger).
        _packet_counts[shuttle_name] = _packet_counts.get(shuttle_name, 0) + 1
        count = _packet_counts[shuttle_name]
        time_since_anchor = now - _ntp_anchor_wall.get(shuttle_name, now)
        if count % NTP_REFRESH_INTERVAL == 0 or time_since_anchor >= NTP_REFRESH_MAX_S:
            old_offset = _ntp_offsets[shuttle_name]
            _ntp_offsets[shuttle_name]     = receipt_ms - tick_ms
            _ntp_anchor_wall[shuttle_name] = now
            drift_ms = _ntp_offsets[shuttle_name] - old_offset
            logger.info(
                "[%s] NTP offset refreshed at pkt %d: %d ms (drift %+d ms)",
                shuttle_name, count, _ntp_offsets[shuttle_name], drift_ms,
            )

        pkt["timestamp_ms"] = tick_ms + _ntp_offsets[shuttle_name]

        # uint16 sequence wrap detection — STM32 counter rolls 65535 → 0.
        last_seq = _last_seq_ids.get(shuttle_name, sequence_id)
        if last_seq > 60000 and sequence_id < 5000:
            _seq_wrap_counts[shuttle_name] = _seq_wrap_counts.get(shuttle_name, 0) + 1
            logger.info(
                "[%s] sequence wrap #%d detected (was %d → %d)",
                shuttle_name, _seq_wrap_counts[shuttle_name], last_seq, sequence_id,
            )
        _last_seq_ids[shuttle_name] = sequence_id
        # Monotonic: unwraps uint16 rollovers so sort order is always globally correct.
        pkt["seq"] = sequence_id + _seq_wrap_counts.get(shuttle_name, 0) * 65536

        # Track last-packet wall time for status-log silence detection and the
        # ghost-shuttle watchdog (no longer used for energy integration).
        _last_packet_wall[shuttle_name] = now

        # Mission boundary tracking: any MOVING packet resets the IDLE timer.
        if state == STATE_MOVING:
            _last_moving_wall[shuttle_name] = now

        # Buffer the packet. Per-shuttle list so multi-shuttle deployments don't
        # interleave each other's missions in one Parquet file (P2-9 fix preserved).
        buf = _telemetry_buf.setdefault(shuttle_name, [])
        # Anchor the buffer-age clock when a fresh (empty) buffer opens — drives BUFFER_MAX_AGE_S.
        if not buf:
            _buffer_open_wall[shuttle_name] = now
        buf.append(pkt)
        _last_sample[shuttle_name]    = pkt
        _tx_rate_window[shuttle_name] = _tx_rate_window.get(shuttle_name, 0) + 1

        shuttle_pkts = len(_telemetry_buf[shuttle_name])
        total_pkts   = sum(len(v) for v in _telemetry_buf.values())

        # Mission-end via state transition is the normal path. We only check on
        # IDLE packets to avoid running the check 50× per second during MOVING.
        if state == STATE_IDLE:
            _maybe_flush_mission(shuttle_name, now)

        # Buffer-pressure flushes (mid-mission). These do not reset shuttle state —
        # the mission keeps streaming and the next batch lands in the next file.
        elif shuttle_pkts >= SHUTTLE_HARD_LIMIT:
            logger.warning(
                "[%s] per-shuttle HARD LIMIT (%d pkts) — mid-mission flush",
                shuttle_name, shuttle_pkts,
            )
            _flush(_telemetry_buf[shuttle_name], "mission")

        elif shuttle_pkts >= SHUTTLE_SOFT_LIMIT:
            logger.info(
                "[%s] per-shuttle soft limit (%d pkts) — proactive flush",
                shuttle_name, shuttle_pkts,
            )
            _flush(_telemetry_buf[shuttle_name], "mission")

        elif total_pkts >= GATEWAY_HARD_LIMIT:
            logger.error(
                "GATEWAY HARD LIMIT (%d total pkts across %d shuttles) — emergency flush all",
                total_pkts, len(_telemetry_buf),
            )
            for s_name, s_buf in list(_telemetry_buf.items()):
                _flush(s_buf, "mission")
            _telemetry_buf.clear()

    def error_received(self, exc: Exception) -> None:
        logger.error("Telemetry UDP socket error: %s", exc)


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

def _write_telemetry_batch(points: list) -> None:
    """Write per-shuttle live telemetry batch to InfluxDB for Grafana monitoring (1 Hz cadence)."""
    def _write() -> None:
        client = InfluxDBClient(url=_INFLUXDB_URL, token=_INFLUXDB_TOKEN, org=_INFLUXDB_ORG)
        try:
            client.write_api(write_options=SYNCHRONOUS).write(
                bucket=_INFLUXDB_BUCKET, record=points
            )
        except Exception as exc:
            logger.debug("[INFLUXDB] telemetry batch write failed: %s", exc)
        finally:
            client.close()
    threading.Thread(target=_write, daemon=True).start()


async def _status_log_task() -> None:
    """Once per STATUS_LOG_PERIOD_S, emit per-shuttle summary log and push live telemetry to InfluxDB."""
    while True:
        await asyncio.sleep(STATUS_LOG_PERIOD_S)
        now = time.monotonic()
        influx_points = []
        for sid in list(_last_sample.keys()):
            sample = _last_sample.get(sid)
            if not sample:
                continue
            rate = _tx_rate_window.get(sid, 0) / STATUS_LOG_PERIOD_S
            _tx_rate_window[sid] = 0
            last_pkt = _last_packet_wall.get(sid)
            silent_s = (now - last_pkt) if last_pkt else 0.0
            # No packets this period: don't re-print the stale sample as if it were live.
            # Emit an explicit silence marker (last state/seq are from when it last spoke),
            # skip the InfluxDB point, and stop entirely once past the silence cutoff —
            # the watchdog cleans up tracking state after MISSION_END_IDLE_S.
            if rate == 0:
                if last_pkt and silent_s > STATUS_LOG_PERIOD_S * 5:
                    continue
                last_state = "MOVING" if sample["state"] == STATE_MOVING else "IDLE"
                logger.info(
                    "[%s] no packets (%.0fs silent) — last seen %s seq=%d",
                    sid, silent_s, last_state, sample["seq_wire"],
                )
                continue

            state_name = "MOVING" if sample["state"] == STATE_MOVING else "IDLE"
            ax, ay, az = sample["accel_x"], sample["accel_y"], sample["accel_z"]
            gx, gy, gz = sample["gyro_x"],  sample["gyro_y"],  sample["gyro_z"]
            temp       = sample["temp_c"]
            hum        = sample["humidity_pct"]

            gyro_ok  = not (math.isnan(gx) or math.isnan(gy) or math.isnan(gz))
            gyro_str = f"({gx:.1f},{gy:.1f},{gz:.1f})" if gyro_ok else "n/a"
            logger.info(
                "[%s] %s %.1fHz seq=%d accel=(%.2f,%.2f,%.2f)g gyro=%sdps "
                "temp=%s°C hum=%s%%",
                sid, state_name, rate, sample["seq_wire"],
                ax, ay, az, gyro_str,
                "n/a" if math.isnan(temp) else f"{temp:.2f}",
                "n/a" if math.isnan(hum)  else f"{hum:.1f}",
            )

            if not _INFLUXDB_TOKEN:
                continue
            # Live telemetry point for Grafana — raw signal only, one per active shuttle
            # per second. Derived quantities (magnitudes, tilt, energy) are no longer
            # computed here: the live view just shows raw motion + IDLE/MOVING state.
            point = (
                Point("stm_telemetry")
                .tag("shuttle_id", str(sample["shuttle_id"]))
                .tag("gateway",    _GATEWAY_TAG)
                .field("state",             sample["state"])
                .field("accel_x",           round(ax, 2))
                .field("accel_y",           round(ay, 2))
                .field("accel_z",           round(az, 2))
                .field("tx_rate_hz",        rate)
                .time(time.time_ns(), WritePrecision.NS)
            )
            if gyro_ok:
                point = (point
                    .field("gyro_x",   round(gx, 2))
                    .field("gyro_y",   round(gy, 2))
                    .field("gyro_z",   round(gz, 2)))
            if not math.isnan(temp):
                point = point.field("temp_c",       round(temp, 2))
            if not math.isnan(hum):
                point = point.field("humidity_pct", round(hum, 1))
            influx_points.append(point)

        if influx_points:
            _write_telemetry_batch(influx_points)


async def _mission_end_watchdog() -> None:
    """Catch the case where a shuttle goes IDLE and then stops sending entirely.
    Without this loop, _maybe_flush_mission only runs on incoming IDLE packets — so
    a shuttle that powers off mid-IDLE would never flush its mission.
    Also resets IDLE-only shuttles (no MOVING run) that have gone permanently silent."""
    while True:
        await asyncio.sleep(5.0)
        now = time.monotonic()
        # Standard path: shuttles that had at least one MOVING packet.
        for sid in list(_last_moving_wall.keys()):
            _maybe_flush_mission(sid, now)
        # Time-cap safety net: force-flush any buffer open longer than BUFFER_MAX_AGE_S.
        # Guards against mission-end never firing (e.g. spurious MOVING resetting the IDLE
        # timer) — without this a single Parquet file could span hours. Mid-mission flush:
        # state is preserved so the mission keeps streaming into the next file.
        for sid in list(_telemetry_buf.keys()):
            buf = _telemetry_buf.get(sid)
            if buf and (now - _buffer_open_wall.get(sid, now)) > BUFFER_MAX_AGE_S:
                logger.warning(
                    "[%s] buffer open > %.0fs (%d pkts) — time-cap flush",
                    sid, BUFFER_MAX_AGE_S, len(buf),
                )
                _flush(buf, "mission")
        # Ghost-shuttle cleanup: shuttles that only ever sent IDLE packets and have
        # gone silent. _maybe_flush_mission never fires for them (no _last_moving_wall
        # entry), so we must reset them here once the silence exceeds MISSION_END_IDLE_S.
        for sid in list(_last_packet_wall.keys()):
            if sid in _last_moving_wall:
                continue  # handled above
            silence_s = now - _last_packet_wall[sid]
            if silence_s > MISSION_END_IDLE_S:
                logger.info(
                    "[%s] IDLE-only shuttle silent %.0fs — clearing tracking state",
                    sid, silence_s,
                )
                _reset_shuttle_state(sid)


# ---------------------------------------------------------------------------
# Beacon broadcast — P2-1 zero-touch provisioning (unchanged from v1)
# ---------------------------------------------------------------------------

def _detect_local_ip() -> str:
    # UDP connect trick: the OS picks the outbound interface without sending any packet.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


async def _broadcast_beacon() -> None:
    # Broadcasts "PLUDOS-GW:<ip>" — or "PLUDOS-GW:<ip>:<csv-ids>" when SHUTTLE_GROUP
    # is set — so STM32s on the same WiFi can discover this gateway and (when the
    # suffix is present) bond only if their SHUTTLE_ID is in the served group.
    # Requires host networking on the Jetson for the broadcast to escape the
    # container bridge.
    ip = GATEWAY_IP or _detect_local_ip()
    if SHUTTLE_GROUP:
        suffix = ",".join(str(i) for i in sorted(SHUTTLE_GROUP))
        payload = f"PLUDOS-GW:{ip}:{suffix}".encode()
        group_log = f"group={suffix}"
    else:
        payload = f"PLUDOS-GW:{ip}".encode()
        group_log = "group=any"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    loop = asyncio.get_running_loop()
    logger.info(
        "[BEACON] announcing %s on UDP port %d every %.0f s (%s)",
        ip, BEACON_PORT, BEACON_INTERVAL_S, group_log,
    )
    try:
        while True:
            try:
                await loop.run_in_executor(
                    None, sock.sendto, payload, ("255.255.255.255", BEACON_PORT)
                )
            except OSError as exc:
                logger.warning("[BEACON] broadcast failed: %s", exc)
            await asyncio.sleep(BEACON_INTERVAL_S)
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Daily consolidation — merges per-mission files into one YYYY-MM-DD.parquet
# ---------------------------------------------------------------------------

def _consolidate_cap_day(date_str: str) -> None:
    """Merge per-mission drain files cap_{sensor}_s*_m*.parquet (whose mtime falls on date_str,
    UTC) into one daily file per sensor: cap_{sensor}_{date}.parquet, ordered by (shuttle_id, t_ms).
    Accel and gyro stay separate — different ODRs give different sample grids, so they can't share
    one time axis. Source files are deleted on success. Runs on a thread executor (blocking I/O)."""
    for sensor in ("accel", "gyro"):
        pattern = os.path.join(BUFFER_DIR, f"{drain_receiver.CAP_PREFIX}_{sensor}_s*_m*.parquet")
        day_files = []
        for path in sorted(glob.glob(pattern)):
            # Cap filenames carry a gateway unix-ms id (cap_accel_s1_m1717....parquet) — still
            # bucket by file mtime, which is the drain time (same UTC day in practice).
            mtime_day = datetime.utcfromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d")
            if mtime_day == date_str:
                day_files.append(path)

        if not day_files:
            continue

        frames = [pd.read_parquet(f) for f in day_files]
        df = pd.concat(frames, ignore_index=True)
        df.sort_values(["shuttle_id", "t_ms"], inplace=True)

        daily_path = os.path.join(BUFFER_DIR, f"{drain_receiver.CAP_PREFIX}_{sensor}_{date_str}.parquet")
        # Merge with an existing daily file in case consolidation runs more than once for the day.
        if os.path.exists(daily_path):
            existing = pd.read_parquet(daily_path)
            df = pd.concat([existing, df], ignore_index=True)
            df.sort_values(["shuttle_id", "t_ms"], inplace=True)
            df.drop_duplicates(subset=["shuttle_id", "mission_id", "sample_index"], keep="last", inplace=True)

        tmp_path = daily_path + ".tmp"
        df.to_parquet(tmp_path, engine="pyarrow", index=False,
                      compression="zstd", compression_level=3)
        os.replace(tmp_path, daily_path)

        for f in day_files:
            os.remove(f)

        logger.info(
            "[CONSOLIDATE] %s: merged %d cap_%s file(s), %d total rows → %s",
            date_str, len(day_files), sensor, len(df), os.path.basename(daily_path),
        )


def _consolidate_day(date_str: str) -> None:
    """Merge all mission_s*_*.parquet files whose flush timestamp falls on date_str (UTC)
    into a single daily file named YYYY-MM-DD.parquet containing all shuttles.
    Source files are deleted on success. Runs on a thread executor — does blocking I/O.
    Also folds the high-rate drain cap files for the day (separate schema, see _consolidate_cap_day)."""
    _consolidate_cap_day(date_str)
    pattern   = os.path.join(BUFFER_DIR, "mission_s*_*.parquet")
    day_files = []
    for path in sorted(glob.glob(pattern)):
        try:
            # Filename: mission_s{id}_{unix_ms}.parquet — extract unix_ms from last segment.
            ts_ms = int(os.path.basename(path).rsplit("_", 1)[-1].replace(".parquet", ""))
            if datetime.utcfromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d") == date_str:
                day_files.append(path)
        except (ValueError, IndexError):
            pass

    if not day_files:
        logger.debug("[CONSOLIDATE] No mission files found for %s", date_str)
        return

    frames = [pd.read_parquet(f) for f in day_files]
    df = pd.concat(frames, ignore_index=True)
    df.sort_values(["shuttle_id", "seq"], inplace=True)

    daily_path = os.path.join(BUFFER_DIR, f"{date_str}.parquet")
    # Merge with an existing daily file in case consolidation runs more than once for the day.
    if os.path.exists(daily_path):
        existing = pd.read_parquet(daily_path)
        df = pd.concat([existing, df], ignore_index=True)
        df.sort_values(["shuttle_id", "seq"], inplace=True)
        df.drop_duplicates(subset=["shuttle_id", "seq"], keep="last", inplace=True)

    tmp_path = daily_path + ".tmp"
    # zstd: consistent compression across mission and daily files.
    # PyArrow write is sync but consolidation runs on an executor — acceptable.
    df.to_parquet(tmp_path, engine="pyarrow", index=False,
                  compression="zstd", compression_level=3)
    os.replace(tmp_path, daily_path)

    for f in day_files:
        os.remove(f)

    logger.info(
        "[CONSOLIDATE] %s: merged %d file(s), %d total rows → %s",
        date_str, len(day_files), len(df), os.path.basename(daily_path),
    )


def _consolidate_stale() -> None:
    """At startup, consolidate any mission files from days before today (Jetson may have been
    offline when midnight consolidation was supposed to run)."""
    today   = datetime.utcnow().strftime("%Y-%m-%d")
    pattern = os.path.join(BUFFER_DIR, "mission_s*_*.parquet")
    stale_dates: set[str] = set()
    for path in glob.glob(pattern):
        try:
            ts_ms = int(os.path.basename(path).rsplit("_", 1)[-1].replace(".parquet", ""))
            date  = datetime.utcfromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d")
            if date < today:
                stale_dates.add(date)
        except (ValueError, IndexError):
            pass
    # Drain cap files carry no timestamp in the name — bucket by mtime so cap-only stale days
    # (no live mission file that day) still get consolidated. _consolidate_day folds cap too.
    for sensor in ("accel", "gyro"):
        for path in glob.glob(os.path.join(BUFFER_DIR, f"{drain_receiver.CAP_PREFIX}_{sensor}_s*_m*.parquet")):
            date = datetime.utcfromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d")
            if date < today:
                stale_dates.add(date)
    for date in sorted(stale_dates):
        logger.info("[CONSOLIDATE] Startup: consolidating stale files for %s", date)
        _consolidate_day(date)


async def _daily_consolidate_task() -> None:
    """At 00:00:05 UTC each day, consolidate yesterday's mission files into YYYY-MM-DD.parquet."""
    while True:
        now_utc  = datetime.utcnow()
        midnight = (now_utc + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
        wait_s   = (midnight - now_utc).total_seconds()
        logger.info("[CONSOLIDATE] Next daily consolidation in %.0f s (at %s UTC)", wait_s, midnight.isoformat())
        await asyncio.sleep(wait_s)
        # 10-second buffer ensures we're solidly into the new day before computing yesterday.
        yesterday = (datetime.utcnow() - timedelta(seconds=10)).strftime("%Y-%m-%d")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _consolidate_day, yesterday)


# ---------------------------------------------------------------------------
# Liveness watchdog
# ---------------------------------------------------------------------------
# The event loop wedged silently once: the container stayed "Up" but emitted no
# logs and its UDP sockets were unbound — `restart: unless-stopped` can't help a
# hung-but-alive process. An asyncio task bumps a heartbeat; a daemon thread
# (outside the loop, so a wedge can't stop it) force-exits if the heartbeat goes
# stale, letting Podman restart a fresh process.

# Monotonic timestamp of the last loop tick; only the running loop can bump it.
_last_heartbeat = time.monotonic()
WATCHDOG_INTERVAL_S = 10.0
# A wedge is declared after this much silence; env-overridable for slow boots.
WATCHDOG_TIMEOUT_S = float(os.getenv("WATCHDOG_TIMEOUT_S", "60"))


# Bump the heartbeat from inside the loop — stops updating the instant it wedges.
async def _heartbeat_task() -> None:
    global _last_heartbeat
    while True:
        _last_heartbeat = time.monotonic()
        await asyncio.sleep(WATCHDOG_INTERVAL_S)


# Daemon thread: force-exit if the loop stops bumping the heartbeat.
def _watchdog_thread() -> None:
    while True:
        time.sleep(WATCHDOG_INTERVAL_S)
        stale_s = time.monotonic() - _last_heartbeat
        if stale_s > WATCHDOG_TIMEOUT_S:
            # stderr + os._exit: bypass the (possibly wedged) loop and logging handlers.
            print(f"[WATCHDOG] event loop stale {stale_s:.0f}s > "
                  f"{WATCHDOG_TIMEOUT_S:.0f}s — forcing restart", file=sys.stderr, flush=True)
            os._exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    logger.info("Gateway wall clock: %s UTC", pd.Timestamp.now(tz="UTC").isoformat())

    # Log available RAM so operators can see how buffer limits relate to headroom.
    try:
        import psutil  # type: ignore[import-untyped]  # optional dep, container-only
        vm = psutil.virtual_memory()
        ram_info = f"RAM avail={vm.available/1e9:.1f}GB total={vm.total/1e9:.1f}GB"
    except ImportError:
        ram_info = "RAM info unavailable (psutil not installed)"

    logger.info(
        "PLUDOS Data Engine (ADR-016 v3 + gyro) starting | mode=%s | TEST_MODE=%s | UDP=%d | "
        "pkt=%dB | limit_mode=%s | shuttle soft=%d (~%.0fMB) hard=%d | gateway hard=%d | "
        "mission_end_idle=%.0fs | group=%s | dir=%s | %s",
        PLUDOS_MODE, TEST_MODE, TELEMETRY_PORT, TELEMETRY_SIZE,
        _LIMIT_MODE,
        SHUTTLE_SOFT_LIMIT, SHUTTLE_SOFT_LIMIT * _PACKET_BYTES_EST / 1e6,
        SHUTTLE_HARD_LIMIT, GATEWAY_HARD_LIMIT,
        MISSION_END_IDLE_S,
        ",".join(str(i) for i in sorted(SHUTTLE_GROUP)) if SHUTTLE_GROUP else "any",
        BUFFER_DIR,
        ram_info,
    )

    # Single UDP listener — replaces both the aiocoap /vib server and the
    # legacy NonCriticalProtocol on 5684 from earlier protocol versions.
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(
        TelemetryProtocol,
        local_addr=("0.0.0.0", TELEMETRY_PORT),
    )
    logger.info("Telemetry UDP listener bound on port %d", TELEMETRY_PORT)

    # High-rate capture drain receiver on UDP 5684 (ADR-020). Separate path from
    # the 5683 live hot loop; drain t0 is self-timed via the STM tx_tick - t0_tick age.
    await drain_receiver.start_drain_receiver(BUFFER_DIR, _write_drain_summary)

    # Consolidate any mission files from days before today (handles Jetson restarts / downtime).
    _consolidate_stale()

    # Background tasks.
    asyncio.create_task(_status_log_task())
    asyncio.create_task(_mission_end_watchdog())
    asyncio.create_task(_broadcast_beacon())
    asyncio.create_task(_daily_consolidate_task())

    # Liveness watchdog: loop-side heartbeat + out-of-loop daemon that restarts on wedge.
    asyncio.create_task(_heartbeat_task())
    threading.Thread(target=_watchdog_thread, name="watchdog", daemon=True).start()

    await loop.create_future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Data Engine shutting down.")
