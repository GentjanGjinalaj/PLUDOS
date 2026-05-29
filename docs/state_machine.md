# STM32 State Machine

Defines the idle/moving FSM that controls sampling rate and telemetry
transmission rate on the STM32U585 edge node.

**Version:** v2 (ADR-015). The previous CoAP buffer-and-flush model is
replaced by a continuous unified UDP stream. There is no buffer.

---

## States

### STATE_IDLE

- **Internal sampling rate:** 10 Hz (every 100 ms, `SAMPLE_PERIOD_IDLE_MS=100`) —
  needed so the FSM can detect a movement dwell at all
- **Telemetry transmit rate:** 0.1 Hz (`TX_PERIOD_IDLE_MS=10000`) — one
  `PludosTelemetry` UDP packet every 10 s carries accel, gyro, temp, and humidity.
  `pressure_hpa` and `power_mw` are no longer on the wire (ADR-015 v2); shuttle
  power/energy is not estimated on the gateway either (the `POWER_*_MW` placeholder
  was removed in the schema-v4 raw-only cull).
- **Entry condition:** no above-threshold accelerometer sample for **20 s**
- **Actions on entry:** none beyond logging the transition; the next loop
  iteration starts streaming at 0.1 Hz with `state = 0`

### STATE_MOVING

- **Internal sampling rate:** 50 Hz target (every 20 ms, `SAMPLE_PERIOD_MOVING_MS=20`)
- **Telemetry transmit rate:** 50 Hz target — every sample is sent immediately;
  the synchronous UDP `sendto` self-throttles to the WiFi ceiling if the radio
  can't sustain 50 Hz
- **Entry condition:** accelerometer deviation `> 0.05 g²` continuously
  for **500 ms** (with a 300 ms debounce tolerance — see below)
- **Actions on entry:** none beyond logging the transition; the next loop
  iteration starts streaming at 50 Hz with `state = 1`

> **Rate note:** MOVING runs at 50 Hz (`SAMPLE_PERIOD_MOVING_MS=20`). The ISM330
> ODR was raised to 104 Hz with its on-chip LPF2 (cutoff ODR/10 ≈ 10.4 Hz) so the
> 50 Hz stream is alias-free below the 25 Hz Nyquist. Whether 50 Hz is sustained
> end-to-end depends on the (unmeasured) WiFi throughput ceiling.

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
produces accelerometer samples that briefly dip below 0.05 g² between
peaks. The dwell counter never reaches 500 ms unless the user shakes
hard.

The firmware tracks `last_above_threshold_tick` — the most recent sample
where `deviation > 0.05 g²`. The dwell counter is only reset when
`HAL_GetTick() - last_above_threshold_tick > MOVEMENT_DEBOUNCE_MS`
(default **300 ms**).

This means: a 500 ms dwell can complete even if the motion dips below
threshold for up to 300 ms at a time. Any sub-300 ms gap is absorbed.

The 300 ms tolerance is large enough to bridge normal motion microbreaks
yet small enough that idle hand-jitter does not accumulate into a false
trigger.

---

## Telemetry timing — no jitter, no buffer

ADR-015 removed the jitter window and the SRAM buffer entirely. Each
loop iteration:

1. Sensors are read (cached env every 500 ms; accel every iteration).
2. FSM is updated.
3. If a transmit is due (every 20 ms in MOVING, every 10 s in IDLE), the
   firmware calls `MX_WIFI_Socket_sendto` with the 24-byte packet and
   returns immediately.

No ACK is awaited. The `sendto` call returns within ~1 ms in normal
operation and within the 10 s MX_WIFI IPC timeout in the worst case. A
WiFi outage does not stall the FSM — the next iteration simply tries
again.

Per-shuttle collision mitigation (when many shuttles enter IDLE
simultaneously) is no longer needed: the 0.1 Hz IDLE rate × random
boot-tick phase gives natural spread, and a single dropped UDP packet
during a momentary burst is invisible because the next packet arrives
within 10 s anyway.

---

## Accelerometer threshold

- **Sensor:** ISM330DHCX, 6-axis IMU on I2C2 at address 0x6B (SA0=VDD on
  IOT02A), left-shifted to 0xD6 in firmware.
- **Threshold:** `0.05 g²` — squared-magnitude comparison. The firmware
  computes `ax² + ay² + az²` in g² and compares directly, equivalent to a
  resultant magnitude of `√(1 + 0.05) − 1 ≈ 0.0247 g` deviation from
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
500 ms (`ENV_READ_PERIOD_MS`). The cached values are stamped into every
outgoing `PludosTelemetry` packet, so the gateway sees env data at the
full transmit rate even though the sensors are physically read at 2 Hz.
This is acceptable because temperature, humidity, and pressure change on
seconds-to-minutes timescales.

Sensor unavailability is signalled by the wire sentinel `0x7FFF` (32767) in
any `int16_t` sensor field — accel, gyro, temp, and humidity alike. The
gateway converts any field equal to 32767 to `NaN` before buffering. See
`wire_protocol.md §1` for the full sentinel contract. `pressure_hpa` is no
longer on the wire (ADR-015 v2).

---

## Configuration constants

| Constant | Value | Location | Purpose |
|---|---|---|---|
| `MOVEMENT_THRESHOLD_G2` | `0.05f` | `main.c` | Above this, sample counts toward dwell |
| `MOVEMENT_DWELL_MS` | `500U` | `main.c` | Continuous-above duration to enter MOVING |
| `MOVEMENT_DEBOUNCE_MS` | `300U` | `main.c` | Tolerance for sub-threshold dips during dwell |
| `NO_MOVEMENT_TIMEOUT_MS` | `20000U` | `main.c` | No-above-threshold duration to exit MOVING |
| `SAMPLE_PERIOD_IDLE_MS` | `100U` | `main.c` | 10 Hz internal sampling in IDLE |
| `SAMPLE_PERIOD_MOVING_MS` | `20U` | `main.c` | 50 Hz sampling + transmit in MOVING |
| `TX_PERIOD_IDLE_MS` | `10000U` | `main.c` | 0.1 Hz transmit rate in IDLE |
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
- **Threshold tuning:** `MOVEMENT_THRESHOLD_G2 = 0.05f` is conservative.
  If false-trigger rate or missed-mission rate becomes an issue with real
  shuttle motion, retune against logged data — the squared-magnitude axis
  in the Parquet files makes this an offline analysis.
