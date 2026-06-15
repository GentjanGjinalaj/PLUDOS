# STM32 State Machine

Defines the idle/moving FSM that controls sampling rate and telemetry
transmission rate on the STM32U585 edge node.

**Version:** v3 (ADR-015 FSM + ADR-020/021 capture-and-drain). ADR-015 replaced
the CoAP buffer-and-flush model with a unified UDP path; ADR-020/021 then moved
the actual signal off the live stream into a duty-cycled PSRAM capture that is
drained after each run. The FSM below still gates behaviour, but the radio is
off except during a drain window — there is no continuous TX.

> **ADR-021 update — no continuous stream.** The radio is off during IDLE and
> MOVING and powers on only to drain finished captures. The internal FSM poll
> rates below (10 Hz IDLE, 50 Hz MOVING) are still current, but there is **no
> per-sample TX**: MOVING is captured to PSRAM at accel 3332 Hz / gyro 416 Hz
> (≈8:1) and drained after the run; IDLE drains a 12.5 Hz snapshot (10 s every
> 10 min). See ADR-020/021.

---

## States

### STATE_IDLE

- **Internal sampling rate:** 10 Hz (every 100 ms, `SAMPLE_PERIOD_IDLE_MS=100`) —
  needed so the FSM can detect a movement dwell at all
- **Telemetry:** no continuous TX. An IDLE snapshot (12.5 Hz, 10 s every 10 min)
  is captured to PSRAM and drained to the gateway later (ADR-021). `pressure_hpa`
  and `power_mw` are not on the wire (ADR-015 v2); shuttle power/energy is not
  estimated on the gateway either (the `POWER_*_MW` placeholder was removed in the
  schema-v4 raw-only cull).
- **Entry condition:** no above-threshold accelerometer sample for **20 s**
- **Actions on entry:** none beyond logging the transition; the next loop
  iteration continues with `state = 0`

### STATE_MOVING

- **Internal FSM poll:** 50 Hz (every 20 ms, `SAMPLE_PERIOD_MOVING_MS=20`) —
  motion-detection cadence only, not a data or TX rate
- **Telemetry:** no continuous TX. MOVING signal is captured into the ISM330
  FIFO → PSRAM at accel 3332 Hz / gyro 416 Hz (≈8:1) and drained after the run
  (ADR-021)
- **Entry condition:** accelerometer deviation `> 0.06 g²` continuously
  for **500 ms** (with a 300 ms debounce tolerance — see below)
- **Actions on entry:** none beyond logging the transition; the next loop
  iteration continues with `state = 1`

> **Rate note:** the 50 Hz `SAMPLE_PERIOD_MOVING_MS` poll only drives the FSM. The
> data PLUDOS keeps is the high-rate PSRAM capture (accel 3332 Hz / gyro 416 Hz),
> drained over UDP after the run — see ADR-020/021 and `docs/sampling_strategy.md`.

---

## Transitions

```
                    ┌─────────────────────────────────────────┐
                    │  No above-threshold sample for 20 s     │
                    ▼                                          │
              ┌──────────┐                              ┌──────────────┐
  Boot ──────►│  IDLE    │                              │   MOVING     │
              └──────────┘                              └──────────────┘
                    │  Above-threshold continuously             │
                    │  for 500 ms (300 ms debounce)              │
                    └───────────────────────────────────────────►┘
```

---

## Movement detection with debounce

The naïve "reset dwell on any below-threshold sample" approach fails for
real-world motion: at 10 Hz IDLE sampling rate, a normal linear push
produces accelerometer samples that briefly dip below 0.06 g² between
peaks. The dwell counter never reaches 500 ms unless the user shakes
hard.

The firmware tracks `last_above_threshold_tick` — the most recent sample
where `deviation > 0.06 g²`. The dwell counter is only reset when
`HAL_GetTick() - last_above_threshold_tick > MOVEMENT_DEBOUNCE_MS`
(default **300 ms**).

This means: a 500 ms dwell can complete even if the motion dips below
threshold for up to 300 ms at a time. Any sub-300 ms gap is absorbed.

The 300 ms tolerance is large enough to bridge normal motion microbreaks
yet small enough that idle hand-jitter does not accumulate into a false
trigger.

---

## Telemetry timing — duty-cycled drain (ADR-021)

ADR-021 turned off the continuous live stream. The radio is powered down
during both IDLE and MOVING; it only powers on to drain a finished capture.
Each main-loop iteration:

1. Sensors are read (cached env every 500 ms; accel every iteration).
2. FSM is updated; MOVING samples flow into the ISM330 FIFO → PSRAM ring.
3. On MOVING→IDLE (mission end), or on an IDLE-snapshot cadence (10 s every
   10 min), or on a PSRAM watermark, the radio powers on and the buffered
   capture is drained on UDP `:5684` (see `wire_protocol.md §2`).

The live 24-byte `PludosTelemetry` path on `:5683` still exists in code but
is gated on `wifi_driver_initialized`, which is only set during a drain
window — so in practice no per-loop telemetry is transmitted. The signal
PLUDOS keeps is the drained PSRAM capture, not the live datagram.

Per-shuttle collision mitigation now lives in the drain path: a
1.0–15.0 s pseudo-random jitter before each drain (seeded from the device
UID) decorrelates shuttles that finish missions at the same time, and a
warm-up burst absorbs the post-power-on ARP/MAC-learning loss window.

---

## Accelerometer threshold

- **Sensor:** ISM330DHCX, 6-axis IMU on I2C2 at address 0x6B (SA0=VDD on
  IOT02A), left-shifted to 0xD6 in firmware.
- **Threshold:** `0.06 g²` — squared-magnitude comparison. The firmware
  computes `ax² + ay² + az²` in g² and compares directly, equivalent to a
  resultant magnitude of `√(1 + 0.06) − 1 ≈ 0.0296 g` deviation from
  gravity. No square root computed (avoids floating-point cost on M33).
- **Dwell to enter MOVING:** 500 ms continuous above threshold, with
  300 ms debounce.
- **Dwell to exit MOVING:** 20 s with no above-threshold sample.

The squared-magnitude formulation comes from the wire layout — the
gateway receives raw `accel_x/y/z` and is free to compute any derived
feature (jerk, RMS, peak-to-peak) for ML training. The FSM threshold is
deliberately coarse: it gates the transmit rate, not the data quality.

---

## Environmental sensor caching

HTS221 (temp/humidity) and LPS22HH (pressure) read latency is 5–10 ms
each over I²C2. Reading both at 50 Hz would consume more than one full
sample period.

The firmware caches the last successful env read and refreshes it every
500 ms (`ENV_READ_PERIOD_MS`). The cached temp/humidity/pressure values are
stamped onto the data that actually leaves the shuttle: the idle-snapshot
`DrainBegin` carries `temp_c_x100` and `pressure_hpa_x10` (and any live
`PludosTelemetry` packet sent during a drain window carries temp/humidity).
Reading at 2 Hz is acceptable because temperature, humidity, and pressure
change on seconds-to-minutes timescales.

Sensor unavailability is signalled by the wire sentinel `0x7FFF` (32767) in
any `int16_t` sensor field — accel, gyro, temp, and humidity alike. The
gateway converts any field equal to 32767 to `NaN` before buffering. See
`wire_protocol.md §1` for the full sentinel contract. `pressure_hpa` is no
longer on the wire (ADR-015 v2).

---

## Configuration constants

| Constant | Value | Location | Purpose |
|---|---|---|---|
| `MOVEMENT_THRESHOLD_G2` | `0.06f` | `main.c` | Above this, sample counts toward dwell |
| `MOVEMENT_DWELL_MS` | `500U` | `main.c` | Continuous-above duration to enter MOVING |
| `MOVEMENT_DEBOUNCE_MS` | `300U` | `main.c` | Tolerance for sub-threshold dips during dwell |
| `NO_MOVEMENT_TIMEOUT_MS` | `20000U` | `main.c` | No-above-threshold duration to exit MOVING |
| `SAMPLE_PERIOD_IDLE_MS` | `100U` | `main.c` | 10 Hz internal sampling in IDLE |
| `SAMPLE_PERIOD_MOVING_MS` | `20U` | `main.c` | 50 Hz FSM motion-poll in MOVING (not a TX rate) |
| `TX_PERIOD_IDLE_MS` | `10000U` | `main.c` | legacy IDLE TX cadence — only applies while the radio is on during a drain (ADR-021) |
| `ENV_READ_PERIOD_MS` | `500U` | `main.c` | 2 Hz env-sensor cache refresh |
| `TELEMETRY_PORT` | `5683U` | `main.c` | Single unified UDP port |

---

## Open items

- **P2-1 Beacon discovery:** end-to-end. Firmware listens for
  `PLUDOS-GW:<ip>` UDP broadcasts on port 5000 — at boot
  (`BEACON_MAX_RETRIES × BEACON_TIMEOUT_MS = 30 s`), after every WiFi
  reconnect (short probe, `BEACON_RETRY_TIMEOUT_MS = 500 ms`), and
  periodically during IDLE (`BEACON_RETRY_PERIOD_MS = 30 s`). The
  `JETSON_IP` define in `wifi_credentials.h` is the compile-time
  fallback used only when the very first boot probe times out.
- **P2-2 ADC power sensing:** `power_mw` was removed from the wire in
  ADR-015 v2 — the firmware no longer estimates or transmits power. The
  gateway-side `POWER_*_MW` estimate was also removed in the schema-v4
  raw-only cull, so there is no shuttle power/energy figure at all. Real
  INA3221/Alumet measurement on the Jetson side is tracked in ADR-011.
- **Threshold tuning:** `MOVEMENT_THRESHOLD_G2 = 0.06f` is conservative.
  If false-trigger rate or missed-mission rate becomes an issue with real
  shuttle motion, retune against logged data — the squared-magnitude axis
  in the Parquet files makes this an offline analysis.
