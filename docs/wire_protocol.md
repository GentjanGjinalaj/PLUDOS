# Wire Protocol

Defines the exact byte layouts for all data exchanged between the STM32
edge nodes, the Jetson gateway, and the central server.

---

## 1. CoAP Critical Payload (STM32 → Jetson)

Used for vibration, accelerometer, power, and status data. Sent as a
**CoAP CON POST** to `udp://<JETSON_IP>:5683/vib`. The gateway ACKs with
`2.04 Changed`. If no ACK arrives within the timeout, the firmware retries
up to 4 times with exponential backoff (2 s / 4 s / 8 s / 16 s).

### Struct layout

```c
/* 39-byte packed binary payload — matches data-engine.py struct '<12sHIBfffff' */
typedef struct __attribute__((packed)) {
    char     shuttle_id[12];   /* null-padded ASCII, e.g. "STM32-Alpha\0" */
    uint16_t sequence_id;      /* monotonic packet counter per shuttle     */
    uint32_t tick_ms;          /* HAL_GetTick() — ms since STM32 boot      */
    uint8_t  mission_active;   /* 1 = shuttle moving, 0 = mission ended    */
    float    ram_usage_pct;    /* STM32 SRAM buffer fill % (0–100)         */
    float    accel_x;          /* Accelerometer X axis, g                  */
    float    accel_y;          /* Accelerometer Y axis, g                  */
    float    accel_z;          /* Accelerometer Z axis, g                  */
    float    power_mw;         /* Power consumption mW (150.0 placeholder) */
} CriticalPayload_t;           /* Total: 39 bytes                          */
```

Python unpack string: `struct.unpack('<12sHIBfffff', data)`

**Float field order:** `ram_usage_pct, accel_x, accel_y, accel_z, power_mw`.
When updating `data-engine.py` unpacking, assign tuple positions accordingly.

### Field notes

- `shuttle_id`: identifies which STM32 / shuttle sent this packet. The
  gateway uses it to maintain per-shuttle NTP offset and buffer state.
- `sequence_id`: wraps at 65535. The gateway uses `(shuttle_id, sequence_id)`
  to sort packets before writing Parquet. Never reset mid-mission.
- `tick_ms`: relative to STM32 boot (HAL_GetTick). The gateway converts to
  absolute time using a per-shuttle NTP offset computed on first packet.
- `mission_active = 0`: signals end of mission. Gateway sorts buffer and
  writes Parquet on receipt.
- `ram_pct`: allows the gateway to monitor STM32 SRAM pressure. At 70%,
  the STM32 triggers a flush; at 95%, it suspends sampling.
- `power_mw`: state-based estimate from `POWER_EstimateMilliwatts()` —
  MCU run (~15 mA) + I2C sensors (~2 mA) + WiFi idle (~10 mA) or TX (~200 mA)
  at 3.3V. Accuracy ±40%. No ADC shunt is wired; see P2-2 in `current_problems.md`
  for the long-term INA219 path.

---

## 2. UDP Non-Critical Payload (STM32 → Jetson)

Used for temperature, humidity, and pressure. Sent as raw UDP (no ACK,
no retry). Sent during `STATE_IDLE` only.

**Status:** implemented. HTS221 reads temp/humidity; LPS22HH reads pressure.
Both on I2C2. Packet dropped if HTS221 unavailable. `pressure_hpa = 0.0`
is the sentinel value for LPS22HH unavailable or data-not-ready.

```c
typedef struct __attribute__((packed)) {
    char     shuttle_id[12];  /* identifies source shuttle for gateway correlation */
    uint16_t sequence_id;     /* monotonic counter shared with CoAP sequence space */
    uint32_t tick_ms;         /* HAL_GetTick() at sensor read time                 */
    float    temp_c;          /* °C from HTS221                                    */
    float    humidity_pct;    /* % RH from HTS221, clamped [0, 100]               */
    float    pressure_hpa;    /* hPa from LPS22HH; 0.0 = sensor unavailable        */
} NonCriticalPayload_t;       /* total: 30 bytes */
```

Python unpack string: `struct.unpack('<12sHIfff', data)`

**⚠ Breaking change from previous 26-byte format.** Update `data-engine.py`
NonCritical unpack path to use `'<12sHIfff'` and handle the `pressure_hpa`
field (index 5 in the unpacked tuple).

---

## 3. Gateway → InfluxDB (energy metrics)

Written by `AlumetProfiler` in `client.py` during `model.fit()`.

- **Measurement:** `fl_energy`
- **Tags:** `fl_round` (Flower round number), `device` (`jetson-<hostname>`)
- **Fields:** `power_w` (watts), `energy_j` (cumulative joules)
- **Precision:** 10 Hz samples during training

**Current state:** values are mocked (`random.uniform(25, 45)` W in
TEST_MODE, `12.0` W in production). Real sensor integration is ADR-011.

---

## 4. Flower model parameters (Jetson → Server)

Each Flower round, the Jetson client serialises the trained XGBoost model
and sends it to the server as `NumPy` parameter bytes.

- **Format:** `booster.save_raw("json")` → raw bytes
- **Transport:** gRPC over Tailscale VPN (Flower handles this)
- **Server aggregation:** currently selects `max(payloads, key=len)` —
  the largest booster wins. This is selection, not aggregation (see ADR-010
  in `decisions.md`).

---

## 5. Retry and reliability rules

| Path | Protocol | Retry | Max attempts | Notes |
|---|---|---|---|---|
| STM32 → Jetson (critical) | CoAP CON | Manual app-layer | 4 | 2/4/8/16 s backoff |
| STM32 → Jetson (non-critical) | Raw UDP | None | 1 | Drop on loss |
| Jetson → Server (FL) | gRPC/Flower | Flower built-in | Configurable | Over Tailscale |
| Jetson → InfluxDB | HTTP | None currently | 1 | Sync write |

Note: the STM32 uses a manual application-layer retry loop rather than
RFC 7252 native CoAP retransmission. This is a known divergence from the
design intent (documented in `architecture.md`).
