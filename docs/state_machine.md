# STM32 State Machine

Defines the idle/moving FSM that controls sampling rate, buffer flush
behaviour, and CoAP transmission on the STM32U585 edge node.

---

## States

### STATE_IDLE

- **Sampling rate:** ~2 Hz (low power)
- **Behaviour:** transmit buffered data to Jetson via CoAP CON
- **Entry condition:** no movement detected for **10 consecutive seconds**
- **Actions on entry:** trigger CoAP flush of any buffered samples

### STATE_MOVING

- **Sampling rate:** 50 Hz (high frequency)
- **Behaviour:** buffer samples locally in SRAM; do NOT transmit during movement
- **Entry condition:** accelerometer magnitude exceeds threshold **continuously
  for 500 ms**
- **Rationale:** 500 ms prevents noise spikes from spurious state switches

---

## Transitions

```
                    ┌─────────────────────────────────────────┐
                    │  No movement for 10 s                    │
                    ▼                                          │
              ┌──────────┐                              ┌──────────────┐
  Boot ──────►│  IDLE    │                              │   MOVING     │
              └──────────┘                              └──────────────┘
                    │  Accel threshold > 0.05 g²                │
                    │  continuously for 500 ms                   │
                    └───────────────────────────────────────────►┘
```

---

## SRAM Buffer Management

The firmware maintains a static circular buffer of `CriticalPayload_t`
samples (`sensor_buffer[SENSOR_BUFFER_SIZE]` with `SENSOR_BUFFER_SIZE = 256`).

| Threshold | Fill % | Action |
|---|---|---|
| **Soft flush** | 70% (179 entries) | Trigger CoAP transmission to Jetson |
| **Hard suspend** | 95% (243 entries) | Stop sampling; preserve buffer |
| **Resume** | After IDLE + successful ACK | Resume sampling from 0% |

The 70% flush is triggered in STATE_MOVING without waiting for IDLE. This
prevents SRAM overflow during long missions or when WiFi is intermittent.

At 95%, sampling suspends immediately to protect existing data. The STM32
cannot recover until it returns to STATE_IDLE AND successfully transmits
the buffered data (receives CoAP ACK).

---

## Accelerometer Threshold

- **Sensor:** ISM330DLC, 6-axis IMU, on I2C2 at address 0x6A
- **Threshold:** 0.05 g² resultant magnitude (configurable in firmware)
- **Dwell time for MOVING:** 500 ms continuous above threshold
- **Dwell time for IDLE:** 10 s continuous below threshold

These values are defined as `#define` constants in `main.c`. They were
chosen empirically for warehouse shuttle speed profiles — adjust if deploying
on a different vehicle type.

---

## Transmission Timing (Traffic Mitigation)

To reduce the probability of multiple shuttles transmitting simultaneously:

1. On entry to STATE_IDLE, the STM32 adds a random delay before transmitting.
2. The delay is drawn from a uniform distribution over a configurable window.
3. If the shuttle re-enters STATE_MOVING during the delay, transmission is
   cancelled (data stays buffered).

**Current status:** the random-delay mechanism is designed but the beacon
discovery that would allow dynamic IP assignment is stubbed. The STM32
currently uses a hardcoded `JETSON_IP`.

---

## Open Items

- **ADC power sensing:** `power_mw` field currently hardcoded to `150.0f`.
  The ADC peripheral is not configured in the `.ioc`. Adding it requires
  CubeMX changes (see `pludos-stm32-cubemx` skill).
- **Beacon discovery:** `broadcast_beacon` on UDP port 5000 sleeps indefinitely
  in the current firmware. Zero-touch provisioning (STM32 discovers Jetson IP
  automatically) is deferred.
- **WiFi credentials:** currently defined as `#define` constants in `main.c`.
  Should be moved to `Core/Inc/wifi_credentials.h` (gitignored) to avoid
  committing secrets.
