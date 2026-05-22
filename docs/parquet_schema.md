# Parquet Schema — PLUDOS Shuttle Telemetry

Each Parquet file represents one **mission flush** for one shuttle: the
complete telemetry buffer from the first MOVING packet until 30 s of
subsequent IDLE (or a mid-mission buffer-pressure overflow). Multiple
files may exist per shuttle per day.

**Schema version:** v3 (ADR-016). Files produced by earlier versions of
`data-engine.py` will be missing `gyro_*`, `seq_gap`, and `interval_ms`.

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
| Soft limit | shuttle buffer reaches 3 000 packets (≈ 5 min at 10 Hz MOVING) | No — mission continues |
| Hard limit | shuttle buffer reaches 4 500 packets (≈ 7.5 min at 10 Hz MOVING) | No — mission continues |
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
| `timestamp` | `pd.Timestamp` (UTC) | — | STM32 `HAL_GetTick()` anchored to gateway NTP wall clock. Per-shuttle offset = `receipt_time_ms − tick_ms`, refreshed every 100 packets to correct crystal drift. Sort by `seq`, not `timestamp` — NTP jitter can cause small out-of-order timestamps. |
| `shuttle_id` | int8 | — | 1-based integer. Set via `SHUTTLE_ID` in `wifi_credentials.h`. Maps to a human name via `SHUTTLE_NAMES` env var (default `shuttle-N`). |
| `seq` | int32 | — | Monotonic packet counter. The uint16 wire value (wraps at 65 535) is unwrapped by the gateway into a globally unique sort key. Always use `seq` for ordering, not `timestamp`. |
| `seq_gap` | int16 | packets | Packets lost **before** this row = `seq[i] − seq[i−1] − 1`. Zero means no loss; 1 means one packet was dropped. First row in each file is always 0. Non-zero values cluster at WiFi dead zones (metal shelving, elevator shaft entry) — this is a position-correlated ML feature. |
| `interval_ms` | float32 | ms | **Deprecated (v2 schema only).** Superseded by deriving `dt` from actual NTP-anchored timestamps at flush time. Not present in v3 (ADR-016) files. |

### Motion state

| Column | Type | Unit | Description |
|---|---|---|---|
| `state` | int8 | — | `0` = IDLE (shuttle stationary, 0.1 Hz TX), `1` = MOVING (shuttle in transit, 10 Hz TX). Derived from the STM32 FSM — see `docs/state_machine.md`. |

### Accelerometer (ISM330DHCX)

| Column | Type | Unit | Description |
|---|---|---|---|
| `accel_x` | float32 | g | Lateral acceleration (left/right relative to shelf row). ±2 g full scale. NaN if ISM330 I²C read failed (sentinel 0x7FFF on wire). |
| `accel_y` | float32 | g | Forward/backward along the direction of travel. |
| `accel_z` | float32 | g | Vertical. At rest on flat ground ≈ 1.00 g (gravity). Bearing wear shows as AC noise on this channel. |
| `accel_mag` | float32 | g | √(x²+y²+z²). ≈ 1.0 at rest. Deviations > 0.05 g² drive the STM32 movement-detection FSM. NaN if any accel axis is NaN. |

Precision: 2 decimal places (wire is int16 × 100, so 0.01 g resolution).

### Gyroscope (ISM330DHCX)

| Column | Type | Unit | Description |
|---|---|---|---|
| `gyro_x` | float32 | dps | Roll rate. Torsional vibration from motor/bearing faults appears here. ±250 dps full scale, 8.75 mdps/LSB sensitivity. NaN if ISM330 gyro init failed. |
| `gyro_y` | float32 | dps | Pitch rate. |
| `gyro_z` | float32 | dps | Yaw rate (turns and curves along the shelf row). |
| `gyro_mag` | float32 | dps | √(gx²+gy²+gz²). Aggregate rotation magnitude. NaN if any gyro axis is NaN. |

Note: a small zero-rate offset (typically ±0.5 dps) is normal for the
ISM330 at power-on without factory calibration. The ML model will
learn around it since it is consistent per device.

Precision: 2 decimal places (wire is int16 × 100, so 0.01 dps resolution).

### Environment (HTS221)

| Column | Type | Unit | Description |
|---|---|---|---|
| `temp_c` | float32 | °C | Ambient temperature. NaN if HTS221 I²C read failed. Typical warehouse: 15–25 °C. Elevated readings may indicate motor heat near the sensor. |
| `humidity_pct` | float32 | %RH | Relative humidity. NaN if HTS221 failed. Precision: 1 decimal place (wire is int16 × 10). |

---

## Columns computed at training time (client.py only, not in Parquet)

These are derived in `client.py:load_buffered_data()` before XGBoost
training. They are **not stored** in the Parquet files.

| Column | Description |
|---|---|
| `speed_ms` | Estimated horizontal speed (m/s) via ZUPT integration: `vel += sqrt(ax²+ay²) × 9.81 × dt` on MOVING packets (where `dt` is the actual inter-packet elapsed time, not a fixed constant); resets to 0 at each IDLE→MOVING transition to bound drift. Coarse proxy — not calibrated against ground truth. |
| `displacement_m` | Cumulative distance travelled since IDLE→MOVING, in metres. Same caveats as `speed_ms`. |

---

## What is missing / will never be here

- **`pressure_hpa`** — LPS22HH is read on the STM32 for local UART debug but not transmitted (ADR-015). Not in the wire protocol.
- **`power_mw`** — Derived from `state` on the gateway (`POWER_IDLE_MW`/`POWER_MOVING_MW`). Not in Parquet to avoid encoding the estimate as ground truth; compute it downstream if needed.
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
print(moving[["seq", "accel_mag", "gyro_mag", "seq_gap"]].describe())
```
