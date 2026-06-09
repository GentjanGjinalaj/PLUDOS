# Wire Protocol

Defines the exact byte layouts for all data exchanged between the STM32
edge nodes, the Jetson gateway, and the central server.

**Version:** v3 (ADR-016). Adds ISM330 gyroscope (gx/gy/gz); replaces
float32 sensor fields with int16 scaled integers to halve per-field wire
cost (28 → 24 bytes despite adding 3 gyro axes). The previous CoAP CON +
NC-UDP split was removed by ADR-015.

> **ADR-021 note — no continuous stream.** The radio is now off except to
> drain finished captures, so there is **no fixed-Hz live TX**. The
> `PludosTelemetry` byte layout below is still current, but it is sent in
> bursts during a drain window, not at 50 Hz / 0.1 Hz. MOVING signal itself
> is captured into PSRAM at **accel 3332 Hz / gyro 416 Hz (≈8:1)** and
> arrives in the separate `cap_*` drain files (see ADR-020/021 and
> `docs/sampling_strategy.md`). `SAMPLE_PERIOD_MOVING_MS=20` (50 Hz) is the
> internal FSM motion-poll cadence only.

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

| State | Internal FSM poll | Captured data rate (to PSRAM, drained later) |
|---|---|---|
| `STATE_IDLE` (state=0) | 10 Hz (`SAMPLE_PERIOD_IDLE_MS=100`) | 12.5 Hz snapshot, 10 s every 10 min |
| `STATE_MOVING` (state=1) | 50 Hz (`SAMPLE_PERIOD_MOVING_MS=20`) | accel 3332 Hz / gyro 416 Hz (≈8:1) |

> **Note:** `SAMPLE_PERIOD_MOVING_MS=20` (50 Hz) is the internal motion-detection
> poll only — **not** a TX or data rate. MOVING signal is captured into the ISM330
> FIFO → PSRAM at accel 3332 Hz / gyro 416 Hz and drained after the run (ADR-021);
> IDLE produces a 12.5 Hz snapshot. The radio is off except to drain.

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
- `accel_x/y/z` — ISM330DHCX accelerometer, ±2 g FS. In the live/FSM read
  the ODR is 104 Hz with on-chip LPF2 (cutoff ODR/10 ≈ 10.4 Hz), alias-free
  over the 0–10 Hz motion band. (High-rate MOVING capture uses ODR 3332 Hz;
  see ADR-021.) FSM uses magnitude deviation > 0.05 g² for movement detection.
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

## 2. High-rate capture drain (STM32 → Jetson, UDP 5684)

ADR-020/021. After a mission ends (MOVING→IDLE), the STM32 drains the raw
ISM330DHCX FIFO words buffered in PSRAM to the gateway as a burst of UDP
datagrams on port **5684** (the legacy `NonCriticalPayload` use of this port was
removed by ADR-015; the port is now reused for the drain). The live 24-byte
`PludosTelemetry` path on 5683 (§1) is untouched.

**Phase 1 (current): blast-only.** STM sends `DRAIN_BEGIN` (×3), then all chunks
back-to-back, then `DRAIN_END` (×3). No back-channel. The gateway reassembles by
`chunk_seq`, validates each chunk's CRC32, and writes one Parquet per
`(shuttle_id, mission_id)` on `DRAIN_END` (or a quiet timeout), marking
`complete=false` and recording gap ranges if any chunk is missing.
**Phase 2 (planned): NAK selective-repeat ARQ** (`sampling_strategy.md §9`) layers
`NAK`/`ACK_COMPLETE` back-channel packets (types 4/5) on top of this same frame
format without changing the on-wire layout of types 1–3.

### Common framing
- All multi-byte fields **little-endian** (both ends are LE; structs are packed).
- Every packet starts with `u32 magic = 0x52444C50` (ASCII `"PLDR"` in memory
  order `P,L,D,R`) and `u8 type`.
- One UDP datagram = one packet. Max datagram 1418 B (18 B chunk header + 1400 B
  payload) — well under the 1472 B non-fragmenting limit (§1 / `sampling_strategy.md §1`).

### Packet types
```c
/* type=1 DRAIN_BEGIN — control, sent x3 for robustness. 36 bytes. */
typedef struct __attribute__((packed)) {
  uint32_t magic;        /* 0x52444C50                                   */
  uint8_t  type;         /* 1                                            */
  uint8_t  shuttle_id;
  uint16_t mission_id;
  uint16_t total_chunks; /* number of CHUNK packets that follow          */
  uint16_t odr_accel_hz; /* MOVING: 3332; idle snapshot: 12 (see below)  */
  uint16_t odr_gyro_hz;  /* MOVING: 416;  idle snapshot: 12 (see below)  */
  int16_t  temp_c_x100;  /* idle snapshot env stamp ×100; 0x7FFF=invalid */
  uint16_t pressure_hpa_x10; /* idle snapshot env stamp ×10; 0=invalid   */
  uint8_t  is_idle_snapshot; /* 1 = low-rate 12.5 Hz idle snapshot,      */
                         /*     0 = MOVING mission (ADR-021 §1)           */
  uint8_t  _pad;
  uint32_t byte_count;   /* total payload bytes across all chunks        */
  uint32_t word_count;   /* FIFO words = byte_count / 7                   */
  uint32_t t0_tick_ms;   /* capture-start HAL_GetTick()                   */
  uint32_t tx_tick_ms;   /* drain-time HAL_GetTick(); capture_age =       */
                         /* tx_tick - t0_tick. Gateway stamps capture     */
                         /* wall = BEGIN_arrival - capture_age (exact,    */
                         /* same-boot, no NTP offset needed)              */
} DrainBegin_t;
/* Idle snapshots (ADR-021 §1) run accel+gyro both at 12.5 Hz. The integer
 * odr_* fields can't carry .5, so when is_idle_snapshot=1 the gateway uses
 * the authoritative 12.5 Hz rate and ignores the rounded odr_* values. The
 * temp/pressure stamp lets Grafana chart the idle environment even though the
 * live 5683 telemetry stream is off during IDLE (ADR-021 Phase 1). */

/* type=2 CHUNK — data. 18-byte header + payload (<=1400 B = <=200 FIFO words). */
typedef struct __attribute__((packed)) {
  uint32_t magic;        /* 0x52444C50                                   */
  uint8_t  type;         /* 2                                            */
  uint8_t  shuttle_id;
  uint16_t mission_id;
  uint16_t chunk_seq;    /* 0 .. total_chunks-1                          */
  uint16_t total_chunks;
  uint16_t payload_len;  /* <=1400, multiple of 7 except possibly last   */
  uint32_t crc32;        /* zlib/IEEE CRC32 of the payload bytes only     */
  /* uint8_t payload[payload_len] — raw 7-byte FIFO words [tag,Xl,Xh,Yl,Yh,Zl,Zh] */
} DrainChunkHdr_t;

/* type=3 DRAIN_END — control, sent x3. 16 bytes. */
typedef struct __attribute__((packed)) {
  uint32_t magic;        /* 0x52444C50                                   */
  uint8_t  type;         /* 3                                            */
  uint8_t  shuttle_id;
  uint16_t mission_id;
  uint16_t total_chunks;
  uint16_t _pad;
  uint32_t crc32_all;    /* CRC32 of the full concatenated payload, 0 = unused */
} DrainEnd_t;
```

### Payload semantics
Chunk payloads are the **raw FIFO byte stream**, concatenated in `chunk_seq`
order, reproducing the PSRAM ring contents exactly. Each 7-byte word is
`[tag, X_L, X_H, Y_L, Y_H, Z_L, Z_H]`; `tag >> 3` selects sensor
(`0x02`=accel `XL_NC`, `0x01`=gyro `GYRO_NC`). Payloads are sized at 1400 B =
200 words so word boundaries never split across chunks (the last chunk holds the
remainder). Axes are int16 little-endian at the ISM330 FS scale (±2 g accel,
±250 dps gyro for the current capture config).

### Gateway Parquet schema (one file per completed mission)
Demux accel/gyro by tag into **separate streams — do not upsample/pad** the gyro
to the accel rate. Per-sample time is derived, never per-sample stamped:
`t_ms = t0_wall + sample_index * 1000 / odr` (per stream, using its own ODR).
Mission metadata columns: `shuttle_id, mission_id, odr_accel_hz, odr_gyro_hz,
t0_wall_ms, is_idle_snapshot (bool), temp_c, pressure_hpa, complete (bool),
missing_chunk_ranges`. **`mission_id` here (and the `_m<id>` filename suffix) is a
gateway-assigned unix-ms id, not the on-wire firmware `mission_id`** — the latter
resets to 0 on every STM32 reset, so it is used only for in-flight reassembly
grouping, never for filenames or cross-reboot dedup (see `decisions.md` ADR-021). `odr_*` are float (idle snapshots are 12.5 Hz);
`temp_c`/`pressure_hpa` are NaN for MOVING missions (stamped on idle snapshots
only).

### CRC32
Standard zlib/IEEE CRC32 (reflected, poly `0xEDB88320`, init `0xFFFFFFFF`, final
XOR `0xFFFFFFFF`) — matches Python `zlib.crc32`. The STM uses a software bitwise
implementation; the drain runs during IDLE where the CPU cost is free.

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

Also written: `fl_phases` (per-phase summary: load/train/round_total, by `AlumetProfiler`) and
`stm_mission` (per-shuttle mission summary: `packets`/`duration_ms`, by the data-engine).
See `docs/ANALYTICS.md §3` for full schemas.

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
