# PLUDOS Data Guide — Parquet Schema Reference

This document describes every column in the Parquet files produced by
`data-engine.py`. As of schema **v4 the gateway stores raw signal only** —
no derived columns. Feature engineering happens at train time in
`anomaly.py:_derive_features()`. Read this before touching the training
feature set or adding new columns.

Why raw-only? The Jetson is energy- and SD-constrained. Computing
distance, energy, jerk, tilt and segmentation on every packet burned CPU
and stored figures that were either untrustworthy (ZUPT distance drift) or
trivially recomputable. Storing raw signal keeps the data-engine a pure
collector and lets the modelling side decide what features it wants.

---

## 1. How data flows into Parquet

```
STM32 firmware (10 Hz MOVING / 0.1 Hz IDLE)
  │  24-byte UDP packet
  ▼
data-engine.py  (Jetson)
  ├── in-memory per-shuttle buffer
  ├── on mission-end (30 s IDLE after MOVING): flush → Parquet
  ├── on time cap (BUFFER_MAX_AGE_S, default 300 s): force-flush
  └── on buffer pressure (soft/hard limit): intermediate flush
```

One Parquet file = one flush for one shuttle.
Files are named `mission_s{id}_{unix_ms}.parquet`.

A single physical mission can produce multiple files if the buffer fills
mid-mission (soft limit 3 000 packets ≈ 5 min at 10 Hz). Always sort files
by the timestamp in the filename and concatenate before analysis.

---

## 2. Sampling rates

| State    | STM32 sensor loop | TX rate to Jetson   |
|----------|-------------------|---------------------|
| IDLE     | 10 Hz             | **0.1 Hz** (1 pkt/10 s) |
| MOVING   | 10 Hz             | **10 Hz** (every sample) |

The firmware always samples at 10 Hz internally. Movement detection uses
a 500 ms dwell window (5 consecutive above-threshold samples). The shuttle
transitions to MOVING within 500 ms regardless of the 0.1 Hz IDLE TX rate.

---

## 3. Stored columns (13, raw only)

All sensor columns are stored as **float16** (rounded before storage) to
halve file size. Identity/timing columns use the smallest integer type
that fits.

### 3.1 Identity and timing

| Column | dtype | Unit | Description |
|---|---|---|---|
| `timestamp` | datetime64[ns, UTC] | — | STM32 `HAL_GetTick()` anchored to the Jetson's NTP wall clock. Each shuttle has its own offset (`receipt_time_ms − tick_ms`), refreshed every 100 packets to compensate STM32 crystal drift. **Do not sort by `timestamp`** — NTP jitter can cause small out-of-order values. |
| `shuttle_id` | int8 | — | 1-based integer. Set at firmware build time via `SHUTTLE_ID` in `wifi_credentials.h`. Maps to a human label via the `SHUTTLE_NAMES` env var (default: `shuttle-N`). |
| `seq` | int32 | — | Monotonic packet counter. The wire value is uint16 (wraps at 65 535); the gateway unwraps it into a globally unique sort key. **Always use `seq` for ordering**, never `timestamp`. |
| `seq_gap` | int16 | packets | `seq[i] − seq[i−1] − 1`. Zero means no loss. Non-zero values cluster at WiFi dead zones (metal shelving, elevator shaft entry). Position-correlated signal — useful ML feature for identifying where on the route a failure occurred. First row in each file is always 0. |
| `state` | int8 | — | `0` = IDLE (stopped, 0.1 Hz TX). `1` = MOVING (in transit, 10 Hz TX). Derived from the STM32 FSM — see `docs/state_machine.md`. |

### 3.2 Accelerometer (ISM330DHCX, ±2 g full scale)

The shuttle moves horizontally along shelf rows. The sensor is mounted so
that gravity falls on the **Z axis** and the direction of travel is roughly
**Y**. X is the lateral (left/right) axis.

Wire encoding: int16 × 100 → 0.01 g resolution. Sentinel 0x7FFF (32767)
means the sensor was unavailable; the gateway converts this to NaN.

| Column | dtype | Unit | Description |
|---|---|---|---|
| `accel_x` | float16 | g | Lateral acceleration. Near 0 during straight travel; peaks on cornering or shelf misalignment. |
| `accel_y` | float16 | g | Forward/backward along the direction of travel. Peaks at acceleration and braking events. |
| `accel_z` | float16 | g | Vertical. ≈ 1.00 g at rest (gravity). AC noise on this channel indicates bearing or motor wear. Drops below 1 g when decelerating into a shelf approach. |

All three become NaN together if the ISM330DHCX I²C read fails.

### 3.3 Gyroscope (ISM330DHCX, ±250 dps full scale)

Wire encoding: int16 × 100. Sentinel 0x7FFF → NaN. A zero-rate offset of
±0.5 dps at power-on is normal (ISM330 without factory calibration); the
ML model learns around it since it is consistent per device.

| Column | dtype | Unit | Description |
|---|---|---|---|
| `gyro_x` | float16 | dps | Roll rate. Torsional vibration from a damaged bearing or motor shows up here as AC noise at frequency proportional to shaft speed. |
| `gyro_y` | float16 | dps | Pitch rate. Changes when the shuttle nose dips during shelf approach or payload load shift. |
| `gyro_z` | float16 | dps | Yaw rate. Captures turns at the end of shelf rows. |

All three become NaN together if the ISM330DHCX gyro init fails.

### 3.4 Environment (HTS221)

| Column | dtype | Unit | Description |
|---|---|---|---|
| `temp_c` | float16 | °C | Air temperature near the shuttle. Typical warehouse: 15–25 °C. Elevated readings during IDLE may indicate motor heat soaking into the board. NaN if HTS221 I²C read failed. |
| `humidity_pct` | float16 | %RH | Relative humidity. Rounded to 0.1 %. NaN if HTS221 failed. |

---

## 4. Columns computed at training time (not stored)

Derived once per FL round in `anomaly.py:_derive_features()`, after the
recent Parquet files are loaded and sorted by `(shuttle_id, seq)`. The CNN
labeller ignores these (it consumes raw axes); IsolationForest and XGBoost
use them.

| Column | Formula | Description |
|---|---|---|
| `accel_mag` | √(accel_x² + accel_y² + accel_z²) | Total acceleration magnitude; ≈ 1.0 g at rest. Deviations > 1.05 g indicate dynamic motion. |
| `gyro_mag` | √(gyro_x² + gyro_y² + gyro_z²) | Aggregate rotation rate magnitude. |
| `rolling_accel_std_10` | 10-packet rolling std of `accel_mag` (`min_periods=2`, leading NaN → 0) | 1 s window at 10 Hz MOVING. Primary surface-roughness / bearing-wear proxy — high std = high vibration variance. |

Other features that earlier schema versions stored (`accel_jerk`,
`horizontal_accel`, `tilt_angle_deg`, `gyro_jerk`, `rolling_accel_mean_10`)
are no longer computed anywhere. Recompute downstream from the raw axes if
a future model needs them.

---

## 5. Columns NOT in Parquet

| Name | Why absent |
|---|---|
| `distance_m_cum` / `displacement_m` / `speed_ms` | ZUPT distance integration removed (v4). The 1-D integrator drifted badly at 10 Hz; the figure was not trustworthy. Recompute downstream if needed — do not treat as ground truth. |
| `energy_j` / `power_mw` | Per-packet energy estimate removed (v4). Gateway/FL-round energy is measured separately (Alumet, server-side `fl_phases`), not derived per telemetry packet. |
| segmentation (`moving_run_id`, `pause_duration_s`, `moving_run_dur_s`, `pause_count`, `is_long_pause`) | Removed (v4). Derive at analysis time from `state` transitions if needed. |
| `pressure_hpa` | LPS22HH is read by the STM32 for local UART debug only. Not in the wire protocol (ADR-015). |
| GPS / position | No GPS on the shuttle. Position is inferred from mission sequence and shelf layout (future work). |

---

## 6. Live InfluxDB fields (streamed in real time)

Written per-packet to the `stm_telemetry` measurement as the Jetson
receives each packet — no buffer needed. All raw:

`state`, `accel_x`, `accel_y`, `accel_z`, `tx_rate_hz`,
`gyro_x`, `gyro_y`, `gyro_z`, `temp_c`, `humidity_pct`

Gyro/temp/humidity fields are only written when present in the packet
(IDLE packets may omit them). Mission summaries are written to
`stm_mission` (`packets`, `duration_ms`) on each flush.

---

## 7. Reading Parquet files in Python

```python
import pandas as pd
import glob

# Load all missions for shuttle 1
files = sorted(
    glob.glob("/app/ram_buffer/mission_s1_*.parquet"),
    key=lambda f: int(f.split("_")[-1].replace(".parquet", ""))
)
df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
df.sort_values("seq", inplace=True)

# Packet loss rate
total_expected = df["seq"].iloc[-1] - df["seq"].iloc[0] + 1
loss_rate = df["seq_gap"].sum() / total_expected
print(f"Packet loss: {loss_rate:.1%}")

# accel_mag is derived, not stored — compute it from raw axes.
df["accel_mag"] = (df["accel_x"]**2 + df["accel_y"]**2 + df["accel_z"]**2)**0.5

# Separate MOVING and IDLE segments.
moving = df[df["state"] == 1]
idle   = df[df["state"] == 0]
print(f"MOVING packets: {len(moving)}  |  IDLE packets: {len(idle)}")
print(moving[["seq", "accel_mag", "accel_z", "seq_gap"]].describe())
```

---

## 8. Quick reference — what each column tells you

```
seq_gap > 0             → packet dropped here (WiFi dead zone at this route position)
state = 0               → shuttle stopped; expect 0.1 Hz data rate
state = 1               → shuttle moving; expect 10 Hz data rate
accel_z ≈ 1.0           → upright on flat surface (normal)
accel_z ≠ 1.0           → tilt or vertical shock
gyro_x/y AC noise       → bearing or motor fault vibration
gyro_z large            → turning at end of shelf row
accel_mag (derived)     → > 1.05 g dynamic motion; ≈ 1.0 g at rest
rolling_accel_std_10    → higher = rougher surface or more vibration
```
