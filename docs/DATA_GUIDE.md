# PLUDOS Telemetry Data Guide

Each Parquet file is one mission segment for one shuttle. A mission starts
when the shuttle begins moving and ends after it has been idle for 30 seconds.
If the buffer fills up mid-mission (every ~1000 packets ≈ 20 s at 50 Hz),
an intermediate file is written and the mission continues into the next file.

---

## File naming

```
mission_s{shuttle_id}_{unix_ms}.parquet
```

Example: `mission_s1_1779285208608.parquet` = shuttle 1, flushed at that timestamp.

Sort files by the number in the name to get chronological order.

---

## Columns

### Who and when

| Column | Type | Description |
|---|---|---|
| `timestamp` | datetime UTC | When the packet was received, anchored to the Jetson's NTP clock. Use `seq` for ordering — timestamp can have small jitter. |
| `shuttle_id` | int8 | Which shuttle. 1-based (1, 2, 3…). |
| `seq` | int32 | Packet counter, always increasing. Use this to sort rows and detect gaps. |
| `seq_gap` | int16 | How many packets were lost before this row. **0 = no loss**, 1 = one packet dropped, etc. Persistent non-zero values at the same position in the route indicate a WiFi dead zone. |
| `state` | int8 | **0 = IDLE** (shuttle stopped, sends 1 packet/s), **1 = MOVING** (shuttle in transit, sends 50 packets/s). |
| `energy_j` | float32 | Cumulative energy consumed by this shuttle since the mission started, in Joules. Estimated from state: 89 mW when IDLE, 260 mW when MOVING. Resets to 0 at the start of each mission. |

### Accelerometer (ISM330DHCX — ±2 g, sampled at 50 Hz)

The shuttle moves horizontally along a shelf row. Gravity is on Z.

| Column | Type | Description |
|---|---|---|
| `accel_x` | float16 g | Left/right (lateral). Near 0 during straight travel. |
| `accel_y` | float16 g | Forward/backward (direction of travel). Peaks on acceleration and braking. |
| `accel_z` | float16 g | Vertical. ≈ 1.0 g at rest (gravity). Drops when decelerating into a shelf. Vibration/noise here indicates bearing or motor wear. |
| `accel_mag` | float16 g | √(x²+y²+z²). ≈ 1.0 at rest. The STM32 uses this to detect movement (threshold 0.05 g² deviation from 1.0). |
| `accel_jerk` | float16 g/s | Rate of change of accel_mag between packets. High values = sudden impact or abrupt start/stop. NaN on the first row of each file. |

Resolution: 0.01 g (limited by the int16 wire encoding).
Sensor not available: all accel columns become NaN.

### Gyroscope (ISM330DHCX — ±250 dps, sampled at 50 Hz)

| Column | Type | Description |
|---|---|---|
| `gyro_x` | float16 dps | Roll rate. Torsional vibration from a damaged bearing or motor shows up here. |
| `gyro_y` | float16 dps | Pitch rate. Changes when the shuttle nose dips (load shift, shelf approach). |
| `gyro_z` | float16 dps | Yaw rate. Captures turns at the end of shelf rows. |
| `gyro_mag` | float16 dps | √(gx²+gy²+gz²). Total rotation rate. |

Note: a small constant offset of ±0.5 dps at rest is normal (ISM330 zero-rate
offset without factory calibration). It is consistent per device and the model
will learn around it.
Resolution: 0.01 dps.
Sensor not available: all gyro columns become NaN.

### Environment (HTS221)

| Column | Type | Description |
|---|---|---|
| `temp_c` | float16 °C | Air temperature near the shuttle. Typical warehouse: 15–25 °C. Rising values near the motor could indicate overheating. Resolution: 0.01 °C. |
| `humidity_pct` | float16 % | Relative humidity. Resolution: 0.1 %. |

Sensor not available: column becomes NaN.

---

## Quick reference — what each column tells you

```
seq_gap  > 0   → packet lost here (WiFi shadow at this route position)
state    = 0   → shuttle stopped
state    = 1   → shuttle moving (data at 50 Hz)
accel_z  ≠ 1.0 → tilt or vertical shock
accel_mag ≈ 1  → moving smoothly
accel_jerk > 0 → sudden acceleration change (bump, braking, impact)
gyro_z   large → turning
gyro_x/y large → torsional vibration or tilt
energy_j       → cumulative Joules since mission start
```

---

## Reading files in Python

```python
import pandas as pd
import glob

# Load all files for shuttle 1, sorted by time
files = sorted(glob.glob("mission_s1_*.parquet"))
df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
df.sort_values("seq", inplace=True)

# Separate MOVING and IDLE
moving = df[df["state"] == 1]
idle   = df[df["state"] == 0]

# Packet loss rate for the mission
total_expected = df["seq"].iloc[-1] - df["seq"].iloc[0] + 1
loss_rate = df["seq_gap"].sum() / total_expected
print(f"Packet loss: {loss_rate:.1%}")

# Peak jerk (impacts)
print(f"Max accel_jerk: {moving['accel_jerk'].max():.2f} g/s")

# Total energy
print(f"Mission energy: {df['energy_j'].iloc[-1]:.2f} J")
```

---

## Sampling rates

| State | STM32 internal sample | What arrives at Jetson |
|---|---|---|
| IDLE | 10 Hz | 1 Hz (every 10th sample) |
| MOVING | 50 Hz | 50 Hz (every sample) |

The STM32 decides the state based on accel_mag. Once MOVING, it stays
MOVING until 5 consecutive samples are below the movement threshold.
The Jetson just receives whatever comes in — it never controls the rate.

---

## Download files from Jetson to your laptop

```bash
# All files
scp warehouse1@192.168.0.100:'~/PLUDOS/client/ram_buffer/*.parquet' ./pludos_data/

# One shuttle only (e.g. shuttle 1)
scp warehouse1@192.168.0.100:'~/PLUDOS/client/ram_buffer/mission_s1_*.parquet' ./pludos_data/

# Latest file only
ssh warehouse1@192.168.0.100 "ls -t ~/PLUDOS/client/ram_buffer/*.parquet | head -1" \
  | xargs -I{} scp warehouse1@192.168.0.100:{} ./
```
