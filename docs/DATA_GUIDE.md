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
STM32 firmware  (radio off except to drain — ADR-021)
  │  24-byte UDP packets, sent in bursts during a drain window
  ▼
data-engine.py  (Jetson)
  ├── in-memory per-shuttle buffer
  ├── on mission-end (30 s IDLE after MOVING): flush → mission_*.parquet
  ├── on time cap (BUFFER_MAX_AGE_S, default 300 s): force-flush
  └── on buffer pressure (soft/hard limit): intermediate flush
```

> **ADR-021 — the `mission_*` live path is now dormant.** The firmware keeps the
> radio off except during a drain window (TX gated on `wifi_driver_initialized`),
> so in practice almost no `PludosTelemetry` packets reach the gateway and the
> `mission_*.parquet` files documented here are usually empty or sparse. The
> **real dataset** is the high-rate MOVING vibration (accel 3332 Hz / gyro 416 Hz,
> ≈8:1) and the 12.5 Hz IDLE snapshots, which are drained on UDP `:5684` by
> `drain_receiver.py` and land in `cap_accel_*` / `cap_gyro_*` files. This guide
> documents the legacy `PludosTelemetry` (`mission_*`) schema; for the active
> drain capture schema see `docs/parquet_schema.md §2`, ADR-020/021, and
> `docs/sampling_strategy.md`.

One Parquet file = one flush for one shuttle.
Files are named `mission_s{id}_{unix_ms}.parquet`.

A single physical mission can produce multiple files if the buffer fills
mid-mission (soft limit 3 000 packets). Always sort files by the timestamp in
the filename and concatenate before analysis.

---

## 2. Sampling rates

| State    | Internal FSM poll | Captured data rate (to PSRAM, drained later) |
|----------|-------------------|----------------------------------------------|
| IDLE     | 10 Hz (`SAMPLE_PERIOD_IDLE_MS`)   | 12.5 Hz snapshot, 10 s every 10 min |
| MOVING   | 50 Hz (`SAMPLE_PERIOD_MOVING_MS`) | accel 3332 Hz / gyro 416 Hz (≈8:1) |

The firmware polls the IMU at 10 Hz in IDLE / 50 Hz in MOVING purely to run the
motion FSM (entry uses a 500 ms dwell = 5 consecutive above-threshold samples at
the 10 Hz IDLE poll). Those poll rates are **not** data or TX rates: the signal
PLUDOS keeps is the high-rate PSRAM capture, drained over UDP after each run
(ADR-021).

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
| `state` | int8 | — | `0` = IDLE (stopped). `1` = MOVING (in transit). Derived from the STM32 FSM — see `docs/state_machine.md`. |

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
| `rolling_accel_std_10` | 10-packet rolling std of `accel_mag` (`min_periods=2`, leading NaN → 0) | 10-packet window. Primary surface-roughness / bearing-wear proxy — high std = high vibration variance. |

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

## 6. InfluxDB measurements

The gateway writes three measurements. Two come from the active drain path
(`drain_receiver.py`); the third is from the legacy live path and is now
effectively dead. See `docs/ANALYTICS.md §3` for the full field/tag schema.

- **`stm_mission`** (drain summary, the live one Grafana actually shows) —
  one point per drained mission/snapshot, tagged `source="drain"`,
  `kind="mission"|"idle_snapshot"`. Fields: `packets_total`,
  `packets_received`, `packets_lost`, `loss_pct`, `accel_samples`,
  `gyro_samples`, `complete`, `accel_rms_g`, `accel_peak_g`,
  `gyro_peak_dps`, and (idle snapshots) `temp_c`, `pressure_hpa`. Dashboards
  filter on `source=="drain"`.
- **`stm_idle_wave`** (drain, idle snapshots only) — per-sample waveform at
  the snapshot ODR. Fields: `ax_g`, `ay_g`, `az_g`, and `gx_dps`/`gy_dps`/
  `gz_dps` when gyro is present.
- **`stm_telemetry`** (legacy live path, **not written under ADR-021**) — was
  written per live packet (`state`, `accel_*`, `gyro_*`, `temp_c`,
  `humidity_pct`, `tx_rate_hz`). The radio is off outside drains, so the live
  path is dead: `data-engine.py` never instantiates this measurement and no
  Grafana panel queries it. Current data lives in `stm_mission` (per-mission
  summary) and `stm_idle_wave` (idle waveform).

Board power on the Jetson itself is measured by the alumet-relay sidecar
(`input_current` / `input_voltage`, ADR-011), not by the data-engine.

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
state = 0               → shuttle stopped (IDLE)
state = 1               → shuttle moving (MOVING)
accel_z ≈ 1.0           → upright on flat surface (normal)
accel_z ≠ 1.0           → tilt or vertical shock
gyro_x/y AC noise       → bearing or motor fault vibration
gyro_z large            → turning at end of shelf row
accel_mag (derived)     → > 1.05 g dynamic motion; ≈ 1.0 g at rest
rolling_accel_std_10    → higher = rougher surface or more vibration
```
