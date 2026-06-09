# Parquet Schema — PLUDOS Shuttle Telemetry

Each Parquet file represents one **mission flush** for one shuttle: the
complete telemetry buffer from the first MOVING packet until 30 s of
subsequent IDLE (or a mid-mission buffer-pressure overflow). Multiple
files may exist per shuttle per day.

**Schema version:** v4 (raw-only). The gateway now stores **only raw
signal** — no derived columns (`accel_mag`, `gyro_mag`, distance, energy,
segmentation). All feature engineering happens at train time in
`anomaly.py:_derive_features()`. This keeps the data-engine a pure
collector and minimises Jetson CPU / SD-card load. Files produced by
earlier versions will additionally carry derived columns that v4 ignores.

---

## File naming

```
{prefix}_s{shuttle_id}_{ts_ms}.parquet
```

- `prefix` — `mission` (normal end or pressure flush)
- `shuttle_id` — 1-based integer (matches `SHUTTLE_ID` in firmware)
- `ts_ms` — Unix timestamp in **milliseconds** at flush time

Example: `mission_s1_1747123456789.parquet`

The shuttle_id and millisecond precision together guarantee that two
shuttles flushing within the same second produce distinct filenames —
the bug that caused one shuttle's file to overwrite the other's.

---

## Flush triggers

| Trigger | Condition | State reset? |
|---|---|---|
| Mission end | shuttle stays IDLE for ≥ 30 s after any MOVING run (`MISSION_END_IDLE_S`) | Yes |
| Time cap | shuttle buffer open longer than `BUFFER_MAX_AGE_S` (default 300 s) wall-clock | Yes |
| Soft limit | shuttle buffer reaches 3 000 packets | No — mission continues |
| Hard limit | shuttle buffer reaches 4 500 packets | No — mission continues |
| Gateway ceiling | all-shuttle total reaches 100 000 packets | No |
| Shutdown | `podman stop` (SIGTERM) — **not** caught; in-flight buffer is lost | — |

Soft and hard limit flushes produce additional files for the same mission.
Load all files sorted by `ts_ms` in the filename to reconstruct the full
mission in sequence order.

---

## Columns

### Identity and timing

| Column | Type | Unit | Description |
|---|---|---|---|
| `timestamp` | `pd.Timestamp` (UTC) | — | STM32 `HAL_GetTick()` anchored to gateway NTP wall clock. Per-shuttle offset = `receipt_time_ms − tick_ms`, refreshed every 100 packets to correct crystal drift. Sort by `seq`, not `timestamp` — NTP jitter can cause small out-of-order timestamps. (This NTP-offset scheme is the **live `:5683` stream only**. High-rate drain captures — the separate `cap_accel_*`/`cap_gyro_*` files — instead recover capture time as `BEGIN_arrival − (tx_tick − t0_tick)`; see `docs/wire_protocol.md §2`.) |
| `shuttle_id` | int8 | — | 1-based integer. Set via `SHUTTLE_ID` in `wifi_credentials.h`. Maps to a human name via `SHUTTLE_NAMES` env var (default `shuttle-N`). |
| `seq` | int32 | — | Monotonic packet counter. The uint16 wire value (wraps at 65 535) is unwrapped by the gateway into a globally unique sort key. Always use `seq` for ordering, not `timestamp`. |
| `seq_gap` | int16 | packets | Packets lost **before** this row = `seq[i] − seq[i−1] − 1`. Zero means no loss; 1 means one packet was dropped. First row in each file is always 0. Non-zero values cluster at WiFi dead zones (metal shelving, elevator shaft entry) — this is a position-correlated ML feature. |

### Motion state

| Column | Type | Unit | Description |
|---|---|---|---|
| `state` | int8 | — | `0` = IDLE (shuttle stationary), `1` = MOVING (shuttle in transit). Derived from the STM32 FSM — see `docs/state_machine.md`. |

### Accelerometer (ISM330DHCX)

| Column | Type | Unit | Description |
|---|---|---|---|
| `accel_x` | float16 | g | Lateral acceleration (left/right relative to shelf row). ±2 g full scale. NaN if ISM330 I²C read failed (sentinel 0x7FFF on wire). |
| `accel_y` | float16 | g | Forward/backward along the direction of travel. |
| `accel_z` | float16 | g | Vertical. At rest on flat ground ≈ 1.00 g (gravity). Bearing wear shows as AC noise on this channel. |

Precision: rounded to 2 decimals before storage (wire is int16 × 100, so
0.01 g resolution). Stored as float16 to halve file size — finer than the
0.01 g wire quantum across the ±2 g range.

### Gyroscope (ISM330DHCX)

| Column | Type | Unit | Description |
|---|---|---|---|
| `gyro_x` | float16 | dps | Roll rate. Torsional vibration from motor/bearing faults appears here. ±250 dps full scale, 8.75 mdps/LSB sensitivity. NaN if ISM330 gyro init failed. |
| `gyro_y` | float16 | dps | Pitch rate. |
| `gyro_z` | float16 | dps | Yaw rate (turns and curves along the shelf row). |

Note: a small zero-rate offset (typically ±0.5 dps) is normal for the
ISM330 at power-on without factory calibration. The ML model will
learn around it since it is consistent per device.

Precision: rounded to 2 decimals before storage (wire is int16 × 100).
Stored as float16; the ±250 dps range exceeds float16's exact-integer
limit, so the LSB is coarser than 0.01 dps at large rates — irrelevant
given 10 Hz sampling already aliases vibration.

### Environment (HTS221)

| Column | Type | Unit | Description |
|---|---|---|---|
| `temp_c` | float16 | °C | Ambient temperature. NaN if HTS221 I²C read failed. Typical warehouse: 15–25 °C. Elevated readings may indicate motor heat near the sensor. |
| `humidity_pct` | float16 | %RH | Relative humidity. NaN if HTS221 failed. Rounded to 1 decimal (wire is int16 × 10). |

---

## Columns computed at training time (not in Parquet)

These are derived in `anomaly.py:_derive_features()` once per FL round,
after the recent Parquet files are loaded and sorted by `(shuttle_id,
seq)`. They are **not stored** — store-raw, derive-at-train-time. The CNN
labeller ignores them (it consumes raw axes directly); IsolationForest
and XGBoost use them.

| Column | Description |
|---|---|
| `accel_mag` | √(accel_x² + accel_y² + accel_z²). Total acceleration magnitude; ≈ 1.0 g at rest. |
| `gyro_mag` | √(gyro_x² + gyro_y² + gyro_z²). Aggregate rotation magnitude. |
| `rolling_accel_std_10` | 10-packet rolling std of `accel_mag` (`min_periods=2`, leading NaN filled 0). Sustained-vibration / bearing-wear proxy. |

---

## What is missing / will never be here

- **`accel_mag` / `gyro_mag` / `rolling_accel_std_10`** — derived columns;
  no longer stored. Computed at train time (see section above).
- **`distance_m_cum` / `displacement_m` / `speed_ms`** — ZUPT distance
  integration removed entirely (v4). The 1-D integrator drifted badly at
  10 Hz and the figure was not trustworthy. Recompute downstream from raw
  accel if needed; do not treat as ground truth.
- **`energy_j` / `power_mw`** — per-packet energy estimate removed (v4).
  Gateway/FL-round energy is measured separately (Alumet, server-side
  `fl_phases`), not derived per telemetry packet.
- **Mission segmentation** (`moving_run_id`, `pause_duration_s`,
  `moving_run_dur_s`, `pause_count`, `is_long_pause`) — removed (v4).
  Derive at analysis time from `state` transitions if needed.
- **`pressure_hpa`** — LPS22HH is read on the STM32 for local UART debug but not transmitted (ADR-015). Not in the wire protocol.
- **GPS / position** — No GPS on the shuttle. Position is inferred from mission sequence number and shelf layout (future work).

---

## Quick start — reading files in Python

```python
import pandas as pd
import glob, os

# Load all mission files for shuttle 1.
files = sorted(glob.glob("/app/ram_buffer/mission_s1_*.parquet"))
df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
df.sort_values("seq", inplace=True)

# Check packet loss rate for this mission.
loss_rate = df["seq_gap"].sum() / (df["seq"].iloc[-1] - df["seq"].iloc[0] + 1)
print(f"Packet loss: {loss_rate:.1%}")

# Separate MOVING and IDLE segments.
moving = df[df["state"] == 1]
idle   = df[df["state"] == 0]

print(f"MOVING packets: {len(moving)}  |  IDLE packets: {len(idle)}")

# accel_mag is derived, not stored — compute it from the raw axes.
moving = moving.assign(
    accel_mag=(moving["accel_x"]**2 + moving["accel_y"]**2 + moving["accel_z"]**2)**0.5
)
print(moving[["seq", "accel_mag", "accel_z", "seq_gap"]].describe())
```
