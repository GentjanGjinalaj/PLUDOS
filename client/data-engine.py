"""
PLUDOS Edge Gateway: Data Engine (ADR-016 v3 — compact 24-byte stream)
-----------------------------------------------------------------------
Listens on a single raw UDP socket:
  - UDP 5683: PludosTelemetry packets from each STM32 shuttle
    (24 bytes — uint8 id + uint16 seq + uint32 tick + uint8 state +
     8×int16 sensors: accel xyz, gyro xyz, temp, humidity)
    Sensors scaled ×100 (g, dps, °C) or ×10 (%RH); 0x7FFF = unavailable.
    See `docs/wire_protocol.md §1`.

pressure_hpa and power_mw are no longer on the wire. Power is derived from
state (POWER_IDLE_MW / POWER_MOVING_MW env vars). Parquet files are enriched
at flush time with accel_mag and a proper UTC Timestamp — zero
per-packet overhead for the enrichment.

Every incoming packet is appended to that shuttle's in-memory buffer. A
mission is delimited by the `state` field: when a shuttle has been
streaming `state == MOVING` packets and then stays in `state == IDLE` for
`MISSION_END_IDLE_S` (default 30 s), the gateway flushes that shuttle's
buffer to a single Parquet file and writes a summary row to InfluxDB
`stm_mission`.

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
import threading
import time
from datetime import datetime, timedelta

import pandas as pd
from influxdb_client import InfluxDBClient, Point, WritePrecision  # type: ignore
from influxdb_client.client.write_api import SYNCHRONOUS           # type: ignore

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
#   Hard packet counts sized for 10 Hz MOVING TX rate on the Jetson 8 GB.
#   At ~300 B Python dict overhead per packet:
#     SHUTTLE_SOFT_LIMIT=3000  → ~5 min of MOVING before proactive flush (≈0.9 MB RAM)
#     SHUTTLE_HARD_LIMIT=4500  → ~7.5 min before emergency flush          (≈1.4 MB RAM)
#     GATEWAY_HARD_LIMIT=100000→ safety ceiling across all shuttles       (≈30 MB RAM)
#   These defaults produce at most one Parquet write per 5-minute mission
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

# In-mission IDLE stops longer than this are flagged is_long_pause=1 (retry suspect:
# shuttle waited unusually long without moving, suggesting a failed pick/place).
RETRY_PAUSE_THRESHOLD_S = float(os.getenv("RETRY_PAUSE_THRESHOLD_S", "8.0"))

# 1D-ZUPT distance for Savoye XTPS: one shuttle per rail, forward/backward only.
# DC offset (mounting tilt) is removed using the mean of IDLE samples in the flush buffer —
# shuttle is physically stopped during IDLE so accel_rail ≈ g×sin(θ) is constant.
# Minimum HPF accel magnitude (g) to count as motion regardless of state flag.
# Lets integration continue during the ~800 ms FSM debounce window at motion onset (T-B2).
DISTANCE_MOVING_EPS = float(os.getenv("DISTANCE_MOVING_EPS", "0.01"))
# Physical upper bound on distance per MOVING segment — catches HPF burn-in errors and sensor drift.
# Update once Savoye confirms the exact minifloor rail length; 20 m is a conservative maximum.
RAIL_LENGTH_M_MAX = float(os.getenv("RAIL_LENGTH_M_MAX", "20.0"))

# Per-second status log: roll up "tx rate" and last-seen sensor values rather
# than logging every packet (50 Hz × 100 shuttles would drown the terminal).
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

# Final Parquet columns in standard order — must stay in sync with client.py feature_cols.
# Intermediate fields (tick_ms, seq_wire, timestamp_ms) are dropped at flush time.
_PARQUET_COLS = [
    "timestamp",    # pd.Timestamp UTC — anchored STM32 tick via per-shuttle NTP offset
    "shuttle_id",   # int8    — 1-based integer matching wifi_credentials.h SHUTTLE_ID
    "seq",          # int32   — uint16 wire counter unwrapped across rollovers; sort key
    "seq_gap",      # int16   — packets dropped before this row; 0=no loss
    "state",        # int8    — 0=IDLE, 1=MOVING
    "energy_j",     # float32 J — cumulative mission energy at this packet (power×elapsed)
    # Accelerometer (ISM330DHCX, ±2 g FS)
    "accel_x",           # float16 g — X axis; NaN if sensor unavailable
    "accel_y",           # float16 g — Y axis
    "accel_z",           # float16 g — Z axis
    "accel_mag",         # float16 g — √(x²+y²+z²); derived at flush
    "accel_jerk",        # float16 g/s — |Δaccel_mag/Δt|; sudden-impact detector; derived at flush
    "horizontal_accel",  # float16 g — √(ax²+ay²); pure horizontal motion, gravity removed
    "tilt_angle_deg",    # float16 ° — arccos(az/accel_mag)×180/π; 0°=flat, 90°=sideways
    # Gyroscope (ISM330DHCX, ±250 dps FS)
    "gyro_x",            # float16 dps — roll rate; NaN if unavailable
    "gyro_y",            # float16 dps — pitch rate
    "gyro_z",            # float16 dps — yaw rate
    "gyro_mag",          # float16 dps — √(gx²+gy²+gz²); derived at flush
    "gyro_jerk",         # float16 dps/s — |Δgyro_mag/Δt|; sharpness of rotational events
    # 1-second rolling context (window = 10 packets at 10 Hz MOVING rate)
    "rolling_accel_mean_10", # float16 g — trailing mean of accel_mag; sustained-motion context
    "rolling_accel_std_10",  # float16 g — trailing std; surface roughness / vibration proxy
    # Environment (HTS221)
    "temp_c",       # float16 °C — NaN when unavailable
    "humidity_pct", # float16 %  — HTS221 RH; NaN when unavailable
    # Kinematic estimates
    "mission_elapsed_s", # float32 s — time since first packet in this flush buffer
    "distance_m_cum",    # float32 m — impulse-counter cumulative distance; reset per mission
    # Mission segmentation (derived from state transitions within the flush buffer)
    "moving_run_id",     # int8    — 1-based travel leg; 0 = pre-mission IDLE (no MOVING yet)
    "pause_duration_s",  # float32 s — duration of this in-mission stop; 0 if MOVING or pre/post-mission IDLE
    "moving_run_dur_s",  # float32 s — duration of the MOVING run containing this packet; 0 for IDLE
    "pause_count",       # int8    — cumulative in-mission pauses completed before this packet
    "is_long_pause",     # int8    — 1 if pause_duration_s > RETRY_PAUSE_THRESHOLD_S (retry suspect)
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
# Power constants — used to derive power_mw and integrate energy on gateway.
# Firmware no longer transmits power. Calibrate with a bench ammeter.
# IDLE  ≈ (MCU 15mA + sensors 2mA + WiFi assoc 10mA) × 3.3V = 89 mW (confirmed).
# MOVING ≈ depends on WiFi TX duty at 50 Hz; needs measurement. Default is a rough estimate.
# ---------------------------------------------------------------------------

POWER_IDLE_MW   = float(os.getenv("POWER_IDLE_MW",   "89"))
POWER_MOVING_MW = float(os.getenv("POWER_MOVING_MW", "260"))

# ---------------------------------------------------------------------------
# Mutable gateway state — per-shuttle dicts, single-threaded asyncio.
# ---------------------------------------------------------------------------

# All telemetry packets waiting for Parquet flush, keyed by shuttle_id.
_telemetry_buf: dict[str, list[dict]] = {}

# Cumulative STM32-estimated energy per shuttle in Joules, reset after each mission flush.
_mission_energy_j: dict[str, float] = {}

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

# Distance carry-over across mid-mission buffer-pressure flushes.
# Cleared on mission-end by _reset_shuttle_state; never cleared on pressure flush.
_dist_carry: dict[str, float] = {}

# Pick-event carry-over across mid-mission pressure flushes — same pattern as _dist_carry.
_pick_carry: dict[str, int] = {}

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _compute_derived(df: pd.DataFrame, carry_dist: float = 0.0) -> pd.DataFrame:
    """Add all derived columns and cast to Parquet dtypes. df must be sorted by (shuttle_id, seq)."""

    # Anchor STM32 relative tick to gateway NTP wall clock → proper UTC Timestamp.
    df["timestamp"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)

    # seq_gap: packets dropped before each row. diff()=1 means consecutive, so sub(1).
    # Clamp negative (reorder edge case) to 0. First row = 0 (no prior to compare).
    df["seq_gap"] = df["seq"].diff().sub(1).clip(lower=0).fillna(0).astype("int16")

    # Magnitude proxies derived at flush; pandas propagates NaN through the arithmetic.
    df["accel_mag"] = (df["accel_x"]**2 + df["accel_y"]**2 + df["accel_z"]**2).pow(0.5)
    df["gyro_mag"]  = (df["gyro_x"]**2  + df["gyro_y"]**2  + df["gyro_z"]**2).pow(0.5)

    # dt_s between consecutive packets — used for all per-packet time derivatives.
    # replace(0) guards against duplicate timestamps on buffer-pressure boundary packets.
    dt_s = df["timestamp"].diff().dt.total_seconds().replace(0, float("nan"))

    # accel_jerk: |Δaccel_mag / Δt| — captures sudden impacts. NaN on first row.
    df["accel_jerk"] = df["accel_mag"].diff().abs().div(dt_s)

    # horizontal_accel: √(ax²+ay²) — motion in the horizontal plane with gravity removed.
    df["horizontal_accel"] = (df["accel_x"]**2 + df["accel_y"]**2).pow(0.5)

    # tilt_angle_deg: arccos(az / accel_mag) in degrees.
    # 0° = sensor flat/upright, 90° = sideways, >90° = inverted.
    # clip(-1,1) prevents domain errors from floating-point accel_mag rounding.
    df["tilt_angle_deg"] = (
        df["accel_z"].div(df["accel_mag"].replace(0, float("nan")))
        .clip(-1.0, 1.0)
        .apply(lambda v: math.degrees(math.acos(v)) if not math.isnan(v) else float("nan"))
    )

    # gyro_jerk: |Δgyro_mag / Δt| — rate of change of angular velocity. NaN on first row.
    df["gyro_jerk"] = df["gyro_mag"].diff().abs().div(dt_s)

    # 1-second rolling context window (10 packets at 10 Hz MOVING rate).
    # min_periods=1 avoids NaN at the start of a buffer; std needs ≥2 points.
    df["rolling_accel_mean_10"] = df["accel_mag"].rolling(10, min_periods=1).mean()
    df["rolling_accel_std_10"]  = df["accel_mag"].rolling(10, min_periods=2).std().fillna(0.0)

    # mission_elapsed_s: seconds from the first packet in this flush buffer.
    df["mission_elapsed_s"] = (
        df["timestamp"] - df["timestamp"].iloc[0]
    ).dt.total_seconds()

    # 1D-ZUPT distance for Savoye XTPS (one shuttle per rail, forward/backward only).
    #
    # Step 1: auto-detect track axis. The rail-aligned axis has far higher variance
    #         during MOVING than the perpendicular axis (arm extend/retract).
    # Step 2: HPF (running mean subtraction) removes mounting-tilt DC offset.
    # Step 3: integrate signed HPF accel. At every IDLE packet reset vel=0 — the
    #         shuttle is physically stopped on the rail, so ZUPT is exact here.
    states    = df["state"].values.astype(int)
    ax_moving = df.loc[df["state"] == STATE_MOVING, "accel_x"].dropna()
    ay_moving = df.loc[df["state"] == STATE_MOVING, "accel_y"].dropna()
    var_x     = float(ax_moving.var()) if len(ax_moving) > 1 else 0.0
    var_y     = float(ay_moving.var()) if len(ay_moving) > 1 else 0.0
    track_a   = (
        df["accel_x"].values.astype(float) if var_x >= var_y
        else df["accel_y"].values.astype(float)
    )
    # Subtract the mean accel of IDLE packets as the DC (tilt) offset.
    # Rolling-mean HPF was removed: it erodes the motion signal during short MOVING segments
    # (shuttle moves < HPF window samples), leaving residual velocity after deceleration.
    idle_vals = track_a[(df["state"] == STATE_IDLE).values]
    dc_offset = float(idle_vals.mean()) if len(idle_vals) > 0 else 0.0
    hpf       = track_a - dc_offset      # signed acceleration (g) with tilt removed
    dt_arr    = dt_s.fillna(0.1).values  # fallback 0.1 s at 10 Hz MOVING rate
    dist_arr    = []
    d, vel      = carry_dist, 0.0
    d_seg_start = carry_dist  # distance at start of current MOVING segment for rail-length clamp
    clamped_seg = False       # True once RAIL_LENGTH_M_MAX is hit in the current MOVING segment
    for i in range(len(states)):
        h = float(hpf[i])
        if not math.isnan(h):
            # Integrate when MOVING or when HPF accel exceeds eps — the FSM debounce
            # delay (~800 ms) reports IDLE while the shuttle is physically accelerating.
            # ZUPT only when state==IDLE and accel is below the noise floor.
            if int(states[i]) == STATE_MOVING or abs(h) > DISTANCE_MOVING_EPS:
                vel += h * 9.81 * float(dt_arr[i])   # g → m/s²; signed integration
                if not clamped_seg:
                    d += abs(vel) * float(dt_arr[i])  # unsigned path length
                    # T-C3: clamp per-segment distance to physical rail length.
                    if (d - d_seg_start) > RAIL_LENGTH_M_MAX:
                        logger.warning(
                            "[DISTANCE] segment %.2f m > RAIL_LENGTH_M_MAX=%.1f m"
                            " — clamping (HPF burn-in or sensor drift)",
                            d - d_seg_start, RAIL_LENGTH_M_MAX,
                        )
                        d = d_seg_start + RAIL_LENGTH_M_MAX
                        clamped_seg = True
            else:
                vel = 0.0        # ZUPT: state==IDLE and |hpf| below noise floor → physically stopped
                d_seg_start = d  # mark start of next MOVING segment
                clamped_seg = False
        elif int(states[i]) == STATE_IDLE:
            vel = 0.0            # ZUPT on unavailable sensor during IDLE — safe to reset
            d_seg_start = d
            clamped_seg = False
        dist_arr.append(round(d, 3))
    df["distance_m_cum"] = dist_arr

    # Mission segmentation: label each packet with its travel leg and pause context.
    # Two-pass scan over contiguous state runs; pure Python/lists — no numpy.
    times_arr = df["mission_elapsed_s"].values.tolist()
    n = len(states)  # `states` already computed above for the distance loop

    # Pass 1: collect contiguous state runs → [(state_val, start_idx, end_idx, duration_s), ...]
    segments = []
    i = 0
    while i < n:
        s_type  = int(states[i])
        s_start = i
        while i < n and int(states[i]) == s_type:
            i += 1
        # duration = elapsed time from first to last packet in this run
        dur = float(times_arr[i - 1] - times_arr[s_start]) if i - 1 > s_start else 0.0
        segments.append((s_type, s_start, i, dur))

    # Pass 2: annotate per-packet output lists.
    moving_run_id_arr  = [0] * n
    pause_dur_arr      = [0.0] * n
    moving_run_dur_arr = [0.0] * n
    pause_count_arr    = [0] * n
    is_long_pause_arr  = [0] * n

    moving_run_count = 0
    cum_pauses       = 0
    for seg_idx, (s_type, s_start, s_end, dur_s) in enumerate(segments):
        if s_type == STATE_MOVING:
            moving_run_count += 1
            for j in range(s_start, s_end):
                moving_run_id_arr[j]  = moving_run_count
                moving_run_dur_arr[j] = dur_s
                pause_count_arr[j]    = cum_pauses
        else:  # STATE_IDLE
            # An in-mission pause is IDLE sandwiched between two MOVING runs.
            has_before = any(seg[0] == STATE_MOVING for seg in segments[:seg_idx])
            has_after  = any(seg[0] == STATE_MOVING for seg in segments[seg_idx + 1:])
            if has_before and has_after:
                cum_pauses += 1
                long_flag   = 1 if dur_s > RETRY_PAUSE_THRESHOLD_S else 0
                for j in range(s_start, s_end):
                    pause_dur_arr[j]     = dur_s
                    pause_count_arr[j]   = cum_pauses
                    is_long_pause_arr[j] = long_flag
            else:  # pre/post-mission IDLE: inherit current cumulative pause count
                for j in range(s_start, s_end):
                    pause_count_arr[j] = cum_pauses

    df["moving_run_id"]    = moving_run_id_arr
    df["pause_duration_s"] = pause_dur_arr
    df["moving_run_dur_s"] = moving_run_dur_arr
    df["pause_count"]      = pause_count_arr
    df["is_long_pause"]    = is_long_pause_arr

    # Round to wire precision before downcasting to float16.
    # float16 halves storage (2 B vs 4 B) with no meaningful precision loss
    # for our data ranges (±2g accel, ±250 dps gyro, 0-50°C, 0-100%RH).
    sensor_cols = (
        "accel_x", "accel_y", "accel_z", "accel_mag", "accel_jerk",
        "horizontal_accel", "tilt_angle_deg",
        "gyro_x", "gyro_y", "gyro_z", "gyro_mag", "gyro_jerk",
        "rolling_accel_mean_10", "rolling_accel_std_10",
        "temp_c",
    )
    df[list(sensor_cols)] = df[list(sensor_cols)].round(2)
    df["humidity_pct"]    = df["humidity_pct"].round(1)

    # Compact dtypes: int for identity/state, float16 for sensors, float32 for accumulators.
    df["shuttle_id"] = df["shuttle_id"].astype("int8")
    df["state"]      = df["state"].astype("int8")
    df["seq"]        = df["seq"].astype("int32")
    df["energy_j"]   = df["energy_j"].astype("float32")
    for col in sensor_cols:
        df[col] = df[col].astype("float16")
    df["humidity_pct"] = df["humidity_pct"].astype("float16")
    for col in ("mission_elapsed_s", "distance_m_cum"):
        df[col] = df[col].astype("float32")
    df["moving_run_id"] = df["moving_run_id"].astype("int8")
    df["pause_count"]   = df["pause_count"].astype("int8")
    df["is_long_pause"] = df["is_long_pause"].astype("int8")
    for col in ("pause_duration_s", "moving_run_dur_s"):
        df[col] = df[col].astype("float32")

    return df[_PARQUET_COLS]


def _flush(buf: list[dict], prefix: str, carry_dist: float = 0.0, carry_picks: int = 0) -> tuple[float, int]:
    """Compute derived columns, write clean Parquet atomically, clear buf.
    carry_dist continues distance_m_cum across pressure flushes (T-B1).
    carry_picks continues pick_events count across pressure flushes (T-C2).
    Returns (total_distance_m, pick_events) for the mission-end InfluxDB summary."""
    if not buf:
        return 0.0, carry_picks
    df = pd.DataFrame(buf)
    df.sort_values(by=["shuttle_id", "seq"], inplace=True)
    df = _compute_derived(df, carry_dist)

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
    # Total mission distance — returned to mission-end caller for InfluxDB.
    total_dist  = round(float(df["distance_m_cum"].iloc[-1]), 2)
    pick_events = carry_picks + int(df["pause_count"].max())
    buf.clear()
    return total_dist, pick_events


def _write_mission_summary(
    shuttle_id: str,
    energy_j: float,
    packets: int,
    duration_ms: float,
    distance_m: float = 0.0,
    pick_events: int = 0,
) -> None:
    # Fire-and-forget background thread so the asyncio loop is never blocked by InfluxDB I/O.
    def _write() -> None:
        client = InfluxDBClient(url=_INFLUXDB_URL, token=_INFLUXDB_TOKEN, org=_INFLUXDB_ORG)
        try:
            point = (
                Point("stm_mission")
                .tag("shuttle_id", shuttle_id)
                .tag("gateway",    _GATEWAY_TAG)
                .field("energy_j",    energy_j)
                .field("packets",     packets)
                .field("duration_ms", duration_ms)
                .field("distance_m",  distance_m)
                .field("pick_events", pick_events)
                .time(time.time_ns(), WritePrecision.NS)
            )
            client.write_api(write_options=SYNCHRONOUS).write(
                bucket=_INFLUXDB_BUCKET, record=point
            )
            logger.info(
                "[INFLUXDB] stm_mission shuttle=%s energy=%.4fJ pkts=%d dur=%.0fms dist=%.1fm picks=%d",
                shuttle_id, energy_j, packets, duration_ms, distance_m, pick_events,
            )
        except Exception as exc:
            logger.warning("[INFLUXDB] stm_mission write failed (%s): %s", shuttle_id, exc)
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
        _mission_energy_j, _mission_start_wall, _ntp_offsets,
        _last_packet_wall, _packet_counts, _last_seq_ids, _seq_wrap_counts,
        _last_moving_wall, _last_sample, _tx_rate_window, _dist_carry, _pick_carry,
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
    energy        = _mission_energy_j.get(shuttle_id, 0.0)
    started_wall  = _mission_start_wall.get(shuttle_id, now)
    duration_ms   = (now - started_wall) * 1000.0

    logger.info(
        "[%s] mission end (IDLE %.0fs) | energy=%.4fJ | pkts=%d | dur=%.0fms | flushing",
        shuttle_id, MISSION_END_IDLE_S, energy, pkts, duration_ms,
    )

    # Pop the buffer before reset so _reset_shuttle_state's pop is a no-op.
    total_dist, pick_events = _flush(
        _telemetry_buf.pop(shuttle_id, []), "mission",
        _dist_carry.get(shuttle_id, 0.0), _pick_carry.get(shuttle_id, 0),
    )
    # headless mode: skip InfluxDB write; Parquet is still written above.
    if PLUDOS_MODE != "headless":
        _write_mission_summary(shuttle_id, energy, pkts, duration_ms, total_dist, pick_events)
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
        # Derive power from state — not transmitted on the wire (ADR-015 v2).
        power_mw     = POWER_MOVING_MW if state == STATE_MOVING else POWER_IDLE_MW

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

        # Energy integration uses wall-clock elapsed since the previous packet so the
        # estimate is correct across IDLE (1 Hz) and MOVING (50 Hz) rates.
        if shuttle_name in _last_packet_wall:
            elapsed_s = now - _last_packet_wall[shuttle_name]
        else:
            elapsed_s = 0.02
        _last_packet_wall[shuttle_name] = now

        _mission_energy_j.setdefault(shuttle_name, 0.0)
        _mission_energy_j[shuttle_name] += (power_mw / 1000.0) * elapsed_s  # mW→W × s = J
        # Snapshot cumulative energy into the packet so Parquet records it per-row.
        pkt["energy_j"] = _mission_energy_j[shuttle_name]

        # Mission boundary tracking: any MOVING packet resets the IDLE timer.
        if state == STATE_MOVING:
            _last_moving_wall[shuttle_name] = now

        # Buffer the packet. Per-shuttle list so multi-shuttle deployments don't
        # interleave each other's missions in one Parquet file (P2-9 fix preserved).
        _telemetry_buf.setdefault(shuttle_name, []).append(pkt)
        _last_sample[shuttle_name]    = pkt
        _tx_rate_window[shuttle_name] = _tx_rate_window.get(shuttle_name, 0) + 1

        shuttle_pkts = len(_telemetry_buf[shuttle_name])
        total_pkts   = sum(len(v) for v in _telemetry_buf.values())

        # Mission-end via state transition is the normal path. We only check on
        # IDLE packets to avoid running the check 50× per second during MOVING.
        if state == STATE_IDLE:
            _maybe_flush_mission(shuttle_name, now)

        # Buffer-pressure flushes (mid-mission). These do not reset shuttle state —
        # the mission keeps accruing energy and the next batch lands in the next file.
        elif shuttle_pkts >= SHUTTLE_HARD_LIMIT:
            logger.warning(
                "[%s] per-shuttle HARD LIMIT (%d pkts) — mid-mission flush",
                shuttle_name, shuttle_pkts,
            )
            dist, picks = _flush(_telemetry_buf[shuttle_name], "mission", _dist_carry.get(shuttle_name, 0.0), _pick_carry.get(shuttle_name, 0))
            _dist_carry[shuttle_name] = dist
            _pick_carry[shuttle_name] = picks

        elif shuttle_pkts >= SHUTTLE_SOFT_LIMIT:
            logger.info(
                "[%s] per-shuttle soft limit (%d pkts) — proactive flush",
                shuttle_name, shuttle_pkts,
            )
            dist, picks = _flush(_telemetry_buf[shuttle_name], "mission", _dist_carry.get(shuttle_name, 0.0), _pick_carry.get(shuttle_name, 0))
            _dist_carry[shuttle_name] = dist
            _pick_carry[shuttle_name] = picks

        elif total_pkts >= GATEWAY_HARD_LIMIT:
            logger.error(
                "GATEWAY HARD LIMIT (%d total pkts across %d shuttles) — emergency flush all",
                total_pkts, len(_telemetry_buf),
            )
            for s_name, s_buf in list(_telemetry_buf.items()):
                dist, picks = _flush(s_buf, "mission", _dist_carry.get(s_name, 0.0), _pick_carry.get(s_name, 0))
                _dist_carry[s_name] = dist
                _pick_carry[s_name] = picks
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
            # Stop logging a shuttle that has gone silent (no packets for 5 periods).
            # The watchdog will clean up its tracking state after MISSION_END_IDLE_S.
            last_pkt = _last_packet_wall.get(sid)
            if rate == 0 and last_pkt and (now - last_pkt) > STATUS_LOG_PERIOD_S * 5:
                continue

            state_name = "MOVING" if sample["state"] == STATE_MOVING else "IDLE"
            ax, ay, az = sample["accel_x"], sample["accel_y"], sample["accel_z"]
            gx, gy, gz = sample["gyro_x"],  sample["gyro_y"],  sample["gyro_z"]
            temp       = sample["temp_c"]
            hum        = sample["humidity_pct"]
            energy     = _mission_energy_j.get(sid, 0.0)
            pwr        = POWER_MOVING_MW if sample["state"] == STATE_MOVING else POWER_IDLE_MW

            gyro_ok  = not (math.isnan(gx) or math.isnan(gy) or math.isnan(gz))
            gyro_str = f"({gx:.1f},{gy:.1f},{gz:.1f})" if gyro_ok else "n/a"
            logger.info(
                "[%s] %s %.1fHz seq=%d accel=(%.2f,%.2f,%.2f)g gyro=%sdps "
                "temp=%s°C hum=%s%% pwr=%.0fmW e=%.2fJ",
                sid, state_name, rate, sample["seq_wire"],
                ax, ay, az, gyro_str,
                "n/a" if math.isnan(temp) else f"{temp:.2f}",
                "n/a" if math.isnan(hum)  else f"{hum:.1f}",
                pwr, energy,
            )

            if not _INFLUXDB_TOKEN:
                continue
            # Live telemetry point for Grafana — one per active shuttle per second.
            accel_mag      = math.sqrt(ax**2 + ay**2 + az**2)
            h_accel        = math.sqrt(ax**2 + ay**2)
            tilt_deg       = (
                math.degrees(math.acos(max(-1.0, min(1.0, az / accel_mag))))
                if accel_mag > 1e-6 else 0.0
            )
            point = (
                Point("stm_telemetry")
                .tag("shuttle_id", str(sample["shuttle_id"]))
                .tag("gateway",    _GATEWAY_TAG)
                .field("state",             sample["state"])
                .field("accel_x",           round(ax, 2))
                .field("accel_y",           round(ay, 2))
                .field("accel_z",           round(az, 2))
                .field("accel_mag",         round(accel_mag, 2))
                .field("horizontal_accel",  round(h_accel, 2))
                .field("tilt_angle_deg",    round(tilt_deg, 1))
                .field("tx_rate_hz",        rate)
                .field("energy_j",          round(energy, 3))
                .time(time.time_ns(), WritePrecision.NS)
            )
            if gyro_ok:
                gyro_mag = math.sqrt(gx**2 + gy**2 + gz**2)
                point = (point
                    .field("gyro_x",   round(gx, 2))
                    .field("gyro_y",   round(gy, 2))
                    .field("gyro_z",   round(gz, 2))
                    .field("gyro_mag", round(gyro_mag, 2)))
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

def _consolidate_day(date_str: str) -> None:
    """Merge all mission_s*_*.parquet files whose flush timestamp falls on date_str (UTC)
    into a single daily file named YYYY-MM-DD.parquet containing all shuttles.
    Source files are deleted on success. Runs on a thread executor — does blocking I/O."""
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

    # Consolidate any mission files from days before today (handles Jetson restarts / downtime).
    _consolidate_stale()

    # Background tasks.
    asyncio.create_task(_status_log_task())
    asyncio.create_task(_mission_end_watchdog())
    asyncio.create_task(_broadcast_beacon())
    asyncio.create_task(_daily_consolidate_task())

    await loop.create_future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Data Engine shutting down.")
