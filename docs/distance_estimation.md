# Distance Estimation — 1D-ZUPT for Savoye XTPS Rail Motion (T2.2 / ADR-017)

## Context

The Savoye XTPS shuttle moves strictly in one dimension: forward/backward on a
horizontal rail (one shuttle per minifloor). It deploys telescopic arms
**perpendicularly** to the rail to pick and place boxes. The shuttle body does
**not** translate perpendicular to the rail.

Cumulative path length (`distance_m_cum`) serves as a wear proxy for the bearing
health classifier: a shuttle that has traveled further since last maintenance is
more likely to show vibration anomalies.

### Why not ZUPT on raw magnitude?

The original attempt used `|√(ax²+ay²)|` (unsigned horizontal magnitude):
- Unsigned magnitude means velocity can only grow — deceleration is ignored.
- Mixes the rail axis with the perpendicular arm-deployment axis.
- No gravity removal, no reset boundary.
- Result: unbounded drift. Column was removed (T2.1).

## Algorithm

### Step 1 — Auto-detect the track axis

For each flush buffer, compute the variance of `accel_x` and `accel_y`
**on MOVING packets only**:

```
var_x = var(accel_x during MOVING)
var_y = var(accel_y during MOVING)
track_a = accel_x  if var_x ≥ var_y  else accel_y
```

**Why it works:** The axis aligned with the rail experiences the full shuttle
acceleration/deceleration profile. The perpendicular axis sees only arm
vibration and sensor noise — its variance is far lower during travel.
No calibration required; the choice updates every flush buffer.

### Step 2 — High-pass filter (mounting-tilt DC removal)

```
HPF[i] = track_a[i] - mean(track_a[i-W+1 : i+1])
```

Window `W = DISTANCE_HPF_WINDOW` (default 20 packets ≈ 2 s at 10 Hz MOVING).
Removes the DC offset caused by imperfect sensor mounting angle. Without this,
a 1° tilt at ±2 g full-scale ≈ 0.034 g DC → 0.33 m/s² → unbounded velocity growth.

Approximate cutoff: `f_c ≈ 0.44 / (W × dt) = 0.44 / (20 × 0.1 s) ≈ 0.22 Hz`.
This is well below typical shuttle acceleration events (0.5–2 Hz).

### Step 3 — Signed ZUPT integration

```
At MOVING packet i:
    if HPF[i] is valid:
        vel += HPF[i] × 9.81 × dt[i]    # g → m/s², signed
        d   += |vel| × dt[i]             # unsigned path length

At IDLE packet:
    vel = 0    # ZUPT: exact physical constraint
```

**Key insight:** The Savoye XTPS shuttle is **stopped** at `state == IDLE` (the
STM32 state machine only asserts MOVING when crossing the movement threshold).
ZUPT (`vel = 0`) is therefore an **exact** physical constraint, not a heuristic.
This bounds velocity drift to the duration of a single MOVING segment. Even if
integration error accumulates during a 10-second MOVING run, it resets to zero
at the next IDLE packet.

`d += |vel| × dt` accumulates path length regardless of direction
(forward and backward both increase distance), which is the correct wear metric.

## Configuration

| Variable              | Default | Meaning |
|-----------------------|---------|---------|
| `DISTANCE_HPF_WINDOW` | `20`    | Running mean window (packets) for the high-pass filter. |

No calibration constants needed for distance: only `g = 9.81 m/s²` which is fixed.
Track axis is selected automatically per flush.

## Output columns

| Column | Type | Where |
|--------|------|-------|
| `distance_m_cum` | float32 Parquet | Cumulative path length since mission start, per packet |
| `distance_m` | float InfluxDB `stm_mission` | Total per-mission path length (last value of `distance_m_cum`) |

## Expected accuracy

| Scenario | Expected error |
|----------|---------------|
| Clean rail, consistent speed | ±20% (HPF window lag at acceleration start) |
| Short burst (<2 s MOVING) | ±40% (HPF burn-in period covers half the run) |
| Long run (>10 s MOVING) | ±10–15% (HPF stable, integration dominated by noise floor) |
| Arm deployment only, no translation | < 0.05 m artifact (arm axis is discarded) |

The error envelope is acceptable for the thesis goal: a wear-correlated feature
with correct order-of-magnitude values over a full shift. It is not metrological.

## Limitations

- **HPF introduces lag at motion onset.** The first `DISTANCE_HPF_WINDOW` packets
  of each MOVING segment use only past IDLE data for the mean, so the DC removal
  converges over ≈2 s. Short runs (<2 s) underestimate distance.
- **Velocity noise floor during cruise.** With zero mean (shuttle at cruise speed,
  constant acceleration ≈ 0), HPF output is near-zero noise. `|vel| × dt` still
  integrates the noise floor. Reduce `DISTANCE_HPF_WINDOW` if this dominates.
- **Single axis only.** If the IMU is mounted at exactly 45° to the rail, both
  axes have equal variance and the algorithm picks `accel_x` as fallback. Verify
  axis selection from logs: `[DISTANCE] track_axis=accel_x` (logged at INFO).
- **No absolute reference.** Validate the first production run with a tape measure.
