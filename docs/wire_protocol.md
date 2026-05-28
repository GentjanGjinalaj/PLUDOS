# Wire Protocol

Defines the exact byte layouts for all data exchanged between the STM32
edge nodes, the Jetson gateway, and the central server.

**Version:** v3 (ADR-016). Adds ISM330 gyroscope (gx/gy/gz); replaces
float32 sensor fields with int16 scaled integers to halve per-field wire
cost (28 → 24 bytes despite adding 3 gyro axes). The previous CoAP CON +
NC-UDP split was removed by ADR-015.

---

## 1. PludosTelemetry (STM32 → Jetson, UDP 5683)

Single 24-byte payload sent as raw UDP datagram. All sensor values are
`int16_t` scaled integers — halves wire cost vs float32 while delivering
2 decimal places of precision, sufficient for ±2 g accel and ±250 dps
gyro. `power_mw` is not transmitted; gateway derives it from `state`.

### Sentinel

`0x7FFF` (32767) in **any** `int16_t` sensor field means that sensor was
unavailable at sample time. Gateway converts to `NaN` before buffering.
No field-specific sentinel needed.

### Struct layout

```c
/* 24-byte packed binary payload — matches data-engine.py struct '<BHIBhhhhhhhh'. */
typedef struct __attribute__((packed)) {
    uint8_t  shuttle_id;      /* 1-based integer                                  */
    uint16_t sequence_id;     /* monotonic per-shuttle, wraps at 65535            */
    uint32_t tick_ms;         /* HAL_GetTick() at sample time                     */
    uint8_t  state;           /* 0 = STATE_IDLE, 1 = STATE_MOVING                 */
    int16_t  accel_x;         /* g × 100; 0x7FFF = ISM330 unavailable             */
    int16_t  accel_y;         /* g × 100                                           */
    int16_t  accel_z;         /* g × 100                                           */
    int16_t  gyro_x;          /* dps × 100; 0x7FFF = ISM330 unavailable           */
    int16_t  gyro_y;          /* dps × 100                                         */
    int16_t  gyro_z;          /* dps × 100                                         */
    int16_t  temp_c;          /* °C × 100; 0x7FFF = HTS221 unavailable            */
    int16_t  humidity_pct;    /* %RH × 10;  0x7FFF = HTS221 unavailable           */
} PludosTelemetry_t;          /* Total: 24 bytes                                  */
```

Python unpack string: `struct.unpack('<BHIBhhhhhhhh', data)`

### Decode (gateway)

```python
val / 100.0  # accel (g), gyro (dps), temp (°C)
val / 10.0   # humidity (%RH)
# NaN if val == 32767 for any field
```

### Sample rates

| State | Sampling rate (internal) | Transmit rate (over UDP) |
|---|---|---|
| `STATE_IDLE` (state=0) | 10 Hz | **0.1 Hz** (every 100th sample, `TX_PERIOD_IDLE_MS=10000`) |
| `STATE_MOVING` (state=1) | 10 Hz | **10 Hz** (every sample, `SAMPLE_PERIOD_MOVING_MS=100`) |

> **Note (commit 3e99444):** TX rates were reduced from the original design (50 Hz MOVING / 1 Hz IDLE)
> to conserve WiFi bandwidth and radio duty cycle. At 10 Hz MOVING, bandwidth per shuttle is
> 10 × 24 B = 240 B/s. The gateway buffer limits are sized accordingly.

### Field notes

- `shuttle_id` — 1-based integer. Set via `SHUTTLE_ID` in
  `wifi_credentials.h`. Gateway maps to a name via `SHUTTLE_NAMES` env var.
- `sequence_id` — `uint16` counter, wraps 65535 → 0. Gateway unwraps to
  `seq = sequence_id + wrap_count × 65536` for Parquet sort key (ADR-009).
- `tick_ms` — `HAL_GetTick()`, ms since STM32 boot. Gateway anchors to wall
  clock via a per-shuttle NTP offset refreshed every `NTP_REFRESH_INTERVAL`
  packets (default 100). Converted to UTC `pd.Timestamp` at flush.
- `state` — `0` (IDLE) or `1` (MOVING). Triggers mission-end Parquet flush
  after 30 s of IDLE following any MOVING run.
- `accel_x/y/z` — ISM330DHCX accelerometer, ±2 g FS. AC content captures
  vibration (bearing wear, motor noise) up to 13 Hz Nyquist at 26 Hz ODR.
  FSM uses magnitude deviation > 0.05 g² for movement detection.
- `gyro_x/y/z` — ISM330DHCX gyroscope, ±250 dps FS, 8.75 mdps/LSB
  (DS13281 Table 3). `gyro_z` = yaw rate (turns/curves); `gyro_x/y` =
  torsional vibration from motor/bearing faults.
- `temp_c` — HTS221, °C × 100. 0x7FFF if sensor unavailable.
- `humidity_pct` — HTS221 %RH × 10. 0x7FFF if unavailable.
- `power_mw` — **not transmitted**. Derived from `state`:
  `POWER_IDLE_MW` (default 89 mW) or `POWER_MOVING_MW` (default 260 mW).
  ADR-011 tracks the path to real INA3221/Alumet measurements.

### No reliability layer

Each `sendto` is fire-and-forget. The packet either reaches the gateway
within one WiFi hop or it is lost. There is no per-packet ACK, no retry,
no `mission_active = 0` end-marker. The continuous stream (24 B every
20 ms during MOVING, every 1 s during IDLE) is the reliability mechanism:
losing one sample is invisible because the next one arrives moments later.

This is a deliberate trade-off, see ADR-015: liveness of the FSM and
visibility of environmental data take priority over per-packet delivery.

---

## 2. UDP 5684 (Legacy NonCriticalPayload) — DEPRECATED

Removed by ADR-015. The 30-byte `NonCriticalPayload` struct on port 5684
no longer exists. `temp_c` and `humidity_pct` are now part of `PludosTelemetry`
in §1 and arrive at the gateway with every packet. `pressure_hpa` was dropped
entirely from the wire in the v2 refinement — the LPS22HH is still read on the
STM32 for local UART debug logging but is not transmitted.

Gateway port 5684 listener has been removed in `data-engine.py`.

---

## 3. Energy metrics → InfluxDB

Written by `AlumetProfiler` in `client.py` (Jetson) and the `alumet` container
in `server/compose.yaml` (server). All devices share one measurement so
Grafana queries span devices with a single `filter`.

- **Measurement:** `fl_energy` (fixed — do not change; Grafana dashboards depend on it)
- **Tags:**
  - `fl_round` — Flower round number, e.g. `"1"`, `"2"`, `"3"`
  - `device` — hostname; Jetson uses `jetson-<hostname>`, server uses `server`
  - `nvpmodel` — NVPModel power mode at profiler init (Jetson only; `"N/A"` on server)
- **Fields:**
  - `power_gpu_w` — GPU rail watts (Jetson: VDD_GPU; server: `0.0` if no discrete GPU)
  - `power_cpu_w` — CPU rail watts (Jetson: VDD_CPU; server: Intel RAPL package power)
  - `power_total_w` — total system watts
  - `energy_j` — cumulative joules integrated as `power × Δt` since round start
- **Precision:** nanosecond timestamps, 10 Hz sample rate during training

Also written by `AlumetProfiler`: `fl_phases` (per-phase summary: load/train/round_total) and
`stm_mission` (per-shuttle mission energy). See `docs/ANALYTICS.md §3` for full schemas.

**Status (ADR-011):** Phase 1 done — `tegrastats` on Jetson, Intel RAPL on server.
Phase 2 scaffolded — Alumet relay sidecar built, relay flags confirmed, hardware build pending.
See ADR-011 in `docs/decisions.md`.

---

## 4. Flower model parameters (Jetson → Server)

Each Flower round, the Jetson client serialises the trained XGBoost model
and sends it to the server as `NumPy` parameter bytes.

- **Format:** `booster.save_raw("json")` → raw bytes
- **Transport:** gRPC over Tailscale VPN (Flower handles this)
- **Server aggregation:** horizontal tree-set union (ADR-010 Option A).
  Each client's booster trees are concatenated, tree IDs re-sequenced, and
  the merged booster validated before broadcast. Single-client rounds are a
  no-op passthrough. See `server/server.py _merge_boosters()`.

---

## 5. Retry and reliability rules

| Path | Protocol | Retry | Max attempts | Notes |
|---|---|---|---|---|
| STM32 → Jetson (telemetry) | Raw UDP | None | 1 | Loss tolerated, ADR-015 |
| Jetson → Server (FL) | gRPC/Flower | Flower built-in | Configurable | Over Tailscale |
| Jetson → InfluxDB | HTTP | None currently | 1 | Sync write |

ADR-015 removes the application-layer CoAP retry. The STM32 firmware no
longer carries any CoAP code path.
