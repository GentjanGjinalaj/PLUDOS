# PLUDOS Data Guide — Parquet Schema Reference

This document describes every column in the Parquet files produced by
`data-engine.py`, including the formulas and reasoning behind every
derived column. Read this before touching `client.py`'s `feature_cols`
or adding new training columns.

---

## 1. How data flows into Parquet

```
STM32 firmware (10 Hz MOVING / 0.1 Hz IDLE)
  │  24-byte UDP packet
  ▼
data-engine.py  (Jetson)
  ├── in-memory per-shuttle buffer
  ├── on mission-end (30 s IDLE after MOVING): flush → Parquet
  └── on buffer pressure (soft/hard limit): intermediate flush
```

One Parquet file = one mission flush for one shuttle.
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

## 3. Column reference (30 columns)

### 3.1 Identity and timing

| Column | dtype | Unit | Description |
|---|---|---|---|
| `timestamp` | datetime64[ns, UTC] | — | STM32 `HAL_GetTick()` anchored to the Jetson's NTP wall clock. Each shuttle has its own offset (`receipt_time_ms − tick_ms`), refreshed every 100 packets to compensate STM32 crystal drift. **Do not sort by `timestamp`** — NTP jitter can cause small out-of-order values. |
| `shuttle_id` | int8 | — | 1-based integer. Set at firmware build time via `SHUTTLE_ID` in `wifi_credentials.h`. Maps to a human label via the `SHUTTLE_NAMES` env var (default: `shuttle-N`). |
| `seq` | int32 | — | Monotonic packet counter. The wire value is uint16 (wraps at 65 535); the gateway unwraps it into a globally unique sort key. **Always use `seq` for ordering**, never `timestamp`. |
| `seq_gap` | int16 | packets | `seq[i] − seq[i−1] − 1`. Zero means no loss. Non-zero values cluster at WiFi dead zones (metal shelving, elevator shaft entry). Position-correlated signal — useful ML feature for identifying where on the route a failure occurred. First row in each file is always 0. |
| `state` | int8 | — | `0` = IDLE (stopped, 0.1 Hz TX). `1` = MOVING (in transit, 10 Hz TX). Derived from the STM32 FSM — see `docs/state_machine.md`. |

---

### 3.2 Energy

| Column | dtype | Unit | Description |
|---|---|---|---|
| `energy_j` | float32 | J | Cumulative energy consumed by this shuttle since the current mission started, in Joules. Computed on the gateway: power × elapsed time, where power = 89 mW (IDLE) or 260 mW (MOVING). Resets to 0 at each mission-end flush. The 89/260 mW constants are rough estimates from bench measurement — calibrate against an INA3221 reading before using as a ground-truth energy label. |

---

### 3.3 Accelerometer — raw (ISM330DHCX, ±2 g full scale)

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

---

### 3.4 Accelerometer — derived

#### `accel_mag` — float16, g
```
accel_mag = √(accel_x² + accel_y² + accel_z²)
```
Total acceleration magnitude. At rest on flat ground ≈ 1.00 g (pure gravity).
Deviations above ≈ 1.05 g indicate dynamic motion; below ≈ 0.95 g indicates
tilt. The STM32 FSM uses a squared deviation from 1.0 (threshold 0.05 g²)
to detect movement — `accel_mag` captures the same signal for the ML model.
NaN if any accel axis is NaN.

#### `accel_jerk` — float16, g/s
```
accel_jerk[i] = |accel_mag[i] − accel_mag[i−1]| / dt[i]
```
Rate of change of the total acceleration magnitude between consecutive
packets. High values correspond to sudden impacts, abrupt braking, shelf
collisions, or drive belt snaps. NaN on the first row of each file (no
previous packet to diff against).

#### `horizontal_accel` — float16, g
```
horizontal_accel = √(accel_x² + accel_y²)
```
Acceleration in the horizontal plane only — **gravity is removed**. Where
`accel_mag` always includes the ~1 g gravity component, `horizontal_accel`
is near 0 when the shuttle is stationary (IDLE) and non-zero only during
actual horizontal movement. This makes it a cleaner motion signal than
`accel_mag` alone and is more directly proportional to the kinetic energy
of travel.

Why not just use `accel_y`? Because the exact mounting orientation of the
sensor may shift slightly between shuttle reassemblies. `horizontal_accel`
is rotation-invariant around the Z axis — it works correctly regardless of
which way the sensor is rotated in the horizontal plane.

#### `tilt_angle_deg` — float16, degrees
```
tilt_angle_deg = arccos(clip(accel_z / accel_mag, −1, 1)) × 180/π
```
Angle between the sensor's Z axis and the gravity vector, expressed in
degrees.

| Value | Meaning |
|---|---|
| 0° | Sensor perfectly flat / upright (Z aligned with gravity) |
| 45° | Sensor tilted at 45° (shelving lean, payload shift) |
| 90° | Sensor sideways |
| > 90° | Sensor inverted |

At rest on a flat floor this should be close to 0°. Persistent elevated
values during a mission indicate shelf misalignment or a payload that has
shifted. The `clip(−1, 1)` guard prevents `math.acos` domain errors from
floating-point rounding when `accel_z / accel_mag` is very slightly outside
[−1, 1].

NaN if `accel_mag` is 0 (free-fall condition — extremely unlikely in
normal operation).

---

### 3.5 Gyroscope — raw (ISM330DHCX, ±250 dps full scale)

Wire encoding: int16 × 100 → 0.01 dps resolution.
Sentinel 0x7FFF → NaN. A zero-rate offset of ±0.5 dps at power-on is
normal (ISM330 without factory calibration); the ML model learns around it
since it is consistent per device.

| Column | dtype | Unit | Description |
|---|---|---|---|
| `gyro_x` | float16 | dps | Roll rate. Torsional vibration from a damaged bearing or motor shows up here as AC noise at frequency proportional to shaft speed. |
| `gyro_y` | float16 | dps | Pitch rate. Changes when the shuttle nose dips during shelf approach or payload load shift. |
| `gyro_z` | float16 | dps | Yaw rate. Captures turns at the end of shelf rows. |

All three become NaN together if the ISM330DHCX gyro init fails.

---

### 3.6 Gyroscope — derived

#### `gyro_mag` — float16, dps
```
gyro_mag = √(gyro_x² + gyro_y² + gyro_z²)
```
Total rotation rate magnitude. NaN if any gyro axis is NaN.

#### `gyro_jerk` — float16, dps/s
```
gyro_jerk[i] = |gyro_mag[i] − gyro_mag[i−1]| / dt[i]
```
Rate of change of angular velocity — the rotational equivalent of `accel_jerk`.
High values indicate sudden starts/stops of rotation: shelf impact, emergency
brake, or a motor fault causing instantaneous reversal. Parquet-only (requires
two consecutive packets with known dt). NaN on the first row.

---

### 3.7 Rolling context (1-second window at 10 Hz MOVING rate)

Both columns use a trailing window of 10 packets (= 1 second of MOVING data
at 10 Hz). `min_periods=1` avoids NaN at the start of a buffer; std requires
≥ 2 points so the first row gets 0.

#### `rolling_accel_mean_10` — float16, g
Trailing mean of `accel_mag` over the last 10 packets. Smooths out
individual packet noise. Low and stable during straight travel; rises
during acceleration/braking phases. Used to give XGBoost a 1-second
context window rather than a single-packet snapshot.

#### `rolling_accel_std_10` — float16, g
Trailing standard deviation of `accel_mag` over the last 10 packets.
High std = high vibration variance = rough surface or mechanical wear.
Low std = smooth, consistent motion. This is the primary **surface roughness
proxy** — concrete vs wooden floor vs cobblestones produce distinct std
signatures in lab tests.

Parquet-only (requires a window of prior packets).

---

### 3.8 Environment (HTS221)

| Column | dtype | Unit | Description |
|---|---|---|---|
| `temp_c` | float16 | °C | Air temperature near the shuttle. Typical warehouse: 15–25 °C. Elevated readings during IDLE may indicate motor heat soaking into the board. Resolution: 0.01 °C. NaN if HTS221 I²C read failed. |
| `humidity_pct` | float16 | %RH | Relative humidity. Resolution: 0.1 %. NaN if HTS221 failed. |

---

### 3.9 Kinematic estimates — ZUPT integration

These three columns use Zero-velocity UPdate (ZUPT) integration to estimate
shuttle speed and position from accelerometer data. They are computed at
flush time on the Jetson — **not transmitted by the STM32**.

#### Why ZUPT?

The shuttle has no GPS or wheel encoder stream in the telemetry protocol.
The only speed signal is the accelerometer. Pure double integration of
acceleration drifts quickly due to sensor bias and noise. ZUPT exploits
the fact that the shuttle repeatedly stops (IDLE) — at each stop, velocity
is known exactly (zero), so the integration is reset, bounding drift to
a single travel leg.

#### `mission_elapsed_s` — float32, seconds
```
mission_elapsed_s[i] = (timestamp[i] − timestamp[0]).total_seconds()
```
Seconds elapsed since the first packet in this flush buffer. Starts at 0.0.
Gives XGBoost a **temporal position** within the mission: early packets are
on the approach, late packets are on the return leg.

Note: when a mission spans multiple Parquet files (buffer-pressure flushes),
this resets to 0 at the start of each file. It is relative to the flush
buffer, not to the physical mission start.

#### `speed_ms` — float32, m/s
```
# At each IDLE → MOVING transition:
vel = 0.0   # ZUPT reset

# Each MOVING packet:
a_h = √(accel_x² + accel_y²)          # horizontal acceleration (g)
vel = max(0, vel + a_h × 9.81 × dt)   # integrate; clamp to ≥ 0
```
Estimated horizontal speed at each packet, in m/s. `dt` is the actual
inter-packet elapsed time from NTP-anchored timestamps (not a fixed
constant). Velocity is forced to zero on IDLE packets and reset on
each IDLE→MOVING boundary so that each travel leg starts clean.

Interpretation: high `speed_ms` = fast travel; low `speed_ms` = slow
approach or heavy load. Not calibrated — use as a relative proxy.

#### `displacement_m` — float32, m
```
# Each MOVING packet:
disp += vel × dt
```
Cumulative horizontal distance since the start of the most recent
IDLE→MOVING transition, in metres. Accumulates across the entire mission
(does not reset at in-mission IDLE pauses — only at the IDLE→MOVING
transition that starts a new travel leg).

Interpretation: higher `displacement_m` at mission end = longer route.
Not calibrated.

---

### 3.10 Mission segmentation

These five columns label each packet with its structural role within the
mission. They are computed in `_flush()` using a two-pass scan over the
`state` column — they require seeing the complete buffer to determine
MOVING/IDLE run boundaries, so they are Parquet-only.

---

#### Mission structure primer

A typical shuttle mission generates this `state` pattern:

```
IDLE  (waiting at home)
MOVING  ← run 1: approach shelf
IDLE    ← in-mission pause: pick box  (2–5 s normal)
MOVING  ← run 2: carry to elevator
IDLE > 30 s  → mission-end flush
```

A complex mission (shelf-to-shelf):

```
MOVING(1) → IDLE(pause 1: pick) → MOVING(2) → IDLE(pause 2: place) → MOVING(3) → long IDLE
```

---

#### `moving_run_id` — int8, dimensionless

1-based index of the current MOVING run within the flush buffer.
0 on any IDLE packet that precedes the first MOVING run (pre-mission).

| Value | Meaning |
|---|---|
| 0 | Pre-mission IDLE (no MOVING run seen yet in buffer) |
| 1 | First travel leg (approach) |
| 2 | Second travel leg (after first pick/pause) |
| 3+ | Third and subsequent legs (complex missions) |

Gives XGBoost positional context: model can learn that vibration patterns
differ between approach (empty shuttle) and return (loaded shuttle).

---

#### `pause_duration_s` — float32, seconds

Duration of the current **in-mission** IDLE stop. 0 on all MOVING packets
and on pre/post-mission IDLE packets.

An in-mission pause is defined as an IDLE run that has at least one MOVING
run before it **and** at least one MOVING run after it in the buffer. This
distinguishes it from the terminal IDLE that ends the mission.

Formula (pass-1 segment scan):
```
For a contiguous IDLE run from packet s_start to s_end:
  dur = mission_elapsed_s[s_end−1] − mission_elapsed_s[s_start]
```
Every packet in that IDLE run gets `pause_duration_s = dur`.

---

#### `moving_run_dur_s` — float32, seconds

Duration of the current MOVING run, in seconds. 0 on IDLE packets.

Every packet in a given MOVING run gets the same value: the span from the
first to the last packet of that run. Short values = nearby shelf; long
values = end-of-row travel.

---

#### `pause_count` — int8, dimensionless

Cumulative number of **in-mission** pauses that have completed before this
packet. The count increments when a pause ends (at the next MOVING run),
not when it begins.

| Value | Meaning |
|---|---|
| 0 | No pauses yet — shuttle is on the approach leg |
| 1 | One pause done — box picked, now travelling to deposit |
| 2 | Two pauses — complex multi-stop mission |

---

#### `is_long_pause` — int8, 0 or 1

```
is_long_pause = 1  if pause_duration_s > RETRY_PAUSE_THRESHOLD_S  else 0
```
where `RETRY_PAUSE_THRESHOLD_S` defaults to 8.0 s (configurable via env var).

A long in-mission pause indicates a **retry event**: the shuttle reached the
pick position but could not grab the box (misalignment, box not present,
gripper fault) and held position waiting for a retry command. The threshold
is heuristic — validate against labelled retry incidents from Savoye before
using this as a ground-truth anomaly label in the thesis.

Non-pause IDLE (pre/post-mission) always gets `is_long_pause = 0`.

---

## 4. Columns NOT in Parquet

| Name | Why absent |
|---|---|
| `power_mw` | Derived from `state` (89 / 260 mW). Omitted to avoid baking a rough estimate into the training data as if it were measured. Compute downstream if needed: `df['state'].map({0: 89.0, 1: 260.0})`. |
| `pressure_hpa` | LPS22HH is read by the STM32 for local UART debug only. Not in the wire protocol (ADR-015). |
| `interval_ms` | Older schema column superseded by deriving `dt` from actual timestamps at flush time. |
| GPS / position | No GPS on the shuttle. Position is inferred from mission sequence and shelf layout (future work). |

---

## 5. Live InfluxDB fields (subset, streamed in real time)

These fields are written per-packet to `stm_telemetry` measurement as the
Jetson receives each packet — no buffer needed:

`accel_x`, `accel_y`, `accel_z`, `accel_mag`, `horizontal_accel`,
`tilt_angle_deg`, `gyro_x`, `gyro_y`, `gyro_z`, `gyro_mag`,
`temp_c`, `humidity_pct`, `state`, `energy_j`, `seq_gap`

The derived columns that require the full buffer (`gyro_jerk`,
`rolling_accel_*`, `mission_elapsed_s`, `speed_ms`, `displacement_m`,
and all five segmentation columns) are **Parquet-only**.

---

## 6. Reading Parquet files in Python

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

# Mission structure
print(df.groupby("moving_run_id")[["accel_mag", "speed_ms", "displacement_m"]].mean())

# Retry events
retries = df[df["is_long_pause"] == 1]
print(f"Retry-suspect pauses: {retries['pause_duration_s'].unique()}")

# Vibration proxy by travel leg
print(df.groupby("moving_run_id")["rolling_accel_std_10"].mean())
```

---

## 7. Quick reference — what each column tells you

```
seq_gap > 0             → packet dropped here (WiFi dead zone at this route position)
state = 0               → shuttle stopped; expect 0.1 Hz data rate
state = 1               → shuttle moving; expect 10 Hz data rate
accel_z ≈ 1.0           → upright on flat surface (normal)
accel_z ≠ 1.0           → tilt or vertical shock
horizontal_accel ≈ 0    → stationary (gravity cancelled)
horizontal_accel > 0    → horizontal motion in progress
tilt_angle_deg > 5°     → persistent lean (payload shift, shelf misalignment)
accel_jerk high         → sudden impact or abrupt braking
gyro_x/y AC noise       → bearing or motor fault vibration
gyro_z large            → turning at end of shelf row
rolling_accel_std_10    → higher = rougher surface or more vibration
moving_run_id = 1       → approach leg (no box yet)
moving_run_id = 2       → delivery leg (box on board)
pause_count = 0         → still on approach
is_long_pause = 1       → retry event (pick failed, waiting to retry)
displacement_m          → relative route length for this travel leg
energy_j (last row)     → total energy consumed for this mission
```
